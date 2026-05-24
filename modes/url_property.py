"""
LocaGenius — URL → 物件分析

物件ページのURLを受け取り、Jina Reader でテキスト化 →
Claude で物件情報を抽出 → 既存の投資分析パイプラインへ振り分ける。

fetch_and_extract_url()  : コアロジック（LINE / Web API 共通）
process_property_url()   : LINE 専用ラッパー
"""

import logging

import httpx

from core.maisoku import extract_from_text

logger = logging.getLogger(__name__)

_JINA_BASE = "https://r.jina.ai/"


# ──────────────────────────────────────
# ページ取得（内部ヘルパー）
# ──────────────────────────────────────
async def _fetch_page_text(url: str) -> str | None:
    """Jina Reader でURLをテキスト化する。失敗時は直接 httpx で取得する。"""

    # ── Jina Reader（JS描画対応・サイト非依存）────────────────
    try:
        jina_url = _JINA_BASE + url
        headers  = {"Accept": "text/plain", "User-Agent": "LocaGenius/1.0"}
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(jina_url, headers=headers)
            resp.raise_for_status()
            text = resp.text
            if text and len(text) > 200:
                logger.info(f"Jina Reader: {len(text)} chars ← {url}")
                return text
    except Exception as e:
        logger.warning(f"Jina Reader failed ({url}): {e}")

    # ── 直接取得フォールバック（サーバー描画ページ用）──────────
    try:
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
            resp = await client.get(url, headers=headers)
            resp.raise_for_status()
            text = resp.text
            logger.info(f"Direct fetch: {len(text)} chars ← {url}")
            return text
    except Exception as e:
        logger.warning(f"Direct fetch failed ({url}): {e}")

    return None


# ──────────────────────────────────────
# コアロジック（LINE / Web API 共通）
# ──────────────────────────────────────
async def fetch_and_extract_url(url: str) -> dict | None:
    """URLから物件情報を取得・抽出して dict を返す。

    LINE Bot・Web API どちらからも呼び出せる共通ロジック。
    Web版ではこの結果を受け取り、フロント側で区分/一棟をUIで選択させてから
    投資分析エンドポイントへ渡す。

    Args:
        url: 物件ページの URL

    Returns:
        抽出した物件情報 dict。取得・抽出に失敗した場合は None。
    """
    text = await _fetch_page_text(url)
    if not text:
        logger.warning(f"fetch_and_extract_url: page fetch failed ({url})")
        return None

    extracted = await extract_from_text(text)
    if not extracted:
        logger.warning(f"fetch_and_extract_url: extraction failed ({url})")
        return None

    return extracted


# ──────────────────────────────────────
# LINE 専用ラッパー
# ──────────────────────────────────────
async def process_property_url(user_id: str, url: str) -> None:
    """物件ページURLから情報を抽出し、投資分析へ振り分ける（LINE専用）"""
    from core.line_api import push
    from modes.investment import route_maisoku
    try:
        extracted = await fetch_and_extract_url(url)

        if extracted is None:
            # ページ取得に失敗した場合
            text = await _fetch_page_text(url)
            if not text:
                await push(
                    user_id,
                    "❌ URLからページを取得できませんでした。\n\n"
                    "以下をお試しください：\n"
                    "・マイソク画像/PDFを直接送信\n"
                    "・住所をテキストで入力",
                )
                return
            # テキスト取得できたが抽出失敗
            await push(
                user_id,
                "❌ URLから物件情報を読み取れませんでした。\n\n"
                "マイソク画像/PDFを直接送信するか、\n"
                "住所をテキストで入力してください。",
            )
            return

        # 既存の投資分析ルーターへ
        await route_maisoku(user_id, extracted)

    except Exception as e:
        logger.exception(f"process_property_url failed ({url}): {e}")
        await push(user_id, "❌ 処理中にエラーが発生しました。再度お試しください。")
