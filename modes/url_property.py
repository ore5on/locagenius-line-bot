"""
LocaGenius — URL → 物件分析

物件ページのURLを受け取り、Jina Reader でテキスト化 →
Claude で物件情報を抽出 → 既存の投資分析パイプラインへ振り分ける。
"""

import logging

import httpx

from core.maisoku  import extract_from_text
from core.line_api import push
from modes.investment import route_maisoku

logger = logging.getLogger(__name__)

_JINA_BASE = "https://r.jina.ai/"


# ──────────────────────────────────────
# ページ取得
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
# メイン処理
# ──────────────────────────────────────
async def process_property_url(user_id: str, url: str) -> None:
    """物件ページURLから情報を抽出し、投資分析へ振り分ける。"""
    try:
        # 1. ページテキスト取得
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

        # 2. 物件情報抽出
        extracted = await extract_from_text(text)
        if not extracted:
            await push(
                user_id,
                "❌ URLから物件情報を読み取れませんでした。\n\n"
                "マイソク画像/PDFを直接送信するか、\n"
                "住所をテキストで入力してください。",
            )
            return

        # 3. 既存の投資分析ルーターへ（確認メッセージは route_maisoku / run_investment が送る）
        await route_maisoku(user_id, extracted)

    except Exception as e:
        logger.exception(f"process_property_url failed ({url}): {e}")
        await push(user_id, "❌ 処理中にエラーが発生しました。再度お試しください。")
