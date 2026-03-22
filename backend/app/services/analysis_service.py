from __future__ import annotations

from statistics import mean

from app.schemas.analysis import AnalysisResponse, MacroPoint, MetricCard, PeerRow, ScoreBreakdownItem, TrendPoint
from app.services.data_sources import COMPANIES, EdgarAdapter, FredAdapter, WorldBankAdapter, YahooFinanceAdapter


class AnalysisService:
    def __init__(self) -> None:
        self.yahoo = YahooFinanceAdapter()
        self.edgar = EdgarAdapter()
        self.fred = FredAdapter()
        self.world_bank = WorldBankAdapter()

    def analyze(self, ticker: str) -> AnalysisResponse:
        normalized_ticker = ticker.upper()
        if normalized_ticker not in COMPANIES:
            raise KeyError(f"Ticker '{normalized_ticker}' is not available in the demo dataset.")

        company = COMPANIES[normalized_ticker]
        yahoo = self.yahoo.fetch(normalized_ticker)
        edgar = self.edgar.fetch(normalized_ticker)
        macro = {**self.fred.fetch(), **self.world_bank.fetch()}

        peers = self._get_sector_peers(company["sector"])
        profitability_score = self._profitability_score(yahoo, edgar)
        stability_score = self._stability_score(edgar)
        valuation_score = self._valuation_score(yahoo, peers)
        growth_score = self._growth_score(yahoo, edgar)
        macro_score = self._macro_score(macro)

        weighted_scores = {
            "profitability": (profitability_score, 0.30, "Высокая рентабельность и эффективность капитала."),
            "stability": (stability_score, 0.20, "Баланс и ликвидность остаются управляемыми."),
            "valuation": (valuation_score, 0.20, "Оценка сравнивается с peer-group по сектору."),
            "growth": (growth_score, 0.20, "Смотрим динамику выручки и свободного денежного потока."),
            "macro": (macro_score, 0.10, "Макрофон охлаждает или усиливает итоговую рекомендацию."),
        }

        total_score = round(sum(score * weight for score, weight, _ in weighted_scores.values()), 1)

        return AnalysisResponse(
            ticker=normalized_ticker,
            company=company["company"],
            sector=company["sector"],
            industry=company["industry"],
            score=total_score,
            verdict=self._verdict(total_score),
            narrative=(
                f"{company['company']} получает {total_score}/100. "
                "Сильнее всего выглядят рентабельность и качество денежного потока, "
                "а главным ограничителем остаётся относительная оценка против сектора."
            ),
            metric_cards=self._metric_cards(yahoo, edgar, peers),
            score_breakdown=[
                ScoreBreakdownItem(
                    key=key,
                    label=key.capitalize(),
                    score=round(score, 1),
                    weight=weight,
                    summary=summary,
                )
                for key, (score, weight, summary) in weighted_scores.items()
            ],
            peers=self._peer_rows(peers),
            trends=self._trend_rows(edgar),
            macro=[
                MacroPoint(label="Fed Funds", value=macro["fed_funds_rate_pct"], unit="%"),
                MacroPoint(label="Inflation", value=macro["inflation_pct"], unit="%"),
                MacroPoint(label="Unemployment", value=macro["unemployment_pct"], unit="%"),
                MacroPoint(label="GDP Growth", value=macro["gdp_growth_pct"], unit="%"),
            ],
            assumptions=[
                "В MVP используются демонстрационные адаптеры вместо живых API-вызовов.",
                "Peer-group определяется по сектору компании внутри доступного датасета.",
                "Итоговый скор нормирован до шкалы 0-100 и агрегирован по пяти блокам.",
            ],
            data_sources=[
                self.yahoo.source_name,
                self.edgar.source_name,
                self.fred.source_name,
                self.world_bank.source_name,
            ],
        )

    def _get_sector_peers(self, sector: str) -> list[tuple[str, dict]]:
        return [(ticker, item) for ticker, item in COMPANIES.items() if item["sector"] == sector]

    def _profitability_score(self, yahoo: dict, edgar: dict) -> float:
        roe = min(yahoo["roe_pct"], 80) / 80 * 100
        margin = min(edgar["ebit_margin_pct"], 45) / 45 * 100
        roic = min(edgar["roic_pct"], 40) / 40 * 100
        return min((roe * 0.4) + (margin * 0.3) + (roic * 0.3), 100)

    def _stability_score(self, edgar: dict) -> float:
        debt = max(0, 100 - (edgar["debt_to_equity"] * 35))
        liquidity = min(edgar["current_ratio"], 2.5) / 2.5 * 100
        cash_flow_consistency = 100 if min(edgar["free_cash_flow_bln"]) > 0 else 60
        return (debt * 0.4) + (liquidity * 0.3) + (cash_flow_consistency * 0.3)

    def _valuation_score(self, yahoo: dict, peers: list[tuple[str, dict]]) -> float:
        peer_pes = [peer["yahoo"]["pe_ratio"] for _, peer in peers]
        peer_ps = [peer["yahoo"]["price_to_sales"] for _, peer in peers]
        pe_score = max(0, min(100, 100 - ((yahoo["pe_ratio"] - mean(peer_pes)) * 3.4)))
        ps_score = max(0, min(100, 100 - ((yahoo["price_to_sales"] - mean(peer_ps)) * 6.0)))
        return (pe_score * 0.6) + (ps_score * 0.4)

    def _growth_score(self, yahoo: dict, edgar: dict) -> float:
        revenue_growth = min(max(yahoo["revenue_growth_pct"], 0), 40) / 40 * 100
        revenue_series = edgar["revenue_bln"]
        cagr_like = ((revenue_series[0] / revenue_series[-1]) - 1) * 50
        cagr_score = min(max(cagr_like, 0), 100)
        fcf_growth = 100 if edgar["free_cash_flow_bln"][0] >= edgar["free_cash_flow_bln"][-1] else 65
        return (revenue_growth * 0.45) + (cagr_score * 0.35) + (fcf_growth * 0.2)

    def _macro_score(self, macro: dict) -> float:
        rates = max(0, 100 - (macro["fed_funds_rate_pct"] * 12))
        inflation = max(0, 100 - (abs(macro["inflation_pct"] - 2.0) * 30))
        jobs = max(0, 100 - (macro["unemployment_pct"] * 10))
        growth = min(max(macro["gdp_growth_pct"], 0), 4) / 4 * 100
        return (rates * 0.35) + (inflation * 0.25) + (jobs * 0.2) + (growth * 0.2)

    def _metric_cards(self, yahoo: dict, edgar: dict, peers: list[tuple[str, dict]]) -> list[MetricCard]:
        peer_pe_mean = mean(peer["yahoo"]["pe_ratio"] for _, peer in peers)
        peer_roe_mean = mean(peer["yahoo"]["roe_pct"] for _, peer in peers)
        peer_growth_mean = mean(peer["yahoo"]["revenue_growth_pct"] for _, peer in peers)
        peer_de_mean = mean(peer["edgar"]["debt_to_equity"] for _, peer in peers)

        return [
            MetricCard(
                label="P/E",
                value=yahoo["pe_ratio"],
                benchmark=round(peer_pe_mean, 1),
                direction="lower_better",
                description="Сравнение мультипликатора с peer-group.",
            ),
            MetricCard(
                label="ROE",
                value=yahoo["roe_pct"],
                unit="%",
                benchmark=round(peer_roe_mean, 1),
                direction="higher_better",
                description="Насколько эффективно компания использует капитал.",
            ),
            MetricCard(
                label="Revenue Growth",
                value=yahoo["revenue_growth_pct"],
                unit="%",
                benchmark=round(peer_growth_mean, 1),
                direction="higher_better",
                description="Темп роста относительно сектора.",
            ),
            MetricCard(
                label="Debt/Equity",
                value=edgar["debt_to_equity"],
                benchmark=round(peer_de_mean, 2),
                direction="lower_better",
                description="Нагрузка долга на капитал компании.",
            ),
        ]

    def _peer_rows(self, peers: list[tuple[str, dict]]) -> list[PeerRow]:
        rows: list[PeerRow] = []
        for ticker, item in peers:
            yahoo = item["yahoo"]
            mini_score = round(
                min(
                    100,
                    (
                        min(yahoo["roe_pct"], 60) / 60 * 40
                        + min(max(yahoo["revenue_growth_pct"], 0), 35) / 35 * 30
                        + max(0, 30 - yahoo["pe_ratio"] / 2)
                    ),
                ),
                1,
            )
            rows.append(
                PeerRow(
                    ticker=ticker,
                    company=item["company"],
                    score=mini_score,
                    market_cap_bln=yahoo["market_cap_bln"],
                    pe_ratio=yahoo["pe_ratio"],
                    roe_pct=yahoo["roe_pct"],
                    revenue_growth_pct=yahoo["revenue_growth_pct"],
                )
            )

        rows.sort(key=lambda row: row.score, reverse=True)
        return rows

    def _trend_rows(self, edgar: dict) -> list[TrendPoint]:
        periods = ["FY2024", "FY2023", "FY2022"]
        return [
            TrendPoint(period=period, revenue_bln=revenue, free_cash_flow_bln=fcf)
            for period, revenue, fcf in zip(periods, edgar["revenue_bln"], edgar["free_cash_flow_bln"])
        ]

    def _verdict(self, score: float) -> str:
        if score >= 80:
            return "Strong candidate"
        if score >= 65:
            return "Selective buy"
        if score >= 50:
            return "Neutral"
        return "High caution"
