from __future__ import annotations

import math

import pytest

from app.services.analysis_runtime_service import AnalysisService
from app.services.analysis_safety import (
    apply_low_confidence_cap,
    coverage_ratio,
    normalize_weights,
    premium_pct,
    safe_ratio,
    score_inverse,
    score_positive,
    score_relative_valuation,
)
from app.services.providers.live_clients import FredProvider, SecEdgarProvider, YahooFinanceProvider


@pytest.mark.parametrize(
    ("numerator", "denominator", "allow_negative_denominator", "expected"),
    [
        (10.0, 2.0, False, 5.0),
        (10.0, 0.0, False, None),
        (10.0, -2.0, False, None),
        (10.0, -2.0, True, -5.0),
        (None, 2.0, False, None),
    ],
)
def test_safe_ratio_cases(numerator, denominator, allow_negative_denominator, expected):
    assert safe_ratio(numerator, denominator, allow_negative_denominator=allow_negative_denominator) == expected


@pytest.mark.parametrize(
    ("value", "cap", "expected"),
    [
        (25.0, 50.0, 50.0),
        (-10.0, 50.0, 0.0),
        (75.0, 50.0, 100.0),
        (None, 50.0, None),
    ],
)
def test_score_positive(value, cap, expected):
    assert score_positive(value, cap) == expected


@pytest.mark.parametrize(
    ("value", "cap", "expected"),
    [
        (0.5, 2.5, 80.0),
        (2.5, 2.5, 0.0),
        (5.0, 2.5, 0.0),
        (None, 2.5, None),
    ],
)
def test_score_inverse(value, cap, expected):
    assert score_inverse(value, cap) == expected


def test_premium_pct_and_relative_valuation_scoring():
    assert premium_pct(12.0, 10.0) == pytest.approx(20.0)
    assert premium_pct(8.0, 10.0) == pytest.approx(-20.0)
    assert score_relative_valuation(-20.0, 100.0) == pytest.approx(68.0)
    assert score_relative_valuation(20.0, 100.0) == pytest.approx(48.0)
    assert score_relative_valuation(None, 100.0) is None


def test_coverage_ratio_and_weight_renormalization():
    components = [(80.0, 0.4), (None, 0.6)]
    assert coverage_ratio(components) == pytest.approx(0.4)

    normalized = normalize_weights(
        {
            "profitability": 80.0,
            "stability": 60.0,
            "valuation": None,
            "growth": 70.0,
        },
        {
            "profitability": 0.27,
            "stability": 0.19,
            "valuation": 0.20,
            "growth": 0.19,
        },
    )
    assert normalized["valuation"] == 0.0
    assert sum(normalized.values()) == pytest.approx(1.0)


def test_low_confidence_cap_limits_partial_score():
    assert apply_low_confidence_cap(90.0, 0.4) == pytest.approx(80.0)
    assert apply_low_confidence_cap(90.0, 0.6) == pytest.approx(90.0)
    assert apply_low_confidence_cap(None, 0.4) is None


def test_market_cap_diagnostics_uses_median_of_valid_sources(regular_analysis_service):
    diagnostics = regular_analysis_service._market_cap_diagnostics(
        {
            "current_price": 100.0,
            "currency": "USD",
            "market_cap_bln_quote": 150.0,
            "market_cap_quote_currency": "USD",
            "shares_outstanding_quote_mln": 1500.0,
            "ticker": "TEST",
        },
        {"shares_outstanding_mln": 1400.0},
        supplemental_market_caps=[("fmp_market_cap", 152.0, "USD"), ("finnhub_market_cap", 149.0, "USD")],
    )

    assert diagnostics["market_cap_bln"] == pytest.approx(150.0)
    assert diagnostics["source"] == "median_of_sources"
    assert diagnostics["status"] == "valid"


def test_market_cap_diagnostics_marks_large_source_disagreement_as_suspect(regular_analysis_service):
    diagnostics = regular_analysis_service._market_cap_diagnostics(
        {
            "current_price": 100.0,
            "currency": "USD",
            "market_cap_bln_quote": 150.0,
            "market_cap_quote_currency": "USD",
            "shares_outstanding_quote_mln": 1500.0,
            "ticker": "TEST",
        },
        {"shares_outstanding_mln": 1500.0},
        supplemental_market_caps=[("fmp_market_cap", 620.0, "USD"), ("finnhub_market_cap", 149.0, "USD")],
    )

    assert diagnostics["status"] == "suspect"
    assert diagnostics["suspect"] is True
    assert "disagree materially" in str(diagnostics["warning"])


def test_market_cap_diagnostics_rejects_out_of_range_values(regular_analysis_service):
    diagnostics = regular_analysis_service._market_cap_diagnostics(
        {
            "current_price": 9000.0,
            "currency": "USD",
            "market_cap_bln_quote": 9001.0,
            "market_cap_quote_currency": "USD",
            "shares_outstanding_quote_mln": 1000.0,
            "ticker": "TEST",
        },
        {"shares_outstanding_mln": 1000.0},
    )

    assert diagnostics["market_cap_bln"] is None
    assert diagnostics["status"] == "invalid"
    assert "sanity checks" in str(diagnostics["warning"])


def test_pe_pb_and_roe_from_market_cap_and_fundamentals(regular_analysis_service):
    yahoo = {
        "ticker": "TEST",
        "current_price": 50.0,
        "currency": "USD",
        "market_cap_bln_quote": 120.0,
        "market_cap_quote_currency": "USD",
        "shares_outstanding_quote_mln": 2400.0,
    }
    edgar = {
        "net_income_bln": 10.0,
        "equity_bln": 30.0,
        "shares_outstanding_mln": 2400.0,
    }

    assert regular_analysis_service._pe_ratio(yahoo, edgar) == pytest.approx(12.0)
    assert regular_analysis_service._pb_ratio(yahoo, edgar) == pytest.approx(4.0)
    assert regular_analysis_service._roe_pct(edgar) == pytest.approx(33.33, rel=1e-3)


def test_revenue_growth_and_cagr_like_handle_period_mismatch(regular_analysis_service):
    aligned = {
        "revenue_bln": [121.0, 110.0, 100.0],
        "revenue_periods": [
            {"period_type": "annual", "fiscal_period": "FY"},
            {"period_type": "annual", "fiscal_period": "FY"},
            {"period_type": "annual", "fiscal_period": "FY"},
        ],
    }
    mismatched = {
        "revenue_bln": [121.0, 110.0],
        "revenue_periods": [
            {"period_type": "annual", "fiscal_period": "FY"},
            {"period_type": "quarterly", "fiscal_period": "Q3"},
        ],
    }

    assert regular_analysis_service._revenue_growth_pct(aligned) == pytest.approx(10.0)
    assert regular_analysis_service._revenue_cagr_like_pct(aligned) == pytest.approx(10.0)
    assert regular_analysis_service._revenue_growth_pct(mismatched) is None


def test_roe_assessment_downgrades_small_equity_for_non_bank(regular_analysis_service):
    assessment = regular_analysis_service._roe_assessment(
        {"net_income_bln": 12.0, "equity_bln": 8.0, "roic_pct": 19.0},
        320.0,
        is_bank_like=False,
    )

    assert assessment["display_pct"] == pytest.approx(150.0)
    assert assessment["reliable"] is False
    assert assessment["score_input_pct"] == pytest.approx(19.0)
    assert assessment["weight_multiplier"] == pytest.approx(0.9)


def test_build_weighted_scores_uses_bank_branch(bank_analysis_service):
    metrics = {
        "roe_score_pct": 18.0,
        "roe_pct": 18.0,
        "roic_pct": None,
        "ebit_margin_pct": None,
        "revenue_growth_pct": 9.0,
        "revenue_cagr_like_pct": 8.0,
        "fcf_margin_pct": None,
        "debt_to_equity": None,
        "current_ratio": None,
        "one_year_return_pct": 12.0,
        "five_year_return_pct": 45.0,
        "pe_premium_pct": -10.0,
        "pb_premium_pct": -5.0,
        "valuation_enabled": True,
        "peer_confidence_multiplier": 1.0,
        "valuation_mode_multiplier": 1.0,
        "profitability_reliability_multiplier": 1.0,
        "is_bank_like": True,
    }

    weighted = bank_analysis_service._build_weighted_scores(metrics, {"fed_funds_rate_pct": 4.0, "inflation_pct": 2.5, "unemployment_pct": 4.0, "gdp_growth_pct": 2.0})

    assert weighted["profitability"][0] == pytest.approx(72.0)
    assert weighted["stability"][0] == pytest.approx(68.25)
    assert weighted["valuation"][0] is not None


def test_yahoo_provider_computes_one_and_five_year_returns():
    provider = YahooFinanceProvider.__new__(YahooFinanceProvider)
    closes = [100.0, 105.0, 110.0, 115.0, 120.0, 125.0, 130.0, 135.0, 140.0, 145.0, 150.0, 155.0, 160.0, 165.0, 170.0, 175.0, 180.0, 185.0, 190.0, 195.0, 200.0, 205.0, 210.0, 220.0]
    chart_payload = {
        "chart": {
            "result": [
                {
                    "meta": {"longName": "Test Corp", "currency": "USD", "regularMarketPrice": 220.0, "previousClose": 210.0},
                    "timestamp": list(range(1, len(closes) + 1)),
                    "indicators": {"quote": [{"close": closes}]},
                }
            ]
        }
    }
    quote_payload = {
        "quoteResponse": {
            "result": [{"currency": "USD", "marketCap": 220_000_000_000, "sharesOutstanding": 1_000_000_000, "quoteType": "EQUITY"}]
        }
    }

    provider._get_json = lambda url, params=None, headers=None: chart_payload if "chart" in url else quote_payload

    payload = provider.fetch_company_bundle("TEST").payload

    assert payload["one_year_return_pct"] == pytest.approx(37.5)
    assert payload["five_year_return_pct"] == pytest.approx(120.0)
    assert payload["market_cap_bln_quote"] == pytest.approx(220.0)


def test_sec_provider_computes_core_financial_metrics(sec_facts_payload, sec_submissions_payload):
    provider = SecEdgarProvider.__new__(SecEdgarProvider)
    provider._ticker_to_cik = lambda ticker: "0000000001"
    provider._get_json = lambda url, params=None, headers=None: sec_facts_payload if "companyfacts" in url else sec_submissions_payload

    result = provider.fetch_company_bundle("TEST").payload

    assert result["current_ratio"] == pytest.approx(1.5)
    assert result["debt_to_equity"] == pytest.approx(0.5)
    assert result["roic_pct"] == pytest.approx(24.0)
    assert result["ebit_margin_pct"] == pytest.approx(20.0)
    assert result["fcf_margin_pct"] == pytest.approx(15.0)
    assert result["net_income_bln"] == pytest.approx(16.0)
    assert result["shares_outstanding_mln"] == pytest.approx(1200.0)


def test_sec_provider_estimates_tax_rate_when_tax_facts_are_missing():
    provider = SecEdgarProvider.__new__(SecEdgarProvider)
    provider._ticker_to_cik = lambda ticker: "0000000002"
    facts = {
        "entityName": "Estimate Tax Corp",
        "facts": {
            "us-gaap": {
                "Revenues": {"units": {"USD": [{"fy": 2024, "end": "2024-12-31", "form": "10-K", "fp": "FY", "val": 80_000_000_000}]}},
                "StockholdersEquity": {"units": {"USD": [{"end": "2024-12-31", "form": "10-K", "val": 40_000_000_000}]}},
                "LongTermDebt": {"units": {"USD": [{"end": "2024-12-31", "form": "10-K", "val": 20_000_000_000}]}},
                "LongTermDebtCurrent": {"units": {"USD": [{"end": "2024-12-31", "form": "10-K", "val": 5_000_000_000}]}},
                "CashAndCashEquivalentsAtCarryingValue": {"units": {"USD": [{"end": "2024-12-31", "form": "10-K", "val": 5_000_000_000}]}},
                "OperatingIncomeLoss": {"units": {"USD": [{"fy": 2024, "end": "2024-12-31", "form": "10-K", "fp": "FY", "val": 20_000_000_000}]}},
                "NetIncomeLoss": {"units": {"USD": [{"fy": 2024, "end": "2024-12-31", "form": "10-K", "fp": "FY", "val": 14_000_000_000}]}},
                "Assets": {"units": {"USD": [{"end": "2024-12-31", "form": "10-K", "val": 100_000_000_000}]}},
            },
            "dei": {"EntityCommonStockSharesOutstanding": {"units": {"shares": [{"end": "2024-12-31", "form": "10-K", "val": 1_000_000_000}]}}},
        },
    }
    submissions = {"name": "Estimate Tax Corp", "sic": "7372", "sicDescription": "Software"}
    provider._get_json = lambda url, params=None, headers=None: facts if "companyfacts" in url else submissions

    result = provider.fetch_company_bundle("TEST")

    assert result.payload["roic_pct"] == pytest.approx(26.33, rel=1e-3)
    assert any("estimated tax rate" in warning for warning in result.warnings)


def test_fred_provider_uses_year_over_year_inflation(fred_payloads):
    provider = FredProvider.__new__(FredProvider)
    provider.api_key = "test"
    provider._get_json = lambda url, params=None, headers=None: fred_payloads[(params or {})["series_id"]]

    payload = provider.fetch_macro_bundle().payload

    assert payload["fed_funds_rate_pct"] == pytest.approx(4.33)
    assert payload["unemployment_pct"] == pytest.approx(4.0)
    assert payload["inflation_pct"] == pytest.approx(3.28, rel=1e-3)
