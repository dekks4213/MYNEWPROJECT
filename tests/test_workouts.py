"""Universal workout builder: blocks, starters, programs, text drafts, isolation."""

from __future__ import annotations

import asyncio
import datetime as dt
from decimal import Decimal

import pytest
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from fitcoach.ai.gateway import AIGateway
from fitcoach.ai.types import ActivitySchemaDraft, FieldDraftAI, WorkoutParse
from fitcoach.config import Settings
from fitcoach.db.models import WorkoutSession
from fitcoach.domain.fields import FieldType
from fitcoach.domain.starters import STARTER_KINDS, starter_fields
from fitcoach.domain.units import ParseError
from fitcoach.domain.workout import (
    ActivityKind,
    Block,
    BlockKind,
    Item,
    SetSpec,
    SetWords,
    WorkoutBody,
    format_item,
    pace_per_100m,
    parse_sets,
    parse_strength_text,
    totals,
)
from fitcoach.i18n import Translator
from fitcoach.services.activities import ActivityService
from fitcoach.services.errors import Conflict, NotFound, ServiceError
from fitcoach.services.users import local_today
from fitcoach.services.workout_drafts import (
    ActivityDraftService,
    WorkoutDraftService,
    to_field_definitions,
)
from tests.conftest import make_user, open_user, requires_db
from tests.fakes import ScriptedProvider

D = Decimal
RU = Translator("ru")
WORDS = SetWords("м", "с", "мин", "отдых", "разм.")


def test_set_notation() -> None:
    assert [(s.load_kg, s.reps) for s in parse_sets("60x10 60х10, 60*8")] == [
        (D(60), 10),
        (D(60), 10),
        (D(60), 8),
    ]
    assert [(s.load_kg, s.reps) for s in parse_sets("3x10", default_load=D(50))] == [
        (D(50), 10)
    ] * 3
    assert [s.distance_m for s in parse_sets("4x50м 200m")] == [50, 50, 50, 50, 200]
    assert len(parse_sets("2x20x15")) == 2
    for bad in ("", "abc", "60x", "1.5", "4x50.5м", "60x10x"):
        with pytest.raises(ParseError):
            parse_sets(bad)
    with pytest.raises(ParseError):
        parse_sets("60x10 " * 60)


def test_strength_text_and_totals() -> None:
    body = parse_strength_text("жим 60 кг 10 10 8,\nтяга вертикального блока 70 кг 12 12 10")
    bench, row = body.blocks[0].items
    assert bench.name == "жим" and row.name == "тяга вертикального блока"
    t = totals(body)
    assert (t.sets, t.reps, t.volume_kg) == (6, 62, D(60 * 28 + 70 * 34))
    assert format_item(bench, WORDS) == "жим: 2×(60×10); 60×8"
    with pytest.raises(ParseError):
        parse_strength_text("просто поплавал")


def test_warmups_rounds_and_pace() -> None:
    body = WorkoutBody(
        blocks=(
            Block(
                kind=BlockKind.WARMUP,
                items=(Item(name="Жим", sets=(SetSpec(load_kg=D(20), reps=15, warmup=True),)),),
            ),
            Block(
                kind=BlockKind.INTERVALS,
                rounds=5,
                items=(Item(name="Кроль", sets=(SetSpec(distance_m=200, rest_s=60),)),),
            ),
        )
    )
    t = totals(body)
    assert t.volume_kg is None and t.working_sets == 5 and t.distance_m == 1000
    assert pace_per_100m(1500, 1000) == 150
    assert pace_per_100m(None, 1000) is None
    assert WorkoutBody.load(body.dump()) == body
    with pytest.raises(ValidationError):
        WorkoutBody.load([{"kind": "main", "items": [{"name": "x", "exec": "rm -rf"}]}])


def test_starters_are_valid_and_localized() -> None:
    from fitcoach.domain.fields import FieldSchema

    for kind in STARTER_KINDS:
        name, fields = starter_fields(kind, RU)
        FieldSchema(fields=tuple(fields))
        assert not name.startswith("starter.")
    assert starter_fields(ActivityKind.SWIMMING, RU)[1][2].unit == "м"


@requires_db
class TestWorkoutServices:
    async def test_templates_with_blocks_keep_history_and_targets_are_not_results(
        self, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        tid = await make_user(sessionmaker)
        async with sessionmaker() as s:
            user = await open_user(s, tid)
            svc = ActivityService(s, user)
            name, fields = starter_fields(ActivityKind.STRENGTH, RU)
            tv = await svc.create_type(name, fields, ActivityKind.STRENGTH)
            plan_body = WorkoutBody(
                blocks=(
                    Block(
                        items=(
                            Item(
                                name="Жим",
                                sets=(
                                    SetSpec(load_kg=D(20), reps=15, warmup=True),
                                    SetSpec(load_kg=D(60), reps=10),
                                    SetSpec(load_kg=D(60), reps=10),
                                ),
                            ),
                        )
                    ),
                )
            )
            wv = await svc.create_template(tv.activity_type_id, "Верх", blocks=plan_body)
            ctx = await svc.recording_context(template_id=wv.template_id)
            assert WorkoutBody.load(list(ctx.target_blocks)) == plan_body
            actual = WorkoutBody(
                blocks=(Block(items=(Item(name="Жим", sets=(SetSpec(load_kg=D(60), reps=8),)),)),)
            )
            old = await svc.record_session(ctx, {"duration": "50"}, blocks=actual)
            assert old.blocks == actual.dump() and old.blocks != plan_body.dump()

            new_body = WorkoutBody(
                blocks=(Block(items=(Item(name="Жим", sets=(SetSpec(load_kg=D(65), reps=8),)),)),)
            )
            v2 = await svc.revise_template(wv.template_id, {}, expected_version=1, blocks=new_body)
            await s.refresh(old)
            assert old.blocks == actual.dump() and old.template_version_id == wv.id
            assert v2.blocks == new_body.dump()
            v3 = await svc.revise_template(wv.template_id, {}, expected_version=2, name="Верх 2")
            assert v3.blocks == new_body.dump()  # blocks carried over when not changed
            with pytest.raises(ServiceError, match="empty_session"):
                await svc.record_session(ctx, {})
            await s.commit()

    async def test_moto_ride_is_not_training(
        self, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        tid = await make_user(sessionmaker)
        async with sessionmaker() as s:
            svc = ActivityService(s, await open_user(s, tid))
            name, fields = starter_fields(ActivityKind.MOTO_RIDE, RU)
            tv = await svc.create_type(name, fields, ActivityKind.MOTO_RIDE)
            activity = await svc.get_type(tv.activity_type_id)
            assert activity.kind == "moto_ride" and activity.counts_as_training is False
            assert all("кал" not in f["label"].lower() for f in tv.fields)
            await s.commit()

    async def test_programs_and_weekday_planning(
        self, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        a, b = await make_user(sessionmaker), await make_user(sessionmaker)
        async with sessionmaker() as s:
            ua = await open_user(s, a)
            svc = ActivityService(s, ua)
            gym = await svc.create_program("Зал 3 раза в неделю")
            swim = await svc.create_program("Плавание")
            name, fields = starter_fields(ActivityKind.STRENGTH, RU)
            tv = await svc.create_type(name, fields, ActivityKind.STRENGTH)
            wv = await svc.create_template(tv.activity_type_id, "Верх", program_id=gym.id)
            assert [t.id for t in await svc.program_templates(gym.id)] == [wv.template_id]
            assert await svc.program_templates(swim.id) == []
            created = await svc.plan_weekdays(wv.template_id, {0, 2, 4}, weeks=2)
            assert 5 <= len(created) <= 7
            assert all(p.planned_date.weekday() in {0, 2, 4} for p in created)
            again = await svc.plan_weekdays(wv.template_id, {0, 2, 4}, weeks=2)
            assert again == []  # no duplicates
            assert all(p.status == "planned" for p in created)
            await s.commit()
        async with sessionmaker() as s:
            ub = await open_user(s, b)
            svc_b = ActivityService(s, ub)
            with pytest.raises(NotFound):
                await svc_b.get_program(gym.id)
            with pytest.raises(NotFound):
                await svc_b.plan_weekdays(wv.template_id, {1})
            with pytest.raises(NotFound):
                await svc_b.recording_context(planned_id=created[0].id)

    async def test_text_workout_draft_flow(
        self, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        tid = await make_user(sessionmaker)
        async with sessionmaker() as s:
            user = await open_user(s, tid)
            drafts = WorkoutDraftService(s, user)
            d = await drafts.draft_from_text("жим 60 кг 10 10 8, тяга блока 70 кг 12 12 10")
            _, state = await drafts.get(d.id)
            assert state.kind is ActivityKind.STRENGTH and state.type_id is None
            with pytest.raises(ServiceError, match="choose_type"):
                await drafts.confirm(d.id, d.version)
            d = await drafts.create_starter_type(d.id, d.version, RU)
            session = await drafts.confirm(d.id, d.version)
            assert session.source == "text" and len(session.blocks[0]["items"]) == 2
            with pytest.raises(Conflict):
                await drafts.confirm(d.id, d.version)
            # Without AI, free text that is not set notation is rejected, not guessed.
            with pytest.raises(ServiceError, match="bad_workout_text"):
                await drafts.draft_from_text("сегодня плавал 1200 метров")
            planned = await ActivityService(s, user).list_planned(
                local_today(user), local_today(user)
            )
            assert planned == []  # logging never creates plans
            await s.commit()

    async def test_ai_swim_draft_maps_distance_into_meters(
        self, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        tid = await make_user(sessionmaker)
        parse = WorkoutParse(
            kind=ActivityKind.SWIMMING,
            distance_km=D("1.2"),
            duration_min=D(40),
            blocks=[
                Block(
                    items=(
                        Item(
                            name="кроль",
                            sets=tuple(
                                SetSpec(distance_m=100, stroke="кроль", rest_s=60) for _ in range(5)
                            ),
                        ),
                    )
                )
            ],
        )
        async with sessionmaker() as s:
            user = await open_user(s, tid)
            user.ai_text_consent_at = dt.datetime.now(dt.UTC)
            name, fields = starter_fields(ActivityKind.SWIMMING, RU)
            tv = await ActivityService(s, user).create_type(name, fields, ActivityKind.SWIMMING)
            gw = AIGateway(ScriptedProvider(workout=parse), Settings(ai_provider="mock"))
            drafts = WorkoutDraftService(s, user, gw)
            d = await drafts.draft_from_text("сегодня плавал 1200 метров, из них 5 по 100 кролем")
            _, state = await drafts.get(d.id)
            assert state.type_id == tv.activity_type_id and state.ai
            session = await drafts.confirm(d.id, d.version)
            assert session.values == {"duration": 2400, "f3": 1200}
            assert session.source == "ai_draft"
            await s.commit()

    async def test_foreign_types_and_drafts_are_rejected(
        self, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        a, b = await make_user(sessionmaker), await make_user(sessionmaker)
        async with sessionmaker() as s:
            ua = await open_user(s, a)
            name, fields = starter_fields(ActivityKind.ENDURO, RU)
            tv = await ActivityService(s, ua).create_type(name, fields, ActivityKind.ENDURO)
            da = await WorkoutDraftService(s, ua).draft_from_text("присед 100 кг 5 5 5")
            await s.commit()
        async with sessionmaker() as s:
            ub = await open_user(s, b)
            drafts_b = WorkoutDraftService(s, ub)
            db = await drafts_b.draft_from_text("присед 80 кг 5 5 5")
            with pytest.raises(NotFound):
                await drafts_b.set_type(db.id, db.version, tv.activity_type_id)
            with pytest.raises(NotFound):
                await drafts_b.confirm(da.id, da.version)
            with pytest.raises(NotFound):
                await ActivityService(s, ub).recording_context(type_id=tv.activity_type_id)

    async def test_concurrent_workout_confirmations(
        self, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        tid = await make_user(sessionmaker)
        async with sessionmaker() as s:
            user = await open_user(s, tid)
            drafts = WorkoutDraftService(s, user)
            d = await drafts.draft_from_text("тяга 100 кг 5 5")
            d = await drafts.create_starter_type(d.id, d.version, RU)
            draft_id, version = d.id, d.version
            await s.commit()

        async def confirm() -> bool:
            async with sessionmaker() as s:
                try:
                    await WorkoutDraftService(s, await open_user(s, tid)).confirm(draft_id, version)
                    await asyncio.sleep(0.05)
                    await s.commit()
                    return True
                except Conflict:
                    await s.rollback()
                    return False

        assert sorted(await asyncio.gather(confirm(), confirm())) == [False, True]
        async with sessionmaker() as s:
            await open_user(s, tid)
            rows = (await s.execute(select(WorkoutSession))).scalars().all()
            assert len(rows) == 1

    async def test_ai_activity_schema_is_converted_server_side(
        self, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        tid = await make_user(sessionmaker)
        proposal = ActivitySchemaDraft(
            name="Плавание",
            kind=ActivityKind.SWIMMING,
            fields=[
                FieldDraftAI(label="Дистанция", type=FieldType.INTEGER, unit="м"),
                FieldDraftAI(label="Стиль", type=FieldType.SELECTION, choices=["кроль", "брасс"]),
                FieldDraftAI(label="Интервал", type=FieldType.DURATION, duration_format="mm:ss"),
            ],
        )
        async with sessionmaker() as s:
            user = await open_user(s, tid)
            user.ai_text_consent_at = dt.datetime.now(dt.UTC)
            gw = AIGateway(ScriptedProvider(activity=proposal), Settings(ai_provider="mock"))
            svc = ActivityDraftService(s, user, gw)
            d = await svc.draft("Создай тренировку Плавание. Дистанция, время, стиль, интервалы")
            tv = await svc.confirm(d.id, d.version, "Длительность")
            assert [f["key"] for f in tv.fields] == ["duration", "f1", "f2", "f3"]
            activity = await ActivityService(s, user).get_type(tv.activity_type_id)
            assert activity.kind == "swimming"
            with pytest.raises(Conflict):
                await svc.confirm(d.id, d.version, "Длительность")
            await s.commit()
        bad = ActivitySchemaDraft(
            name="X", fields=[FieldDraftAI(label="Выбор", type=FieldType.SELECTION)]
        )
        with pytest.raises(ServiceError, match="bad_field"):
            to_field_definitions(bad, "Длительность")
        with pytest.raises(ValidationError):  # the model cannot smuggle keys or owners
            FieldDraftAI.model_validate({"label": "x", "type": "text", "key": "duration"})
