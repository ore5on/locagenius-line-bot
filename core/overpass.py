"""
LocaGenius — Overpass API（OpenStreetMap）ヘルパー

最寄駅・小学校・医療施設の取得に使用。APIキー不要・無料。
"""

import math
import logging

import httpx

logger = logging.getLogger(__name__)

_OVERPASS_ENDPOINTS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://lz4.overpass-api.de/api/interpreter",
]


async def _overpass_query(query: str) -> list[dict]:
    """Overpass API にクエリを投げて elements リストを返す（複数エンドポイントでフォールバック）"""
    headers = {
        "User-Agent":   "LocaGenius/1.0 real-estate-research-bot",
        "Accept":       "application/json, */*;q=0.9",
        "Content-Type": "application/x-www-form-urlencoded",
    }
    for url in _OVERPASS_ENDPOINTS:
        try:
            async with httpx.AsyncClient(timeout=25.0) as client:
                resp = await client.post(url, data={"data": query}, headers=headers)
                resp.raise_for_status()
                return resp.json().get("elements", [])
        except Exception as e:
            logger.warning(f"Overpass query failed ({url}): {e}")
    return []


def _nearest_element(elements: list[dict], lat: float, lon: float) -> dict | None:
    """要素リストから最近傍を返す（Python側で距離計算）"""
    def dist(e: dict) -> float:
        try:
            dlat = (float(e.get("lat", 0)) - lat) * 111000
            dlon = (float(e.get("lon", 0)) - lon) * 91000
            return math.sqrt(dlat ** 2 + dlon ** 2)
        except Exception:
            return float("inf")

    return min(elements, key=dist) if elements else None


def _walk_min(e: dict, lat: float, lon: float) -> int:
    """要素までの徒歩時間（分）を計算。80m/分で算出"""
    dlat = (float(e.get("lat", 0)) - lat) * 111000
    dlon = (float(e.get("lon", 0)) - lon) * 91000
    distance = math.sqrt(dlat ** 2 + dlon ** 2)
    return max(1, round(distance / 80))


# 実用性の低い路線のオペレーター名（直線距離にペナルティを掛ける）
# ゆりかもめ・モノレール・新交通システムなどは通勤実用性が低い
_LOW_PRIORITY_OPERATORS = {
    "東京臨海新交通", "ゆりかもめ",
    "東京モノレール", "千葉都市モノレール",
    "多摩都市モノレール", "北九州高速鉄道",
    "沖縄都市モノレール", "舞浜リゾートライン",
    "ディズニーリゾートライン",
    "ニュートラム", "大阪港トランスポートシステム",
    "神戸新交通", "広島高速交通",
    "スカイレール", "桃花台新交通",
}

# 駅名に含まれるゆりかもめ系キーワード
_LOW_PRIORITY_NAME_HINTS = {
    "市場前", "新豊洲", "有明テニスの森", "有明", "お台場海浜公園",
    "台場", "船の科学館", "テレコムセンター", "青海",
}


def _station_distance_penalty(tags: dict, name: str) -> float:
    """路線種別に基づくペナルティ係数（値が大きいほど選ばれにくい）

    地下鉄・JR・大手私鉄 → 1.0（ペナルティなし）
    ゆりかもめ・モノレール等 → 2.0（実質距離2倍扱い）
    """
    operator = tags.get("operator", "") or ""
    network  = tags.get("network", "") or ""

    for kw in _LOW_PRIORITY_OPERATORS:
        if kw in operator or kw in network:
            return 2.0

    for kw in _LOW_PRIORITY_NAME_HINTS:
        if kw in name:
            return 2.0

    return 1.0


async def get_nearest_station(lat: float, lon: float) -> str | None:
    """緯度経度から最寄駅を取得する（Overpass API）

    直線距離に路線種別ペナルティを掛けて評価することで、
    ゆりかもめ・モノレールより地下鉄・JR・私鉄を優先する。
    """
    query = f"""
[out:json][timeout:15];
(
  node["railway"="station"](around:3000,{lat},{lon});
  node["railway"="halt"](around:3000,{lat},{lon});
);
out body;
"""
    elements = await _overpass_query(query)
    if not elements:
        logger.warning("No station found within 3km")
        return None

    def _weighted_dist(e: dict) -> float:
        try:
            dlat    = (float(e.get("lat", 0)) - lat) * 111000
            dlon    = (float(e.get("lon", 0)) - lon) * 91000
            dist    = math.sqrt(dlat ** 2 + dlon ** 2)
            tags    = e.get("tags", {})
            name    = tags.get("name:ja") or tags.get("name") or ""
            penalty = _station_distance_penalty(tags, name)
            return dist * penalty
        except Exception:
            return float("inf")

    nearest = min(elements, key=_weighted_dist) if elements else None
    if not nearest:
        return None

    tags         = nearest.get("tags", {})
    name         = tags.get("name:ja") or tags.get("name") or "不明"
    railway_name = tags.get("railway:line") or tags.get("operator") or ""
    wmin         = _walk_min(nearest, lat, lon)

    result = f"{name}駅 徒歩約{wmin}分"
    if railway_name:
        result += f"［{railway_name}］"
    logger.info(f"Nearest station: {result} (penalty applied)")
    return result


async def get_nearest_school(lat: float, lon: float) -> str | None:
    """緯度経度から最寄りの小学校を取得する（Overpass API）"""
    query = f"""
[out:json][timeout:15];
(
  node["amenity"="school"](around:2000,{lat},{lon});
  way["amenity"="school"](around:2000,{lat},{lon});
);
out center;
"""
    elements = await _overpass_query(query)

    for e in elements:
        if e.get("type") == "way" and "center" in e:
            e["lat"] = e["center"]["lat"]
            e["lon"] = e["center"]["lon"]

    schools = [
        e for e in elements
        if "小学校" in (e.get("tags", {}).get("name") or "")
    ] or elements

    nearest = _nearest_element(schools, lat, lon)
    if not nearest:
        return None

    tags = nearest.get("tags", {})
    name = tags.get("name:ja") or tags.get("name") or "不明"
    wmin = _walk_min(nearest, lat, lon)
    logger.info(f"Nearest school: {name} 徒歩{wmin}分")
    return f"{name} 徒歩約{wmin}分"


async def get_nearest_medical(lat: float, lon: float) -> str | None:
    """緯度経度から最寄りの医療施設を取得する（Overpass API）"""
    query = f"""
[out:json][timeout:15];
(
  node["amenity"="hospital"](around:2000,{lat},{lon});
  node["amenity"="clinic"](around:2000,{lat},{lon});
  node["amenity"="doctors"](around:2000,{lat},{lon});
  way["amenity"="hospital"](around:2000,{lat},{lon});
);
out center;
"""
    elements = await _overpass_query(query)

    for e in elements:
        if e.get("type") == "way" and "center" in e:
            e["lat"] = e["center"]["lat"]
            e["lon"] = e["center"]["lon"]

    hospitals = [
        e for e in elements
        if e.get("tags", {}).get("amenity") == "hospital"
    ] or elements

    nearest = _nearest_element(hospitals, lat, lon)
    if not nearest:
        return None

    tags = nearest.get("tags", {})
    name = tags.get("name:ja") or tags.get("name") or "不明"
    wmin = _walk_min(nearest, lat, lon)
    logger.info(f"Nearest medical: {name} 徒歩{wmin}分")
    return f"{name} 徒歩約{wmin}分"
