"""Task-specific AI gateway: consent, input limits, budgets, circuit breaker, metering.

The gateway never writes diary records. It returns drafts that ordinary code validates
and the user confirms.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import time

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from fitcoach.ai.mock import MockProvider
from fitcoach.ai.types import AIProvider, AIUnavailableError, MealDraft, ProviderResult
from fitcoach.config import Settings
from fitcoach.db.models import AiCall, User

log = logging.getLogger(__name__)


class CircuitBreaker:
    def __init__(self, threshold: int = 3, cooldown_seconds: float = 60.0) -> None:
        self.threshold = threshold
        self.cooldown = cooldown_seconds
        self._failures = 0
        self._opened_at: float | None = None

    def allow(self) -> bool:
        if self._opened_at is None:
            return True
        if time.monotonic() - self._opened_at >= self.cooldown:
            self._opened_at = None
            self._failures = self.threshold - 1  # half-open: one more failure re-opens
            return True
        return False

    def success(self) -> None:
        self._failures = 0
        self._opened_at = None

    def failure(self) -> None:
        self._failures += 1
        if self._failures >= self.threshold:
            self._opened_at = time.monotonic()


_RESERVE_USER = text(
    """
    INSERT INTO ai_user_budget (owner_id, day, calls) VALUES (:owner, :day, 1)
    ON CONFLICT (owner_id, day) DO UPDATE SET calls = ai_user_budget.calls + 1
    WHERE ai_user_budget.calls < :limit
    RETURNING calls
    """
)
_RESERVE_GLOBAL = text(
    """
    INSERT INTO ai_global_budget (day, calls) VALUES (:day, 1)
    ON CONFLICT (day) DO UPDATE SET calls = ai_global_budget.calls + 1
    WHERE ai_global_budget.calls < :limit
    RETURNING calls
    """
)


async def reserve_call(
    session: AsyncSession, user: User, *, user_limit: int, global_limit: int, day: dt.date
) -> None:
    """Atomically reserve one call. Row locks make concurrent reservations serialize.

    A reserved call is not refunded on provider failure (conservative accounting).
    """
    if user_limit <= 0 or global_limit <= 0:
        raise AIUnavailableError("budget_exhausted")
    got = await session.execute(_RESERVE_USER, {"owner": user.id, "day": day, "limit": user_limit})
    if got.scalar_one_or_none() is None:
        raise AIUnavailableError("budget_exhausted")
    got = await session.execute(_RESERVE_GLOBAL, {"day": day, "limit": global_limit})
    if got.scalar_one_or_none() is None:
        raise AIUnavailableError("budget_exhausted")


class AIGateway:
    def __init__(self, provider: AIProvider | None, settings: Settings) -> None:
        self.provider = provider
        self.settings = settings
        self.breaker = CircuitBreaker()
        self._concurrency = asyncio.Semaphore(4)

    @property
    def enabled(self) -> bool:
        return self.provider is not None

    @property
    def is_mock(self) -> bool:
        return self.provider is not None and self.provider.is_mock

    async def parse_meal(self, session: AsyncSession, user: User, text_in: str) -> MealDraft:
        """Commits the session (to persist the budget reservation) before calling out."""
        if self.provider is None:
            raise AIUnavailableError("disabled")
        if user.ai_text_consent_at is None:
            raise AIUnavailableError("no_consent")
        text_in = text_in.strip()
        if not text_in or len(text_in) > self.settings.ai_max_input_chars:
            raise AIUnavailableError("input_too_long")
        if not self.breaker.allow():
            raise AIUnavailableError("circuit_open")

        await reserve_call(
            session,
            user,
            user_limit=self.settings.ai_user_daily_calls,
            global_limit=self.settings.ai_global_daily_calls,
            day=dt.datetime.now(dt.UTC).date(),
        )
        await session.commit()

        status = "ok"
        result: ProviderResult[MealDraft] | None = None
        try:
            async with self._concurrency:
                result = await asyncio.wait_for(
                    self.provider.parse_meal(text_in, user.language),
                    timeout=self.settings.ai_timeout_seconds,
                )
            self.breaker.success()
            return result.value
        except AIUnavailableError as exc:
            status = exc.code
            if exc.code != "invalid_output":
                self.breaker.failure()
            raise
        except Exception as exc:
            status = "provider_error"
            self.breaker.failure()
            log.warning("ai provider failure: %s", type(exc).__name__)
            raise AIUnavailableError("provider_error") from exc
        finally:
            session.add(
                AiCall(
                    owner_id=user.id,
                    task="parse_meal",
                    provider=self.provider.name,
                    model=result.model if result else getattr(self.provider, "model", None),
                    status=status,
                    input_tokens=result.input_tokens if result else None,
                    output_tokens=result.output_tokens if result else None,
                )
            )
            await session.commit()


def build_gateway(settings: Settings) -> AIGateway:
    provider: AIProvider | None
    if settings.ai_provider == "mock":
        provider = MockProvider()
    elif settings.ai_provider == "gemini":
        from fitcoach.ai.gemini import GeminiProvider

        assert settings.gemini_api_key is not None and settings.gemini_model is not None
        provider = GeminiProvider(
            api_key=settings.gemini_api_key.get_secret_value(),
            model=settings.gemini_model,
            base_url=settings.gemini_base_url,
            timeout=settings.ai_timeout_seconds,
        )
    else:
        provider = None
    return AIGateway(provider, settings)
