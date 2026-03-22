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
        weighted_scores = self._build_weighted_scores(silver_metrics, macro)
        total_score = round(sum(score * weight for score, weight, _ in weighted_scores.values()), 1)
        verdict = self._verdict(total_score)
        narrative = self._build_narrative(company_profile["company"], total_score, weighted_scores)

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
                MacroPoint(label="Ключевая ставка ФРС (Fed Funds)", value=macro.get("fed_funds_rate_pct", 0.0), unit="%", source="FRED"),
                MacroPoint(label="Инфляция CPI (Inflation)", value=macro.get("inflation_pct", 0.0), unit="%", source="FRED"),
                MacroPoint(label="Безработица (Unemployment)", value=macro.get("unemployment_pct", 0.0), unit="%", source="FRED"),
                MacroPoint(label="Рост ВВП США (GDP Growth)", value=macro.get("gdp_growth_pct", 0.0), unit="%", source="World Bank"),
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
        revenue_cagr_like = 0.0
        if len(revenue_history) >= 2 and revenue_history[-1] > 0:
            revenue_cagr_like = ((revenue_history[0] / revenue_history[-1]) - 1) * 100

        market_cap_bln = self._market_cap_bln(yahoo, edgar)
        pe_ratio = self._pe_ratio(yahoo, edgar)
        pb_ratio = self._pb_ratio(yahoo, edgar)
        roe_pct = self._roe_pct(edgar)
        revenue_growth_pct = self._revenue_growth_pct(edgar)

        pe_premium_pct = ((pe_ratio / peers["pe_ratio"]) - 1) * 100 if peers["pe_ratio"] else 0.0
        pb_premium_pct = ((pb_ratio / peers["pb_ratio"]) - 1) * 100 if peers["pb_ratio"] else 0.0

        return {
            "market_cap_bln": market_cap_bln,
            "pe_ratio": pe_ratio,
            "pb_ratio": pb_ratio,
            "roe_pct": roe_pct,
            "roic_pct": edgar.get("roic_pct", 0.0),
            "ebit_margin_pct": edgar.get("ebit_margin_pct", 0.0),
            "revenue_growth_pct": revenue_growth_pct,
            "revenue_cagr_like_pct": revenue_cagr_like,
            "fcf_margin_pct": edgar.get("fcf_margin_pct", 0.0),
            "debt_to_equity": edgar.get("debt_to_equity", 0.0),
            "current_ratio": edgar.get("current_ratio", 0.0),
            "one_year_return_pct": yahoo.get("one_year_return_pct", 0.0),
            "five_year_return_pct": yahoo.get("five_year_return_pct", 0.0),
            "peer_pe_avg": peers["pe_ratio"],
            "peer_pb_avg": peers["pb_ratio"],
            "peer_roe_avg": peers["roe_pct"],
            "peer_growth_avg": peers["revenue_growth_pct"],
            "peer_debt_avg": peers["debt_to_equity"],
            "pe_premium_pct": pe_premium_pct,
            "pb_premium_pct": pb_premium_pct,
        }

    def _market_cap_bln(self, yahoo: dict, edgar: dict) -> float:
        price = yahoo.get("current_price", 0.0)
        shares_mln = edgar.get("shares_outstanding_mln", 0.0)
        return round((price * shares_mln) / 1000, 2) if price and shares_mln else 0.0

    def _pe_ratio(self, yahoo: dict, edgar: dict) -> float:
        market_cap = self._market_cap_bln(yahoo, edgar)
        net_income = edgar.get("net_income_bln", 0.0)
        return round(market_cap / net_income, 2) if market_cap and net_income > 0 else 0.0

    def _pb_ratio(self, yahoo: dict, edgar: dict) -> float:
        market_cap = self._market_cap_bln(yahoo, edgar)
        equity = edgar.get("equity_bln", 0.0)
        return round(market_cap / equity, 2) if market_cap and equity > 0 else 0.0

    def _roe_pct(self, edgar: dict) -> float:
        net_income = edgar.get("net_income_bln", 0.0)
        equity = edgar.get("equity_bln", 0.0)
        return round((net_income / equity) * 100, 2) if equity > 0 else 0.0

    def _revenue_growth_pct(self, edgar: dict) -> float:
        history = edgar.get("revenue_bln", [])
        if len(history) >= 2 and history[1] > 0:
            return round(((history[0] / history[1]) - 1) * 100, 2)
        return 0.0

    def _build_weighted_scores(self, metrics: dict, macro: dict) -> dict[str, tuple[float, float, str]]:
        weights = self.scoring_config["weights"]
        caps = self.scoring_config["caps"]

        profitability = (
            self._score_positive(metrics["roe_pct"], caps["roe_pct"], 0.4)
            + self._score_positive(metrics["roic_pct"], caps["roic_pct"], 0.35)
            + self._score_positive(metrics["ebit_margin_pct"], caps["ebit_margin_pct"], 0.25)
        )
        stability = (
            self._score_inverse(metrics["debt_to_equity"], caps["debt_to_equity"], 0.55)
            + self._score_positive(metrics["current_ratio"], caps["current_ratio"], 0.20)
            + self._score_positive(metrics["fcf_margin_pct"], caps["fcf_margin_pct"], 0.25)
        )
        valuation = (
            self._score_inverse(max(metrics["pe_premium_pct"], 0.0), caps["pe_premium_pct"], 0.55)
            + self._score_inverse(max(metrics["pb_premium_pct"], 0.0), caps["pb_premium_pct"], 0.45)
        )
        growth = (
            self._score_positive(metrics["revenue_growth_pct"], caps["revenue_growth_pct"], 0.55)
            + self._score_positive(metrics["revenue_cagr_like_pct"], caps["revenue_growth_pct"], 0.20)
            + self._score_positive(metrics["fcf_margin_pct"], caps["fcf_margin_pct"], 0.25)
        )
        market = (
            self._score_positive(metrics["one_year_return_pct"], 60, 0.45)
            + self._score_positive(metrics["five_year_return_pct"], caps["five_year_return_pct"], 0.55)
        )
        macro_score = self._macro_score(macro)

        return {
            "profitability": (round(profitability, 2), weights["profitability"], "Высокие ROE, ROIC и операционная маржа поддерживают качество бизнеса."),
            "stability": (round(stability, 2), weights["stability"], "Смотрим долговую нагрузку, ликвидность и запас денежного потока."),
            "valuation": (round(valuation, 2), weights["valuation"], "Оцениваем премию или дисконт по P/E и P/B против peer-group."),
            "growth": (round(growth, 2), weights["growth"], "Учитываем рост выручки, ее динамику и качество FCF."),
            "market": (round(market, 2), weights["market"], "Рыночный импульс учитывает доходность цены за 1 и 5 лет."),
            "macro": (round(macro_score, 2), weights["macro"], "Макросреда корректирует оценку с учетом ставок, инфляции и роста экономики."),
        }

    def _metric_cards(self, metrics: dict, peers: dict) -> list[MetricCard]:
        return [
            MetricCard(
                label="Коэффициент P/E (Price/Earnings)",
                value=round(metrics["pe_ratio"], 2),
                benchmark=round(peers["pe_ratio"], 2),
                direction="lower_better",
                description="Меньшее значение обычно означает более умеренную оценку относительно прибыли.",
            ),
            MetricCard(
                label="Рентабельность капитала ROE (Return on Equity)",
                value=round(metrics["roe_pct"], 2),
                unit="%",
                benchmark=round(peers["roe_pct"], 2),
                direction="higher_better",
                description="Показывает, насколько эффективно компания генерирует прибыль на капитал акционеров.",
            ),
            MetricCard(
                label="Рост выручки (Revenue Growth)",
                value=round(metrics["revenue_growth_pct"], 2),
                unit="%",
                benchmark=round(peers["revenue_growth_pct"], 2),
                direction="higher_better",
                description="Сравнение темпа роста выручки с компаниями того же сектора.",
            ),
            MetricCard(
                label="Долг/Капитал (Debt/Equity)",
                value=round(metrics["debt_to_equity"], 2),
                benchmark=round(peers["debt_to_equity"], 2),
                direction="lower_better",
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
                    self._score_positive(item["roe_pct"], 50, 0.4)
                    + self._score_positive(item["revenue_growth_pct"], 35, 0.35)
                    + self._score_inverse(item["pe_ratio"], 60, 0.25),
                    1,
                ),
                market_cap_bln=round(item["market_cap_bln"], 2),
                pe_ratio=round(item["pe_ratio"], 2),
                roe_pct=round(item["roe_pct"], 2),
                revenue_growth_pct=round(item["revenue_growth_pct"], 2),
            )
            for item in peers
        ]
        rows.sort(key=lambda item: item.score, reverse=True)
        return rows[:8]

    def _score_positive(self, value: float, cap: float, weight: float) -> float:
        normalized = min(max(value, 0.0), cap) / cap * 100 if cap else 0.0
        return normalized * weight

    def _score_inverse(self, value: float, cap: float, weight: float) -> float:
        normalized = max(0.0, 100 - (min(max(value, 0.0), cap) / cap * 100)) if cap else 0.0
        return normalized * weight

    def _macro_score(self, macro: dict) -> float:
        fed_component = max(0.0, 100 - (macro.get("fed_funds_rate_pct", 0.0) * 12))
        inflation_component = max(0.0, 100 - (abs(macro.get("inflation_pct", 2.0) - 2.0) * 25))
        unemployment_component = max(0.0, 100 - (macro.get("unemployment_pct", 0.0) * 10))
        gdp_component = min(max(macro.get("gdp_growth_pct", 0.0), 0.0), 4.0) / 4.0 * 100
        return fed_component * 0.30 + inflation_component * 0.25 + unemployment_component * 0.20 + gdp_component * 0.25

    def _build_narrative(self, company: str, total_score: float, weighted_scores: dict[str, tuple[float, float, str]]) -> str:
        strongest = max(weighted_scores.items(), key=lambda item: item[1][0] * item[1][1])
        weakest = min(weighted_scores.items(), key=lambda item: item[1][0] * item[1][1])
        return (
            f"{company} получает итоговую оценку {total_score}/100. "
            f"Сильнее всего компанию поддерживает блок «{self.scoring_config['labels'][strongest[0]]}», "
            f"а главным ограничителем сейчас остается «{self.scoring_config['labels'][weakest[0]]}»."
        )

    def _verdict(self, score: float) -> str:
        if score >= 80:
            return "Сильный кандидат (Strong Candidate)"
        if score >= 65:
            return "Избирательная покупка (Selective Buy)"
        if score >= 50:
            return "Нейтрально (Neutral)"
        return "Повышенный риск (High Caution)"

