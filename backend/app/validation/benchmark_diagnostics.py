from __future__ import annotations

from dataclasses import dataclass
import logging
import statistics
import time
from typing import Any

import httpx

from app.core.settings import get_settings
from app.services.providers.live_clients import BaseHttpProvider
from app.validation.common import (
    build_mock_analysis_service,
    scenario_registry,
    validation_output_dir,
    write_bar_chart_svg,
    write_csv,
    write_json,
)


class DelayedStaticPeerProvider:
    def __init__(
        self,
        *args,
        delay_sec: float = 0.0,
        discover_error: Exception | None = None,
        market_cap_error: Exception | None = None,
        **kwargs,
    ) -> None:
        from tests.support.fakes import StaticPeerProvider

        self.provider = StaticPeerProvider(*args, **kwargs)
        self.delay_sec = delay_sec
        self.discover_error = discover_error
        self.market_cap_error = market_cap_error
        self.source_name = getattr(self.provider, "source_name", "static")

    def discover(self, ticker: str, company_profile: dict):
        time.sleep(self.delay_sec)
        if self.discover_error is not None:
            raise self.discover_error
        return self.provider.discover(ticker, company_profile)

    def fetch_market_cap_snapshot(self, ticker: str):
        time.sleep(self.delay_sec)
        if self.market_cap_error is not None:
            raise self.market_cap_error
        return self.provider.fetch_market_cap_snapshot(ticker)


@dataclass
class BenchmarkRow:
    scenario: str
    mean_response_time_sec: float
    min_response_time_sec: float
    max_response_time_sec: float
    used_cache: bool
    degraded_mode: bool
    fallback_used: bool


def _measure_run(callable_obj) -> float:
    started = time.perf_counter()
    callable_obj()
    return time.perf_counter() - started


def _strong_dataset() -> dict:
    return scenario_registry()["strong_non_bank"]


def _fallback_dataset() -> dict:
    return scenario_registry()["fallback_baseline"]


def _service_for_benchmark(dataset: dict, *, degraded_peer: bool = False, market_cap_failure: bool = False):
    market_cap_snapshots = dataset.get("market_cap_snapshots", {})
    peer_discovery = dataset.get("peer_discovery", {})
    return build_mock_analysis_service(
        dataset,
        peer_providers=[
            DelayedStaticPeerProvider(
                source_name="fmp",
                discovery_map=peer_discovery.get("fmp", {}),
                market_cap_snapshots={
                    ticker: snapshots.get("fmp")
                    for ticker, snapshots in market_cap_snapshots.items()
                    if snapshots.get("fmp")
                },
                delay_sec=0.005,
                discover_error=httpx.ReadTimeout("peer provider timeout") if degraded_peer else None,
                market_cap_error=httpx.ReadTimeout("market cap timeout") if market_cap_failure else None,
            ),
            DelayedStaticPeerProvider(
                source_name="finnhub",
                discovery_map=peer_discovery.get("finnhub", {}),
                market_cap_snapshots={
                    ticker: snapshots.get("finnhub")
                    for ticker, snapshots in market_cap_snapshots.items()
                    if snapshots.get("finnhub")
                },
                delay_sec=0.005,
            ),
            DelayedStaticPeerProvider(
                source_name="business_type",
                discovery_map=peer_discovery.get("business_type", {}),
                delay_sec=0.001,
            ),
            DelayedStaticPeerProvider(
                source_name="config",
                discovery_map=peer_discovery.get("config", {}),
                delay_sec=0.001,
            ),
        ],
    )


def benchmark_analysis_scenarios(repeats: int = 5) -> list[BenchmarkRow]:
    rows: list[BenchmarkRow] = []

    uncached_times: list[float] = []
    for _ in range(repeats):
        service = _service_for_benchmark(_strong_dataset())
        uncached_times.append(_measure_run(lambda: service.analyze("ACME")))
    uncached_response = _service_for_benchmark(_strong_dataset()).analyze("ACME")
    rows.append(
        BenchmarkRow(
            scenario="uncached_analysis",
            mean_response_time_sec=round(statistics.mean(uncached_times), 4),
            min_response_time_sec=round(min(uncached_times), 4),
            max_response_time_sec=round(max(uncached_times), 4),
            used_cache=False,
            degraded_mode=False,
            fallback_used=any("fallback" in warning.lower() for warning in uncached_response.warnings),
        )
    )

    cached_times: list[float] = []
    for _ in range(repeats):
        service = _service_for_benchmark(_strong_dataset())
        service.analyze("ACME")
        cached_times.append(_measure_run(lambda: service.analyze("ACME")))
    cached_service = _service_for_benchmark(_strong_dataset())
    cached_service.analyze("ACME")
    cached_response = cached_service.analyze("ACME")
    rows.append(
        BenchmarkRow(
            scenario="cached_analysis",
            mean_response_time_sec=round(statistics.mean(cached_times), 4),
            min_response_time_sec=round(min(cached_times), 4),
            max_response_time_sec=round(max(cached_times), 4),
            used_cache=True,
            degraded_mode=False,
            fallback_used=any("fallback" in warning.lower() for warning in cached_response.warnings),
        )
    )

    degraded_times: list[float] = []
    degraded_responses = []
    for _ in range(repeats):
        service = _service_for_benchmark(_strong_dataset(), degraded_peer=True)
        degraded_times.append(_measure_run(lambda: degraded_responses.append(service.analyze("ACME"))))
    degraded_response = degraded_responses[-1]
    rows.append(
        BenchmarkRow(
            scenario="degraded_peer_provider",
            mean_response_time_sec=round(statistics.mean(degraded_times), 4),
            min_response_time_sec=round(min(degraded_times), 4),
            max_response_time_sec=round(max(degraded_times), 4),
            used_cache=False,
            degraded_mode=True,
            fallback_used=any(
                "peer low confidence" in warning.lower() or "fallback" in warning.lower()
                for warning in degraded_response.warnings
            ),
        )
    )

    fallback_times: list[float] = []
    fallback_responses = []
    for _ in range(repeats):
        service = _service_for_benchmark(_fallback_dataset(), market_cap_failure=True)
        fallback_times.append(_measure_run(lambda: fallback_responses.append(service.analyze("AUTOX"))))
    fallback_response = fallback_responses[-1]
    rows.append(
        BenchmarkRow(
            scenario="fallback_baseline",
            mean_response_time_sec=round(statistics.mean(fallback_times), 4),
            min_response_time_sec=round(min(fallback_times), 4),
            max_response_time_sec=round(max(fallback_times), 4),
            used_cache=False,
            degraded_mode=True,
            fallback_used=any("fallback" in warning.lower() for warning in fallback_response.warnings),
        )
    )

    return rows


def provider_retry_probe() -> dict[str, Any]:
    provider = BaseHttpProvider()
    configured_attempts = provider.max_attempts

    class ProbeClient:
        def __init__(self) -> None:
            self.calls = 0

        def get(self, url: str, **kwargs):
            self.calls += 1
            if self.calls == 1:
                raise httpx.ReadTimeout("timeout")
            if configured_attempts >= 3 and self.calls == 2:
                request = httpx.Request("GET", url)
                raise httpx.HTTPStatusError(
                    "bad gateway",
                    request=request,
                    response=httpx.Response(502, request=request),
                )
            return type(
                "_Response",
                (),
                {
                    "raise_for_status": staticmethod(lambda: None),
                    "json": staticmethod(lambda: {"ok": True}),
                },
            )()

        def close(self) -> None:
            return None

    provider.client = ProbeClient()
    started = time.perf_counter()
    payload = provider._get_json("https://example.com/retry-probe")
    elapsed = time.perf_counter() - started
    return {
        "payload": payload,
        "provider_retry_count": provider.client.calls,
        "provider_timeout_sec": provider.timeout,
        "provider_retry_attempts_configured": configured_attempts,
        "probe_elapsed_sec": round(elapsed, 4),
    }


def build_system_summary(rows: list[BenchmarkRow], retry_probe: dict[str, Any]) -> list[dict[str, Any]]:
    settings = get_settings()
    by_name = {row.scenario: row for row in rows}
    return [
        {
            "metric": "analysis_response_time_sec",
            "unit": "sec",
            "current_value_or_range": by_name["uncached_analysis"].mean_response_time_sec,
            "comment": "Mean response time for a full uncached analysis on the synthetic harness.",
        },
        {
            "metric": "cached_response_time_sec",
            "unit": "sec",
            "current_value_or_range": by_name["cached_analysis"].mean_response_time_sec,
            "comment": "Mean response time when the analysis cache path is hit.",
        },
        {
            "metric": "degraded_response_time_sec",
            "unit": "sec",
            "current_value_or_range": by_name["degraded_peer_provider"].mean_response_time_sec,
            "comment": "Mean response time when one peer provider fails and the system continues in degraded mode.",
        },
        {
            "metric": "provider_retry_count",
            "unit": "attempts",
            "current_value_or_range": retry_probe["provider_retry_count"],
            "comment": "Observed retry attempts in the probe; the ceiling comes from configuration.",
        },
        {
            "metric": "provider_timeout_sec",
            "unit": "sec",
            "current_value_or_range": retry_probe["provider_timeout_sec"],
            "comment": "Configured timeout for HTTP providers.",
        },
        {
            "metric": "cache_ttl_sec",
            "unit": "sec",
            "current_value_or_range": f"analysis={settings.analysis_cache_ttl_seconds}, provider={settings.provider_cache_ttl_seconds}",
            "comment": "TTL values for analysis cache and provider cache.",
        },
        {
            "metric": "analysis_cache_key",
            "unit": "key",
            "current_value_or_range": "normalized_ticker",
            "comment": "Analysis cache is keyed by normalized ticker.",
        },
        {
            "metric": "peer_group_cache_key",
            "unit": "key",
            "current_value_or_range": "ticker|sector|industry|sic",
            "comment": "Peer group cache is keyed by the normalized company profile.",
        },
        {
            "metric": "provider_cache_key",
            "unit": "key",
            "current_value_or_range": "url|params",
            "comment": "Base HTTP provider caches by request URL and query params.",
        },
        {
            "metric": "fallback_behavior",
            "unit": "mode",
            "current_value_or_range": "peer / low_confidence / fallback_low_confidence / weak_only_fallback / disabled",
            "comment": "Fallback mode is driven by peer support quality instead of a fixed hard-off rule.",
        },
    ]


def build_summary_markdown(
    output_dir,
    rows: list[BenchmarkRow],
    retry_probe: dict[str, Any],
    system_table: list[dict[str, Any]],
) -> None:
    lines = [
        "# Non-functional Diagnostics Summary",
        "",
        "This diagnostic harness is not a production benchmark. It is a reproducible synthetic setup for comparing normal, cached, and degraded execution paths on the same analysis logic.",
        "",
        "## Benchmark scenarios",
    ]
    for row in rows:
        lines.append(
            f"- `{row.scenario}`: mean `{row.mean_response_time_sec}` sec, min `{row.min_response_time_sec}`, max `{row.max_response_time_sec}`, cache `{row.used_cache}`, degraded `{row.degraded_mode}`, fallback `{row.fallback_used}`"
        )
    lines.extend(
        [
            "",
            "## Retry probe",
            f"- observed attempts: `{retry_probe['provider_retry_count']}`",
            f"- configured max attempts: `{retry_probe['provider_retry_attempts_configured']}`",
            f"- timeout sec: `{retry_probe['provider_timeout_sec']}`",
            "",
            "## Final metrics table",
        ]
    )
    for row in system_table:
        lines.append(f"- `{row['metric']}`: `{row['current_value_or_range']}` {row['unit']} - {row['comment']}")
    (output_dir / "summary.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    quiet_loggers = [
        logging.getLogger("app.services.analysis_runtime_service"),
        logging.getLogger("app.services.providers.live_clients"),
    ]
    previous_levels = [logger.level for logger in quiet_loggers]
    for logger in quiet_loggers:
        logger.setLevel(logging.ERROR)

    output_dir = validation_output_dir("benchmark_diagnostics")
    try:
        rows = benchmark_analysis_scenarios()
        retry_probe = provider_retry_probe()
        system_table = build_system_summary(rows, retry_probe)

        write_csv(output_dir / "benchmark_scenarios.csv", [row.__dict__ for row in rows])
        write_csv(output_dir / "system_metrics_table.csv", system_table)
        write_json(
            output_dir / "benchmark_summary.json",
            {
                "benchmark_rows": [row.__dict__ for row in rows],
                "retry_probe": retry_probe,
                "system_table": system_table,
            },
        )
        write_bar_chart_svg(
            output_dir / "response_times.svg",
            "Mean Response Time by Scenario",
            [(row.scenario, row.mean_response_time_sec) for row in rows],
            color="#0f766e",
        )
        write_bar_chart_svg(
            output_dir / "cache_benefit.svg",
            "Cache Benefit: Uncached vs Cached",
            [
                ("uncached", next(row.mean_response_time_sec for row in rows if row.scenario == "uncached_analysis")),
                ("cached", next(row.mean_response_time_sec for row in rows if row.scenario == "cached_analysis")),
            ],
            color="#1d4ed8",
        )
        build_summary_markdown(output_dir, rows, retry_probe, system_table)
    finally:
        for logger, previous_level in zip(quiet_loggers, previous_levels):
            logger.setLevel(previous_level)


if __name__ == "__main__":
    main()
