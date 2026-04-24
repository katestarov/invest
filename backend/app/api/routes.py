import httpx
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path

from app.schemas.analysis import AnalysisResponse
from app.services.analysis_runtime_service import AnalysisService


router = APIRouter()
service = AnalysisService()


def get_analysis_service() -> AnalysisService:
    return service


@router.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@router.post("/cache/clear")
def clear_cache(analysis_service: Annotated[AnalysisService, Depends(get_analysis_service)]) -> dict[str, str]:
    analysis_service.clear_cache()
    return {"status": "cache cleared"}


@router.get("/analyze/{ticker}", response_model=AnalysisResponse)
def analyze_company(
    ticker: Annotated[
        str,
        Path(
            min_length=1,
            max_length=16,
            pattern=r"^[A-Za-z0-9]{1,16}$",
            description="Ticker symbol containing only Latin letters and digits.",
        ),
    ],
    analysis_service: Annotated[AnalysisService, Depends(get_analysis_service)],
) -> AnalysisResponse:
    try:
        return analysis_service.analyze(ticker)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except httpx.TimeoutException as exc:
        raise HTTPException(status_code=504, detail="Таймаут внешнего API") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"Ошибка внешнего источника: {exc}") from exc
