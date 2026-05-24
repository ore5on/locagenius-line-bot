"""
LocaGenius — Mode 3: マイソク → 投資分析（区分 / 一棟）

run_investment_core()    : コアロジック（LINE / Web API 共通）
process_maisoku_image()  : LINE 専用（LINE APIからダウンロード）
process_maisoku_pdf()    : LINE 専用（LINE APIからダウンロード）
route_maisoku()          : LINE 専用（区分/一棟 確認メッセージを送信）
run_investment()         : LINE 専用ラッパー
"""

import asyncio
import logging

from core.config       import pending_type_confirm
from core.geocoding    import geocode, reverse_geocode
from core.overpass     import get_nearest_station, get_nearest_school, get_nearest_medical
from core.investigator import run_investigation
from core.maisoku      import extract_property, build_investigation_text, format_extracted_info

logger = logging.getLogger(__name__)


# ──────────────────────────────────────
# コアロジック（LINE / Web API 共通）
# ──────────────────────────────────────
async def run_investment_core(extracted: dict) -> str:
    """マイソク抽出データで投資分析を実行するコアロジック。

    LINE Bot・Web API どちらからも呼び出せる共通ロジック。
    エラー時もユーザー向けメッセージ文字列を返す（例外は投げない）。

    Args:
        extracted: マイソクから抽出した物件情報 dict。
                   必須キー: address または building_name
                   任意キー: property_category（"区分" / "一棟"、デフォルト "区分"）

    Returns:
        投資分析レポート文字列
    """
    address       = extracted.get("address") or extracted.get("building_name")
    prop_category = extracted.get("property_category", "区分")

    if not address:
        return (
            "⚠️ 住所を読み取れませんでした。\n\n"
            f"読み取れた情報：\n{format_extracted_info(extracted)}\n\n"
            "住所をテキストで送ってください。"
        )

    try:
        investigation_text = build_investigation_text(extracted)
        coords = await geocode(address)

        if coords is None:
            return "🔍 座標が見つかりませんでした。住所で再度お試しください。"

        nearest_station, nearest_school, nearest_medical, resolved_address = (
            await asyncio.gather(
                get_nearest_station(coords[0], coords[1]),
                get_nearest_school(coords[0], coords[1]),
                get_nearest_medical(coords[0], coords[1]),
                reverse_geocode(coords[0], coords[1]),
            )
        )
        # エリア判定には逆ジオコーディング住所を使う（建物名だとキーワード不一致で利回りが誤る）
        address_for_yield = resolved_address or address
        mode = "investment_kubun" if prop_category == "区分" else "investment_ittou"
        return await run_investigation(
            address_for_yield, coords, nearest_station, nearest_school, nearest_medical,
            mode=mode, maisoku_data=extracted,
        )
    except Exception as e:
        logger.exception(f"Investment analysis failed: {e}")
        return (
            "❌ 分析中にエラーが発生しました。\n"
            "しばらく待ってから再度お試しください。"
        )


# ──────────────────────────────────────
# マイソクファイル受信（画像 / PDF）— LINE 専用
# ──────────────────────────────────────
async def process_maisoku_image(user_id: str, message_id: str) -> None:
    """マイソク画像から物件情報を抽出して投資分析へ振り分ける（LINE専用）"""
    from core.line_api import push, download_content
    try:
        image_bytes = await download_content(message_id)
        if not image_bytes:
            await push(user_id, "❌ 画像の取得に失敗しました。再度送信してください。")
            return

        extracted = await extract_property("image/jpeg", image_bytes)
        if not extracted:
            await push(
                user_id,
                "❌ マイソクから物件情報を読み取れませんでした。\n\n"
                "画像が鮮明か確認するか、テキストで住所を入力してください。",
            )
            return

        await route_maisoku(user_id, extracted)

    except Exception as e:
        logger.exception(f"Maisoku image processing failed: {e}")
        from core.line_api import push
        await push(user_id, "❌ 処理中にエラーが発生しました。再度お試しください。")


async def process_maisoku_pdf(user_id: str, message_id: str) -> None:
    """マイソク PDF から物件情報を抽出して投資分析へ振り分ける（LINE専用）"""
    from core.line_api import push, download_content
    try:
        pdf_bytes = await download_content(message_id)
        if not pdf_bytes:
            await push(user_id, "❌ PDFの取得に失敗しました。再度送信してください。")
            return

        extracted = await extract_property("application/pdf", pdf_bytes)
        if not extracted:
            await push(
                user_id,
                "❌ PDFから物件情報を読み取れませんでした。\n\n"
                "・PDFが破損していないか確認してください\n"
                "・テキストで住所を入力してお試しください",
            )
            return

        await route_maisoku(user_id, extracted)

    except Exception as e:
        logger.exception(f"PDF processing failed: {e}")
        from core.line_api import push
        await push(user_id, "❌ 処理中にエラーが発生しました。再度お試しください。")


# ──────────────────────────────────────
# 区分 / 一棟 振り分け — LINE 専用
# ──────────────────────────────────────
async def route_maisoku(user_id: str, extracted: dict) -> None:
    """抽出済みデータを区分/一棟に振り分ける（画像・PDF 共通・LINE専用）

    property_category が不明な場合、ユーザーへ確認メッセージを送信し
    pending_type_confirm に保持する。Web版ではUIセレクターで代替する。
    """
    from core.line_api import push
    address = extracted.get("address") or extracted.get("building_name")
    if not address:
        await push(
            user_id,
            "⚠️ 住所を読み取れませんでした。\n\n"
            f"読み取れた情報：\n{format_extracted_info(extracted)}\n\n"
            "住所をテキストで送ってください。",
        )
        return

    prop_category = extracted.get("property_category")

    # 区分/一棟が判別できない場合はユーザーに確認する
    if not prop_category:
        pending_type_confirm[user_id] = extracted
        await push(
            user_id,
            f"✅ 物件情報を読み取りました！\n"
            f"─────────────\n"
            f"{format_extracted_info(extracted)}\n"
            f"─────────────\n"
            f"この物件は「区分」マンションですか？\n"
            f"それとも「一棟」物件ですか？\n\n"
            f"「区分」または「一棟」と入力してください。",
        )
        return

    await run_investment(user_id, extracted)


# ──────────────────────────────────────
# 投資分析実行 — LINE 専用ラッパー
# ──────────────────────────────────────
async def run_investment(user_id: str, extracted: dict) -> None:
    """マイソク抽出データで投資分析を実行する LINE専用ラッパー（Mode 3）

    分析開始メッセージを先送りし、run_investment_core() の結果を push する。
    """
    from core.line_api import push
    address       = extracted.get("address") or extracted.get("building_name")
    prop_category = extracted.get("property_category", "区分")

    if not address:
        await push(
            user_id,
            "⚠️ 住所を読み取れませんでした。\n\n"
            f"読み取れた情報：\n{format_extracted_info(extracted)}\n\n"
            "住所をテキストで送ってください。",
        )
        return

    await push(
        user_id,
        f"✅ 物件情報を読み取りました！\n"
        f"─────────────\n"
        f"種別：{prop_category}物件\n"
        f"{format_extracted_info(extracted)}\n"
        f"─────────────\n"
        f"投資分析を開始します...\n30秒〜1分程度お待ちください 🏃",
    )

    result = await run_investment_core(extracted)
    await push(user_id, result)
