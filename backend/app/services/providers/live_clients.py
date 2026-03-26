from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import logging
import httpx

from app.core.settings import get_settings
from app.services.analysis_safety import robust_baseline, round_or_none, safe_ratio
from app.utils.cache import TTLCache

logger = logging.getLogger(__name__)

def _safe_number(value: object, default: float | None = None) -> float | None:
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


def _series_latest(entries: list[dict]) -> float | None:
    if not entries:
        return None
    ordered = sorted(entries, key=lambda item: item.get("fy") or item.get("end") or "")
    return _safe_number(ordered[-1].get("val"))


def _parse_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value[:10])
    except ValueError:
        return None


def _is_annual_period(item: dict) -> bool:
    form = str(item.get("form") or "").upper()
    fp = str(item.get("fp") or "").upper()
    start_date = _parse_date(item.get("start"))
    end_date = _parse_date(item.get("end"))
    if form not in {"10-K", "20-F", "10-K/A", "20-F/A", "40-F", "40-F/A"}:
        return False
    if fp == "FY":
        return True
    if start_date and end_date:
        duration_days = (end_date - start_date).days
        return 330 <= duration_days <= 380
    return False


def _series_annual(entries: list[dict], limit: int = 4) -> list[dict]:
    annual = [item for item in entries if _is_annual_period(item)]
    deduped: dict[str, dict] = {}
    for item in annual:
        dedupe_key = f"{item.get('fy') or ''}|{item.get('end') or ''}"
        current = deduped.get(dedupe_key)
        if current is None:
            deduped[dedupe_key] = item
            continue
        current_filed = _parse_date(current.get("filed"))
        item_filed = _parse_date(item.get("filed"))
        if item_filed and (current_filed is None or item_filed > current_filed):
            deduped[dedupe_key] = item
    ordered = sorted(
        deduped.values(),
        key=lambda item: (_parse_date(item.get("end")) or datetime.min, str(item.get("fy") or "")),
        reverse=True,
    )
    return ordered[:limit]


def _series_instant(entries: list[dict], limit: int = 4) -> list[dict]:
    instant_forms = {"10-K", "20-F", "10-K/A", "20-F/A", "40-F", "40-F/A"}
    instant = [item for item in entries if str(item.get("form") or "").upper() in instant_forms]
    deduped: dict[str, dict] = {}
    for item in instant:
        dedupe_key = str(item.get("end") or "")
        current = deduped.get(dedupe_key)
        if current is None:
            deduped[dedupe_key] = item
            continue
        current_filed = _parse_date(current.get("filed"))
        item_filed = _parse_date(item.get("filed"))
        if item_filed and (current_filed is None or item_filed > current_filed):
            deduped[dedupe_key] = item
    ordered = sorted(
        deduped.values(),
        key=lambda item: (_parse_date(item.get("end")) or datetime.min, _parse_date(item.get("filed")) or datetime.min),
        reverse=True,
    )
    return ordered[:limit]


def _first_series(facts: dict, taxonomy: str, concepts: list[str], unit: str = "USD") -> list[dict]:
    for concept in concepts:
        series = facts.get("facts", {}).get(taxonomy, {}).get(concept, {}).get("units", {}).get(unit, [])
        if series:
            return series
    return []


def _period_key(item: dict) -> str | None:
    end = str(item.get("end") or "").strip()
    if end:
        return end[:10]
    fy = str(item.get("fy") or "").strip()
    fp = str(item.get("fp") or "").strip()
    if fy or fp:
        return f"{fy}|{fp}"
    return None


def _annual_value_points(entries: list[dict], *, scale: float = 1.0, absolute: bool = False) -> list[dict[str, object]]:
    points: list[dict[str, object]] = []
    for item in entries:
        key = _period_key(item)
        numeric_value = _safe_number(item.get("val"))
        if key is None or numeric_value is None:
            continue
        value = abs(numeric_value) if absolute else numeric_value
        points.append({"key": key, "item": item, "value": value / scale})
    return points


def _sum_present(*values: float | None) -> float | None:
    present = [value for value in values if value is not None]
    if not present:
        return None
    return sum(present)


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
        self.client = httpx.Client(
            timeout=self.timeout,
            follow_redirects=True,
            headers=self.headers,
            verify=True,
        )

    def close(self) -> None:
        self.client.close()

    def _get_json(self, url: str, params: dict | None = None, headers: dict | None = None) -> dict | list:
        cache_key = f"{url}|{params}"
        cached = self.cache.get(cache_key)
        if cached is not None:
            return cached

        last_error: Exception | None = None
        for attempt in range(2):
            try:
                response = self.client.get(
                    url,
                    params=params,
                    headers=headers,
                )
                response.raise_for_status()
                payload = response.json()
                self.cache.set(cache_key, payload)
                return payload
            except Exception as exc:
                last_error = exc
                logger.warning(
                    "http_provider_request_failed",
                    extra={"url": url, "attempt": attempt + 1, "error_type": type(exc).__name__},
                )
        if last_error:
            raise last_error
        raise RuntimeError("HTTP request failed without a captured error.")


class YahooFinanceProvider(BaseHttpProvider):
    source_name = "Yahoo Finance"

    def fetch_company_bundle(self, ticker: str) -> ProviderResult:
        warnings: list[str] = []
        chart = self._get_json(
            f"https://query2.finance.yahoo.com/v8/finance/chart/{ticker}",
            params={"range": "5y", "interval": "1mo", "includeAdjustedClose": "true"},
            headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json,text/plain,*/*"},
        )
        quote_result: dict[str, object] = {}
        try:
            quote = self._get_json(
                "https://query1.finance.yahoo.com/v7/finance/quote",
                params={"symbols": ticker},
                headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json,text/plain,*/*"},
            )
            quote_items = quote.get("quoteResponse", {}).get("result", []) if isinstance(quote, dict) else []
            if quote_items:
                quote_result = quote_items[0]
        except Exception as exc:
            logger.warning(
                "yahoo_quote_snapshot_unavailable",
                extra={"ticker": ticker, "error_type": type(exc).__name__},
            )
        chart_result = chart["chart"]["result"][0]
        meta = chart_result.get("meta", {})
        timestamps = chart_result.get("timestamp", [])
        closes = chart_result["indicators"]["quote"][0].get("close", [])
        price_history: list[dict[str, object]] = []
        for ts, close in zip(timestamps, closes):
            parsed_close = _safe_number(close)
            if parsed_close is None:
                continue
            price_history.append(
                {
                    "date": datetime.utcfromtimestamp(ts).strftime("%Y-%m-%d"),
                    "close": round(parsed_close, 2),
                }
            )
        closes_only = [point["close"] for point in price_history if point["close"] > 0]
        one_year_window = closes_only[-12:] if len(closes_only) >= 12 else closes_only
        five_year_return = 0.0
        one_year_return = 0.0
        if len(closes_only) >= 2:
            five_year_return = ((closes_only[-1] / closes_only[0]) - 1) * 100
        if len(one_year_window) >= 2:
            one_year_return = ((one_year_window[-1] / one_year_window[0]) - 1) * 100

        payload = {
            "ticker": ticker,
            "company": meta.get("longName") or meta.get("shortName") or ticker,
            "currency": quote_result.get("currency") or meta.get("currency", "USD"),
            "current_price": round_or_none(_safe_number(meta.get("regularMarketPrice") or meta.get("previousClose")), 2),
            "market_cap_bln_quote": round_or_none(
                _safe_number(quote_result.get("marketCap")) / 1_000_000_000 if _safe_number(quote_result.get("marketCap")) is not None else None,
                2,
            ),
            "market_cap_quote_currency": quote_result.get("currency") or meta.get("currency", "USD"),
            "shares_outstanding_quote_mln": round_or_none(
                _safe_number(quote_result.get("sharesOutstanding")) / 1_000_000 if _safe_number(quote_result.get("sharesOutstanding")) is not None else None,
                2,
            ),
            "quote_type": quote_result.get("quoteType"),
            "one_year_return_pct": round(one_year_return, 2),
            "five_year_return_pct": round(five_year_return, 2),
            "price_history": price_history[-24:],
        }
        return ProviderResult(payload=payload, warnings=warnings)


class SecEdgarProvider(BaseHttpProvider):
    source_name = "SEC EDGAR"

    def _ticker_to_cik(self, ticker: str) -> str:
        mapping = self._get_json("https://www.sec.gov/files/company_tickers.json")
        normalized = ticker.upper()
        for item in mapping.values():
            if item["ticker"].upper() == normalized:
                return f"{int(item['cik_str']):010d}"
        raise KeyError(f"SEC не нашёл CIK для тикера {normalized}.")

    def fetch_company_bundle(self, ticker: str) -> ProviderResult:
        cik = self._ticker_to_cik(ticker)
        facts = self._get_json(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json")
        submissions = self._get_json(f"https://data.sec.gov/submissions/CIK{cik}.json")

        revenue_series = _series_annual(
            _first_series(
                facts,
                "us-gaap",
                [
                    "RevenueFromContractWithCustomerExcludingAssessedTax",
                    "SalesRevenueNet",
                    "RevenueFromContractWithCustomerIncludingAssessedTax",
                    "Revenues",
                ],
            )
        )
        cfo_series = _series_annual(
            _first_series(
                facts,
                "us-gaap",
                [
                    "NetCashProvidedByUsedInOperatingActivities",
                    "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations",
                ],
            )
        )
        capex_series = _series_annual(
            _first_series(
                facts,
                "us-gaap",
                [
                    "PaymentsToAcquirePropertyPlantAndEquipment",
                    "CapitalExpendituresIncurredButNotYetPaid",
                    "PropertyPlantAndEquipmentAdditions",
                ],
            )
        )
        equity_series = _series_instant(
            _first_series(
                facts,
                "us-gaap",
                [
                    "StockholdersEquity",
                    "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
                    "PartnersCapitalIncludingPortionAttributableToNoncontrollingInterest",
                ],
            )
        )
        current_assets_series = _series_instant(_first_series(facts, "us-gaap", ["AssetsCurrent"]))
        current_liabilities_series = _series_instant(_first_series(facts, "us-gaap", ["LiabilitiesCurrent"]))
        liabilities_series = _series_instant(
            _first_series(
                facts,
                "us-gaap",
                [
                    "Liabilities",
                    "LiabilitiesFairValueDisclosure",
                ],
            )
        )
        debt_series = _series_instant(
            _first_series(
                facts,
                "us-gaap",
                [
                    "LongTermDebtAndFinanceLeaseObligations",
                    "LongTermDebtNoncurrent",
                    "LongTermDebt",
                ],
            )
        )
        current_debt_series = _series_instant(
            _first_series(
                facts,
                "us-gaap",
                [
                    "LongTermDebtCurrent",
                    "ShortTermBorrowings",
                    "ShortTermDebt",
                    "CommercialPaper",
                ],
            )
        )
        cash_series = _series_instant(
            _first_series(
                facts,
                "us-gaap",
                [
                    "CashAndCashEquivalentsAtCarryingValue",
                    "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents",
                ],
            )
        )
        assets_series = _series_instant(_first_series(facts, "us-gaap", ["Assets"]))
        operating_income_series = _series_annual(_first_series(facts, "us-gaap", ["OperatingIncomeLoss"]))
        pretax_income_series = _series_annual(_first_series(facts, "us-gaap", ["IncomeBeforeTaxExpenseBenefit"]))
        tax_expense_series = _series_annual(_first_series(facts, "us-gaap", ["IncomeTaxExpenseBenefit"]))
        net_income_series = _series_annual(
            _first_series(
                facts,
                "us-gaap",
                [
                    "NetIncomeLoss",
                    "ProfitLoss",
                    "NetIncomeLossAvailableToCommonStockholdersBasic",
                ],
            )
        )
        shares_series = _series_instant(
            facts.get("facts", {}).get("dei", {}).get("EntityCommonStockSharesOutstanding", {}).get("units", {}).get("shares", [])
        )

        revenue_points = _annual_value_points(revenue_series, scale=1_000_000_000)
        cfo_points = _annual_value_points(cfo_series, scale=1_000_000_000)
        capex_points = _annual_value_points(capex_series, scale=1_000_000_000, absolute=True)
        revenues = [round_or_none(point["value"], 2) for point in revenue_points]
        cfo_by_key = {str(point["key"]): float(point["value"]) for point in cfo_points}
        capex_by_key = {str(point["key"]): float(point["value"]) for point in capex_points}
        fcf_by_key = {
            key: round(cfo_by_key[key] - capex_by_key[key], 2)
            for key in cfo_by_key.keys() & capex_by_key.keys()
        }
        latest_equity = _series_latest(equity_series)
        latest_assets = _series_latest(assets_series)
        latest_liabilities = _series_latest(liabilities_series)
        latest_debt = _series_latest(debt_series)
        latest_current_debt = _series_latest(current_debt_series)
        latest_cash = _series_latest(cash_series)
        latest_current_assets = _series_latest(current_assets_series)
        latest_current_liabilities = _series_latest(current_liabilities_series)
        latest_operating_income = _series_latest(operating_income_series)
        latest_pretax_income = _series_latest(pretax_income_series)
        latest_tax_expense = _series_latest(tax_expense_series)
        latest_net_income = _series_latest(net_income_series)
        latest_shares_outstanding = _series_latest(shares_series)
        sic_description = submissions.get("sicDescription") or ""

        history = [
            {
                "period": str(point["item"].get("fy") or str(point["item"].get("end", ""))[:4]),
                "revenue_bln": round(float(point["value"]), 2),
                "free_cash_flow_bln": round_or_none(fcf_by_key.get(str(point["key"])), 2),
            }
            for point in revenue_points
        ]
        latest_revenue = history[0]["revenue_bln"] if history else None
        latest_fcf = history[0]["free_cash_flow_bln"] if history else None
        current_ratio = safe_ratio(latest_current_assets, latest_current_liabilities)
        total_debt = _sum_present(latest_debt, latest_current_debt)
        fallback_debt = max(latest_liabilities - latest_equity, 0) if latest_liabilities is not None and latest_equity is not None else None
        debt_base = total_debt if total_debt is not None and total_debt > 0 else fallback_debt
        debt_to_equity = safe_ratio(debt_base, latest_equity)
        effective_tax_rate = safe_ratio(latest_tax_expense, latest_pretax_income)
        estimated_tax_rate = effective_tax_rate is None and latest_operating_income is not None
        tax_rate = min(max(effective_tax_rate, 0.0), 0.35) if effective_tax_rate is not None else (0.21 if estimated_tax_rate else None)
        nopat = latest_operating_income * (1 - tax_rate) if latest_operating_income is not None and tax_rate is not None else None
        invested_capital = None
        if latest_equity is not None:
            invested_capital = (total_debt or 0.0) + latest_equity - (latest_cash or 0.0)
            if invested_capital <= 0:
                invested_capital = None
        roic_pct = safe_ratio(nopat, invested_capital)
        ebit_margin_pct = safe_ratio(latest_operating_income, latest_revenue * 1_000_000_000 if latest_revenue is not None else None)
        fcf_margin_pct = safe_ratio(latest_fcf, latest_revenue)
        revenue_periods = [
            {
                "fy": str(point["item"].get("fy") or ""),
                "end": str(point["item"].get("end") or ""),
                "period_type": "annual",
                "fiscal_period": str(point["item"].get("fp") or ""),
                "form": str(point["item"].get("form") or ""),
            }
            for point in revenue_points
        ]

        payload = {
            "cik": cik,
            "sic": str(submissions.get("sic") or ""),
            "company": submissions.get("name") or facts.get("entityName") or ticker,
            "industry": sic_description or "Unknown",
            "sector": _map_sector(sic_description),
            "revenue_bln": revenues,
            "free_cash_flow_bln": [item["free_cash_flow_bln"] for item in history],
            "current_ratio": round_or_none(current_ratio, 2),
            "debt_to_equity": round_or_none(debt_to_equity, 2),
            "roic_pct": round_or_none(roic_pct * 100 if roic_pct is not None else None, 2),
            "ebit_margin_pct": round_or_none(ebit_margin_pct * 100 if ebit_margin_pct is not None else None, 2),
            "fcf_margin_pct": round_or_none(fcf_margin_pct * 100 if fcf_margin_pct is not None else None, 2),
            "net_income_bln": round_or_none(latest_net_income / 1_000_000_000 if latest_net_income is not None else None, 2),
            "shares_outstanding_mln": round_or_none(latest_shares_outstanding / 1_000_000 if latest_shares_outstanding is not None else None, 2),
            "history": history,
            "assets_bln": round_or_none(latest_assets / 1_000_000_000 if latest_assets is not None else None, 2),
            "equity_bln": round_or_none(latest_equity / 1_000_000_000 if latest_equity is not None else None, 2),
            "revenue_periods": revenue_periods,
        }
        warnings: list[str] = []
        if not revenues:
            warnings.append("SEC EDGAR не вернул годовую выручку в ожидаемом формате.")
        if latest_fcf is None:
            warnings.append("FCF periods could not be aligned for the latest annual period.")
            warnings.append("SEC EDGAR не вернул достаточно данных для расчёта свободного денежного потока.")
        if latest_shares_outstanding is None:
            warnings.append("SEC EDGAR не вернул число акций в обращении, часть рыночных коэффициентов будет приближённой.")
        if latest_equity is not None and latest_equity <= 0:
            warnings.append("Negative equity detected: ROE, Debt/Equity and P/B may be unreliable")
        if estimated_tax_rate:
            warnings.append("ROIC uses an estimated tax rate because tax facts were incomplete.")
        if latest_operating_income is not None and roic_pct is None:
            warnings.append("ROIC is unavailable because invested capital inputs were incomplete or non-interpretable.")
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

    def _year_over_year_pct(self, observations: list[dict]) -> float | None:
        points: list[tuple[datetime, float]] = []
        for item in observations:
            observation_date = _parse_date(item.get("date"))
            value = _safe_number(item.get("value"))
            if observation_date is None or value is None:
                continue
            points.append((observation_date, value))
        if len(points) < 2:
            return None

        ordered = sorted(points, key=lambda item: item[0], reverse=True)
        current_date, current_value = ordered[0]
        prior_candidates = [
            (abs((current_date - date).days - 365), value)
            for date, value in ordered[1:]
            if 330 <= (current_date - date).days <= 400
        ]
        if not prior_candidates:
            prior_candidates = [
                (abs((current_date - date).days - 365), value)
                for date, value in ordered[1:]
                if (current_date - date).days >= 330
            ]
        if not prior_candidates:
            return None

        _, prior_value = min(prior_candidates, key=lambda item: item[0])
        inflation_ratio = safe_ratio(current_value, prior_value)
        return ((inflation_ratio - 1) * 100) if inflation_ratio is not None else None

    def fetch_macro_bundle(self) -> ProviderResult:
        if not self.api_key:
            return ProviderResult(
                payload={},
                warnings=["FRED API key не задан, блок макроданных из FRED пропущен."],
            )

        cpi_series = self._get_json(
            "https://api.stlouisfed.org/fred/series/observations",
            params={
                "series_id": "CPIAUCSL",
                "api_key": self.api_key,
                "file_type": "json",
                "sort_order": "desc",
                "limit": 24,
            },
        )
        inflation = self._year_over_year_pct(cpi_series.get("observations", []) if isinstance(cpi_series, dict) else [])

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
        return {
            "pe_ratio": None,
            "pb_ratio": None,
            "roe_pct": None,
            "revenue_growth_pct": None,
            "debt_to_equity": None,
            "pe_ratio_valid_count": 0,
            "pb_ratio_valid_count": 0,
            "roe_pct_valid_count": 0,
            "revenue_growth_pct_valid_count": 0,
            "debt_to_equity_valid_count": 0,
            "pe_ratio_baseline_noisy": True,
            "pb_ratio_baseline_noisy": True,
            "roe_pct_baseline_noisy": True,
            "revenue_growth_pct_baseline_noisy": True,
            "debt_to_equity_baseline_noisy": True,
        }

    def collect(metric: str) -> list[float]:
        return [float(value) for row in rows if (value := row.get(metric)) is not None]

    pe_ratio, pe_count, pe_noisy = robust_baseline(collect("pe_ratio"), prefer_median=True)
    pb_ratio, pb_count, pb_noisy = robust_baseline(collect("pb_ratio"), prefer_median=True)
    roe_pct, roe_count, roe_noisy = robust_baseline(collect("roe_pct"))
    growth_pct, growth_count, growth_noisy = robust_baseline(collect("revenue_growth_pct"))
    debt_to_equity, debt_count, debt_noisy = robust_baseline(collect("debt_to_equity"))

    return {
        "pe_ratio": round_or_none(pe_ratio, 2),
        "pb_ratio": round_or_none(pb_ratio, 2),
        "roe_pct": round_or_none(roe_pct, 2),
        "revenue_growth_pct": round_or_none(growth_pct, 2),
        "debt_to_equity": round_or_none(debt_to_equity, 2),
        "pe_ratio_valid_count": pe_count,
        "pb_ratio_valid_count": pb_count,
        "roe_pct_valid_count": roe_count,
        "revenue_growth_pct_valid_count": growth_count,
        "debt_to_equity_valid_count": debt_count,
        "pe_ratio_baseline_noisy": pe_noisy,
        "pb_ratio_baseline_noisy": pb_noisy,
        "roe_pct_baseline_noisy": roe_noisy,
        "revenue_growth_pct_baseline_noisy": growth_noisy,
        "debt_to_equity_baseline_noisy": debt_noisy,
    }
