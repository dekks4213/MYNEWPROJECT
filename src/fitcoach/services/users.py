from __future__ import annotations

import datetime as dt
from decimal import Decimal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from fitcoach.db.models import User
from fitcoach.db.session import bind_identity
from fitcoach.domain.nutrition import parse_macros
from fitcoach.domain.units import ParseError, parse_decimal
from fitcoach.services.errors import ServiceError

MACRO_TARGET_MAX = Decimal(1000)
ONBOARDING_STEPS = ("language", "age", "privacy", "timezone", "units", "goal", "target", "done")
LANGUAGES = ("ru", "en")
GOALS = ("maintain", "lose", "gain", "habits")
UNITS = ("metric",)  # imperial is planned, not implemented
KCAL_TARGET_MIN = Decimal(500)
KCAL_TARGET_MAX = Decimal(10000)


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def user_zone(user: User) -> ZoneInfo:
    if not user.timezone:
        raise ServiceError("timezone_required")
    return ZoneInfo(user.timezone)


def local_today(user: User, now: dt.datetime | None = None) -> dt.date:
    return (now or utcnow()).astimezone(user_zone(user)).date()


async def resolve_user(session: AsyncSession, telegram_id: int) -> User:
    """Load or create the user for a *verified* Telegram ID and bind the RLS identity."""
    await bind_identity(session, telegram_id=telegram_id, user_id=None)
    await session.execute(
        insert(User)
        .values(telegram_id=telegram_id)
        .on_conflict_do_nothing(index_elements=[User.telegram_id])
    )
    user = (await session.execute(select(User).where(User.telegram_id == telegram_id))).scalar_one()
    await bind_identity(session, telegram_id=telegram_id, user_id=user.id)
    return user


class UserService:
    def __init__(self, session: AsyncSession, user: User) -> None:
        self.session = session
        self.user = user

    def _advance(self, step: str) -> None:
        if self.user.onboarding_step == step:
            self.user.onboarding_step = ONBOARDING_STEPS[ONBOARDING_STEPS.index(step) + 1]

    async def _flush(self) -> User:
        await self.session.flush()
        return self.user

    async def set_language(self, language: str) -> User:
        if language not in LANGUAGES:
            raise ServiceError("bad_choice")
        self.user.language = language
        self._advance("language")
        return await self._flush()

    async def confirm_adult(self, now: dt.datetime | None = None) -> User:
        self.user.age_confirmed_at = now or utcnow()
        self._advance("age")
        return await self._flush()

    async def set_ai_consent(self, allowed: bool, now: dt.datetime | None = None) -> User:
        self.user.ai_text_consent_at = (now or utcnow()) if allowed else None
        self._advance("privacy")
        return await self._flush()

    async def set_timezone(self, name: str) -> User:
        name = name.strip()
        if name not in available_timezones():
            raise ServiceError("bad_timezone")
        try:
            ZoneInfo(name)
        except ZoneInfoNotFoundError as exc:  # pragma: no cover - guarded above
            raise ServiceError("bad_timezone") from exc
        self.user.timezone = name
        self._advance("timezone")
        return await self._flush()

    async def set_units(self, units: str) -> User:
        if units not in UNITS:
            raise ServiceError("bad_choice")
        self.user.units = units
        self._advance("units")
        return await self._flush()

    async def set_goal(self, goal: str | None) -> User:
        if goal is not None and goal not in GOALS:
            raise ServiceError("bad_choice")
        self.user.goal = goal
        self._advance("goal")
        return await self._flush()

    async def set_kcal_target(self, raw: str | None, now: dt.datetime | None = None) -> User:
        """Manual, user-chosen target. The app does not compute recommended intake in P0."""
        if raw is None:
            self.user.daily_kcal_target = None
            self.user.target_set_at = None
        else:
            try:
                value = parse_decimal(raw)
            except ParseError as exc:
                raise ServiceError("not_a_number") from exc
            if not KCAL_TARGET_MIN <= value <= KCAL_TARGET_MAX:
                raise ServiceError("target_out_of_range")
            self.user.daily_kcal_target = value
            self.user.target_set_at = now or utcnow()
        self._advance("target")
        return await self._flush()

    async def set_targets(self, raw: str | None, now: dt.datetime | None = None) -> User:
        """'2300; 180/80/230', '2300' or '; 180/80/230'. Manual targets only."""
        if raw is None:
            self.user.daily_kcal_target = None
            self.user.protein_target_g = self.user.fat_target_g = self.user.carbs_target_g = None
            self.user.target_set_at = None
            await self.session.flush()
            return self.user
        kcal_text, _, macro_text = raw.partition(";")
        if kcal_text.strip():
            await self.set_kcal_target(kcal_text, now)
        if macro_text.strip():
            try:
                p, f, c = parse_macros(macro_text)
            except ParseError as exc:
                raise ServiceError(exc.code) from exc
            if any(v is not None and v > MACRO_TARGET_MAX for v in (p, f, c)):
                raise ServiceError("target_out_of_range")
            self.user.protein_target_g, self.user.fat_target_g = p, f
            self.user.carbs_target_g = c
            self.user.target_set_at = now or utcnow()
        if not kcal_text.strip() and not macro_text.strip():
            raise ServiceError("not_a_number")
        await self.session.flush()
        return self.user

    async def set_media_consent(self, allowed: bool, now: dt.datetime | None = None) -> User:
        self.user.ai_media_consent_at = (now or utcnow()) if allowed else None
        await self.session.flush()
        return self.user
