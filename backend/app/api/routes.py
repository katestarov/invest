import httpx
from fastapi import APIRouter, HTTPException

from app.schemas.analysis import AnalysisResponse
from app.services.analysis_runtime_service import AnalysisService


router = APIRouter()
service = AnalysisService()


@router.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@router.post("/cache/clear")
def clear_cache() -> dict[str, str]:
    service.clear_cache()
    return {"status": "cache cleared"}


@router.get("/analyze/{ticker}", response_model=AnalysisResponse)
def analyze_company(ticker: str) -> AnalysisResponse:
    try:
        return service.analyze(ticker)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"Ошибка внешнего источника: {exc}") from exc
