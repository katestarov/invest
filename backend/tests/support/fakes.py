from __future__ import annotations

from copy import deepcopy

from app.services.providers.live_clients import ProviderResult
from app.services.providers.peer_providers import PeerCandidate, PeerDiscoveryResult


class StaticCompanyBundleProvider:
    def __init__(
        self,
        bundles_by_ticker: dict[str, dict],
        *,
        source_name: str,
        warnings_by_ticker: dict[str, list[str]] | None = None,
    ) -> None:
        self.source_name = source_name
        self._bundles_by_ticker = {str(key).upper(): deepcopy(value) for key, value in bundles_by_ticker.items()}
        self._warnings_by_ticker = {str(key).upper(): list(value) for key, value in (warnings_by_ticker or {}).items()}
        self.calls: list[str] = []

    def fetch_company_bundle(self, ticker: str) -> ProviderResult:
        normalized = str(ticker).upper()
        self.calls.append(normalized)
        payload = deepcopy(self._bundles_by_ticker[normalized])
        warnings = list(self._warnings_by_ticker.get(normalized, []))
        return ProviderResult(payload=payload, warnings=warnings)

    def close(self) -> None:
        return None


class StaticMacroProvider:
    def __init__(self, payload: dict, *, source_name: str, warnings: list[str] | None = None) -> None:
        self.source_name = source_name
        self._payload = deepcopy(payload)
        self._warnings = list(warnings or [])
        self.calls = 0

    def fetch_macro_bundle(self) -> ProviderResult:
        self.calls += 1
        return ProviderResult(payload=deepcopy(self._payload), warnings=list(self._warnings))

    def close(self) -> None:
        return None


class StaticPeerProvider:
    def __init__(
        self,
        *,
        source_name: str,
        discovery_map: dict[str, list[str]] | None = None,
        reason_map: dict[str, str] | None = None,
        market_cap_snapshots: dict[str, tuple[float | None, str | None]] | None = None,
    ) -> None:
        self.source_name = source_name
        self._discovery_map = {str(key).upper(): list(value) for key, value in (discovery_map or {}).items()}
        self._reason_map = {str(key).upper(): value for key, value in (reason_map or {}).items()}
        self._market_cap_snapshots = {str(key).upper(): value for key, value in (market_cap_snapshots or {}).items()}
        self.discovery_calls: list[str] = []

    def discover(self, ticker: str, company_profile: dict) -> PeerDiscoveryResult:
        normalized = str(ticker).upper()
        self.discovery_calls.append(normalized)
        candidates = [PeerCandidate(ticker=item, source=self.source_name) for item in self._discovery_map.get(normalized, [])]
        reason = self._reason_map.get(normalized, f"selected from {self.source_name} test fixture")
        return PeerDiscoveryResult(candidates=candidates, source=self.source_name, reason=reason)

    def fetch_market_cap_snapshot(self, ticker: str) -> tuple[float | None, str | None] | None:
        return self._market_cap_snapshots.get(str(ticker).upper())


class RecordingSession:
    def __init__(self) -> None:
        self.records: list[tuple[str, tuple]] = []
        self.committed = False
        self.closed = False
        self.rolled_back = False

    def rollback(self) -> None:
        self.rolled_back = True

    def close(self) -> None:
        self.closed = True


class RecordingRepository:
    def __init__(self, session: RecordingSession) -> None:
        self.session = session

    def save_bronze(self, ticker: str, source: str, payload: dict) -> None:
        self.session.records.append(("bronze", (ticker, source, deepcopy(payload))))

    def save_silver(self, ticker: str, sector: str, industry: str, metrics: dict, peer_snapshot: dict) -> None:
        self.session.records.append(("silver", (ticker, sector, industry, deepcopy(metrics), deepcopy(peer_snapshot))))

    def save_gold(self, ticker: str, score: float, verdict: str, narrative: str, response_payload: dict) -> None:
        self.session.records.append(("gold", (ticker, score, verdict, narrative, deepcopy(response_payload))))

    def commit(self) -> None:
        self.session.committed = True


class RecordingPersistence:
    def __init__(self) -> None:
        self.sessions: list[RecordingSession] = []

    def session_factory(self) -> RecordingSession:
        session = RecordingSession()
        self.sessions.append(session)
        return session

    def repository_factory(self, session: RecordingSession) -> RecordingRepository:
        return RecordingRepository(session)
