from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Branding is configuration, not business logic.
    app_name: str = "РИТМ"
    app_name_en: str = "RITM"

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
    gemini_media_model: str | None = None  # photos/voice; defaults to gemini_model
    gemini_base_url: str = "https://generativelanguage.googleapis.com/v1beta"
    ai_timeout_seconds: float = 45.0
    ai_user_daily_calls: int = 40
    ai_user_daily_media_calls: int = 15
    ai_global_daily_calls: int = 400
    ai_max_input_chars: int = 1500
    ai_max_concurrency: int = 4
    # Allow the model to propose generic per-100 g reference values for unbranded foods.
    # They are always labelled as estimates and require confirmation.
    ai_nutrient_estimates: bool = True

    # Media limits (Telegram downloads)
    max_photo_bytes: int = 8_000_000
    max_voice_bytes: int = 2_000_000
    max_voice_seconds: int = 120
    max_import_bytes: int = 3_000_000

    # External food databases: comma-separated subset of "off,usda". Empty = disabled.
    food_sources: str = ""
    usda_api_key: SecretStr | None = None
    food_source_timeout_seconds: float = 8.0

    reminders_enabled: bool = True

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
