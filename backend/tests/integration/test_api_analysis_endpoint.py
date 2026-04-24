from __future__ import annotations


def test_analysis_endpoint_works_with_injected_mock_service(api_client_factory, regular_analysis_service):
    client = api_client_factory(regular_analysis_service)

    response = client.get("/api/v1/analyze/ACME")

    assert response.status_code == 200
    payload = response.json()
    assert payload["ticker"] == "ACME"
    assert payload["company"] == "Acme Cloud"
    assert payload["peers"]
    assert any(peer["quality_class"] == "weak" for peer in payload["peers"])


def test_cache_clear_endpoint_uses_dependency_override(api_client_factory, regular_analysis_service):
    client = api_client_factory(regular_analysis_service)

    response = client.post("/api/v1/cache/clear")

    assert response.status_code == 200
    assert response.json() == {"status": "cache cleared"}
