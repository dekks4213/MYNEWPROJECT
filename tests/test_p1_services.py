"""Reminders, Strong import, history, export and deletion."""

from __future__ import annotations

import asyncio
import datetime as dt
import json
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from fitcoach.db.models import FoodEntry, Reminder, ReminderDelivery, User, WorkoutSession
from fitcoach.domain.nutrition import Precision
from fitcoach.domain.schedule import WEEKDAYS, in_quiet_hours, next_fire, parse_hhmm
from fitcoach.domain.starters import starter_fields
from fitcoach.domain.workout import ActivityKind
from fitcoach.i18n import Translator
from fitcoach.services.account import delete_account, export_json
from fitcoach.services.activities import ActivityService
from fitcoach.services.diary import DiaryService
from fitcoach.services.errors import Conflict, NotFound, ServiceError
from fitcoach.services.food import FoodService
from fitcoach.services.history import activity_stats, nutrition_history, weight_history
from fitcoach.services.reminders import (
    DeliveryBlockedError,
    DueReminder,
    ReminderService,
    process_due,
)
from fitcoach.services.strong_import import StrongImportService, parse_strong_csv
from tests.conftest import make_user, open_user, requires_db

RU = Translator("ru")
UTC = dt.UTC
MSK = ZoneInfo("Europe/Moscow")
BERLIN = ZoneInfo("Europe/Berlin")


# --- schedule arithmetic ----------------------------------------------------------------


def test_next_fire_respects_timezone_days_and_quiet_hours() -> None:
    after = dt.datetime(2026, 9, 28, 5, 0, tzinfo=UTC)  # Monday 08:00 Moscow
    assert next_fire(dt.time(8, 30), 0b1111111, MSK, after) == dt.datetime(
        2026, 9, 28, 5, 30, tzinfo=UTC
    )
    assert next_fire(dt.time(7, 0), 0b1111111, MSK, after) == dt.datetime(
        2026, 9, 29, 4, 0, tzinfo=UTC
    )
    saturday = dt.datetime(2026, 10, 3, 5, 0, tzinfo=UTC)
    assert next_fire(dt.time(9, 0), WEEKDAYS, MSK, saturday).date() == dt.date(2026, 10, 5)
    quiet = next_fire(dt.time(6, 30), 0b1111111, MSK, after, dt.time(22, 0), dt.time(8, 0))
    assert quiet == dt.datetime(2026, 9, 29, 5, 0, tzinfo=UTC)  # moved to 08:00 local
    assert in_quiet_hours(dt.time(23, 30), dt.time(22, 0), dt.time(7, 0))
    assert not in_quiet_hours(dt.time(12, 0), dt.time(22, 0), dt.time(7, 0))
    assert next_fire(dt.time(9, 0), 0, MSK, after) is None


def test_next_fire_across_dst() -> None:
    before_change = dt.datetime(2026, 10, 24, 12, 0, tzinfo=UTC)  # CEST (UTC+2)
    first = next_fire(dt.time(9, 0), 0b1111111, BERLIN, before_change)
    second = next_fire(dt.time(9, 0), 0b1111111, BERLIN, first)
    assert first == dt.datetime(2026, 10, 25, 8, 0, tzinfo=UTC)  # Oct 25 is CET (UTC+1)
    assert second == dt.datetime(2026, 10, 26, 8, 0, tzinfo=UTC)
    spring = next_fire(
        dt.time(2, 30), 0b1111111, BERLIN, dt.datetime(2026, 3, 28, 12, 0, tzinfo=UTC)
    )
    assert spring is not None and spring.astimezone(BERLIN).hour in (1, 3)  # gap resolved


def test_parse_hhmm() -> None:
    assert parse_hhmm("7:05") == dt.time(7, 5) and parse_hhmm("21.30") == dt.time(21, 30)
    for bad in ("25:00", "7", "ab:cd", "7:60"):
        with pytest.raises(ValueError):
            parse_hhmm(bad)


# --- reminders with database ---------------------------------------------------------------


@requires_db
class TestReminders:
    async def test_create_snooze_disable_and_isolation(
        self, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        a, b = await make_user(sessionmaker, "Europe/Moscow"), await make_user(sessionmaker)
        now = dt.datetime(2026, 9, 28, 5, 0, tzinfo=UTC)
        async with sessionmaker() as s:
            svc = ReminderService(s, await open_user(s, a))
            r = await svc.create("weigh_in", "08:30", now=now)
            assert r.next_fire_at == dt.datetime(2026, 9, 28, 5, 30, tzinfo=UTC)
            with pytest.raises(ServiceError):
                await svc.create("custom", "08:00", custom_text="  ")
            with pytest.raises(ServiceError):
                await svc.create("weigh_in", "99:00")
            snoozed = await svc.snooze(r.id, 30, now=now)
            assert snoozed.next_fire_at == now + dt.timedelta(minutes=30)
            off = await svc.set_enabled(r.id, False)
            assert off.next_fire_at is None
            with pytest.raises(ServiceError):
                await svc.snooze(r.id, 10)
            await svc.set_enabled(r.id, True, now=now)
            await s.commit()
        async with sessionmaker() as s:
            svc_b = ReminderService(s, await open_user(s, b))
            for call in (
                lambda: svc_b.get(r.id),
                lambda: svc_b.snooze(r.id, 5),
                lambda: svc_b.delete(r.id),
            ):
                with pytest.raises(NotFound):
                    await call()

    async def test_scheduler_delivers_once_even_with_parallel_ticks(
        self, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        tid = await make_user(sessionmaker, "Europe/Moscow")
        created_at = dt.datetime(2026, 9, 28, 5, 0, tzinfo=UTC)
        async with sessionmaker() as s:
            r = await ReminderService(s, await open_user(s, tid)).create(
                "custom", "08:30", custom_text="Выпить воды", now=created_at
            )
            await s.commit()
        sent: list[tuple[int, str]] = []

        async def send(due: DueReminder, session: AsyncSession) -> bool:
            await asyncio.sleep(0.02)
            sent.append((due.user.telegram_id, due.reminder.text or ""))
            return True

        tick = dt.datetime(2026, 9, 28, 5, 31, tzinfo=UTC)
        results = await asyncio.gather(
            *(process_due(sessionmaker, send, now=tick) for _ in range(4))
        )
        # The shared test database may contain other users' due reminders.
        assert [x for x in sent if x[0] == tid] == [(tid, "Выпить воды")]
        assert sum(r["sent"] for r in results) == len(sent)
        async with sessionmaker() as s:
            await open_user(s, tid)
            reminder = (await s.execute(select(Reminder).where(Reminder.id == r.id))).scalar_one()
            assert reminder.next_fire_at == dt.datetime(2026, 9, 29, 5, 30, tzinfo=UTC)
            statuses = (await s.execute(select(ReminderDelivery.status))).scalars().all()
            assert statuses == ["sent"]
        # Not due yet: nothing happens.
        before = len(sent)
        await process_due(sessionmaker, send, now=tick + dt.timedelta(minutes=5))
        assert [x for x in sent[before:] if x[0] == tid] == []

    async def test_stale_skip_and_blocked_bot_disables(
        self, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        tid = await make_user(sessionmaker, "UTC")
        t0 = dt.datetime(2026, 9, 28, 6, 0, tzinfo=UTC)
        async with sessionmaker() as s:
            svc = ReminderService(s, await open_user(s, tid))
            r1 = await svc.create("meals", "07:00", now=t0)
            r2 = await svc.create("weigh_in", "12:00", now=t0)
            await s.commit()

        async def ok(due: DueReminder, session: AsyncSession) -> bool:
            return True

        # Server was down: the 07:00 occurrence is 5 hours old -> skipped, not spammed.
        late = dt.datetime(2026, 9, 28, 12, 0, 30, tzinfo=UTC)
        await process_due(sessionmaker, ok, now=late)
        async with sessionmaker() as s:
            await open_user(s, tid)
            got = (
                await s.execute(
                    select(ReminderDelivery.reminder_id, ReminderDelivery.status).order_by(
                        ReminderDelivery.id
                    )
                )
            ).all()
            assert got == [(r1.id, "skipped"), (r2.id, "sent")]

        async def blocked(due: DueReminder, session: AsyncSession) -> bool:
            raise DeliveryBlockedError

        next_day = dt.datetime(2026, 9, 29, 7, 0, 30, tzinfo=UTC)
        await process_due(sessionmaker, blocked, now=next_day)
        async with sessionmaker() as s:
            await open_user(s, tid)
            rows = (await s.execute(select(Reminder).order_by(Reminder.id))).scalars().all()
            assert [(x.id, x.enabled, x.next_fire_at) for x in rows] == [
                (r1.id, False, None),
                (r2.id, False, None),
            ]

    async def test_due_function_exposes_no_other_data(
        self, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        async with sessionmaker() as s:
            cols = (await s.execute(text("SELECT * FROM app_due_reminders(now(), 1)"))).keys()
            assert list(cols) == ["reminder_id", "owner_id", "telegram_id"]
            # Without an identity the runtime role still sees no reminder rows directly.
            direct = await s.execute(text("SELECT count(*) FROM reminders"))
            assert direct.scalar_one() == 0


# --- Strong import ---------------------------------------------------------------------------

STRONG_CSV = (
    "Date,Workout Name,Duration,Exercise Name,Set Order,Weight,Reps,Distance,Seconds,Notes,"
    "Workout Notes,RPE\n"
    "2026-09-20 18:00:00,Верх,1h 5m,Bench Press (Barbell),W,20,15,,,,,\n"
    "2026-09-20 18:00:00,Верх,1h 5m,Bench Press (Barbell),1,60,10,,,,,8\n"
    "2026-09-20 18:00:00,Верх,1h 5m,Bench Press (Barbell),2,60,8,,,,,\n"
    "2026-09-20 18:00:00,Верх,1h 5m,Lat Pulldown (Cable),1,70,12,,,,,\n"
    "bad-date,Верх,1h,Row,1,50,10,,,,,\n"
    "2026-09-22 07:30:00,Кардио,30m,Running,1,,,5000,1800,,,\n"
    "2026-09-22 07:30:00,Кардио,30m,Plank,1,,,,,,,\n"
)


def test_parse_strong_csv() -> None:
    parsed = parse_strong_csv(STRONG_CSV.encode())
    assert [w["name"] for w in parsed.workouts] == ["Верх", "Кардио"]
    top = parsed.workouts[0]
    assert top["duration_s"] == 3900
    bench = top["exercises"]["Bench Press (Barbell)"]
    assert bench[0] == {"reps": 15, "load_kg": "20", "warmup": True}
    assert bench[1]["rpe"] == "8"
    assert parsed.sets == 5
    assert parsed.errors == [(6, "date"), (8, "empty_set")]
    semicolon = STRONG_CSV.replace(",", ";").replace("Weight;", "Weight (lbs);")
    lbs = parse_strong_csv(semicolon.encode())
    assert lbs.weight_unit == "lb"
    assert lbs.workouts[0]["exercises"]["Bench Press (Barbell)"][1]["load_kg"] == "27.2"


@pytest.mark.parametrize(
    "data", [b"", b"\xff\xfe\x00garbage", b"a,b,c\n1,2,3\n", b"Date,Exercise Name\n\x00\x00"]
)
def test_malformed_csv_is_rejected(data: bytes) -> None:
    with pytest.raises(ServiceError):
        parse_strong_csv(data)


@requires_db
async def test_strong_import_is_idempotent_and_owned(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    a, b = await make_user(sessionmaker), await make_user(sessionmaker)
    async with sessionmaker() as s:
        svc = StrongImportService(s, await open_user(s, a))
        batch = await svc.preview(STRONG_CSV.encode())
        assert batch.summary["error_count"] == 2 and batch.summary["duplicates"] == 0
        assert (await svc.preview(STRONG_CSV.encode())).id == batch.id  # same preview
        assert await svc.confirm(batch.id, RU) == 2
        with pytest.raises(Conflict):
            await svc.confirm(batch.id, RU)
        with pytest.raises(Conflict, match="already_imported"):
            await svc.preview(STRONG_CSV.encode())
        # Overlapping export (different file) only adds new workouts.
        extra = STRONG_CSV + "2026-09-24 18:00:00,Низ,50m,Squat,1,100,5,,,,,\n"
        batch2 = await svc.preview(extra.encode())
        assert batch2.summary["duplicates"] == 2
        assert await svc.confirm(batch2.id, RU) == 1
        sessions = (await s.execute(select(WorkoutSession))).scalars().all()
        assert len(sessions) == 3 and {x.source for x in sessions} == {"import"}
        top = next(x for x in sessions if x.template_name == "Верх")
        assert top.local_date == dt.date(2026, 9, 20) and top.values == {"duration": 3900}
        await s.commit()
    async with sessionmaker() as s:
        svc_b = StrongImportService(s, await open_user(s, b))
        with pytest.raises(NotFound):
            await svc_b.confirm(batch.id, RU)
        own = await svc_b.preview(STRONG_CSV.encode())  # same file, separate owner
        assert own.owner_id != batch.owner_id and own.summary["duplicates"] == 0


# --- history, export, deletion -----------------------------------------------------------------


@requires_db
async def test_history_aggregates_only_compatible_metrics(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    tid = await make_user(sessionmaker)
    async with sessionmaker() as s:
        user = await open_user(s, tid)
        svc = ActivityService(s, user)
        swim = await svc.create_type(
            *starter_fields(ActivityKind.SWIMMING, RU), ActivityKind.SWIMMING
        )
        moto = await svc.create_type(
            *starter_fields(ActivityKind.MOTO_RIDE, RU), ActivityKind.MOTO_RIDE
        )
        for meters in ("1000", "1500"):
            ctx = await svc.recording_context(type_id=swim.activity_type_id)
            await svc.record_session(ctx, {"duration": "40", "f3": meters})
        ctx = await svc.recording_context(type_id=moto.activity_type_id)
        await svc.record_session(ctx, {"duration": "120", "f1": "85,5"})
        diary = DiaryService(s, user)
        await diary.add_weight("80,5")
        await diary.add_food("x", energy_kcal=Decimal(500), precision=Precision.MEASURED)
        await diary.add_food("y", energy_kcal=None, precision=Precision.UNKNOWN)
        stats = {st.name: st for st in await activity_stats(s, user)}
        swim_st, moto_st = stats[swim.name], stats[moto.name]
        swim_metrics = {m.label: m.value for m in swim_st.metrics.values()}
        moto_metrics = {m.label: m.value for m in moto_st.metrics.values()}
        assert swim_metrics["Дистанция"] == Decimal(2500)
        assert moto_metrics["Дистанция"] == Decimal("85.5")  # never merged with swim metres
        assert moto_st.counts_as_training is False
        days = await nutrition_history(s, user)
        assert days[0].totals.energy_kcal.value == 500
        assert days[0].totals.energy_kcal.unknown_entries == 1
        assert [w.weight_kg for w in await weight_history(s, user)] == [Decimal("80.50")]
        await s.commit()


@requires_db
async def test_export_contains_only_own_data_and_delete_cascades(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    a, b = await make_user(sessionmaker), await make_user(sessionmaker)
    for tid, name in ((a, "еда A"), (b, "еда B")):
        async with sessionmaker() as s:
            user = await open_user(s, tid)
            food = FoodService(s, user)
            d = await food.draft_from_text(f"{name} 100 г")
            await food.confirm(d.id, d.version)
            await ReminderService(s, user).create("weigh_in", "08:00")
            await ActivityService(s, user).create_type(
                *starter_fields(ActivityKind.STRENGTH, RU), ActivityKind.STRENGTH
            )
            await s.commit()
    async with sessionmaker() as s:
        ua = await open_user(s, a)
        data = json.loads(await export_json(s, ua))
        assert [e["name"] for e in data["food_entries"]] == ["еда A (100 г)"]
        assert "еда B" not in json.dumps(data, ensure_ascii=False)
        assert "owner_id" not in data["food_entries"][0]
        assert data["profile"]["timezone"] == "Europe/Berlin"
        with pytest.raises(ServiceError):
            await delete_account(s, ua, "да")
        await delete_account(s, ua, "удалить")
        await s.commit()
    async with sessionmaker() as s:
        # Owner-role view: nothing of A remains, B untouched.
        counts = {}
        for table in (
            "food_entries",
            "reminders",
            "activity_types",
            "drafts",
            "activity_type_versions",
        ):
            ub = await open_user(s, b)
            query = text(f"SELECT count(*) FROM {table}")  # noqa: S608 - fixed table names
            counts[table] = (await s.execute(query)).scalar_one()
        assert all(v >= 1 for v in counts.values())
        del ub
    async with sessionmaker() as s:
        fresh = await open_user(s, a)  # A starts over as a brand-new user
        assert fresh.onboarding_step == "language"
        assert (await s.execute(select(FoodEntry))).scalars().all() == []
        assert (await s.execute(select(User))).scalars().all() == [fresh]
