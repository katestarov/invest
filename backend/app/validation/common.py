from __future__ import annotations

import csv
from copy import deepcopy
import json
from pathlib import Path
import sys
from typing import Any

from app.core.scoring import get_scoring_config
from app.schemas.analysis import AnalysisResponse
from app.services.analysis_runtime_service import AnalysisService
from app.services.providers.peer_providers import ConfigPeerProvider


BACKEND_ROOT = Path(__file__).resolve().parents[2]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from tests.support.factories import (  # noqa: E402
    make_bank_company_dataset,
    make_fallback_baseline_dataset,
    make_incomplete_company_dataset,
    make_regular_company_dataset,
    make_strong_company_dataset,
)
from tests.support.fakes import (  # noqa: E402
    RecordingPersistence,
    StaticCompanyBundleProvider,
    StaticMacroProvider,
    StaticPeerProvider,
)


def validation_output_dir(name: str) -> Path:
    path = BACKEND_ROOT / "validation_outputs" / name
    path.mkdir(parents=True, exist_ok=True)
    return path


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fieldnames = list(rows[0].keys())
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def _svg_frame(title: str, width: int, height: int, body: str) -> str:
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">'
        f'<rect width="{width}" height="{height}" fill="white"/>'
        f'<text x="{width / 2}" y="28" text-anchor="middle" font-size="18" font-family="Segoe UI, Arial">{title}</text>'
        f"{body}</svg>"
    )


def write_bar_chart_svg(path: Path, title: str, rows: list[tuple[str, float]], *, color: str = "#2563eb") -> None:
    width = 980
    height = 420
    left = 80
    bottom = 330
    chart_width = 840
    chart_height = 240
    max_value = max((value for _, value in rows), default=1.0) or 1.0
    bar_width = chart_width / max(len(rows), 1)
    pieces = [
        f'<line x1="{left}" y1="{bottom}" x2="{left + chart_width}" y2="{bottom}" stroke="#111827" stroke-width="1"/>',
        f'<line x1="{left}" y1="{bottom - chart_height}" x2="{left}" y2="{bottom}" stroke="#111827" stroke-width="1"/>',
    ]
    for index, (label, value) in enumerate(rows):
        bar_height = 0 if max_value <= 0 else (value / max_value) * chart_height
        x = left + (index * bar_width) + 12
        y = bottom - bar_height
        pieces.append(f'<rect x="{x:.1f}" y="{y:.1f}" width="{max(bar_width - 24, 18):.1f}" height="{bar_height:.1f}" fill="{color}" rx="4"/>')
        pieces.append(f'<text x="{x + max(bar_width - 24, 18) / 2:.1f}" y="{bottom + 18}" text-anchor="middle" font-size="10" font-family="Segoe UI, Arial">{label}</text>')
        pieces.append(f'<text x="{x + max(bar_width - 24, 18) / 2:.1f}" y="{max(y - 6, 52):.1f}" text-anchor="middle" font-size="10" font-family="Segoe UI, Arial">{value:.2f}</text>')
    path.write_text(_svg_frame(title, width, height, "".join(pieces)), encoding="utf-8")


def write_line_chart_svg(path: Path, title: str, rows: list[tuple[str, float]], *, color: str = "#dc2626") -> None:
    width = 980
    height = 420
    left = 80
    bottom = 330
    chart_width = 840
    chart_height = 240
    values = [value for _, value in rows]
    min_value = min(values, default=0.0)
    max_value = max(values, default=1.0)
    span = max(max_value - min_value, 1.0)
    step = chart_width / max(len(rows) - 1, 1)
    points: list[str] = []
    pieces = [
        f'<line x1="{left}" y1="{bottom}" x2="{left + chart_width}" y2="{bottom}" stroke="#111827" stroke-width="1"/>',
        f'<line x1="{left}" y1="{bottom - chart_height}" x2="{left}" y2="{bottom}" stroke="#111827" stroke-width="1"/>',
    ]
    for index, (label, value) in enumerate(rows):
        x = left + (index * step)
        y = bottom - (((value - min_value) / span) * chart_height)
        points.append(f"{x:.1f},{y:.1f}")
        pieces.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="4" fill="{color}"/>')
        pieces.append(f'<text x="{x:.1f}" y="{bottom + 18}" text-anchor="middle" font-size="10" font-family="Segoe UI, Arial">{label}</text>')
        pieces.append(f'<text x="{x:.1f}" y="{max(y - 8, 52):.1f}" text-anchor="middle" font-size="10" font-family="Segoe UI, Arial">{value:.2f}</text>')
    if points:
        pieces.insert(2, f'<polyline fill="none" stroke="{color}" stroke-width="2" points="{" ".join(points)}"/>')
    path.write_text(_svg_frame(title, width, height, "".join(pieces)), encoding="utf-8")


def scenario_registry() -> dict[str, dict]:
    return {
        "strong_non_bank": make_strong_company_dataset(),
        "regular_non_bank": make_regular_company_dataset(),
        "bank_like": make_bank_company_dataset(),
        "incomplete_data": make_incomplete_company_dataset(),
        "fallback_baseline": make_fallback_baseline_dataset(),
    }


def clone_dataset(dataset: dict) -> dict:
    return deepcopy(dataset)


def with_weight_override(scoring_config: dict, focus: str, multiplier: float) -> dict:
    overridden = deepcopy(scoring_config)
    weights = deepcopy(overridden["weights"])
    weights[focus] = weights[focus] * multiplier
    total = sum(weights.values())
    overridden["weights"] = {key: value / total for key, value in weights.items()}
    return overridden


def build_mock_analysis_service(
    dataset: dict,
    *,
    scoring_config: dict | None = None,
    peer_group_config: dict | None = None,
    peer_providers: list[object] | None = None,
) -> AnalysisService:
    persistence = RecordingPersistence()
    peer_group_config = deepcopy(peer_group_config) if peer_group_config is not None else {
        "rules": [
            {"sector": "Technology", "industry_contains": ["software"], "tickers": ["ORCL", "CRM", "NOW", "ADBE", "SAP"]},
            {"sector": "Financial Services", "industry_contains": ["bank"], "tickers": ["JPM", "BAC", "USB"]},
            {"sector": "Industrials", "industry_contains": ["industrial", "machinery", "conglomerate"], "tickers": ["GE", "CAT", "HON"]},
            {"sector": "Consumer Cyclical", "industry_contains": ["auto", "automobile"], "tickers": ["GM", "F", "RIVN", "LCID"]},
        ],
        "fallback": {"tickers": ["ORCL", "CRM", "GE", "CAT", "GM"]},
    }
    if peer_providers is None:
        market_cap_snapshots = dataset.get("market_cap_snapshots", {})
        peer_discovery = dataset.get("peer_discovery", {})
        peer_providers = [
            StaticPeerProvider(
                source_name="fmp",
                discovery_map=peer_discovery.get("fmp", {}),
                market_cap_snapshots={ticker: snapshots.get("fmp") for ticker, snapshots in market_cap_snapshots.items() if snapshots.get("fmp")},
            ),
            StaticPeerProvider(
                source_name="finnhub",
                discovery_map=peer_discovery.get("finnhub", {}),
                market_cap_snapshots={ticker: snapshots.get("finnhub") for ticker, snapshots in market_cap_snapshots.items() if snapshots.get("finnhub")},
            ),
            StaticPeerProvider(
                source_name="business_type",
                discovery_map=peer_discovery.get("business_type", {}),
            ),
            StaticPeerProvider(
                source_name="config",
                discovery_map=peer_discovery.get("config", {}),
            ) if peer_discovery.get("config") else ConfigPeerProvider(peer_group_config),
        ]

    return AnalysisService(
        yahoo=StaticCompanyBundleProvider(dataset["yahoo_payloads"], source_name="Yahoo Finance"),
        edgar=StaticCompanyBundleProvider(dataset["edgar_payloads"], source_name="SEC EDGAR"),
        fred=StaticMacroProvider(dataset.get("fred_payload", {}), source_name="FRED"),
        world_bank=StaticMacroProvider(dataset.get("world_bank_payload", {}), source_name="World Bank"),
        peer_providers=peer_providers,
        scoring_config=deepcopy(scoring_config) if scoring_config is not None else deepcopy(get_scoring_config()),
        peer_group_config=peer_group_config,
        session_factory=persistence.session_factory,
        repository_factory=persistence.repository_factory,
    )


def collect_analysis_snapshot(service: AnalysisService, dataset: dict) -> dict[str, Any]:
    ticker = dataset["ticker"]
    yahoo = dataset["yahoo_payloads"][ticker]
    edgar = dataset["edgar_payloads"][ticker]
    profile = service._resolve_company_profile(
        ticker=ticker,
        company=edgar["company"],
        sector=edgar.get("sector"),
        industry=edgar.get("industry"),
        sic=edgar.get("sic"),
    )
    is_bank_like = service._is_bank_like(profile)
    macro = {**dataset.get("fred_payload", {}), **dataset.get("world_bank_payload", {})}
    peers, peer_selection = service._build_peer_group(profile, yahoo, edgar)
    peer_averages = service._build_peer_averages(peers, peer_selection, yahoo, edgar)
    metrics = service._build_silver_metrics(yahoo, edgar, peer_averages | peer_selection, is_bank_like)
    weighted_scores = service._build_weighted_scores(metrics, macro)
    response: AnalysisResponse = service.analyze(ticker)
    breakdown = {item.key: item for item in response.score_breakdown}
    return {
        "ticker": ticker,
        "profile": profile,
        "is_bank_like": is_bank_like,
        "macro": macro,
        "peer_selection": peer_selection,
        "peer_averages": peer_averages,
        "metrics": metrics,
        "weighted_scores": weighted_scores,
        "response": response,
        "breakdown": breakdown,
    }
