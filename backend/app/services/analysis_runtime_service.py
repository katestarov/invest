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
    coverage_ratio,
    normalize_weights,
    premium_pct,
    round_or_none,
    safe_ratio,
    score_inverse,
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
        }
        macro = {**fred_result.payload, **world_bank_result.payload}
        peers = self._build_peer_group(company_profile)
        peer_averages = summarize_peer_averages(peers)
        silver_metrics = self._build_silver_metrics(yahoo_result.payload, edgar_result.payload, peer_averages)
        warnings.extend(self._build_data_quality_warnings(edgar_result.payload, macro, peers))
        weighted_scores = self._build_weighted_scores(silver_metrics, macro)
        total_score = round(sum(score * weight for score, weight, _ in weighted_scores.values()), 1)
        verdict = self._verdict(total_score)
        narrative = self._build_narrative(company_profile["company"], total_score, weighted_scores)
        warnings = self._dedupe_warnings(warnings)

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

    def _build_peer_group(self, company_profile: dict) -> list[dict]:
        sector = company_profile["sector"]
        industry = company_profile["industry"]
        matched_rule = None
        for rule in self.peer_group_config["rules"]:
            sector_match = rule["sector"] == sector
            industry_match = any(fragment.lower() in industry.lower() for fragment in rule["industry_contains"])
            if sector_match or industry_match:
                matched_rule = rule
                break

        tickers = (matched_rule or self.peer_group_config["fallback"])["tickers"][:6]
        rows: list[dict] = []
        for peer_ticker in tickers:
            try:
                yahoo_payload = self.yahoo.fetch_company_bundle(peer_ticker).payload
                edgar_payload = self.edgar.fetch_company_bundle(peer_ticker).payload
                rows.append(
                    {
                        "ticker": peer_ticker,
                        "company": yahoo_payload.get("company") or edgar_payload.get("company") or peer_ticker,
                        "sector": edgar_payload.get("sector", "Unknown"),
                        "industry": edgar_payload.get("industry", "Unknown"),
                        "market_cap_bln": self._market_cap_bln(yahoo_payload, edgar_payload),
                        "pe_ratio": self._pe_ratio(yahoo_payload, edgar_payload),
                        "pb_ratio": self._pb_ratio(yahoo_payload, edgar_payload),
                        "roe_pct": self._roe_pct(edgar_payload),
                        "revenue_growth_pct": self._revenue_growth_pct(edgar_payload),
                        "debt_to_equity": edgar_payload["debt_to_equity"],
                    }
                )
            except Exception:
                continue
            if len(rows) >= 6:
                break
        return rows

    def _build_silver_metrics(self, yahoo: dict, edgar: dict, peers: dict) -> dict:
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
            "pe_premium_pct": pe_premium_pct,
            "pb_premium_pct": pb_premium_pct,
        }

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
        if len(history) >= 2:
            ratio = safe_ratio(history[0], history[1])
            return round_or_none((ratio - 1) * 100 if ratio is not None else None, 2)
        return None

    def _build_weighted_scores(self, metrics: dict, macro: dict) -> dict[str, tuple[float, float, str]]:
        weights = self.scoring_config["weights"]
        caps = self.scoring_config["caps"]

        profitability_components = [
            (score_positive(metrics["roe_pct"], caps["roe_pct"]), 0.4),
            (score_positive(metrics["roic_pct"], caps["roic_pct"]), 0.35),
            (score_positive(metrics["ebit_margin_pct"], caps["ebit_margin_pct"]), 0.25),
        ]
        stability_components = [
            (score_inverse(metrics["debt_to_equity"], caps["debt_to_equity"]), 0.55),
            (score_positive(metrics["current_ratio"], caps["current_ratio"]), 0.20),
            (score_positive(metrics["fcf_margin_pct"], caps["fcf_margin_pct"]), 0.25),
        ]
        valuation_components = [
            (score_inverse(max(metrics["pe_premium_pct"], 0.0) if metrics["pe_premium_pct"] is not None else None, caps["pe_premium_pct"]), 0.55),
            (score_inverse(max(metrics["pb_premium_pct"], 0.0) if metrics["pb_premium_pct"] is not None else None, caps["pb_premium_pct"]), 0.45),
        ]
        growth_components = [
            (score_positive(metrics["revenue_growth_pct"], caps["revenue_growth_pct"]), 0.55),
            (score_positive(metrics["revenue_cagr_like_pct"], caps["revenue_growth_pct"]), 0.20),
            (score_positive(metrics["fcf_margin_pct"], caps["fcf_margin_pct"]), 0.25),
        ]
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
                comparison_label=self._comparison_label(metrics["pe_ratio"], peers["pe_ratio"], "lower_better"),
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
                comparison_label=self._comparison_label(metrics["roe_pct"], peers["roe_pct"], "higher_better"),
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
                comparison_label=self._comparison_label(metrics["revenue_growth_pct"], peers["revenue_growth_pct"], "higher_better"),
                description="Сравнение темпа роста выручки с компаниями того же сектора.",
            ),
            MetricCard(
                label="Долг/Капитал (Debt/Equity)",
                value=round_or_none(metrics["debt_to_equity"], 2),
                benchmark=round_or_none(peers["debt_to_equity"], 2),
                direction="lower_better",
                display_value=self._display_metric(metrics["debt_to_equity"]),
                display_benchmark=self._display_metric(peers["debt_to_equity"]),
                comparison_label=self._comparison_label(metrics["debt_to_equity"], peers["debt_to_equity"], "lower_better"),
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

    def _build_narrative(self, company: str, total_score: float, weighted_scores: dict[str, tuple[float, float, str]]) -> str:
        eligible = [(key, item) for key, item in weighted_scores.items() if item[1] > 0]
        strongest = max(eligible, key=lambda item: item[1][0] * item[1][1]) if eligible else next(iter(weighted_scores.items()))
        weakest = min(eligible, key=lambda item: item[1][0] * item[1][1]) if eligible else next(iter(weighted_scores.items()))
        return (
            f"{company} получает итоговую оценку {total_score}/100. "
            f"Сильнее всего компанию поддерживает блок «{self.scoring_config['labels'][strongest[0]]}», "
            f"а главным ограничителем сейчас остается «{self.scoring_config['labels'][weakest[0]]}»."
        )

    def _display_metric(self, value: float | None) -> str:
        return f"{round(value, 2):g}" if value is not None else "N/A"

    def _comparison_label(self, value: float | None, benchmark: float | None, direction: str) -> str:
        if value is None or benchmark is None:
            return "Недостаточно данных"
        is_positive = value >= benchmark if direction == "higher_better" else value <= benchmark
        return "Лучше peers" if is_positive else "Слабее peers"

    def _build_data_quality_warnings(self, edgar: dict, macro: dict, peers: list[dict]) -> list[str]:
        warnings: list[str] = []
        if edgar.get("equity_bln") is not None and edgar.get("equity_bln") <= 0:
            warnings.append("Negative equity detected: ROE, Debt/Equity and P/B may be unreliable")
        fred_missing = [
            macro.get("fed_funds_rate_pct"),
            macro.get("inflation_pct"),
            macro.get("unemployment_pct"),
        ]
        if any(value is None for value in fred_missing):
            warnings.append("FRED data unavailable: macro score calculated on partial data")
        if any(
            row.get("pe_ratio") is None
            or row.get("pb_ratio") is None
            or row.get("roe_pct") is None
            or row.get("debt_to_equity") is None
            for row in peers
        ):
            warnings.append("Some peer metrics were excluded due to invalid or non-interpretable values")
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

