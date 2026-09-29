"""Task-specific AI gateway: consent, input limits, budgets, circuit breaker, metering.

The gateway never writes diary records. It returns drafts that services validate and the
user confirms. Every task goes through the same `_run` pipeline:

  consent -> input limits -> breaker -> atomic reservation (committed) -> provider call
  (timeout, concurrency limit, provider retries) -> validation -> metering/reconciliation
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import time
from collections.abc import Awaitable, Callable

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from fitcoach.ai.mock import MockProvider
from fitcoach.ai.types import (
    ActivitySchemaDraft,
    AIProvider,
    AIUnavailableError,
    FoodParse,
    Media,
    ProviderResult,
    WorkoutParse,
)
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
    INSERT INTO ai_user_budget (owner_id, day, calls, media_calls)
    VALUES (:owner, :day, 1, :media)
    ON CONFLICT (owner_id, day) DO UPDATE
       SET calls = ai_user_budget.calls + 1,
           media_calls = ai_user_budget.media_calls + :media
    WHERE ai_user_budget.calls < :limit
      AND ai_user_budget.media_calls + :media <= :media_limit
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
_REFUND_USER = text(
    "UPDATE ai_user_budget SET calls = greatest(calls - 1, 0), "
    "media_calls = greatest(media_calls - :media, 0) WHERE owner_id = :owner AND day = :day"
)
_REFUND_GLOBAL = text("UPDATE ai_global_budget SET calls = greatest(calls - 1, 0) WHERE day = :day")


async def reserve_call(
    session: AsyncSession,
    user: User,
    *,
    user_limit: int,
    global_limit: int,
    day: dt.date,
    media: bool = False,
    media_limit: int = 0,
) -> None:
    """Atomically reserve one call (and one media call). Row locks serialize concurrent
    reservations, so limits hold under parallel requests."""
    if user_limit <= 0 or global_limit <= 0 or (media and media_limit <= 0):
        raise AIUnavailableError("budget_exhausted")
    got = await session.execute(
        _RESERVE_USER,
        {
            "owner": user.id,
            "day": day,
            "limit": user_limit,
            "media": int(media),
            "media_limit": media_limit if media else 10**9,
        },
    )
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
        self._concurrency = asyncio.Semaphore(settings.ai_max_concurrency)

    @property
    def enabled(self) -> bool:
        return self.provider is not None

    @property
    def is_mock(self) -> bool:
        return self.provider is not None and self.provider.is_mock

    def text_available(self, user: User) -> bool:
        return self.enabled and user.ai_text_consent_at is not None

    def media_available(self, user: User) -> bool:
        return self.enabled and user.ai_media_consent_at is not None

    # --- tasks ---------------------------------------------------------------------

    async def parse_food_text(self, session: AsyncSession, user: User, text_in: str) -> FoodParse:
        provider = self._require()
        text_in = self._check_text(text_in)
        return await self._run(
            session,
            user,
            "food_text",
            Media.NONE,
            lambda: provider.parse_food_text(text_in, user.language),
        )

    async def analyze_food_image(
        self, session: AsyncSession, user: User, image: bytes, mime: str, caption: str | None
    ) -> FoodParse:
        provider = self._require()
        if caption is not None:
            caption = self._check_text(caption)
        return await self._run(
            session,
            user,
            "food_image",
            Media.IMAGE,
            lambda: provider.analyze_food_image(image, mime, caption, user.language),
        )

    async def parse_food_voice(
        self, session: AsyncSession, user: User, audio: bytes, mime: str
    ) -> FoodParse:
        provider = self._require()
        return await self._run(
            session,
            user,
            "food_voice",
            Media.AUDIO,
            lambda: provider.parse_food_voice(audio, mime, user.language),
        )

    async def parse_workout_text(
        self, session: AsyncSession, user: User, text_in: str
    ) -> WorkoutParse:
        provider = self._require()
        text_in = self._check_text(text_in)
        return await self._run(
            session,
            user,
            "workout_text",
            Media.NONE,
            lambda: provider.parse_workout_text(text_in, user.language),
        )

    async def build_activity_draft(
        self, session: AsyncSession, user: User, text_in: str
    ) -> ActivitySchemaDraft:
        provider = self._require()
        text_in = self._check_text(text_in)
        return await self._run(
            session,
            user,
            "activity_schema",
            Media.NONE,
            lambda: provider.build_activity_draft(text_in, user.language),
        )

    # --- pipeline ------------------------------------------------------------------

    def _require(self) -> AIProvider:
        if self.provider is None:
            raise AIUnavailableError("disabled")
        return self.provider

    def _check_text(self, text_in: str) -> str:
        text_in = text_in.strip()
        if not text_in or len(text_in) > self.settings.ai_max_input_chars:
            raise AIUnavailableError("input_too_long")
        return text_in

    async def _run[T](
        self,
        session: AsyncSession,
        user: User,
        task: str,
        media: Media,
        call: Callable[[], Awaitable[ProviderResult[T]]],
    ) -> T:
        """Commits the session (to persist the reservation) before calling out."""
        provider = self._require()
        if media is Media.NONE and user.ai_text_consent_at is None:
            raise AIUnavailableError("no_consent")
        if media is not Media.NONE and user.ai_media_consent_at is None:
            raise AIUnavailableError("no_media_consent")
        if not self.breaker.allow():
            raise AIUnavailableError("circuit_open")

        day = dt.datetime.now(dt.UTC).date()
        is_media = media is not Media.NONE
        await reserve_call(
            session,
            user,
            user_limit=self.settings.ai_user_daily_calls,
            global_limit=self.settings.ai_global_daily_calls,
            media=is_media,
            media_limit=self.settings.ai_user_daily_media_calls,
            day=day,
        )
        user_id = user.id
        await session.commit()

        status = "ok"
        refunded = False
        result: ProviderResult[T] | None = None
        started = time.monotonic()
        try:
            async with self._concurrency:
                result = await asyncio.wait_for(call(), timeout=self.settings.ai_timeout_seconds)
            self.breaker.success()
            return result.value
        except AIUnavailableError as exc:
            status = exc.code
            if exc.code != "invalid_output":
                self.breaker.failure()
            if not exc.billable:
                refunded = True
            raise
        except TimeoutError as exc:
            status = "provider_timeout"
            self.breaker.failure()
            raise AIUnavailableError("provider_timeout") from exc
        except Exception as exc:
            status = "provider_error"
            self.breaker.failure()
            log.warning("ai provider failure: %s", type(exc).__name__)
            raise AIUnavailableError("provider_error") from exc
        finally:
            # Reconciliation: release the reservation if the provider certainly did not
            # process the request; always record what happened (never the content).
            if refunded:
                await session.execute(
                    _REFUND_USER, {"owner": user_id, "day": day, "media": int(is_media)}
                )
                await session.execute(_REFUND_GLOBAL, {"day": day})
            session.add(
                AiCall(
                    owner_id=user_id,
                    task=task,
                    provider=provider.name,
                    model=result.model if result else getattr(provider, "model", None),
                    status=status[:32],
                    input_tokens=result.input_tokens if result else None,
                    output_tokens=result.output_tokens if result else None,
                    media=media.value,
                    latency_ms=int((time.monotonic() - started) * 1000),
                    refunded=refunded,
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
            media_model=settings.gemini_media_model,
            base_url=settings.gemini_base_url,
            timeout=settings.ai_timeout_seconds,
        )
    else:
        provider = None
    return AIGateway(provider, settings)


async def verify_provider(gateway: AIGateway) -> str:
    """Startup check. Returns a status string safe to log (no key material)."""
    if gateway.provider is None:
        return "ai disabled"
    try:
        missing = await gateway.provider.check()
    except AIUnavailableError as exc:
        return f"ai provider check failed: {exc.code}"
    if missing:
        gateway.provider = None  # fail closed to manual mode
        return f"ai disabled: configured model(s) unavailable: {', '.join(missing)}"
    return f"ai provider {gateway.provider.name} ok"
