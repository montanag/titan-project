"""Application settings, loaded from the environment (12-factor style)."""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="TITAN_", env_file=".env", extra="ignore")

    # Storage. Defaults match docker-compose.yml (host port 5433).
    database_url: str = "postgresql+psycopg://titan:titan@localhost:5433/titan"

    # Open Library API.
    openlibrary_base_url: str = "https://openlibrary.org"
    covers_base_url: str = "https://covers.openlibrary.org"
    user_agent: str = "titan-catalog/0.1 (https://github.com/example/titan)"

    # Politeness / rate limiting for outbound calls to Open Library.
    request_timeout_seconds: float = 20.0
    request_min_interval_seconds: float = 0.2  # ~5 req/s ceiling
    max_retries: int = 4
    retry_backoff_base_seconds: float = 0.5

    # Ingestion bounds — cap how much a single run pulls from search.
    max_pages_per_run: int = 5
    search_page_size: int = 100

    # Background worker.
    worker_poll_interval_seconds: float = 2.0

    # PII handling. The pepper is mixed into the email HMAC so the dedup hash
    # is not reversible via a rainbow table. MUST be overridden in production
    # (TITAN_PII_PEPPER); the default is for local dev only.
    pii_pepper: str = "dev-only-insecure-pepper-change-me"

    # Catalog freshness — re-sync a job if its last run finished longer ago
    # than this. Defaults are short so the behavior is observable in a demo.
    freshness_interval_seconds: float = 86400.0  # 24h
    scheduler_tick_seconds: float = 30.0


settings = Settings()
