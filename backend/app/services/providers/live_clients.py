from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import httpx

from app.core.settings import get_settings
from app.services.analysis_safety import round_or_none, safe_ratio, winsorized_mean
from app.utils.cache import TTLCache


def _safe_number(value: object, default: float = 0.0) -> float:
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, dict):
        if "raw" in value:
            return _safe_number(value.get("raw"), default)
        if "reportedValue" in value:
            return _safe_number(value.get("reportedValue"), default)
        if "value" in value:
            return _safe_number(value.get("value"), default)
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _series_latest(entries: list[dict]) -> float:
    if not entries:
        return 0.0
    ordered = sorted(entries, key=lambda item: item.get("fy") or item.get("end") or "")
    return _safe_number(ordered[-1].get("val"))


def _series_annual(entries: list[dict], limit: int = 4) -> list[dict]:
    annual = [item for item in entries if item.get("form") in {"10-K", "20-F", "10-K/A"}]
    ordered = sorted(annual, key=lambda item: item.get("fy") or item.get("end") or "", reverse=True)
    return ordered[:limit]


def _map_sector(sic_description: str) -> str:
    text = (sic_description or "").lower()
    mapping = {
        "Technology": ["software", "semiconductor", "computer", "electronic", "internet", "data"],
        "Financial Services": ["bank", "financial", "insurance", "capital", "asset"],
        "Healthcare": ["pharmaceutical", "biotech", "medical", "health", "laborator"],
        "Energy": ["oil", "gas", "petroleum", "energy", "drilling"],
        "Industrials": ["industrial", "aerospace", "machinery", "railroad", "transport"],
        "Consumer Cyclical": ["retail", "automobile", "restaurant", "apparel", "travel", "lodging"],
        "Communication Services": ["media", "telecom", "communication", "broadcast", "entertainment"],
    }
    for sector, fragments in mapping.items():
        if any(fragment in text for fragment in fragments):
            return sector
    return "Unknown"


@dataclass
class ProviderResult:
    payload: dict
    warnings: list[str]


class BaseHttpProvider:
    def __init__(self) -> None:
        settings = get_settings()
        self.timeout = settings.request_timeout_seconds
        self.cache = TTLCache(ttl_seconds=settings.provider_cache_ttl_seconds, max_items=512)
        self.headers = {
            "User-Agent": settings.sec_user_agent,
            "Accept": "application/json,text/plain,*/*",
        }

    def _get_json(self, url: str, params: dict | None = None, headers: dict | None = None) -> dict | list:
        cache_key = f"{url}|{params}"
        cached = self.cache.get(cache_key)
        if cached is not None:
            return cached

        last_error: Exception | None = None
        for _ in range(2):
            try:
                use_verify = False if "sec.gov" in url else True
                response = httpx.get(
                    url,
                    params=params,
                    headers=headers or self.headers,
                    timeout=self.timeout,
                    follow_redirects=True,
                    verify=use_verify,
                )
                response.raise_for_status()
                payload = response.json()
                self.cache.set(cache_key, payload)
                return payload
            except Exception as exc:
                last_error = exc
        if last_error:
            raise last_error
        raise RuntimeError("HTTP request failed without a captured error.")


class YahooFinanceProvider(BaseHttpProvider):
    source_name = "Yahoo Finance"

    def fetch_company_bundle(self, ticker: str) -> ProviderResult:
        chart = self._get_json(
            f"https://query2.finance.yahoo.com/v8/finance/chart/{ticker}",
            params={"range": "5y", "interval": "1mo", "includeAdjustedClose": "true"},
            headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json,text/plain,*/*"},
        )
        chart_result = chart["chart"]["result"][0]
        meta = chart_result.get("meta", {})
        timestamps = chart_result.get("timestamp", [])
        closes = chart_result["indicators"]["quote"][0].get("close", [])
        price_history = [
            {
                "date": datetime.utcfromtimestamp(ts).strftime("%Y-%m-%d"),
                "close": round(_safe_number(close), 2),
            }
            for ts, close in zip(timestamps, closes)
            if close is not None
        ]
        closes_only = [point["close"] for point in price_history if point["close"] > 0]
        one_year_window = closes_only[-12:] if len(closes_only) >= 12 else closes_only
        five_year_return = 0.0
        one_year_return = 0.0
        if len(closes_only) >= 2:
            five_year_return = ((closes_only[-1] / closes_only[0]) - 1) * 100
        if len(one_year_window) >= 2:
            one_year_return = ((one_year_window[-1] / one_year_window[0]) - 1) * 100

        payload = {
            "company": meta.get("longName") or meta.get("shortName") or ticker,
            "currency": meta.get("currency", "USD"),
            "current_price": _safe_number(meta.get("regularMarketPrice") or meta.get("previousClose")),
            "one_year_return_pct": round(one_year_return, 2),
            "five_year_return_pct": round(five_year_return, 2),
            "price_history": price_history[-24:],
        }
        return ProviderResult(payload=payload, warnings=[])


class SecEdgarProvider(BaseHttpProvider):
    source_name = "SEC EDGAR"

    def _ticker_to_cik(self, ticker: str) -> str:
        mapping = self._get_json("https://www.sec.gov/files/company_tickers.json")
        normalized = ticker.upper()
        for item in mapping.values():
            if item["ticker"].upper() == normalized:
                return f"{int(item['cik_str']):010d}"
        raise KeyError(f"SEC не нашёл CIK для тикера {normalized}.")

    def _fact_series(self, facts: dict, taxonomy: str, concept: str) -> list[dict]:
        return facts.get("facts", {}).get(taxonomy, {}).get(concept, {}).get("units", {}).get("USD", [])

    def fetch_company_bundle(self, ticker: str) -> ProviderResult:
        cik = self._ticker_to_cik(ticker)
        facts = self._get_json(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json")
        submissions = self._get_json(f"https://data.sec.gov/submissions/CIK{cik}.json")

        revenue_series = _series_annual(
            self._fact_series(facts, "us-gaap", "RevenueFromContractWithCustomerExcludingAssessedTax")
            or self._fact_series(facts, "us-gaap", "Revenues")
        )
        cfo_series = _series_annual(self._fact_series(facts, "us-gaap", "NetCashProvidedByUsedInOperatingActivities"))
        capex_series = _series_annual(
            self._fact_series(facts, "us-gaap", "PaymentsToAcquirePropertyPlantAndEquipment")
            or self._fact_series(facts, "us-gaap", "CapitalExpendituresIncurredButNotYetPaid")
        )
        equity_series = _series_annual(self._fact_series(facts, "us-gaap", "StockholdersEquity"))
        current_assets_series = _series_annual(self._fact_series(facts, "us-gaap", "AssetsCurrent"))
        current_liabilities_series = _series_annual(self._fact_series(facts, "us-gaap", "LiabilitiesCurrent"))
        liabilities_series = _series_annual(self._fact_series(facts, "us-gaap", "Liabilities"))
        assets_series = _series_annual(self._fact_series(facts, "us-gaap", "Assets"))
        operating_income_series = _series_annual(self._fact_series(facts, "us-gaap", "OperatingIncomeLoss"))
        net_income_series = _series_annual(self._fact_series(facts, "us-gaap", "NetIncomeLoss"))
        shares_series = _series_annual(
            facts.get("facts", {}).get("dei", {}).get("EntityCommonStockSharesOutstanding", {}).get("units", {}).get("shares", [])
        )

        revenues = [_safe_number(item.get("val")) / 1_000_000_000 for item in revenue_series]
        cfo = [_safe_number(item.get("val")) / 1_000_000_000 for item in cfo_series]
        capex = [abs(_safe_number(item.get("val"))) / 1_000_000_000 for item in capex_series]
        fcf = [round(cfo_item - capex_item, 2) for cfo_item, capex_item in zip(cfo, capex)]
        latest_equity = _series_latest(equity_series)
        latest_assets = _series_latest(assets_series)
        latest_liabilities = _series_latest(liabilities_series)
        latest_current_assets = _series_latest(current_assets_series)
        latest_current_liabilities = _series_latest(current_liabilities_series)
        latest_operating_income = _series_latest(operating_income_series)
        latest_net_income = _series_latest(net_income_series)
        latest_shares_outstanding = _series_latest(shares_series)
        sic_description = submissions.get("sicDescription") or ""

        latest_revenue = revenues[0] if revenues else 0.0
        latest_fcf = fcf[0] if fcf else 0.0
        current_ratio = safe_ratio(latest_current_assets, latest_current_liabilities)
        debt_to_equity = safe_ratio(max(latest_liabilities - latest_equity, 0), latest_equity)
        roic_pct = safe_ratio(latest_operating_income, latest_equity)
        ebit_margin_pct = safe_ratio(latest_operating_income, latest_revenue * 1_000_000_000 if latest_revenue else None)
        fcf_margin_pct = safe_ratio(latest_fcf, latest_revenue)

        payload = {
            "cik": cik,
            "company": submissions.get("name") or facts.get("entityName") or ticker,
            "industry": sic_description or "Unknown",
            "sector": _map_sector(sic_description),
            "revenue_bln": revenues,
            "free_cash_flow_bln": fcf,
            "current_ratio": round_or_none(current_ratio, 2),
            "debt_to_equity": round_or_none(debt_to_equity, 2),
            "roic_pct": round_or_none(roic_pct * 100 if roic_pct is not None else None, 2),
            "ebit_margin_pct": round_or_none(ebit_margin_pct * 100 if ebit_margin_pct is not None else None, 2),
            "fcf_margin_pct": round_or_none(fcf_margin_pct * 100 if fcf_margin_pct is not None else None, 2),
            "net_income_bln": round(latest_net_income / 1_000_000_000, 2),
            "shares_outstanding_mln": round(latest_shares_outstanding / 1_000_000, 2),
            "history": [
                {
                    "period": str(item.get("fy") or item.get("end", "")[:4]),
                    "revenue_bln": revenue,
                    "free_cash_flow_bln": free_cash_flow,
                }
                for item, revenue, free_cash_flow in zip(revenue_series, revenues, fcf)
            ],
            "assets_bln": round(latest_assets / 1_000_000_000, 2),
            "equity_bln": round(latest_equity / 1_000_000_000, 2),
        }
        warnings: list[str] = []
        if not revenues:
            warnings.append("SEC EDGAR не вернул годовую выручку в ожидаемом формате.")
        if not fcf:
            warnings.append("SEC EDGAR не вернул достаточно данных для расчёта свободного денежного потока.")
        if not latest_shares_outstanding:
            warnings.append("SEC EDGAR не вернул число акций в обращении, часть рыночных коэффициентов будет приближённой.")
        if latest_equity <= 0:
            warnings.append("Negative equity detected: ROE, Debt/Equity and P/B may be unreliable")
        return ProviderResult(payload=payload, warnings=warnings)


class FredProvider(BaseHttpProvider):
    source_name = "FRED"

    def __init__(self) -> None:
        super().__init__()
        self.api_key = get_settings().fred_api_key

    def _latest_value(self, series_id: str) -> float | None:
        data = self._get_json(
            "https://api.stlouisfed.org/fred/series/observations",
            params={
                "series_id": series_id,
                "api_key": self.api_key,
                "file_type": "json",
                "sort_order": "desc",
                "limit": 24,
            },
        )
        observations = [item for item in data.get("observations", []) if item.get("value") not in {".", None}]
        if not observations:
            return None
        return _safe_number(observations[0]["value"])

    def fetch_macro_bundle(self) -> ProviderResult:
        if not self.api_key:
            return ProviderResult(
                payload={},
                warnings=["FRED API key не задан, блок макроданных из FRED пропущен."],
            )

        cpi_latest = self._latest_value("CPIAUCSL")
        cpi_prev_year = self._get_json(
            "https://api.stlouisfed.org/fred/series/observations",
            params={
                "series_id": "CPIAUCSL",
                "api_key": self.api_key,
                "file_type": "json",
                "sort_order": "desc",
                "limit": 13,
            },
        )
        cpi_values = [item for item in cpi_prev_year.get("observations", []) if item.get("value") not in {".", None}]
        inflation = None
        if len(cpi_values) >= 13:
            inflation = ((_safe_number(cpi_values[0]["value"]) / _safe_number(cpi_values[12]["value"])) - 1) * 100

        return ProviderResult(
            payload={
                "fed_funds_rate_pct": self._latest_value("FEDFUNDS"),
                "unemployment_pct": self._latest_value("UNRATE"),
                "inflation_pct": round(inflation, 2) if inflation is not None else None,
            },
            warnings=[],
        )


class WorldBankProvider(BaseHttpProvider):
    source_name = "World Bank"

    def fetch_macro_bundle(self) -> ProviderResult:
        payload = self._get_json(
            "https://api.worldbank.org/v2/country/USA/indicator/NY.GDP.MKTP.KD.ZG",
            params={"format": "json", "per_page": 10},
        )
        series = payload[1] if isinstance(payload, list) and len(payload) > 1 else []
        latest = next((item for item in series if item.get("value") is not None), None)
        return ProviderResult(
            payload={"gdp_growth_pct": _safe_number(latest.get("value")) if latest else None},
            warnings=[],
        )


def summarize_peer_averages(rows: list[dict]) -> dict[str, float | None]:
    if not rows:
        return {"pe_ratio": None, "pb_ratio": None, "roe_pct": None, "revenue_growth_pct": None, "debt_to_equity": None}

    def collect(metric: str) -> list[float]:
        return [float(value) for row in rows if (value := row.get(metric)) is not None]

    return {
        "pe_ratio": round_or_none(winsorized_mean(collect("pe_ratio")), 2),
        "pb_ratio": round_or_none(winsorized_mean(collect("pb_ratio")), 2),
        "roe_pct": round_or_none(winsorized_mean(collect("roe_pct")), 2),
        "revenue_growth_pct": round_or_none(winsorized_mean(collect("revenue_growth_pct")), 2),
        "debt_to_equity": round_or_none(winsorized_mean(collect("debt_to_equity")), 2),
    }
