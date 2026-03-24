from __future__ import annotations

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
        normalized_ticker = ticker.upper().strip()
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
        warnings.extend(self._build_data_quality_warnings(edgar_result.payload, macro, peer_averages | peer_selection, is_bank_like))
        warnings.extend(self._build_completeness_warnings(silver_metrics, macro))
        weighted_scores = self._build_weighted_scores(silver_metrics, macro)
        total_score = round(sum(score * weight for score, weight, _ in weighted_scores.values()), 1)
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
                    score=round(score, 1),
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

    def clear_cache(self) -> None:
        self.analysis_cache.clear()

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
        finally:
            session.close()

    def _build_peer_group(self, company_profile: dict, yahoo: dict, edgar: dict) -> tuple[list[dict], dict]:
        cache_key = f"{company_profile.get('ticker')}|{company_profile.get('sector')}|{company_profile.get('industry')}|{company_profile.get('sic')}"
        cached = self.peer_group_cache.get(cache_key)
        if cached is not None:
            return cached

        company_market_cap = self._market_cap_bln(yahoo, edgar)
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
        revenue_history = edgar.get("revenue_bln", [])
        revenue_cagr_like = None
        if len(revenue_history) >= 2:
            cagr_ratio = safe_ratio(revenue_history[0], revenue_history[-1])
            revenue_cagr_like = round_or_none((cagr_ratio - 1) * 100 if cagr_ratio is not None else None, 2)

        market_cap_bln = self._market_cap_bln(yahoo, edgar)
        pe_ratio = self._pe_ratio(yahoo, edgar)
        pb_ratio = self._pb_ratio(yahoo, edgar)
        roe_pct = self._roe_pct(edgar)
        revenue_growth_pct = self._revenue_growth_pct(edgar)

        pe_premium_pct = round_or_none(premium_pct(pe_ratio, peers["pe_ratio"]), 2)
        pb_premium_pct = round_or_none(premium_pct(pb_ratio, peers["pb_ratio"]), 2)

        return {
            "market_cap_bln": market_cap_bln,
            "pe_ratio": pe_ratio,
            "pb_ratio": pb_ratio,
            "roe_pct": roe_pct,
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
            "peer_confidence": peers.get("peer_confidence", peers.get("peer_selection_confidence", "low")),
            "peer_count_total": peers.get("peer_count_total", peers.get("peer_count", 0)),
            "peer_count_usable": peers.get("peer_count_usable", 0),
            "valuation_baseline_mode": peers.get("valuation_baseline_mode", "peer"),
            "peer_selection_confidence": peers.get("peer_selection_confidence", "low"),
            "peer_selection_reason": peers.get("peer_selection_reason", "fallback"),
            "pe_premium_pct": pe_premium_pct,
            "pb_premium_pct": pb_premium_pct,
            "is_bank_like": is_bank_like,
        }

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
                        "roe_pct": self._roe_pct(edgar_payload),
                        "revenue_growth_pct": self._revenue_growth_pct(edgar_payload),
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
        if len(strict_usable) >= 2:
            peer_selection_mode = "strict"
            chosen_ranked = strict_ranked + extended_ranked + fallback_ranked
            usable_ranked = strict_usable
        elif len(extended_usable) >= 2:
            peer_selection_mode = "extended"
            chosen_ranked = strict_ranked + extended_ranked + fallback_ranked
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
            quality_passed = bool(selected and average_score >= 2.0 and len(usable_selected) >= 2)
        if peer_selection_mode == "fallback":
            quality_passed = False
        sample_mode = "excluded"
        if len(usable_selected) >= self.peer_target_count:
            sample_mode = "full"
        elif len(usable_selected) >= self.peer_min_valid_count:
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
        reason = "manual peer group" if manual_valid >= 3 else "auto-selected peers"
        return selected, {
            "peer_selection_mode": peer_selection_mode,
            "peer_confidence": confidence,
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
        if selection_meta.get("peer_selection_mode") == "extended" and selection_meta.get("peer_count_usable", 0) >= self.peer_min_valid_count:
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
        if compatibility == "REJECT" and self._is_mega_cap_tech_pair(company_profile.get("ticker"), candidate.get("ticker")):
            return "WEAK"
        return compatibility

    def _industry_is_close(self, company_profile: dict, candidate: dict) -> bool:
        company_sic = str(company_profile.get("sic") or "")
        candidate_sic = str(candidate.get("sic") or "")
        company_industry = (company_profile.get("industry") or "").lower()
        candidate_industry = (candidate.get("industry") or "").lower()
        if company_sic and candidate_sic and (company_sic[:4] == candidate_sic[:4] or company_sic[:3] == candidate_sic[:3]):
            return True
        if company_industry and candidate_industry:
            if company_industry in candidate_industry or candidate_industry in company_industry:
                return True
            company_tokens = [token for token in company_industry.replace("&", " ").replace("-", " ").split() if len(token) > 4]
            return any(token in candidate_industry for token in company_tokens)
        return False

    def _special_peer_universe(self, company_profile: dict) -> list[str]:
        ticker = str(company_profile.get("ticker") or "").upper()
        business_type = str(company_profile.get("business_type") or "").upper()
        if ticker in {"AAPL", "MSFT", "GOOGL", "AMZN", "META", "NVDA"}:
            return ["AAPL", "MSFT", "GOOGL", "AMZN", "META", "NVDA", "ORCL", "CRM", "ADBE", "SAP"]
        if business_type == "BANK":
            return ["JPM", "BAC", "WFC", "C", "USB", "PNC", "GS", "MS"]
        if business_type == "PAYMENTS":
            return ["V", "MA", "AXP", "PYPL", "FI", "GPN", "COF", "DFS"]
        return []

    def _is_mega_cap_tech_pair(self, ticker_a: str | None, ticker_b: str | None) -> bool:
        mega_cap_tech = {"AAPL", "MSFT", "GOOGL", "AMZN", "META", "NVDA"}
        return str(ticker_a or "").upper() in mega_cap_tech and str(ticker_b or "").upper() in mega_cap_tech

    def _peer_match_score(self, company_profile: dict, company_market_cap: float | None, candidate: dict) -> float:
        score = 0.0
        company_sic = str(company_profile.get("sic") or "")
        candidate_sic = str(candidate.get("sic") or "")
        company_industry = (company_profile.get("industry") or "").lower()
        candidate_industry = (candidate.get("industry") or "").lower()
        company_business_type = company_profile.get("business_type", "OTHER")
        candidate_business_type = self._candidate_business_type(candidate)
        compatibility = business_type_compatibility(company_business_type, candidate_business_type)
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

        if company_industry and candidate_industry:
            if company_industry in candidate_industry or candidate_industry in company_industry:
                score += 2.0
            elif any(token in candidate_industry for token in company_industry.split() if len(token) > 4):
                score += 1.0

        if company_profile.get("sector") == candidate.get("sector"):
            score += 1.0

        candidate_market_cap = candidate.get("market_cap_bln")
        if company_market_cap and candidate_market_cap and company_market_cap > 0 and candidate_market_cap > 0:
            distance = abs(company_market_cap - candidate_market_cap) / max(company_market_cap, candidate_market_cap)
            score -= min(distance, 1.5)

        score -= max(0, 4 - self._peer_data_quality(candidate)) * 0.35
        if compatibility == "WEAK":
            score -= 1.25
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
        return sum(
            1
            for key in ("pe_ratio", "pb_ratio", "roe_pct", "revenue_growth_pct", "debt_to_equity")
            if candidate.get(key) is not None
        )

    def _build_peer_averages(self, peers: list[dict], peer_selection: dict, yahoo: dict, edgar: dict) -> dict:
        usable_tickers = set(peer_selection.get("usable_peer_tickers", []))
        usable_rows = [row for row in peers if row.get("ticker") in usable_tickers]
        baseline_rows = usable_rows or peers
        averages = summarize_peer_averages(baseline_rows)
        averages["valuation_baseline_mode"] = "peer"

        company_pe = self._pe_ratio(yahoo, edgar)
        company_pb = self._pb_ratio(yahoo, edgar)
        neutral_used = False
        if averages.get("pe_ratio") is None or averages.get("pe_ratio_valid_count", 0) < 2:
            averages["pe_ratio"] = company_pe
            averages["pe_ratio_baseline_noisy"] = True
            neutral_used = neutral_used or company_pe is not None
        if averages.get("pb_ratio") is None or averages.get("pb_ratio_valid_count", 0) < 2:
            averages["pb_ratio"] = company_pb
            averages["pb_ratio_baseline_noisy"] = True
            neutral_used = neutral_used or company_pb is not None
        if neutral_used:
            averages["valuation_baseline_mode"] = "neutral"
        return averages

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

    def _build_weighted_scores(self, metrics: dict, macro: dict) -> dict[str, tuple[float, float, str]]:
        weights = self.scoring_config["weights"]
        caps = self.scoring_config["caps"]

        profitability_components = [
            (score_positive(metrics["roe_pct"], caps["roe_pct"]), 0.4),
            (score_positive(metrics["roic_pct"], caps["roic_pct"]), 0.35),
            (score_positive(metrics["ebit_margin_pct"], caps["ebit_margin_pct"]), 0.25),
        ]
        stability_components = [] if metrics.get("is_bank_like") else [
            (score_inverse(metrics["debt_to_equity"], caps["debt_to_equity"]), 0.55),
            (score_positive(metrics["current_ratio"], caps["current_ratio"]), 0.20),
            (score_positive(metrics["fcf_margin_pct"], caps["fcf_margin_pct"]), 0.25),
        ]
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
            "profitability": weights["profitability"] * coverage_ratio(profitability_components),
            "stability": weights["stability"] * coverage_ratio(stability_components),
            "valuation": weights["valuation"] * coverage_ratio(valuation_components),
            "growth": weights["growth"] * coverage_ratio(growth_components),
            "market": weights["market"] * coverage_ratio(market_components),
            "macro": weights["macro"] * coverage_ratio(macro_components),
        }
        effective_weights = normalize_weights(block_scores, weighted_config)

        return {
            "profitability": (round(profitability or 0.0, 2), effective_weights["profitability"], "Высокие ROE, ROIC и операционная маржа поддерживают качество бизнеса."),
            "stability": (round(stability or 0.0, 2), effective_weights["stability"], "Смотрим долговую нагрузку, ликвидность и запас денежного потока."),
            "valuation": (round(valuation or 0.0, 2), effective_weights["valuation"], "Оцениваем премию или дисконт по P/E и P/B против peer-group."),
            "growth": (round(growth or 0.0, 2), effective_weights["growth"], "Учитываем рост выручки, ее динамику и качество FCF."),
            "market": (round(market or 0.0, 2), effective_weights["market"], "Рыночный импульс учитывает доходность цены за 1 и 5 лет."),
            "macro": (round(macro_score or 0.0, 2), effective_weights["macro"], "Макросреда корректирует оценку с учетом ставок, инфляции и роста экономики."),
        }

    def _metric_cards(self, metrics: dict, peers: dict) -> list[MetricCard]:
        return [
            MetricCard(
                label="Коэффициент P/E (Price/Earnings)",
                value=round_or_none(metrics["pe_ratio"], 2),
                benchmark=round_or_none(peers["pe_ratio"], 2),
                direction="lower_better",
                display_value=self._display_metric(metrics["pe_ratio"]),
                display_benchmark=self._display_metric(peers["pe_ratio"]),
                comparison_label=self._comparison_label(
                    metrics["pe_ratio"],
                    peers["pe_ratio"],
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
                    bool(metrics.get("peer_roe_noisy", False)) or metrics.get("peer_selection_confidence") == "low",
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
                            (score_positive(item["roe_pct"], 50), 0.4),
                            (score_positive(item["revenue_growth_pct"], 35), 0.35),
                            (score_inverse(item["pe_ratio"], 60), 0.25),
                        ]
                    )
                    or 0.0,
                    1,
                ),
                market_cap_bln=round(item["market_cap_bln"] or 0.0, 2),
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
        eligible = [(key, item) for key, item in weighted_scores.items() if item[1] > 0]
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
    def _build_data_quality_warnings(self, edgar: dict, macro: dict, peers: dict, is_bank_like: bool) -> list[str]:
        warnings: list[str] = []
        if edgar.get("equity_bln") is not None and edgar.get("equity_bln") <= 0:
            warnings.append("Negative equity detected: ROE, Debt/Equity and P/B may be unreliable")
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
        if any(value is None for value in fred_missing):
            warnings.append("FRED data unavailable: macro score calculated on partial data")
        if any(
            peers.get(key, 0) < 3
            for key in (
                "pe_ratio_valid_count",
                "pb_ratio_valid_count",
                "roe_pct_valid_count",
                "revenue_growth_pct_valid_count",
            )
        ):
            warnings.append("Peer baseline is sparse or affected by outliers; comparative valuation may be noisy")
        if any(peers.get(key, False) for key in ("pe_ratio_baseline_noisy", "pb_ratio_baseline_noisy")):
            warnings.append("Low-confidence valuation comparison: peer baseline is noisy")
        if peers.get("peer_selection_confidence") == "low":
            warnings.append("Peer baseline is noisy or based on fallback selection")
            warnings.append("Low-confidence peer comparison")
        if peers.get("peer_group_sample_limited"):
            warnings.append("Peer comparison based on limited peer set")
            warnings.append("Peer averages computed from small sample size")
        if peers.get("peer_selection_mode") == "extended":
            warnings.append("Peer group was downgraded to extended mode due to weak strict peer match")
        if peers.get("peer_selection_mode") == "fallback":
            warnings.append("Low-confidence peer group: fallback basket was used")
        if peers.get("valuation_baseline_mode") == "neutral":
            warnings.append("Valuation used neutral baseline because usable peer valuation data was insufficient")
        if peers.get("peer_expansion_level", 0) >= 2:
            warnings.append("Peer set expanded using lower-confidence candidates within same business type")
        if 0 < peers.get("peer_count", 0) < peers.get("target_peer_count", self.peer_target_count):
            warnings.append("Peer target count was not reached; comparison is based on partial peer universe")
        if peers.get("peer_selection_source") == "business_type":
            warnings.append("Peer selection used business-type fallback universe")
        if peers.get("peer_selection_source") in {"fmp", "finnhub"}:
            warnings.append("Peers were selected via external API and filtered locally")
        if peers.get("peer_selection_source") == "config":
            warnings.append("Peer baseline used fallback source due to insufficient API peers")
            if peers.get("peer_selection_confidence") == "low":
                warnings.append("Peer selection used broad fallback because no reliable API peers were found")
        if peers.get("peer_group_quality_passed") is False:
            warnings.append("No reliable peers found within inferred business type")
            warnings.append("Comparative metrics were downgraded due to weak peer relevance")
            if peers.get("peer_selection_source") == "config":
                warnings.append("Broad fallback peers were rejected for scoring use")
        if any(
            peers.get(key, 0) == 0
            for key in ("pe_ratio_valid_count", "pb_ratio_valid_count", "roe_pct_valid_count", "debt_to_equity_valid_count")
        ):
            warnings.append("Some peer metrics were excluded due to invalid or non-interpretable values")
        if peers.get("incompatible_peer_count", 0) > 0:
            warnings.append("Some peers were excluded due to incompatible business models")
        if is_bank_like:
            warnings.append("Bank-specific scoring rules were applied; some generic corporate metrics were excluded")
        if peers.get("business_type_confidence") in {"medium", "low"}:
            warnings.append("Business type was inferred from rule-based classification")
        return warnings

    def _is_bank_like(self, company_profile: dict) -> bool:
        return is_bank_like_company(company_profile.get("sector"), company_profile.get("industry"))
    def _build_completeness_warnings(self, metrics: dict, macro: dict) -> list[str]:
        profitability_components = [metrics.get("roe_pct"), metrics.get("roic_pct"), metrics.get("ebit_margin_pct")]
        stability_components = [] if metrics.get("is_bank_like") else [metrics.get("debt_to_equity"), metrics.get("current_ratio"), metrics.get("fcf_margin_pct")]
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

