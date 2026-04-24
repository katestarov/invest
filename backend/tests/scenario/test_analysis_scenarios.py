from __future__ import annotations


def test_regular_company_scenario_exposes_usable_weak_and_excluded_peers(
    regular_analysis_service,
    regular_company_dataset,
    recording_persistence,
):
    response = regular_analysis_service.analyze(regular_company_dataset["ticker"])

    peer_by_ticker = {peer.ticker: peer for peer in response.peers}
    assert peer_by_ticker["ORCL"].quality_class == "usable"
    assert peer_by_ticker["CRM"].quality_class == "usable"
    assert peer_by_ticker["NOW"].quality_class == "weak"
    assert peer_by_ticker["SAP"].quality_class == "excluded"
    assert peer_by_ticker["NOW"].included_in_baseline is True
    assert peer_by_ticker["SAP"].included_in_baseline is False
    assert any("Limited data" in str(peer_by_ticker[ticker].quality_note) for ticker in ("NOW",))
    assert any("Excluded from baseline" in str(peer_by_ticker[ticker].quality_note) for ticker in ("SAP",))
    assert recording_persistence.sessions
    assert recording_persistence.sessions[-1].committed is True


def test_bank_like_company_scenario_runs_bank_branch(bank_analysis_service, bank_company_dataset):
    response = bank_analysis_service.analyze(bank_company_dataset["ticker"])

    assert response.ticker == "BNK1"
    assert response.sector == "Financial Services"
    assert response.score_breakdown
    valuation_item = next(item for item in response.score_breakdown if item.key == "valuation")
    profitability_item = next(item for item in response.score_breakdown if item.key == "profitability")
    assert profitability_item.weight > 0
    assert valuation_item.weight >= 0
    assert {peer.ticker for peer in response.peers[:3]} <= {"JPM", "BAC", "USB"}


def test_incomplete_company_scenario_survives_missing_data(incomplete_analysis_service, incomplete_company_dataset):
    response = incomplete_analysis_service.analyze(incomplete_company_dataset["ticker"])

    assert response.ticker == "GAPS"
    assert response.score >= 0
    assert response.warnings
    assert any(item.score is None or item.weight >= 0 for item in response.score_breakdown)
    assert any(peer.quality_class in {"usable", "weak"} for peer in response.peers)
