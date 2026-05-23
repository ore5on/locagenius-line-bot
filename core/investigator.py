"""
LocaGenius — Claude + MLIT MCP Server による調査エンジン

全モード共通の調査実行ロジック。
・call_claude_with_retry  : リトライ付き Claude API 呼び出し
・summarize_real_estate   : API 1 レスポンス圧縮
・strip_preamble          : Claude 前置きコメント除去
・run_investigation       : MCP Server 起動 → Claude エージェントループ
"""

import os
import re
import json
import asyncio
import logging
from collections import defaultdict
from datetime import datetime

import anthropic
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from core.config import (
    anthropic_client,
    LIBRARY_API_KEY,
    MCP_SERVER_PATH,
    SYSTEM_PROMPT,
    JST,
)
from core.maisoku    import calc_age
from core.yield_calc import build_income_block, get_yield_context

logger = logging.getLogger(__name__)


# ──────────────────────────────────────
# リトライ付き Claude API 呼び出し
# ──────────────────────────────────────
async def call_claude_with_retry(**kwargs) -> anthropic.types.Message:
    """RateLimitError（429）と Overloaded（529）を最大3回リトライする"""
    for attempt in range(3):
        try:
            return anthropic_client.messages.create(**kwargs)
        except anthropic.APIStatusError as e:
            # 429 RateLimit / 529 Overloaded のみリトライ。それ以外は即再送出
            if e.status_code not in (429, 529):
                raise
            wait     = 20 * (attempt + 1)
            err_type = "Rate limit (429)" if e.status_code == 429 else "Overloaded (529)"
            logger.warning(f"{err_type} on API call. Waiting {wait}s... (attempt {attempt+1}/3)")
            if attempt == 2:
                raise
            await asyncio.sleep(wait)


# ──────────────────────────────────────
# API 1 レスポンス圧縮
# ──────────────────────────────────────
def summarize_real_estate(content: str) -> str:
    """18,000文字超の API 1 レスポンスを売買・賃貸別に数百文字に圧縮する。

    MLIT API では Type（物件種別）と PriceCategory（売買/貸借）が独立したフィールド。
    賃貸データは PriceCategory == '貸借' で識別する。
    """
    try:
        data     = json.loads(content)
        results  = (data.get("data") or {}).get("api_results", [])
        if not results or results[0] is None:
            return content

        features = (results[0].get("data") or {}).get("features", [])
        if not features:
            return content

        sales:   list[dict] = []
        rentals: list[dict] = []
        for f in features:
            props    = f.get("properties") or {}
            category = props.get("PriceCategory") or ""
            if "貸借" in category:
                rentals.append(props)
            else:
                sales.append(props)

        logger.info(
            f"  API1 split: sales={len(sales)}件 rentals={len(rentals)}件 "
            f"(total={len(features)}件)"
        )

        def _build_section(records: list[dict], label: str) -> list[str]:
            if not records:
                return []
            by_type: dict = defaultdict(list)
            for r in records:
                by_type[r.get("Type") or "不明"].append(r)

            section = [f"\n【{label}】（計{len(records)}件）"]
            for tx_type, recs in sorted(by_type.items()):
                prices, unit_prices, periods = [], [], []
                for r in recs:
                    for key in ("TradePrice", "Price", "Rent"):
                        v = r.get(key)
                        if v and str(v).strip() not in ("", "不明"):
                            try:
                                prices.append(int(str(v).replace(",", "").replace("円", "")))
                            except Exception:
                                pass
                            break
                    for key in ("UnitPrice", "PricePerUnit", "RentPerSquareMeter"):
                        v = r.get(key)
                        if v and str(v).strip() not in ("", "不明"):
                            try:
                                unit_prices.append(int(str(v).replace(",", "").replace("円", "")))
                            except Exception:
                                pass
                            break
                    period = r.get("Period") or r.get("Year") or ""
                    if period and str(period) not in ("", "不明"):
                        periods.append(str(period))

                section.append(f"  ■ {tx_type}（{len(recs)}件）")
                if prices:
                    prices.sort()
                    section.append(
                        f"    取引価格：{prices[0]:,}円" if len(prices) == 1
                        else f"    取引価格：{prices[0]:,}〜{prices[-1]:,}円"
                    )
                if unit_prices:
                    unit_prices.sort()
                    section.append(
                        f"    ㎡単価　：{unit_prices[0]:,}円/㎡" if len(unit_prices) == 1
                        else f"    ㎡単価　：{unit_prices[0]:,}〜{unit_prices[-1]:,}円/㎡"
                    )
                if periods:
                    section.append(f"    取引時点：{periods[-1]}")
            return section

        lines  = [f"不動産取引価格情報（計{len(features)}件）"]
        lines += _build_section(sales,   "売買取引")
        lines += _build_section(rentals, "賃貸取引（貸借）")

        summary = "\n".join(lines)
        logger.info(f"  API1 summarized: {len(features)}件 → {len(summary)}文字")
        return summary

    except Exception as e:
        logger.warning(f"  API1 summarization failed: {e}")
        return content


# ──────────────────────────────────────
# 前置きコメント除去
# ──────────────────────────────────────
def strip_preamble(text: str) -> str:
    """Claude の回答から前置きコメント・末尾の問いかけを除去する"""
    idx = text.find("📍")
    if idx != -1:
        text = text[idx:]

    unwanted_patterns = [
        r".*ファイル.*保存.*[？?].*",
        r".*保存.*しますか.*",
        r".*他に.*何か.*[？?].*",
        r".*ご不明な点.*[？?].*",
        r".*お気軽に.*どうぞ.*",
        r".*何かご質問.*[？?].*",
        r".*お役に立て.*",
    ]
    cleaned_lines = []
    for line in text.splitlines():
        if any(re.search(p, line) for p in unwanted_patterns):
            logger.info(f"Stripped unwanted line: {line[:50]}")
            continue
        cleaned_lines.append(line)

    return "\n".join(cleaned_lines).rstrip()


# ──────────────────────────────────────
# メイン調査エンジン
# ──────────────────────────────────────
async def run_investigation(
    address: str,
    coords: tuple[float, float] | None,
    nearest_station: str | None,
    nearest_school: str | None = None,
    nearest_medical: str | None = None,
    mode: str = "location",
    maisoku_data: dict | None = None,
) -> str:
    """MLIT Geospatial MCP Server を起動し、Claude がツールを呼び出して調査を実行する。

    Args:
        address:         調査対象の住所・建物名
        coords:          緯度経度（取得済みの場合）
        nearest_station: 最寄駅（OpenStreetMap 確定値）
        nearest_school:  最寄小学校（OpenStreetMap 確定値）
        nearest_medical: 最寄医療施設（OpenStreetMap 確定値）
        mode:            "location" / "assessment" / "investment_kubun" / "investment_ittou"
        maisoku_data:    マイソク抽出データ（Mode 3 用）

    Returns:
        Claude が生成したレポート文字列
    """
    server_params = StdioServerParameters(
        command="python",
        args=[MCP_SERVER_PATH],
        env={
            **os.environ.copy(),
            "LIBRARY_API_KEY": LIBRARY_API_KEY,
            "PYTHONUNBUFFERED": "1",
            "LOG_LEVEL": "WARNING",
        },
    )

    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            tools_response = await session.list_tools()
            tools = [
                {
                    "name":         t.name,
                    "description":  t.description or t.name,
                    "input_schema": t.inputSchema or {"type": "object", "properties": {}},
                }
                for t in tools_response.tools
            ]
            logger.info(f"MCP tools loaded ({len(tools)}): {[t['name'] for t in tools]}")

            # ── モード別タスク説明文 ──────────────────────────────
            mode_task = {
                "location":         "以下の住所・地名の立地調査をしてください",
                "assessment":       "以下のマンションの資産性評価をしてください",
                "investment_kubun": "以下の区分マンションの投資分析をしてください",
                "investment_ittou": "以下の一棟物件の投資分析をしてください",
            }.get(mode, "以下の住所・地名の立地調査をしてください")

            # ── マイソクデータの追加コンテキスト（Mode 3）────────
            maisoku_context = ""
            if maisoku_data:
                lines = []
                if maisoku_data.get("price"):
                    lines.append(f"売出価格：{maisoku_data['price']}万円")
                if maisoku_data.get("exclusive_area"):
                    lines.append(f"専有面積：{maisoku_data['exclusive_area']}㎡")
                if maisoku_data.get("land_area"):
                    lines.append(f"土地面積：{maisoku_data['land_area']}㎡")
                if maisoku_data.get("building_area"):
                    lines.append(f"延床面積：{maisoku_data['building_area']}㎡")
                if maisoku_data.get("structure"):
                    lines.append(f"構造：{maisoku_data['structure']}")
                age = calc_age(maisoku_data)
                if age is not None:
                    lines.append(f"築年数：{age}年")
                if maisoku_data.get("management_fee"):
                    lines.append(f"管理費：{maisoku_data['management_fee']}円/月")
                if maisoku_data.get("repair_fund"):
                    lines.append(f"修繕積立金：{maisoku_data['repair_fund']}円/月")
                if maisoku_data.get("total_units"):
                    lines.append(f"総戸数：{maisoku_data['total_units']}戸")
                if maisoku_data.get("annual_revenue"):
                    lines.append(f"年間収入：{maisoku_data['annual_revenue']}万円")
                if lines:
                    maisoku_context = "\n\n【マイソク物件情報】\n" + "\n".join(lines)

            # ── Python確定値：想定利回り（同一住所で常に同じ値を渡す）──
            yield_context = get_yield_context(address, maisoku_data, mode=mode)

            # ── Claude へ渡すプロンプトを組み立て ────────────────
            if coords:
                lat, lon = coords
                location_info = (
                    f"{mode_task}：{address}{maisoku_context}\n\n"
                    f"【座標情報（確定値）】\n"
                    f"緯度：{lat}  経度：{lon}\n"
                    f"Googleマップ：https://www.google.com/maps?q={lat},{lon}\n\n"
                    f"【利便性情報（OpenStreetMap確定値）】\n"
                    f"最寄駅：{nearest_station or '－'}\n"
                    f"小学校：{nearest_school or '－'}\n"
                    f"医療　：{nearest_medical or '－'}\n\n"
                    f"{yield_context}\n\n"
                    f"【MCPツール「get_multi_api」呼び出し指示】\n"
                    f"以下を全て順番に呼び出してデータを取得すること。\n"
                    f"共通パラメータ：lat={lat}, lon={lon}, save_file=False\n\n"
                    f"① 公示地価　　　　　　：target_apis=[3], distance=425\n"
                    f"  ※ nullなら広域(distance=425のまま)で再試行。それでもnullなら「－」\n"
                    f"② 洪水リスク　　　　　：target_apis=[26]\n"
                    f"③ 高潮リスク　　　　　：target_apis=[27]\n"
                    f"④ 津波リスク　　　　　：target_apis=[28]\n"
                    f"⑤ 土砂リスク　　　　　：target_apis=[29]\n"
                    f"⑥ 液状化リスク　　　　：target_apis=[25]\n"
                    f"⑦ 用途地域・建蔽率・容積率：target_apis=[5]\n"
                    f"⑧ 防火・準防火地域　　：target_apis=[14]\n"
                    f"⑨ 将来推計人口　　　　：target_apis=[13]\n"
                    f"⑩ 人口集中地区（DID）：target_apis=[30]\n"
                    f"⑪ 駅別乗降客数　　　　：target_apis=[15], distance=425\n"
                    f"⑫ 不動産取引価格　　　：target_apis=[1]\n"
                    f"  ※ PriceCategoryフィールドで区別する：「売買」=売買取引、「貸借」=賃貸取引\n"
                    f"  ※ Typeフィールドは物件種別（中古マンション等）であり賃貸かどうかの判別には使えない\n"
                    f"  ※ 距離パラメータは指定しないこと（最大425mのため指定すると失敗する場合がある）\n\n"
                    f"【注意】\n"
                    f"・最寄駅・小学校・医療は上記確定値を使用すること（MCPで再取得しない）\n"
                    f"・公示地価が複数件ある場合は座標({lat},{lon})に最も近い標準地の値1件のみ使用\n"
                    f"・用途地域（API 5）のデータには建蔽率・容積率も含まれる。必ず抽出すること\n"
                    f"・データなしの場合は「－」と記載すること"
                )
            else:
                location_info = (
                    f"{mode_task}：{address}{maisoku_context}\n\n"
                    f"{yield_context}\n\n"
                    f"※ 座標の自動取得に失敗しました。日本の地理知識から緯度経度を推定し、"
                    f"「get_multi_api」ツールで以下を順番に取得してください：\n"
                    f"target_apis=[3](公示地価,distance=425), [26](洪水), [27](高潮),\n"
                    f"[28](津波), [29](土砂), [25](液状化), [5](用途地域+建蔽率+容積率),\n"
                    f"[14](防火地域), [13](将来推計人口), [30](人口集中地区),\n"
                    f"[15](乗降客数,distance=425), [1](不動産取引価格・distanceなし)"
                )

            messages = [{"role": "user", "content": location_info}]

            # ── モード別レポートヘッダー ──────────────────────────
            report_header_title = {
                "location":         "🔍 立地調査レポート",
                "assessment":       "🏢 資産性評価レポート",
                "investment_kubun": "📊 投資分析レポート（区分）",
                "investment_ittou": "📊 投資分析レポート（一棟）",
            }.get(mode, "🔍 立地調査レポート")

            # ── Claude エージェントループ ─────────────────────────
            for turn in range(15):
                response = await call_claude_with_retry(
                    model="claude-sonnet-4-6",
                    max_tokens=4096,
                    temperature=0,
                    system=SYSTEM_PROMPT,
                    tools=tools,
                    messages=messages,
                )
                logger.info(f"Turn {turn + 1}: stop_reason={response.stop_reason}")

                if response.stop_reason == "end_turn":
                    header = (
                        f"{report_header_title}\n"
                        f"by LocaGenius\n"
                        f"{datetime.now(JST).strftime('%Y/%m/%d %H:%M')}\n"
                        f"─────────────\n"
                    )
                    for block in response.content:
                        if hasattr(block, "text"):
                            return header + strip_preamble(block.text)
                    return "（回答を生成できませんでした。再度お試しください）"

                if response.stop_reason == "tool_use":
                    assistant_content = []
                    for block in response.content:
                        if block.type == "text":
                            assistant_content.append({"type": "text", "text": block.text})
                        elif block.type == "tool_use":
                            assistant_content.append({
                                "type":  "tool_use",
                                "id":    block.id,
                                "name":  block.name,
                                "input": block.input,
                            })
                    messages.append({"role": "assistant", "content": assistant_content})

                    tool_results = []
                    for block in response.content:
                        if block.type != "tool_use":
                            continue
                        logger.info(f"  Calling: {block.name}({block.input})")
                        try:
                            call_result = await session.call_tool(block.name, block.input)
                            if call_result.content:
                                parts = [
                                    item.text if hasattr(item, "text") else str(item)
                                    for item in call_result.content
                                ]
                                content = "\n".join(parts) if parts else "（データなし）"
                            else:
                                content = "（データなし）"
                            is_err = getattr(call_result, "isError", False)
                            # API 1（不動産取引価格）：収益指標を Python 側で確定計算してから要約
                            if 1 in block.input.get("target_apis", []) and not is_err and content != "（データなし）":
                                income_block = build_income_block(
                                    content, address, mode, maisoku_data
                                )
                                content = summarize_real_estate(content)
                                if income_block:
                                    content += "\n\n" + income_block
                            logger.info(f"  Result[{block.name}] isError={is_err} len={len(content)}: {content[:600]}")
                        except Exception as e:
                            logger.warning(f"  Tool '{block.name}' error: {e}")
                            content = f"取得エラー: {e}"

                        if len(content) > 12000:
                            content = content[:12000] + "\n…（以下省略）"

                        tool_results.append({
                            "type":        "tool_result",
                            "tool_use_id": block.id,
                            "content":     content,
                        })
                    messages.append({"role": "user", "content": tool_results})
                else:
                    logger.warning(f"Unexpected stop_reason: {response.stop_reason}")
                    break

    return "調査がタイムアウトしました。お手数ですが再度お試しください。"
