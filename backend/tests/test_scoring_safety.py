from __future__ import annotations

import os
import sys
import types
import unittest

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

sqlalchemy_module = types.ModuleType("sqlalchemy")
sqlalchemy_orm_module = types.ModuleType("sqlalchemy.orm")
sqlalchemy_orm_module.Session = object
sqlalchemy_module.orm = sqlalchemy_orm_module
sys.modules.setdefault("sqlalchemy", sqlalchemy_module)
sys.modules.setdefault("sqlalchemy.orm", sqlalchemy_orm_module)

database_module = types.ModuleType("app.core.database")
database_module.SessionLocal = lambda: None
database_module.Base = types.SimpleNamespace(metadata=types.SimpleNamespace(create_all=lambda bind=None: None))
database_module.engine = object()
sys.modules.setdefault("app.core.database", database_module)

repository_module = types.ModuleType("app.repositories.analysis_repository")


class _DummyRepository:
    def __init__(self, session: object) -> None:
        self.session = session


repository_module.AnalysisRepository = _DummyRepository
sys.modules.setdefault("app.repositories.analysis_repository", repository_module)

from app.core.scoring import get_scoring_config
from app.core.request_context import get_correlation_id
from app.api import routes
from app.middleware.request_context import correlation_id_middleware
from app.schemas.analysis import AnalysisResponse
from app.services.analysis_runtime_service import AnalysisService
from app.services.analysis_safety import business_type_compatibility, classify_company, is_bank_like_company, normalize_weights, safe_ratio
from app.services.providers.live_clients import BaseHttpProvider, FredProvider, SecEdgarProvider, _map_sector, _safe_number, _series_latest, summarize_peer_averages
from app.services.providers.peer_providers import ConfigPeerProvider, FmpPeerProvider, PeerCandidate, PeerDiscoveryResult


class ScoringSafetyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = AnalysisService.__new__(AnalysisService)
        self.service.scoring_config = get_scoring_config()
        self.service.peer_target_count = 6
        self.service.peer_min_valid_count = 3

    def test_safe_ratio_rejects_non_interpretable_denominator(self) -> None:
        self.assertIsNone(safe_ratio(10.0, 0.0))
        self.assertIsNone(safe_ratio(10.0, -2.0))
        self.assertEqual(safe_ratio(10.0, 2.0), 5.0)

    def test_business_classification_examples(self) -> None:
        self.assertEqual(classify_company(ticker="JPM", sector="Financial Services", industry="Commercial Banks")[0], "BANK")
        self.assertEqual(classify_company(ticker="PGR", sector="Financial Services", industry="Insurance - Property & Casualty")[0], "INSURANCE")
        self.assertEqual(classify_company(ticker="BLK", sector="Financial Services", industry="Asset Management")[0], "ASSET_MANAGER")
        self.assertEqual(classify_company(ticker="V", sector="Financial Services", industry="Credit Services")[0], "PAYMENTS")
        self.assertEqual(classify_company(ticker="AXP", sector="Financial Services", industry="Credit Services")[0], "PAYMENTS")
        self.assertEqual(classify_company(ticker="UBER", sector="Technology", industry="Software - Application")[0], "INTERNET_PLATFORM")
        self.assertEqual(classify_company(ticker="ABNB", sector="Consumer Cyclical", industry="Travel Services")[0], "INTERNET_PLATFORM")
        self.assertEqual(classify_company(ticker="NVDA", sector="Technology", industry="Semiconductors")[0], "SEMICONDUCTORS")
        self.assertEqual(classify_company(ticker="AAPL", sector="Technology", industry="Consumer Electronics")[0], "CONSUMER_HARDWARE_ECOSYSTEM")
        self.assertEqual(classify_company(ticker="MSFT", sector="Technology", industry="Software - Infrastructure")[0], "ENTERPRISE_SOFTWARE")
        self.assertEqual(classify_company(ticker="SBUX", sector="Consumer Cyclical", industry="Restaurants")[0], "RESTAURANTS")
        self.assertEqual(classify_company(ticker="HD", sector="Consumer Cyclical", industry="Home Improvement Retail")[0], "HOME_IMPROVEMENT_RETAIL")
        self.assertEqual(classify_company(ticker="TSLA", sector="Unknown", industry="Unknown")[0], "AUTO_MANUFACTURER")
        self.assertEqual(classify_company(ticker="CVX", sector="Energy", industry="Oil & Gas Integrated")[0], "OIL_GAS")
        self.assertEqual(classify_company(ticker="O", sector="Real Estate", industry="Real Estate Investment Trust")[0], "REIT")

    def test_business_classification_does_not_match_erp_inside_caterpillar(self) -> None:
        business_type, confidence, reason = classify_company(
            ticker="CAT",
            sector="Industrials",
            industry="Construction Machinery & Equip",
            company="CATERPILLAR INC",
        )

        self.assertEqual(business_type, "INDUSTRIALS")
        self.assertEqual(confidence, "high")
        self.assertNotIn("erp", reason.lower())

    def test_sector_mapping_handles_defensive_retail_and_utilities(self) -> None:
        self.assertEqual(_map_sector("Retail-Variety Stores"), "Consumer Defensive")
        self.assertEqual(_map_sector("Electric Services"), "Utilities")

    def test_business_classification_handles_utilities(self) -> None:
        business_type, confidence, reason = classify_company(
            ticker="NEE",
            sector="Utilities",
            industry="Electric Services",
            company="NEXTERA ENERGY INC",
        )

        self.assertEqual(business_type, "UTILITIES")
        self.assertEqual(confidence, "high")
        self.assertIn("electric services", reason)

    def test_business_type_compatibility_matrix_examples(self) -> None:
        self.assertEqual(business_type_compatibility("BANK", "INSURANCE"), "REJECT")
        self.assertEqual(business_type_compatibility("INTERNET_PLATFORM", "OIL_GAS"), "REJECT")
        self.assertEqual(business_type_compatibility("RESTAURANTS", "RETAIL"), "WEAK")
        self.assertEqual(business_type_compatibility("REIT", "REIT"), "STRICT")
        self.assertEqual(business_type_compatibility("CONSUMER_HARDWARE_ECOSYSTEM", "SEMICONDUCTORS"), "RELATED")

    def test_business_type_compatibility_unknown_is_weak(self) -> None:
        self.assertEqual(business_type_compatibility("UNKNOWN", "BANK"), "WEAK")
        self.assertEqual(business_type_compatibility("OTHER", "INDUSTRIALS"), "WEAK")

    def test_live_client_missing_values_stay_none(self) -> None:
        self.assertIsNone(_safe_number(None))
        self.assertIsNone(_safe_number("not-a-number"))
        self.assertIsNone(_series_latest([]))

    def test_fred_inflation_uses_year_over_year_change(self) -> None:
        provider = FredProvider.__new__(FredProvider)
        provider.api_key = "test"

        def fake_get_json(url: str, params: dict | None = None, headers: dict | None = None):
            series_id = (params or {}).get("series_id")
            if series_id == "CPIAUCSL":
                return {
                    "observations": [
                        {"date": "2025-02-01", "value": "315.0"},
                        {"date": "2025-01-01", "value": "314.0"},
                        {"date": "2024-12-01", "value": "313.5"},
                        {"date": "2024-11-01", "value": "312.8"},
                        {"date": "2024-10-01", "value": "311.7"},
                        {"date": "2024-09-01", "value": "310.9"},
                        {"date": "2024-08-01", "value": "309.9"},
                        {"date": "2024-07-01", "value": "309.1"},
                        {"date": "2024-06-01", "value": "308.2"},
                        {"date": "2024-05-01", "value": "307.1"},
                        {"date": "2024-04-01", "value": "306.4"},
                        {"date": "2024-03-01", "value": "305.6"},
                        {"date": "2024-02-01", "value": "305.0"},
                    ]
                }
            if series_id == "FEDFUNDS":
                return {"observations": [{"date": "2025-02-01", "value": "4.33"}]}
            if series_id == "UNRATE":
                return {"observations": [{"date": "2025-02-01", "value": "4.0"}]}
            return {"observations": []}

        provider._get_json = fake_get_json

        payload = provider.fetch_macro_bundle().payload

        self.assertEqual(payload["fed_funds_rate_pct"], 4.33)
        self.assertEqual(payload["unemployment_pct"], 4.0)
        self.assertAlmostEqual(payload["inflation_pct"], 3.28, places=2)

    def test_http_provider_does_not_disable_ssl_verification(self) -> None:
        captured: dict[str, object] = {}
        original_client = httpx.Client

        class _FakeClient:
            def __init__(self, **kwargs) -> None:
                captured["client_kwargs"] = kwargs

            def get(self, url: str, **kwargs):
                captured["url"] = url
                captured["request_kwargs"] = kwargs

                class _Response:
                    def raise_for_status(self) -> None:
                        return None

                    def json(self) -> dict:
                        return {"ok": True}

                return _Response()

            def close(self) -> None:
                return None

        try:
            httpx.Client = _FakeClient
            provider = BaseHttpProvider()
            payload = provider._get_json("https://www.sec.gov/files/company_tickers.json")
        finally:
            httpx.Client = original_client

        self.assertEqual(payload, {"ok": True})
        self.assertEqual(captured["client_kwargs"]["verify"], True)
        self.assertNotIn("verify", captured["request_kwargs"])

    def test_http_provider_stops_retrying_after_rate_limit(self) -> None:
        original_client = httpx.Client
        calls = {"count": 0}

        class _FakeClient:
            def __init__(self, **kwargs) -> None:
                return None

            def get(self, url: str, **kwargs):
                calls["count"] += 1
                request = httpx.Request("GET", url)

                class _Response:
                    def raise_for_status(self) -> None:
                        raise httpx.HTTPStatusError("rate limited", request=request, response=httpx.Response(429, request=request))

                return _Response()

            def close(self) -> None:
                return None

        try:
            httpx.Client = _FakeClient
            provider = BaseHttpProvider()
            with self.assertRaises(httpx.HTTPStatusError):
                provider._get_json("https://financialmodelingprep.com/stable/stock-peers")
        finally:
            httpx.Client = original_client

        self.assertEqual(calls["count"], 1)

    def test_fmp_peer_provider_returns_empty_result_when_rate_limited(self) -> None:
        provider = FmpPeerProvider.__new__(FmpPeerProvider)
        provider.api_key = "test"
        provider._is_rate_limited = lambda exc: True
        provider._get_json = lambda *args, **kwargs: (_ for _ in ()).throw(
            httpx.HTTPStatusError(
                "rate limited",
                request=httpx.Request("GET", "https://financialmodelingprep.com/stable/stock-peers"),
                response=httpx.Response(429, request=httpx.Request("GET", "https://financialmodelingprep.com/stable/stock-peers")),
            )
        )

        result = provider.discover("AAPL", {"ticker": "AAPL"})

        self.assertEqual(result.candidates, [])
        self.assertEqual(result.source, "fmp")
        self.assertIn("rate-limited", result.reason)

    def test_correlation_id_middleware_sets_header_and_context(self) -> None:
        app = FastAPI()
        app.middleware("http")(correlation_id_middleware)

        @app.get("/cid")
        def correlation_id_view() -> dict[str, str | None]:
            return {"correlation_id": get_correlation_id()}

        client = TestClient(app)
        response = client.get("/cid", headers={"X-Correlation-ID": "req-123"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["X-Correlation-ID"], "req-123")
        self.assertEqual(response.json()["correlation_id"], "req-123")

    def test_sec_provider_aligns_fcf_by_period_and_computes_roic(self) -> None:
        provider = SecEdgarProvider()
        provider._ticker_to_cik = lambda ticker: "0000000001"

        facts = {
            "entityName": "Test Corp",
            "facts": {
                "us-gaap": {
                    "Revenues": {"units": {"USD": [
                        {"fy": 2024, "end": "2024-12-31", "form": "10-K", "fp": "FY", "val": 130_000_000_000},
                        {"fy": 2023, "end": "2023-12-31", "form": "10-K", "fp": "FY", "val": 120_000_000_000},
                    ]}},
                    "NetCashProvidedByUsedInOperatingActivities": {"units": {"USD": [
                        {"fy": 2023, "end": "2023-12-31", "form": "10-K", "fp": "FY", "val": 30_000_000_000},
                    ]}},
                    "PaymentsToAcquirePropertyPlantAndEquipment": {"units": {"USD": [
                        {"fy": 2023, "end": "2023-12-31", "form": "10-K", "fp": "FY", "val": 5_000_000_000},
                    ]}},
                    "StockholdersEquity": {"units": {"USD": [{"end": "2024-12-31", "form": "10-K", "val": 50_000_000_000}]}},
                    "Liabilities": {"units": {"USD": [{"end": "2024-12-31", "form": "10-K", "val": 70_000_000_000}]}},
                    "LongTermDebt": {"units": {"USD": [{"end": "2024-12-31", "form": "10-K", "val": 20_000_000_000}]}},
                    "LongTermDebtCurrent": {"units": {"USD": [{"end": "2024-12-31", "form": "10-K", "val": 5_000_000_000}]}},
                    "CashAndCashEquivalentsAtCarryingValue": {"units": {"USD": [{"end": "2024-12-31", "form": "10-K", "val": 10_000_000_000}]}},
                    "OperatingIncomeLoss": {"units": {"USD": [{"fy": 2024, "end": "2024-12-31", "form": "10-K", "fp": "FY", "val": 25_000_000_000}]}},
                    "IncomeBeforeTaxExpenseBenefit": {"units": {"USD": [{"fy": 2024, "end": "2024-12-31", "form": "10-K", "fp": "FY", "val": 20_000_000_000}]}},
                    "IncomeTaxExpenseBenefit": {"units": {"USD": [{"fy": 2024, "end": "2024-12-31", "form": "10-K", "fp": "FY", "val": 4_000_000_000}]}},
                    "NetIncomeLoss": {"units": {"USD": [{"fy": 2024, "end": "2024-12-31", "form": "10-K", "fp": "FY", "val": 16_000_000_000}]}},
                    "Assets": {"units": {"USD": [{"end": "2024-12-31", "form": "10-K", "val": 120_000_000_000}]}},
                },
                "dei": {
                    "EntityCommonStockSharesOutstanding": {"units": {"shares": [{"end": "2024-12-31", "form": "10-K", "val": 1_000_000_000}]}}
                },
            },
        }
        submissions = {"name": "Test Corp", "sic": "7372", "sicDescription": "Software"}

        provider._get_json = lambda url, params=None, headers=None: facts if "companyfacts" in url else submissions
        result = provider.fetch_company_bundle("TEST")

        self.assertIsNone(result.payload["history"][0]["free_cash_flow_bln"])
        self.assertEqual(result.payload["history"][1]["free_cash_flow_bln"], 25.0)
        self.assertIsNone(result.payload["fcf_margin_pct"])
        self.assertAlmostEqual(result.payload["roic_pct"], 30.77, places=2)
        self.assertTrue(any("FCF" in warning for warning in result.warnings))

    def test_ticker_normalization_rejects_non_alphanumeric_input(self) -> None:
        with self.assertRaises(ValueError):
            self.service._normalize_ticker("AAPL;DROP TABLE")

        self.assertEqual(self.service._normalize_ticker(" msft "), "MSFT")

    def test_analyze_route_returns_504_for_timeout(self) -> None:
        app = FastAPI()
        app.include_router(routes.router)
        client = TestClient(app)
        original_analyze = routes.service.analyze

        try:
            routes.service.analyze = lambda ticker: (_ for _ in ()).throw(httpx.ReadTimeout("upstream timeout"))
            response = client.get("/analyze/MSFT")
        finally:
            routes.service.analyze = original_analyze

        self.assertEqual(response.status_code, 504)
        self.assertTrue(response.json()["detail"])

    def test_analyze_route_rejects_invalid_ticker_path(self) -> None:
        app = FastAPI()
        app.include_router(routes.router)
        client = TestClient(app)

        response = client.get("/analyze/MSFT;DROP")

        self.assertEqual(response.status_code, 422)

    def test_analyze_route_maps_keyerror_to_404(self) -> None:
        app = FastAPI()
        app.include_router(routes.router)
        client = TestClient(app)
        original_analyze = routes.service.analyze

        try:
            routes.service.analyze = lambda ticker: (_ for _ in ()).throw(KeyError("missing ticker"))
            response = client.get("/analyze/MISSING")
        finally:
            routes.service.analyze = original_analyze

        self.assertEqual(response.status_code, 404)

    def test_analyze_route_maps_http_error_to_502(self) -> None:
        app = FastAPI()
        app.include_router(routes.router)
        client = TestClient(app)
        original_analyze = routes.service.analyze

        try:
            routes.service.analyze = lambda ticker: (_ for _ in ()).throw(httpx.HTTPStatusError("bad gateway", request=None, response=None))
            response = client.get("/analyze/MSFT")
        finally:
            routes.service.analyze = original_analyze

        self.assertEqual(response.status_code, 502)

    def test_normalize_weights_sum_stays_one_for_incomplete_blocks(self) -> None:
        weights = normalize_weights(
            {
                "profitability": 80.0,
                "stability": 60.0,
                "valuation": 55.0,
                "growth": 70.0,
                "market": 50.0,
                "macro": None,
            },
            {
                "profitability": 0.27,
                "stability": 0.24,
                "valuation": 0.16,
                "growth": 0.18,
                "market": 0.10,
                "macro": 0.05,
            },
        )

        self.assertAlmostEqual(sum(weights.values()), 1.0, places=8)
        self.assertEqual(weights["macro"], 0.0)

    def test_revenue_cagr_like_uses_multi_year_compound_growth(self) -> None:
        metrics = self.service._build_silver_metrics(
            {"current_price": 100.0, "one_year_return_pct": 0.0, "five_year_return_pct": 0.0},
            {
                "revenue_bln": [121.0, 110.0, 100.0],
                "net_income_bln": 10.0,
                "equity_bln": 50.0,
                "shares_outstanding_mln": 1000.0,
            },
            {
                "pe_ratio": 15.0,
                "pb_ratio": 3.0,
                "roe_pct": 12.0,
                "revenue_growth_pct": 8.0,
                "debt_to_equity": 0.8,
            },
        )

        self.assertAlmostEqual(metrics["revenue_cagr_like_pct"], 10.0, places=2)

    def test_analysis_uses_cached_response_for_second_call(self) -> None:
        class FakeCache:
            def __init__(self) -> None:
                self.items: dict[str, AnalysisResponse] = {}

            def get(self, key: str) -> AnalysisResponse | None:
                return self.items.get(key)

            def set(self, key: str, value: AnalysisResponse) -> None:
                self.items[key] = value

        class Provider:
            def __init__(self, payload: dict, source_name: str) -> None:
                self.payload = payload
                self.source_name = source_name
                self.warnings: list[str] = []
                self.calls = 0

            def fetch_company_bundle(self, ticker: str):
                self.calls += 1
                return types.SimpleNamespace(payload=self.payload, warnings=[])

            def fetch_macro_bundle(self):
                self.calls += 1
                return types.SimpleNamespace(payload=self.payload, warnings=[])

        cached_service = AnalysisService.__new__(AnalysisService)
        cached_service.scoring_config = get_scoring_config()
        cached_service.analysis_cache = FakeCache()
        cached_service.peer_group_cache = FakeCache()
        cached_service.peer_target_count = 6
        cached_service.peer_min_valid_count = 3
        cached_service.yahoo = Provider({"company": "Test Corp", "current_price": 100.0, "one_year_return_pct": 5.0, "five_year_return_pct": 15.0, "price_history": []}, "Yahoo")
        cached_service.edgar = Provider({"company": "Test Corp", "sector": "Technology", "industry": "Software", "sic": "7372", "history": [], "revenue_bln": [110.0, 100.0], "net_income_bln": 10.0, "equity_bln": 50.0, "shares_outstanding_mln": 1000.0}, "EDGAR")
        cached_service.fred = Provider({"fed_funds_rate_pct": 4.0, "inflation_pct": 3.0, "unemployment_pct": 4.0}, "FRED")
        cached_service.world_bank = Provider({"gdp_growth_pct": 2.0}, "World Bank")
        cached_service._is_bank_like = lambda company_profile: False
        cached_service._build_peer_group = lambda company_profile, yahoo, edgar: ([], {"peer_selection_confidence": "low", "peer_selection_reason": "test", "peer_count": 0})
        cached_service._build_peer_averages = lambda peers, peer_selection, yahoo, edgar: {"pe_ratio": None, "pb_ratio": None, "roe_pct": None, "revenue_growth_pct": None, "debt_to_equity": None}
        cached_service._build_silver_metrics = lambda yahoo, edgar, peers, is_bank_like: {"pe_ratio": None, "pb_ratio": None, "roe_pct": None, "roic_pct": None, "ebit_margin_pct": None, "revenue_growth_pct": None, "revenue_cagr_like_pct": None, "fcf_margin_pct": None, "debt_to_equity": None, "current_ratio": None, "one_year_return_pct": 5.0, "five_year_return_pct": 15.0, "pe_premium_pct": None, "pb_premium_pct": None, "is_bank_like": False}
        cached_service._build_data_quality_warnings = lambda edgar, macro, peers, is_bank_like, metrics=None: []
        cached_service._build_completeness_warnings = lambda silver_metrics, macro: []
        cached_service._build_weighted_scores = lambda silver_metrics, macro: {
            "valuation": (0.0, 0.0, "missing"),
            "macro": (50.0, 1.0, "ok"),
        }
        cached_service._verdict = lambda total_score: "Neutral"
        cached_service._dedupe_warnings = lambda warnings: warnings
        cached_service._build_narrative = lambda company, total_score, weighted_scores, warnings: "Test narrative"
        cached_service._metric_cards = lambda silver_metrics, peer_averages: []
        cached_service._peer_rows = lambda peers: []
        cached_service._persist_layers = lambda *args, **kwargs: None

        first = cached_service.analyze("test")
        second = cached_service.analyze("test")

        self.assertEqual(first.model_dump(), second.model_dump())
        self.assertIsNone(next(item for item in first.score_breakdown if item.key == "valuation").score)
        self.assertEqual(cached_service.yahoo.calls, 1)
        self.assertEqual(cached_service.edgar.calls, 1)
        self.assertEqual(cached_service.fred.calls, 1)
        self.assertEqual(cached_service.world_bank.calls, 1)

    def test_negative_equity_metrics_become_none(self) -> None:
        edgar = {
            "net_income_bln": 5.0,
            "equity_bln": -2.0,
            "debt_to_equity": None,
            "roic_pct": None,
            "ebit_margin_pct": 18.0,
            "fcf_margin_pct": 12.0,
            "current_ratio": 1.4,
            "revenue_bln": [100.0, 90.0, 80.0],
            "shares_outstanding_mln": 1000.0,
        }
        yahoo = {
            "current_price": 10.0,
            "one_year_return_pct": 5.0,
            "five_year_return_pct": 25.0,
        }
        peers = {
            "pe_ratio": 15.0,
            "pb_ratio": 3.0,
            "roe_pct": 12.0,
            "revenue_growth_pct": 8.0,
            "debt_to_equity": 0.8,
        }

        metrics = self.service._build_silver_metrics(yahoo, edgar, peers)

        self.assertIsNone(metrics["roe_pct"])
        self.assertIsNone(metrics["pb_ratio"])
        self.assertIsNone(metrics["pb_premium_pct"])

        cards = self.service._metric_cards(
            metrics
            | {
                "peer_pe_valid_count": 3,
                "peer_roe_valid_count": 3,
                "peer_growth_valid_count": 3,
                "peer_debt_valid_count": 3,
            },
            peers,
        )
        roe_card = next(card for card in cards if "ROE" in card.label)
        self.assertIsNone(roe_card.value)
        self.assertEqual(roe_card.comparison_label, "Недостаточно данных")

        warnings = self.service._build_data_quality_warnings(
            edgar,
            {"gdp_growth_pct": 2.0},
            {
                "pe_ratio_valid_count": 3,
                "pb_ratio_valid_count": 3,
                "roe_pct_valid_count": 3,
                "revenue_growth_pct_valid_count": 3,
                "debt_to_equity_valid_count": 3,
                "pe_ratio_baseline_noisy": False,
                "pb_ratio_baseline_noisy": False,
            },
            False,
        )
        self.assertIn("Negative equity detected: ROE, Debt/Equity and P/B may be unreliable", warnings)

    def test_market_cap_prefers_quote_fallback_when_sec_shares_look_adr_adjusted(self) -> None:
        diagnostics = self.service._market_cap_diagnostics(
            {
                "current_price": 180.0,
                "currency": "USD",
                "market_cap_bln_quote": 1800.0,
                "market_cap_quote_currency": "USD",
                "shares_outstanding_quote_mln": 10000.0,
                "quote_type": "ADR",
            },
            {
                "shares_outstanding_mln": 50025.67,
            },
        )

        self.assertEqual(diagnostics["market_cap_bln"], 1800.0)
        self.assertEqual(diagnostics["source"], "median_of_sources")
        self.assertTrue(diagnostics["suspect"])
        self.assertEqual(diagnostics["status"], "suspect")
        self.assertIn("disagree materially", diagnostics["warning"])

    def test_market_cap_uses_quote_path_for_foreign_listing_when_quote_is_usd(self) -> None:
        diagnostics = self.service._market_cap_diagnostics(
            {
                "current_price": 950.0,
                "currency": "TWD",
                "market_cap_bln_quote": 910.0,
                "market_cap_quote_currency": "USD",
                "quote_type": "ADR",
            },
            {
                "shares_outstanding_mln": 26000.0,
            },
        )

        self.assertEqual(diagnostics["market_cap_bln"], 910.0)
        self.assertEqual(diagnostics["source"], "yahoo_quote")
        self.assertFalse(diagnostics["suspect"])
        self.assertEqual(diagnostics["status"], "valid")
        self.assertIn("quote-based USD fallback", diagnostics["warning"])

    def test_market_cap_without_usd_conversion_path_is_excluded(self) -> None:
        diagnostics = self.service._market_cap_diagnostics(
            {
                "current_price": 950.0,
                "currency": "TWD",
                "market_cap_bln_quote": None,
            },
            {
                "shares_outstanding_mln": 26000.0,
            },
        )

        self.assertIsNone(diagnostics["market_cap_bln"])
        self.assertFalse(diagnostics["suspect"])
        self.assertEqual(diagnostics["status"], "invalid")
        self.assertIn("USD conversion path", diagnostics["warning"])

    def test_market_cap_uses_median_when_external_sources_disagree_with_adr_like_quote(self) -> None:
        diagnostics = self.service._market_cap_diagnostics(
            {
                "ticker": "TM",
                "current_price": 250.0,
                "currency": "USD",
                "market_cap_bln_quote": 2771.07,
                "market_cap_quote_currency": "USD",
                "shares_outstanding_quote_mln": 16000.0,
            },
            {
                "shares_outstanding_mln": 16000.0,
            },
            supplemental_market_caps=[
                ("fmp_market_cap", 352.0, "USD"),
                ("finnhub_market_cap", 347.0, "USD"),
            ],
        )

        self.assertAlmostEqual(diagnostics["market_cap_bln"], 352.0, places=1)
        self.assertEqual(diagnostics["source"], "median_of_sources")
        self.assertTrue(diagnostics["suspect"])
        self.assertEqual(diagnostics["status"], "suspect")
        self.assertIn("disagree materially", diagnostics["warning"])

    def test_missing_fred_does_not_turn_into_zero_macro_bonus(self) -> None:
        weighted_scores = self.service._build_weighted_scores(
            {
                "roe_pct": 20.0,
                "roe_score_pct": 20.0,
                "roic_pct": 20.0,
                "ebit_margin_pct": 20.0,
                "debt_to_equity": 1.0,
                "current_ratio": 1.5,
                "fcf_margin_pct": 10.0,
                "pe_premium_pct": 5.0,
                "pb_premium_pct": 5.0,
                "revenue_growth_pct": 10.0,
                "revenue_cagr_like_pct": 9.0,
                "one_year_return_pct": 5.0,
                "five_year_return_pct": 15.0,
                "is_bank_like": False,
            },
            {
                "fed_funds_rate_pct": None,
                "inflation_pct": None,
                "unemployment_pct": None,
                "gdp_growth_pct": 2.0,
            },
        )

        macro_score, macro_weight, _ = weighted_scores["macro"]
        self.assertIsNone(macro_score)
        self.assertEqual(macro_weight, 0.0)

    def test_aapl_low_equity_roe_falls_back_to_roic_for_scoring(self) -> None:
        metrics = self.service._build_silver_metrics(
            {
                "current_price": 210.0,
                "one_year_return_pct": 12.0,
                "five_year_return_pct": 180.0,
            },
            {
                "revenue_bln": [390.0, 380.0, 365.0],
                "net_income_bln": 100.0,
                "equity_bln": 60.0,
                "roic_pct": 31.0,
                "ebit_margin_pct": 32.0,
                "fcf_margin_pct": 25.0,
                "debt_to_equity": 1.8,
                "current_ratio": 1.0,
                "shares_outstanding_mln": 15500.0,
            },
            {
                "pe_ratio": 30.0,
                "pb_ratio": 8.0,
                "roe_pct": 28.0,
                "revenue_growth_pct": 12.0,
                "debt_to_equity": 0.6,
                "pe_ratio_valid_count": 4,
                "pb_ratio_valid_count": 4,
                "peer_count_usable": 4,
                "peer_confidence": "high",
                "peer_baseline_reliability": "high",
            },
        )

        self.assertFalse(metrics["roe_reliable"])
        self.assertGreater(metrics["roe_pct"], 100.0)
        self.assertEqual(metrics["roe_score_pct"], 31.0)
        self.assertGreaterEqual(metrics["data_completeness_score"], 80.0)
        self.assertLess(metrics["data_reliability_score"], 100.0)

        weighted_scores = self.service._build_weighted_scores(
            metrics
            | {
                "pe_premium_pct": 5.0,
                "pb_premium_pct": 10.0,
            },
            {
                "fed_funds_rate_pct": 4.0,
                "inflation_pct": 3.0,
                "unemployment_pct": 4.0,
                "gdp_growth_pct": 2.0,
            },
        )

        self.assertLess(weighted_scores["profitability"][0], 85.0)

    def test_revenue_growth_requires_matching_period_metadata(self) -> None:
        growth = self.service._revenue_growth_pct(
            {
                "revenue_bln": [147.0, 100.0],
                "revenue_periods": [
                    {"period_type": "annual", "fiscal_period": "FY"},
                    {"period_type": "quarterly", "fiscal_period": "Q4"},
                ],
            }
        )
        self.assertIsNone(growth)

        growth = self.service._revenue_growth_pct(
            {
                "revenue_bln": [147.0, 100.0],
                "revenue_periods": [
                    {"period_type": "annual", "fiscal_period": "FY"},
                    {"period_type": "annual", "fiscal_period": "FY"},
                ],
            }
        )
        self.assertEqual(growth, 47.0)

    def test_peer_average_uses_robust_baseline_for_valuation(self) -> None:
        averages = summarize_peer_averages(
            [
                {"pe_ratio": 10.0, "pb_ratio": 2.0, "roe_pct": 12.0, "revenue_growth_pct": 8.0, "debt_to_equity": 0.5},
                {"pe_ratio": 12.0, "pb_ratio": 2.2, "roe_pct": 13.0, "revenue_growth_pct": 9.0, "debt_to_equity": 0.6},
                {"pe_ratio": 1000.0, "pb_ratio": None, "roe_pct": None, "revenue_growth_pct": 10.0, "debt_to_equity": None},
            ]
        )

        self.assertEqual(averages["pe_ratio"], 12.0)
        self.assertAlmostEqual(averages["pb_ratio"], 2.1, places=2)
        self.assertAlmostEqual(averages["roe_pct"], 12.5, places=2)
        self.assertTrue(averages["pe_ratio_baseline_noisy"])

    def test_smart_peer_selection_prefers_relevant_candidates(self) -> None:
        selected, meta = self.service._select_peers_from_candidates(
            {
                "ticker": "JPM",
                "sector": "Financial Services",
                "industry": "Commercial Banks",
                "sic": "6021",
            },
            [
                {"ticker": "BAC", "sector": "Financial Services", "industry": "Commercial Banks", "sic": "6021", "market_cap_bln": 280.0, "pe_ratio": 12.0, "pb_ratio": 1.2, "roe_pct": 11.0, "revenue_growth_pct": 3.0, "debt_to_equity": 8.0},
                {"ticker": "WFC", "sector": "Financial Services", "industry": "Banks", "sic": "6021", "market_cap_bln": 210.0, "pe_ratio": 11.0, "pb_ratio": 1.1, "roe_pct": 10.0, "revenue_growth_pct": 2.0, "debt_to_equity": 7.0},
                {"ticker": "TSLA", "sector": "Consumer Cyclical", "industry": "Automobiles", "sic": "3711", "market_cap_bln": 700.0, "pe_ratio": 95.0, "pb_ratio": 9.0, "roe_pct": 18.0, "revenue_growth_pct": 20.0, "debt_to_equity": 0.2},
            ],
            250.0,
            ["BAC", "WFC"],
        )

        self.assertEqual([row["ticker"] for row in selected[:2]], ["BAC", "WFC"])
        self.assertEqual(meta["peer_selection_confidence"], "low")
        self.assertLess(meta["peer_count_usable"], 3)

    def test_business_type_peer_selection_rejects_irrelevant_uber_peers(self) -> None:
        selected, meta = self.service._select_peers_from_candidates(
            {
                "ticker": "UBER",
                "sector": "Technology",
                "industry": "Platform Services",
                "sic": "",
                "business_type": "INTERNET_PLATFORM",
            },
            [
                {"ticker": "ABNB", "sector": "Consumer Cyclical", "industry": "Travel Platform", "sic": "", "market_cap_bln": 90.0, "pe_ratio": 28.0, "pb_ratio": 8.0, "roe_pct": 18.0, "revenue_growth_pct": 12.0, "debt_to_equity": 0.3},
                {"ticker": "DASH", "sector": "Technology", "industry": "Marketplace Platform", "sic": "", "market_cap_bln": 60.0, "pe_ratio": 35.0, "pb_ratio": 7.0, "roe_pct": 8.0, "revenue_growth_pct": 15.0, "debt_to_equity": 0.2},
                {"ticker": "XOM", "sector": "Energy", "industry": "Oil & Gas Integrated", "sic": "2911", "market_cap_bln": 450.0, "pe_ratio": 14.0, "pb_ratio": 2.0, "roe_pct": 16.0, "revenue_growth_pct": 4.0, "debt_to_equity": 0.1},
                {"ticker": "GE", "sector": "Industrials", "industry": "Industrial Conglomerates", "sic": "3500", "market_cap_bln": 180.0, "pe_ratio": 30.0, "pb_ratio": 6.0, "roe_pct": 10.0, "revenue_growth_pct": 5.0, "debt_to_equity": 1.1},
            ],
            140.0,
            ["ABNB", "DASH", "XOM", "GE"],
        )

        self.assertEqual({row["ticker"] for row in selected}, {"ABNB", "DASH"})
        self.assertFalse(meta["peer_group_quality_passed"])
        self.assertLess(meta["peer_count_usable"], 3)

    def test_business_type_selection_keeps_insurance_separate_from_banks(self) -> None:
        selected, meta = self.service._select_peers_from_candidates(
            {
                "ticker": "PGR",
                "sector": "Financial Services",
                "industry": "Insurance - Property & Casualty",
                "sic": "",
                "business_type": "INSURANCE",
            },
            [
                {"ticker": "TRV", "sector": "Financial Services", "industry": "Insurance - Property & Casualty", "sic": "", "market_cap_bln": 40.0, "pe_ratio": 15.0, "pb_ratio": 2.0, "roe_pct": 14.0, "revenue_growth_pct": 4.0, "debt_to_equity": 0.2},
                {"ticker": "ALL", "sector": "Financial Services", "industry": "Insurance - Diversified", "sic": "", "market_cap_bln": 35.0, "pe_ratio": 13.0, "pb_ratio": 1.8, "roe_pct": 12.0, "revenue_growth_pct": 3.0, "debt_to_equity": 0.3},
                {"ticker": "JPM", "sector": "Financial Services", "industry": "Commercial Banks", "sic": "6021", "market_cap_bln": 550.0, "pe_ratio": 12.0, "pb_ratio": 1.9, "roe_pct": 16.0, "revenue_growth_pct": 5.0, "debt_to_equity": 8.0},
                {"ticker": "BAC", "sector": "Financial Services", "industry": "Banks - Diversified", "sic": "6021", "market_cap_bln": 300.0, "pe_ratio": 11.0, "pb_ratio": 1.3, "roe_pct": 11.0, "revenue_growth_pct": 4.0, "debt_to_equity": 7.0},
            ],
            60.0,
            ["TRV", "ALL", "JPM", "BAC"],
        )

        self.assertEqual([row["ticker"] for row in selected[:2]], ["TRV", "ALL"])
        self.assertFalse(meta["peer_group_quality_passed"])
        self.assertLess(meta["peer_count_usable"], 3)

    def test_business_type_selection_prefers_hardware_over_enterprise_software_for_aapl(self) -> None:
        selected, meta = self.service._select_peers_from_candidates(
            {
                "ticker": "AAPL",
                "sector": "Technology",
                "industry": "Consumer Electronics",
                "sic": "",
                "business_type": "CONSUMER_HARDWARE_ECOSYSTEM",
            },
            [
                {"ticker": "HPQ", "sector": "Technology", "industry": "Computer Hardware", "sic": "", "market_cap_bln": 30.0, "pe_ratio": 11.0, "pb_ratio": 5.0, "roe_pct": 20.0, "revenue_growth_pct": 3.0, "debt_to_equity": 1.0},
                {"ticker": "DELL", "sector": "Technology", "industry": "Computer Hardware", "sic": "", "market_cap_bln": 80.0, "pe_ratio": 16.0, "pb_ratio": 6.0, "roe_pct": 35.0, "revenue_growth_pct": 4.0, "debt_to_equity": 1.5},
                {"ticker": "CRM", "sector": "Technology", "industry": "Enterprise Software", "sic": "", "market_cap_bln": 260.0, "pe_ratio": 26.0, "pb_ratio": 4.0, "roe_pct": 12.0, "revenue_growth_pct": 9.0, "debt_to_equity": 0.2},
                {"ticker": "NOW", "sector": "Technology", "industry": "Enterprise Software", "sic": "", "market_cap_bln": 150.0, "pe_ratio": 58.0, "pb_ratio": 12.0, "roe_pct": 10.0, "revenue_growth_pct": 18.0, "debt_to_equity": 0.1},
            ],
            3000.0,
            ["HPQ", "DELL", "CRM", "NOW"],
        )

        self.assertEqual({row["ticker"] for row in selected[:2]}, {"DELL", "HPQ"})
        self.assertFalse(meta["peer_group_quality_passed"])
        self.assertLess(meta["peer_count_usable"], 3)

    def test_meta_peer_selection_prefers_internet_platforms_over_crm(self) -> None:
        selected, meta = self.service._select_peers_from_candidates(
            {
                "ticker": "META",
                "sector": "Communication Services",
                "industry": "Internet Content & Information",
                "sic": "7375",
                "business_type": "INTERNET_PLATFORM",
                "revenue_bln_latest": 135.0,
                "ebit_margin_pct": 40.0,
            },
            [
                {"ticker": "GOOGL", "sector": "Communication Services", "industry": "Internet Content & Information", "sic": "7375", "market_cap_bln": 2000.0, "pe_ratio": 26.0, "pb_ratio": 7.0, "roe_pct": 30.0, "revenue_growth_pct": 12.0, "debt_to_equity": 0.1, "revenue_bln": 330.0, "ebit_margin_pct": 32.0},
                {"ticker": "SNAP", "sector": "Communication Services", "industry": "Internet Content & Information", "sic": "7375", "market_cap_bln": 120.0, "pe_ratio": 24.0, "pb_ratio": 5.0, "roe_pct": 18.0, "revenue_growth_pct": 14.0, "debt_to_equity": 0.2, "revenue_bln": 8.0, "ebit_margin_pct": 24.0},
                {"ticker": "PINS", "sector": "Communication Services", "industry": "Internet Content & Information", "sic": "7375", "market_cap_bln": 90.0, "pe_ratio": 22.0, "pb_ratio": 4.0, "roe_pct": 16.0, "revenue_growth_pct": 13.0, "debt_to_equity": 0.1, "revenue_bln": 4.0, "ebit_margin_pct": 22.0},
                {"ticker": "CRM", "sector": "Technology", "industry": "Software - Application", "sic": "7372", "market_cap_bln": 260.0, "pe_ratio": 26.0, "pb_ratio": 4.0, "roe_pct": 12.0, "revenue_growth_pct": 9.0, "debt_to_equity": 0.2, "revenue_bln": 37.0, "ebit_margin_pct": 18.0},
                {"ticker": "ORCL", "sector": "Technology", "industry": "Software - Infrastructure", "sic": "7372", "market_cap_bln": 380.0, "pe_ratio": 28.0, "pb_ratio": 30.0, "roe_pct": 20.0, "revenue_growth_pct": 8.0, "debt_to_equity": 5.0, "revenue_bln": 54.0, "ebit_margin_pct": 28.0},
            ],
            1500.0,
            ["GOOGL", "SNAP", "PINS", "CRM", "ORCL"],
        )

        tickers = [row["ticker"] for row in selected[:3]]
        self.assertEqual(set(tickers), {"GOOGL", "SNAP", "PINS"})
        self.assertEqual(meta["peer_selection_mode"], "strict")

    def test_lly_peer_selection_keeps_only_pharma_when_strict_set_is_sufficient(self) -> None:
        selected, meta = self.service._select_peers_from_candidates(
            {
                "ticker": "LLY",
                "sector": "Healthcare",
                "industry": "Drug Manufacturers - General",
                "sic": "2834",
                "business_type": "PHARMA",
                "revenue_bln_latest": 40.0,
                "ebit_margin_pct": 32.0,
            },
            [
                {"ticker": "PFE", "sector": "Healthcare", "industry": "Drug Manufacturers - General", "sic": "2834", "market_cap_bln": 180.0, "pe_ratio": 15.0, "pb_ratio": 2.1, "roe_pct": 18.0, "revenue_growth_pct": 3.0, "debt_to_equity": 0.5, "revenue_bln": 60.0, "ebit_margin_pct": 28.0},
                {"ticker": "MRK", "sector": "Healthcare", "industry": "Drug Manufacturers - General", "sic": "2834", "market_cap_bln": 320.0, "pe_ratio": 18.0, "pb_ratio": 4.5, "roe_pct": 22.0, "revenue_growth_pct": 6.0, "debt_to_equity": 0.7, "revenue_bln": 62.0, "ebit_margin_pct": 31.0},
                {"ticker": "ABBV", "sector": "Healthcare", "industry": "Drug Manufacturers - General", "sic": "2834", "market_cap_bln": 300.0, "pe_ratio": 17.0, "pb_ratio": 15.0, "roe_pct": 30.0, "revenue_growth_pct": 5.0, "debt_to_equity": 3.5, "revenue_bln": 55.0, "ebit_margin_pct": 30.0},
                {"ticker": "UNH", "sector": "Healthcare", "industry": "Healthcare Plans", "sic": "6324", "market_cap_bln": 420.0, "pe_ratio": 19.0, "pb_ratio": 5.0, "roe_pct": 26.0, "revenue_growth_pct": 8.0, "debt_to_equity": 0.8, "revenue_bln": 370.0, "ebit_margin_pct": 8.0},
                {"ticker": "HCA", "sector": "Healthcare", "industry": "Medical Care Facilities", "sic": "8062", "market_cap_bln": 95.0, "pe_ratio": 13.0, "pb_ratio": 20.0, "roe_pct": 60.0, "revenue_growth_pct": 7.0, "debt_to_equity": 12.0, "revenue_bln": 65.0, "ebit_margin_pct": 14.0},
            ],
            700.0,
            ["PFE", "MRK", "ABBV", "UNH", "HCA"],
        )

        tickers = {row["ticker"] for row in selected}
        self.assertTrue({"PFE", "MRK", "ABBV"}.issubset(tickers))
        self.assertNotIn("UNH", tickers)
        self.assertNotIn("HCA", tickers)
        self.assertEqual(meta["peer_selection_mode"], "strict")

    def test_aapl_peers_are_not_empty_with_mega_cap_fallback(self) -> None:
        selected, meta = self.service._select_peers_from_candidates(
            {
                "ticker": "AAPL",
                "sector": "Technology",
                "industry": "Consumer Electronics",
                "sic": "",
                "business_type": "CONSUMER_HARDWARE_ECOSYSTEM",
            },
            [
                {"ticker": "MSFT", "sector": "Technology", "industry": "Software - Infrastructure", "sic": "", "market_cap_bln": 3100.0, "pe_ratio": 34.0, "pb_ratio": 10.0, "roe_pct": 35.0, "revenue_growth_pct": 15.0, "debt_to_equity": 0.4},
                {"ticker": "NVDA", "sector": "Technology", "industry": "Semiconductors", "sic": "", "market_cap_bln": 2800.0, "pe_ratio": 40.0, "pb_ratio": 20.0, "roe_pct": 65.0, "revenue_growth_pct": 60.0, "debt_to_equity": 0.3},
                {"ticker": "GOOGL", "sector": "Communication Services", "industry": "Internet Content & Information", "sic": "", "market_cap_bln": 2000.0, "pe_ratio": 26.0, "pb_ratio": 7.0, "roe_pct": 30.0, "revenue_growth_pct": 12.0, "debt_to_equity": 0.1},
            ],
            3200.0,
            ["MSFT", "NVDA", "GOOGL"],
        )

        self.assertNotEqual(selected, [])
        self.assertEqual(meta["peer_selection_mode"], "fallback")
        self.assertGreaterEqual(meta["peer_count_total"], 3)

    def test_msft_peers_are_not_empty(self) -> None:
        selected, meta = self.service._select_peers_from_candidates(
            {
                "ticker": "MSFT",
                "sector": "Technology",
                "industry": "Software - Infrastructure",
                "sic": "",
                "business_type": "ENTERPRISE_SOFTWARE",
            },
            [
                {"ticker": "ORCL", "sector": "Technology", "industry": "Software - Infrastructure", "sic": "", "market_cap_bln": 380.0, "pe_ratio": 28.0, "pb_ratio": 30.0, "roe_pct": None, "revenue_growth_pct": 8.0, "debt_to_equity": 5.0},
                {"ticker": "CRM", "sector": "Technology", "industry": "Software - Application", "sic": "", "market_cap_bln": 260.0, "pe_ratio": 26.0, "pb_ratio": 4.0, "roe_pct": 12.0, "revenue_growth_pct": 9.0, "debt_to_equity": 0.2},
                {"ticker": "ADBE", "sector": "Technology", "industry": "Software - Infrastructure", "sic": "", "market_cap_bln": 220.0, "pe_ratio": 30.0, "pb_ratio": 12.0, "roe_pct": 35.0, "revenue_growth_pct": 11.0, "debt_to_equity": 0.3},
            ],
            3100.0,
            ["ORCL", "CRM", "ADBE"],
        )

        self.assertNotEqual(selected, [])
        self.assertIn(meta["peer_selection_mode"], {"strict", "extended"})

    def test_small_relevant_peer_set_uses_soft_fallback_not_na(self) -> None:
        selected, meta = self.service._select_peers_from_candidates(
            {
                "ticker": "HD",
                "sector": "Consumer Cyclical",
                "industry": "Home Improvement Retail",
                "sic": "",
                "business_type": "HOME_IMPROVEMENT_RETAIL",
            },
            [
                {"ticker": "LOW", "sector": "Consumer Cyclical", "industry": "Home Improvement Retail", "sic": "", "market_cap_bln": 130.0, "pe_ratio": 19.0, "pb_ratio": 12.0, "roe_pct": 85.0, "revenue_growth_pct": 4.0, "debt_to_equity": 4.2},
                {"ticker": "WMT", "sector": "Consumer Defensive", "industry": "Discount Stores", "sic": "", "market_cap_bln": 450.0, "pe_ratio": 28.0, "pb_ratio": 6.5, "roe_pct": 18.0, "revenue_growth_pct": 5.0, "debt_to_equity": 0.8},
            ],
            340.0,
            ["LOW", "WMT"],
        )

        self.assertIn("LOW", [row["ticker"] for row in selected])
        self.assertFalse(meta["peer_group_quality_passed"])
        self.assertEqual(meta["peer_selection_confidence"], "low")
        self.assertTrue(meta["peer_group_sample_limited"])
        self.assertEqual(meta["peer_selection_mode"], "fallback")

        averages = summarize_peer_averages(selected)
        self.assertEqual(averages["pe_ratio"], 23.5)
        self.assertEqual(averages["pb_ratio"], 9.25)

    def test_valuation_uses_neutral_baseline_when_strict_baseline_is_empty(self) -> None:
        peer_averages = self.service._build_peer_averages(
            [
                {"ticker": "MSFT", "pe_ratio": None, "pb_ratio": None, "roe_pct": 30.0, "revenue_growth_pct": 12.0, "debt_to_equity": 0.2},
            ],
            {
                "company_ticker": "MSFT",
                "usable_peer_tickers": ["MSFT"],
            },
            {"current_price": 100.0},
            {"shares_outstanding_mln": 1000.0, "net_income_bln": 10.0, "equity_bln": 50.0},
        )

        self.assertEqual(peer_averages["valuation_baseline_mode"], "neutral")
        self.assertIsNone(peer_averages["pe_ratio"])
        self.assertIsNone(peer_averages["pb_ratio"])

    def test_valuation_block_is_disabled_when_usable_peers_are_below_three(self) -> None:
        metrics = self.service._build_silver_metrics(
            {
                "current_price": 100.0,
                "one_year_return_pct": 5.0,
                "five_year_return_pct": 15.0,
            },
            {
                "revenue_bln": [100.0, 95.0, 90.0],
                "net_income_bln": 10.0,
                "equity_bln": 50.0,
                "roic_pct": 18.0,
                "ebit_margin_pct": 22.0,
                "fcf_margin_pct": 12.0,
                "debt_to_equity": 0.8,
                "current_ratio": 1.5,
                "shares_outstanding_mln": 1000.0,
            },
            {
                "pe_ratio": 20.0,
                "pb_ratio": 4.0,
                "roe_pct": 15.0,
                "revenue_growth_pct": 7.0,
                "debt_to_equity": 0.9,
                "pe_ratio_valid_count": 4,
                "pb_ratio_valid_count": 4,
                "peer_count_usable": 2,
                "peer_confidence": "medium",
                "peer_baseline_reliability": "medium",
            },
        )

        self.assertTrue(metrics["valuation_enabled"])
        self.assertTrue(metrics["valuation_low_confidence"])
        self.assertFalse(metrics["valuation_fallback"])
        self.assertFalse(metrics["valuation_partial"])
        self.assertIsNotNone(metrics["pe_premium_pct"])
        self.assertIsNotNone(metrics["pb_premium_pct"])

        weighted_scores = self.service._build_weighted_scores(
            metrics,
            {
                "fed_funds_rate_pct": 4.0,
                "inflation_pct": 3.0,
                "unemployment_pct": 4.0,
                "gdp_growth_pct": 2.0,
            },
        )

        self.assertIsNotNone(weighted_scores["valuation"][0])
        self.assertGreater(weighted_scores["valuation"][1], 0.0)

    def test_valuation_stays_enabled_with_two_usable_and_one_weak_peer(self) -> None:
        metrics = self.service._build_silver_metrics(
            {
                "current_price": 100.0,
                "one_year_return_pct": 5.0,
                "five_year_return_pct": 15.0,
            },
            {
                "revenue_bln": [100.0, 95.0, 90.0],
                "net_income_bln": 10.0,
                "equity_bln": 50.0,
                "roic_pct": 18.0,
                "ebit_margin_pct": 22.0,
                "fcf_margin_pct": 12.0,
                "debt_to_equity": 0.8,
                "current_ratio": 1.5,
                "shares_outstanding_mln": 1000.0,
            },
            {
                "pe_ratio": 20.0,
                "pb_ratio": 4.0,
                "roe_pct": 15.0,
                "revenue_growth_pct": 7.0,
                "debt_to_equity": 0.9,
                "pe_ratio_valid_count": 3,
                "pb_ratio_valid_count": 3,
                "peer_count_usable": 2,
                "peer_count_weak": 1,
                "peer_count_supported": 3,
                "valuation_support_mode": "low_confidence",
                "peer_confidence": "medium",
                "peer_baseline_reliability": "low",
            },
        )

        self.assertTrue(metrics["valuation_enabled"])
        self.assertTrue(metrics["valuation_low_confidence"])
        self.assertFalse(metrics["valuation_fallback"])
        self.assertIsNotNone(metrics["pe_premium_pct"])
        self.assertIsNotNone(metrics["pb_premium_pct"])

    def test_valuation_fallback_uses_one_usable_and_two_weak_peers(self) -> None:
        metrics = self.service._build_silver_metrics(
            {
                "current_price": 100.0,
                "one_year_return_pct": 5.0,
                "five_year_return_pct": 15.0,
            },
            {
                "revenue_bln": [100.0, 95.0, 90.0],
                "net_income_bln": 10.0,
                "equity_bln": 50.0,
                "roic_pct": 18.0,
                "ebit_margin_pct": 22.0,
                "fcf_margin_pct": 12.0,
                "debt_to_equity": 0.8,
                "current_ratio": 1.5,
                "shares_outstanding_mln": 1000.0,
            },
            {
                "pe_ratio": 18.0,
                "pb_ratio": 3.8,
                "roe_pct": 14.0,
                "revenue_growth_pct": 6.0,
                "debt_to_equity": 0.9,
                "pe_ratio_valid_count": 2,
                "pb_ratio_valid_count": 2,
                "peer_count_usable": 1,
                "peer_count_weak": 2,
                "peer_count_supported": 3,
                "valuation_support_mode": "fallback_low_confidence",
                "peer_confidence": "low",
                "peer_baseline_reliability": "low",
            },
        )

        self.assertTrue(metrics["valuation_enabled"])
        self.assertTrue(metrics["valuation_low_confidence"])
        self.assertTrue(metrics["valuation_fallback"])

    def test_weak_only_fallback_uses_reduced_valuation_weight(self) -> None:
        metrics = self.service._build_silver_metrics(
            {
                "current_price": 100.0,
                "one_year_return_pct": 5.0,
                "five_year_return_pct": 15.0,
            },
            {
                "revenue_bln": [100.0, 95.0, 90.0],
                "net_income_bln": 10.0,
                "equity_bln": 50.0,
                "roic_pct": 18.0,
                "ebit_margin_pct": 22.0,
                "fcf_margin_pct": 12.0,
                "debt_to_equity": 0.8,
                "current_ratio": 1.5,
                "shares_outstanding_mln": 1000.0,
            },
            {
                "pe_ratio": 18.0,
                "pb_ratio": 3.8,
                "roe_pct": 14.0,
                "revenue_growth_pct": 6.0,
                "debt_to_equity": 0.9,
                "pe_ratio_valid_count": 2,
                "pb_ratio_valid_count": 2,
                "peer_count_usable": 0,
                "peer_count_weak": 3,
                "peer_count_supported": 3,
                "valuation_support_mode": "weak_only_fallback",
                "peer_confidence": "low",
                "peer_baseline_reliability": "low",
            },
        )

        self.assertTrue(metrics["valuation_enabled"])
        self.assertEqual(metrics["valuation_mode_multiplier"], 0.45)
        self.assertTrue(metrics["valuation_fallback"])

    def test_partial_valuation_stays_enabled_with_single_available_multiple(self) -> None:
        metrics = self.service._build_silver_metrics(
            {
                "current_price": 100.0,
                "one_year_return_pct": 5.0,
                "five_year_return_pct": 15.0,
            },
            {
                "revenue_bln": [100.0, 95.0, 90.0],
                "net_income_bln": 10.0,
                "equity_bln": 50.0,
                "roic_pct": 18.0,
                "ebit_margin_pct": 22.0,
                "fcf_margin_pct": 12.0,
                "debt_to_equity": 0.8,
                "current_ratio": 1.5,
                "shares_outstanding_mln": 1000.0,
            },
            {
                "pe_ratio": None,
                "pb_ratio": 3.8,
                "roe_pct": 14.0,
                "revenue_growth_pct": 6.0,
                "debt_to_equity": 0.9,
                "pe_ratio_valid_count": 0,
                "pb_ratio_valid_count": 3,
                "peer_count_usable": 3,
                "peer_count_weak": 0,
                "peer_count_supported": 3,
                "valuation_support_mode": "normal",
                "peer_confidence": "medium",
                "peer_baseline_reliability": "medium",
            },
        )

        self.assertTrue(metrics["valuation_enabled"])
        self.assertTrue(metrics["valuation_partial"])
        self.assertEqual(metrics["valuation_metric_count"], 1)
        self.assertIsNone(metrics["pe_premium_pct"])
        self.assertIsNotNone(metrics["pb_premium_pct"])

        weighted_scores = self.service._build_weighted_scores(
            metrics,
            {
                "fed_funds_rate_pct": 4.0,
                "inflation_pct": 3.0,
                "unemployment_pct": 4.0,
                "gdp_growth_pct": 2.0,
            },
        )

        self.assertIsNotNone(weighted_scores["valuation"][0])
        self.assertGreater(weighted_scores["valuation"][1], 0.0)

    def test_peer_confidence_reduces_valuation_weight(self) -> None:
        base_metrics = {
            "roe_pct": 20.0,
            "roe_score_pct": 20.0,
            "roic_pct": 18.0,
            "ebit_margin_pct": 20.0,
            "debt_to_equity": 1.0,
            "current_ratio": 1.5,
            "fcf_margin_pct": 10.0,
            "pe_premium_pct": 5.0,
            "pb_premium_pct": 5.0,
            "revenue_growth_pct": 10.0,
            "revenue_cagr_like_pct": 9.0,
            "one_year_return_pct": 5.0,
            "five_year_return_pct": 15.0,
            "is_bank_like": False,
            "valuation_enabled": True,
        }
        macro = {
            "fed_funds_rate_pct": 4.0,
            "inflation_pct": 3.0,
            "unemployment_pct": 4.0,
            "gdp_growth_pct": 2.0,
        }

        high_weight = self.service._build_weighted_scores(
            base_metrics | {"peer_confidence_multiplier": 1.0},
            macro,
        )["valuation"][1]
        low_weight = self.service._build_weighted_scores(
            base_metrics | {"peer_confidence_multiplier": 0.65},
            macro,
        )["valuation"][1]

        self.assertLess(low_weight, high_weight)

    def test_aapl_two_usable_and_one_weak_peer_set_stays_enabled(self) -> None:
        peer_averages = self.service._build_peer_averages(
            [
                {"ticker": "NVDA", "market_cap_bln": 2800.0, "pe_ratio": 38.45, "pb_ratio": 20.0, "roe_pct": 65.0, "revenue_growth_pct": 61.4, "debt_to_equity": 0.3},
                {"ticker": "MSFT", "market_cap_bln": 3100.0, "pe_ratio": 32.0, "pb_ratio": 10.0, "roe_pct": 35.0, "revenue_growth_pct": 15.0, "debt_to_equity": 0.4},
                {"ticker": "GOOGL", "market_cap_bln": 4500.0, "market_cap_status": "suspect", "pe_ratio": 26.0, "pb_ratio": 7.0, "roe_pct": 30.0, "revenue_growth_pct": 12.0, "debt_to_equity": 0.1},
            ],
            {
                "company_ticker": "AAPL",
                "usable_peer_tickers": ["NVDA", "MSFT"],
                "baseline_peer_tickers": ["NVDA", "MSFT", "GOOGL"],
            },
            {"current_price": 200.0},
            {"shares_outstanding_mln": 15000.0, "net_income_bln": 90.0, "equity_bln": 70.0},
        )

        self.assertEqual(peer_averages["valuation_baseline_mode"], "peer")
        self.assertFalse(peer_averages["peer_baseline_insufficient"])
        self.assertEqual(peer_averages["peer_baseline_reliability"], "low")
        self.assertEqual(peer_averages["valuation_support_mode"], "low_confidence")
        self.assertTrue(peer_averages["valuation_low_confidence"])
        self.assertEqual(peer_averages["peer_count_usable"], 2)
        self.assertEqual(peer_averages["peer_count_weak"], 1)
        self.assertTrue(peer_averages["peer_row_states"]["GOOGL"]["included_in_baseline"])
        self.assertAlmostEqual(peer_averages["peer_row_states"]["GOOGL"]["baseline_weight"], 0.12, places=2)
        self.assertLess(peer_averages["revenue_growth_pct"], 40.0)
        self.assertNotEqual(peer_averages["pe_ratio"], 3.33)

    def test_large_cap_anchor_peers_stay_in_aapl_fallback_baseline_even_when_suspect_and_sparse(self) -> None:
        peer_averages = self.service._build_peer_averages(
            [
                {"ticker": "MSFT", "sector": "Technology", "industry": "Software - Infrastructure", "market_cap_bln": 3100.0, "market_cap_status": "suspect", "pe_ratio": 34.0, "pb_ratio": None, "roe_pct": None, "revenue_growth_pct": None, "debt_to_equity": None},
                {"ticker": "NVDA", "sector": "Technology", "industry": "Semiconductors", "market_cap_bln": 2800.0, "market_cap_status": "suspect", "pe_ratio": 40.0, "pb_ratio": None, "roe_pct": None, "revenue_growth_pct": None, "debt_to_equity": None},
                {"ticker": "SONY", "sector": "Technology", "industry": "Consumer Electronics", "market_cap_bln": 120.0, "pe_ratio": 18.0, "pb_ratio": 2.2, "roe_pct": 12.0, "revenue_growth_pct": 4.0, "debt_to_equity": 0.5},
            ],
            {
                "company_ticker": "AAPL",
                "usable_peer_tickers": ["SONY"],
                "baseline_peer_tickers": ["MSFT", "NVDA", "SONY"],
            },
            {"ticker": "AAPL", "current_price": 200.0},
            {"shares_outstanding_mln": 15000.0, "net_income_bln": 90.0, "equity_bln": 70.0},
        )

        self.assertEqual(peer_averages["valuation_support_mode"], "fallback_low_confidence")
        self.assertTrue(peer_averages["valuation_low_confidence"])
        self.assertTrue(peer_averages["valuation_fallback"])
        self.assertTrue(peer_averages["peer_row_states"]["MSFT"]["included_in_baseline"])
        self.assertTrue(peer_averages["peer_row_states"]["NVDA"]["included_in_baseline"])
        self.assertAlmostEqual(peer_averages["peer_row_states"]["MSFT"]["baseline_weight"], 0.12, places=2)
        self.assertAlmostEqual(peer_averages["peer_row_states"]["NVDA"]["baseline_weight"], 0.12, places=2)

    def test_small_sample_warnings_are_emitted(self) -> None:
        warnings = self.service._build_data_quality_warnings(
            {"equity_bln": 10.0},
            {"fed_funds_rate_pct": 4.0, "inflation_pct": 3.0, "unemployment_pct": 4.0},
            {
                "pe_ratio_valid_count": 1,
                "pb_ratio_valid_count": 1,
                "roe_pct_valid_count": 1,
                "revenue_growth_pct_valid_count": 1,
                "debt_to_equity_valid_count": 1,
                "pe_ratio_baseline_noisy": True,
                "pb_ratio_baseline_noisy": True,
                "peer_selection_confidence": "medium",
                "peer_selection_source": "config",
                "peer_group_quality_passed": True,
                "peer_group_sample_limited": True,
                "peer_baseline_insufficient": True,
                "business_type_confidence": "medium",
            },
            False,
        )

        self.assertTrue(any("Peer low confidence:" in warning for warning in warnings))
        self.assertIn("Usable peer set too small (<3); valuation was disabled", warnings)
        self.assertIn("Valuation skipped due to insufficient comparable peers", warnings)

    def test_thematic_fallback_does_not_emit_disable_warning(self) -> None:
        warnings = self.service._build_data_quality_warnings(
            {"equity_bln": 10.0},
            {"fed_funds_rate_pct": 4.0, "inflation_pct": 3.0, "unemployment_pct": 4.0},
            {
                "pe_ratio_valid_count": 3,
                "pb_ratio_valid_count": 3,
                "roe_pct_valid_count": 3,
                "revenue_growth_pct_valid_count": 3,
                "debt_to_equity_valid_count": 3,
                "pe_ratio_baseline_noisy": True,
                "pb_ratio_baseline_noisy": True,
                "peer_selection_confidence": "low",
                "peer_selection_source": "business_type",
                "peer_group_sample_limited": True,
                "peer_baseline_insufficient": False,
                "peer_count_usable": 0,
                "peer_count_weak": 3,
                "valuation_support_mode": "weak_only_fallback",
                "valuation_low_confidence": True,
                "valuation_fallback": True,
                "valuation_baseline_mode": "thematic",
            },
            False,
        )

        self.assertNotIn("Usable peer set too small (<3); valuation was disabled", warnings)
        self.assertNotIn("Valuation skipped due to insufficient comparable peers", warnings)
        self.assertIn("Weak-only fallback baseline", warnings)

    def test_low_confidence_warning_uses_requested_copy(self) -> None:
        warnings = self.service._build_data_quality_warnings(
            {"equity_bln": 10.0},
            {"fed_funds_rate_pct": 4.0, "inflation_pct": 3.0, "unemployment_pct": 4.0},
            {
                "pe_ratio_valid_count": 3,
                "pb_ratio_valid_count": 3,
                "roe_pct_valid_count": 3,
                "revenue_growth_pct_valid_count": 3,
                "debt_to_equity_valid_count": 3,
                "peer_count_usable": 2,
                "peer_count_weak": 1,
                "valuation_support_mode": "low_confidence",
                "valuation_low_confidence": True,
                "peer_selection_confidence": "medium",
            },
            False,
        )

        self.assertIn("Low-confidence peer baseline", warnings)

    def test_fallback_warning_uses_requested_copy(self) -> None:
        warnings = self.service._build_data_quality_warnings(
            {"equity_bln": 10.0},
            {"fed_funds_rate_pct": 4.0, "inflation_pct": 3.0, "unemployment_pct": 4.0},
            {
                "pe_ratio_valid_count": 2,
                "pb_ratio_valid_count": 2,
                "roe_pct_valid_count": 2,
                "revenue_growth_pct_valid_count": 2,
                "debt_to_equity_valid_count": 2,
                "peer_count_usable": 1,
                "peer_count_weak": 2,
                "valuation_support_mode": "fallback_low_confidence",
                "valuation_low_confidence": True,
                "valuation_fallback": True,
                "peer_selection_confidence": "medium",
            },
            False,
        )

        self.assertIn("Fallback baseline from usable + weak peers", warnings)

    def test_peer_market_cap_warning_is_emitted(self) -> None:
        warnings = self.service._build_data_quality_warnings(
            {"equity_bln": 10.0},
            {"fed_funds_rate_pct": 4.0, "inflation_pct": 3.0, "unemployment_pct": 4.0},
            {
                "pe_ratio_valid_count": 3,
                "pb_ratio_valid_count": 3,
                "roe_pct_valid_count": 3,
                "revenue_growth_pct_valid_count": 3,
                "debt_to_equity_valid_count": 3,
                "pe_ratio_baseline_noisy": False,
                "pb_ratio_baseline_noisy": False,
                "market_cap_warning_count": 1,
            },
            False,
        )

        self.assertIn("Peer market cap normalization used fallback estimates", warnings)

    def test_suspect_market_cap_warning_uses_reduced_weight_copy(self) -> None:
        warnings = self.service._build_data_quality_warnings(
            {"equity_bln": 10.0},
            {"fed_funds_rate_pct": 4.0, "inflation_pct": 3.0, "unemployment_pct": 4.0},
            {
                "pe_ratio_valid_count": 3,
                "pb_ratio_valid_count": 3,
                "roe_pct_valid_count": 3,
                "revenue_growth_pct_valid_count": 3,
                "debt_to_equity_valid_count": 3,
                "market_cap_warning_count": 1,
                "market_cap_suspect_count": 1,
            },
            False,
        )

        self.assertIn("Suspect market cap used with reduced weight", warnings)

    def test_expansion_and_partial_universe_warnings_are_emitted(self) -> None:
        warnings = self.service._build_data_quality_warnings(
            {"equity_bln": 10.0},
            {"fed_funds_rate_pct": 4.0, "inflation_pct": 3.0, "unemployment_pct": 4.0},
            {
                "pe_ratio_valid_count": 2,
                "pb_ratio_valid_count": 2,
                "roe_pct_valid_count": 2,
                "revenue_growth_pct_valid_count": 2,
                "debt_to_equity_valid_count": 2,
                "pe_ratio_baseline_noisy": False,
                "pb_ratio_baseline_noisy": False,
                "peer_selection_confidence": "medium",
                "peer_selection_source": "business_type",
                "peer_group_quality_passed": True,
                "peer_group_sample_limited": True,
                "peer_selection_mode": "extended",
                "peer_expansion_level": 2,
                "peer_count": 2,
                "target_peer_count": 5,
                "incompatible_peer_count": 3,
                "business_type_confidence": "medium",
            },
            False,
        )

        self.assertTrue(any("Peer low confidence:" in warning for warning in warnings))
        self.assertIn("Peer cleanup applied: incompatible business models were excluded", warnings)

    def test_business_type_selection_keeps_reit_away_from_retail_and_industrials(self) -> None:
        selected, meta = self.service._select_peers_from_candidates(
            {
                "ticker": "O",
                "sector": "Real Estate",
                "industry": "Real Estate Investment Trust",
                "sic": "",
                "business_type": "REIT",
            },
            [
                {"ticker": "PLD", "sector": "Real Estate", "industry": "Real Estate Investment Trust", "sic": "", "market_cap_bln": 100.0, "pe_ratio": 30.0, "pb_ratio": 2.2, "roe_pct": 8.0, "revenue_growth_pct": 6.0, "debt_to_equity": 0.7},
                {"ticker": "SPG", "sector": "Real Estate", "industry": "Real Estate Investment Trust", "sic": "", "market_cap_bln": 55.0, "pe_ratio": 24.0, "pb_ratio": 1.8, "roe_pct": 9.0, "revenue_growth_pct": 4.0, "debt_to_equity": 0.9},
                {"ticker": "WMT", "sector": "Consumer Defensive", "industry": "Discount Stores", "sic": "", "market_cap_bln": 450.0, "pe_ratio": 28.0, "pb_ratio": 6.5, "roe_pct": 18.0, "revenue_growth_pct": 5.0, "debt_to_equity": 0.8},
                {"ticker": "GE", "sector": "Industrials", "industry": "Industrial Conglomerates", "sic": "", "market_cap_bln": 180.0, "pe_ratio": 30.0, "pb_ratio": 6.0, "roe_pct": 10.0, "revenue_growth_pct": 5.0, "debt_to_equity": 1.1},
            ],
            40.0,
            ["PLD", "SPG", "WMT", "GE"],
        )

        self.assertEqual([row["ticker"] for row in selected[:2]], ["SPG", "PLD"])
        self.assertFalse(meta["peer_group_quality_passed"])
        self.assertLess(meta["peer_count_usable"], 3)

    def test_config_provider_skips_broad_fallback_when_safe_business_universe_exists(self) -> None:
        provider = ConfigPeerProvider(
            {
                "rules": [],
                "fallback": {"tickers": ["CENN", "SEV", "FFAI", "XOM"]},
            }
        )

        result = provider.discover(
            "TSLA",
            {
                "ticker": "TSLA",
                "sector": "Unknown",
                "industry": "Unknown",
                "business_type": "AUTO_MANUFACTURER",
            },
        )

        self.assertEqual(result.candidates, [])
        self.assertIn("business-type-safe universe", result.reason)

    def test_tsla_rule_based_fallback_keeps_large_auto_peers_and_excludes_junk(self) -> None:
        selected, meta = self.service._select_peers_from_candidates(
            {
                "ticker": "TSLA",
                "sector": "Consumer Cyclical",
                "industry": "Automobiles",
                "sic": "",
                "business_type": "AUTO_MANUFACTURER",
            },
            [
                {"ticker": "GM", "sector": "Consumer Cyclical", "industry": "Automobiles", "sic": "3711", "market_cap_bln": 55.0, "pe_ratio": 6.0, "pb_ratio": 0.8, "roe_pct": 14.0, "revenue_growth_pct": 4.0, "debt_to_equity": 1.7},
                {"ticker": "F", "sector": "Consumer Cyclical", "industry": "Automobiles", "sic": "3711", "market_cap_bln": 52.0, "pe_ratio": 7.0, "pb_ratio": 1.1, "roe_pct": 16.0, "revenue_growth_pct": 5.0, "debt_to_equity": 2.0},
                {"ticker": "TM", "sector": "Consumer Cyclical", "industry": "Auto Manufacturers", "sic": "3711", "market_cap_bln": 310.0, "pe_ratio": 10.0, "pb_ratio": 1.3, "roe_pct": 11.0, "revenue_growth_pct": 7.0, "debt_to_equity": 1.1},
                {"ticker": "RIVN", "sector": "Consumer Cyclical", "industry": "Automobiles", "sic": "3711", "market_cap_bln": 13.0, "pe_ratio": None, "pb_ratio": 2.5, "roe_pct": None, "revenue_growth_pct": 18.0, "debt_to_equity": 0.4},
                {"ticker": "CENN", "sector": "Consumer Cyclical", "industry": "Automobiles", "sic": "3711", "market_cap_bln": 0.08, "pe_ratio": None, "pb_ratio": None, "roe_pct": None, "revenue_growth_pct": None, "debt_to_equity": None},
                {"ticker": "SEV", "sector": "Consumer Cyclical", "industry": "Automobiles", "sic": "3711", "market_cap_bln": 0.4, "pe_ratio": None, "pb_ratio": None, "roe_pct": None, "revenue_growth_pct": 2.0, "debt_to_equity": None},
                {"ticker": "FFAI", "sector": "Unknown", "industry": "Unknown", "sic": "", "market_cap_bln": 0.03, "pe_ratio": None, "pb_ratio": None, "roe_pct": None, "revenue_growth_pct": None, "debt_to_equity": None},
            ],
            700.0,
            ["GM", "F", "TM", "RIVN", "CENN", "SEV", "FFAI"],
        )

        selected_tickers = [row["ticker"] for row in selected]
        self.assertIn("GM", selected_tickers)
        self.assertIn("F", selected_tickers)
        self.assertIn("TM", selected_tickers)
        self.assertNotIn("CENN", selected_tickers)
        self.assertNotIn("SEV", selected_tickers)
        self.assertNotIn("FFAI", selected_tickers)
        self.assertGreaterEqual(meta["peer_count_usable"], 3)

    def test_tsla_valuation_uses_low_confidence_mode_with_two_clean_comparables(self) -> None:
        metrics = self.service._build_silver_metrics(
            {"current_price": 200.0, "one_year_return_pct": 12.0, "five_year_return_pct": 80.0},
            {
                "revenue_bln": [95.0, 82.0, 60.0],
                "net_income_bln": 12.0,
                "equity_bln": 68.0,
                "shares_outstanding_mln": 3200.0,
            },
            {
                "pe_ratio": 12.0,
                "pb_ratio": 4.0,
                "roe_pct": 14.0,
                "revenue_growth_pct": 8.0,
                "debt_to_equity": 1.0,
                "pe_ratio_valid_count": 2,
                "pb_ratio_valid_count": 2,
                "peer_count_usable": 2,
                "peer_selection_confidence": "medium",
            },
        )

        self.assertTrue(metrics["valuation_enabled"])
        self.assertTrue(metrics["valuation_low_confidence"])
        self.assertFalse(metrics["valuation_fallback"])
        self.assertFalse(metrics["valuation_partial"])

    def test_restaurants_do_not_take_retail_as_strict_peers(self) -> None:
        selected, meta = self.service._select_peers_from_candidates(
            {
                "ticker": "SBUX",
                "sector": "Consumer Cyclical",
                "industry": "Restaurants",
                "sic": "",
                "business_type": "RESTAURANTS",
            },
            [
                {"ticker": "MCD", "sector": "Consumer Cyclical", "industry": "Restaurants", "sic": "", "market_cap_bln": 210.0, "pe_ratio": 26.0, "pb_ratio": 0.0, "roe_pct": None, "revenue_growth_pct": 7.0, "debt_to_equity": None},
                {"ticker": "TJX", "sector": "Consumer Cyclical", "industry": "Apparel Retail", "sic": "", "market_cap_bln": 120.0, "pe_ratio": 25.0, "pb_ratio": 14.0, "roe_pct": 55.0, "revenue_growth_pct": 5.0, "debt_to_equity": 1.8},
            ],
            110.0,
            ["MCD", "TJX"],
        )

        self.assertEqual([row["ticker"] for row in selected[:1]], ["MCD"])
        self.assertGreaterEqual(meta["peer_expansion_level"], 1)

    def test_reject_peers_do_not_enter_scoring_baseline(self) -> None:
        selected, meta = self.service._select_peers_from_candidates(
            {
                "ticker": "UBER",
                "sector": "Technology",
                "industry": "Platform Services",
                "sic": "",
                "business_type": "INTERNET_PLATFORM",
            },
            [
                {"ticker": "XOM", "sector": "Energy", "industry": "Oil & Gas Integrated", "sic": "2911", "market_cap_bln": 450.0, "pe_ratio": 14.0, "pb_ratio": 2.0, "roe_pct": 16.0, "revenue_growth_pct": 4.0, "debt_to_equity": 0.1},
                {"ticker": "GE", "sector": "Industrials", "industry": "Industrial Conglomerates", "sic": "3500", "market_cap_bln": 180.0, "pe_ratio": 30.0, "pb_ratio": 6.0, "roe_pct": 10.0, "revenue_growth_pct": 5.0, "debt_to_equity": 1.1},
            ],
            140.0,
            ["XOM", "GE"],
        )

        self.assertEqual(selected, [])
        self.assertFalse(meta["peer_group_quality_passed"])

    def test_mixed_weak_peers_do_not_pass_quality_gate(self) -> None:
        selected, meta = self.service._select_peers_from_candidates(
            {
                "ticker": "HD",
                "sector": "Consumer Cyclical",
                "industry": "Home Improvement Retail",
                "sic": "5211",
                "business_type": "HOME_IMPROVEMENT_RETAIL",
            },
            [
                {"ticker": "LOW", "sector": "Consumer Cyclical", "industry": "Home Improvement Retail", "sic": "5211", "market_cap_bln": 130.0, "pe_ratio": 19.0, "pb_ratio": 12.0, "roe_pct": 85.0, "revenue_growth_pct": 4.0, "debt_to_equity": 4.2},
                {"ticker": "WMT", "sector": "Consumer Defensive", "industry": "Discount Stores", "sic": "5331", "market_cap_bln": 450.0, "pe_ratio": 28.0, "pb_ratio": 6.5, "roe_pct": 18.0, "revenue_growth_pct": 5.0, "debt_to_equity": 0.8},
                {"ticker": "COST", "sector": "Consumer Defensive", "industry": "Discount Stores", "sic": "5331", "market_cap_bln": 380.0, "pe_ratio": 45.0, "pb_ratio": 12.0, "roe_pct": 28.0, "revenue_growth_pct": 7.0, "debt_to_equity": 0.5},
            ],
            340.0,
            ["LOW", "WMT", "COST"],
        )

        self.assertEqual([row["ticker"] for row in selected[:1]], ["LOW"])
        self.assertEqual(set(row["ticker"] for row in selected[1:]), {"WMT", "COST"})
        self.assertFalse(meta["peer_group_quality_passed"])

    def test_api_first_peer_selection_prefers_primary_provider(self) -> None:
        class _Provider:
            def __init__(self, source: str, tickers: list[str], reason: str) -> None:
                self.source = source
                self.tickers = tickers
                self.reason = reason

            def discover(self, ticker: str, company_profile: dict) -> PeerDiscoveryResult:
                return PeerDiscoveryResult(
                    candidates=[PeerCandidate(ticker=item, source=self.source) for item in self.tickers],
                    source=self.source,
                    reason=self.reason,
                )

        self.service.peer_group_cache = type("Cache", (), {"get": staticmethod(lambda key: None), "set": staticmethod(lambda key, value: None)})()
        self.service.peer_providers = [
            _Provider("fmp", ["AMD", "NVDA", "QCOM"], "selected via FMP peers API and filtered by industry/market-cap similarity"),
            _Provider("config", ["IBM", "ORCL"], "selected from local config fallback due to insufficient API peers"),
        ]
        self.service._fetch_peer_rows = lambda tickers, company_ticker: [
            {"ticker": "AMD", "company": "AMD", "sector": "Technology", "industry": "Semiconductors", "sic": "3674", "market_cap_bln": 300.0, "pe_ratio": 25.0, "pb_ratio": 4.0, "roe_pct": 18.0, "revenue_growth_pct": 9.0, "debt_to_equity": 0.2},
            {"ticker": "NVDA", "company": "NVIDIA", "sector": "Technology", "industry": "Semiconductors", "sic": "3674", "market_cap_bln": 350.0, "pe_ratio": 30.0, "pb_ratio": 5.0, "roe_pct": 22.0, "revenue_growth_pct": 12.0, "debt_to_equity": 0.1},
            {"ticker": "QCOM", "company": "Qualcomm", "sector": "Technology", "industry": "Semiconductors", "sic": "3674", "market_cap_bln": 180.0, "pe_ratio": 20.0, "pb_ratio": 6.0, "roe_pct": 30.0, "revenue_growth_pct": 6.0, "debt_to_equity": 0.5},
        ]

        peers, meta = self.service._build_peer_group(
            {"ticker": "AAPL", "sector": "Technology", "industry": "Semiconductors", "sic": "3674"},
            {"current_price": 200.0},
            {"shares_outstanding_mln": 1500.0},
        )

        self.assertEqual({item["ticker"] for item in peers}, {"AMD", "NVDA", "QCOM"})
        self.assertEqual(meta["peer_selection_source"], "fmp")

    def test_api_peer_selection_falls_back_through_chain(self) -> None:
        class _Provider:
            def __init__(self, source: str, tickers: list[str], reason: str) -> None:
                self.source = source
                self.tickers = tickers
                self.reason = reason

            def discover(self, ticker: str, company_profile: dict) -> PeerDiscoveryResult:
                return PeerDiscoveryResult(
                    candidates=[PeerCandidate(ticker=item, source=self.source) for item in self.tickers],
                    source=self.source,
                    reason=self.reason,
                )

        self.service.peer_group_cache = type("Cache", (), {"get": staticmethod(lambda key: None), "set": staticmethod(lambda key, value: None)})()
        self.service.peer_providers = [
            _Provider("fmp", [], "selected via FMP peers API and filtered by industry/market-cap similarity"),
            _Provider("finnhub", ["MSFT", "ORCL"], "selected via Finnhub fallback and local filtering"),
            _Provider("config", ["IBM", "SAP", "CRM"], "selected from local config fallback due to insufficient API peers"),
        ]
        self.service._fetch_peer_rows = lambda tickers, company_ticker: [
            row
            for row in [
                {"ticker": "MSFT", "company": "Microsoft", "sector": "Technology", "industry": "Software", "sic": "7372", "market_cap_bln": 2900.0, "pe_ratio": 32.0, "pb_ratio": 10.0, "roe_pct": 33.0, "revenue_growth_pct": 12.0, "debt_to_equity": 0.4},
                {"ticker": "ORCL", "company": "Oracle", "sector": "Technology", "industry": "Software", "sic": "7372", "market_cap_bln": 320.0, "pe_ratio": 24.0, "pb_ratio": 22.0, "roe_pct": 45.0, "revenue_growth_pct": 8.0, "debt_to_equity": 5.0},
                {"ticker": "IBM", "company": "IBM", "sector": "Technology", "industry": "Information Technology Services", "sic": "7373", "market_cap_bln": 180.0, "pe_ratio": 18.0, "pb_ratio": 7.0, "roe_pct": 20.0, "revenue_growth_pct": 3.0, "debt_to_equity": 2.0},
                {"ticker": "SAP", "company": "SAP", "sector": "Technology", "industry": "Software", "sic": "7372", "market_cap_bln": 220.0, "pe_ratio": 28.0, "pb_ratio": 5.0, "roe_pct": 16.0, "revenue_growth_pct": 7.0, "debt_to_equity": 0.3},
                {"ticker": "CRM", "company": "Salesforce", "sector": "Technology", "industry": "Software", "sic": "7372", "market_cap_bln": 260.0, "pe_ratio": 26.0, "pb_ratio": 4.0, "roe_pct": 12.0, "revenue_growth_pct": 9.0, "debt_to_equity": 0.2},
            ]
            if row["ticker"] in tickers and row["ticker"] != company_ticker
        ]

        peers, meta = self.service._build_peer_group(
            {"ticker": "ADBE", "sector": "Technology", "industry": "Software", "sic": "7372"},
            {"current_price": 500.0},
            {"shares_outstanding_mln": 450.0},
        )

        self.assertEqual(meta["peer_selection_source"], "config")
        self.assertIn(meta["peer_selection_confidence"], {"medium", "high"})
        self.assertIn("fallback", meta["peer_selection_reason"])

    def test_target_count_expansion_accumulates_candidates_across_sources(self) -> None:
        class _Provider:
            def __init__(self, source: str, tickers: list[str], reason: str) -> None:
                self.source = source
                self.tickers = tickers
                self.reason = reason

            def discover(self, ticker: str, company_profile: dict) -> PeerDiscoveryResult:
                return PeerDiscoveryResult(
                    candidates=[PeerCandidate(ticker=item, source=self.source) for item in self.tickers],
                    source=self.source,
                    reason=self.reason,
                )

        self.service.peer_group_cache = type("Cache", (), {"get": staticmethod(lambda key: None), "set": staticmethod(lambda key, value: None)})()
        self.service.peer_target_count = 3
        self.service.peer_min_valid_count = 2
        self.service.peer_providers = [
            _Provider("fmp", ["LOW"], "selected via FMP peers API and filtered by industry/market-cap similarity"),
            _Provider("business_type", ["FLOOR"], "selected via business-type fallback universe"),
            _Provider("config", ["WMT"], "selected from local config fallback due to insufficient API peers"),
        ]
        self.service._fetch_peer_rows = lambda tickers, company_ticker: [
            row
            for row in [
                {"ticker": "LOW", "company": "Lowe's", "sector": "Consumer Cyclical", "industry": "Home Improvement Retail", "sic": "", "market_cap_bln": 130.0, "pe_ratio": 19.0, "pb_ratio": 12.0, "roe_pct": 85.0, "revenue_growth_pct": 4.0, "debt_to_equity": 4.2},
                {"ticker": "FLOOR", "company": "Floor & Decor", "sector": "Consumer Cyclical", "industry": "Home Improvement Retail", "sic": "", "market_cap_bln": 12.0, "pe_ratio": 32.0, "pb_ratio": 4.0, "roe_pct": 14.0, "revenue_growth_pct": 9.0, "debt_to_equity": 0.2},
                {"ticker": "WMT", "company": "Walmart", "sector": "Consumer Defensive", "industry": "Discount Stores", "sic": "", "market_cap_bln": 450.0, "pe_ratio": 28.0, "pb_ratio": 6.5, "roe_pct": 18.0, "revenue_growth_pct": 5.0, "debt_to_equity": 0.8},
            ]
            if row["ticker"] in tickers and row["ticker"] != company_ticker
        ]

        peers, meta = self.service._build_peer_group(
            {"ticker": "HD", "sector": "Consumer Cyclical", "industry": "Home Improvement Retail", "sic": "", "business_type": "HOME_IMPROVEMENT_RETAIL"},
            {"current_price": 340.0},
            {"shares_outstanding_mln": 1000.0},
        )

        self.assertEqual([row["ticker"] for row in peers[:2]], ["LOW", "FLOOR"])
        self.assertEqual(meta["peer_count"], 3)
        self.assertEqual(meta["peer_sample_mode"], "full")
        self.assertEqual(meta["peer_expansion_level"], 3)

    def test_fail_closed_when_broad_fallback_is_irrelevant(self) -> None:
        class _Provider:
            def __init__(self, source: str, tickers: list[str], reason: str) -> None:
                self.source = source
                self.tickers = tickers
                self.reason = reason

            def discover(self, ticker: str, company_profile: dict) -> PeerDiscoveryResult:
                return PeerDiscoveryResult(
                    candidates=[PeerCandidate(ticker=item, source=self.source) for item in self.tickers],
                    source=self.source,
                    reason=self.reason,
                )

        self.service.peer_group_cache = type("Cache", (), {"get": staticmethod(lambda key: None), "set": staticmethod(lambda key, value: None)})()
        self.service.peer_providers = [
            _Provider("fmp", ["XOM", "GE"], "selected via FMP peers API and filtered by industry/market-cap similarity"),
            _Provider("finnhub", ["TSLA", "GE"], "selected via Finnhub fallback and local filtering"),
            _Provider("config", ["XOM", "GE", "TSLA"], "selected from broad config fallback due to insufficient API peers"),
        ]
        self.service._fetch_peer_rows = lambda tickers, company_ticker: [
            row
            for row in [
                {"ticker": "XOM", "company": "Exxon", "sector": "Energy", "industry": "Oil & Gas Integrated", "sic": "2911", "market_cap_bln": 460.0, "pe_ratio": 14.0, "pb_ratio": 2.0, "roe_pct": 16.0, "revenue_growth_pct": 4.0, "debt_to_equity": 0.1},
                {"ticker": "GE", "company": "GE", "sector": "Industrials", "industry": "Industrial Conglomerates", "sic": "3500", "market_cap_bln": 180.0, "pe_ratio": 30.0, "pb_ratio": 6.0, "roe_pct": 10.0, "revenue_growth_pct": 5.0, "debt_to_equity": 1.1},
                {"ticker": "TSLA", "company": "Tesla", "sector": "Consumer Cyclical", "industry": "Automobiles", "sic": "3711", "market_cap_bln": 700.0, "pe_ratio": 95.0, "pb_ratio": 9.0, "roe_pct": 18.0, "revenue_growth_pct": 20.0, "debt_to_equity": 0.2},
            ]
            if row["ticker"] in tickers and row["ticker"] != company_ticker
        ]

        peers, meta = self.service._build_peer_group(
            {"ticker": "ABNB", "sector": "Consumer Cyclical", "industry": "Travel Services", "sic": "", "business_type": "INTERNET_PLATFORM"},
            {"current_price": 160.0},
            {"shares_outstanding_mln": 650.0},
        )

        self.assertEqual(peers, [])
        self.assertFalse(meta["peer_group_quality_passed"])
        self.assertEqual(meta["peer_selection_confidence"], "low")

    def test_low_confidence_peer_warning_includes_fallback_source(self) -> None:
        warnings = self.service._build_data_quality_warnings(
            {"equity_bln": 10.0},
            {"fed_funds_rate_pct": 4.0, "inflation_pct": 3.0, "unemployment_pct": 4.0},
            {
                "pe_ratio_valid_count": 2,
                "pb_ratio_valid_count": 2,
                "roe_pct_valid_count": 2,
                "revenue_growth_pct_valid_count": 2,
                "debt_to_equity_valid_count": 2,
                "pe_ratio_baseline_noisy": False,
                "pb_ratio_baseline_noisy": False,
                "peer_selection_confidence": "low",
                "peer_selection_mode": "fallback",
                "peer_selection_source": "config",
                "peer_group_quality_passed": False,
                "business_type_confidence": "low",
            },
            False,
        )

        self.assertTrue(any("Peer low confidence:" in warning for warning in warnings))

    def test_jpm_bank_stability_does_not_fall_to_zero_from_generic_rules(self) -> None:
        weighted_scores = self.service._build_weighted_scores(
            {
                "roe_pct": 16.0,
                "roic_pct": None,
                "ebit_margin_pct": None,
                "debt_to_equity": None,
                "current_ratio": None,
                "fcf_margin_pct": None,
                "pe_premium_pct": 10.0,
                "pb_premium_pct": 5.0,
                "revenue_growth_pct": 5.0,
                "revenue_cagr_like_pct": 4.0,
                "one_year_return_pct": 12.0,
                "five_year_return_pct": 85.0,
                "is_bank_like": True,
                "peer_count_usable": 4,
            },
            {
                "fed_funds_rate_pct": 4.0,
                "inflation_pct": 3.0,
                "unemployment_pct": 4.0,
                "gdp_growth_pct": 2.0,
            },
        )

        self.assertGreater(weighted_scores["stability"][0], 0.0)

    def test_jpm_bank_profitability_uses_roe_when_roic_and_ebit_are_missing(self) -> None:
        weighted_scores = self.service._build_weighted_scores(
            {
                "roe_pct": 16.0,
                "roe_score_pct": 16.0,
                "roic_pct": None,
                "ebit_margin_pct": None,
                "debt_to_equity": None,
                "current_ratio": None,
                "fcf_margin_pct": None,
                "pe_premium_pct": 10.0,
                "pb_premium_pct": 5.0,
                "revenue_growth_pct": 5.0,
                "revenue_cagr_like_pct": 4.0,
                "one_year_return_pct": 12.0,
                "five_year_return_pct": 85.0,
                "is_bank_like": True,
                "peer_count_usable": 4,
            },
            {
                "fed_funds_rate_pct": 4.0,
                "inflation_pct": 3.0,
                "unemployment_pct": 4.0,
                "gdp_growth_pct": 2.0,
            },
        )

        self.assertGreater(weighted_scores["profitability"][0], 50.0)
        self.assertGreater(weighted_scores["growth"][0], 20.0)

    def test_bank_roe_is_not_downgraded_by_equity_to_market_cap_rule(self) -> None:
        assessment = self.service._roe_assessment(
            {
                "net_income_bln": 45.0,
                "equity_bln": 300.0,
                "roic_pct": 4.0,
            },
            780.0,
            is_bank_like=True,
        )

        self.assertTrue(assessment["reliable"])
        self.assertEqual(assessment["score_input_pct"], assessment["display_pct"])
        self.assertIsNone(assessment["reason"])

    def test_direct_peer_baseline_requires_at_least_three_usable_peers(self) -> None:
        peer_averages = self.service._build_peer_averages(
            [
                {"ticker": "CRM", "pe_ratio": 26.0, "pb_ratio": 4.0, "roe_pct": 12.0, "revenue_growth_pct": 9.0, "debt_to_equity": 0.2},
                {"ticker": "ADBE", "pe_ratio": 30.0, "pb_ratio": 12.0, "roe_pct": 35.0, "revenue_growth_pct": 11.0, "debt_to_equity": 0.3},
            ],
            {
                "company_ticker": "MSFT",
                "usable_peer_tickers": ["CRM", "ADBE"],
            },
            {"current_price": 100.0},
            {"shares_outstanding_mln": 1000.0, "net_income_bln": 10.0, "equity_bln": 50.0},
        )

        self.assertNotEqual(peer_averages["valuation_baseline_mode"], "peer")
        self.assertEqual(peer_averages["peer_baseline_reliability"], "low")
        self.assertIsNone(peer_averages["pe_ratio"])

    def test_peer_averages_downweight_rows_with_untrusted_market_cap(self) -> None:
        peer_averages = self.service._build_peer_averages(
            [
                {"ticker": "MSFT", "sector": "Technology", "industry": "Software - Infrastructure", "market_cap_bln": 3100.0, "pe_ratio": 30.0, "pb_ratio": 10.0, "roe_pct": 35.0, "revenue_growth_pct": 12.0, "debt_to_equity": 0.4},
                {"ticker": "ORCL", "sector": "Technology", "industry": "Software - Infrastructure", "market_cap_bln": 380.0, "pe_ratio": 20.0, "pb_ratio": 8.0, "roe_pct": 28.0, "revenue_growth_pct": 9.0, "debt_to_equity": 1.7},
                {"ticker": "ADBE", "sector": "Technology", "industry": "Software - Infrastructure", "market_cap_bln": 220.0, "pe_ratio": 10.0, "pb_ratio": 12.0, "roe_pct": 30.0, "revenue_growth_pct": 11.0, "debt_to_equity": 0.3},
                {"ticker": "TSM", "sector": "Technology", "industry": "Semiconductors", "market_cap_bln": None, "market_cap_suspect": True, "market_cap_warning": "market cap failed sanity checks", "pe_ratio": 90.0, "pb_ratio": 25.0, "roe_pct": 6.0, "revenue_growth_pct": 2.0, "debt_to_equity": 0.1},
            ],
            {
                "company_ticker": "NVDA",
                "usable_peer_tickers": ["MSFT", "ORCL", "ADBE", "TSM"],
            },
            {"current_price": 100.0},
            {"shares_outstanding_mln": 1000.0, "net_income_bln": 10.0, "equity_bln": 50.0},
        )

        self.assertEqual(peer_averages["pe_ratio_valid_count"], 4)
        self.assertEqual(peer_averages["pb_ratio_valid_count"], 4)
        self.assertAlmostEqual(peer_averages["pe_ratio"], 16.61, places=2)
        self.assertAlmostEqual(peer_averages["pb_ratio"], 10.11, places=2)
        self.assertAlmostEqual(peer_averages["pe_ratio_weighted_support"], 2.24, places=2)
        self.assertEqual(peer_averages["valuation_support_mode"], "low_confidence")
        self.assertTrue(peer_averages["valuation_low_confidence"])

    def test_weak_peer_share_is_capped_in_baseline(self) -> None:
        peer_averages = self.service._build_peer_averages(
            [
                {"ticker": "GM", "sector": "Consumer Cyclical", "industry": "Automobiles", "market_cap_bln": 55.0, "pe_ratio": 6.0, "pb_ratio": 0.8, "roe_pct": 14.0, "revenue_growth_pct": 4.0, "debt_to_equity": 1.7},
                {"ticker": "F", "sector": "Consumer Cyclical", "industry": "Automobiles", "market_cap_bln": 52.0, "pe_ratio": 7.0, "pb_ratio": 1.1, "roe_pct": 16.0, "revenue_growth_pct": 5.0, "debt_to_equity": 2.0},
                {"ticker": "RIVN", "sector": "Consumer Cyclical", "industry": "Automobiles", "market_cap_bln": 13.0, "pe_ratio": None, "pb_ratio": 2.5, "roe_pct": None, "revenue_growth_pct": None, "debt_to_equity": None},
                {"ticker": "LCID", "sector": "Consumer Cyclical", "industry": "Automobiles", "market_cap_bln": 8.0, "pe_ratio": None, "pb_ratio": 2.2, "roe_pct": None, "revenue_growth_pct": None, "debt_to_equity": None},
                {"ticker": "NIO", "sector": "Consumer Cyclical", "industry": "Automobiles", "market_cap_bln": 9.0, "pe_ratio": None, "pb_ratio": 2.1, "roe_pct": None, "revenue_growth_pct": None, "debt_to_equity": None},
                {"ticker": "XPEV", "sector": "Consumer Cyclical", "industry": "Automobiles", "market_cap_bln": 11.0, "pe_ratio": None, "pb_ratio": 2.4, "roe_pct": None, "revenue_growth_pct": None, "debt_to_equity": None},
                {"ticker": "LI", "sector": "Consumer Cyclical", "industry": "Automobiles", "market_cap_bln": 12.0, "pe_ratio": None, "pb_ratio": 2.0, "roe_pct": None, "revenue_growth_pct": None, "debt_to_equity": None},
                {"ticker": "BIDU", "sector": "Consumer Cyclical", "industry": "Automobiles", "market_cap_bln": 15.0, "pe_ratio": None, "pb_ratio": 1.9, "roe_pct": None, "revenue_growth_pct": None, "debt_to_equity": None},
            ],
            {
                "company_ticker": "TSLA",
                "usable_peer_tickers": ["GM", "F"],
                "baseline_peer_tickers": ["GM", "F", "RIVN", "LCID", "NIO", "XPEV", "LI", "BIDU"],
            },
            {"ticker": "TSLA", "current_price": 200.0},
            {"shares_outstanding_mln": 3200.0, "net_income_bln": 12.0, "equity_bln": 68.0},
        )

        self.assertEqual(peer_averages["valuation_support_mode"], "low_confidence")
        self.assertLessEqual(peer_averages["peer_support_effective"], 3.1)
        self.assertLessEqual(peer_averages["peer_row_states"]["RIVN"]["baseline_weight"], 0.18)

    def test_extended_or_thematic_peers_are_used_when_strict_set_is_below_three(self) -> None:
        selected, meta = self.service._select_peers_from_candidates(
            {
                "ticker": "MSFT",
                "sector": "Technology",
                "industry": "Software - Infrastructure",
                "sic": "",
                "business_type": "ENTERPRISE_SOFTWARE",
            },
            [
                {"ticker": "ORCL", "sector": "Technology", "industry": "Software - Infrastructure", "sic": "", "market_cap_bln": 380.0, "pe_ratio": 28.0, "pb_ratio": 30.0, "roe_pct": None, "revenue_growth_pct": 8.0, "debt_to_equity": 5.0},
                {"ticker": "ADBE", "sector": "Technology", "industry": "Software - Infrastructure", "sic": "", "market_cap_bln": 220.0, "pe_ratio": 30.0, "pb_ratio": 12.0, "roe_pct": 35.0, "revenue_growth_pct": 11.0, "debt_to_equity": 0.3},
                {"ticker": "CRM", "sector": "Technology", "industry": "Software - Application", "sic": "", "market_cap_bln": 260.0, "pe_ratio": 26.0, "pb_ratio": 4.0, "roe_pct": 12.0, "revenue_growth_pct": 9.0, "debt_to_equity": 0.2},
                {"ticker": "SAP", "sector": "Technology", "industry": "Software", "sic": "", "market_cap_bln": 220.0, "pe_ratio": 28.0, "pb_ratio": 5.0, "roe_pct": 16.0, "revenue_growth_pct": 7.0, "debt_to_equity": 0.3},
            ],
            3100.0,
            ["ORCL", "ADBE", "CRM", "SAP"],
        )

        self.assertNotEqual(selected, [])
        self.assertGreaterEqual(meta["peer_count_total"], 3)

    def test_aapl_mega_cap_peers_reach_ranked_pool(self) -> None:
        selected, meta = self.service._select_peers_from_candidates(
            {
                "ticker": "AAPL",
                "sector": "Technology",
                "industry": "Consumer Electronics",
                "sic": "",
                "business_type": "CONSUMER_HARDWARE_ECOSYSTEM",
            },
            [
                {"ticker": "MSFT", "sector": "Technology", "industry": "Software - Infrastructure", "sic": "", "market_cap_bln": 3100.0, "pe_ratio": 34.0, "pb_ratio": 10.0, "roe_pct": 35.0, "revenue_growth_pct": 15.0, "debt_to_equity": 0.4},
                {"ticker": "GOOGL", "sector": "Communication Services", "industry": "Internet Content & Information", "sic": "", "market_cap_bln": 2000.0, "pe_ratio": 26.0, "pb_ratio": 7.0, "roe_pct": 30.0, "revenue_growth_pct": 12.0, "debt_to_equity": 0.1},
                {"ticker": "AMZN", "sector": "Consumer Cyclical", "industry": "Internet Retail", "sic": "", "market_cap_bln": 1900.0, "pe_ratio": 45.0, "pb_ratio": 8.0, "roe_pct": 24.0, "revenue_growth_pct": 11.0, "debt_to_equity": 0.6},
                {"ticker": "META", "sector": "Communication Services", "industry": "Internet Content & Information", "sic": "", "market_cap_bln": 1500.0, "pe_ratio": 28.0, "pb_ratio": 9.0, "roe_pct": 33.0, "revenue_growth_pct": 18.0, "debt_to_equity": 0.2},
                {"ticker": "NVDA", "sector": "Technology", "industry": "Semiconductors", "sic": "", "market_cap_bln": 2800.0, "pe_ratio": 40.0, "pb_ratio": 20.0, "roe_pct": 65.0, "revenue_growth_pct": 60.0, "debt_to_equity": 0.3},
            ],
            3200.0,
            ["MSFT", "GOOGL", "AMZN", "META", "NVDA"],
        )

        self.assertGreaterEqual(meta["peer_count_total"], 4)
        self.assertIn("MSFT", [row["ticker"] for row in selected])
        self.assertIn("GOOGL", [row["ticker"] for row in selected])

    def test_relative_valuation_is_not_perfect_at_peer_parity(self) -> None:
        weighted_scores = self.service._build_weighted_scores(
            {
                "roe_pct": 20.0,
                "roic_pct": 20.0,
                "ebit_margin_pct": 20.0,
                "debt_to_equity": 1.0,
                "current_ratio": 1.5,
                "fcf_margin_pct": 10.0,
                "pe_premium_pct": 0.0,
                "pb_premium_pct": 0.0,
                "revenue_growth_pct": 10.0,
                "revenue_cagr_like_pct": 9.0,
                "one_year_return_pct": 5.0,
                "five_year_return_pct": 15.0,
                "is_bank_like": False,
            },
            {
                "fed_funds_rate_pct": 4.0,
                "inflation_pct": 3.0,
                "unemployment_pct": 4.0,
                "gdp_growth_pct": 2.0,
            },
        )

        self.assertLess(weighted_scores["valuation"][0], 70.0)

    def test_sparse_peer_baseline_makes_labels_neutral(self) -> None:
        cards = self.service._metric_cards(
            {
                "pe_ratio": 18.0,
                "roe_pct": 12.0,
                "revenue_growth_pct": 7.0,
                "debt_to_equity": 1.0,
                "peer_pe_valid_count": 2,
                "peer_roe_valid_count": 2,
                "peer_growth_valid_count": 2,
                "peer_debt_valid_count": 2,
            },
            {
                "pe_ratio": 15.0,
                "roe_pct": 10.0,
                "revenue_growth_pct": 5.0,
                "debt_to_equity": 1.2,
            },
        )

        self.assertTrue(all(card.comparison_label == "Нейтрально" for card in cards))

    def test_incomplete_block_is_capped(self) -> None:
        weighted_scores = self.service._build_weighted_scores(
            {
                "roe_pct": 50.0,
                "roic_pct": None,
                "ebit_margin_pct": None,
                "debt_to_equity": 1.0,
                "current_ratio": 1.5,
                "fcf_margin_pct": 10.0,
                "pe_premium_pct": 5.0,
                "pb_premium_pct": 5.0,
                "revenue_growth_pct": 10.0,
                "revenue_cagr_like_pct": 9.0,
                "one_year_return_pct": 5.0,
                "five_year_return_pct": 15.0,
                "is_bank_like": False,
            },
            {
                "fed_funds_rate_pct": 4.0,
                "inflation_pct": 3.0,
                "unemployment_pct": 4.0,
                "gdp_growth_pct": 2.0,
            },
        )

        self.assertLessEqual(weighted_scores["profitability"][0], 76.7)

    def test_bank_scoring_excludes_generic_corporate_stability_metrics(self) -> None:
        self.assertTrue(is_bank_like_company("Financial Services", "Commercial Banks"))

        metrics = self.service._build_silver_metrics(
            {
                "current_price": 200.0,
                "one_year_return_pct": 10.0,
                "five_year_return_pct": 40.0,
            },
            {
                "revenue_bln": [100.0, 95.0, 90.0],
                "net_income_bln": 12.0,
                "equity_bln": 50.0,
                "roic_pct": 14.0,
                "ebit_margin_pct": 30.0,
                "fcf_margin_pct": None,
                "debt_to_equity": 8.0,
                "current_ratio": 0.1,
                "shares_outstanding_mln": 1000.0,
            },
            {
                "pe_ratio": 14.0,
                "pb_ratio": 1.5,
                "roe_pct": 11.0,
                "revenue_growth_pct": 4.0,
                "debt_to_equity": 7.0,
            },
            True,
        )

        weighted_scores = self.service._build_weighted_scores(
            metrics
            | {
                "pe_premium_pct": 10.0,
                "pb_premium_pct": 10.0,
                "peer_pe_valid_count": 4,
                "peer_pb_valid_count": 4,
                "peer_roe_valid_count": 4,
                "peer_growth_valid_count": 4,
                "peer_debt_valid_count": 4,
            },
            {
                "fed_funds_rate_pct": 4.0,
                "inflation_pct": 3.0,
                "unemployment_pct": 4.0,
                "gdp_growth_pct": 2.0,
            },
        )

        self.assertGreater(weighted_scores["stability"][1], 0.0)
        self.assertGreater(weighted_scores["stability"][0], 0.0)
        self.assertGreater(weighted_scores["growth"][0], 0.0)

        warnings = self.service._build_data_quality_warnings(
            {"equity_bln": 50.0},
            {"fed_funds_rate_pct": 4.0, "inflation_pct": 3.0, "unemployment_pct": 4.0},
            {
                "pe_ratio_valid_count": 4,
                "pb_ratio_valid_count": 4,
                "roe_pct_valid_count": 4,
                "revenue_growth_pct_valid_count": 4,
                "debt_to_equity_valid_count": 4,
                "pe_ratio_baseline_noisy": False,
                "pb_ratio_baseline_noisy": False,
            },
            True,
        )
        self.assertIn("Sector-specific fallback applied: bank-safe stability logic replaced generic debt/FCF rules", warnings)

    def test_bank_stability_score_no_longer_depends_on_peer_count_placeholder(self) -> None:
        base_metrics = {
            "roe_pct": 18.0,
            "roic_pct": None,
            "ebit_margin_pct": None,
            "debt_to_equity": 8.0,
            "current_ratio": None,
            "fcf_margin_pct": None,
            "pe_premium_pct": 10.0,
            "pb_premium_pct": 10.0,
            "revenue_growth_pct": 5.0,
            "revenue_cagr_like_pct": 4.0,
            "one_year_return_pct": 3.0,
            "five_year_return_pct": 10.0,
            "is_bank_like": True,
        }
        macro = {
            "fed_funds_rate_pct": 4.0,
            "inflation_pct": 3.0,
            "unemployment_pct": 4.0,
            "gdp_growth_pct": 2.0,
        }

        low_peer_score = self.service._build_weighted_scores(base_metrics | {"peer_count_usable": 1}, macro)["stability"][0]
        high_peer_score = self.service._build_weighted_scores(base_metrics | {"peer_count_usable": 5}, macro)["stability"][0]

        self.assertEqual(low_peer_score, high_peer_score)

    def test_peer_rows_show_excluded_entries_with_reason(self) -> None:
        rows = self.service._peer_rows(
            [
                {
                    "ticker": "TEST",
                    "company": "Test",
                    "sector": "Technology",
                    "industry": "Software",
                    "market_cap_bln": 120.0,
                    "pe_ratio": None,
                    "roe_pct": None,
                    "revenue_growth_pct": None,
                },
                {
                    "ticker": "GOOD",
                    "company": "Good",
                    "sector": "Technology",
                    "industry": "Software",
                    "market_cap_bln": 200.0,
                    "pe_ratio": 20.0,
                    "roe_pct": 18.0,
                    "revenue_growth_pct": 12.0,
                }
            ]
        )

        self.assertEqual([row.ticker for row in rows], ["GOOD", "TEST"])
        self.assertEqual(rows[1].quality_class, "excluded")
        self.assertEqual(rows[1].quality_note, "Excluded from baseline: no usable metrics")

    def test_peer_rows_hide_suspect_market_cap_value_but_keep_warning_label(self) -> None:
        rows = self.service._peer_rows(
            [
                {
                    "ticker": "TM",
                    "company": "Toyota",
                    "sector": "Consumer Cyclical",
                    "industry": "Auto Manufacturers",
                    "market_cap_bln": 2771.07,
                    "market_cap_status": "suspect",
                    "quality_class": "weak",
                    "quality_reasons": ["suspect_market_cap", "market_cap_outlier_vs_peers"],
                    "pe_ratio": 10.0,
                    "roe_pct": 11.0,
                    "revenue_growth_pct": 7.0,
                }
            ]
        )

        self.assertEqual(rows[0].ticker, "TM")
        self.assertIsNone(rows[0].market_cap_bln)
        self.assertIn("suspect market cap", rows[0].quality_note.lower())

    def test_peer_rows_expose_baseline_flags_and_weights(self) -> None:
        rows = self.service._peer_rows(
            [
                {
                    "ticker": "GM",
                    "company": "GM",
                    "sector": "Consumer Cyclical",
                    "industry": "Automobiles",
                    "market_cap_bln": 55.0,
                    "pe_ratio": 6.0,
                    "roe_pct": 14.0,
                    "revenue_growth_pct": 4.0,
                    "quality_class": "usable",
                    "included_in_baseline": True,
                    "baseline_weight": 1.0,
                },
                {
                    "ticker": "RIVN",
                    "company": "Rivian",
                    "sector": "Consumer Cyclical",
                    "industry": "Automobiles",
                    "market_cap_bln": 13.0,
                    "pe_ratio": None,
                    "roe_pct": None,
                    "revenue_growth_pct": 18.0,
                    "quality_class": "weak",
                    "included_in_baseline": True,
                    "baseline_weight": 0.25,
                },
                {
                    "ticker": "LCID",
                    "company": "Lucid",
                    "sector": "Consumer Cyclical",
                    "industry": "Automobiles",
                    "market_cap_bln": 8.0,
                    "pe_ratio": None,
                    "roe_pct": None,
                    "revenue_growth_pct": 10.0,
                    "quality_class": "weak",
                    "included_in_baseline": False,
                    "baseline_weight": 0.0,
                },
            ]
        )

        self.assertTrue(rows[0].included_in_baseline)
        self.assertEqual(rows[0].baseline_weight, 1.0)
        self.assertTrue(rows[1].included_in_baseline)
        self.assertEqual(rows[1].baseline_weight, 0.25)
        self.assertFalse(rows[2].included_in_baseline)
        self.assertEqual(rows[2].baseline_weight, 0.0)

    def test_peer_baseline_exclusion_is_logged_with_reason(self) -> None:
        with self.assertLogs("app.services.analysis_runtime_service", level="INFO") as captured:
            peer_averages = self.service._build_peer_averages(
                [
                    {"ticker": "GOOD1", "sector": "Technology", "industry": "Software", "market_cap_bln": 300.0, "pe_ratio": 18.0, "pb_ratio": 4.0, "roe_pct": 15.0, "revenue_growth_pct": 8.0, "debt_to_equity": 0.2},
                    {"ticker": "GOOD2", "sector": "Technology", "industry": "Software", "market_cap_bln": 250.0, "pe_ratio": 20.0, "pb_ratio": 5.0, "roe_pct": 17.0, "revenue_growth_pct": 9.0, "debt_to_equity": 0.3},
                    {"ticker": "GOOD3", "sector": "Technology", "industry": "Software", "market_cap_bln": 200.0, "pe_ratio": 22.0, "pb_ratio": 6.0, "roe_pct": 19.0, "revenue_growth_pct": 10.0, "debt_to_equity": 0.4},
                    {"ticker": "BAD", "sector": "Unknown", "industry": "Unknown", "market_cap_bln": None, "market_cap_suspect": True, "pe_ratio": None, "pb_ratio": None, "roe_pct": None, "revenue_growth_pct": None, "debt_to_equity": None},
                ],
                {
                    "company_ticker": "MSFT",
                    "usable_peer_tickers": ["GOOD1", "GOOD2", "GOOD3", "BAD"],
                },
                {"current_price": 100.0},
                {"shares_outstanding_mln": 1000.0, "net_income_bln": 10.0, "equity_bln": 50.0},
            )

        self.assertEqual(peer_averages["pe_ratio_valid_count"], 3)
        joined_logs = "\n".join(captured.output)
        self.assertIn("peer_row_excluded", joined_logs)
        self.assertIn("BAD", joined_logs)

    def test_real_zero_stays_distinct_from_invalid(self) -> None:
        cards = self.service._metric_cards(
            {
                "pe_ratio": 15.0,
                "roe_pct": None,
                "revenue_growth_pct": 0.0,
                "debt_to_equity": None,
                "peer_pe_valid_count": 3,
                "peer_roe_valid_count": 3,
                "peer_growth_valid_count": 3,
                "peer_debt_valid_count": 3,
            },
            {
                "pe_ratio": 14.0,
                "roe_pct": 10.0,
                "revenue_growth_pct": 5.0,
                "debt_to_equity": 1.2,
            },
        )

        growth_card = next(card for card in cards if "Revenue Growth" in card.label)
        debt_card = next(card for card in cards if "Debt/Equity" in card.label)

        self.assertEqual(growth_card.value, 0.0)
        self.assertNotEqual(growth_card.comparison_label, "Недостаточно данных")
        self.assertIsNone(debt_card.value)
        self.assertEqual(debt_card.display_value, "N/A")

    def test_backward_compatible_payload_shape(self) -> None:
        response = AnalysisResponse(
            ticker="TEST",
            company="Test Corp",
            sector="Technology",
            industry="Software",
            score=55.0,
            verdict="Neutral",
            narrative="Test narrative",
            metric_cards=[],
            score_breakdown=[],
            peers=[],
            fundamentals_history=[],
            price_history=[],
            macro=[],
            assumptions=[],
            data_sources=[],
            warnings=[],
        )

        self.assertEqual(response.ticker, "TEST")


if __name__ == "__main__":
    unittest.main()
