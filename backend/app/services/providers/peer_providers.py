from __future__ import annotations

from dataclasses import dataclass
import logging

import httpx

from app.core.settings import get_settings
from app.services.analysis_safety import round_or_none
from app.services.analysis_safety import get_business_type_universe
from app.services.providers.live_clients import BaseHttpProvider, _safe_number

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PeerCandidate:
    ticker: str
    source: str
    raw_relevance_hint: float | None = None


@dataclass(frozen=True)
class PeerDiscoveryResult:
    candidates: list[PeerCandidate]
    source: str
    reason: str


class PeerProvider:
    source_name = "unknown"

    def discover(self, ticker: str, company_profile: dict) -> PeerDiscoveryResult:
        raise NotImplementedError


class FmpPeerProvider(BaseHttpProvider, PeerProvider):
    source_name = "fmp"

    def __init__(self) -> None:
        super().__init__()
        self.api_key = get_settings().fmp_api_key

    def discover(self, ticker: str, company_profile: dict) -> PeerDiscoveryResult:
        if not self.api_key:
            return PeerDiscoveryResult(candidates=[], source=self.source_name, reason="FMP peers API unavailable")
        try:
            payload = self._get_json(
                "https://financialmodelingprep.com/stable/stock-peers",
                params={"symbol": ticker, "apikey": self.api_key},
            )
        except Exception as exc:
            if self._is_rate_limited(exc):
                logger.warning("peer_provider_rate_limited", extra={"provider": self.source_name, "ticker": ticker, "status_code": 429})
                return PeerDiscoveryResult(candidates=[], source=self.source_name, reason="FMP peers API rate-limited")
            raise
        tickers = self._extract_tickers(payload)
        return PeerDiscoveryResult(
            candidates=[PeerCandidate(ticker=item, source=self.source_name) for item in tickers],
            source=self.source_name,
            reason="selected via FMP peers API and filtered by industry/market-cap similarity",
        )

    def fetch_market_cap_snapshot(self, ticker: str) -> tuple[float | None, str | None] | None:
        if not self.api_key:
            return None
        try:
            payload = self._get_json(
                "https://financialmodelingprep.com/stable/profile",
                params={"symbol": ticker, "apikey": self.api_key},
            )
        except Exception as exc:
            if self._is_rate_limited(exc):
                logger.warning("peer_market_cap_rate_limited", extra={"provider": self.source_name, "ticker": ticker, "status_code": 429})
                return None
            raise
        values = payload if isinstance(payload, list) else [payload] if isinstance(payload, dict) else []
        item = next((entry for entry in values if isinstance(entry, dict)), None)
        if not item:
            return None
        market_cap_raw = _safe_number(item.get("marketCap") or item.get("mktCap") or item.get("marketcap"))
        if market_cap_raw is None or market_cap_raw <= 0:
            return None
        currency = str(item.get("currency") or item.get("reportedCurrency") or item.get("defaultCurrency") or "").upper() or None
        return round_or_none(market_cap_raw / 1_000_000_000, 2), currency

    def _extract_tickers(self, payload: dict | list) -> list[str]:
        if isinstance(payload, list):
            values = payload
        elif isinstance(payload, dict):
            if isinstance(payload.get("peersList"), list):
                values = payload["peersList"]
            elif isinstance(payload.get("peers"), list):
                values = payload["peers"]
            elif isinstance(payload.get("symbolsList"), list):
                values = payload["symbolsList"]
            else:
                values = []
        else:
            values = []
        return [str(item).upper() for item in values if item]


class FinnhubPeerProvider(BaseHttpProvider, PeerProvider):
    source_name = "finnhub"

    def __init__(self) -> None:
        super().__init__()
        self.api_key = get_settings().finnhub_api_key

    def discover(self, ticker: str, company_profile: dict) -> PeerDiscoveryResult:
        if not self.api_key:
            return PeerDiscoveryResult(candidates=[], source=self.source_name, reason="Finnhub peers API unavailable")
        try:
            payload = self._get_json(
                "https://finnhub.io/api/v1/stock/peers",
                params={"symbol": ticker, "token": self.api_key},
            )
        except Exception as exc:
            if self._is_rate_limited(exc):
                logger.warning("peer_provider_rate_limited", extra={"provider": self.source_name, "ticker": ticker, "status_code": 429})
                return PeerDiscoveryResult(candidates=[], source=self.source_name, reason="Finnhub peers API rate-limited")
            raise
        values = payload if isinstance(payload, list) else []
        tickers = [str(item).upper() for item in values if item]
        return PeerDiscoveryResult(
            candidates=[PeerCandidate(ticker=item, source=self.source_name) for item in tickers],
            source=self.source_name,
            reason="selected via Finnhub fallback and local filtering",
        )

    def fetch_market_cap_snapshot(self, ticker: str) -> tuple[float | None, str | None] | None:
        if not self.api_key:
            return None
        try:
            payload = self._get_json(
                "https://finnhub.io/api/v1/stock/profile2",
                params={"symbol": ticker, "token": self.api_key},
            )
        except Exception as exc:
            if self._is_rate_limited(exc):
                logger.warning("peer_market_cap_rate_limited", extra={"provider": self.source_name, "ticker": ticker, "status_code": 429})
                return None
            raise
        if not isinstance(payload, dict):
            return None
        market_cap_raw = _safe_number(payload.get("marketCapitalization"))
        if market_cap_raw is None or market_cap_raw <= 0:
            return None
        currency = str(payload.get("currency") or "").upper() or None
        return round_or_none(market_cap_raw / 1_000, 2), currency


class ConfigPeerProvider(PeerProvider):
    source_name = "config"

    def __init__(self, peer_group_config: dict) -> None:
        self.peer_group_config = peer_group_config

    def discover(self, ticker: str, company_profile: dict) -> PeerDiscoveryResult:
        sector = company_profile.get("sector", "")
        industry = company_profile.get("industry", "")
        strict_match = None
        sector_only_match = None
        for rule in self.peer_group_config["rules"]:
            sector_match = rule["sector"] == sector
            industry_match = any(fragment.lower() in industry.lower() for fragment in rule["industry_contains"])
            if sector_match and industry_match:
                strict_match = rule
                break
            if sector_match and sector_only_match is None:
                sector_only_match = rule

        matched_rule = strict_match or sector_only_match
        if matched_rule is None and get_business_type_universe(company_profile.get("business_type")):
            return PeerDiscoveryResult(
                candidates=[],
                source=self.source_name,
                reason="skipped broad config fallback because a business-type-safe universe is available",
            )
        selected = matched_rule["tickers"] if matched_rule else self.peer_group_config["fallback"]["tickers"]
        reason = (
            "selected from local config fallback due to insufficient API peers"
            if matched_rule
            else "selected from broad config fallback due to insufficient API peers"
        )
        return PeerDiscoveryResult(
            candidates=[PeerCandidate(ticker=item, source=self.source_name) for item in selected],
            source=self.source_name,
            reason=reason,
        )


class BusinessTypePeerProvider(PeerProvider):
    source_name = "business_type"

    def discover(self, ticker: str, company_profile: dict) -> PeerDiscoveryResult:
        business_type = company_profile.get("business_type")
        tickers = [item for item in get_business_type_universe(business_type) if item != ticker]
        reason = "selected via business-type fallback universe"
        if str(business_type or "").upper() == "AUTO_MANUFACTURER":
            reason = "selected via sector-safe auto/EV fallback universe"
        return PeerDiscoveryResult(
            candidates=[PeerCandidate(ticker=item, source=self.source_name) for item in tickers],
            source=self.source_name,
            reason=reason,
        )
