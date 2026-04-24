# Non-functional Diagnostics Summary

This diagnostic harness is not a production benchmark. It is a reproducible synthetic setup for comparing normal, cached, and degraded execution paths on the same analysis logic.

## Benchmark scenarios
- `uncached_analysis`: mean `0.1602` sec, min `0.1523`, max `0.1659`, cache `False`, degraded `False`, fallback `False`
- `cached_analysis`: mean `0.0` sec, min `0.0`, max `0.0001`, cache `True`, degraded `False`, fallback `False`
- `degraded_peer_provider`: mean `0.1544` sec, min `0.1513`, max `0.1581`, cache `False`, degraded `True`, fallback `True`
- `fallback_baseline`: mean `0.133` sec, min `0.13`, max `0.1396`, cache `False`, degraded `True`, fallback `False`

## Retry probe
- observed attempts: `2`
- configured max attempts: `2`
- timeout sec: `20.0`

## Final metrics table
- `analysis_response_time_sec`: `0.1602` sec - Mean response time for a full uncached analysis on the synthetic harness.
- `cached_response_time_sec`: `0.0` sec - Mean response time when the analysis cache path is hit.
- `degraded_response_time_sec`: `0.1544` sec - Mean response time when one peer provider fails and the system continues in degraded mode.
- `provider_retry_count`: `2` attempts - Observed retry attempts in the probe; the ceiling comes from configuration.
- `provider_timeout_sec`: `20.0` sec - Configured timeout for HTTP providers.
- `cache_ttl_sec`: `analysis=900, provider=1800` sec - TTL values for analysis cache and provider cache.
- `analysis_cache_key`: `normalized_ticker` key - Analysis cache is keyed by normalized ticker.
- `peer_group_cache_key`: `ticker|sector|industry|sic` key - Peer group cache is keyed by the normalized company profile.
- `provider_cache_key`: `url|params` key - Base HTTP provider caches by request URL and query params.
- `fallback_behavior`: `peer / low_confidence / fallback_low_confidence / weak_only_fallback / disabled` mode - Fallback mode is driven by peer support quality instead of a fixed hard-off rule.