from __future__ import annotations

import os
import sys
import types
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

sqlalchemy_module = types.ModuleType("sqlalchemy")
sqlalchemy_orm_module = types.ModuleType("sqlalchemy.orm")
sqlalchemy_orm_module.Session = object
sqlalchemy_module.orm = sqlalchemy_orm_module
sys.modules.setdefault("sqlalchemy", sqlalchemy_module)
sys.modules.setdefault("sqlalchemy.orm", sqlalchemy_orm_module)

database_module = types.ModuleType("app.core.database")
database_module.SessionLocal = lambda: None
sys.modules.setdefault("app.core.database", database_module)

repository_module = types.ModuleType("app.repositories.analysis_repository")


class _DummyRepository:
    def __init__(self, session: object) -> None:
        self.session = session


repository_module.AnalysisRepository = _DummyRepository
sys.modules.setdefault("app.repositories.analysis_repository", repository_module)

from app.core.scoring import get_scoring_config
from app.schemas.analysis import AnalysisResponse
from app.services.analysis_runtime_service import AnalysisService
from app.services.analysis_safety import classify_company, is_bank_like_company, safe_ratio
from app.services.providers.live_clients import summarize_peer_averages
from app.services.providers.peer_providers import PeerCandidate, PeerDiscoveryResult


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
        self.assertEqual(classify_company(ticker="UBER", sector="Technology", industry="Software - Application")[0], "INTERNET_PLATFORM")
        self.assertEqual(classify_company(ticker="ABNB", sector="Consumer Cyclical", industry="Travel Services")[0], "INTERNET_PLATFORM")
        self.assertEqual(classify_company(ticker="NVDA", sector="Technology", industry="Semiconductors")[0], "SEMICONDUCTORS")
        self.assertEqual(classify_company(ticker="AAPL", sector="Technology", industry="Consumer Electronics")[0], "CONSUMER_HARDWARE_ECOSYSTEM")
        self.assertEqual(classify_company(ticker="MSFT", sector="Technology", industry="Software - Infrastructure")[0], "ENTERPRISE_SOFTWARE")
        self.assertEqual(classify_company(ticker="SBUX", sector="Consumer Cyclical", industry="Restaurants")[0], "RESTAURANTS")
        self.assertEqual(classify_company(ticker="HD", sector="Consumer Cyclical", industry="Home Improvement Retail")[0], "HOME_IMPROVEMENT_RETAIL")
        self.assertEqual(classify_company(ticker="CVX", sector="Energy", industry="Oil & Gas Integrated")[0], "OIL_GAS")
        self.assertEqual(classify_company(ticker="O", sector="Real Estate", industry="Real Estate Investment Trust")[0], "REIT")

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

    def test_missing_fred_does_not_turn_into_zero_macro_bonus(self) -> None:
        weighted_scores = self.service._build_weighted_scores(
            {
                "roe_pct": 20.0,
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
        self.assertEqual(macro_score, 50.0)
        self.assertLess(macro_weight, self.service.scoring_config["weights"]["macro"])

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
        self.assertIn(meta["peer_selection_confidence"], {"medium", "high"})
        self.assertTrue(meta["peer_group_quality_passed"])

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
        self.assertTrue(meta["peer_group_quality_passed"])

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
        self.assertTrue(meta["peer_group_quality_passed"])

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

        self.assertEqual([row["ticker"] for row in selected[:2]], ["DELL", "HPQ"])
        self.assertTrue(meta["peer_group_quality_passed"])

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

        self.assertEqual([row["ticker"] for row in selected], ["LOW"])
        self.assertTrue(meta["peer_group_quality_passed"])
        self.assertEqual(meta["peer_selection_confidence"], "medium")
        self.assertTrue(meta["peer_group_sample_limited"])

        averages = summarize_peer_averages(selected)
        self.assertEqual(averages["pe_ratio"], 19.0)
        self.assertEqual(averages["pb_ratio"], 12.0)

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
                "business_type_confidence": "medium",
            },
            False,
        )

        self.assertIn("Peer comparison based on limited peer set", warnings)
        self.assertIn("Peer averages computed from small sample size", warnings)

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
        self.assertTrue(meta["peer_group_quality_passed"])

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

        self.assertEqual([item["ticker"] for item in peers], ["AMD", "NVDA", "QCOM"])
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
                "peer_selection_source": "config",
                "peer_group_quality_passed": False,
                "business_type_confidence": "low",
            },
            False,
        )

        self.assertIn("Peer baseline used fallback source due to insufficient API peers", warnings)
        self.assertIn("Peer selection used broad fallback because no reliable API peers were found", warnings)
        self.assertIn("No reliable peers found within inferred business type", warnings)
        self.assertIn("Comparative metrics were downgraded due to weak peer relevance", warnings)
        self.assertIn("Broad fallback peers were rejected for scoring use", warnings)

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

        self.assertEqual(weighted_scores["stability"][1], 0.0)
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
        self.assertIn("Bank-specific scoring rules were applied; some generic corporate metrics were excluded", warnings)

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
