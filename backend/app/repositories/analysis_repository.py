from __future__ import annotations

from sqlalchemy.orm import Session

from app.db.models import BronzeSnapshot, GoldScore, SilverAnalysis


class AnalysisRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def save_bronze(self, ticker: str, source: str, payload: dict) -> None:
        self.session.add(BronzeSnapshot(ticker=ticker, source=source, payload=payload))

    def save_silver(self, ticker: str, sector: str, industry: str, metrics: dict, peer_snapshot: dict) -> None:
        self.session.add(
            SilverAnalysis(
                ticker=ticker,
                sector=sector,
                industry=industry,
                metrics=metrics,
                peer_snapshot=peer_snapshot,
            )
        )

    def save_gold(self, ticker: str, score: float, verdict: str, narrative: str, response_payload: dict) -> None:
        self.session.add(
            GoldScore(
                ticker=ticker,
                score=score,
                verdict=verdict,
                narrative=narrative,
                response_payload=response_payload,
            )
        )

    def commit(self) -> None:
        self.session.commit()
