"""🏋️ Training: today's workout, step-by-step recording, creating workouts (gym, swimming,
enduro, anything else), programs, plans and Strong CSV import. Works fully without AI."""

from __future__ import annotations

import datetime as dt
import io
from typing import Any

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from fitcoach.ai.gateway import AIGateway
from fitcoach.bot.handlers.common import Event
from fitcoach.bot.screen import answer, progress, render, replace
from fitcoach.bot.ui import (
    Ac,
    Fd,
    Go,
    Row,
    Wd,
    day_month,
    field_icon,
    field_input_kb,
    field_line,
    field_prompt,
    field_value_text,
    format_body,
    grid,
    inline,
    minutes_text,
    nav,
    num,
    rel_day,
    session_duration,
    session_title,
    sets_text,
)
from fitcoach.config import Settings
from fitcoach.db.models import ActivityType, Draft, User
from fitcoach.domain.fields import (
    DURATION_KEY,
    MAX_FIELDS,
    FieldDefinition,
    FieldSchema,
    default_duration_field,
    next_custom_key,
    parse_field_value,
)
from fitcoach.domain.starters import STARTER_KINDS, starter_fields
from fitcoach.domain.units import ParseError
from fitcoach.domain.workout import (
    ActivityKind,
    Block,
    Item,
    SetSpec,
    WorkoutBody,
    compare_sets,
    parse_plan_text,
    parse_sets,
    totals,
)
from fitcoach.i18n import Translator, all_labels
from fitcoach.services.activities import ActivityService
from fitcoach.services.errors import ServiceError
from fitcoach.services.strong_import import StrongImportService
from fitcoach.services.users import UserService, local_today, utcnow
from fitcoach.services.workout_drafts import ActivityDraftService, WorkoutDraftService

router = Router(name="training")
WEEKDAY_KEYS = ("wd.mon", "wd.tue", "wd.wed", "wd.thu", "wd.fri", "wd.sat", "wd.sun")
KIND_ICONS = {
    "strength": "🏋️",
    "swimming": "🏊",
    "enduro": "🏍",
    "moto_ride": "🛵",
    "custom": "🏃",
}
# What people want to track -> how it is stored. Types stay hidden from the user.
MEASURES = ("time", "distance", "count", "rating", "note", "other")
OTHER_MEASURES = ("bool", "list", "number")
STRENGTH_NAMES = ("tpl.suggest.upper", "tpl.suggest.lower", "tpl.suggest.full")
POOLS = ("25", "50")


class TypeSG(StatesGroup):
    name = State()
    builder = State()
    field_label = State()
    field_unit = State()
    field_choices = State()
    ai_text = State()


class TplSG(StatesGroup):
    name = State()
    pool = State()
    blocks = State()
    review = State()


class ValuesSG(StatesGroup):
    item = State()
    item_done = State()
    value = State()
    extra_blocks = State()
    review = State()


class TextSG(StatesGroup):
    workout = State()


class ProgramSG(StatesGroup):
    name = State()


class ImportSG(StatesGroup):
    file = State()


def _schema(fields: list[dict[str, Any]]) -> FieldSchema:
    return FieldSchema.model_validate({"fields": fields})


def _parse(fn: Any, *args: Any, **kwargs: Any) -> Any:
    try:
        return fn(*args, **kwargs)
    except ParseError as exc:
        raise ServiceError(exc.code) from exc


def _icon(kind: str | None) -> str:
    return KIND_ICONS.get(kind or "custom", KIND_ICONS["custom"])


# --- training home -----------------------------------------------------------------------------


async def show_training(
    event: Event, session: AsyncSession, user: User, tr: Translator, state: FSMContext
) -> None:
    await state.clear()
    svc = ActivityService(session, user)
    today = local_today(user)
    planned = await svc.list_planned(today, today)
    done_today = [s for s in await svc.list_sessions(since=today, limit=5) if s.local_date == today]
    lines = [tr("act.title"), ""]
    rows: list[Row] = []
    if planned:
        p, v = planned[0]
        duration = v.targets.get(DURATION_KEY)
        extra = f" · ~{minutes_text(tr, duration)}" if isinstance(duration, int) else ""
        lines += [tr("act.today"), f"{v.name}{extra}"]
        rows.append([(tr("act.start"), Ac(action="rec_plan", id=p.id))])
    elif done_today:
        duration = session_duration(done_today[0])
        extra = f" · {minutes_text(tr, duration)}" if duration else ""
        lines.append(tr("act.done_today", name=session_title(done_today[0]) + extra))
    else:
        lines.append(tr("act.none_today"))
    if planned or done_today:
        rows.append(
            [(tr("act.new"), Ac(action="create")), (tr("act.mine"), Ac(action="templates"))]
        )
    else:
        rows.append(
            [
                (tr("act.create_short"), Ac(action="create")),
                (tr("act.pick_template"), Ac(action="templates")),
            ]
        )
    rows.append(
        [(tr("act.programs"), Ac(action="programs")), (tr("act.history"), Ac(action="history"))]
    )
    rows.append([(tr("act.record_done"), Ac(action="rec_menu"))])
    rows.append(nav(tr))
    await answer(event)
    await render(event, state, "\n".join(lines), inline(*rows))


@router.message(F.text.in_(all_labels("menu.training") | all_labels("menu.old_training")))
async def training_menu(
    message: Message, session: AsyncSession, user: User, tr: Translator, state: FSMContext
) -> None:
    await show_training(message, session, user, tr, state)


@router.callback_query(Go.filter(F.s == "train"))
async def training_menu_cb(
    query: CallbackQuery, session: AsyncSession, user: User, tr: Translator, state: FSMContext
) -> None:
    await show_training(query, session, user, tr, state)


@router.callback_query(Ac.filter(F.action == "menu"))
async def training_menu_ac(
    query: CallbackQuery, session: AsyncSession, user: User, tr: Translator, state: FSMContext
) -> None:
    await show_training(query, session, user, tr, state)


@router.callback_query(Ac.filter(F.action == "history"))
async def training_history(
    query: CallbackQuery, session: AsyncSession, user: User, tr: Translator, state: FSMContext
) -> None:
    await state.clear()
    items = await ActivityService(session, user).list_sessions(limit=10)
    today = local_today(user)
    lines = [tr("act.history_title"), ""]
    for s in items:
        duration = session_duration(s)
        extra = f" · {minutes_text(tr, duration)}" if duration else ""
        lines.append(f"✓ {session_title(s)}{extra} · {rel_day(tr, s.local_date, today)}")
    if not items:
        lines.append(tr("hist.empty"))
    await answer(query)
    await render(
        query,
        state,
        "\n".join(lines),
        inline([(tr("act.stats"), Go(s="hist", a="stats"))], nav(tr, Go(s="train"))),
    )


# --- plan (home "🗓 План") ----------------------------------------------------------------------


@router.callback_query(Go.filter(F.s == "plan"))
async def plan_screen(
    query: CallbackQuery, session: AsyncSession, user: User, tr: Translator, state: FSMContext
) -> None:
    await state.clear()
    today = local_today(user)
    items = await ActivityService(session, user).list_planned(today, today + dt.timedelta(days=7))
    lines = [tr("plan.title"), ""]
    rows: list[Row] = []
    for p, v in items[:8]:
        when = rel_day(tr, p.planned_date, today)
        if p.planned_date != today:
            when = tr(WEEKDAY_KEYS[p.planned_date.weekday()]) + ", " + day_month(tr, p.planned_date)
        lines.append(f"○ {when} · {v.name}")
        if p.planned_date == today:
            rows.append([(f"▶️ {v.name}", Ac(action="rec_plan", id=p.id))])
    if not items:
        lines.append(tr("plan.none"))
    rows.append([(tr("plan.add"), Ac(action="templates"))])
    rows.append(nav(tr))
    await answer(query)
    await render(query, state, "\n".join(lines), inline(*rows))


# --- create ------------------------------------------------------------------------------------


@router.callback_query(Ac.filter(F.action == "create"))
async def create_menu(
    query: CallbackQuery, user: User, tr: Translator, gateway: AIGateway, state: FSMContext
) -> None:
    await state.clear()
    await answer(query)
    rows: list[Row] = []
    if gateway.enabled:
        rows.append([(tr("act.describe_ai"), Ac(action="ai_type"))])
    rows.append(
        [
            (tr("act.kind.strength"), Ac(action="new", id=0)),
            (tr("act.kind.swimming"), Ac(action="new", id=1)),
        ]
    )
    rows.append(
        [(tr("act.kind.enduro"), Ac(action="new", id=2)), (tr("act.other"), Ac(action="other"))]
    )
    rows.append(nav(tr, Go(s="train")))
    await render(query, state, tr("act.create_ask"), inline(*rows))


@router.callback_query(Ac.filter(F.action == "other"))
async def create_other(query: CallbackQuery, tr: Translator, state: FSMContext) -> None:
    await answer(query)
    await render(
        query,
        state,
        tr("act.other_ask"),
        inline(
            [(tr("act.kind.moto_ride"), Ac(action="new", id=3))],
            [(tr("act.custom"), Ac(action="new_type"))],
            nav(tr, Ac(action="create")),
        ),
    )


async def _ensure_type(
    session: AsyncSession, user: User, tr: Translator, kind: ActivityKind
) -> ActivityType:
    """Reuse the user's activity of this kind; create it from the starter only once."""
    svc = ActivityService(session, user)
    for t in await svc.list_types():
        if t.kind == kind.value:
            return t
    name, fields = starter_fields(kind, tr)
    version = await svc.create_type(name, fields, kind)
    return await svc.get_type(version.activity_type_id)


@router.callback_query(Ac.filter(F.action == "new"))
async def create_starter(
    query: CallbackQuery,
    callback_data: Ac,
    session: AsyncSession,
    user: User,
    tr: Translator,
    state: FSMContext,
) -> None:
    if not 0 <= callback_data.id < len(STARTER_KINDS):
        raise ServiceError("bad_choice")
    kind = STARTER_KINDS[callback_data.id]
    activity = await _ensure_type(session, user, tr, kind)
    await session.commit()
    await answer(query)
    if kind in (ActivityKind.STRENGTH, ActivityKind.SWIMMING):
        await _tpl_begin(query, session, user, tr, state, activity.id)
    else:
        await _type_ready(query, session, user, tr, state, activity.id)


async def _type_ready(
    event: Event,
    session: AsyncSession,
    user: User,
    tr: Translator,
    state: FSMContext,
    type_id: int,
    *,
    created: bool = False,
) -> None:
    svc = ActivityService(session, user)
    activity = await svc.get_type(type_id)
    tv = await svc.current_type_version(type_id)
    fields = _schema(tv.fields).fields
    head = f"{_icon(activity.kind)} {activity.name}"
    lines = [tr("type.saved", name=activity.name) if created else head, "", tr("type.tracks")]
    lines += [field_line(tr, f) for f in fields]
    if activity.kind == ActivityKind.MOTO_RIDE.value:
        lines += ["", tr("type.moto_note")]
    await state.clear()
    await render(
        event,
        state,
        "\n".join(lines),
        inline(
            [(tr("act.record_now"), Ac(action="rec_type", id=type_id))],
            [(tr("tpl.new_from_type"), Ac(action="tpl_type", id=type_id))],
            nav(tr, Ac(action="create")),
        ),
    )


# --- custom builder: "what do you want to track?" ----------------------------------------------


@router.callback_query(Ac.filter(F.action == "new_type"))
async def new_type(query: CallbackQuery, tr: Translator, state: FSMContext) -> None:
    await answer(query)
    await state.clear()
    await state.set_state(TypeSG.name)
    await render(query, state, tr("type.ask_name"), inline(nav(tr, Ac(action="other"), home=False)))


@router.message(TypeSG.name, F.text)
async def type_name(message: Message, tr: Translator, state: FSMContext) -> None:
    assert message.text is not None
    name = " ".join(message.text.split())
    if not name or len(name) > 60:
        raise ServiceError("bad_name")
    duration = default_duration_field(tr("type.duration_label"))
    await state.update_data(name=name, fields=[duration.model_dump(mode="json", exclude_none=True)])
    await _show_builder(message, tr, state)


async def _show_builder(event: Event, tr: Translator, state: FSMContext) -> None:
    await state.set_state(TypeSG.builder)
    data = await state.get_data()
    schema = _schema(data["fields"])
    lines = [f"🛠 {data['name']}", "", tr("type.tracks")]
    lines += [field_line(tr, f) for f in schema.fields]
    rows: list[Row] = []
    if len(schema.fields) < MAX_FIELDS:
        lines += ["", tr("type.what_else")]
        rows += grid([(tr(f"measure.{m}"), Fd(action="measure", value=m)) for m in MEASURES])
    rows.append(
        [(tr("type.done"), Fd(action="save_type")), (tr("nav.cancel"), Fd(action="cancel"))]
    )
    await render(event, state, "\n".join(lines), inline(*rows))


@router.callback_query(TypeSG.builder, Fd.filter(F.action == "measure"))
@router.callback_query(TypeSG.field_label, Fd.filter(F.action == "measure"))
async def pick_measure(
    query: CallbackQuery, callback_data: Fd, tr: Translator, state: FSMContext
) -> None:
    measure = callback_data.value
    await answer(query)
    if measure == "other":
        await render(
            query,
            state,
            tr("measure.other_ask"),
            inline(
                *grid(
                    [(tr(f"measure.{m}"), Fd(action="measure", value=m)) for m in OTHER_MEASURES]
                ),
                nav(tr, Fd(action="builder"), home=False),
            ),
        )
        return
    if measure not in MEASURES + OTHER_MEASURES:
        raise ServiceError("bad_choice")
    await state.update_data(measure=measure)
    await state.set_state(TypeSG.field_label)
    default = tr(f"measure.{measure}.label")
    await render(
        query,
        state,
        tr("type.ask_field_label"),
        inline(
            [(f"✓ {default}", Fd(action="mname"))],
            nav(tr, Fd(action="builder"), home=False),
        ),
    )


@router.callback_query(Fd.filter(F.action == "builder"))
async def back_to_builder(query: CallbackQuery, tr: Translator, state: FSMContext) -> None:
    data = await state.get_data()
    if "fields" not in data:
        await answer(query, tr("stale_button"))
        return
    await answer(query)
    await _show_builder(query, tr, state)


async def _after_label(event: Event, tr: Translator, state: FSMContext, label: str) -> None:
    label = " ".join(label.split())
    if not label or len(label) > 40:
        raise ServiceError("bad_name")
    data = await state.get_data()
    measure = data.get("measure")
    await state.update_data(label=label)
    if measure == "distance":
        await state.set_state(TypeSG.field_unit)
        await render(
            event,
            state,
            tr("type.ask_distance_unit"),
            inline(
                [
                    (tr("unit.km"), Fd(action="unit", value="km")),
                    (tr("unit.m"), Fd(action="unit", value="m")),
                ],
                nav(tr, Fd(action="builder"), home=False),
            ),
        )
    elif measure == "number":
        await state.set_state(TypeSG.field_unit)
        await render(
            event,
            state,
            tr("type.ask_unit"),
            inline(
                [(tr("type.no_unit"), Fd(action="unit", value=""))],
                nav(tr, Fd(action="builder"), home=False),
            ),
        )
    elif measure == "list":
        await state.set_state(TypeSG.field_choices)
        await render(
            event, state, tr("type.ask_choices"), inline(nav(tr, Fd(action="builder"), home=False))
        )
    else:
        await _finish_field(event, tr, state, {})


@router.message(TypeSG.field_label, F.text)
async def field_label(message: Message, tr: Translator, state: FSMContext) -> None:
    assert message.text is not None
    await _after_label(message, tr, state, message.text)


@router.callback_query(TypeSG.field_label, Fd.filter(F.action == "mname"))
async def field_label_default(query: CallbackQuery, tr: Translator, state: FSMContext) -> None:
    data = await state.get_data()
    await answer(query)
    await _after_label(query, tr, state, tr(f"measure.{data.get('measure')}.label"))


@router.message(TypeSG.field_unit, F.text)
async def field_unit(message: Message, tr: Translator, state: FSMContext) -> None:
    assert message.text is not None
    await _finish_field(message, tr, state, {"unit": message.text.strip()[:12]})


@router.callback_query(TypeSG.field_unit, Fd.filter(F.action == "unit"))
async def field_unit_button(
    query: CallbackQuery, callback_data: Fd, tr: Translator, state: FSMContext
) -> None:
    await answer(query)
    unit = {"km": tr("unit.km"), "m": tr("unit.m")}.get(callback_data.value)
    await _finish_field(query, tr, state, {"unit": unit} if unit else {})


@router.message(TypeSG.field_choices, F.text)
async def field_choices(message: Message, tr: Translator, state: FSMContext) -> None:
    assert message.text is not None
    choices = [c.strip() for c in message.text.split(",") if c.strip()]
    await _finish_field(message, tr, state, {"choices": choices})


def _measure_spec(measure: str) -> dict[str, Any]:
    return {
        "time": {"type": "duration", "duration_format": "h:mm", "aggregation": "sum"},
        "distance": {"type": "decimal", "aggregation": "sum"},
        "count": {"type": "integer", "aggregation": "sum"},
        "rating": {"type": "integer", "min_value": "1", "max_value": "5"},
        "note": {"type": "text"},
        "bool": {"type": "boolean"},
        "list": {"type": "selection"},
        "number": {"type": "decimal"},
    }[measure]


async def _finish_field(
    event: Event, tr: Translator, state: FSMContext, extra: dict[str, Any]
) -> None:
    data = await state.get_data()
    if "fields" not in data or data.get("measure") not in MEASURES + OTHER_MEASURES:
        raise ServiceError("bad_field")
    fields = _schema(data["fields"]).fields
    raw = {
        **_measure_spec(data["measure"]),
        **extra,
        "label": data["label"],
        "key": next_custom_key(fields),
    }
    try:
        field = FieldDefinition.model_validate(raw)
        dumped = field.model_dump(mode="json", exclude_none=True)
        _schema([*data["fields"], dumped])
    except ValidationError as exc:
        raise ServiceError("bad_field") from exc
    await state.update_data(fields=[*data["fields"], dumped], measure=None, label=None)
    await _show_builder(event, tr, state)


@router.callback_query(TypeSG.builder, Fd.filter(F.action == "save_type"))
async def save_type(
    query: CallbackQuery, session: AsyncSession, user: User, tr: Translator, state: FSMContext
) -> None:
    data = await state.get_data()
    fields = list(_schema(data["fields"]).fields)
    version = await ActivityService(session, user).create_type(data["name"], fields)
    await session.commit()
    await answer(query, tr("saved"))
    await _type_ready(query, session, user, tr, state, version.activity_type_id, created=True)


# --- "✨ Describe in words" (AI draft of an activity) -------------------------------------------


@router.callback_query(Ac.filter(F.action == "ai_type"))
async def ai_type_start(
    query: CallbackQuery, user: User, tr: Translator, gateway: AIGateway, state: FSMContext
) -> None:
    await answer(query)
    if not gateway.text_available(user):
        await render(
            query,
            state,
            tr("ai.consent_text"),
            inline(
                [
                    (tr("ai.allow_btn"), Ac(action="ai_allow")),
                    (tr("ai.not_now"), Ac(action="create")),
                ]
            ),
        )
        return
    await state.set_state(TypeSG.ai_text)
    await render(query, state, tr("type.ai_ask"), inline(nav(tr, Ac(action="create"), home=False)))


@router.callback_query(Ac.filter(F.action == "ai_allow"))
async def ai_allow(
    query: CallbackQuery,
    session: AsyncSession,
    user: User,
    tr: Translator,
    gateway: AIGateway,
    state: FSMContext,
) -> None:
    await UserService(session, user).set_ai_consent(True)
    await session.commit()
    await ai_type_start(query, user, tr, gateway, state)


@router.message(TypeSG.ai_text, F.text)
async def ai_type_text(
    message: Message,
    session: AsyncSession,
    user: User,
    tr: Translator,
    gateway: AIGateway,
    state: FSMContext,
) -> None:
    assert message.text is not None
    holder = await progress(message, state, tr("food.working"))
    svc = ActivityDraftService(session, user, gateway)
    draft = await svc.draft(message.text)
    await session.commit()
    await state.clear()
    _, proposal = await svc.get(draft.id)
    lines = [tr("type.ai_preview", name=proposal.name), "", tr("type.tracks")]
    lines.append(f"⏱ {tr('type.duration_label')}")
    for f in proposal.fields:
        extra = ": " + ", ".join(f.choices) if f.choices else ""
        lines.append(f"• {f.label}{extra}")
    if gateway.is_mock:
        lines += ["", tr("draft.mock")]
    await replace(
        holder,
        message,
        state,
        "\n".join(lines),
        inline(
            [
                (tr("type.create_btn"), Wd(a="type_ok", d=draft.id, v=draft.version)),
                (tr("nav.cancel"), Wd(a="type_no", d=draft.id, v=draft.version)),
            ]
        ),
    )


@router.callback_query(Wd.filter(F.a.in_({"type_ok", "type_no"})))
async def ai_type_resolve(
    query: CallbackQuery,
    callback_data: Wd,
    session: AsyncSession,
    user: User,
    tr: Translator,
    gateway: AIGateway,
    state: FSMContext,
) -> None:
    svc = ActivityDraftService(session, user, gateway)
    if callback_data.a == "type_no":
        await svc.cancel(callback_data.d, callback_data.v)
        await session.commit()
        await answer(query, tr("cancelled"))
        await show_training(query, session, user, tr, state)
        return
    version = await svc.confirm(callback_data.d, callback_data.v, tr("type.duration_label"))
    await session.commit()
    await answer(query, tr("saved"))
    await _type_ready(query, session, user, tr, state, version.activity_type_id, created=True)


@router.callback_query(Ac.filter(F.action == "types"))
async def types_list(
    query: CallbackQuery, session: AsyncSession, user: User, tr: Translator, state: FSMContext
) -> None:
    await answer(query)
    await state.clear()
    items = await ActivityService(session, user).list_types()
    buttons = [(f"{_icon(t.kind)} {t.name}", Ac(action="type", id=t.id)) for t in items[:16]]
    await render(
        query,
        state,
        tr("act.types_title") if items else tr("act.no_types"),
        inline(
            *grid(buttons), [(tr("act.new"), Ac(action="create"))], nav(tr, Ac(action="templates"))
        ),
    )


@router.callback_query(Ac.filter(F.action == "type"))
async def type_view(
    query: CallbackQuery,
    callback_data: Ac,
    session: AsyncSession,
    user: User,
    tr: Translator,
    state: FSMContext,
) -> None:
    await ActivityService(session, user).get_type(callback_data.id)  # ownership check
    await answer(query)
    await _type_ready(query, session, user, tr, state, callback_data.id)


# --- workouts (templates) ----------------------------------------------------------------------


@router.callback_query(Ac.filter(F.action == "templates"))
async def templates(
    query: CallbackQuery, session: AsyncSession, user: User, tr: Translator, state: FSMContext
) -> None:
    await answer(query)
    await state.clear()
    items = await ActivityService(session, user).list_templates()
    buttons = [(f"📋 {t.name}", Ac(action="tpl", id=t.id)) for t in items[:14]]
    await render(
        query,
        state,
        tr("tpl.list") if items else tr("tpl.none"),
        inline(
            *grid(buttons),
            [(tr("act.new"), Ac(action="create")), (tr("act.types"), Ac(action="types"))],
            [(tr("act.import"), Ac(action="import"))],
            nav(tr, Go(s="train")),
        ),
    )


@router.callback_query(Ac.filter(F.action == "tpl_type"))
async def template_for_type(
    query: CallbackQuery,
    callback_data: Ac,
    session: AsyncSession,
    user: User,
    tr: Translator,
    state: FSMContext,
) -> None:
    await ActivityService(session, user).get_type(callback_data.id)  # ownership check
    await answer(query)
    await _tpl_begin(query, session, user, tr, state, callback_data.id)


async def _tpl_begin(
    event: Event,
    session: AsyncSession,
    user: User,
    tr: Translator,
    state: FSMContext,
    type_id: int,
) -> None:
    activity = await ActivityService(session, user).get_type(type_id)
    await state.clear()
    await state.update_data(type_id=type_id, kind=activity.kind, type_name=activity.name, values={})
    if activity.kind == ActivityKind.SWIMMING.value:
        await state.set_state(TplSG.pool)
        await render(
            event,
            state,
            tr("tpl.pool_ask"),
            inline(
                [(f"{p} {tr('unit.m')}", Fd(action="pool", value=p)) for p in POOLS]
                + [(tr("starter.c.open_water"), Fd(action="pool", value="open"))],
                nav(tr, Ac(action="create"), home=False),
            ),
        )
        return
    await state.set_state(TplSG.name)
    if activity.kind == ActivityKind.STRENGTH.value:
        suggestions = [
            (tr(k), Fd(action="tname", value=str(i))) for i, k in enumerate(STRENGTH_NAMES)
        ]
    else:
        suggestions = [(activity.name, Fd(action="tname", value="type"))]
    await render(
        event,
        state,
        tr("tpl.ask_name", icon=_icon(activity.kind)),
        inline(suggestions, nav(tr, Ac(action="create"), home=False)),
    )


async def _tpl_named(event: Event, tr: Translator, state: FSMContext, name: str) -> None:
    name = " ".join(name.split())
    if not name or len(name) > 60:
        raise ServiceError("bad_name")
    await state.update_data(tpl_name=name)
    data = await state.get_data()
    if data.get("kind") == ActivityKind.STRENGTH.value:
        await _ask_blocks(event, tr, state)
    else:
        await _tpl_preview(event, tr, state)


@router.message(TplSG.name, F.text)
async def template_name(message: Message, tr: Translator, state: FSMContext) -> None:
    assert message.text is not None
    await _tpl_named(message, tr, state, message.text)


@router.callback_query(TplSG.name, Fd.filter(F.action == "tname"))
async def template_name_button(
    query: CallbackQuery, callback_data: Fd, tr: Translator, state: FSMContext
) -> None:
    data = await state.get_data()
    await answer(query)
    if callback_data.value == "type":
        name = str(data.get("type_name", ""))
    elif callback_data.value.isdigit() and int(callback_data.value) < len(STRENGTH_NAMES):
        name = tr(STRENGTH_NAMES[int(callback_data.value)])
    else:
        raise ServiceError("bad_choice")
    await _tpl_named(query, tr, state, name)


@router.callback_query(TplSG.pool, Fd.filter(F.action == "pool"))
async def template_pool(
    query: CallbackQuery,
    callback_data: Fd,
    session: AsyncSession,
    user: User,
    tr: Translator,
    state: FSMContext,
) -> None:
    if callback_data.value not in (*POOLS, "open"):
        raise ServiceError("bad_choice")
    data = await state.get_data()
    tv = await ActivityService(session, user).current_type_version(int(data["type_id"]))
    values: dict[str, str] = {}
    for f in _schema(tv.fields).fields:
        if f.label == tr("starter.f.pool_length") and callback_data.value in POOLS:
            values[f.key] = callback_data.value
        if f.label == tr("starter.f.water") and f.choices:
            choice = tr(
                "starter.c.pool" if callback_data.value in POOLS else "starter.c.open_water"
            )
            if choice in f.choices:
                values[f.key] = choice
    await state.update_data(pool=callback_data.value, values=values)
    await answer(query)
    await _ask_blocks(query, tr, state)


async def _ask_blocks(event: Event, tr: Translator, state: FSMContext) -> None:
    data = await state.get_data()
    await state.set_state(TplSG.blocks)
    swim = data.get("kind") == ActivityKind.SWIMMING.value
    await render(
        event,
        state,
        tr("tpl.swim_blocks_ask" if swim else "tpl.blocks_ask"),
        inline([(tr("tpl.blocks_later"), Fd(action="blocks_skip"))], nav(tr, cancel=True)),
    )


@router.message(TplSG.blocks, F.text)
async def template_blocks(message: Message, tr: Translator, state: FSMContext) -> None:
    assert message.text is not None
    body = _parse(parse_plan_text, message.text)
    await state.update_data(plan_blocks=body.dump())
    await _tpl_preview(message, tr, state)


@router.callback_query(TplSG.blocks, Fd.filter(F.action == "blocks_skip"))
async def template_blocks_skip(query: CallbackQuery, tr: Translator, state: FSMContext) -> None:
    await answer(query)
    await state.update_data(plan_blocks=[])
    await _tpl_preview(query, tr, state)


async def _tpl_preview(event: Event, tr: Translator, state: FSMContext) -> None:
    data = await state.get_data()
    body = WorkoutBody.load(data.get("plan_blocks") or [])
    kind = data.get("kind")
    total_m = totals(body).distance_m
    if kind == ActivityKind.SWIMMING.value and not data.get("tpl_name"):
        name = tr("tpl.swim_name", m=num(tr, total_m)) if total_m else data.get("type_name", "")
        await state.update_data(tpl_name=name)
        data["tpl_name"] = name
    head = f"{_icon(kind)} {data['tpl_name']}"
    pool = data.get("pool")
    if pool in POOLS:
        head += " · " + tr("tpl.pool", m=pool)
    elif pool == "open":
        head += " · " + tr("starter.c.open_water").lower()
    lines = [head]
    if body.blocks:
        lines += ["", *format_body(tr, body)]
    if total_m and kind == ActivityKind.SWIMMING.value:
        lines += ["", tr("tpl.total_m", m=num(tr, total_m))]
    targets = data.get("values") or {}
    if targets and data.get("targets_set"):
        lines += ["", tr("tpl.targets")]
        lines += [f"• {k}" for k in data.get("targets_shown", [])]
    await state.set_state(TplSG.review)
    edit = Fd(action="tpl_edit")
    await render(
        event,
        state,
        "\n".join(lines),
        inline(
            [
                (tr("btn.save"), Fd(action="save_tpl")),
                (tr("act.start_now"), Fd(action="save_tpl_go")),
            ],
            [(tr("btn.change"), edit), (tr("tpl.set_targets"), Fd(action="tpl_targets"))],
            nav(tr, cancel=True),
        ),
    )


@router.callback_query(TplSG.review, Fd.filter(F.action == "tpl_edit"))
async def template_edit(query: CallbackQuery, tr: Translator, state: FSMContext) -> None:
    data = await state.get_data()
    await answer(query)
    if data.get("kind") in (ActivityKind.STRENGTH.value, ActivityKind.SWIMMING.value):
        if data.get("kind") == ActivityKind.SWIMMING.value:
            await state.update_data(tpl_name=None)
        await _ask_blocks(query, tr, state)
    else:
        await state.set_state(TplSG.name)
        await render(
            query,
            state,
            tr("tpl.ask_name", icon=_icon(data.get("kind"))),
            inline(nav(tr, cancel=True)),
        )


@router.callback_query(TplSG.review, Fd.filter(F.action == "tpl_targets"))
async def template_targets(
    query: CallbackQuery, session: AsyncSession, user: User, tr: Translator, state: FSMContext
) -> None:
    data = await state.get_data()
    tv = await ActivityService(session, user).current_type_version(int(data["type_id"]))
    await state.update_data(
        mode="targets",
        fields=tv.fields,
        idx=0,
        title=f"🎯 {data['tpl_name']}",
    )
    await answer(query)
    await _prompt_value(query, tr, state)


async def _save_template(
    query: CallbackQuery, session: AsyncSession, user: User, tr: Translator, state: FSMContext
) -> int:
    data = await state.get_data()
    if "tpl_name" not in data or "type_id" not in data:
        raise ServiceError("already_resolved")
    body = WorkoutBody.load(data.get("plan_blocks") or [])
    version = await ActivityService(session, user).create_template(
        int(data["type_id"]),
        data["tpl_name"],
        dict(data.get("values") or {}),
        blocks=body if body.blocks else None,
    )
    await session.commit()
    await state.clear()
    return version.template_id


@router.callback_query(TplSG.review, Fd.filter(F.action == "save_tpl"))
async def save_template(
    query: CallbackQuery, session: AsyncSession, user: User, tr: Translator, state: FSMContext
) -> None:
    data = await state.get_data()
    template_id = await _save_template(query, session, user, tr, state)
    await answer(query, tr("saved"))
    await render(
        query,
        state,
        tr("tpl.saved", name=data["tpl_name"]),
        inline(
            [
                (tr("act.start_now"), Ac(action="rec_tpl", id=template_id)),
                (tr("tpl.plan"), Ac(action="plan_menu", id=template_id)),
            ],
            nav(tr),
        ),
    )


@router.callback_query(TplSG.review, Fd.filter(F.action == "save_tpl_go"))
async def save_template_and_start(
    query: CallbackQuery, session: AsyncSession, user: User, tr: Translator, state: FSMContext
) -> None:
    template_id = await _save_template(query, session, user, tr, state)
    await _start_recording(query, session, user, tr, state, {"template_id": template_id})


@router.callback_query(Ac.filter(F.action == "tpl"))
async def template_view(
    query: CallbackQuery,
    callback_data: Ac,
    session: AsyncSession,
    user: User,
    tr: Translator,
    state: FSMContext,
) -> None:
    svc = ActivityService(session, user)
    template = await svc.get_template(callback_data.id)
    ctx = await svc.recording_context(template_id=template.id)
    await state.clear()
    await answer(query)
    lines = [f"{_icon(ctx.kind)} {template.name}"]
    if template.program_id is not None:
        lines.append(tr("tpl.in_program", name=(await svc.get_program(template.program_id)).name))
    if ctx.target_blocks:
        body = WorkoutBody.load(list(ctx.target_blocks))
        lines += ["", *format_body(tr, body)]
        if totals(body).distance_m and ctx.kind == ActivityKind.SWIMMING.value:
            lines += ["", tr("tpl.total_m", m=num(tr, totals(body).distance_m))]
    goals = [
        f"{field_icon(f)} {f.label}: {field_value_text(tr, f, ctx.targets[f.key])}"
        for f in ctx.schema.fields
        if ctx.targets.get(f.key) is not None
    ]
    if goals:
        lines += ["", tr("tpl.targets"), *goals]
    await render(
        query,
        state,
        "\n".join(lines),
        inline(
            [
                (tr("act.start_now"), Ac(action="rec_tpl", id=template.id)),
                (tr("tpl.plan"), Ac(action="plan_menu", id=template.id)),
            ],
            [
                (tr("tpl.to_program"), Ac(action="to_program", id=template.id)),
                (tr("tpl.archive"), Ac(action="archive", id=template.id)),
            ],
            nav(tr, Ac(action="templates")),
        ),
    )


@router.callback_query(Ac.filter(F.action == "plan_menu"))
async def plan_menu(
    query: CallbackQuery,
    callback_data: Ac,
    session: AsyncSession,
    user: User,
    tr: Translator,
    state: FSMContext,
) -> None:
    template = await ActivityService(session, user).get_template(callback_data.id)
    await answer(query)
    tid = template.id
    await render(
        query,
        state,
        tr("plan.when", name=template.name),
        inline(
            [
                (tr("plan.today"), Ac(action="plan_today", id=tid)),
                (tr("plan.tomorrow"), Ac(action="plan_tomorrow", id=tid)),
            ],
            [(tr("plan.weekly"), Ac(action="plan_week", id=tid))],
            nav(tr, Ac(action="tpl", id=tid)),
        ),
    )


@router.callback_query(Ac.filter(F.action.in_({"plan_today", "plan_tomorrow"})))
async def plan_template(
    query: CallbackQuery,
    callback_data: Ac,
    session: AsyncSession,
    user: User,
    tr: Translator,
    state: FSMContext,
) -> None:
    day = local_today(user)
    if callback_data.action == "plan_tomorrow":
        day += dt.timedelta(days=1)
    await ActivityService(session, user).plan(callback_data.id, day)
    await session.commit()
    await answer(query, tr("saved"))
    await render(
        query,
        state,
        tr("plan.saved", date=rel_day(tr, day, local_today(user))),
        inline([(tr("home.btn_plan"), Go(s="plan")), (tr("nav.home"), Go(s="home"))]),
    )


@router.callback_query(Ac.filter(F.action == "plan_week"))
async def plan_week(
    query: CallbackQuery,
    callback_data: Ac,
    session: AsyncSession,
    user: User,
    tr: Translator,
    state: FSMContext,
) -> None:
    await ActivityService(session, user).get_template(callback_data.id)
    await answer(query)
    await state.clear()
    await state.update_data(week_tpl=callback_data.id, week_days=[])
    await _show_week(query, tr, state, [], callback_data.id)


async def _show_week(
    event: Event, tr: Translator, state: FSMContext, days: list[int], tpl: int
) -> None:
    buttons: Row = [
        (("✓ " if i in days else "") + tr(k), Fd(action="wday", value=str(i)))
        for i, k in enumerate(WEEKDAY_KEYS)
    ]
    rows: list[Row] = [buttons[:4], buttons[4:]]
    if days:
        rows.append([(tr("plan.weeks_btn"), Fd(action="wdone", value=str(tpl)))])
    rows.append(nav(tr, Ac(action="plan_menu", id=tpl), home=False))
    await render(event, state, tr("plan.week_ask"), inline(*rows))


@router.callback_query(Fd.filter(F.action == "wday"))
async def plan_week_toggle(
    query: CallbackQuery, callback_data: Fd, tr: Translator, state: FSMContext
) -> None:
    data = await state.get_data()
    if "week_tpl" not in data or not callback_data.value.isdigit():
        await answer(query, tr("stale_button"))
        return
    days = set(data.get("week_days", [])) ^ {int(callback_data.value) % 7}
    await state.update_data(week_days=sorted(days))
    await answer(query)
    await _show_week(query, tr, state, sorted(days), int(data["week_tpl"]))


@router.callback_query(Fd.filter(F.action == "wdone"))
async def plan_week_done(
    query: CallbackQuery, session: AsyncSession, user: User, tr: Translator, state: FSMContext
) -> None:
    data = await state.get_data()
    if "week_tpl" not in data:
        await answer(query, tr("stale_button"))
        return
    created = await ActivityService(session, user).plan_weekdays(
        int(data["week_tpl"]), set(data.get("week_days", [])), weeks=4
    )
    await session.commit()
    await state.clear()
    await answer(query, tr("saved"))
    await render(
        query,
        state,
        tr("plan.week_saved", n=len(created)),
        inline([(tr("home.btn_plan"), Go(s="plan")), (tr("nav.home"), Go(s="home"))]),
    )


@router.callback_query(Ac.filter(F.action == "archive"))
async def archive_template(
    query: CallbackQuery,
    callback_data: Ac,
    session: AsyncSession,
    user: User,
    tr: Translator,
    state: FSMContext,
) -> None:
    await ActivityService(session, user).archive_template(callback_data.id)
    await session.commit()
    await answer(query, tr("tpl.archived"), show_alert=True)
    await templates(query, session, user, tr, state)


# --- programs ----------------------------------------------------------------------------------


@router.callback_query(Ac.filter(F.action == "programs"))
async def programs(
    query: CallbackQuery, session: AsyncSession, user: User, tr: Translator, state: FSMContext
) -> None:
    await answer(query)
    await state.clear()
    items = await ActivityService(session, user).list_programs()
    buttons = [(f"📚 {p.name}", Ac(action="program", id=p.id)) for p in items[:12]]
    await render(
        query,
        state,
        tr("prog.list") if items else tr("prog.none"),
        inline(
            *[[b] for b in buttons],
            [(tr("prog.new"), Ac(action="new_program"))],
            nav(tr, Go(s="train")),
        ),
    )


@router.callback_query(Ac.filter(F.action == "new_program"))
async def new_program(query: CallbackQuery, tr: Translator, state: FSMContext) -> None:
    await answer(query)
    await state.set_state(ProgramSG.name)
    await render(
        query, state, tr("prog.ask_name"), inline(nav(tr, Ac(action="programs"), home=False))
    )


@router.message(ProgramSG.name, F.text)
async def program_name(
    message: Message, session: AsyncSession, user: User, tr: Translator, state: FSMContext
) -> None:
    assert message.text is not None
    program = await ActivityService(session, user).create_program(message.text)
    await session.commit()
    await state.clear()
    await render(
        message,
        state,
        tr("prog.saved", name=program.name),
        inline(
            [(tr("prog.open"), Ac(action="program", id=program.id))], nav(tr, Ac(action="programs"))
        ),
    )


@router.callback_query(Ac.filter(F.action == "program"))
async def program_view(
    query: CallbackQuery,
    callback_data: Ac,
    session: AsyncSession,
    user: User,
    tr: Translator,
    state: FSMContext,
) -> None:
    svc = ActivityService(session, user)
    program = await svc.get_program(callback_data.id)
    items = await svc.program_templates(program.id)
    await answer(query)
    lines = [f"📚 {program.name}", ""]
    lines += [f"• {t.name}" for t in items] or [tr("prog.empty")]
    buttons = [(f"📋 {t.name}", Ac(action="tpl", id=t.id)) for t in items[:12]]
    await render(
        query,
        state,
        "\n".join(lines),
        inline(
            *[[b] for b in buttons],
            [(tr("prog.archive"), Ac(action="program_archive", id=program.id))],
            nav(tr, Ac(action="programs")),
        ),
    )


@router.callback_query(Ac.filter(F.action == "program_archive"))
async def program_archive(
    query: CallbackQuery,
    callback_data: Ac,
    session: AsyncSession,
    user: User,
    tr: Translator,
    state: FSMContext,
) -> None:
    await ActivityService(session, user).archive_program(callback_data.id)
    await session.commit()
    await answer(query, tr("prog.archived"), show_alert=True)
    await programs(query, session, user, tr, state)


@router.callback_query(Ac.filter(F.action == "to_program"))
async def to_program(
    query: CallbackQuery,
    callback_data: Ac,
    session: AsyncSession,
    user: User,
    tr: Translator,
    state: FSMContext,
) -> None:
    svc = ActivityService(session, user)
    await svc.get_template(callback_data.id)
    items = await svc.list_programs()
    await answer(query)
    back = nav(tr, Ac(action="tpl", id=callback_data.id), home=False)
    if not items:
        await render(
            query,
            state,
            tr("prog.none"),
            inline([(tr("prog.new"), Ac(action="new_program"))], back),
        )
        return
    await state.update_data(assign_tpl=callback_data.id)
    buttons = [(f"📚 {p.name}", Ac(action="assign", id=p.id)) for p in items[:12]]
    await render(query, state, tr("prog.choose"), inline(*[[b] for b in buttons], back))


@router.callback_query(Ac.filter(F.action == "assign"))
async def assign(
    query: CallbackQuery,
    callback_data: Ac,
    session: AsyncSession,
    user: User,
    tr: Translator,
    state: FSMContext,
) -> None:
    data = await state.get_data()
    if "assign_tpl" not in data:
        await answer(query, tr("stale_button"))
        return
    await ActivityService(session, user).assign_template(int(data["assign_tpl"]), callback_data.id)
    await session.commit()
    await state.clear()
    await answer(query, tr("saved"))
    await program_view(query, Ac(action="program", id=callback_data.id), session, user, tr, state)


# --- record: choose what -----------------------------------------------------------------------


@router.callback_query(Ac.filter(F.action.in_({"rec_menu", "start"})))
async def record_menu(
    query: CallbackQuery,
    session: AsyncSession,
    user: User,
    tr: Translator,
    gateway: AIGateway,
    state: FSMContext,
) -> None:
    await answer(query)
    await state.clear()
    svc = ActivityService(session, user)
    today = local_today(user)
    rows: list[Row] = [[(tr("act.by_text"), Ac(action="by_text"))]]
    rows += [
        [(f"○ {v.name}", Ac(action="rec_plan", id=p.id))]
        for p, v in await svc.list_planned(today, today)
    ]
    buttons = [
        (f"📋 {t.name}", Ac(action="rec_tpl", id=t.id)) for t in (await svc.list_templates())[:8]
    ]
    buttons += [
        (f"{_icon(t.kind)} {t.name}", Ac(action="rec_type", id=t.id))
        for t in (await svc.list_types())[:8]
    ]
    rows += grid(buttons)
    rows.append(nav(tr, Go(s="train")))
    await render(query, state, tr("act.choose_what"), inline(*rows))


@router.callback_query(Ac.filter(F.action.in_({"rec_type", "rec_tpl", "rec_plan"})))
async def record_start(
    query: CallbackQuery,
    callback_data: Ac,
    session: AsyncSession,
    user: User,
    tr: Translator,
    state: FSMContext,
) -> None:
    key = {"rec_type": "type_id", "rec_tpl": "template_id", "rec_plan": "planned_id"}[
        callback_data.action
    ]
    await _start_recording(query, session, user, tr, state, {key: callback_data.id})


async def _start_recording(
    query: CallbackQuery,
    session: AsyncSession,
    user: User,
    tr: Translator,
    state: FSMContext,
    source: dict[str, int],
) -> None:
    ctx = await ActivityService(session, user).recording_context(**source)
    await answer(query)
    await state.clear()
    items = [
        {"b": bi, "item": it} for bi, b in enumerate(ctx.target_blocks) for it in b.get("items", [])
    ]
    await state.update_data(
        mode="session",
        source=source,
        kind=ctx.kind,
        fields=[f.model_dump(mode="json", exclude_none=True) for f in ctx.schema.fields],
        targets=ctx.targets,
        idx=0,
        values={},
        title=f"{_icon(ctx.kind)} {ctx.template_name or ctx.activity_name}",
        plan_blocks=list(ctx.target_blocks),
        items=items,
        iidx=0,
        actual={},
        started=utcnow().isoformat(),
    )
    if items:
        await _prompt_item(query, session, user, tr, state)
    else:
        await _prompt_value(query, tr, state)


# --- record: one exercise per screen -----------------------------------------------------------


async def _prompt_item(
    event: Event, session: AsyncSession, user: User, tr: Translator, state: FSMContext
) -> None:
    data = await state.get_data()
    items = data["items"]
    i = int(data["iidx"])
    if i >= len(items):
        await _prompt_value(event, tr, state)
        return
    await state.set_state(ValuesSG.item)
    target = Item.model_validate(items[i]["item"])
    last = await ActivityService(session, user).last_sets(target.name)
    lines = [data["title"], tr("rec.item_n", n=i + 1, total=len(items)), "", target.name]
    if target.sets:
        lines.append(tr("rec.plan", sets=sets_text(tr, target.sets)))
    if last:
        lines.append(tr("rec.last", sets=sets_text(tr, last)))
        await state.update_data(last=[s.model_dump(mode="json", exclude_none=True) for s in last])
    else:
        await state.update_data(last=None)
    rows: list[Row] = []
    if target.sets:
        rows.append([("✓ " + sets_text(tr, target.sets), Fd(action="item_same"))])
    if last and tuple(last) != target.sets:
        rows.append([(tr("rec.as_last"), Fd(action="item_last"))])
    rows.append([(tr("rec.other_result"), Fd(action="item_other"))])
    rows.append(
        [(tr("btn.skip"), Fd(action="item_skip")), (tr("rec.finish"), Fd(action="item_finish"))]
    )
    await render(event, state, "\n".join(lines), inline(*rows))


async def _store_item(
    event: Event,
    session: AsyncSession,
    user: User,
    tr: Translator,
    state: FSMContext,
    sets: list[dict[str, Any]] | None,
) -> None:
    data = await state.get_data()
    i = int(data["iidx"])
    actual = dict(data["actual"])
    if not sets:
        await state.update_data(iidx=i + 1)
        await _prompt_item(event, session, user, tr, state)
        return
    actual[str(i)] = sets
    await state.update_data(actual=actual, iidx=i + 1)
    await state.set_state(ValuesSG.item_done)
    item = Item.model_validate(data["items"][i]["item"])
    done = tuple(SetSpec.model_validate(s) for s in sets)
    lines = [f"✓ {item.name}", sets_text(tr, done)]
    if data.get("last"):
        prev = tuple(SetSpec.model_validate(s) for s in data["last"])
        progress_note = compare_sets(prev, done)
        if progress_note is not None:
            if progress_note.kind == "load":
                lines.append(tr("rec.progress_load", kg=num(tr, progress_note.delta, 2)))
            elif progress_note.kind == "reps":
                lines.append(tr("rec.progress_reps", n=int(progress_note.delta)))
            else:
                lines.append(tr("rec.progress_same"))
    last_item = i + 1 >= len(data["items"])
    await render(
        event,
        state,
        "\n".join(lines),
        inline([(tr("rec.next_finish" if last_item else "rec.next"), Fd(action="item_next"))]),
    )


@router.message(ValuesSG.item, F.text)
async def item_text(
    message: Message, session: AsyncSession, user: User, tr: Translator, state: FSMContext
) -> None:
    assert message.text is not None
    data = await state.get_data()
    target = Item.model_validate(data["items"][int(data["iidx"])]["item"])
    loads = {s.load_kg for s in target.sets if not s.warmup and s.load_kg is not None}
    # "60x10 60x9" is weight × reps; plain "10 10 8" are reps at the planned weight.
    has_pairs = any(ch in message.text.lower() for ch in "x×х*")
    default_load = loads.pop() if len(loads) == 1 and not has_pairs else None
    sets = _parse(parse_sets, message.text, default_load=default_load)
    warm = all(s.warmup for s in target.sets) and bool(target.sets)
    dumped = [
        s.model_copy(update={"warmup": warm}).model_dump(
            mode="json", exclude_none=True, exclude_defaults=True
        )
        for s in sets
    ]
    await _store_item(message, session, user, tr, state, dumped)


@router.callback_query(
    ValuesSG.item, Fd.filter(F.action.in_({"item_same", "item_last", "item_skip"}))
)
async def item_button(
    query: CallbackQuery,
    callback_data: Fd,
    session: AsyncSession,
    user: User,
    tr: Translator,
    state: FSMContext,
) -> None:
    await answer(query)
    data = await state.get_data()
    sets = None
    if callback_data.action == "item_same":
        # Explicit user action: "done exactly as planned".
        sets = list(data["items"][int(data["iidx"])]["item"].get("sets", []))
    elif callback_data.action == "item_last":
        sets = list(data.get("last") or [])
    await _store_item(query, session, user, tr, state, sets)


@router.callback_query(ValuesSG.item, Fd.filter(F.action == "item_other"))
async def item_other(query: CallbackQuery, tr: Translator, state: FSMContext) -> None:
    await answer(query)
    await render(
        query, state, tr("rec.other_ask"), inline(nav(tr, Fd(action="item_back"), home=False))
    )


@router.callback_query(ValuesSG.item, Fd.filter(F.action == "item_back"))
@router.callback_query(ValuesSG.item_done, Fd.filter(F.action == "item_next"))
async def item_next(
    query: CallbackQuery, session: AsyncSession, user: User, tr: Translator, state: FSMContext
) -> None:
    await answer(query)
    await _prompt_item(query, session, user, tr, state)


@router.callback_query(ValuesSG.item, Fd.filter(F.action == "item_finish"))
async def item_finish(query: CallbackQuery, tr: Translator, state: FSMContext) -> None:
    await answer(query)
    data = await state.get_data()
    await state.update_data(iidx=len(data["items"]))
    await _prompt_value(query, tr, state)


# --- record: fields (duration, effort, ...) ----------------------------------------------------


def _timer_minutes(data: dict[str, Any]) -> int | None:
    started = data.get("started")
    if not isinstance(started, str):
        return None
    minutes = int((utcnow() - dt.datetime.fromisoformat(started)).total_seconds() // 60)
    return minutes if 5 <= minutes <= 600 else None


async def _prompt_value(event: Event, tr: Translator, state: FSMContext) -> None:
    data = await state.get_data()
    fields = _schema(data["fields"]).fields
    idx = int(data["idx"])
    if idx >= len(fields):
        await _values_done(event, tr, state)
        return
    await state.set_state(ValuesSG.value)
    field = fields[idx]
    target = (data.get("targets") or {}).get(field.key) if data["mode"] == "session" else None
    timer = (
        _timer_minutes(data) if field.key == DURATION_KEY and data["mode"] == "session" else None
    )
    await render(
        event,
        state,
        field_prompt(tr, data["title"], field, target, idx + 1, len(fields)),
        field_input_kb(tr, field, timer_min=timer, more=idx + 1 < len(fields)),
    )


async def _store_value(event: Event, tr: Translator, state: FSMContext, raw: str | None) -> None:
    data = await state.get_data()
    field = _schema(data["fields"]).fields[int(data["idx"])]
    values = dict(data["values"])
    if raw is not None:
        _parse(parse_field_value, field, raw)  # immediate feedback; the service re-validates
        values[field.key] = raw
    await state.update_data(values=values, idx=int(data["idx"]) + 1)
    await _prompt_value(event, tr, state)


@router.message(ValuesSG.value, F.text)
async def value_text(message: Message, tr: Translator, state: FSMContext) -> None:
    await _store_value(message, tr, state, message.text)


@router.callback_query(ValuesSG.value, Fd.filter(F.action.in_({"skip", "choice", "bool", "val"})))
async def value_button(
    query: CallbackQuery, callback_data: Fd, tr: Translator, state: FSMContext
) -> None:
    await answer(query)
    raw: str | None = None
    if callback_data.action == "choice":
        data = await state.get_data()
        field = _schema(data["fields"]).fields[int(data["idx"])]
        if field.choices is None or not callback_data.value.isdigit():
            raise ServiceError("not_a_choice")
        index = int(callback_data.value)
        if index >= len(field.choices):
            raise ServiceError("not_a_choice")
        raw = field.choices[index]
    elif callback_data.action == "bool":
        raw = "yes" if callback_data.value == "1" else "no"
    elif callback_data.action == "val":
        raw = callback_data.value[:20]  # re-validated by parse_field_value and the service
    await _store_value(query, tr, state, raw)


@router.callback_query(ValuesSG.value, Fd.filter(F.action == "skip_rest"))
async def value_skip_rest(query: CallbackQuery, tr: Translator, state: FSMContext) -> None:
    """Skip all remaining fields: unknown stays unknown."""
    await answer(query)
    data = await state.get_data()
    await state.update_data(idx=len(data["fields"]))
    await _values_done(query, tr, state)


def _actual_body(data: dict[str, Any]) -> WorkoutBody | None:
    """Actual blocks = only items the user entered, grouped like the plan."""
    if data.get("extra_blocks"):
        return WorkoutBody.load(data["extra_blocks"])
    actual = data.get("actual") or {}
    if not actual:
        return None
    blocks: list[Block] = []
    for bi, block in enumerate(data.get("plan_blocks", [])):
        items = []
        for n, entry in enumerate(data["items"]):
            if entry["b"] == bi and str(n) in actual:
                items.append(Item(name=entry["item"]["name"], sets=tuple(actual[str(n)])))
        if items:
            blocks.append(Block.model_validate({**block, "items": items}))
    return WorkoutBody(blocks=tuple(blocks))


def _value_lines(tr: Translator, data: dict[str, Any]) -> list[str]:
    lines = []
    for f in _schema(data["fields"]).fields:
        raw = data["values"].get(f.key)
        if raw is not None:
            value = parse_field_value(f, raw)
            lines.append(f"{field_icon(f)} {f.label}: {field_value_text(tr, f, value)}")
    return lines


async def _values_done(event: Event, tr: Translator, state: FSMContext) -> None:
    data = await state.get_data()
    lines = _value_lines(tr, data)
    if data["mode"] == "targets":
        await state.update_data(targets_set=True, targets_shown=[line for line in lines])
        await _tpl_preview(event, tr, state)
        return
    body = _actual_body(data)
    if (
        body is None
        and not data.get("plan_blocks")
        and not data.get("asked_extra")
        and data.get("kind") in ("strength", "swimming")
    ):
        await state.update_data(asked_extra=True)
        await state.set_state(ValuesSG.extra_blocks)
        await render(
            event,
            state,
            tr("rec.extra_ask"),
            inline([(tr("btn.skip"), Fd(action="extra_skip"))], nav(tr, cancel=True)),
        )
        return
    text_lines = [tr("rec.review", title=data["title"])]
    if body:
        text_lines += ["", *format_body(tr, body)]
    if lines:
        text_lines += ["", *lines]
    if not lines and not body:
        await state.clear()
        await render(event, state, tr("rec.empty"), inline(nav(tr, Go(s="train"))))
        return
    await state.set_state(ValuesSG.review)
    await render(
        event,
        state,
        "\n".join(text_lines),
        inline(
            [
                (tr("btn.save"), Fd(action="save_session")),
                (tr("btn.change"), Fd(action="rev_edit")),
            ],
            nav(tr, cancel=True),
        ),
    )


@router.message(ValuesSG.extra_blocks, F.text)
async def extra_blocks(message: Message, tr: Translator, state: FSMContext) -> None:
    assert message.text is not None
    body = _parse(parse_plan_text, message.text)
    await state.update_data(extra_blocks=body.dump())
    await _values_done(message, tr, state)


@router.callback_query(ValuesSG.extra_blocks, Fd.filter(F.action == "extra_skip"))
async def extra_skip(query: CallbackQuery, tr: Translator, state: FSMContext) -> None:
    await answer(query)
    await _values_done(query, tr, state)


@router.callback_query(ValuesSG.review, Fd.filter(F.action == "rev_edit"))
async def review_edit(query: CallbackQuery, tr: Translator, state: FSMContext) -> None:
    await answer(query)
    await state.update_data(idx=0, values={})
    await _prompt_value(query, tr, state)


@router.callback_query(ValuesSG.review, Fd.filter(F.action == "save_session"))
async def save_session(
    query: CallbackQuery, session: AsyncSession, user: User, tr: Translator, state: FSMContext
) -> None:
    data = await state.get_data()
    svc = ActivityService(session, user)
    ctx = await svc.recording_context(**{k: int(v) for k, v in data["source"].items()})
    saved = await svc.record_session(ctx, dict(data["values"]), blocks=_actual_body(data))
    await session.commit()
    await state.clear()
    await answer(query, tr("saved"))
    duration = session_duration(saved)
    extra = f" · {minutes_text(tr, duration)}" if duration else ""
    await render(
        query,
        state,
        tr("rec.saved", title=data["title"] + extra),
        inline([(tr("home.btn_day"), Go(s="day", a="0")), (tr("nav.home"), Go(s="home"))]),
    )


# --- free-text workout logging -----------------------------------------------------------------


@router.callback_query(Ac.filter(F.action == "by_text"))
async def by_text(
    query: CallbackQuery, user: User, tr: Translator, gateway: AIGateway, state: FSMContext
) -> None:
    await answer(query)
    await state.set_state(TextSG.workout)
    key = "wo.text_ask_ai" if gateway.text_available(user) else "wo.text_ask"
    await render(query, state, tr(key), inline(nav(tr, Ac(action="rec_menu"), home=False)))


async def show_workout_draft(
    event: Event,
    session: AsyncSession,
    user: User,
    tr: Translator,
    state: FSMContext,
    draft: Draft,
    holder: Message | None = None,
) -> None:
    svc = WorkoutDraftService(session, user)
    row, wstate = await svc.get(draft.id)
    types = await ActivityService(session, user).list_types()
    chosen = next((t for t in types if t.id == wstate.type_id), None)
    kind = chosen.kind if chosen else wstate.kind.value
    lines = [f"{_icon(kind)} {tr('wo.draft_title')}", ""]
    lines.append(chosen.name if chosen else tr("wo.type_missing"))
    lines.extend(format_body(tr, WorkoutBody(blocks=tuple(wstate.blocks))))
    if wstate.duration_s is not None:
        lines.append(f"⏱ {minutes_text(tr, wstate.duration_s)}")
    if wstate.distance_km is not None:
        lines.append(f"📏 {num(tr, wstate.distance_km, 2)} {tr('unit.km')}")
    if wstate.notes:
        lines.append(f"📝 {wstate.notes}")
    if wstate.clarification:
        lines += ["", "❓ " + wstate.clarification]
    if wstate.mock:
        lines += ["", tr("draft.mock")]
    rows: list[Row] = []
    if chosen is None:
        starter = wstate.kind.value if wstate.kind is not ActivityKind.CUSTOM else "strength"
        rows.append(
            [
                (
                    tr("wo.create_starter", name=tr("starter." + starter)),
                    Wd(a="starter", d=row.id, v=row.version),
                )
            ]
        )
    else:
        rows.append([(tr("btn.save"), Wd(a="ok", d=row.id, v=row.version))])
    others = [
        (f"↻ {t.name}", Wd(a="type", d=row.id, v=row.version, t=t.id))
        for t in types
        if t.id != wstate.type_id
    ]
    if others:
        lines += ["", tr("wo.other_type")]
        rows += grid(others[:6])
    rows.append([(tr("nav.cancel"), Wd(a="no", d=row.id, v=row.version))])
    await replace(holder, event, state, "\n".join(lines), inline(*rows))


@router.message(TextSG.workout, F.text)
async def workout_text(
    message: Message,
    session: AsyncSession,
    user: User,
    tr: Translator,
    gateway: AIGateway,
    state: FSMContext,
) -> None:
    assert message.text is not None
    await workout_from_text(message, message.text, session, user, tr, gateway, state)


async def workout_from_text(
    event: Event,
    text: str,
    session: AsyncSession,
    user: User,
    tr: Translator,
    gateway: AIGateway,
    state: FSMContext,
) -> None:
    holder = (
        await progress(event, state, tr("food.working")) if gateway.text_available(user) else None
    )
    draft = await WorkoutDraftService(session, user, gateway).draft_from_text(text)
    await session.commit()
    await state.clear()
    await show_workout_draft(event, session, user, tr, state, draft, holder)


@router.callback_query(Wd.filter(F.a.in_({"type", "starter", "ok", "no"})))
async def workout_draft_action(
    query: CallbackQuery,
    callback_data: Wd,
    session: AsyncSession,
    user: User,
    tr: Translator,
    state: FSMContext,
) -> None:
    svc = WorkoutDraftService(session, user)
    cb = callback_data
    if cb.a == "ok":
        saved = await svc.confirm(cb.d, cb.v)
        await session.commit()
        await answer(query, tr("saved"))
        await render(
            query,
            state,
            tr("rec.saved", title=f"{_icon(None)} {session_title(saved)}"),
            inline([(tr("home.btn_day"), Go(s="day", a="0")), (tr("nav.home"), Go(s="home"))]),
        )
        return
    if cb.a == "no":
        await svc.cancel(cb.d, cb.v)
        await session.commit()
        await answer(query, tr("cancelled"))
        await show_training(query, session, user, tr, state)
        return
    if cb.a == "type":
        draft = await svc.set_type(cb.d, cb.v, cb.t)
    else:
        draft = await svc.create_starter_type(cb.d, cb.v, tr)
    await session.commit()
    await answer(query)
    await show_workout_draft(query, session, user, tr, state, draft)


# --- Strong CSV import -------------------------------------------------------------------------


@router.callback_query(Ac.filter(F.action == "import"))
async def import_start(query: CallbackQuery, tr: Translator, state: FSMContext) -> None:
    await answer(query)
    await state.set_state(ImportSG.file)
    await render(query, state, tr("imp.ask"), inline(nav(tr, Ac(action="templates"), home=False)))


@router.message(ImportSG.file, F.document)
async def import_file(
    message: Message,
    bot: Bot,
    session: AsyncSession,
    user: User,
    tr: Translator,
    settings: Settings,
    state: FSMContext,
) -> None:
    doc = message.document
    assert doc is not None
    if (doc.file_size or 0) > settings.max_import_bytes:
        raise ServiceError("media_too_large")
    if not (doc.file_name or "").lower().endswith(".csv"):
        raise ServiceError("bad_csv")
    buffer = io.BytesIO()
    await bot.download(doc.file_id, destination=buffer, timeout=60)
    data = buffer.getvalue()
    if len(data) > settings.max_import_bytes:
        raise ServiceError("media_too_large")
    batch = await StrongImportService(session, user).preview(data)
    await session.commit()
    await state.clear()
    s = batch.summary
    lines = [
        tr(
            "imp.preview",
            workouts=len(s["workouts"]),
            sets=s["sets"],
            exercises=len(s["exercises"]),
        )
    ]
    if s["duplicates"]:
        lines.append(tr("imp.duplicates", n=s["duplicates"]))
    if s["error_count"]:
        rows = ", ".join(str(r) for r, _ in s["errors"][:10])
        lines.append(tr("imp.errors", n=s["error_count"], rows=rows))
    if s["unknown_columns"]:
        lines.append(tr("imp.unknown_columns", cols=", ".join(s["unknown_columns"][:8])))
    unit_key = {"kg": "imp.unit_kg", "lb": "imp.unit_lb"}.get(s["weight_unit"], "imp.unit_unknown")
    lines.append(tr(unit_key))
    lines.append(tr("imp.exercises", names=", ".join(s["exercises"][:15])))
    lines.append(tr("imp.no_sync"))
    await render(
        message,
        state,
        "\n".join(lines),
        inline([(tr("imp.confirm"), Ac(action="import_ok", id=batch.id))], nav(tr, cancel=True)),
    )


@router.callback_query(Ac.filter(F.action == "import_ok"))
async def import_confirm(
    query: CallbackQuery,
    callback_data: Ac,
    session: AsyncSession,
    user: User,
    tr: Translator,
    state: FSMContext,
) -> None:
    count = await StrongImportService(session, user).confirm(callback_data.id, tr)
    await session.commit()
    await answer(query, tr("saved"))
    await render(
        query,
        state,
        tr("imp.done", n=count),
        inline([(tr("act.history"), Ac(action="history")), (tr("nav.home"), Go(s="home"))]),
    )
