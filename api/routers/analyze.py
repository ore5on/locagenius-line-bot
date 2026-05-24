"""
PrimeAsset Web API — 分析エンドポイント

POST /api/analyze/location    : 立地調査
POST /api/analyze/assessment  : 資産性評価
POST /api/analyze/investment  : 投資分析（区分 / 一棟）
POST /api/analyze/url         : 物件URL → 情報抽出
POST /api/analyze/maisoku     : マイソクファイル → 情報抽出

全エンドポイントは modes/ のコアロジックを呼び出す。
LINE 固有の処理（push / download_content）は一切含まない。
"""

import logging
from typing import Literal

from fastapi import APIRouter, HTTPException, UploadFile, File
from pydantic import BaseModel, HttpUrl

from modes.location   import run_location_analysis
from modes.assessment import run_assessment_analysis
from modes.investment import run_investment_core
from modes.url_property import fetch_and_extract_url
from core.maisoku     import extract_property

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/analyze", tags=["analyze"])


# ──────────────────────────────────────
# リクエスト / レスポンス スキーマ
# ──────────────────────────────────────
class AnalysisResponse(BaseModel):
    """分析レポートのレスポンス共通スキーマ"""
    report: str
    mode: str


class LocationRequest(BaseModel):
    address: str


class AssessmentRequest(BaseModel):
    building_name: str


class InvestmentRequest(BaseModel):
    address: str | None = None
    building_name: str | None = None
    property_category: Literal["区分", "一棟"] = "区分"
    price: float | None = None
    exclusive_area: float | None = None
    land_area: float | None = None
    building_area: float | None = None
    structure: str | None = None
    built_year: int | None = None
    management_fee: float | None = None
    repair_fund: float | None = None
    total_units: int | None = None
    annual_revenue: float | None = None
    rosenka: float | None = None


class UrlRequest(BaseModel):
    url: str


class ExtractResponse(BaseModel):
    """マイソク / URL 抽出結果のレスポンス"""
    extracted: dict
    needs_type_selection: bool


# ──────────────────────────────────────
# エンドポイント
# ──────────────────────────────────────
@router.post("/location", response_model=AnalysisResponse)
async def analyze_location(req: LocationRequest):
    """住所・地名から立地調査レポートを生成する（Mode 1）"""
    if not req.address.strip():
        raise HTTPException(status_code=422, detail="address は必須です")

    logger.info(f"[Web API] location: {req.address}")
    report = await run_location_analysis(req.address)
    return AnalysisResponse(report=report, mode="location")


@router.post("/assessment", response_model=AnalysisResponse)
async def analyze_assessment(req: AssessmentRequest):
    """マンション名・住所から資産性評価レポートを生成する（Mode 2）"""
    if not req.building_name.strip():
        raise HTTPException(status_code=422, detail="building_name は必須です")

    logger.info(f"[Web API] assessment: {req.building_name}")
    report = await run_assessment_analysis(req.building_name)
    return AnalysisResponse(report=report, mode="assessment")


@router.post("/investment", response_model=AnalysisResponse)
async def analyze_investment(req: InvestmentRequest):
    """マイソク抽出データから投資分析レポートを生成する（Mode 3）

    address または building_name のどちらかが必須。
    property_category で区分/一棟を指定する（Web版ではUIで選択済み）。
    """
    if not req.address and not req.building_name:
        raise HTTPException(status_code=422, detail="address または building_name が必要です")

    extracted = req.model_dump(exclude_none=True)
    logger.info(f"[Web API] investment: {extracted.get('address') or extracted.get('building_name')} ({extracted.get('property_category')})")
    report = await run_investment_core(extracted)
    return AnalysisResponse(report=report, mode=f"investment_{extracted.get('property_category', '区分')}")


@router.post("/url", response_model=ExtractResponse)
async def analyze_url(req: UrlRequest):
    """物件ページURLから物件情報を抽出して返す。

    投資分析はクライアント側で区分/一棟を選択した後、
    /api/analyze/investment に extracted を渡して実行する。
    """
    if not req.url.strip():
        raise HTTPException(status_code=422, detail="url は必須です")

    logger.info(f"[Web API] url extract: {req.url}")
    extracted = await fetch_and_extract_url(req.url)
    if not extracted:
        raise HTTPException(
            status_code=422,
            detail="URLから物件情報を取得できませんでした。マイソクを直接アップロードしてください。"
        )

    needs_type_selection = "property_category" not in extracted
    return ExtractResponse(extracted=extracted, needs_type_selection=needs_type_selection)


@router.post("/maisoku", response_model=ExtractResponse)
async def analyze_maisoku(file: UploadFile = File(...)):
    """マイソクファイル（画像 / PDF）から物件情報を抽出して返す。

    投資分析はクライアント側で区分/一棟を選択した後、
    /api/analyze/investment に extracted を渡して実行する。
    """
    content_type = file.content_type or ""
    if content_type not in ("image/jpeg", "image/png", "application/pdf"):
        raise HTTPException(
            status_code=415,
            detail="対応形式：JPEG / PNG / PDF"
        )

    # PNG は Claude API の image/jpeg として送信（PNG も jpeg エンコードで受け付ける）
    media_type = "application/pdf" if content_type == "application/pdf" else "image/jpeg"

    logger.info(f"[Web API] maisoku upload: {file.filename} ({content_type})")
    content_bytes = await file.read()
    extracted = await extract_property(media_type, content_bytes)

    if not extracted:
        raise HTTPException(
            status_code=422,
            detail="マイソクから物件情報を読み取れませんでした。画像が鮮明か確認してください。"
        )

    needs_type_selection = "property_category" not in extracted
    return ExtractResponse(extracted=extracted, needs_type_selection=needs_type_selection)
