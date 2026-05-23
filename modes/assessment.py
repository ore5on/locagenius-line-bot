"""
LocaGenius — Mode 2: マンション名 → 資産性評価
"""

import asyncio
import logging

from core.geocoding    import geocode
from core.overpass     import get_nearest_station, get_nearest_school, get_nearest_medical
from core.investigator import run_investigation
from core.line_api     import push

logger = logging.getLogger(__name__)


async def assess_and_push(user_id: str, building_name: str) -> None:
    """マンション名から資産性評価を実行し、結果を LINE に push する（Mode 2）"""
    try:
        coords = await geocode(building_name)
        if coords is None:
            await push(
                user_id,
                f"📍「{building_name}」\nの場所を特定できませんでした。\n\n"
                "マンション名は地図データベースに\n"
                "登録されていない場合があります。\n\n"
                "▶ 住所で入力してください\n"
                "例：東京都世田谷区池尻2-1-1\n\n"
                "住所が分かれば同じ精度で調査できます。",
            )
            return

        nearest_station, nearest_school, nearest_medical = await asyncio.gather(
            get_nearest_station(coords[0], coords[1]),
            get_nearest_school(coords[0], coords[1]),
            get_nearest_medical(coords[0], coords[1]),
        )
        result = await run_investigation(
            building_name, coords, nearest_station, nearest_school, nearest_medical,
            mode="assessment",
        )
    except Exception as e:
        logger.exception(f"Assessment failed for '{building_name}': {e}")
        result = (
            "❌ 調査中にエラーが発生しました。\n\n"
            "しばらく待ってから再度お試しください。"
        )
    await push(user_id, result)
