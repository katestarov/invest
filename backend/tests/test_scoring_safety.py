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
from app.services.analysis_safety import is_bank_like_company, safe_ratio
from app.services.providers.live_clients import summarize_peer_averages


class ScoringSafetyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = AnalysisService.__new__(AnalysisService)
        self.service.scoring_config = get_scoring_config()

    def test_safe_ratio_rejects_non_interpretable_denominator(self) -> None:
        self.assertIsNone(safe_ratio(10.0, 0.0))
        self.assertIsNone(safe_ratio(10.0, -2.0))
        self.assertEqual(safe_ratio(10.0, 2.0), 5.0)

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