"""Opt-in reminders and the delivery bookkeeping used by the single scheduler loop.

Claiming a due reminder is atomic (row lock + unique (reminder_id, scheduled_for)), so
several processes or a restarted process cannot deliver the same occurrence twice.
Delivery is still at-most-once per claim, not guaranteed: Telegram may fail or be slow,
and outcomes are recorded per occurrence.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from sqlalchemy import select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from fitcoach.db.models import PlannedWorkout, Reminder, ReminderDelivery, User
from fitcoach.db.session import bind_identity
from fitcoach.domain.schedule import ALL_DAYS, next_fire, parse_hhmm
from fitcoach.services.errors import NotFound, ServiceError
from fitcoach.services.users import local_today, user_zone, utcnow

KINDS = ("weigh_in", "workout", "meals", "custom")
MAX_REMINDERS = 20
STALE_AFTER = dt.timedelta(hours=2)
MAX_SNOOZE_MIN = 24 * 60


class DeliveryBlockedError(Exception):
    """The user blocked the bot: reminders are switched off."""


class DeliveryFailedError(Exception):
    pass


def compute_next(reminder: Reminder, user: User, after: dt.datetime) -> dt.datetime | None:
    if not reminder.enabled or not user.timezone:
        return None
    return next_fire(
        reminder.local_time,
        reminder.days_mask,
        user_zone(user),
        after,
        user.quiet_start,
        user.quiet_end,
    )


class ReminderService:
    def __init__(self, session: AsyncSession, user: User) -> None:
        self.session = session
        self.user = user

    async def create(
        self,
        kind: str,
        hhmm: str,
        days_mask: int = ALL_DAYS,
        custom_text: str | None = None,
        now: dt.datetime | None = None,
    ) -> Reminder:
        if kind not in KINDS or not 1 <= days_mask <= ALL_DAYS:
            raise ServiceError("bad_choice")
        try:
            at = parse_hhmm(hhmm)
        except ValueError as exc:
            raise ServiceError("bad_time") from exc
        if kind == "custom":
            custom_text = " ".join((custom_text or "").split())
            if not custom_text or len(custom_text) > 200:
                raise ServiceError("bad_name")
        else:
            custom_text = None
        if len(await self.list()) >= MAX_REMINDERS:
            raise ServiceError("limit_reached")
        reminder = Reminder(
            owner_id=self.user.id,
            kind=kind,
            text=custom_text,
            local_time=at,
            days_mask=days_mask,
            enabled=True,
        )
        reminder.next_fire_at = compute_next(reminder, self.user, now or utcnow())
        self.session.add(reminder)
        await self.session.flush()
        return reminder

    async def list(self) -> list[Reminder]:
        rows = await self.session.execute(
            select(Reminder).where(Reminder.owner_id == self.user.id).order_by(Reminder.local_time)
        )
        return list(rows.scalars())

    async def get(self, reminder_id: int) -> Reminder:
        row = (
            await self.session.execute(
                select(Reminder).where(
                    Reminder.id == reminder_id, Reminder.owner_id == self.user.id
                )
            )
        ).scalar_one_or_none()
        if row is None:
            raise NotFound
        return row

    async def set_enabled(
        self, reminder_id: int, enabled: bool, now: dt.datetime | None = None
    ) -> Reminder:
        reminder = await self.get(reminder_id)
        reminder.enabled = enabled
        reminder.next_fire_at = compute_next(reminder, self.user, now or utcnow())
        await self.session.flush()
        return reminder

    async def snooze(
        self, reminder_id: int, minutes: int, now: dt.datetime | None = None
    ) -> Reminder:
        if not 1 <= minutes <= MAX_SNOOZE_MIN:
            raise ServiceError("bad_choice")
        reminder = await self.get(reminder_id)
        if not reminder.enabled:
            raise ServiceError("reminder_disabled")
        reminder.next_fire_at = (now or utcnow()) + dt.timedelta(minutes=minutes)
        await self.session.flush()
        return reminder

    async def delete(self, reminder_id: int) -> None:
        reminder = await self.get(reminder_id)
        await self.session.delete(reminder)
        await self.session.flush()

    async def reschedule_all(self, now: dt.datetime | None = None) -> None:
        """After a timezone or quiet-hours change."""
        for reminder in await self.list():
            reminder.next_fire_at = compute_next(reminder, self.user, now or utcnow())
        await self.session.flush()

    async def set_quiet_hours(self, start: str | None, end: str | None) -> User:
        try:
            self.user.quiet_start = parse_hhmm(start) if start else None
            self.user.quiet_end = parse_hhmm(end) if end else None
        except ValueError as exc:
            raise ServiceError("bad_time") from exc
        await self.session.flush()
        await self.reschedule_all()
        return self.user


@dataclass(frozen=True)
class DueReminder:
    reminder: Reminder
    user: User
    scheduled_for: dt.datetime


# Returns False to skip (e.g. nothing planned today). May read the user's own data through
# the provided session, which is bound to that user's RLS identity.
Sender = Callable[[DueReminder, AsyncSession], Awaitable[bool]]


async def process_due(
    sessionmaker: async_sessionmaker[AsyncSession],
    send: Sender,
    now: dt.datetime | None = None,
    limit: int = 50,
) -> dict[str, int]:
    """One scheduler tick."""
    now = now or utcnow()
    counts = {"sent": 0, "skipped": 0, "failed": 0, "blocked": 0}
    async with sessionmaker() as session:
        due = (
            await session.execute(
                text(
                    "SELECT reminder_id, owner_id, telegram_id FROM app_due_reminders(:now, :lim)"
                ),
                {"now": now, "lim": limit},
            )
        ).all()
    for reminder_id, owner_id, telegram_id in due:
        async with sessionmaker() as session:
            await bind_identity(session, telegram_id=telegram_id, user_id=owner_id)
            claimed = await _claim(session, reminder_id, now)
            if claimed is None:
                await session.commit()  # keeps an advanced next_fire_at, if any
                continue
            await session.commit()
            status = "skipped"
            error = None
            if now - claimed.scheduled_for <= STALE_AFTER:
                try:
                    status = "sent" if await send(claimed, session) else "skipped"
                except DeliveryBlockedError:
                    status = "blocked"
                    await session.execute(
                        update(Reminder)
                        .where(Reminder.owner_id == owner_id)
                        .values(enabled=False, next_fire_at=None)
                        .execution_options(synchronize_session=False)
                    )
                except DeliveryFailedError as exc:
                    status, error = "failed", str(exc)[:40]
            await session.execute(
                update(ReminderDelivery)
                .where(
                    ReminderDelivery.reminder_id == reminder_id,
                    ReminderDelivery.scheduled_for == claimed.scheduled_for,
                )
                .values(status=status, error=error)
                .execution_options(synchronize_session=False)
            )
            await session.commit()
            counts[status] += 1
    return counts


async def _claim(session: AsyncSession, reminder_id: int, now: dt.datetime) -> DueReminder | None:
    reminder = (
        await session.execute(
            select(Reminder).where(Reminder.id == reminder_id).with_for_update(skip_locked=True)
        )
    ).scalar_one_or_none()
    if reminder is None or not reminder.enabled or reminder.next_fire_at is None:
        return None
    if reminder.next_fire_at > now:
        return None
    user = (await session.execute(select(User).where(User.id == reminder.owner_id))).scalar_one()
    scheduled_for = reminder.next_fire_at
    inserted = await session.execute(
        insert(ReminderDelivery)
        .values(
            owner_id=reminder.owner_id,
            reminder_id=reminder.id,
            scheduled_for=scheduled_for,
            status="sending",
        )
        .on_conflict_do_nothing(index_elements=["reminder_id", "scheduled_for"])
        .returning(ReminderDelivery.id)
    )
    reminder.next_fire_at = compute_next(reminder, user, max(now, scheduled_for))
    await session.flush()
    if inserted.scalar_one_or_none() is None:
        return None  # already handled; next_fire_at advanced anyway
    return DueReminder(reminder, user, scheduled_for)


async def has_open_plan_today(session: AsyncSession, user: User) -> bool:
    today = local_today(user)
    row = (
        await session.execute(
            select(PlannedWorkout.id)
            .where(
                PlannedWorkout.owner_id == user.id,
                PlannedWorkout.planned_date == today,
                PlannedWorkout.status == "planned",
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    return row is not None
