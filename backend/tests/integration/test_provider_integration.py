from __future__ import annotations

from copy import deepcopy
import json

import httpx
import pytest

from app.core.settings import get_settings
from app.services.providers.live_clients import BaseHttpProvider, FredProvider, SecEdgarProvider, WorldBankProvider, YahooFinanceProvider
from app.services.providers.peer_providers import BusinessTypePeerProvider, ConfigPeerProvider, FmpPeerProvider, FinnhubPeerProvider
from tests.support.fakes import StaticPeerProvider


class _JsonResponse:
    def __init__(self, payload: dict | list, *, url: str = "https://example.com", status_code: int = 200) -> None:
        self._payload = payload
        self._url = url
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            request = httpx.Request("GET", self._url)
            raise httpx.HTTPStatusError(
                f"status {self.status_code}",
                request=request,
                response=httpx.Response(self.status_code, request=request),
            )

    def json(self) -> dict | list:
        return deepcopy(self._payload)


class _SequenceClient:
    def __init__(self, sequence: list[object]) -> None:
        self._sequence = list(sequence)
        self.calls = 0

    def get(self, url: str, **kwargs):
        self.calls += 1
        if not self._sequence:
            raise AssertionError("No more fake responses configured")
        item = self._sequence.pop(0)
        if isinstance(item, Exception):
            raise item
        if callable(item):
            return item(url, **kwargs)
        return item

    def close(self) -> None:
        return None


def _http_status_error(status_code: int, url: str) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", url)
    return httpx.HTTPStatusError(
        f"status {status_code}",
        request=request,
        response=httpx.Response(status_code, request=request),
    )


def test_base_http_provider_uses_cache_for_repeat_requests():
    provider = BaseHttpProvider()
    provider.client = _SequenceClient([_JsonResponse({"ok": True})])

    first = provider._get_json("https://example.com/test", params={"q": "x"})
    second = provider._get_json("https://example.com/test", params={"q": "x"})

    assert first == {"ok": True}
    assert second == {"ok": True}
    assert provider.client.calls == 1


def test_base_http_provider_retries_up_to_configured_attempts(monkeypatch):
    monkeypatch.setenv("PROVIDER_RETRY_ATTEMPTS", "3")
    get_settings.cache_clear()
    provider = BaseHttpProvider()
    try:
        provider.client = _SequenceClient(
            [
                httpx.ReadTimeout("timeout"),
                _http_status_error(502, "https://example.com/test"),
                _JsonResponse({"ok": True}),
            ]
        )
        payload = provider._get_json("https://example.com/test")
    finally:
        get_settings.cache_clear()

    assert payload == {"ok": True}
    assert provider.client.calls == 3
    assert provider.max_attempts == 3


def test_base_http_provider_stops_retry_loop_on_429():
    provider = BaseHttpProvider()
    provider.client = _SequenceClient([_JsonResponse({"error": "rate limited"}, url="https://example.com/test", status_code=429)])

    with pytest.raises(httpx.HTTPStatusError):
        provider._get_json("https://example.com/test")

    assert provider.client.calls == 1


def test_yahoo_provider_parses_successful_chart_and_quote(yahoo_chart_payload, yahoo_quote_payload):
    provider = YahooFinanceProvider.__new__(YahooFinanceProvider)
    provider._suppress_request_failure_log = False
    provider._status_code = lambda exc: 401 if isinstance(exc, httpx.HTTPStatusError) else None
    provider._get_json = lambda url, params=None, headers=None: deepcopy(yahoo_chart_payload) if "chart" in url else deepcopy(yahoo_quote_payload)

    payload = provider.fetch_company_bundle("TEST").payload

    assert payload["company"] == "Test Corp"
    assert payload["market_cap_bln_quote"] == pytest.approx(150.0)
    assert payload["shares_outstanding_quote_mln"] == pytest.approx(1000.0)
    assert payload["price_history"]


def test_yahoo_provider_handles_partial_quote_response_with_safe_fallback(yahoo_chart_payload):
    provider = YahooFinanceProvider.__new__(YahooFinanceProvider)
    provider._suppress_request_failure_log = False
    provider._status_code = lambda exc: 401

    def fake_get_json(url: str, params=None, headers=None):
        if "chart" in url:
            return deepcopy(yahoo_chart_payload)
        raise _http_status_error(401, url)

    provider._get_json = fake_get_json

    payload = provider.fetch_company_bundle("TEST").payload

    assert payload["company"] == "Test Corp"
    assert payload["market_cap_bln_quote"] is None
    assert payload["currency"] == "USD"


def test_yahoo_provider_raises_clear_error_for_empty_chart():
    provider = YahooFinanceProvider.__new__(YahooFinanceProvider)
    provider._suppress_request_failure_log = False
    provider._status_code = lambda exc: None
    provider._get_json = lambda url, params=None, headers=None: {"chart": {"result": []}} if "chart" in url else {}

    with pytest.raises(ValueError, match="no chart result"):
        provider.fetch_company_bundle("TEST")


def test_sec_provider_handles_empty_facts_payload_with_warnings(sec_submissions_payload):
    provider = SecEdgarProvider.__new__(SecEdgarProvider)
    provider._ticker_to_cik = lambda ticker: "0000000001"
    provider._get_json = lambda url, params=None, headers=None: {"entityName": "Test Corp", "facts": {}} if "companyfacts" in url else deepcopy(sec_submissions_payload)

    result = provider.fetch_company_bundle("TEST")

    assert result.payload["revenue_bln"] == []
    assert result.payload["net_income_bln"] is None
    assert result.warnings


def test_sec_provider_handles_partially_filled_payload_with_estimated_tax(sec_submissions_payload):
    provider = SecEdgarProvider.__new__(SecEdgarProvider)
    provider._ticker_to_cik = lambda ticker: "0000000002"
    facts = {
        "entityName": "Partial Corp",
        "facts": {
            "us-gaap": {
                "Revenues": {"units": {"USD": [{"fy": 2024, "end": "2024-12-31", "form": "10-K", "fp": "FY", "val": 90_000_000_000}]}},
                "StockholdersEquity": {"units": {"USD": [{"end": "2024-12-31", "form": "10-K", "val": 45_000_000_000}]}},
                "LongTermDebt": {"units": {"USD": [{"end": "2024-12-31", "form": "10-K", "val": 15_000_000_000}]}},
                "CashAndCashEquivalentsAtCarryingValue": {"units": {"USD": [{"end": "2024-12-31", "form": "10-K", "val": 5_000_000_000}]}},
                "OperatingIncomeLoss": {"units": {"USD": [{"fy": 2024, "end": "2024-12-31", "form": "10-K", "fp": "FY", "val": 15_000_000_000}]}},
                "NetIncomeLoss": {"units": {"USD": [{"fy": 2024, "end": "2024-12-31", "form": "10-K", "fp": "FY", "val": 11_000_000_000}]}},
                "Assets": {"units": {"USD": [{"end": "2024-12-31", "form": "10-K", "val": 110_000_000_000}]}},
            },
            "dei": {"EntityCommonStockSharesOutstanding": {"units": {"shares": [{"end": "2024-12-31", "form": "10-K", "val": 900_000_000}]}}},
        },
    }
    provider._get_json = lambda url, params=None, headers=None: facts if "companyfacts" in url else deepcopy(sec_submissions_payload)

    result = provider.fetch_company_bundle("TEST")

    assert result.payload["roic_pct"] is not None
    assert any("estimated tax rate" in warning for warning in result.warnings)


def test_fred_provider_handles_missing_api_key(monkeypatch):
    monkeypatch.delenv("FRED_API_KEY", raising=False)
    get_settings.cache_clear()
    try:
        provider = FredProvider()
        result = provider.fetch_macro_bundle()
    finally:
        get_settings.cache_clear()

    assert result.payload == {}
    assert result.warnings


def test_fred_provider_handles_empty_observations():
    provider = FredProvider.__new__(FredProvider)
    provider.api_key = "test"
    provider._get_json = lambda url, params=None, headers=None: {"observations": []}

    result = provider.fetch_macro_bundle()

    assert result.payload["fed_funds_rate_pct"] is None
    assert result.payload["unemployment_pct"] is None
    assert result.payload["inflation_pct"] is None


def test_world_bank_provider_parses_successful_payload(world_bank_payload):
    provider = WorldBankProvider.__new__(WorldBankProvider)
    provider._get_json = lambda url, params=None, headers=None: deepcopy(world_bank_payload)

    result = provider.fetch_macro_bundle()

    assert result.payload["gdp_growth_pct"] == pytest.approx(2.1)


def test_world_bank_provider_handles_empty_payload():
    provider = WorldBankProvider.__new__(WorldBankProvider)
    provider._get_json = lambda url, params=None, headers=None: []

    result = provider.fetch_macro_bundle()

    assert result.payload["gdp_growth_pct"] is None


def test_fmp_peer_provider_handles_success_empty_and_429(fmp_peer_payload):
    provider = FmpPeerProvider.__new__(FmpPeerProvider)
    provider.api_key = "test"
    provider._is_rate_limited = lambda exc: isinstance(exc, httpx.HTTPStatusError) and exc.response is not None and exc.response.status_code == 429

    provider._get_json = lambda *args, **kwargs: deepcopy(fmp_peer_payload)
    result = provider.discover("ACME", {"ticker": "ACME"})
    assert [item.ticker for item in result.candidates] == ["ORCL", "CRM", "NOW"]

    provider._get_json = lambda *args, **kwargs: {}
    empty_result = provider.discover("ACME", {"ticker": "ACME"})
    assert empty_result.candidates == []

    provider._get_json = lambda *args, **kwargs: (_ for _ in ()).throw(_http_status_error(429, "https://financialmodelingprep.com/stable/stock-peers"))
    limited_result = provider.discover("ACME", {"ticker": "ACME"})
    assert limited_result.candidates == []
    assert "rate-limited" in limited_result.reason


@pytest.mark.parametrize("error_factory", [lambda: httpx.ReadTimeout("timeout"), lambda: _http_status_error(502, "https://finnhub.io/api/v1/stock/peers")])
def test_finnhub_peer_provider_propagates_timeout_and_5xx(error_factory):
    provider = FinnhubPeerProvider.__new__(FinnhubPeerProvider)
    provider.api_key = "test"
    provider._is_rate_limited = lambda exc: False
    provider._get_json = lambda *args, **kwargs: (_ for _ in ()).throw(error_factory())

    with pytest.raises(Exception):
        provider.discover("ACME", {"ticker": "ACME"})


def test_business_type_and_config_peer_providers_return_safe_fallbacks():
    business_type_provider = BusinessTypePeerProvider()
    business_result = business_type_provider.discover("AAPL", {"business_type": "CONSUMER_HARDWARE_ECOSYSTEM"})
    assert business_result.candidates

    config_provider = ConfigPeerProvider(
        {
            "rules": [{"sector": "Technology", "industry_contains": ["software"], "tickers": ["ORCL", "CRM"]}],
            "fallback": {"tickers": ["GE", "CAT"]},
        }
    )
    config_result = config_provider.discover("ACME", {"sector": "Technology", "industry": "Software - Infrastructure", "business_type": "ENTERPRISE_SOFTWARE"})
    assert [item.ticker for item in config_result.candidates] == ["ORCL", "CRM"]


def test_analysis_survives_peer_provider_discovery_failure(analysis_service_factory, strong_company_dataset):
    class ExplodingPeerProvider:
        source_name = "broken"

        def discover(self, ticker: str, company_profile: dict):
            raise httpx.ReadTimeout("peer provider timeout")

    static_peer_provider = StaticPeerProvider(
        source_name="business_type",
        discovery_map=strong_company_dataset["peer_discovery"]["business_type"],
    )

    service = analysis_service_factory(
        yahoo_payloads=strong_company_dataset["yahoo_payloads"],
        edgar_payloads=strong_company_dataset["edgar_payloads"],
        fred_payload=strong_company_dataset["fred_payload"],
        world_bank_payload=strong_company_dataset["world_bank_payload"],
    )
    service.peer_providers = [ExplodingPeerProvider(), static_peer_provider]

    response = service.analyze("ACME")

    assert response.score is not None
    assert response.peers


def test_analysis_survives_supplemental_market_cap_source_failure(analysis_service_factory, fallback_baseline_dataset):
    class MarketCapFailurePeerProvider(StaticPeerProvider):
        def fetch_market_cap_snapshot(self, ticker: str):
            raise httpx.ReadTimeout("market cap timeout")

    service = analysis_service_factory(
        yahoo_payloads=fallback_baseline_dataset["yahoo_payloads"],
        edgar_payloads=fallback_baseline_dataset["edgar_payloads"],
        fred_payload=fallback_baseline_dataset["fred_payload"],
        world_bank_payload=fallback_baseline_dataset["world_bank_payload"],
        peer_discovery=fallback_baseline_dataset["peer_discovery"],
        market_cap_snapshots=fallback_baseline_dataset["market_cap_snapshots"],
    )
    service.peer_providers = [
        MarketCapFailurePeerProvider(source_name="fmp", discovery_map=fallback_baseline_dataset["peer_discovery"]["fmp"]),
        StaticPeerProvider(source_name="finnhub", discovery_map=fallback_baseline_dataset["peer_discovery"]["finnhub"]),
        StaticPeerProvider(source_name="business_type", discovery_map=fallback_baseline_dataset["peer_discovery"]["business_type"]),
        StaticPeerProvider(source_name="config", discovery_map=fallback_baseline_dataset["peer_discovery"]["config"]),
    ]

    response = service.analyze("AUTOX")

    assert response.score is not None
    assert response.peers


def test_analysis_cache_reuses_previous_result(regular_analysis_service):
    first = regular_analysis_service.analyze("ACME")
    yahoo_calls_after_first = len(regular_analysis_service.yahoo.calls)
    edgar_calls_after_first = len(regular_analysis_service.edgar.calls)

    second = regular_analysis_service.analyze("ACME")

    assert first.model_dump() == second.model_dump()
    assert len(regular_analysis_service.yahoo.calls) == yahoo_calls_after_first
    assert len(regular_analysis_service.edgar.calls) == edgar_calls_after_first
