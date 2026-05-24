"""
LocaGenius — Mode 1: 住所 → 立地調査

run_location_analysis()  : コアロジック（LINE / Web API 共通）
investigate_and_push()   : LINE 専用ラッパー
"""

import asyncio
import logging

from core.geocoding    import geocode, extract_location_query
from core.overpass     import get_nearest_station, get_nearest_school, get_nearest_medical
from core.investigator import run_investigation

logger = logging.getLogger(__name__)

_NOT_FOUND_MSG = (
    "🔍 建物情報が見つかりませんでした。\n\n"
    "マンション名はデータベースに\n"
    "登録されていない場合があります。\n\n"
    "以下の形式でお試しください：\n"
    "・住所で入力\n"
    "  例：東京都渋谷区恵比寿1-1-1\n"
    "・駅名で入力\n"
    "  例：恵比寿駅"
)

_ERROR_MSG = (
    "❌ 調査中にエラーが発生しました。\n\n"
    "以下をお試しください：\n"
    "・住所をより具体的に入力\n"
    "  例：東京都渋谷区恵比寿1-1-1\n"
    "・しばらく待ってから再度送信\n\n"
    "問題が続く場合は\n「ヘルプ」と送ってください。"
)


# ──────────────────────────────────────
# コアロジック（LINE / Web API 共通）
# ──────────────────────────────────────
async def run_location_analysis(address: str) -> str:
    """立地調査を実行してレポート文字列を返す。

    LINE Bot・Web API どちらからも呼び出せる共通ロジック。
    エラー時もユーザー向けメッセージ文字列を返す（例外は投げない）。

    Args:
        address: 調査対象の住所・地名

    Returns:
        調査レポート文字列
    """
    try:
        _, _, is_building = extract_location_query(address)
        coords = await geocode(address)

        # 建物名検索でヒットしなかった場合
        if is_building and coords is None:
            return _NOT_FOUND_MSG

        if coords:
            nearest_station, nearest_school, nearest_medical = await asyncio.gather(
                get_nearest_station(coords[0], coords[1]),
                get_nearest_school(coords[0], coords[1]),
                get_nearest_medical(coords[0], coords[1]),
            )
        else:
            nearest_station = nearest_school = nearest_medical = None

        return await run_investigation(
            address, coords, nearest_station, nearest_school, nearest_medical,
            mode="location",
        )
    except Exception as e:
        logger.exception(f"Investigation failed for '{address}': {e}")
        return _ERROR_MSG


# ──────────────────────────────────────
# LINE 専用ラッパー
# ──────────────────────────────────────
async def investigate_and_push(user_id: str, address: str) -> None:
    """立地調査を実行し、結果を LINE に push する（Mode 1）"""
    from core.line_api import push
    result = await run_location_analysis(address)
    await push(user_id, result)
