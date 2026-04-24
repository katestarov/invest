from __future__ import annotations


def _breakdown_map(response) -> dict[str, object]:
    return {item.key: item for item in response.score_breakdown}


def test_strong_non_bank_company_with_normal_peer_baseline(strong_analysis_service, strong_company_dataset):
    """Моделируется сильная software-компания с 3 usable peers и нормальным valuation baseline."""

    ticker = strong_company_dataset["ticker"]
    yahoo = strong_company_dataset["yahoo_payloads"][ticker]
    edgar = strong_company_dataset["edgar_payloads"][ticker]
    profile = strong_analysis_service._resolve_company_profile(
        ticker=ticker,
        company=edgar["company"],
        sector=edgar["sector"],
        industry=edgar["industry"],
        sic=edgar["sic"],
    )
    peers, peer_selection = strong_analysis_service._build_peer_group(profile, yahoo, edgar)
    peer_averages = strong_analysis_service._build_peer_averages(peers, peer_selection, yahoo, edgar)
    metrics = strong_analysis_service._build_silver_metrics(yahoo, edgar, peer_averages | peer_selection, False)
    response = strong_analysis_service.analyze(ticker)

    breakdown = _breakdown_map(response)
    peer_by_ticker = {peer.ticker: peer for peer in response.peers}

    assert profile["business_type"] in {"SOFTWARE", "ENTERPRISE_SOFTWARE"}
    assert peer_selection["peer_count_usable"] >= 3
    assert metrics["valuation_support_mode"] == "normal"
    assert metrics["valuation_mode_multiplier"] == 1.0
    assert response.score is not None
    assert response.verdict
    assert response.peers
    assert breakdown["valuation"].weight > 0
    assert breakdown["valuation"].score is not None
    assert peer_by_ticker["ORCL"].quality_class == "usable"
    assert peer_by_ticker["CRM"].quality_class == "usable"
    assert peer_by_ticker["ADBE"].quality_class == "usable"
    assert peer_by_ticker["NOW"].quality_class == "weak"
    assert peer_by_ticker["SAP"].quality_class == "excluded"
    assert all(hasattr(peer, "quality_note") for peer in response.peers)


def test_company_with_incomplete_data_survives_and_emits_warnings(incomplete_analysis_service, incomplete_company_dataset):
    """Моделируется компания с неполными фундаментальными данными и консервативной деградацией score."""

    ticker = incomplete_company_dataset["ticker"]
    yahoo = incomplete_company_dataset["yahoo_payloads"][ticker]
    edgar = incomplete_company_dataset["edgar_payloads"][ticker]
    profile = incomplete_analysis_service._resolve_company_profile(
        ticker=ticker,
        company=edgar["company"],
        sector=edgar["sector"],
        industry=edgar["industry"],
        sic=edgar["sic"],
    )
    peers, peer_selection = incomplete_analysis_service._build_peer_group(profile, yahoo, edgar)
    peer_averages = incomplete_analysis_service._build_peer_averages(peers, peer_selection, yahoo, edgar)
    metrics = incomplete_analysis_service._build_silver_metrics(yahoo, edgar, peer_averages | peer_selection, False)
    response = incomplete_analysis_service.analyze(ticker)

    breakdown = _breakdown_map(response)

    assert profile["business_type"] == "INDUSTRIALS"
    assert response.score is not None
    assert response.verdict
    assert response.warnings
    assert metrics["data_completeness_score"] < 100
    assert breakdown["valuation"].weight >= 0
    assert any(card.value is None for card in response.metric_cards)
    assert response.peers
    assert any(peer.quality_class in {"usable", "weak"} for peer in response.peers)


def test_bank_like_company_uses_bank_branch_and_bank_peers(bank_analysis_service, bank_company_dataset):
    """Моделируется bank-like компания, где scoring и peer universe идут по банковской ветке."""

    ticker = bank_company_dataset["ticker"]
    yahoo = bank_company_dataset["yahoo_payloads"][ticker]
    edgar = bank_company_dataset["edgar_payloads"][ticker]
    profile = bank_analysis_service._resolve_company_profile(
        ticker=ticker,
        company=edgar["company"],
        sector=edgar["sector"],
        industry=edgar["industry"],
        sic=edgar["sic"],
    )
    peers, peer_selection = bank_analysis_service._build_peer_group(profile, yahoo, edgar)
    peer_averages = bank_analysis_service._build_peer_averages(peers, peer_selection, yahoo, edgar)
    metrics = bank_analysis_service._build_silver_metrics(yahoo, edgar, peer_averages | peer_selection, True)
    response = bank_analysis_service.analyze(ticker)

    breakdown = _breakdown_map(response)

    assert profile["business_type"] == "BANK"
    assert metrics["is_bank_like"] is True
    assert response.score is not None
    assert response.verdict
    assert response.peers
    assert {peer.ticker for peer in response.peers[:3]} <= {"JPM", "BAC", "USB"}
    assert breakdown["profitability"].weight > 0
    assert breakdown["stability"].weight > 0
    assert breakdown["valuation"].weight >= 0
    assert any(peer.included_in_baseline for peer in response.peers)


def test_peer_group_degradation_uses_fallback_baseline_with_reduced_valuation_weight(
    fallback_analysis_service,
    fallback_baseline_dataset,
):
    """Моделируется деградация peer-group: 1 usable + 2 weak, reduced-weight valuation и не-доминантные weak peers."""

    ticker = fallback_baseline_dataset["ticker"]
    yahoo = fallback_baseline_dataset["yahoo_payloads"][ticker]
    edgar = fallback_baseline_dataset["edgar_payloads"][ticker]
    profile = fallback_analysis_service._resolve_company_profile(
        ticker=ticker,
        company=edgar["company"],
        sector=edgar["sector"],
        industry=edgar["industry"],
        sic=edgar["sic"],
    )
    peers, peer_selection = fallback_analysis_service._build_peer_group(profile, yahoo, edgar)
    peer_averages = fallback_analysis_service._build_peer_averages(peers, peer_selection, yahoo, edgar)
    metrics = fallback_analysis_service._build_silver_metrics(yahoo, edgar, peer_averages | peer_selection, False)
    response = fallback_analysis_service.analyze(ticker)

    breakdown = _breakdown_map(response)
    baseline_peers = [peer for peer in response.peers if peer.included_in_baseline]
    usable_weight = sum((peer.baseline_weight or 0.0) for peer in baseline_peers if peer.quality_class == "usable")
    weak_weight = sum((peer.baseline_weight or 0.0) for peer in baseline_peers if peer.quality_class == "weak")
    peer_by_ticker = {peer.ticker: peer for peer in response.peers}

    assert profile["business_type"] == "AUTO_MANUFACTURER"
    assert peer_selection["peer_count_usable"] >= 1
    assert peer_selection["peer_count_weak"] >= 2
    assert metrics["valuation_support_mode"] in {"fallback_low_confidence", "weak_only_fallback"}
    assert metrics["valuation_mode_multiplier"] < 1.0
    assert response.score is not None
    assert response.verdict
    assert breakdown["valuation"].weight > 0
    assert weak_weight <= usable_weight
    assert peer_by_ticker["GM"].quality_class == "usable"
    assert peer_by_ticker["F"].quality_class == "weak"
    assert peer_by_ticker["RIVN"].quality_class == "weak"
    assert peer_by_ticker["LCID"].quality_class == "excluded"
    assert any("Fallback baseline" in warning or "Weak-only fallback" in warning for warning in response.warnings)
