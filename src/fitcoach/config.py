from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_name: str = "Fit Coach"

    # Runtime DB role: must NOT own tables and must NOT have BYPASSRLS.
    database_url: str = "postgresql+asyncpg://fitcoach_app:change-me@localhost:5432/fitcoach"
    # Migration/owner role, used only by Alembic.
    migration_database_url: str = (
        "postgresql+asyncpg://fitcoach_owner:change-me@localhost:5432/fitcoach"
    )
    app_db_role: str = "fitcoach_app"
    db_pool_size: int = 5

    bot_token: SecretStr | None = None
    bot_mode: Literal["polling", "webhook"] = "polling"
    webhook_base_url: str | None = None
    webhook_path: str = "/telegram/webhook"
    webhook_secret: SecretStr | None = None

    # AI gateway. "disabled" = manual only; "mock" = deterministic fake, never presented as real.
    ai_provider: Literal["disabled", "mock", "gemini"] = "disabled"
    gemini_api_key: SecretStr | None = None
    gemini_model: str | None = None  # set explicitly after verifying availability
    gemini_base_url: str = "https://generativelanguage.googleapis.com/v1beta"
    ai_timeout_seconds: float = 20.0
    ai_user_daily_calls: int = 20
    ai_global_daily_calls: int = 200
    ai_max_input_chars: int = 1000

    log_level: str = "INFO"

    @model_validator(mode="after")
    def _check(self) -> Settings:
        if self.bot_mode == "webhook" and (
            not self.webhook_secret or len(self.webhook_secret.get_secret_value()) < 32
        ):
            raise ValueError("webhook mode requires WEBHOOK_SECRET of at least 32 characters")
        if self.ai_provider == "gemini" and (not self.gemini_api_key or not self.gemini_model):
            raise ValueError("AI_PROVIDER=gemini requires GEMINI_API_KEY and GEMINI_MODEL")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
