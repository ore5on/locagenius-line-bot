"""
LocaGenius — Mode 2: マンション名 → 資産性評価

run_assessment_analysis() : コアロジック（LINE / Web API 共通）
assess_and_push()          : LINE 専用ラッパー
"""

import asyncio
import logging

from core.geocoding    import geocode, reverse_geocode
from core.overpass     import get_nearest_station, get_nearest_school, get_nearest_medical
from core.investigator import run_investigation

logger = logging.getLogger(__name__)

_ERROR_MSG = (
    "❌ 調査中にエラーが発生しました。\n\n"
    "しばらく待ってから再度お試しください。"
)


# ──────────────────────────────────────
# コアロジック（LINE / Web API 共通）
# ──────────────────────────────────────
async def run_assessment_analysis(building_name: str) -> str:
    """資産性評価を実行してレポート文字列を返す。

    LINE Bot・Web API どちらからも呼び出せる共通ロジック。
    エラー時もユーザー向けメッセージ文字列を返す（例外は投げない）。

    Args:
        building_name: マンション名または住所

    Returns:
        資産性評価レポート文字列
    """
    try:
        coords = await geocode(building_name)
        if coords is None:
            return (
                f"📍「{building_name}」\nの場所を特定できませんでした。\n\n"
                "マンション名は地図データベースに\n"
                "登録されていない場合があります。\n\n"
                "▶ 住所で入力してください\n"
                "例：東京都世田谷区池尻2-1-1\n\n"
                "住所が分かれば同じ精度で調査できます。"
            )

        nearest_station, nearest_school, nearest_medical, resolved_address = (
            await asyncio.gather(
                get_nearest_station(coords[0], coords[1]),
                get_nearest_school(coords[0], coords[1]),
                get_nearest_medical(coords[0], coords[1]),
                reverse_geocode(coords[0], coords[1]),
            )
        )
        # エリア判定には逆ジオコーディングで得た住所を使う（建物名だとキーワード不一致で利回りが誤る）
        address_for_yield = resolved_address or building_name
        return await run_investigation(
            address_for_yield, coords, nearest_station, nearest_school, nearest_medical,
            mode="assessment",
        )
    except Exception as e:
        logger.exception(f"Assessment failed for '{building_name}': {e}")
        return _ERROR_MSG


# ──────────────────────────────────────
# LINE 専用ラッパー
# ──────────────────────────────────────
async def assess_and_push(user_id: str, building_name: str) -> None:
    """マンション名から資産性評価を実行し、結果を LINE に push する（Mode 2）"""
    from core.line_api import push
    result = await run_assessment_analysis(building_name)
    await push(user_id, result)
