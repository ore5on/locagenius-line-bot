"""
LocaGenius — マイソク（物件情報シート）処理

・MAISOKU_EXTRACT_PROMPT   : Claude への抽出指示プロンプト
・extract_property          : 画像/PDF から物件情報を JSON で抽出
・calc_age                  : built_year から築年数を計算
・build_investigation_text  : 抽出情報を調査用テキストに組み立て
・format_extracted_info     : 抽出情報を LINE 表示用にフォーマット
"""

import re
import json
import base64
import logging
from datetime import datetime

from core.config import anthropic_client, JST

logger = logging.getLogger(__name__)

# ──────────────────────────────────────
# 抽出プロンプト
# ──────────────────────────────────────
MAISOKU_EXTRACT_PROMPT = f"""この文書は日本の不動産マイソク（物件情報シート）です。
以下のルールに従って情報を抽出し、見つかった項目のみJSON形式で返してください。
見つからない項目はJSONに含めないでください。

【抽出する項目】
- address: 物件住所（都道府県から番地まで）
- building_name: 物件名・マンション名
- property_category: 物件種別。区分マンション・区分所有は「区分」、一棟マンション・一棟アパート・一棟ビル等は「一棟」。判別できない場合は含めない
- land_area: 土地面積（数値のみ、単位㎡。坪表記の場合は×3.306で㎡換算）
- building_area: 建物延床面積（数値のみ、単位㎡。坪表記の場合は×3.306で㎡換算）
- exclusive_area: 専有面積（区分マンション用。数値のみ、単位㎡）
- structure: 構造（RC造・鉄骨造・木造のいずれかに統一）
- rosenka: 路線価（下記ルール参照）
- price: 売出価格（数値のみ、単位万円）
- management_fee: 管理費（区分マンション用。数値のみ、単位円/月）
- repair_fund: 修繕積立金（区分マンション用。数値のみ、単位円/月）
- total_units: 総戸数（数値のみ）
- annual_revenue: 年間賃料収入（一棟物件用。数値のみ、単位万円）

【建築年の抽出ルール】
- age（築年数）は抽出しない。代わりに built_year（建築西暦年）を抽出する
- 「建築年月」「竣工年」「新築年」「築年月」が記載されている場合：
  西暦表記（例：1988年）→ built_year: 1988
  和暦表記は必ず西暦に変換してから抽出すること：
    明治○年 = 1867+○、大正○年 = 1911+○
    昭和○年 = 1925+○（例：昭和63年 → 1925+63 = 1988）
    平成○年 = 1988+○（例：平成3年 → 1988+3 = 1991）
    令和○年 = 2018+○（例：令和2年 → 2018+2 = 2020）
- 「新築」と書いてある場合は built_year: {datetime.now(JST).year}
- 建築年の記載が一切ない場合はbuilt_yearをJSONに含めない

【路線価の抽出ルール】
- 「路線価」と明記されている場合のみ抽出（単位：円/㎡）
- 「坪単価」「売買単価」「公示価格」「評価額」は路線価ではないため抽出しない
- マイソクに路線価の記載がない場合はJSONに含めない

JSONのみ返してください。説明文は不要です。
例：{{"address": "東京都渋谷区恵比寿1-1-1", "land_area": 50.0, "building_area": 80.0, "structure": "RC造", "built_year": 2009}}"""


# ──────────────────────────────────────
# 内部ヘルパー（リトライ付き Claude 呼び出し）
# ──────────────────────────────────────
async def _call_claude_with_retry(**kwargs):
    """core.investigator の call_claude_with_retry と同じロジックの局所版。
    循環 import を避けるため maisoku 内でも独立して定義する。
    """
    import asyncio
    import anthropic as _anthropic
    for attempt in range(3):
        try:
            return anthropic_client.messages.create(**kwargs)
        except _anthropic.APIStatusError as e:
            if e.status_code not in (429, 529):
                raise
            wait     = 20 * (attempt + 1)
            err_type = "Rate limit (429)" if e.status_code == 429 else "Overloaded (529)"
            logger.warning(f"{err_type} on extract. Waiting {wait}s... (attempt {attempt+1}/3)")
            if attempt == 2:
                raise
            await asyncio.sleep(wait)


# ──────────────────────────────────────
# 物件情報抽出
# ──────────────────────────────────────
async def extract_property(media_type: str, content_bytes: bytes) -> dict | None:
    """Claude API でマイソクから物件情報を抽出する（画像・PDF共通）。

    Args:
        media_type: "image/jpeg" または "application/pdf"
        content_bytes: ファイルのバイト列

    Returns:
        抽出した物件情報の dict。失敗時は None。
    """
    b64          = base64.standard_b64encode(content_bytes).decode("utf-8")
    content_type = "document" if media_type == "application/pdf" else "image"
    try:
        response = await _call_claude_with_retry(
            model="claude-sonnet-4-6",
            max_tokens=512,
            messages=[{
                "role": "user",
                "content": [
                    {
                        "type":   content_type,
                        "source": {
                            "type":       "base64",
                            "media_type": media_type,
                            "data":       b64,
                        },
                    },
                    {"type": "text", "text": MAISOKU_EXTRACT_PROMPT},
                ],
            }],
        )
        text      = re.sub(r"```(?:json)?", "", response.content[0].text.strip()).strip()
        m         = re.search(r'\{[\s\S]*\}', text)
        extracted = json.loads(m.group() if m else text)
        logger.info(f"Extracted from {content_type}: {extracted}")
        return extracted
    except Exception as e:
        logger.warning(f"Property extraction failed ({media_type}): {e}")
        return None


# ──────────────────────────────────────
# Webテキストからの抽出（URL読み込み用）
# ──────────────────────────────────────

# 物件情報の手がかりとなるキーワード（出現順で最も早いものを起点にする）
_PROPERTY_KEYWORDS = [
    "所在地", "物件詳細", "延床面積", "土地面積", "築年月", "築年数",
    "構造", "想定利回り", "年間収入", "年間賃料", "販売価格", "売出価格",
    "間取り", "専有面積", "管理費", "修繕積立", "総戸数",
]
_MAX_CHARS = 15_000   # Claude に渡す最大文字数


def _extract_relevant_section(text: str) -> str:
    """物件情報キーワードを起点に関連セクションを抽出する。

    ナビゲーションやフッターなどの定型文字列がページ先頭を埋めているケースで、
    先頭切り詰めだと物件データが届かない問題を回避する。
    """
    if len(text) <= _MAX_CHARS:
        return text

    # キーワードが最初に現れる位置を探す
    earliest = len(text)
    for kw in _PROPERTY_KEYWORDS:
        pos = text.find(kw)
        if 0 <= pos < earliest:
            earliest = pos

    if earliest == len(text):
        # キーワードが見つからなければ先頭から切り詰め
        return text[:_MAX_CHARS]

    # キーワード直前 500 文字（タイトル等を拾う）＋以降を合わせて _MAX_CHARS 文字
    start = max(0, earliest - 500)
    end   = min(len(text), start + _MAX_CHARS)
    return text[start:end]


async def extract_from_text(text: str) -> dict | None:
    """Webページのテキストから物件情報を抽出する。

    Jina Reader 等で取得したプレーンテキスト・Markdownを対象とする。
    内部的には MAISOKU_EXTRACT_PROMPT を流用し、Claude に JSON 抽出させる。

    Args:
        text: Webページのテキスト（Markdown / プレーンテキスト）

    Returns:
        抽出した物件情報の dict。失敗時は None。
    """
    truncated = _extract_relevant_section(text)
    try:
        response = await _call_claude_with_retry(
            model="claude-sonnet-4-6",
            max_tokens=512,
            messages=[{
                "role": "user",
                "content": (
                    "以下は不動産物件ページのテキストです。\n"
                    f"{MAISOKU_EXTRACT_PROMPT}\n\n"
                    "---\n"
                    f"{truncated}"
                ),
            }],
        )
        raw       = re.sub(r"```(?:json)?", "", response.content[0].text.strip()).strip()
        m         = re.search(r'\{[\s\S]*\}', raw)
        extracted = json.loads(m.group() if m else raw)
        logger.info(f"Extracted from URL text: {extracted}")
        return extracted
    except Exception as e:
        logger.warning(f"extract_from_text failed: {e}")
        return None


# ──────────────────────────────────────
# 築年数計算
# ──────────────────────────────────────
def calc_age(extracted: dict) -> int | None:
    """built_year から築年数を Python で正確に計算する（AI に計算させない）"""
    built_year = extracted.get("built_year")
    if built_year is None:
        return None
    current_year = datetime.now(JST).year
    return max(0, current_year - int(built_year))


# ──────────────────────────────────────
# 調査テキスト組み立て
# ──────────────────────────────────────
def build_investigation_text(extracted: dict) -> str:
    """抽出した物件情報を _run_investigation に渡す調査用テキストに組み立てる"""
    parts = []

    address  = extracted.get("address", "")
    building = extracted.get("building_name", "")
    if address:
        parts.append(address)
    elif building:
        parts.append(building)

    if extracted.get("land_area"):
        parts.append(f"土地面積：{extracted['land_area']}㎡")
    if extracted.get("building_area"):
        parts.append(f"建物面積：{extracted['building_area']}㎡")
    if extracted.get("structure"):
        parts.append(f"構造：{extracted['structure']}")

    age = calc_age(extracted)
    if age is not None:
        parts.append(f"築年数：{age}年（{extracted.get('built_year')}年築）")

    if extracted.get("rosenka"):
        parts.append(f"路線価：{extracted['rosenka']}円/㎡")
    if extracted.get("price"):
        parts.append(f"売出価格：{extracted['price']}万円")

    return "\n".join(parts)


# ──────────────────────────────────────
# LINE 表示用フォーマット
# ──────────────────────────────────────
def format_extracted_info(extracted: dict) -> str:
    """抽出した物件情報を LINE メッセージ用に整形する"""
    lines = []
    if extracted.get("building_name"):
        lines.append(f"物件名：{extracted['building_name']}")
    if extracted.get("address"):
        lines.append(f"住所　：{extracted['address']}")
    if extracted.get("exclusive_area"):
        lines.append(f"専有　：{extracted['exclusive_area']}㎡")
    if extracted.get("land_area"):
        lines.append(f"土地　：{extracted['land_area']}㎡")
    if extracted.get("building_area"):
        lines.append(f"建物　：{extracted['building_area']}㎡")
    if extracted.get("structure"):
        lines.append(f"構造　：{extracted['structure']}")
    age = calc_age(extracted)
    if age is not None:
        lines.append(f"築年数：{age}年（{extracted.get('built_year')}年築）")
    if extracted.get("rosenka"):
        lines.append(f"路線価：{extracted['rosenka']:,}円/㎡")
    if extracted.get("price"):
        lines.append(f"売出価格：{extracted['price']}万円")
    if extracted.get("management_fee"):
        lines.append(f"管理費：{extracted['management_fee']:,}円/月")
    if extracted.get("repair_fund"):
        lines.append(f"修繕積立：{extracted['repair_fund']:,}円/月")
    return "\n".join(lines)
