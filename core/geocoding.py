"""
LocaGenius — ジオコーディング & テキスト前処理

・_looks_like_location   : 入力が住所・建物名らしいか判定
・_is_property_info_only : 物件情報のみで住所なしを検出
・_extract_location_query: テキストから検索クエリ・駅/建物フラグを抽出
・geocode_nominatim      : Nominatim で座標取得
・geocode                : 住所/駅名/建物名 → (lat, lon) の統合エントリポイント
"""

import re
import logging

import httpx

from core.config import logger as _root_logger, GOOGLE_MAPS_API_KEY

logger = logging.getLogger(__name__)

# ──────────────────────────────────────
# 汎用語（地名として扱わない）
# ──────────────────────────────────────
NON_LOCATION_WORDS = {
    "はい", "いいえ", "うん", "そう", "なるほど", "わかった", "わかりました",
    "ありがとう", "ありがとうございます", "よろしく", "お願いします",
    "ok", "okay", "yes", "no", "了解", "了解です", "了解しました",
    "なし", "ない", "不要", "結構です", "大丈夫", "おk",
}

# ──────────────────────────────────────
# 都道府県コード対応表
# ──────────────────────────────────────
PREF_CODES: dict[str, str] = {
    "北海道": "01", "青森県": "02", "岩手県": "03", "宮城県": "04", "秋田県": "05",
    "山形県": "06", "福島県": "07", "茨城県": "08", "栃木県": "09", "群馬県": "10",
    "埼玉県": "11", "千葉県": "12", "東京都": "13", "神奈川県": "14", "新潟県": "15",
    "富山県": "16", "石川県": "17", "福井県": "18", "山梨県": "19", "長野県": "20",
    "岐阜県": "21", "静岡県": "22", "愛知県": "23", "三重県": "24", "滋賀県": "25",
    "京都府": "26", "大阪府": "27", "兵庫県": "28", "奈良県": "29", "和歌山県": "30",
    "鳥取県": "31", "島根県": "32", "岡山県": "33", "広島県": "34", "山口県": "35",
    "徳島県": "36", "香川県": "37", "愛媛県": "38", "高知県": "39", "福岡県": "40",
    "佐賀県": "41", "長崎県": "42", "熊本県": "43", "大分県": "44", "宮崎県": "45",
    "鹿児島県": "46", "沖縄県": "47",
}

# 建物名キーワード（_extract_location_query / _looks_like_location 共用）
_BUILDING_KEYWORDS = [
    "マンション", "アパート", "レジデンス", "コート", "パレス",
    "ガーデン", "ハイツ", "ハウス", "プレイス", "スクエア",
    "タワー", "ヒルズ", "テラス", "ビル", "ビレッジ", "シティ",
    "グランド", "プレミア", "ロイヤル", "パーク", "フォレスト",
]


# ──────────────────────────────────────
# テキスト判定ヘルパー
# ──────────────────────────────────────
def is_property_info_only(text: str) -> bool:
    """物件情報（面積・構造・築年数）のみで住所・地名が含まれていない入力を検出する"""
    property_kws = [
        "土地面積", "建物面積", "延床面積", "床面積",
        "RC造", "鉄骨造", "木造", "築年数", "築", "平米", "㎡",
    ]
    location_kws = ["都", "道", "府", "県", "市", "区", "町", "村", "駅", "丁目", "番地", "番", "号"]
    has_property = sum(1 for kw in property_kws if kw in text) >= 2
    has_location = any(kw in text for kw in location_kws)
    return has_property and not has_location


def looks_like_location(text: str) -> bool:
    """入力テキストが地名・住所・建物名らしいかどうかを判定する"""
    if text.lower() in NON_LOCATION_WORDS:
        return False

    location_indicators = [
        "都", "道", "府", "県",
        "市", "区", "町", "村",
        "駅", "丁目", "番地", "番", "号",
        "通り", "街", "地区", "エリア", "周辺", "付近", "圏内",
    ] + _BUILDING_KEYWORDS

    if any(kw in text for kw in location_indicators):
        return True

    # 10文字以上あれば住所・建物名の可能性あり
    return len(text) >= 10


def extract_location_query(text: str) -> tuple[str, bool, bool]:
    """ユーザー入力から検索用クエリ・「駅検索か」・「建物検索か」を返す。

    例：
        「横浜駅から徒歩5分圏内」  → ("横浜駅", True, False)
        「パークマンション渋谷」    → ("パークマンション渋谷", False, True)
        「東京都渋谷区恵比寿1-1-1」 → ("東京都渋谷区恵比寿1-1-1", False, False)
        「梅田周辺」               → ("梅田", False, False)
    """
    cleaned = re.sub(
        r'(から徒歩\d+分圏内|徒歩\d+分圏内|から\d+分圏内|\d+分圏内'
        r'|周辺|付近|近く|エリア|界隈)',
        '',
        text,
    ).strip()

    station_match = re.search(r'[一-鿿゠-ヿ぀-ゟA-Za-z0-9]+駅', cleaned)
    if station_match:
        return station_match.group(0), True, False

    if any(kw in cleaned for kw in _BUILDING_KEYWORDS):
        return cleaned, False, True

    return cleaned, False, False


# ──────────────────────────────────────
# Nominatim ジオコーダー
# ──────────────────────────────────────
async def geocode_google_maps(query: str) -> tuple[float, float] | None:
    """Google Maps Geocoding API で座標を取得する。

    Nominatim で見つからない建物名（マンション・ビル等）のフォールバックとして使用。
    GOOGLE_MAPS_API_KEY が未設定の場合はスキップする。
    """
    if not GOOGLE_MAPS_API_KEY:
        return None
    url = "https://maps.googleapis.com/maps/api/geocode/json"
    params = {
        "address":  query,
        "region":   "jp",
        "language": "ja",
        "key":      GOOGLE_MAPS_API_KEY,
    }
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url, params=params)
            resp.raise_for_status()
            data = resp.json()
        if data.get("status") != "OK" or not data.get("results"):
            logger.info(f"Google Maps: no results for '{query}' (status={data.get('status')})")
            return None
        location = data["results"][0]["geometry"]["location"]
        lat, lon  = location["lat"], location["lng"]
        logger.info(
            f"Google Maps geocoded '{query}' → lat={lat}, lon={lon} "
            f"({data['results'][0].get('formatted_address', '')[:60]})"
        )
        return lat, lon
    except Exception as e:
        logger.warning(f"Google Maps geocoding failed for '{query}': {e}")
    return None


async def geocode_nominatim(query: str) -> tuple[float, float] | None:
    """地名・駅名・建物名から Nominatim で座標を取得する"""
    url     = "https://nominatim.openstreetmap.org/search"
    headers = {"User-Agent": "LocaGenius/1.0 (real-estate-bot)"}
    params  = {
        "q":               query,
        "format":          "json",
        "limit":           5,
        "countrycodes":    "jp",
        "accept-language": "ja",
    }
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(url, params=params, headers=headers)
            resp.raise_for_status()
            results = resp.json()

        if results:
            results.sort(key=lambda r: float(r.get("importance", 0)), reverse=True)
            top = results[0]
            lat, lon = float(top["lat"]), float(top["lon"])
            logger.info(
                f"Nominatim geocoded '{query}' → lat={lat}, lon={lon} "
                f"({top.get('display_name', '')[:50]})"
            )
            return lat, lon
    except Exception as e:
        logger.warning(f"Nominatim geocoding failed for '{query}': {e}")
    return None


# ──────────────────────────────────────
# 逆ジオコーダー（緯度経度 → 住所文字列）
# ──────────────────────────────────────
async def reverse_geocode(lat: float, lon: float) -> str | None:
    """緯度経度から住所文字列を取得する（Nominatim 逆ジオコーディング）。

    エリア判定に使えるよう「東京都江東区…」形式の文字列を返す。
    失敗時は None を返す。
    """
    url     = "https://nominatim.openstreetmap.org/reverse"
    headers = {"User-Agent": "LocaGenius/1.0 (real-estate-bot)"}
    params  = {
        "lat":             lat,
        "lon":             lon,
        "format":          "json",
        "accept-language": "ja",
        "zoom":            18,
    }
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url, params=params, headers=headers)
            resp.raise_for_status()
            data = resp.json()

        # address フィールドから都道府県・市区町村を組み立てる
        addr = data.get("address", {})
        parts = [
            addr.get("state", ""),        # 東京都
            addr.get("city", ""),         # 江東区 など（政令市は county の場合あり）
            addr.get("county", ""),       # 郡・区
            addr.get("suburb", ""),       # 丁目レベル
        ]
        resolved = "".join(p for p in parts if p)
        if not resolved:
            # フォールバック：display_name の先頭を使用
            resolved = data.get("display_name", "")
        logger.info(f"reverse_geocode({lat}, {lon}) → '{resolved}'")
        return resolved or None
    except Exception as e:
        logger.warning(f"reverse_geocode failed ({lat}, {lon}): {e}")
        return None


# ──────────────────────────────────────
# 統合ジオコーダー
# ──────────────────────────────────────
async def geocode(address: str) -> tuple[float, float] | None:
    """住所から緯度・経度を取得する統合エントリポイント。

    ・駅名 → Nominatim（失敗時は国土地理院APIにフォールバック）
    ・建物名 → Nominatim のみ
    ・それ以外 → 国土地理院 住所検索API
    """
    query, is_station, is_building = extract_location_query(address)
    logger.info(
        f"Geocoding: original='{address}' → query='{query}', "
        f"is_station={is_station}, is_building={is_building}"
    )

    if is_station:
        coords = await geocode_nominatim(query)
        if coords:
            return coords

    if is_building:
        coords = await geocode_nominatim(query)
        if coords:
            return coords
        # Nominatim に登録されていない建物は Google Maps でフォールバック
        logger.info(f"Nominatim: building '{query}' not found, trying Google Maps")
        return await geocode_google_maps(query)

    # 住所検索：国土地理院API（APIキー不要）
    url = "https://msearch.gsi.go.jp/address-search/AddressSearch"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url, params={"q": query})
            resp.raise_for_status()
            results = resp.json()
            if results:
                # GeoJSON形式: coordinates は [経度, 緯度] の順
                lon, lat = results[0]["geometry"]["coordinates"]
                logger.info(f"Geocoded '{query}' → lat={lat}, lon={lon}")
                return lat, lon
    except Exception as e:
        logger.warning(f"Geocoding failed for '{query}': {e}")
    return None
