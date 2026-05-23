"""
LocaGenius — Mode 1: 住所 → 立地調査
"""

import asyncio
import logging

from core.geocoding import geocode, extract_location_query
from core.overpass  import get_nearest_station, get_nearest_school, get_nearest_medical
from core.investigator import run_investigation
from core.line_api  import push

logger = logging.getLogger(__name__)


async def investigate_and_push(user_id: str, address: str) -> None:
    """立地調査を実行し、結果を LINE に push する（Mode 1）"""
    try:
        _, _, is_building = extract_location_query(address)
        coords = await geocode(address)

        # 建物名検索でヒットしなかった場合は調査しない
        if is_building and coords is None:
            await push(
                user_id,
                "🔍 建物情報が見つかりませんでした。\n\n"
                "マンション名はデータベースに\n"
                "登録されていない場合があります。\n\n"
                "以下の形式でお試しください：\n"
                "・住所で入力\n"
                "  例：東京都渋谷区恵比寿1-1-1\n"
                "・駅名で入力\n"
                "  例：恵比寿駅",
            )
            return

        if coords:
            nearest_station, nearest_school, nearest_medical = await asyncio.gather(
                get_nearest_station(coords[0], coords[1]),
                get_nearest_school(coords[0], coords[1]),
                get_nearest_medical(coords[0], coords[1]),
            )
        else:
            nearest_station = nearest_school = nearest_medical = None

        result = await run_investigation(
            address, coords, nearest_station, nearest_school, nearest_medical,
            mode="location",
        )
    except Exception as e:
        logger.exception(f"Investigation failed for '{address}': {e}")
        result = (
            "❌ 調査中にエラーが発生しました。\n\n"
            "以下をお試しください：\n"
            "・住所をより具体的に入力\n"
            "  例：東京都渋谷区恵比寿1-1-1\n"
            "・しばらく待ってから再度送信\n\n"
            "問題が続く場合は\n「ヘルプ」と送ってください。"
        )
    await push(user_id, result)
