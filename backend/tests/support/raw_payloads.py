from __future__ import annotations

from datetime import datetime, timedelta, timezone


def make_yahoo_chart_payload(
    closes: list[float],
    *,
    ticker: str = "TEST",
    company: str = "Test Corp",
    currency: str = "USD",
    start: datetime | None = None,
) -> dict:
    start_at = start or datetime(2021, 1, 1, tzinfo=timezone.utc)
    timestamps = [int((start_at + timedelta(days=30 * index)).timestamp()) for index in range(len(closes))]
    return {
        "chart": {
            "result": [
                {
                    "meta": {
                        "symbol": ticker,
                        "longName": company,
                        "currency": currency,
                        "regularMarketPrice": closes[-1] if closes else None,
                        "previousClose": closes[-2] if len(closes) >= 2 else closes[-1] if closes else None,
                    },
                    "timestamp": timestamps,
                    "indicators": {"quote": [{"close": closes}]},
                }
            ]
        }
    }


def make_yahoo_quote_payload(
    *,
    ticker: str = "TEST",
    currency: str = "USD",
    market_cap: float | None = None,
    shares_outstanding: float | None = None,
    quote_type: str = "EQUITY",
) -> dict:
    return {
        "quoteResponse": {
            "result": [
                {
                    "symbol": ticker,
                    "currency": currency,
                    "marketCap": market_cap,
                    "sharesOutstanding": shares_outstanding,
                    "quoteType": quote_type,
                }
            ]
        }
    }


def make_sec_facts_payload(
    *,
    entity_name: str = "Test Corp",
    revenues: list[tuple[int, float]] | None = None,
    cfo: list[tuple[int, float]] | None = None,
    capex: list[tuple[int, float]] | None = None,
    equity: float | None = None,
    liabilities: float | None = None,
    debt: float | None = None,
    current_debt: float | None = None,
    cash: float | None = None,
    current_assets: float | None = None,
    current_liabilities: float | None = None,
    operating_income: float | None = None,
    pretax_income: float | None = None,
    tax_expense: float | None = None,
    net_income: float | None = None,
    assets: float | None = None,
    shares_outstanding: float | None = None,
) -> dict:
    revenues = revenues or [(2024, 120_000_000_000), (2023, 100_000_000_000)]
    cfo = cfo or [(2024, 24_000_000_000)]
    capex = capex or [(2024, 6_000_000_000)]

    def annual_series(points: list[tuple[int, float]]) -> list[dict]:
        return [
            {"fy": year, "end": f"{year}-12-31", "form": "10-K", "fp": "FY", "val": value}
            for year, value in points
        ]

    def instant_series(value: float | None) -> list[dict]:
        if value is None:
            return []
        return [{"end": "2024-12-31", "form": "10-K", "val": value}]

    facts = {
        "entityName": entity_name,
        "facts": {
            "us-gaap": {
                "Revenues": {"units": {"USD": annual_series(revenues)}},
                "NetCashProvidedByUsedInOperatingActivities": {"units": {"USD": annual_series(cfo)}},
                "PaymentsToAcquirePropertyPlantAndEquipment": {"units": {"USD": annual_series(capex)}},
                "StockholdersEquity": {"units": {"USD": instant_series(equity)}},
                "Liabilities": {"units": {"USD": instant_series(liabilities)}},
                "LongTermDebt": {"units": {"USD": instant_series(debt)}},
                "LongTermDebtCurrent": {"units": {"USD": instant_series(current_debt)}},
                "CashAndCashEquivalentsAtCarryingValue": {"units": {"USD": instant_series(cash)}},
                "AssetsCurrent": {"units": {"USD": instant_series(current_assets)}},
                "LiabilitiesCurrent": {"units": {"USD": instant_series(current_liabilities)}},
                "OperatingIncomeLoss": {"units": {"USD": annual_series([(2024, operating_income)]) if operating_income is not None else []}},
                "IncomeBeforeTaxExpenseBenefit": {"units": {"USD": annual_series([(2024, pretax_income)]) if pretax_income is not None else []}},
                "IncomeTaxExpenseBenefit": {"units": {"USD": annual_series([(2024, tax_expense)]) if tax_expense is not None else []}},
                "NetIncomeLoss": {"units": {"USD": annual_series([(2024, net_income)]) if net_income is not None else []}},
                "Assets": {"units": {"USD": instant_series(assets)}},
            },
            "dei": {
                "EntityCommonStockSharesOutstanding": {
                    "units": {"shares": instant_series(shares_outstanding)}
                }
            },
        },
    }
    return facts


def make_sec_submissions_payload(
    *,
    company: str = "Test Corp",
    sic: str = "7372",
    sic_description: str = "Software",
) -> dict:
    return {
        "name": company,
        "sic": sic,
        "sicDescription": sic_description,
    }


def make_fred_series_payload(observations: list[tuple[str, str | float]]) -> dict:
    return {
        "observations": [{"date": date, "value": str(value)} for date, value in observations]
    }


def make_world_bank_payload(values: list[float | None]) -> list:
    return [
        {"page": 1, "pages": 1},
        [{"date": str(2025 - index), "value": value} for index, value in enumerate(values)],
    ]


def make_fmp_peer_payload(tickers: list[str]) -> dict:
    return {"peersList": tickers}


def make_finnhub_peer_payload(tickers: list[str]) -> list[str]:
    return tickers
