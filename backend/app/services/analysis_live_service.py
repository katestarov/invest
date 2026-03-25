from __future__ import annotations

import logging
import math
import re

from sqlalchemy.orm import Session

from app.core.database import SessionLocal
from app.core.scoring import get_peer_group_config, get_scoring_config
from app.core.settings import get_settings
from app.repositories.analysis_repository import AnalysisRepository
from app.schemas.analysis import (
    AnalysisResponse,
    FundamentalTrendPoint,
    MacroPoint,
    MetricCard,
    PeerRow,
    PriceHistoryPoint,
    ScoreBreakdownItem,
)
from app.services.analysis_safety import (
    apply_low_confidence_cap,
    business_type_compatibility,
    clamp_or_none,
    classify_company,
    coverage_ratio,
    get_business_type_universe,
    is_bank_like_company,
    normalize_weights,
    premium_pct,
    round_or_none,
    safe_ratio,
    score_inverse,
    score_relative_valuation,
    score_positive,
    weighted_score,
)
from app.services.providers.live_clients import (
    FredProvider,
    SecEdgarProvider,
    WorldBankProvider,
    YahooFinanceProvider,
    summarize_peer_averages,
)
from app.services.providers.peer_providers import BusinessTypePeerProvider, ConfigPeerProvider, FmpPeerProvider, FinnhubPeerProvider, PeerDiscoveryResult
from app.utils.cache import TTLCache

logger = logging.getLogger(__name__)


class AnalysisService:
    def __init__(self) -> None:
        settings = get_settings()
        self.yahoo = YahooFinanceProvider()
        self.edgar = SecEdgarProvider()
        self.fred = FredProvider()
        self.world_bank = WorldBankProvider()
        self.scoring_config = get_scoring_config()
        self.peer_group_config = get_peer_group_config()
        self.peer_target_count = settings.peer_target_count
        self.peer_min_valid_count = settings.peer_min_valid_count
        self.peer_providers = [
            FmpPeerProvider(),
            FinnhubPeerProvider(),
            BusinessTypePeerProvider(),
            ConfigPeerProvider(self.peer_group_config),
        ]
        self.peer_group_cache = TTLCache[tuple[list[dict], dict]](
            ttl_seconds=settings.provider_cache_ttl_seconds,
            max_items=128,
        )
        self.analysis_cache = TTLCache[AnalysisResponse](
            ttl_seconds=settings.analysis_cache_ttl_seconds,
            max_items=128,
        )

    def analyze(self, ticker: str) -> AnalysisResponse:
        normalized_ticker = self._normalize_ticker(ticker)
        cached_response = self.analysis_cache.get(normalized_ticker)
        if cached_response is not None:
            return cached_response

        warnings: list[str] = []

        yahoo_result = self.yahoo.fetch_company_bundle(normalized_ticker)
        edgar_result = self.edgar.fetch_company_bundle(normalized_ticker)
        fred_result = self.fred.fetch_macro_bundle()
        world_bank_result = self.world_bank.fetch_macro_bundle()

        warnings.extend(yahoo_result.warnings)
        warnings.extend(edgar_result.warnings)
        warnings.extend(fred_result.warnings)
        warnings.extend(world_bank_result.warnings)

        company_profile = {
            "ticker": normalized_ticker,
            "company": yahoo_result.payload.get("company") or edgar_result.payload.get("company") or normalized_ticker,
            "sector": edgar_result.payload.get("sector", "Unknown"),
            "industry": edgar_result.payload.get("industry", "Unknown"),
            "sic": edgar_result.payload.get("sic", ""),
        }
        business_type, business_type_confidence, business_type_reason = classify_company(
            ticker=normalized_ticker,
            sector=company_profile["sector"],
            industry=company_profile["industry"],
            sic=company_profile["sic"],
            company=company_profile["company"],
        )
        company_profile["business_type"] = business_type
        company_profile["business_type_confidence"] = business_type_confidence
        company_profile["business_type_reason"] = business_type_reason
        is_bank_like = self._is_bank_like(company_profile)
        macro = {**fred_result.payload, **world_bank_result.payload}
        peers, peer_selection = self._build_peer_group(company_profile, yahoo_result.payload, edgar_result.payload)
        peer_selection["business_type_confidence"] = company_profile["business_type_confidence"]
        peer_selection["business_type_reason"] = company_profile["business_type_reason"]
        peer_averages = self._build_peer_averages(peers, peer_selection, yahoo_result.payload, edgar_result.payload)
        silver_metrics = self._build_silver_metrics(
            yahoo_result.payload,
            edgar_result.payload,
            peer_averages | peer_selection,
            is_bank_like,
        )
        warnings.extend(self._build_data_quality_warnings(edgar_result.payload, macro, peer_averages | peer_selection, is_bank_like, silver_metrics))
        warnings.extend(self._build_completeness_warnings(silver_metrics, macro))
        weighted_scores = self._build_weighted_scores(silver_metrics, macro)
        total_score = round(sum((score or 0.0) * weight for score, weight, _ in weighted_scores.values()), 1)
        verdict = self._verdict(total_score)
        warnings = self._dedupe_warnings(warnings)
        narrative = self._build_narrative(company_profile["company"], total_score, weighted_scores, warnings)

        response = AnalysisResponse(
            ticker=normalized_ticker,
            company=company_profile["company"],
            sector=company_profile["sector"],
            industry=company_profile["industry"],
            score=total_score,
            verdict=verdict,
            narrative=narrative,
            metric_cards=self._metric_cards(silver_metrics, peer_averages),
            score_breakdown=[
                ScoreBreakdownItem(
                    key=key,
                    label=self.scoring_config["labels"].get(key, key),
                    score=round_or_none(score if weight > 0 else None, 1),
                    weight=weight,
                    summary=summary,
                )
                for key, (score, weight, summary) in weighted_scores.items()
            ],
            peers=self._peer_rows(peers),
            fundamentals_history=[
                FundamentalTrendPoint(**row) for row in edgar_result.payload.get("history", [])[:4]
            ],
            price_history=[
                PriceHistoryPoint(**row) for row in yahoo_result.payload.get("price_history", [])
            ],
            macro=[
                MacroPoint(label="Ключевая ставка ФРС (Fed Funds)", value=macro.get("fed_funds_rate_pct"), unit="%", source="FRED"),
                MacroPoint(label="Инфляция CPI (Inflation)", value=macro.get("inflation_pct"), unit="%", source="FRED"),
                MacroPoint(label="Безработица (Unemployment)", value=macro.get("unemployment_pct"), unit="%", source="FRED"),
                MacroPoint(label="Рост ВВП США (GDP Growth)", value=macro.get("gdp_growth_pct"), unit="%", source="World Bank"),
            ],
            assumptions=[
                "Источники запрашиваются в реальном времени и сохраняются в слои bronze, silver и gold.",
                "Peer-group подбирается по правилу sector + industry_contains из конфигурации.",
                "Формула скоринга читается из конфигурации и нормирует каждый блок до шкалы 0-100.",
            ],
            data_sources=[
                self.yahoo.source_name,
                self.edgar.source_name,
                self.fred.source_name,
                self.world_bank.source_name,
            ],
            warnings=warnings,
        )
        logger.info(
            "analysis_completed",
            extra={
                "ticker": normalized_ticker,
                "peer_mode": peer_selection.get("peer_selection_mode"),
                "baseline_mode": peer_averages.get("valuation_baseline_mode"),
                "peer_count": peer_selection.get("peer_count_usable"),
                "warning_count": len(warnings),
            },
        )

        self._persist_layers(
            normalized_ticker,
            yahoo_result.payload,
            edgar_result.payload,
            fred_result.payload,
            world_bank_result.payload,
            silver_metrics,
            peers,
            response,
        )
        self.analysis_cache.set(normalized_ticker, response)
        return response

    def _normalize_ticker(self, ticker: str) -> str:
        normalized_ticker = str(ticker or "").upper().strip()
        if not re.fullmatch(r"[A-Z0-9]{1,16}", normalized_ticker):
            raise ValueError("Ticker must contain only Latin letters and digits and be 1-16 characters long.")
        return normalized_ticker

    def clear_cache(self) -> None:
        self.analysis_cache.clear()

    def close(self) -> None:
        for provider in (self.yahoo, self.edgar, self.fred, self.world_bank):
            close = getattr(provider, "close", None)
            if callable(close):
                close()

    def _persist_layers(
        self,
        ticker: str,
        yahoo_payload: dict,
        edgar_payload: dict,
        fred_payload: dict,
        world_bank_payload: dict,
        silver_metrics: dict,
        peers: list[dict],
        response: AnalysisResponse,
    ) -> None:
        session: Session = SessionLocal()
        try:
            repository = AnalysisRepository(session)
            repository.save_bronze(ticker, self.yahoo.source_name, yahoo_payload)
            repository.save_bronze(ticker, self.edgar.source_name, edgar_payload)
            repository.save_bronze(ticker, self.fred.source_name, fred_payload)
            repository.save_bronze(ticker, self.world_bank.source_name, world_bank_payload)
            repository.save_silver(ticker, response.sector, response.industry, silver_metrics, {"rows": peers})
            repository.save_gold(ticker, response.score, response.verdict, response.narrative, response.model_dump())
            repository.commit()
        except Exception:
            session.rollback()
            logger.exception("persist_layers_failed", extra={"ticker": ticker})
        finally:
            session.close()

    def _build_peer_group(self, company_profile: dict, yahoo: dict, edgar: dict) -> tuple[list[dict], dict]:
        cache_key = f"{company_profile.get('ticker')}|{company_profile.get('sector')}|{company_profile.get('industry')}|{company_profile.get('sic')}"
        cached = self.peer_group_cache.get(cache_key)
        if cached is not None:
            return cached

        company_market_cap = self._market_cap_bln(yahoo, edgar)
        company_profile = company_profile | {
            "market_cap_bln": company_market_cap,
            "revenue_bln_latest": (edgar.get("revenue_bln") or [None])[0],
            "ebit_margin_pct": edgar.get("ebit_margin_pct"),
        }
        fallback_rows: list[dict] = []
        fallback_meta = {
            "peer_selection_confidence": "low",
            "peer_selection_reason": "selected from broad config fallback due to insufficient API peers",
            "peer_selection_source": "config",
            "peer_group_quality_passed": False,
            "peer_group_sample_limited": False,
            "peer_sample_mode": "excluded",
            "peer_count": 0,
            "target_peer_count": self.peer_target_count,
            "peer_expansion_level": 0,
            "incompatible_peer_count": 0,
        }
        aggregated_candidate_tickers: list[str] = []
        for provider in self.peer_providers:
            discovery = provider.discover(company_profile["ticker"], company_profile)
            candidate_tickers = self._filter_candidate_tickers_by_business_type(
                company_profile,
                [candidate.ticker for candidate in discovery.candidates],
                discovery.source,
            )
            if not candidate_tickers:
                continue
            aggregated_candidate_tickers = list(dict.fromkeys(aggregated_candidate_tickers + candidate_tickers))
            rows = self._fetch_peer_rows(aggregated_candidate_tickers, company_profile["ticker"])
            selected_rows, selection_meta = self._select_peers_from_candidates(
                company_profile,
                rows,
                company_market_cap,
                aggregated_candidate_tickers,
            )
            selection_meta["peer_selection_reason"] = discovery.reason
            selection_meta["peer_selection_source"] = discovery.source
            if selected_rows and self._is_better_peer_selection(selection_meta, fallback_meta):
                fallback_rows, fallback_meta = selected_rows, selection_meta
            if self._peer_selection_is_sufficient(selected_rows, selection_meta, discovery):
                self.peer_group_cache.set(cache_key, (selected_rows, selection_meta))
                return selected_rows, selection_meta

        self.peer_group_cache.set(cache_key, (fallback_rows, fallback_meta))
        return fallback_rows, fallback_meta

    def _build_silver_metrics(self, yahoo: dict, edgar: dict, peers: dict, is_bank_like: bool = False) -> dict:
        revenue_cagr_like = self._revenue_cagr_like_pct(edgar)
        market_cap_bln = self._market_cap_bln(yahoo, edgar)
        roe_assessment = self._roe_assessment(edgar, market_cap_bln)
        pe_ratio = self._pe_ratio(yahoo, edgar)
        pb_ratio = self._pb_ratio(yahoo, edgar)
        revenue_growth_pct = self._revenue_growth_pct(edgar)
        peer_confidence = peers.get("peer_confidence", peers.get("peer_selection_confidence", "low"))
        peer_count_usable = peers.get("peer_count_usable", 0)
        minimum_usable_peers = max(3, self.peer_min_valid_count)
        valuation_enabled = bool(
            peer_count_usable >= minimum_usable_peers
            and peers.get("pe_ratio_valid_count", 0) >= minimum_usable_peers
            and peers.get("pb_ratio_valid_count", 0) >= minimum_usable_peers
        )
        pe_premium_pct = round_or_none(premium_pct(pe_ratio, peers["pe_ratio"]), 2) if valuation_enabled else None
        pb_premium_pct = round_or_none(premium_pct(pb_ratio, peers["pb_ratio"]), 2) if valuation_enabled else None

        metrics = {
            "market_cap_bln": market_cap_bln,
            "pe_ratio": pe_ratio,
            "pb_ratio": pb_ratio,
            "roe_pct": roe_assessment["display_pct"],
            "roe_score_pct": roe_assessment["score_input_pct"],
            "roe_reliable": roe_assessment["reliable"],
            "roe_reliability_reason": roe_assessment["reason"],
            "roic_pct": edgar.get("roic_pct"),
            "ebit_margin_pct": edgar.get("ebit_margin_pct"),
            "revenue_growth_pct": revenue_growth_pct,
            "revenue_cagr_like_pct": revenue_cagr_like,
            "fcf_margin_pct": edgar.get("fcf_margin_pct"),
            "debt_to_equity": edgar.get("debt_to_equity"),
            "current_ratio": edgar.get("current_ratio"),
            "one_year_return_pct": yahoo.get("one_year_return_pct"),
            "five_year_return_pct": yahoo.get("five_year_return_pct"),
            "peer_pe_avg": peers["pe_ratio"],
            "peer_pb_avg": peers["pb_ratio"],
            "peer_roe_avg": peers["roe_pct"],
            "peer_growth_avg": peers["revenue_growth_pct"],
            "peer_debt_avg": peers["debt_to_equity"],
            "peer_pe_valid_count": peers.get("pe_ratio_valid_count", 0),
            "peer_pb_valid_count": peers.get("pb_ratio_valid_count", 0),
            "peer_roe_valid_count": peers.get("roe_pct_valid_count", 0),
            "peer_growth_valid_count": peers.get("revenue_growth_pct_valid_count", 0),
            "peer_debt_valid_count": peers.get("debt_to_equity_valid_count", 0),
            "peer_pe_noisy": peers.get("pe_ratio_baseline_noisy", True),
            "peer_pb_noisy": peers.get("pb_ratio_baseline_noisy", True),
            "peer_roe_noisy": peers.get("roe_pct_baseline_noisy", True),
            "peer_growth_noisy": peers.get("revenue_growth_pct_baseline_noisy", True),
            "peer_debt_noisy": peers.get("debt_to_equity_baseline_noisy", True),
            "peer_selection_mode": peers.get("peer_selection_mode", "fallback"),
            "peer_confidence": peer_confidence,
            "peer_confidence_multiplier": self._peer_confidence_multiplier(peer_confidence),
            "peer_count_total": peers.get("peer_count_total", peers.get("peer_count", 0)),
            "peer_count_usable": peer_count_usable,
            "peer_baseline_reliability": peers.get("peer_baseline_reliability", "low"),
            "valuation_baseline_mode": peers.get("valuation_baseline_mode", "peer"),
            "peer_selection_confidence": peers.get("peer_selection_confidence", "low"),
            "peer_selection_reason": peers.get("peer_selection_reason", "fallback"),
            "valuation_enabled": valuation_enabled,
            "profitability_reliability_multiplier": roe_assessment["weight_multiplier"],
            "pe_premium_pct": pe_premium_pct,
            "pb_premium_pct": pb_premium_pct,
            "is_bank_like": is_bank_like,
        }
        tracked_metrics = [
            metrics.get("pe_ratio"),
            metrics.get("pb_ratio"),
            metrics.get("roe_score_pct"),
            metrics.get("roic_pct"),
            metrics.get("ebit_margin_pct"),
            metrics.get("revenue_growth_pct"),
            metrics.get("revenue_cagr_like_pct"),
            metrics.get("fcf_margin_pct"),
            metrics.get("debt_to_equity"),
            metrics.get("current_ratio"),
            metrics.get("one_year_return_pct"),
            metrics.get("five_year_return_pct"),
        ]
        completeness = len([value for value in tracked_metrics if value is not None]) / max(len(tracked_metrics), 1)
        peer_reliability = {"high": 1.0, "medium": 0.85, "low": 0.7}.get(metrics["peer_baseline_reliability"], 0.7)
        noise_penalty = 0.85 if metrics.get("peer_pe_noisy") or metrics.get("peer_pb_noisy") else 1.0
        reliability_components = [
            metrics["peer_confidence_multiplier"],
            peer_reliability,
            noise_penalty,
            roe_assessment["weight_multiplier"],
        ]
        metrics["data_completeness_score"] = round(completeness * 100, 1)
        metrics["data_reliability_score"] = round(
            (sum(reliability_components) / max(len(reliability_components), 1)) * 100,
            1,
        )
        return metrics

    def _revenue_cagr_like_pct(self, edgar: dict) -> float | None:
        revenue_history = edgar.get("revenue_bln", [])
        if len(revenue_history) < 2:
            return None
        latest_revenue = revenue_history[0]
        earliest_revenue = revenue_history[-1]
        interval_count = len(revenue_history) - 1
        growth_ratio = safe_ratio(latest_revenue, earliest_revenue)
        if growth_ratio is None or growth_ratio <= 0 or interval_count <= 0:
            return None
        return round_or_none(((growth_ratio ** (1 / interval_count)) - 1) * 100, 2)

    def _fetch_peer_rows(self, tickers: list[str], company_ticker: str) -> list[dict]:
        rows: list[dict] = []
        for peer_ticker in list(dict.fromkeys(tickers)):
            if peer_ticker == company_ticker:
                continue
            try:
                yahoo_payload = self.yahoo.fetch_company_bundle(peer_ticker).payload
                edgar_payload = self.edgar.fetch_company_bundle(peer_ticker).payload
                rows.append(
                    {
                        "ticker": peer_ticker,
                        "company": yahoo_payload.get("company") or edgar_payload.get("company") or peer_ticker,
                        "sector": edgar_payload.get("sector", "Unknown"),
                        "industry": edgar_payload.get("industry", "Unknown"),
                        "sic": edgar_payload.get("sic", ""),
                        "market_cap_bln": self._market_cap_bln(yahoo_payload, edgar_payload),
                        "pe_ratio": self._pe_ratio(yahoo_payload, edgar_payload),
                        "pb_ratio": self._pb_ratio(yahoo_payload, edgar_payload),
                        "roe_pct": self._roe_assessment(
                            edgar_payload,
                            self._market_cap_bln(yahoo_payload, edgar_payload),
                        )["score_input_pct"],
                        "revenue_growth_pct": self._revenue_growth_pct(edgar_payload),
                        "revenue_bln": (edgar_payload.get("revenue_bln") or [None])[0],
                        "ebit_margin_pct": edgar_payload.get("ebit_margin_pct"),
                        "roic_pct": edgar_payload.get("roic_pct"),
                        "debt_to_equity": edgar_payload.get("debt_to_equity"),
                    }
                )
            except Exception:
                continue
        return rows

    def _select_peers_from_candidates(
        self,
        company_profile: dict,
        candidates: list[dict],
        company_market_cap: float | None,
        manual_tickers: list[str],
    ) -> tuple[list[dict], dict]:
        company_business_type = company_profile.get("business_type")
        if not company_business_type:
            company_business_type, _, _ = classify_company(
                ticker=company_profile.get("ticker"),
                sector=company_profile.get("sector"),
                industry=company_profile.get("industry"),
                sic=company_profile.get("sic"),
                company=company_profile.get("company"),
            )
            company_profile = company_profile | {"business_type": company_business_type}
        scored_candidates = self._rank_peer_candidates(company_profile, candidates, company_market_cap)
        scored_manual = [item for item in scored_candidates if item[1]["ticker"] in manual_tickers]
        manual_valid = len([1 for _, candidate, _, _ in scored_manual if self._peer_data_quality(candidate) >= 2])
        ranked = sorted(scored_manual if manual_valid >= 3 else scored_candidates, key=lambda item: item[0], reverse=True)

        strict_ranked = [item for item in ranked if item[2] == "strict"]
        extended_ranked = [item for item in ranked if item[2] == "extended"]
        fallback_ranked = [item for item in ranked if item[2] == "fallback"]
        strict_usable = [item for item in strict_ranked if self._peer_data_quality(item[1]) >= 2]
        extended_usable = [item for item in strict_ranked + extended_ranked if self._peer_data_quality(item[1]) >= 2]
        fallback_usable = [item for item in strict_ranked + extended_ranked + fallback_ranked if self._peer_data_quality(item[1]) >= 2]

        peer_selection_mode = "fallback"
        chosen_ranked = strict_ranked + extended_ranked + fallback_ranked
        usable_ranked = fallback_usable
        minimum_usable_peers = max(3, self.peer_min_valid_count)
        if len(strict_usable) >= minimum_usable_peers:
            peer_selection_mode = "strict"
            chosen_ranked = strict_ranked
            usable_ranked = strict_usable
        elif len(extended_usable) >= minimum_usable_peers:
            peer_selection_mode = "extended"
            chosen_ranked = strict_ranked + extended_ranked
            usable_ranked = extended_usable

        selected = [candidate for _, candidate, _, _ in chosen_ranked[: self.peer_target_count]]
        usable_selected = [candidate for _, candidate, _, _ in usable_ranked[: self.peer_target_count]]
        top_score = chosen_ranked[0][0] if chosen_ranked else 0.0
        average_score = sum(score for score, _, _, _ in chosen_ranked[: self.peer_target_count]) / max(len(selected), 1) if selected else 0.0
        matching_business_types = sum(
            1 for candidate in selected if self._candidate_business_type(candidate) == company_business_type and company_business_type != "OTHER"
        )
        strong_peer_count = sum(
            1
            for candidate in selected
            if self._peer_compatibility(company_profile, candidate) == "STRICT"
        )
        compatibility_levels = [
            self._peer_compatibility(company_profile, candidate)
            for candidate in selected
        ]
        expansion_level = 0 if peer_selection_mode == "strict" else 2 if peer_selection_mode == "extended" else 3 if selected else 0
        strict_share = strong_peer_count / max(len(selected), 1) if selected else 0.0
        quality_passed = bool(
            selected
            and average_score >= 2.0
            and (
                company_business_type in {"OTHER", "UNKNOWN"}
                or matching_business_types >= max(1, len(selected) // 2)
            )
            and strict_share >= 0.5
        )
        if peer_selection_mode == "extended":
            quality_passed = bool(selected and average_score >= 2.0)
        if peer_selection_mode == "fallback":
            quality_passed = False
        sample_mode = "excluded"
        if len(usable_selected) >= self.peer_target_count:
            sample_mode = "full"
        elif len(usable_selected) >= minimum_usable_peers:
            sample_mode = "limited"
        elif len(usable_selected) >= 1:
            sample_mode = "very_small"
        if quality_passed and any(level == "WEAK" for level in compatibility_levels) and len(selected) >= 3:
            quality_passed = False
            sample_mode = "excluded"
        sample_limited = sample_mode in {"limited", "very_small"}
        confidence = "low"
        if peer_selection_mode == "strict" and sample_mode == "full" and top_score >= 4.5 and strong_peer_count >= 2:
            confidence = "high"
        elif peer_selection_mode in {"strict", "extended"} and len(usable_selected) >= 2:
            confidence = "medium"
        elif peer_selection_mode == "fallback" and len(usable_selected) >= 1:
            confidence = "low"
        peer_baseline_reliability = "low"
        if len(usable_selected) >= self.peer_target_count:
            peer_baseline_reliability = "high"
        elif len(usable_selected) >= minimum_usable_peers:
            peer_baseline_reliability = "medium"
        reason = "manual peer group" if manual_valid >= 3 else "auto-selected peers"
        return selected, {
            "company_ticker": company_profile.get("ticker"),
            "peer_selection_mode": peer_selection_mode,
            "peer_confidence": confidence,
            "peer_baseline_reliability": peer_baseline_reliability,
            "peer_selection_confidence": confidence,
            "peer_selection_reason": reason,
            "peer_group_quality_passed": quality_passed,
            "peer_group_sample_limited": sample_limited,
            "peer_sample_mode": sample_mode,
            "peer_count": len(selected),
            "peer_count_total": len(selected),
            "peer_count_usable": len(usable_selected),
            "target_peer_count": self.peer_target_count,
            "peer_expansion_level": expansion_level,
            "incompatible_peer_count": len(candidates) - len(scored_candidates),
            "usable_peer_tickers": [candidate["ticker"] for candidate in usable_selected],
        }

    def _peer_selection_is_sufficient(
        self,
        selected_rows: list[dict],
        selection_meta: dict,
        discovery: PeerDiscoveryResult,
    ) -> bool:
        if selection_meta.get("peer_selection_mode") == "strict" and selection_meta.get("peer_sample_mode") == "full":
            return True
        if selection_meta.get("peer_selection_mode") == "extended" and selection_meta.get("peer_count_usable", 0) >= max(3, self.peer_min_valid_count):
            return True
        return False

    def _rank_peer_candidates(
        self,
        company_profile: dict,
        candidates: list[dict],
        company_market_cap: float | None,
    ) -> list[tuple[float, dict, str, str]]:
        ranked: list[tuple[float, dict, str, str]] = []
        company_business_type = company_profile.get("business_type", "OTHER")
        for candidate in candidates:
            score = self._peer_match_score(company_profile, company_market_cap, candidate)
            if score <= 0:
                continue
            compatibility = self._peer_compatibility(company_profile, candidate)
            same_sector = company_profile.get("sector") == candidate.get("sector")
            industry_close = self._industry_is_close(company_profile, candidate)
            business_match = self._candidate_business_type(candidate) == company_business_type and company_business_type not in {"OTHER", "UNKNOWN"}
            tier = "fallback"
            if compatibility == "STRICT" and (business_match or (same_sector and industry_close) or (same_sector and company_profile.get("business_type") in {"BANK", "PAYMENTS", "INSURANCE", "ASSET_MANAGER"})):
                tier = "strict"
            elif compatibility in {"STRICT", "RELATED"} and (same_sector or business_match):
                tier = "extended"
            ranked.append((score, candidate, tier, compatibility))
        return ranked

    def _peer_compatibility(self, company_profile: dict, candidate: dict) -> str:
        company_business_type = company_profile.get("business_type", "OTHER")
        candidate_business_type = self._candidate_business_type(candidate)
        compatibility = business_type_compatibility(company_business_type, candidate_business_type)
        if self._is_mega_cap_tech_pair(company_profile.get("ticker"), candidate.get("ticker")):
            if compatibility in {"REJECT", "WEAK"}:
                return "RELATED"
        return compatibility

    def _industry_is_close(self, company_profile: dict, candidate: dict) -> bool:
        company_sic = str(company_profile.get("sic") or "")
        candidate_sic = str(candidate.get("sic") or "")
        if company_sic and candidate_sic and (company_sic[:4] == candidate_sic[:4] or company_sic[:3] == candidate_sic[:3]):
            return True
        return self._industry_similarity_score(
            company_profile.get("industry"),
            candidate.get("industry"),
        ) >= 0.34

    def _special_peer_universe(self, company_profile: dict) -> list[str]:
        business_type = str(company_profile.get("business_type") or "").upper()
        if business_type == "CONSUMER_HARDWARE_ECOSYSTEM":
            return ["AAPL", "MSFT", "NVDA", "QCOM", "SONY", "DELL", "HPQ", "SMSN"]
        if business_type == "ENTERPRISE_SOFTWARE":
            return ["MSFT", "ORCL", "ADBE", "SAP", "NOW", "CRM"]
        if business_type == "INTERNET_PLATFORM":
            return ["META", "GOOGL", "AMZN", "UBER", "ABNB", "BKNG", "EXPE", "SNAP", "PINS", "TTD"]
        if business_type == "PHARMA":
            return ["LLY", "PFE", "MRK", "JNJ", "ABBV", "BMY", "AZN", "NVS"]
        if business_type == "BANK":
            return ["JPM", "BAC", "WFC", "C", "USB", "PNC", "GS", "MS"]
        if business_type == "PAYMENTS":
            return ["V", "MA", "AXP", "PYPL", "FI", "GPN", "COF", "DFS"]
        return []

    def _is_mega_cap_tech_pair(self, ticker_a: str | None, ticker_b: str | None) -> bool:
        mega_cap_tech = {"AAPL", "MSFT", "GOOGL", "AMZN", "META", "NVDA"}
        return str(ticker_a or "").upper() in mega_cap_tech and str(ticker_b or "").upper() in mega_cap_tech

    def _peer_confidence_multiplier(self, confidence: str | None) -> float:
        return {"high": 1.0, "medium": 0.85, "low": 0.65}.get(str(confidence or "").lower(), 0.65)

    def _roe_equity_threshold_bln(self, market_cap_bln: float | None) -> float:
        if market_cap_bln is None or market_cap_bln <= 0:
            return 1.0
        return max(1.0, min(market_cap_bln * 0.08, 75.0))

    def _normalized_tokens(self, value: str | None) -> set[str]:
        text = (value or "").lower()
        tokens = re.split(r"[^a-z0-9]+", text)
        stop_words = {"and", "the", "for", "with", "services", "service", "information"}
        return {token for token in tokens if len(token) >= 3 and token not in stop_words}

    def _industry_similarity_score(self, company_industry: str | None, candidate_industry: str | None) -> float:
        company_text = (company_industry or "").lower().strip()
        candidate_text = (candidate_industry or "").lower().strip()
        if not company_text or not candidate_text:
            return 0.0
        if company_text in candidate_text or candidate_text in company_text:
            return 1.0
        company_tokens = self._normalized_tokens(company_text)
        candidate_tokens = self._normalized_tokens(candidate_text)
        if not company_tokens or not candidate_tokens:
            return 0.0
        overlap = len(company_tokens & candidate_tokens)
        union = len(company_tokens | candidate_tokens)
        return overlap / union if union else 0.0

    def _relative_scale_score(
        self,
        company_value: float | None,
        candidate_value: float | None,
        *,
        lower_ratio: float,
        upper_ratio: float,
        bonus: float,
        penalty: float,
    ) -> float:
        if company_value is None or candidate_value is None or company_value <= 0 or candidate_value <= 0:
            return 0.0
        ratio = candidate_value / company_value
        if lower_ratio <= ratio <= upper_ratio:
            return bonus
        log_distance = abs(math.log(ratio))
        band_distance = max(abs(math.log(lower_ratio)), abs(math.log(upper_ratio)))
        if band_distance <= 0:
            return 0.0
        return -min((log_distance - band_distance) / band_distance, 1.0) * penalty

    def _margin_similarity_score(self, company_margin: float | None, candidate_margin: float | None) -> float:
        if company_margin is None or candidate_margin is None:
            return 0.0
        distance = abs(company_margin - candidate_margin)
        if distance <= 5:
            return 1.0
        if distance <= 10:
            return 0.6
        if distance <= 20:
            return 0.2
        return -0.6

    def _roe_assessment(self, edgar: dict, market_cap_bln: float | None) -> dict[str, float | bool | str | None]:
        net_income = edgar.get("net_income_bln")
        equity = edgar.get("equity_bln")
        roic_pct = clamp_or_none(edgar.get("roic_pct"), 0.0, 100.0)
        ratio = safe_ratio(net_income, equity)
        display_pct = round_or_none(ratio * 100 if ratio is not None else None, 2)
        clamped_roe = round_or_none(clamp_or_none(display_pct, 0.0, 100.0), 2)

        reliable = True
        reason: str | None = None
        if display_pct is None:
            reliable = False
            reason = "ROE unavailable because equity is non-interpretable"
        elif equity is not None and equity < self._roe_equity_threshold_bln(market_cap_bln):
            reliable = False
            reason = "ROE treated as unreliable because the equity base is too small relative to market value"

        score_input = roic_pct if not reliable and roic_pct is not None else clamped_roe
        weight_multiplier = 1.0
        if not reliable:
            weight_multiplier = 0.9 if roic_pct is not None else 0.75

        return {
            "display_pct": display_pct,
            "score_input_pct": score_input,
            "reliable": reliable,
            "reason": reason,
            "weight_multiplier": weight_multiplier,
        }

    def _peer_match_score(self, company_profile: dict, company_market_cap: float | None, candidate: dict) -> float:
        score = 0.0
        company_sic = str(company_profile.get("sic") or "")
        candidate_sic = str(candidate.get("sic") or "")
        company_industry = (company_profile.get("industry") or "").lower()
        candidate_industry = (candidate.get("industry") or "").lower()
        company_business_type = company_profile.get("business_type", "OTHER")
        candidate_business_type = self._candidate_business_type(candidate)
        compatibility = self._peer_compatibility(company_profile, candidate)
        if compatibility == "REJECT":
            return -100.0
        if compatibility == "STRICT":
            score += 4.0
        elif compatibility == "RELATED":
            score += 2.0
        elif compatibility == "WEAK":
            score += 0.5

        if company_business_type == candidate_business_type and company_business_type not in {"OTHER", "UNKNOWN"}:
            score += 1.0

        if company_sic and candidate_sic:
            if company_sic[:4] == candidate_sic[:4]:
                score += 3.0
            elif company_sic[:3] == candidate_sic[:3]:
                score += 2.0
            elif company_sic[:2] == candidate_sic[:2]:
                score += 1.0

        score += self._industry_similarity_score(company_industry, candidate_industry) * 3.0

        if company_profile.get("sector") == candidate.get("sector"):
            score += 0.75

        candidate_market_cap = candidate.get("market_cap_bln")
        score += self._relative_scale_score(
            company_market_cap,
            candidate_market_cap,
            lower_ratio=0.30,
            upper_ratio=1.70,
            bonus=1.5,
            penalty=1.5,
        )
        score += self._relative_scale_score(
            company_profile.get("revenue_bln_latest"),
            candidate.get("revenue_bln"),
            lower_ratio=0.35,
            upper_ratio=2.50,
            bonus=0.9,
            penalty=1.0,
        )
        score += self._margin_similarity_score(
            company_profile.get("ebit_margin_pct"),
            candidate.get("ebit_margin_pct"),
        )

        score -= max(0, 4 - self._peer_data_quality(candidate)) * 0.35
        if compatibility == "WEAK":
            score -= 1.25
        if self._is_mega_cap_tech_pair(company_profile.get("ticker"), candidate.get("ticker")):
            score += 1.0
        if candidate.get("pe_ratio") is not None and candidate["pe_ratio"] > 120:
            score -= 0.6
        if candidate.get("pb_ratio") is not None and candidate["pb_ratio"] > 25:
            score -= 0.4
        return score

    def _candidate_business_type(self, candidate: dict) -> str:
        business_type, _, _ = classify_company(
            ticker=candidate.get("ticker"),
            sector=candidate.get("sector"),
            industry=candidate.get("industry"),
            sic=candidate.get("sic"),
            company=candidate.get("company"),
        )
        return business_type

    def _filter_candidate_tickers_by_business_type(
        self,
        company_profile: dict,
        candidate_tickers: list[str],
        source: str,
    ) -> list[str]:
        unique = [ticker for ticker in list(dict.fromkeys(candidate_tickers)) if ticker != company_profile.get("ticker")]
        business_universe = get_business_type_universe(company_profile.get("business_type"))
        special_universe = self._special_peer_universe(company_profile)
        allowed_universe = list(dict.fromkeys(business_universe + special_universe))
        if not allowed_universe:
            return unique
        if source in {"fmp", "finnhub", "config"}:
            matching = [ticker for ticker in unique if ticker in allowed_universe]
            if matching:
                return matching
            return [] if source in {"fmp", "finnhub"} else unique
        return [ticker for ticker in unique if ticker in allowed_universe]

    def _is_better_peer_selection(self, candidate_meta: dict, current_meta: dict) -> bool:
        candidate_rank = (
            1 if candidate_meta.get("peer_group_quality_passed") else 0,
            {"excluded": 0, "very_small": 1, "limited": 2, "full": 3}.get(candidate_meta.get("peer_sample_mode", "excluded"), 0),
            candidate_meta.get("peer_count", 0),
            candidate_meta.get("peer_expansion_level", 0) * -1,
            1 if candidate_meta.get("peer_selection_confidence") == "high" else 0,
        )
        current_rank = (
            1 if current_meta.get("peer_group_quality_passed") else 0,
            {"excluded": 0, "very_small": 1, "limited": 2, "full": 3}.get(current_meta.get("peer_sample_mode", "excluded"), 0),
            current_meta.get("peer_count", 0),
            current_meta.get("peer_expansion_level", 0) * -1,
            1 if current_meta.get("peer_selection_confidence") == "high" else 0,
        )
        return candidate_rank > current_rank

    def _peer_data_quality(self, candidate: dict) -> int:
        metric_availability = self._peer_metric_availability(candidate)
        core_metrics = ("pe_ratio", "roe_pct", "revenue_growth_pct")
        optional_metrics = ("pb_ratio", "debt_to_equity")
        core_count = sum(1 for key in core_metrics if metric_availability[key])
        optional_count = sum(1 for key in optional_metrics if metric_availability[key])
        return (core_count * 2) + optional_count

    def _peer_metric_availability(self, candidate: dict) -> dict[str, bool]:
        return {
            "pe_ratio": candidate.get("pe_ratio") is not None,
            "pb_ratio": candidate.get("pb_ratio") is not None,
            "roe_pct": candidate.get("roe_pct") is not None,
            "revenue_growth_pct": candidate.get("revenue_growth_pct") is not None,
            "debt_to_equity": candidate.get("debt_to_equity") is not None,
        }

    def _build_peer_averages(self, peers: list[dict], peer_selection: dict, yahoo: dict, edgar: dict) -> dict:
        usable_tickers = set(peer_selection.get("usable_peer_tickers", []))
        usable_rows = [row for row in peers if row.get("ticker") in usable_tickers]
        minimum_usable_peers = max(3, self.peer_min_valid_count)
        baseline_mode = "peer"
        baseline_rows = usable_rows if len(usable_rows) >= minimum_usable_peers else []
        company_ticker = str(peer_selection.get("company_ticker") or "").upper()
        if self._is_mega_cap_tech_company(company_ticker) and len(usable_rows) < minimum_usable_peers and len(peers) >= minimum_usable_peers:
            baseline_rows = peers
            baseline_mode = "thematic"
        elif len(usable_rows) < minimum_usable_peers and len(peers) >= minimum_usable_peers:
            baseline_rows = peers
            baseline_mode = "fallback"
        elif len(usable_rows) < minimum_usable_peers:
            baseline_rows = []
            baseline_mode = "neutral"
        baseline_candidates = baseline_rows or []
        metric_rows = {
            "pe_ratio": [row for row in baseline_candidates if row.get("pe_ratio") is not None],
            "pb_ratio": [row for row in baseline_candidates if row.get("pb_ratio") is not None],
            "roe_pct": [row for row in baseline_candidates if row.get("roe_pct") is not None],
            "revenue_growth_pct": [row for row in baseline_candidates if row.get("revenue_growth_pct") is not None],
            "debt_to_equity": [row for row in baseline_candidates if row.get("debt_to_equity") is not None],
        }
        averages = {
            "pe_ratio": None,
            "pb_ratio": None,
            "roe_pct": None,
            "revenue_growth_pct": None,
            "debt_to_equity": None,
            "pe_ratio_valid_count": 0,
            "pb_ratio_valid_count": 0,
            "roe_pct_valid_count": 0,
            "revenue_growth_pct_valid_count": 0,
            "debt_to_equity_valid_count": 0,
            "pe_ratio_baseline_noisy": True,
            "pb_ratio_baseline_noisy": True,
            "roe_pct_baseline_noisy": True,
            "revenue_growth_pct_baseline_noisy": True,
            "debt_to_equity_baseline_noisy": True,
        }
        for metric, rows in metric_rows.items():
            metric_summary = summarize_peer_averages(rows)
            averages[metric] = metric_summary[metric]
            averages[f"{metric}_valid_count"] = metric_summary[f"{metric}_valid_count"]
            averages[f"{metric}_baseline_noisy"] = metric_summary[f"{metric}_baseline_noisy"]
        averages["valuation_baseline_mode"] = baseline_mode
        averages["peer_baseline_insufficient"] = len(usable_rows) < minimum_usable_peers
        averages["peer_baseline_reliability"] = "high" if len(usable_rows) >= self.peer_target_count else "medium" if len(usable_rows) >= minimum_usable_peers else "low"

        if averages.get("pe_ratio") is None or averages.get("pe_ratio_valid_count", 0) < minimum_usable_peers:
            averages["pe_ratio"] = None
            averages["pe_ratio_baseline_noisy"] = True
        if averages.get("pb_ratio") is None or averages.get("pb_ratio_valid_count", 0) < minimum_usable_peers:
            averages["pb_ratio"] = None
            averages["pb_ratio_baseline_noisy"] = True
        return averages

    def _is_mega_cap_tech_company(self, ticker: str | None) -> bool:
        return str(ticker or "").upper() in {"AAPL", "MSFT", "GOOGL", "AMZN", "META", "NVDA"}

    def _market_cap_bln(self, yahoo: dict, edgar: dict) -> float | None:
        price = yahoo.get("current_price")
        shares_mln = edgar.get("shares_outstanding_mln")
        return round_or_none((price * shares_mln) / 1000 if price and shares_mln else None, 2)

    def _pe_ratio(self, yahoo: dict, edgar: dict) -> float | None:
        market_cap = self._market_cap_bln(yahoo, edgar)
        net_income = edgar.get("net_income_bln")
        return round_or_none(safe_ratio(market_cap, net_income), 2)

    def _pb_ratio(self, yahoo: dict, edgar: dict) -> float | None:
        market_cap = self._market_cap_bln(yahoo, edgar)
        equity = edgar.get("equity_bln")
        return round_or_none(safe_ratio(market_cap, equity), 2)

    def _roe_pct(self, edgar: dict) -> float | None:
        net_income = edgar.get("net_income_bln")
        equity = edgar.get("equity_bln")
        ratio = safe_ratio(net_income, equity)
        return round_or_none(ratio * 100 if ratio is not None else None, 2)

    def _revenue_growth_pct(self, edgar: dict) -> float | None:
        history = edgar.get("revenue_bln", [])
        periods = edgar.get("revenue_periods", [])
        if len(history) >= 2:
            current_period = periods[0] if len(periods) >= 1 else {}
            previous_period = periods[1] if len(periods) >= 2 else {}
            if current_period and previous_period:
                if current_period.get("period_type") != previous_period.get("period_type"):
                    return None
                if current_period.get("fiscal_period") and previous_period.get("fiscal_period"):
                    if current_period.get("fiscal_period") != previous_period.get("fiscal_period"):
                        return None
            ratio = safe_ratio(history[0], history[1])
            growth_pct = (ratio - 1) * 100 if ratio is not None else None
            return round_or_none(growth_pct, 2)
        return None

    def _build_weighted_scores(self, metrics: dict, macro: dict) -> dict[str, tuple[float | None, float, str]]:
        weights = self.scoring_config["weights"]
        caps = self.scoring_config["caps"]
        valuation_enabled = bool(metrics.get("valuation_enabled", True))
        effective_roe_pct = metrics.get("roe_score_pct", metrics.get("roe_pct"))

        profitability_components = [
            (score_positive(effective_roe_pct, caps["roe_pct"]), 0.4),
            (score_positive(metrics["roic_pct"], caps["roic_pct"]), 0.35),
            (score_positive(metrics["ebit_margin_pct"], caps["ebit_margin_pct"]), 0.25),
        ]
        stability_components = [
            (score_inverse(metrics["debt_to_equity"], caps["debt_to_equity"]), 0.55),
            (score_positive(metrics["current_ratio"], caps["current_ratio"]), 0.20),
            (score_positive(metrics["fcf_margin_pct"], caps["fcf_margin_pct"]), 0.25),
        ]
        if metrics.get("is_bank_like"):
            stability_components = [
                (score_positive(effective_roe_pct, caps["roe_pct"]), 0.55),
                (score_positive(metrics["revenue_growth_pct"], caps["revenue_growth_pct"]), 0.25),
                (None, 0.20),
            ]
        valuation_components: list[tuple[float | None, float]] = []
        if valuation_enabled:
            valuation_components = [
                (score_relative_valuation(metrics["pe_premium_pct"], caps["pe_premium_pct"]), 0.55),
                (score_relative_valuation(metrics["pb_premium_pct"], caps["pb_premium_pct"]), 0.45),
            ]
        growth_components = [
            (score_positive(metrics["revenue_growth_pct"], caps["revenue_growth_pct"]), 0.55),
            (score_positive(metrics["revenue_cagr_like_pct"], caps["revenue_growth_pct"]), 0.20),
        ]
        if not metrics.get("is_bank_like"):
            growth_components.append((score_positive(metrics["fcf_margin_pct"], caps["fcf_margin_pct"]), 0.25))
        market_components = [
            (score_positive(metrics["one_year_return_pct"], 60), 0.45),
            (score_positive(metrics["five_year_return_pct"], caps["five_year_return_pct"]), 0.55),
        ]
        macro_components, macro_score = self._macro_components_and_score(macro)
        profitability = weighted_score(profitability_components)
        stability = weighted_score(stability_components)
        valuation = weighted_score(valuation_components)
        growth = weighted_score(growth_components)
        market = weighted_score(market_components)
        profitability = apply_low_confidence_cap(profitability, coverage_ratio(profitability_components))
        stability = apply_low_confidence_cap(stability, coverage_ratio(stability_components))
        valuation = apply_low_confidence_cap(valuation, coverage_ratio(valuation_components))
        growth = apply_low_confidence_cap(growth, coverage_ratio(growth_components))
        market = apply_low_confidence_cap(market, coverage_ratio(market_components))
        macro_score = apply_low_confidence_cap(macro_score, coverage_ratio(macro_components))
        block_scores = {
            "profitability": profitability,
            "stability": stability,
            "valuation": valuation,
            "growth": growth,
            "market": market,
            "macro": macro_score,
        }
        weighted_config = {
            "profitability": weights["profitability"]
            * coverage_ratio(profitability_components)
            * float(metrics.get("profitability_reliability_multiplier", 1.0)),
            "stability": weights["stability"] * coverage_ratio(stability_components),
            "valuation": weights["valuation"]
            * coverage_ratio(valuation_components)
            * float(metrics.get("peer_confidence_multiplier", 1.0)),
            "growth": weights["growth"] * coverage_ratio(growth_components),
            "market": weights["market"] * coverage_ratio(market_components),
            "macro": weights["macro"] * coverage_ratio(macro_components),
        }
        effective_weights = normalize_weights(block_scores, weighted_config)

        return {
            "profitability": (round_or_none(profitability, 2), effective_weights["profitability"], "Высокие ROE, ROIC и операционная маржа поддерживают качество бизнеса."),
            "stability": (round_or_none(stability, 2), effective_weights["stability"], "Смотрим долговую нагрузку, ликвидность и запас денежного потока."),
            "valuation": (round_or_none(valuation, 2), effective_weights["valuation"], "Оцениваем премию или дисконт по P/E и P/B против peer-group."),
            "growth": (round_or_none(growth, 2), effective_weights["growth"], "Учитываем рост выручки, ее динамику и качество FCF."),
            "market": (round_or_none(market, 2), effective_weights["market"], "Рыночный импульс учитывает доходность цены за 1 и 5 лет."),
            "macro": (round_or_none(macro_score, 2), effective_weights["macro"], "Макросреда корректирует оценку с учетом ставок, инфляции и роста экономики."),
        }

    def _metric_cards(self, metrics: dict, peers: dict) -> list[MetricCard]:
        valuation_enabled = bool(metrics.get("valuation_enabled", True))
        pe_benchmark = peers["pe_ratio"] if valuation_enabled else None
        return [
            MetricCard(
                label="Коэффициент P/E (Price/Earnings)",
                value=round_or_none(metrics["pe_ratio"], 2),
                benchmark=round_or_none(pe_benchmark, 2),
                direction="lower_better",
                display_value=self._display_metric(metrics["pe_ratio"]),
                display_benchmark=self._display_metric(pe_benchmark),
                comparison_label=self._comparison_label(
                    metrics["pe_ratio"],
                    pe_benchmark,
                    "lower_better",
                    metrics.get("peer_pe_valid_count", 3),
                    bool(metrics.get("peer_pe_noisy", False)) or metrics.get("peer_selection_confidence") == "low",
                ),
                description="Меньшее значение обычно означает более умеренную оценку относительно прибыли.",
            ),
            MetricCard(
                label="Рентабельность капитала ROE (Return on Equity)",
                value=round_or_none(metrics["roe_pct"], 2),
                unit="%",
                benchmark=round_or_none(peers["roe_pct"], 2),
                direction="higher_better",
                display_value=self._display_metric(metrics["roe_pct"]),
                display_benchmark=self._display_metric(peers["roe_pct"]),
                comparison_label=self._comparison_label(
                    metrics["roe_pct"],
                    peers["roe_pct"],
                    "higher_better",
                    metrics.get("peer_roe_valid_count", 3),
                    bool(metrics.get("peer_roe_noisy", False))
                    or metrics.get("peer_selection_confidence") == "low"
                    or not bool(metrics.get("roe_reliable", True)),
                ),
                description="Показывает, насколько эффективно компания генерирует прибыль на капитал акционеров.",
            ),
            MetricCard(
                label="Рост выручки (Revenue Growth)",
                value=round_or_none(metrics["revenue_growth_pct"], 2),
                unit="%",
                benchmark=round_or_none(peers["revenue_growth_pct"], 2),
                direction="higher_better",
                display_value=self._display_metric(metrics["revenue_growth_pct"]),
                display_benchmark=self._display_metric(peers["revenue_growth_pct"]),
                comparison_label=self._comparison_label(
                    metrics["revenue_growth_pct"],
                    peers["revenue_growth_pct"],
                    "higher_better",
                    metrics.get("peer_growth_valid_count", 3),
                    bool(metrics.get("peer_growth_noisy", False)) or metrics.get("peer_selection_confidence") == "low",
                ),
                description="Сравнение темпа роста выручки с компаниями того же сектора.",
            ),
            MetricCard(
                label="Долг/Капитал (Debt/Equity)",
                value=round_or_none(metrics["debt_to_equity"], 2),
                benchmark=round_or_none(peers["debt_to_equity"], 2),
                direction="lower_better",
                display_value=self._display_metric(metrics["debt_to_equity"]),
                display_benchmark=self._display_metric(peers["debt_to_equity"]),
                comparison_label=self._comparison_label(
                    metrics["debt_to_equity"],
                    peers["debt_to_equity"],
                    "lower_better",
                    metrics.get("peer_debt_valid_count", 3),
                    bool(metrics.get("peer_debt_noisy", False)) or bool(metrics.get("is_bank_like")) or metrics.get("peer_selection_confidence") == "low",
                ),
                description="Показывает, насколько агрессивно бизнес финансируется долгом.",
            ),
        ]

    def _peer_rows(self, peers: list[dict]) -> list[PeerRow]:
        rows = [
            PeerRow(
                ticker=item["ticker"],
                company=item["company"],
                sector=item["sector"],
                industry=item["industry"],
                score=round(
                    weighted_score(
                        [
                            (score_positive(item.get("roe_score_pct", item["roe_pct"]), 50), 0.4),
                            (score_positive(item["revenue_growth_pct"], 35), 0.35),
                            (score_inverse(item["pe_ratio"], 60), 0.25),
                        ]
                    )
                    or 0.0,
                    1,
                ),
                market_cap_bln=round_or_none(item["market_cap_bln"], 2),
                pe_ratio=round_or_none(item["pe_ratio"], 2),
                roe_pct=round_or_none(item["roe_pct"], 2),
                revenue_growth_pct=round_or_none(item["revenue_growth_pct"], 2),
            )
            for item in peers
        ]
        rows.sort(key=lambda item: item.score, reverse=True)
        return rows[:8]

    def _macro_components_and_score(self, macro: dict) -> tuple[list[tuple[float | None, float]], float | None]:
        fed_value = macro.get("fed_funds_rate_pct")
        inflation_value = macro.get("inflation_pct")
        unemployment_value = macro.get("unemployment_pct")
        gdp_value = macro.get("gdp_growth_pct")
        if fed_value is None and inflation_value is None and unemployment_value is None:
            return [
                (None, 0.30),
                (None, 0.25),
                (None, 0.20),
                (None, 0.25),
            ], None
        components = [
            (max(0.0, 100 - (fed_value * 12)) if fed_value is not None else None, 0.30),
            (max(0.0, 100 - (abs(inflation_value - 2.0) * 25)) if inflation_value is not None else None, 0.25),
            (max(0.0, 100 - (unemployment_value * 10)) if unemployment_value is not None else None, 0.20),
            (min(max(gdp_value, 0.0), 4.0) / 4.0 * 100 if gdp_value is not None else None, 0.25),
        ]
        return components, weighted_score(components)

    def _build_narrative(
        self,
        company: str,
        total_score: float,
        weighted_scores: dict[str, tuple[float, float, str]],
        warnings: list[str] | None = None,
    ) -> str:
        eligible = [(key, item) for key, item in weighted_scores.items() if item[1] > 0 and item[0] is not None]
        strongest = max(eligible, key=lambda item: item[1][0] * item[1][1]) if eligible else next(iter(weighted_scores.items()))
        weakest = min(eligible, key=lambda item: item[1][0] * item[1][1]) if eligible else next(iter(weighted_scores.items()))
        narrative = (
            f"{company} получает итоговую оценку {total_score}/100. "
            f"Сильнее всего компанию поддерживает блок «{self.scoring_config['labels'][strongest[0]]}», "
            f"а главным ограничителем сейчас остается «{self.scoring_config['labels'][weakest[0]]}»."
        )
        if any("baseline" in warning or "partial" in warning or "Bank-specific" in warning for warning in (warnings or [])):
            narrative += " Часть выводов интерпретируется осторожно из-за неполных или шумных сравнительных данных."
        return narrative
    def _display_metric(self, value: float | None) -> str:
        return f"{round(value, 2):g}" if value is not None else "N/A"

    def _comparison_label(
        self,
        value: float | None,
        benchmark: float | None,
        direction: str,
        valid_count: int = 3,
        baseline_noisy: bool = False,
    ) -> str:
        if value is None or benchmark is None:
            return "Недостаточно данных"
        if valid_count < 3 or baseline_noisy:
            return "Нейтрально"
        is_positive = value >= benchmark if direction == "higher_better" else value <= benchmark
        return "Лучше peers" if is_positive else "Слабее peers"
    def _build_data_quality_warnings(
        self,
        edgar: dict,
        macro: dict,
        peers: dict,
        is_bank_like: bool,
        metrics: dict | None = None,
    ) -> list[str]:
        warnings: list[str] = []
        if edgar.get("equity_bln") is not None and edgar.get("equity_bln") <= 0:
            warnings.append("Negative equity detected: ROE, Debt/Equity and P/B may be unreliable")
        if metrics and not metrics.get("roe_reliable", True):
            reason = metrics.get("roe_reliability_reason") or "ROE was de-emphasized"
            warnings.append(f"ROE reliability warning: {reason}")
        revenue_history = edgar.get("revenue_bln", [])
        if len(revenue_history) >= 2:
            ratio = safe_ratio(revenue_history[0], revenue_history[1])
            growth_pct = (ratio - 1) * 100 if ratio is not None else None
            if growth_pct is not None and abs(growth_pct) > 80:
                warnings.append("Revenue growth shows unusually large swing; underlying period matching was treated conservatively")
        fred_missing = [
            macro.get("fed_funds_rate_pct"),
            macro.get("inflation_pct"),
            macro.get("unemployment_pct"),
        ]
        if all(value is None for value in fred_missing):
            warnings.append("Macro disabled: FRED data unavailable, so macro weight was redistributed")
        elif any(value is None for value in fred_missing):
            warnings.append("Macro incomplete: FRED data unavailable, so macro score uses partial inputs")

        peer_reasons: list[str] = []
        if peers.get("peer_selection_mode") == "extended":
            peer_reasons.append("extended peer mode")
        if peers.get("peer_selection_mode") == "fallback":
            peer_reasons.append("fallback basket")
        if peers.get("peer_group_sample_limited"):
            peer_reasons.append("small sample")
        if any(peers.get(key, 0) < 3 for key in ("pe_ratio_valid_count", "pb_ratio_valid_count", "roe_pct_valid_count", "revenue_growth_pct_valid_count")):
            peer_reasons.append("sparse baseline")
        if any(peers.get(key, False) for key in ("pe_ratio_baseline_noisy", "pb_ratio_baseline_noisy")):
            peer_reasons.append("noisy valuation baseline")
        if peers.get("peer_baseline_insufficient"):
            peer_reasons.append("direct peer comparison insufficient")
        if peers.get("valuation_baseline_mode") == "thematic":
            peer_reasons.append("thematic median used")
        if peers.get("valuation_baseline_mode") == "fallback":
            peer_reasons.append("fallback basket median used")
        if peers.get("valuation_baseline_mode") == "neutral":
            peer_reasons.append("neutral valuation baseline")
        if peers.get("peer_count_usable", 0) < 3:
            warnings.append("Usable peer set too small (<3); valuation was disabled")
        if peers.get("peer_selection_confidence") == "low" or peer_reasons:
            warnings.append(f"Peer low confidence: {', '.join(dict.fromkeys(peer_reasons or ['limited relevance']))}")

        if any(
            peers.get(key, 0) == 0
            for key in ("pe_ratio_valid_count", "pb_ratio_valid_count", "roe_pct_valid_count", "debt_to_equity_valid_count")
        ):
            warnings.append("Incomplete fundamentals: some peer metrics were excluded as invalid or non-interpretable")
        if peers.get("incompatible_peer_count", 0) > 0:
            warnings.append("Peer cleanup applied: incompatible business models were excluded")
        if is_bank_like:
            warnings.append("Sector-specific fallback applied: bank-safe stability logic replaced generic debt/FCF rules")
        if peers.get("business_type_confidence") in {"medium", "low"}:
            warnings.append("Business type was inferred from rule-based classification")
        return warnings

    def _is_bank_like(self, company_profile: dict) -> bool:
        return is_bank_like_company(company_profile.get("sector"), company_profile.get("industry"))
    def _build_completeness_warnings(self, metrics: dict, macro: dict) -> list[str]:
        profitability_components = [metrics.get("roe_score_pct"), metrics.get("roic_pct"), metrics.get("ebit_margin_pct")]
        stability_components = [metrics.get("roe_score_pct"), metrics.get("revenue_growth_pct"), None] if metrics.get("is_bank_like") else [metrics.get("debt_to_equity"), metrics.get("current_ratio"), metrics.get("fcf_margin_pct")]
        valuation_components = [metrics.get("pe_premium_pct"), metrics.get("pb_premium_pct")]
        growth_components = [metrics.get("revenue_growth_pct"), metrics.get("revenue_cagr_like_pct")]
        if not metrics.get("is_bank_like"):
            growth_components.append(metrics.get("fcf_margin_pct"))
        market_components = [metrics.get("one_year_return_pct"), metrics.get("five_year_return_pct")]
        macro_components = [macro.get("fed_funds_rate_pct"), macro.get("inflation_pct"), macro.get("unemployment_pct"), macro.get("gdp_growth_pct")]

        coverages = {
            "profitability": len([value for value in profitability_components if value is not None]) / max(len(profitability_components), 1),
            "stability": len([value for value in stability_components if value is not None]) / max(len(stability_components), 1) if stability_components else 0.0,
            "valuation": len([value for value in valuation_components if value is not None]) / max(len(valuation_components), 1),
            "growth": len([value for value in growth_components if value is not None]) / max(len(growth_components), 1),
            "market": len([value for value in market_components if value is not None]) / max(len(market_components), 1),
            "macro": len([value for value in macro_components if value is not None]) / max(len(macro_components), 1),
        }

        warnings: list[str] = []
        if any(coverage < 1.0 for coverage in coverages.values()):
            warnings.append("Some block scores were capped due to incomplete data")
        if any(0 < coverage < 0.5 for coverage in coverages.values()):
            warnings.append("Low data completeness detected: some block scores were capped conservatively")
        if metrics.get("data_completeness_score") is not None and metrics["data_completeness_score"] < 75:
            warnings.append("Data completeness score is low; several metrics were excluded from scoring")
        if metrics.get("data_reliability_score") is not None and metrics["data_reliability_score"] < 75:
            warnings.append("Data reliability score is low; peer confidence and metric quality reduced block weights")
        return warnings
    def _dedupe_warnings(self, warnings: list[str]) -> list[str]:
        seen: set[str] = set()
        result: list[str] = []
        for warning in warnings:
            if warning not in seen:
                seen.add(warning)
                result.append(warning)
        return result

    def _verdict(self, score: float) -> str:
        if score >= 80:
            return "Сильный кандидат (Strong Candidate)"
        if score >= 65:
            return "Избирательная покупка (Selective Buy)"
        if score >= 50:
            return "Нейтрально (Neutral)"
        return "Повышенный риск (High Caution)"

