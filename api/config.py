"""Settings loaded from environment (HF Space secrets) or api/.env for local dev.

Secrets live only here and in the per-run context; they are never put in graph
state or prompts.
"""
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        # repo-root .env or api/.env; later files win, so api/.env overrides the root one
        env_file=(Path(__file__).parent.parent / ".env", Path(__file__).parent / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    anthropic_api_key: str = ""
    model_id: str = "claude-haiku-5-5"

    # Tavily: web search, page fetch and SERP position checks (free tier)
    tavily_api_key: str = ""
    tavily_max_searches_per_run: int = 8     # hard cap per /chat turn
    tavily_daily_limit: int = 150            # in-process daily cap (free tier is ~1000/month)
    fact_check_max_claims: int = 3           # one search per claim

    database_url: str = ""

    allowed_origins: str = "http://localhost:3000"
    public_site_url: str = "http://localhost:3000"

    cron_secret: str = "change-me"

    # Haiku 5.5 pricing, USD per million tokens
    price_in_per_mtok: float = 0.10
    price_out_per_mtok: float = 0.50
    max_spend_per_run_usd: float = 0.02
    daily_spend_ceiling_usd: float = 1.50

    # Conversation window sent to the model (short-term memory trimming)
    history_window: int = 12

    @property
    def origins(self) -> list[str]:
        return [o.strip() for o in self.allowed_origins.split(",") if o.strip()]


settings = Settings()
