import os
from dataclasses import dataclass
from functools import lru_cache


@dataclass(frozen=True)
class Settings:
    app_env: str
    app_port: int
    database_url: str
    fred_api_key: str | None
    sec_user_agent: str
    request_timeout_seconds: float
    frontend_origin: str
    analysis_cache_ttl_seconds: int
    provider_cache_ttl_seconds: int


@lru_cache
def get_settings() -> Settings:
    return Settings(
        app_env=os.getenv("APP_ENV", "development"),
        app_port=int(os.getenv("APP_PORT", "8000")),
        database_url=os.getenv("DATABASE_URL", "postgresql+psycopg://postgres:postgres@postgres:5432/invest"),
        fred_api_key=os.getenv("FRED_API_KEY"),
        sec_user_agent=os.getenv("SEC_USER_AGENT", "invest-app research@example.com"),
        request_timeout_seconds=float(os.getenv("REQUEST_TIMEOUT_SECONDS", "20")),
        frontend_origin=os.getenv("FRONTEND_ORIGIN", "http://localhost:5173"),
        analysis_cache_ttl_seconds=int(os.getenv("ANALYSIS_CACHE_TTL_SECONDS", "900")),
        provider_cache_ttl_seconds=int(os.getenv("PROVIDER_CACHE_TTL_SECONDS", "1800")),
    )
