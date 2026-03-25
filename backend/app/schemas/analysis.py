from pydantic import BaseModel, Field


class MetricCard(BaseModel):
    label: str
    value: float | None
    unit: str = ""
    benchmark: float | None
    direction: str
    display_value: str | None = None
    display_benchmark: str | None = None
    comparison_label: str | None = None
    description: str


class ScoreBreakdownItem(BaseModel):
    key: str
    label: str
    score: float | None = Field(default=None, ge=0, le=100)
    weight: float = Field(ge=0, le=1)
    summary: str


class PeerRow(BaseModel):
    ticker: str
    company: str
    sector: str
    industry: str
    score: float = Field(ge=0, le=100)
    market_cap_bln: float | None
    pe_ratio: float | None
    roe_pct: float | None
    revenue_growth_pct: float | None


class FundamentalTrendPoint(BaseModel):
    period: str
    revenue_bln: float
    free_cash_flow_bln: float | None


class PriceHistoryPoint(BaseModel):
    date: str
    close: float


class MacroPoint(BaseModel):
    label: str
    value: float | None
    unit: str
    source: str


class AnalysisResponse(BaseModel):
    ticker: str
    company: str
    sector: str
    industry: str
    score: float = Field(ge=0, le=100)
    verdict: str
    narrative: str
    metric_cards: list[MetricCard]
    score_breakdown: list[ScoreBreakdownItem]
    peers: list[PeerRow]
    fundamentals_history: list[FundamentalTrendPoint]
    price_history: list[PriceHistoryPoint]
    macro: list[MacroPoint]
    assumptions: list[str]
    data_sources: list[str]
    warnings: list[str]
