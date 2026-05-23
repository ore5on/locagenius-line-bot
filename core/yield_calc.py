"""
LocaGenius — 収益指標の確定計算（Python側）

Claude に委ねると再現性がないため、以下をPythonで確定計算する：
  ・エリア別想定利回りの決定
  ・不動産取引事例からの中央値㎡単価算出
  ・推定月額賃料・収益還元価格の計算

計算結果は build_income_block() の戻り値としてテキストブロックで返し、
investigator.py が API 1 ツール結果に付加して Claude へ渡す。
Claude はこのブロックの値をそのまま使用し、独自計算しない。
"""

import json
import logging
import statistics

logger = logging.getLogger(__name__)


def get_yield_context(address: str, maisoku_data: dict | None = None) -> str:
    """住所からエリア区分と想定利回りを確定し、Claude へ渡す指示テキストを返す。

    API 1 の取引事例有無に関係なく、プロンプト組み立て時点で呼び出す。
    これにより同じ住所では常に同じ利回りが使われる。
    """
    area_label, base_yield = classify_yield(address)
    building_name          = (maisoku_data or {}).get("building_name", "") or address
    corrected_yield, corrs = apply_yield_corrections(base_yield, building_name)

    lines = [
        "【Python確定値：想定利回り】",
        f"エリア区分：{area_label}",
        f"想定利回り：{corrected_yield}%",
    ]
    if corrs:
        lines.append(f"補正内容　：{'/ '.join(corrs)}")
    lines.append("※賃料・収益還元価格の計算は必ずこの利回りを使用すること。")

    return "\n".join(lines)


# ──────────────────────────────────────
# エリア別基準利回りテーブル
# ──────────────────────────────────────

def classify_yield(address: str) -> tuple[str, float]:
    """住所文字列からエリア区分名と基準利回り（%）を返す。

    マッチしない場合は「その他地方」7.5% を返す。
    """
    a = address

    # ── 東京都 ─────────────────────────────────────────────────
    if "東京" in a:
        if any(d in a for d in ["千代田区", "中央区", "港区"]):
            return "都心3区", 3.5
        if any(d in a for d in ["渋谷区", "新宿区", "目黒区", "品川区", "世田谷区", "杉並区"]):
            return "東京城南・城西", 4.0
        if any(d in a for d in [
            "江東区", "墨田区", "江戸川区", "葛飾区", "荒川区",
            "足立区", "北区", "板橋区", "練馬区", "大田区",
            "文京区", "台東区", "豊島区", "中野区",
        ]):
            return "東京城東・城北", 4.5
        # 東京都内でどの区にも当てはまらない → 23区外・多摩
        return "東京23区外・多摩", 5.5

    # ── 神奈川県 ────────────────────────────────────────────────
    if "横浜市" in a:
        return "横浜市", 4.5
    if "川崎市" in a:
        return "川崎市", 4.5

    # ── 埼玉・千葉 ─────────────────────────────────────────────
    if "さいたま市" in a:
        return "さいたま市", 5.0
    if "千葉市" in a:
        return "千葉市", 5.0

    # ── 大阪府 ─────────────────────────────────────────────────
    if "大阪" in a:
        if "大阪市" in a and any(d in a for d in ["北区", "中央区", "浪速区", "西区", "天王寺区"]):
            return "大阪市中心部", 4.5
        if "大阪市" in a:
            return "大阪市その他", 5.0

    if "京都市" in a:
        return "京都市", 5.0
    if "神戸市" in a:
        return "神戸市", 5.0

    # ── 愛知県 ─────────────────────────────────────────────────
    if "名古屋市" in a:
        return "名古屋市", 5.0

    # ── 政令指定都市 ────────────────────────────────────────────
    for city in ["札幌市", "仙台市", "新潟市", "静岡市", "浜松市",
                 "岡山市", "広島市", "北九州市", "福岡市", "熊本市", "堺市"]:
        if city in a:
            return "政令指定都市", 5.5

    return "その他地方", 7.5


def apply_yield_corrections(
    base_yield: float,
    address: str,
    property_type: str = "",
) -> tuple[float, list[str]]:
    """基準利回りにタワーマンション補正を適用する。

    Returns:
        (補正後利回り, 適用した補正のリスト)
    """
    corrections: list[str] = []
    y = base_yield

    tower_keywords = ["タワー", "TOWER", "tower", "超高層"]
    if any(kw in address or kw in property_type for kw in tower_keywords):
        y = round(y - 0.5, 1)
        corrections.append("タワー補正 −0.5%")

    return y, corrections


def extract_mansion_unit_prices(raw_api1_json: str) -> list[float]:
    """API 1 の生JSONからマンション系 売買取引の㎡単価リスト（万円/㎡）を抽出する。

    - 貸借（賃貸）は除外
    - 土地のみ（宅地・農地・林地）は除外
    - UnitPrice / PricePerUnit フィールドを使用（単位：円/㎡ → 万円/㎡に変換）
    """
    prices: list[float] = []
    try:
        data    = json.loads(raw_api1_json)
        results = (data.get("data") or {}).get("api_results", [])
        if not results or results[0] is None:
            return prices
        features = (results[0].get("data") or {}).get("features", [])

        mansion_types = ["マンション", "区分所有", "住宅", "共同住宅"]
        exclude_types = ["宅地(土地)", "土地", "農地", "林地"]

        for f in features:
            props    = f.get("properties") or {}
            category = props.get("PriceCategory") or ""
            tx_type  = props.get("Type") or ""

            if "貸借" in category:
                continue
            if any(ex in tx_type for ex in exclude_types):
                continue
            if not any(t in tx_type for t in mansion_types):
                continue

            for key in ("UnitPrice", "PricePerUnit"):
                v = props.get(key)
                if v and str(v).strip() not in ("", "不明"):
                    try:
                        yen_per_sqm = float(str(v).replace(",", "").replace("円", ""))
                        man_per_sqm = yen_per_sqm / 10_000   # 円/㎡ → 万円/㎡
                        if 1.0 <= man_per_sqm <= 3_000.0:    # 現実的な範囲でフィルタ
                            prices.append(man_per_sqm)
                    except Exception:
                        pass
                    break

    except Exception as e:
        logger.warning(f"extract_mansion_unit_prices failed: {e}")

    return prices


def build_income_block(
    raw_api1_json: str,
    address: str,
    mode: str,
    maisoku_data: dict | None = None,
) -> str | None:
    """収益指標をPythonで確定計算し、Claudeへのコンテキストブロックを返す。

    Args:
        raw_api1_json: API 1 の生JSONレスポンス文字列
        address:       調査対象の住所・建物名（エリア判定に使用）
        mode:          "location" / "assessment" / "investment_kubun" / "investment_ittou"
        maisoku_data:  マイソク抽出データ（Mode 3 で専有面積・売出価格等に使用）

    Returns:
        確定計算結果のテキストブロック。計算不能な場合は None。
    """
    area_label, base_yield = classify_yield(address)

    building_name           = (maisoku_data or {}).get("building_name", "") or address
    corrected_yield, corrs  = apply_yield_corrections(base_yield, building_name)

    unit_prices = extract_mansion_unit_prices(raw_api1_json)
    if not unit_prices:
        logger.info("build_income_block: no mansion unit prices found")
        return None

    median_uprice = statistics.median(unit_prices)   # 万円/㎡
    n             = len(unit_prices)

    lines = [
        "─────────────",
        "【Python確定値：収益指標】",
        f"エリア区分：{area_label}",
        f"想定利回り：{corrected_yield}%",
    ]
    if corrs:
        lines.append(f"補正内容　：{'/ '.join(corrs)}")
    lines.append(f"取引㎡単価：{median_uprice:.0f}万円/㎡（{n}件・中央値）")

    # ── location / assessment：間取り別賃料レンジ ───────────────
    if mode in ("location", "assessment"):
        for room_type, sqm in [("1K/1R", 22), ("1LDK", 40), ("2LDK", 60)]:
            monthly = median_uprice * sqm * corrected_yield / 100 / 12
            lines.append(f"{room_type}推定賃料：約{monthly:.1f}万円/月")

    # ── investment：収益還元価格まで計算 ────────────────────────
    elif mode in ("investment_kubun", "investment_ittou"):
        area_sqm = None
        if maisoku_data:
            area_sqm = (
                maisoku_data.get("exclusive_area")
                or maisoku_data.get("building_area")
            )

        if area_sqm:
            area_sqm_f = float(area_sqm)

            # 一棟で年間収入が提供されている場合はそちらを優先
            annual_revenue = (maisoku_data or {}).get("annual_revenue")
            if annual_revenue and mode == "investment_ittou":
                annual_rent  = float(annual_revenue)
                monthly_rent = annual_rent / 12
                income_value = annual_rent / (corrected_yield / 100)
                lines.append(f"実績年間収入：{annual_rent:.0f}万円（提供値）")
                lines.append(f"推定月額賃料：{monthly_rent:.1f}万円/月")
                lines.append(f"収益還元価格：{income_value:.0f}万円")
            else:
                estimated_mkt = median_uprice * area_sqm_f   # 周辺相場換算価格（万円）
                monthly_rent  = estimated_mkt * corrected_yield / 100 / 12
                annual_rent   = monthly_rent * 12
                income_value  = annual_rent / (corrected_yield / 100)  # = estimated_mkt
                lines.append(f"推定月額賃料：約{monthly_rent:.1f}万円/月")
                lines.append(f"推定年間賃料：約{annual_rent:.0f}万円/年")
                lines.append(f"収益還元価格：約{income_value:.0f}万円")

            # 売出価格との比較
            asking_price = (maisoku_data or {}).get("price")
            if asking_price:
                asking_f = float(asking_price)
                diff     = asking_f - income_value
                pct      = (diff / income_value) * 100
                verdict  = "割高" if diff > 0 else "割安"
                lines.append(
                    f"売出比較　：{asking_f:.0f}万円"
                    f" / {diff:+.0f}万円（{pct:+.1f}%・{verdict}）"
                )
        else:
            # 面積不明の場合は㎡単価ベースの参考値
            monthly_per_sqm = median_uprice * corrected_yield / 100 / 12
            lines.append(f"推定賃料　：約{monthly_per_sqm:.2f}万円/㎡/月")

    lines += [
        "─────────────",
        "※収益指標はPythonで確定計算済みです。",
        "※上記の値をそのまま使用し、独自に再計算しないこと。",
    ]

    result = "\n".join(lines)
    logger.info(
        f"build_income_block: area={area_label}, yield={corrected_yield}%,"
        f" n={n}, median={median_uprice:.0f}万円/㎡"
    )
    return result
