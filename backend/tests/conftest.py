from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

os.environ.setdefault("DATABASE_URL", "sqlite+pysqlite:///:memory:")
os.environ.setdefault("APP_ENV", "test")

# Preload real modules before legacy unittest-style tests try to replace them via sys.modules.setdefault.
import sqlalchemy  # noqa: F401
import sqlalchemy.orm  # noqa: F401
import app.core.database  # noqa: F401
import app.repositories.analysis_repository  # noqa: F401

from app.api import routes
from app.main import app
from app.services.analysis_runtime_service import AnalysisService
from app.services.providers.peer_providers import ConfigPeerProvider
from tests.support.factories import (
    make_bank_company_dataset,
    make_fallback_baseline_dataset,
    make_incomplete_company_dataset,
    make_regular_company_dataset,
    make_strong_company_dataset,
    make_yahoo_payload,
    make_edgar_payload,
)
from tests.support.fakes import (
    RecordingPersistence,
    StaticCompanyBundleProvider,
    StaticMacroProvider,
    StaticPeerProvider,
)
from tests.support.raw_payloads import (
    make_finnhub_peer_payload,
    make_fmp_peer_payload,
    make_fred_series_payload,
    make_sec_facts_payload,
    make_sec_submissions_payload,
    make_world_bank_payload,
    make_yahoo_chart_payload,
    make_yahoo_quote_payload,
)


@pytest.fixture
def recording_persistence() -> RecordingPersistence:
    return RecordingPersistence()


@pytest.fixture
def yahoo_payload_factory():
    return make_yahoo_payload


@pytest.fixture
def edgar_payload_factory():
    return make_edgar_payload


@pytest.fixture
def yahoo_chart_payload():
    return make_yahoo_chart_payload([100.0, 110.0, 115.0, 120.0, 135.0, 150.0], ticker="TEST", company="Test Corp")


@pytest.fixture
def yahoo_quote_payload():
    return make_yahoo_quote_payload(ticker="TEST", market_cap=150_000_000_000, shares_outstanding=1_000_000_000)


@pytest.fixture
def sec_facts_payload():
    return make_sec_facts_payload(
        entity_name="Test Corp",
        revenues=[(2024, 120_000_000_000), (2023, 100_000_000_000)],
        cfo=[(2024, 24_000_000_000)],
        capex=[(2024, 6_000_000_000)],
        equity=60_000_000_000,
        liabilities=90_000_000_000,
        debt=25_000_000_000,
        current_debt=5_000_000_000,
        cash=10_000_000_000,
        current_assets=45_000_000_000,
        current_liabilities=30_000_000_000,
        operating_income=24_000_000_000,
        pretax_income=20_000_000_000,
        tax_expense=4_000_000_000,
        net_income=16_000_000_000,
        assets=150_000_000_000,
        shares_outstanding=1_200_000_000,
    )


@pytest.fixture
def sec_submissions_payload():
    return make_sec_submissions_payload(company="Test Corp", sic="7372", sic_description="Software")


@pytest.fixture
def fred_payloads():
    return {
        "FEDFUNDS": make_fred_series_payload([("2025-02-01", 4.33)]),
        "UNRATE": make_fred_series_payload([("2025-02-01", 4.0)]),
        "CPIAUCSL": make_fred_series_payload(
            [
                ("2025-02-01", 315.0),
                ("2025-01-01", 314.0),
                ("2024-12-01", 313.5),
                ("2024-11-01", 312.8),
                ("2024-10-01", 311.7),
                ("2024-09-01", 310.9),
                ("2024-08-01", 309.9),
                ("2024-07-01", 309.1),
                ("2024-06-01", 308.2),
                ("2024-05-01", 307.1),
                ("2024-04-01", 306.4),
                ("2024-03-01", 305.6),
                ("2024-02-01", 305.0),
            ]
        ),
    }


@pytest.fixture
def world_bank_payload():
    return make_world_bank_payload([2.1, 1.9, None])


@pytest.fixture
def fmp_peer_payload():
    return make_fmp_peer_payload(["ORCL", "CRM", "NOW"])


@pytest.fixture
def finnhub_peer_payload():
    return make_finnhub_peer_payload(["SAP", "ADBE"])


@pytest.fixture
def regular_company_dataset():
    return make_regular_company_dataset()


@pytest.fixture
def bank_company_dataset():
    return make_bank_company_dataset()


@pytest.fixture
def strong_company_dataset():
    return make_strong_company_dataset()


@pytest.fixture
def fallback_baseline_dataset():
    return make_fallback_baseline_dataset()


@pytest.fixture
def incomplete_company_dataset():
    return make_incomplete_company_dataset()


@pytest.fixture
def peer_group_with_quality_classes(regular_company_dataset):
    return {
        "usable": ["ORCL", "CRM"],
        "weak": ["NOW"],
        "excluded": ["SAP"],
        "dataset": regular_company_dataset,
    }


@pytest.fixture
def analysis_service_factory(recording_persistence):
    def _build_service(
        *,
        yahoo_payloads: dict[str, dict],
        edgar_payloads: dict[str, dict],
        fred_payload: dict | None = None,
        world_bank_payload: dict | None = None,
        peer_discovery: dict[str, dict[str, list[str]]] | None = None,
        market_cap_snapshots: dict[str, dict[str, tuple[float | None, str | None]]] | None = None,
    ) -> AnalysisService:
        peer_discovery = peer_discovery or {}
        market_cap_snapshots = market_cap_snapshots or {}
        peer_group_config = {
            "rules": [
                {
                    "sector": "Technology",
                    "industry_contains": ["software"],
                    "tickers": ["ORCL", "CRM", "NOW", "SAP"],
                },
                {
                    "sector": "Financial Services",
                    "industry_contains": ["bank"],
                    "tickers": ["JPM", "BAC", "USB"],
                },
                {
                    "sector": "Industrials",
                    "industry_contains": ["industrial", "machinery", "conglomerate"],
                    "tickers": ["GE", "CAT", "HON"],
                },
            ],
            "fallback": {"tickers": ["ORCL", "CRM", "GE", "CAT"]},
        }
        fmp_provider = StaticPeerProvider(
            source_name="fmp",
            discovery_map=peer_discovery.get("fmp", {}),
            market_cap_snapshots={ticker: snapshots.get("fmp") for ticker, snapshots in market_cap_snapshots.items() if snapshots.get("fmp")},
        )
        finnhub_provider = StaticPeerProvider(
            source_name="finnhub",
            discovery_map=peer_discovery.get("finnhub", {}),
            market_cap_snapshots={ticker: snapshots.get("finnhub") for ticker, snapshots in market_cap_snapshots.items() if snapshots.get("finnhub")},
        )
        business_type_provider = StaticPeerProvider(
            source_name="business_type",
            discovery_map=peer_discovery.get("business_type", {}),
        )
        config_provider = ConfigPeerProvider(peer_group_config)
        if peer_discovery.get("config"):
            config_provider = StaticPeerProvider(
                source_name="config",
                discovery_map=peer_discovery["config"],
            )

        return AnalysisService(
            yahoo=StaticCompanyBundleProvider(yahoo_payloads, source_name="Yahoo Finance"),
            edgar=StaticCompanyBundleProvider(edgar_payloads, source_name="SEC EDGAR"),
            fred=StaticMacroProvider(fred_payload or {}, source_name="FRED"),
            world_bank=StaticMacroProvider(world_bank_payload or {}, source_name="World Bank"),
            peer_providers=[fmp_provider, finnhub_provider, business_type_provider, config_provider],
            session_factory=recording_persistence.session_factory,
            repository_factory=recording_persistence.repository_factory,
            peer_group_config=peer_group_config,
        )

    return _build_service


@pytest.fixture
def regular_analysis_service(analysis_service_factory, regular_company_dataset):
    return analysis_service_factory(
        yahoo_payloads=regular_company_dataset["yahoo_payloads"],
        edgar_payloads=regular_company_dataset["edgar_payloads"],
        fred_payload=regular_company_dataset["fred_payload"],
        world_bank_payload=regular_company_dataset["world_bank_payload"],
        peer_discovery=regular_company_dataset["peer_discovery"],
        market_cap_snapshots=regular_company_dataset["market_cap_snapshots"],
    )


@pytest.fixture
def bank_analysis_service(analysis_service_factory, bank_company_dataset):
    return analysis_service_factory(
        yahoo_payloads=bank_company_dataset["yahoo_payloads"],
        edgar_payloads=bank_company_dataset["edgar_payloads"],
        fred_payload=bank_company_dataset["fred_payload"],
        world_bank_payload=bank_company_dataset["world_bank_payload"],
        peer_discovery=bank_company_dataset["peer_discovery"],
        market_cap_snapshots=bank_company_dataset["market_cap_snapshots"],
    )


@pytest.fixture
def strong_analysis_service(analysis_service_factory, strong_company_dataset):
    return analysis_service_factory(
        yahoo_payloads=strong_company_dataset["yahoo_payloads"],
        edgar_payloads=strong_company_dataset["edgar_payloads"],
        fred_payload=strong_company_dataset["fred_payload"],
        world_bank_payload=strong_company_dataset["world_bank_payload"],
        peer_discovery=strong_company_dataset["peer_discovery"],
        market_cap_snapshots=strong_company_dataset["market_cap_snapshots"],
    )


@pytest.fixture
def fallback_analysis_service(analysis_service_factory, fallback_baseline_dataset):
    return analysis_service_factory(
        yahoo_payloads=fallback_baseline_dataset["yahoo_payloads"],
        edgar_payloads=fallback_baseline_dataset["edgar_payloads"],
        fred_payload=fallback_baseline_dataset["fred_payload"],
        world_bank_payload=fallback_baseline_dataset["world_bank_payload"],
        peer_discovery=fallback_baseline_dataset["peer_discovery"],
        market_cap_snapshots=fallback_baseline_dataset["market_cap_snapshots"],
    )


@pytest.fixture
def incomplete_analysis_service(analysis_service_factory, incomplete_company_dataset):
    return analysis_service_factory(
        yahoo_payloads=incomplete_company_dataset["yahoo_payloads"],
        edgar_payloads=incomplete_company_dataset["edgar_payloads"],
        fred_payload=incomplete_company_dataset["fred_payload"],
        world_bank_payload=incomplete_company_dataset["world_bank_payload"],
        peer_discovery=incomplete_company_dataset["peer_discovery"],
        market_cap_snapshots=incomplete_company_dataset["market_cap_snapshots"],
    )


@pytest.fixture
def api_client_factory():
    created_clients: list[TestClient] = []

    def _build_client(service: AnalysisService) -> TestClient:
        app.dependency_overrides[routes.get_analysis_service] = lambda: service
        client = TestClient(app)
        created_clients.append(client)
        return client

    yield _build_client

    app.dependency_overrides.pop(routes.get_analysis_service, None)
    for client in created_clients:
        client.close()
