from __future__ import annotations

import logging

import httpx
import pytest

from app.core.logging_config import configure_logging
from app.services.providers.live_clients import BaseHttpProvider, YahooFinanceProvider


def test_configure_logging_raises_httpx_log_threshold():
    configure_logging("INFO")

    assert logging.getLogger("httpx").level == logging.WARNING
    assert logging.getLogger("httpcore").level == logging.WARNING


def test_http_provider_can_suppress_failure_log(caplog):
    provider = BaseHttpProvider()

    class _FailingClient:
        def get(self, url: str, **kwargs):
            request = httpx.Request("GET", url)
            raise httpx.HTTPStatusError("unauthorized", request=request, response=httpx.Response(401, request=request))

        def close(self) -> None:
            return None

    provider.client = _FailingClient()
    provider._suppress_request_failure_log = True

    with caplog.at_level(logging.WARNING):
        with pytest.raises(httpx.HTTPStatusError):
            provider._get_json("https://query1.finance.yahoo.com/v7/finance/quote")

    assert not any(record.message == "http_provider_request_failed" for record in caplog.records)


def test_yahoo_optional_quote_snapshot_failure_stays_quiet(caplog):
    provider = YahooFinanceProvider.__new__(YahooFinanceProvider)
    chart_payload = {
        "chart": {
            "result": [
                {
                    "meta": {"longName": "Test Corp", "currency": "USD", "regularMarketPrice": 120.0, "previousClose": 119.0},
                    "timestamp": [1, 2],
                    "indicators": {"quote": [{"close": [100.0, 120.0]}]},
                }
            ]
        }
    }

    def fake_get_json(url: str, params=None, headers=None):
        if "chart" in url:
            return chart_payload
        request = httpx.Request("GET", url)
        raise httpx.HTTPStatusError("unauthorized", request=request, response=httpx.Response(401, request=request))

    provider._get_json = fake_get_json
    provider._status_code = lambda exc: 401
    provider._suppress_request_failure_log = False

    with caplog.at_level(logging.WARNING):
        payload = provider.fetch_company_bundle("TEST").payload

    assert payload["ticker"] == "TEST"
    assert not any(record.message == "yahoo_quote_snapshot_unavailable" for record in caplog.records)
