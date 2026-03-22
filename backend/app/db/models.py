from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Float, Integer, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class BronzeSnapshot(Base):
    __tablename__ = "bronze_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ticker: Mapped[str] = mapped_column(String(16), index=True)
    source: Mapped[str] = mapped_column(String(64), index=True)
    payload: Mapped[dict] = mapped_column(JSON)
    fetched_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)


class SilverAnalysis(Base):
    __tablename__ = "silver_analyses"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ticker: Mapped[str] = mapped_column(String(16), index=True)
    sector: Mapped[str] = mapped_column(String(128))
    industry: Mapped[str] = mapped_column(String(128))
    metrics: Mapped[dict] = mapped_column(JSON)
    peer_snapshot: Mapped[dict] = mapped_column(JSON)
    calculated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)


class GoldScore(Base):
    __tablename__ = "gold_scores"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ticker: Mapped[str] = mapped_column(String(16), index=True)
    score: Mapped[float] = mapped_column(Float)
    verdict: Mapped[str] = mapped_column(String(64))
    narrative: Mapped[str] = mapped_column(Text)
    response_payload: Mapped[dict] = mapped_column(JSON)
    calculated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
