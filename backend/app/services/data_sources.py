from __future__ import annotations


COMPANIES: dict[str, dict] = {
    "AAPL": {
        "company": "Apple Inc.",
        "sector": "Technology",
        "industry": "Consumer Electronics",
        "yahoo": {
            "market_cap_bln": 2870,
            "pe_ratio": 28.4,
            "pb_ratio": 41.5,
            "roe_pct": 156.3,
            "price_to_sales": 7.4,
            "revenue_growth_pct": 6.1,
            "gross_margin_pct": 45.8,
        },
        "edgar": {
            "revenue_bln": [394.3, 383.3, 365.8],
            "free_cash_flow_bln": [99.6, 110.5, 93.0],
            "debt_to_equity": 1.7,
            "current_ratio": 0.99,
            "roic_pct": 36.8,
            "ebit_margin_pct": 31.4,
        },
    },
    "MSFT": {
        "company": "Microsoft",
        "sector": "Technology",
        "industry": "Software",
        "yahoo": {
            "market_cap_bln": 3120,
            "pe_ratio": 34.7,
            "pb_ratio": 11.8,
            "roe_pct": 33.8,
            "price_to_sales": 12.6,
            "revenue_growth_pct": 15.7,
            "gross_margin_pct": 69.3,
        },
        "edgar": {
            "revenue_bln": [245.1, 211.9, 198.3],
            "free_cash_flow_bln": [74.1, 59.5, 65.1],
            "debt_to_equity": 0.32,
            "current_ratio": 1.77,
            "roic_pct": 24.6,
            "ebit_margin_pct": 44.6,
        },
    },
    "NVDA": {
        "company": "NVIDIA",
        "sector": "Technology",
        "industry": "Semiconductors",
        "yahoo": {
            "market_cap_bln": 2240,
            "pe_ratio": 51.2,
            "pb_ratio": 46.0,
            "roe_pct": 69.2,
            "price_to_sales": 30.4,
            "revenue_growth_pct": 125.8,
            "gross_margin_pct": 72.7,
        },
        "edgar": {
            "revenue_bln": [60.9, 26.9, 27.0],
            "free_cash_flow_bln": [27.0, 3.8, 8.1],
            "debt_to_equity": 0.22,
            "current_ratio": 3.5,
            "roic_pct": 58.7,
            "ebit_margin_pct": 61.2,
        },
    },
    "AMZN": {
        "company": "Amazon",
        "sector": "Technology",
        "industry": "Internet Retail & Cloud",
        "yahoo": {
            "market_cap_bln": 1910,
            "pe_ratio": 41.6,
            "pb_ratio": 7.9,
            "roe_pct": 20.5,
            "price_to_sales": 3.4,
            "revenue_growth_pct": 11.8,
            "gross_margin_pct": 48.4,
        },
        "edgar": {
            "revenue_bln": [638.0, 575.0, 514.0],
            "free_cash_flow_bln": [36.8, 32.2, -11.6],
            "debt_to_equity": 0.46,
            "current_ratio": 1.08,
            "roic_pct": 13.7,
            "ebit_margin_pct": 10.8,
        },
    },
    "TSLA": {
        "company": "Tesla",
        "sector": "Consumer Cyclical",
        "industry": "Automobiles",
        "yahoo": {
            "market_cap_bln": 610,
            "pe_ratio": 55.3,
            "pb_ratio": 9.4,
            "roe_pct": 18.7,
            "price_to_sales": 6.5,
            "revenue_growth_pct": 2.2,
            "gross_margin_pct": 18.2,
        },
        "edgar": {
            "revenue_bln": [96.8, 81.5, 53.8],
            "free_cash_flow_bln": [4.4, 4.1, 7.6],
            "debt_to_equity": 0.17,
            "current_ratio": 1.73,
            "roic_pct": 9.2,
            "ebit_margin_pct": 8.3,
        },
    },
    "JPM": {
        "company": "JPMorgan Chase",
        "sector": "Financial Services",
        "industry": "Banks - Diversified",
        "yahoo": {
            "market_cap_bln": 575,
            "pe_ratio": 12.8,
            "pb_ratio": 2.1,
            "roe_pct": 17.1,
            "price_to_sales": 3.1,
            "revenue_growth_pct": 12.4,
            "gross_margin_pct": 100.0,
        },
        "edgar": {
            "revenue_bln": [212.1, 158.1, 128.7],
            "free_cash_flow_bln": [48.2, 29.3, 26.1],
            "debt_to_equity": 1.25,
            "current_ratio": 1.01,
            "roic_pct": 14.4,
            "ebit_margin_pct": 33.7,
        },
    },
}

MACRO = {
    "fred": {
        "fed_funds_rate_pct": 4.75,
        "inflation_pct": 2.8,
        "unemployment_pct": 4.1,
    },
    "world_bank": {
        "gdp_growth_pct": 2.1,
    },
}


class YahooFinanceAdapter:
    source_name = "Yahoo Finance"

    def fetch(self, ticker: str) -> dict:
        return COMPANIES[ticker]["yahoo"]


class EdgarAdapter:
    source_name = "SEC EDGAR"

    def fetch(self, ticker: str) -> dict:
        return COMPANIES[ticker]["edgar"]


class FredAdapter:
    source_name = "FRED"

    def fetch(self) -> dict:
        return MACRO["fred"]


class WorldBankAdapter:
    source_name = "World Bank"

    def fetch(self) -> dict:
        return MACRO["world_bank"]

