"""Service-level behaviour against a real database (synthetic users only)."""

from __future__ import annotations

import asyncio
import datetime as dt
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from fitcoach.ai.gateway import AIGateway, reserve_call
from fitcoach.ai.mock import MockProvider
from fitcoach.ai.types import AIUnavailableError
from fitcoach.config import Settings
from fitcoach.db.models import AiCall, FoodEntry, PlannedWorkout
from fitcoach.domain.fields import FieldDefinition, FieldType, default_duration_field
from fitcoach.domain.nutrition import Precision
from fitcoach.services.activities import ActivityService
from fitcoach.services.diary import DiaryService
from fitcoach.services.drafts import DraftService
from fitcoach.services.errors import Conflict, ServiceError
from fitcoach.services.summary import build_day_summary
from fitcoach.services.users import UserService, local_today
from tests.conftest import make_user, open_user, requires_db

pytestmark = requires_db

LAPS = FieldDefinition(key="f1", label="Круги", type=FieldType.INTEGER, unit="кр")


async def _type_and_template(svc: ActivityService) -> tuple[int, int]:
    tv = await svc.create_type("Мотокросс", [default_duration_field("Время"), LAPS])
    wv = await svc.create_template(tv.activity_type_id, "Трасса", {"duration": "60", "f1": "10"})
    return tv.activity_type_id, wv.template_id


async def test_planning_does_not_mark_completed_and_targets_are_not_copied(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    tid = await make_user(sessionmaker)
    async with sessionmaker() as s:
        user = await open_user(s, tid)
        svc = ActivityService(s, user)
        _, template_id = await _type_and_template(svc)
        planned = await svc.plan(template_id, local_today(user))
        summary = await build_day_summary(s, user)
        assert summary.sessions == []
        assert [p.id for p, _ in summary.planned_open] == [planned.id]
        assert planned.status == "planned"

        ctx = await svc.recording_context(planned_id=planned.id)
        assert ctx.targets == {"duration": 3600, "f1": 10}
        session = await svc.record_session(ctx, {"duration": "45"})  # laps not entered
        assert session.values == {"duration": 2700}
        assert "f1" not in session.values
        await s.refresh(planned)
        assert (planned.status, planned.session_id) == ("completed", session.id)

        with pytest.raises(Conflict):
            await svc.recording_context(planned_id=planned.id)
        with pytest.raises(Conflict):  # replayed submit with stale client context
            await svc.record_session(ctx, {"duration": "45"})
        await s.rollback()


async def test_template_and_type_revisions_preserve_history(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    tid = await make_user(sessionmaker)
    async with sessionmaker() as s:
        user = await open_user(s, tid)
        svc = ActivityService(s, user)
        type_id, template_id = await _type_and_template(svc)
        old = await svc.record_session(
            await svc.recording_context(template_id=template_id), {"duration": "1:00", "f1": "8"}
        )
        old_snapshot = list(old.field_snapshot)

        distance = FieldDefinition(key="f2", label="Дистанция", type=FieldType.DECIMAL, unit="км")
        await svc.revise_type(type_id, [default_duration_field("Время"), LAPS, distance])
        new_wv = await svc.revise_template(template_id, {"f2": "25,5"}, expected_version=1)
        assert new_wv.version == 2
        with pytest.raises(Conflict):
            await svc.revise_template(template_id, {}, expected_version=1)

        new = await svc.record_session(
            await svc.recording_context(template_id=template_id), {"duration": "50", "f2": "20"}
        )
        await s.refresh(old)
        assert old.field_snapshot == old_snapshot
        assert [f["key"] for f in old.field_snapshot] == ["duration", "f1"]
        assert [f["key"] for f in new.field_snapshot] == ["duration", "f1", "f2"]
        assert old.template_version_id != new.template_version_id
        assert new.values["f2"] == "20"

        with pytest.raises(ServiceError):  # changing a key's type would rewrite meaning
            await svc.revise_type(
                type_id,
                [
                    default_duration_field("Время"),
                    FieldDefinition(key="f1", label="Круги", type=FieldType.TEXT),
                ],
            )

        await svc.archive_template(template_id)
        assert await svc.list_templates() == []
        _, _, sessions = await DiaryService(s, user).entries_for_day()
        assert {x.id for x in sessions} >= {old.id, new.id}
        await s.commit()


async def test_custom_field_validation_errors(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    tid = await make_user(sessionmaker)
    async with sessionmaker() as s:
        user = await open_user(s, tid)
        svc = ActivityService(s, user)
        with pytest.raises(ServiceError, match="duration_required"):
            await svc.create_type("Нет времени", [LAPS])
        type_id, _ = await _type_and_template(svc)
        ctx = await svc.recording_context(type_id=type_id)
        for bad in ({"f1": "2.5"}, {"f1": "-1"}, {"zz": "1"}, {}):
            with pytest.raises(ServiceError):
                await svc.record_session(ctx, bad)
        await s.rollback()


async def test_food_edit_uses_optimistic_version_and_delete_is_idempotent(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    tid = await make_user(sessionmaker)
    async with sessionmaker() as s:
        user = await open_user(s, tid)
        diary = DiaryService(s, user)
        entry = await diary.add_food("Суп", energy_kcal=Decimal(200), precision=Precision.MEASURED)
        shown_version = entry.version
        await diary.update_food_energy(entry.id, shown_version, "250,5")
        assert entry.energy_kcal == Decimal("250.5")
        with pytest.raises(Conflict):
            await diary.update_food_energy(entry.id, shown_version, "300")
        await diary.delete("food", entry.id)
        await diary.delete("food", entry.id)
        assert (await diary.entries_for_day())[0] == []
        await diary.restore("food", entry.id)
        assert [f.id for f in (await diary.entries_for_day())[0]] == [entry.id]
        with pytest.raises(ServiceError):
            await diary.add_weight("abc")
        with pytest.raises(ServiceError):
            await diary.add_weight("5")
        w = await diary.add_weight("72,45")
        assert w.weight_kg == Decimal("72.45")
        await s.commit()


async def test_summary_counts_unknown_calories_separately(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    tid = await make_user(sessionmaker)
    async with sessionmaker() as s:
        user = await open_user(s, tid)
        diary = DiaryService(s, user)
        await diary.add_food("A", energy_kcal=Decimal(300), precision=Precision.MEASURED)
        await diary.add_food("B", energy_kcal=None, precision=Precision.UNKNOWN)
        summary = await build_day_summary(s, user)
        assert summary.totals.energy_kcal.value == Decimal(300)
        assert summary.totals.energy_kcal.unknown_entries == 1
        await s.commit()


async def test_local_date_follows_user_timezone(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    moment = dt.datetime(2026, 1, 1, 23, 30, tzinfo=dt.UTC)
    east, west = (
        await make_user(sessionmaker, "Asia/Tokyo"),
        await make_user(sessionmaker, "America/New_York"),
    )
    async with sessionmaker() as s:
        e = await DiaryService(s, await open_user(s, east)).add_weight("70", now=moment)
        await s.commit()
    async with sessionmaker() as s:
        w = await DiaryService(s, await open_user(s, west)).add_weight("70", now=moment)
        await s.commit()
    assert e.local_date == dt.date(2026, 1, 2)
    assert w.local_date == dt.date(2026, 1, 1)


async def test_onboarding_validation(sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    tid = await make_user(sessionmaker)
    async with sessionmaker() as s:
        svc = UserService(s, await open_user(s, tid))
        with pytest.raises(ServiceError):
            await svc.set_timezone("Mars/Olympus")
        with pytest.raises(ServiceError):
            await svc.set_kcal_target("50")
        user = await svc.set_kcal_target("2 100")
        assert user.daily_kcal_target == Decimal(2100)
        assert user.onboarding_step == "done"
        await s.commit()


async def test_draft_confirmation_is_idempotent(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    tid = await make_user(sessionmaker)
    async with sessionmaker() as s:
        user = await open_user(s, tid)
        user.ai_text_consent_at = dt.datetime.now(dt.UTC)
        gateway = AIGateway(MockProvider(), Settings(ai_provider="mock"))
        meal = await gateway.parse_meal(s, user, "гречка, котлета и чай")
        assert [i.name for i in meal.items] == ["гречка", "котлета", "чай"]
        assert all(i.energy_kcal is None for i in meal.items)  # mock never invents numbers
        drafts = DraftService(s, user)
        draft = await drafts.create_meal_draft(meal)
        entries = await drafts.confirm_meal(draft.id)
        assert len(entries) == 3
        with pytest.raises(Conflict):
            await drafts.confirm_meal(draft.id)
        with pytest.raises(Conflict):
            await drafts.cancel(draft.id)
        count = (
            (await s.execute(select(FoodEntry).where(FoodEntry.draft_id == draft.id)))
            .scalars()
            .all()
        )
        assert len(count) == 3
        await s.commit()


async def test_ai_failure_budget_and_consent_keep_manual_mode_working(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    tid = await make_user(sessionmaker)
    async with sessionmaker() as s:
        user = await open_user(s, tid)
        gateway = AIGateway(
            MockProvider(fail=True), Settings(ai_provider="mock", ai_user_daily_calls=2)
        )
        with pytest.raises(AIUnavailableError, match="no_consent"):
            await gateway.parse_meal(s, user, "суп")
        user.ai_text_consent_at = dt.datetime.now(dt.UTC)
        with pytest.raises(AIUnavailableError, match="input_too_long"):
            await gateway.parse_meal(s, user, "x" * 5000)
        for _ in range(2):
            with pytest.raises(AIUnavailableError, match="provider_error"):
                await gateway.parse_meal(s, user, "суп")
        gateway.breaker.success()  # isolate the budget check from the circuit breaker
        with pytest.raises(AIUnavailableError, match="budget_exhausted"):
            await gateway.parse_meal(s, user, "суп")
        statuses = await s.execute(select(AiCall.status).where(AiCall.owner_id == user.id))
        assert sorted(statuses.scalars().all()) == ["provider_error", "provider_error"]
        entry = await DiaryService(s, user).add_food(
            "суп", energy_kcal=None, precision=Precision.UNKNOWN
        )
        await s.commit()
        assert entry.id


async def test_circuit_breaker_opens_after_repeated_failures(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    tid = await make_user(sessionmaker)
    async with sessionmaker() as s:
        user = await open_user(s, tid)
        user.ai_text_consent_at = dt.datetime.now(dt.UTC)
        provider = MockProvider(fail=True)
        gateway = AIGateway(provider, Settings(ai_provider="mock", ai_user_daily_calls=10))
        for _ in range(3):
            with pytest.raises(AIUnavailableError, match="provider_error"):
                await gateway.parse_meal(s, user, "суп")
        with pytest.raises(AIUnavailableError, match="circuit_open"):
            await gateway.parse_meal(s, user, "суп")
        assert provider.calls == 3


async def test_parallel_budget_reservations_never_exceed_limit(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    tid = await make_user(sessionmaker)
    day = dt.date(2030, 1, 1)

    async def attempt() -> bool:
        async with sessionmaker() as s:
            user = await open_user(s, tid)
            try:
                await reserve_call(s, user, user_limit=3, global_limit=1000, day=day)
                await asyncio.sleep(0.05)  # hold the row lock while others wait
                await s.commit()
                return True
            except AIUnavailableError:
                await s.rollback()
                return False

    results = await asyncio.gather(*(attempt() for _ in range(8)))
    assert sum(results) == 3


async def test_plan_rejects_past_dates(sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    tid = await make_user(sessionmaker)
    async with sessionmaker() as s:
        user = await open_user(s, tid)
        svc = ActivityService(s, user)
        _, template_id = await _type_and_template(svc)
        with pytest.raises(ServiceError, match="bad_date"):
            await svc.plan(template_id, local_today(user) - dt.timedelta(days=1))
        assert (await s.execute(select(PlannedWorkout))).scalars().all() == []
        await s.rollback()
