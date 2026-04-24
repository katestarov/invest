import os
from dataclasses import dataclass
from functools import lru_cache


@dataclass(frozen=True)
class Settings:
    app_env: str
    app_port: int
    log_level: str
    database_url: str
    fred_api_key: str | None
    fmp_api_key: str | None
    finnhub_api_key: str | None
    sec_user_agent: str
    request_timeout_seconds: float
    provider_retry_attempts: int
    frontend_origin: str
    analysis_cache_ttl_seconds: int
    provider_cache_ttl_seconds: int
    peer_target_count: int
    peer_min_valid_count: int


@lru_cache
def get_settings() -> Settings:
    return Settings(
        app_env=os.getenv("APP_ENV", "development"),
        app_port=int(os.getenv("APP_PORT", "8000")),
        log_level=os.getenv("LOG_LEVEL", "INFO"),
        database_url=os.getenv("DATABASE_URL", "postgresql+psycopg://postgres:postgres@postgres:5432/invest"),
        fred_api_key=os.getenv("FRED_API_KEY"),
        fmp_api_key=os.getenv("FMP_API_KEY"),
        finnhub_api_key=os.getenv("FINNHUB_API_KEY"),
        sec_user_agent=os.getenv("SEC_USER_AGENT", "invest-app research@example.com"),
        request_timeout_seconds=float(os.getenv("REQUEST_TIMEOUT_SECONDS", "20")),
        provider_retry_attempts=max(1, int(os.getenv("PROVIDER_RETRY_ATTEMPTS", "2"))),
        frontend_origin=os.getenv("FRONTEND_ORIGIN", "http://localhost:5173"),
        analysis_cache_ttl_seconds=int(os.getenv("ANALYSIS_CACHE_TTL_SECONDS", "900")),
        provider_cache_ttl_seconds=int(os.getenv("PROVIDER_CACHE_TTL_SECONDS", "1800")),
        peer_target_count=int(os.getenv("PEER_TARGET_COUNT", "6")),
        peer_min_valid_count=int(os.getenv("PEER_MIN_VALID_COUNT", "3")),
    )
