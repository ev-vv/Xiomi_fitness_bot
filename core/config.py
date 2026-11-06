"""Bot/worker settings from the environment (and `.env`)."""

from __future__ import annotations

from functools import lru_cache

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    bot_token: SecretStr = SecretStr("")
    owner_ids: str = ""  # comma-separated Telegram user ids
    redis_url: str = "redis://localhost:6379/0"
    amqp_url: str = "amqp://guest:guest@localhost:5672//"
    db_path: str = "data/state/bot.sqlite"
    telegram_proxy: str | None = None

    anthropic_api_key: SecretStr = SecretStr("")
    anthropic_base_url: str = ""  # empty: SDK default (api.anthropic.com)
    llm_model_analysis: str = "claude-haiku-5-5"
    llm_model_chat: str = "claude-haiku-5-5"
    llm_effort_analysis: str = "medium"
    llm_effort_chat: str = "medium"
    llm_effort_brief: str = "low"
    llm_daily_budget_usd: float = 0.5
    llm_timeout_s: float = 60.0

    # Defaults for the user-editable settings stored in SQLite.
    autosync_hours: int = 2
    brief_time: str = "08:30"

    @property
    def owners(self) -> set[int]:
        return {int(x) for x in self.owner_ids.replace(" ", "").split(",") if x}


@lru_cache
def settings() -> Settings:
    return Settings()
