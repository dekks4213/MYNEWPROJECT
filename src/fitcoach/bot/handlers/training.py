"""Training UI: start/record workouts, programs, templates with blocks, activity types
(starters, manual builder, AI draft) and Strong CSV import. Works fully without AI."""

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
from fitcoach.bot.handlers.common import msg
from fitcoach.bot.ui import (
    Ac,
    Fd,
    Wd,
    cancel_kb,
    column,
    field_input_kb,
    field_prompt,
    format_body,
    inline,
    main_menu,
    num,
    words,
)
from fitcoach.config import Settings
from fitcoach.db.models import Draft, User
from fitcoach.domain.fields import (
    MAX_FIELDS,
    FieldDefinition,
    FieldSchema,
    FieldType,
    default_duration_field,
    format_field_value,
    next_custom_key,
    parse_field_value,
)
from fitcoach.domain.starters import STARTER_KINDS, starter_fields
from fitcoach.domain.units import ParseError
from fitcoach.domain.workout import (
    ActivityKind,
    Block,
    Item,
    WorkoutBody,
    format_item,
    parse_plan_text,
    parse_sets,
)
from fitcoach.i18n import Translator, all_labels
from fitcoach.services.activities import ActivityService
from fitcoach.services.errors import ServiceError
from fitcoach.services.strong_import import StrongImportService
from fitcoach.services.users import local_today
from fitcoach.services.workout_drafts import ActivityDraftService, WorkoutDraftService

router = Router(name="training")
WEEKDAY_KEYS = ("wd.mon", "wd.tue", "wd.wed", "wd.thu", "wd.fri", "wd.sat", "wd.sun")


class TypeSG(StatesGroup):
    name = State()
    builder = State()
    field_label = State()
    field_type = State()
    field_unit = State()
    field_choices = State()
    field_format = State()
    ai_text = State()


class TplSG(StatesGroup):
    name = State()
    blocks = State()


class ValuesSG(StatesGroup):
    item = State()
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


# --- menu --------------------------------------------------------------------------------


def training_kb(tr: Translator) -> Any:
    return inline(
        [(tr("act.start"), Ac(action="start")), (tr("act.record"), Ac(action="rec_menu"))],
        [
            (tr("act.programs"), Ac(action="programs")),
            (tr("act.templates"), Ac(action="templates")),
        ],
        [(tr("act.create"), Ac(action="create")), (tr("act.types"), Ac(action="types"))],
        [(tr("act.planned"), Ac(action="planned")), (tr("act.import"), Ac(action="import"))],
    )


@router.message(F.text.in_(all_labels("menu.training")))
async def training_menu(message: Message, tr: Translator, state: FSMContext) -> None:
    await state.clear()
    await message.answer(tr("act.menu"), reply_markup=training_kb(tr))


@router.callback_query(Ac.filter(F.action == "menu"))
async def training_menu_cb(query: CallbackQuery, tr: Translator, state: FSMContext) -> None:
    await query.answer()
    await state.clear()
    await msg(query).answer(tr("act.menu"), reply_markup=training_kb(tr))


# --- create: starters, custom builder, AI draft --------------------------------------------

STARTER_ICONS = {
    ActivityKind.STRENGTH: "🏋️",
    ActivityKind.SWIMMING: "🏊",
    ActivityKind.ENDURO: "🏍",
    ActivityKind.MOTO_RIDE: "🛵",
}


@router.callback_query(Ac.filter(F.action == "create"))
async def create_menu(query: CallbackQuery, user: User, tr: Translator, gateway: AIGateway) -> None:
    await query.answer()
    buttons = [
        (
            f"{STARTER_ICONS[k]} {tr('starter.' + k.value)}",
            Ac(action="starter", id=STARTER_KINDS.index(k)),
        )
        for k in STARTER_KINDS
    ]
    buttons.append((tr("act.custom"), Ac(action="new_type")))
    if gateway.text_available(user):
        buttons.append((tr("act.describe_ai"), Ac(action="ai_type")))
    await msg(query).answer(tr("act.create_ask"), reply_markup=column(buttons, width=2))


@router.callback_query(Ac.filter(F.action == "starter"))
async def starter_preview(query: CallbackQuery, callback_data: Ac, tr: Translator) -> None:
    if not 0 <= callback_data.id < len(STARTER_KINDS):
        raise ServiceError("bad_choice")
    kind = STARTER_KINDS[callback_data.id]
    name, fields = starter_fields(kind, tr)
    await query.answer()
    lines = [tr("type.starter_preview", name=name)]
    lines += [
        f"• {f.label} — {tr('ftype.' + f.type.value)}" + (f", {f.unit}" if f.unit else "")
        for f in fields
    ]
    if kind in (ActivityKind.STRENGTH, ActivityKind.SWIMMING):
        lines.append(tr("type.starter_blocks_note"))
    if kind is ActivityKind.MOTO_RIDE:
        lines.append(tr("type.moto_note"))
    await msg(query).answer(
        "\n".join(lines),
        reply_markup=inline(
            [(tr("type.create_btn"), Ac(action="starter_ok", id=callback_data.id))],
            [(tr("btn.cancel"), Fd(action="cancel"))],
        ),
    )


@router.callback_query(Ac.filter(F.action == "starter_ok"))
async def starter_create(
    query: CallbackQuery, callback_data: Ac, session: AsyncSession, user: User, tr: Translator
) -> None:
    if not 0 <= callback_data.id < len(STARTER_KINDS):
        raise ServiceError("bad_choice")
    kind = STARTER_KINDS[callback_data.id]
    name, fields = starter_fields(kind, tr)
    version = await ActivityService(session, user).create_type(name, fields, kind)
    await session.commit()
    await query.answer(tr("saved"))
    await _after_type_created(msg(query), tr, version.name, version.activity_type_id)


async def _after_type_created(message: Message, tr: Translator, name: str, type_id: int) -> None:
    await message.answer(
        tr("type.saved", name=name),
        reply_markup=column(
            [
                (tr("act.record_now"), Ac(action="rec_type", id=type_id)),
                (tr("tpl.new"), Ac(action="tpl_type", id=type_id)),
            ]
        ),
    )


@router.callback_query(Ac.filter(F.action == "new_type"))
async def new_type(query: CallbackQuery, tr: Translator, state: FSMContext) -> None:
    await query.answer()
    await state.clear()
    await state.set_state(TypeSG.name)
    await msg(query).answer(tr("type.ask_name"), reply_markup=cancel_kb(tr))


@router.message(TypeSG.name, F.text)
async def type_name(message: Message, tr: Translator, state: FSMContext) -> None:
    assert message.text is not None
    name = " ".join(message.text.split())
    if not name or len(name) > 60:
        raise ServiceError("bad_name")
    duration = default_duration_field(tr("type.duration_label"))
    await state.update_data(name=name, fields=[duration.model_dump(mode="json", exclude_none=True)])
    await _show_builder(message, tr, state)


async def _show_builder(message: Message, tr: Translator, state: FSMContext) -> None:
    await state.set_state(TypeSG.builder)
    data = await state.get_data()
    schema = _schema(data["fields"])
    lines = [tr("type.builder", name=data["name"])]
    rows: list[list[tuple[str, Any]]] = []
    for f in schema.fields:
        unit = f", {f.unit}" if f.unit else ""
        lines.append(f"• {f.label} — {tr('ftype.' + f.type.value)}{unit}")
    if len(schema.fields) < MAX_FIELDS:
        rows.append([(tr("type.add_field"), Fd(action="add_field"))])
    rows.append([(tr("type.save"), Fd(action="save_type"))])
    rows.append([(tr("btn.cancel"), Fd(action="cancel"))])
    await message.answer("\n".join(lines), reply_markup=inline(*rows))


@router.callback_query(TypeSG.builder, Fd.filter(F.action == "add_field"))
async def add_field(query: CallbackQuery, tr: Translator, state: FSMContext) -> None:
    await query.answer()
    await state.set_state(TypeSG.field_label)
    await msg(query).answer(tr("type.ask_field_label"), reply_markup=cancel_kb(tr))


@router.message(TypeSG.field_label, F.text)
async def field_label(message: Message, tr: Translator, state: FSMContext) -> None:
    assert message.text is not None
    label = " ".join(message.text.split())
    if not label or len(label) > 40:
        raise ServiceError("bad_name")
    await state.update_data(pending={"label": label})
    await state.set_state(TypeSG.field_type)
    buttons = [(tr("ftype." + t.value), Fd(action="ftype", value=t.value)) for t in FieldType]
    await message.answer(tr("type.ask_field_type"), reply_markup=column(buttons, width=2))


@router.callback_query(TypeSG.field_type, Fd.filter(F.action == "ftype"))
async def field_type(
    query: CallbackQuery, callback_data: Fd, tr: Translator, state: FSMContext
) -> None:
    await query.answer()
    ftype = FieldType(callback_data.value)
    data = await state.get_data()
    await state.update_data(pending={**data["pending"], "type": ftype.value})
    message = msg(query)
    if ftype in (FieldType.DECIMAL, FieldType.INTEGER):
        await state.set_state(TypeSG.field_unit)
        await message.answer(
            tr("type.ask_unit"),
            reply_markup=inline(
                [(tr("btn.skip"), Fd(action="unit_skip"))],
                [(tr("btn.cancel"), Fd(action="cancel"))],
            ),
        )
    elif ftype is FieldType.DURATION:
        await state.set_state(TypeSG.field_format)
        await message.answer(
            tr("type.ask_format"),
            reply_markup=inline(
                [(tr("type.format_hmm"), Fd(action="dfmt", value="h:mm"))],
                [(tr("type.format_mmss"), Fd(action="dfmt", value="mm:ss"))],
            ),
        )
    elif ftype is FieldType.SELECTION:
        await state.set_state(TypeSG.field_choices)
        await message.answer(tr("type.ask_choices"), reply_markup=cancel_kb(tr))
    else:
        await _finish_field(message, tr, state, {})


@router.message(TypeSG.field_unit, F.text)
async def field_unit(message: Message, tr: Translator, state: FSMContext) -> None:
    assert message.text is not None
    await _finish_field(message, tr, state, {"unit": message.text.strip()})


@router.callback_query(TypeSG.field_unit, Fd.filter(F.action == "unit_skip"))
async def field_unit_skip(query: CallbackQuery, tr: Translator, state: FSMContext) -> None:
    await query.answer()
    await _finish_field(msg(query), tr, state, {})


@router.callback_query(TypeSG.field_format, Fd.filter(F.action == "dfmt"))
async def field_format(
    query: CallbackQuery, callback_data: Fd, tr: Translator, state: FSMContext
) -> None:
    await query.answer()
    extra = {"duration_format": callback_data.value, "aggregation": "sum"}
    await _finish_field(msg(query), tr, state, extra)


@router.message(TypeSG.field_choices, F.text)
async def field_choices(message: Message, tr: Translator, state: FSMContext) -> None:
    assert message.text is not None
    choices = [c.strip() for c in message.text.split(",") if c.strip()]
    await _finish_field(message, tr, state, {"choices": choices})


async def _finish_field(
    message: Message, tr: Translator, state: FSMContext, extra: dict[str, Any]
) -> None:
    data = await state.get_data()
    fields = _schema(data["fields"]).fields
    raw = {**data["pending"], **extra, "key": next_custom_key(fields)}
    try:
        field = FieldDefinition.model_validate(raw)
        _schema([*data["fields"], field.model_dump(mode="json", exclude_none=True)])
    except ValidationError as exc:
        raise ServiceError("bad_field") from exc
    await state.update_data(
        fields=[*data["fields"], field.model_dump(mode="json", exclude_none=True)], pending=None
    )
    await _show_builder(message, tr, state)


@router.callback_query(TypeSG.builder, Fd.filter(F.action == "save_type"))
async def save_type(
    query: CallbackQuery, session: AsyncSession, user: User, tr: Translator, state: FSMContext
) -> None:
    data = await state.get_data()
    fields = list(_schema(data["fields"]).fields)
    version = await ActivityService(session, user).create_type(data["name"], fields)
    await session.commit()
    await state.clear()
    await query.answer(tr("saved"))
    await _after_type_created(msg(query), tr, version.name, version.activity_type_id)


@router.callback_query(Ac.filter(F.action == "ai_type"))
async def ai_type_start(query: CallbackQuery, tr: Translator, state: FSMContext) -> None:
    await query.answer()
    await state.set_state(TypeSG.ai_text)
    await msg(query).answer(tr("type.ai_ask"), reply_markup=cancel_kb(tr))


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
    await message.answer(tr("food.working"))
    svc = ActivityDraftService(session, user, gateway)
    draft = await svc.draft(message.text)
    await session.commit()
    await state.clear()
    _, proposal = await svc.get(draft.id)
    lines = [tr("type.ai_preview", name=proposal.name), f"• {tr('type.duration_label')}"]
    for f in proposal.fields:
        extra = f", {f.unit}" if f.unit else ""
        if f.choices:
            extra += ": " + ", ".join(f.choices)
        lines.append(f"• {f.label} — {tr('ftype.' + f.type.value)}{extra}")
    if gateway.is_mock:
        lines.append(tr("draft.mock"))
    await message.answer(
        "\n".join(lines),
        reply_markup=inline(
            [
                (tr("type.create_btn"), Wd(a="type_ok", d=draft.id, v=draft.version)),
                (tr("btn.cancel"), Wd(a="type_no", d=draft.id, v=draft.version)),
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
) -> None:
    svc = ActivityDraftService(session, user, gateway)
    if callback_data.a == "type_no":
        await svc.cancel(callback_data.d, callback_data.v)
        await session.commit()
        await query.answer(tr("cancelled"))
        return
    version = await svc.confirm(callback_data.d, callback_data.v, tr("type.duration_label"))
    await session.commit()
    await query.answer(tr("saved"))
    await _after_type_created(msg(query), tr, version.name, version.activity_type_id)


@router.callback_query(Ac.filter(F.action == "types"))
async def types_list(
    query: CallbackQuery, session: AsyncSession, user: User, tr: Translator
) -> None:
    await query.answer()
    svc = ActivityService(session, user)
    items = await svc.list_types()
    if not items:
        await msg(query).answer(
            tr("act.no_types"), reply_markup=column([(tr("act.create"), Ac(action="create"))])
        )
        return
    lines = [tr("act.types_title")]
    for t in items:
        tv = await svc.current_type_version(t.id)
        labels = ", ".join(f["label"] for f in tv.fields)
        note = "" if t.counts_as_training else " " + tr("type.not_training")
        lines.append(f"• {t.name}{note}: {labels}")
    buttons = [(f"✅ {t.name}", Ac(action="rec_type", id=t.id)) for t in items]
    await msg(query).answer("\n".join(lines), reply_markup=column(buttons, width=2))


# --- templates -------------------------------------------------------------------------------


@router.callback_query(Ac.filter(F.action == "templates"))
async def templates(
    query: CallbackQuery, session: AsyncSession, user: User, tr: Translator
) -> None:
    await query.answer()
    items = await ActivityService(session, user).list_templates()
    buttons = [(t.name, Ac(action="tpl", id=t.id)) for t in items]
    buttons.append((tr("tpl.new"), Ac(action="new_tpl")))
    await msg(query).answer(
        tr("tpl.list") if items else tr("tpl.none"), reply_markup=column(buttons)
    )


@router.callback_query(Ac.filter(F.action == "new_tpl"))
async def new_template(
    query: CallbackQuery, session: AsyncSession, user: User, tr: Translator
) -> None:
    await query.answer()
    types = await ActivityService(session, user).list_types()
    if not types:
        await msg(query).answer(
            tr("act.no_types"), reply_markup=column([(tr("act.create"), Ac(action="create"))])
        )
        return
    buttons = [(t.name, Ac(action="tpl_type", id=t.id)) for t in types]
    await msg(query).answer(tr("tpl.choose_type"), reply_markup=column(buttons))


@router.callback_query(Ac.filter(F.action == "tpl_type"))
async def template_type(
    query: CallbackQuery,
    callback_data: Ac,
    session: AsyncSession,
    user: User,
    tr: Translator,
    state: FSMContext,
) -> None:
    await ActivityService(session, user).get_type(callback_data.id)  # ownership check
    await query.answer()
    await state.clear()
    await state.set_state(TplSG.name)
    await state.update_data(type_id=callback_data.id)
    await msg(query).answer(tr("tpl.ask_name"), reply_markup=cancel_kb(tr))


@router.message(TplSG.name, F.text)
async def template_name(message: Message, tr: Translator, state: FSMContext) -> None:
    assert message.text is not None
    name = " ".join(message.text.split())
    if not name or len(name) > 60:
        raise ServiceError("bad_name")
    await state.update_data(tpl_name=name)
    await state.set_state(TplSG.blocks)
    await message.answer(
        tr("tpl.blocks_ask"),
        reply_markup=inline(
            [(tr("btn.skip"), Fd(action="blocks_skip"))], [(tr("btn.cancel"), Fd(action="cancel"))]
        ),
    )


async def _start_targets(
    message: Message,
    session: AsyncSession,
    user: User,
    tr: Translator,
    state: FSMContext,
    body: WorkoutBody | None,
) -> None:
    data = await state.get_data()
    tv = await ActivityService(session, user).current_type_version(int(data["type_id"]))
    await state.update_data(
        mode="targets",
        fields=tv.fields,
        targets={},
        idx=0,
        values={},
        plan_blocks=body.dump() if body else [],
    )
    if body:
        await message.answer("\n".join([tr("tpl.blocks_parsed"), *format_body(tr, body)]))
    await message.answer(tr("tpl.targets_intro"))
    await _prompt_value(message, tr, state)


@router.message(TplSG.blocks, F.text)
async def template_blocks(
    message: Message, session: AsyncSession, user: User, tr: Translator, state: FSMContext
) -> None:
    assert message.text is not None
    body = _parse(parse_plan_text, message.text)
    await _start_targets(message, session, user, tr, state, body)


@router.callback_query(TplSG.blocks, Fd.filter(F.action == "blocks_skip"))
async def template_blocks_skip(
    query: CallbackQuery, session: AsyncSession, user: User, tr: Translator, state: FSMContext
) -> None:
    await query.answer()
    await _start_targets(msg(query), session, user, tr, state, None)


@router.callback_query(Ac.filter(F.action == "tpl"))
async def template_view(
    query: CallbackQuery, callback_data: Ac, session: AsyncSession, user: User, tr: Translator
) -> None:
    svc = ActivityService(session, user)
    template = await svc.get_template(callback_data.id)
    ctx = await svc.recording_context(template_id=template.id)
    await query.answer()
    lines = [f"{template.name} · {ctx.activity_name}"]
    if template.program_id is not None:
        lines.append(tr("tpl.in_program", name=(await svc.get_program(template.program_id)).name))
    for f in ctx.schema.fields:
        if ctx.targets.get(f.key) is not None:
            value = format_field_value(f, ctx.targets[f.key], tr("word.yes"), tr("word.no"))
            lines.append(f"• {f.label}: {value}")
    if ctx.target_blocks:
        lines.extend(format_body(tr, WorkoutBody.load(list(ctx.target_blocks))))
    lines.append(tr("tpl.plan_note"))
    await msg(query).answer(
        "\n".join(lines),
        reply_markup=column(
            [
                (tr("act.start_now"), Ac(action="rec_tpl", id=template.id)),
                (tr("tpl.plan_today"), Ac(action="plan_today", id=template.id)),
                (tr("tpl.plan_tomorrow"), Ac(action="plan_tomorrow", id=template.id)),
                (tr("tpl.plan_weekly"), Ac(action="plan_week", id=template.id)),
                (tr("tpl.to_program"), Ac(action="to_program", id=template.id)),
                (tr("tpl.archive"), Ac(action="archive", id=template.id)),
            ],
            width=2,
        ),
    )


@router.callback_query(Ac.filter(F.action.in_({"plan_today", "plan_tomorrow"})))
async def plan_template(
    query: CallbackQuery, callback_data: Ac, session: AsyncSession, user: User, tr: Translator
) -> None:
    day = local_today(user)
    if callback_data.action == "plan_tomorrow":
        day += dt.timedelta(days=1)
    await ActivityService(session, user).plan(callback_data.id, day)
    await session.commit()
    await query.answer(tr("saved"))
    await msg(query).answer(tr("plan.saved", date=day.strftime("%d.%m")))


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
    await query.answer()
    await state.clear()
    await state.update_data(week_tpl=callback_data.id, week_days=[])
    await _show_week(msg(query), tr, [], callback_data.id)


async def _show_week(message: Message, tr: Translator, days: list[int], tpl: int) -> None:
    buttons = [
        (("✅ " if i in days else "") + tr(k), Fd(action="wday", value=str(i)))
        for i, k in enumerate(WEEKDAY_KEYS)
    ]
    kb = column(buttons, width=4)
    kb.inline_keyboard.append(
        [
            *inline([(tr("plan.weeks_btn"), Fd(action="wdone", value=str(tpl)))]).inline_keyboard[
                0
            ],
            *cancel_kb(tr).inline_keyboard[0],
        ]
    )
    await message.answer(tr("plan.week_ask"), reply_markup=kb)


@router.callback_query(Fd.filter(F.action == "wday"))
async def plan_week_toggle(
    query: CallbackQuery, callback_data: Fd, tr: Translator, state: FSMContext
) -> None:
    data = await state.get_data()
    if "week_tpl" not in data or not callback_data.value.isdigit():
        await query.answer(tr("stale_button"))
        return
    day = int(callback_data.value) % 7
    days = set(data.get("week_days", []))
    days ^= {day}
    await state.update_data(week_days=sorted(days))
    await query.answer()
    await _show_week(msg(query), tr, sorted(days), int(data["week_tpl"]))


@router.callback_query(Fd.filter(F.action == "wdone"))
async def plan_week_done(
    query: CallbackQuery, session: AsyncSession, user: User, tr: Translator, state: FSMContext
) -> None:
    data = await state.get_data()
    if "week_tpl" not in data:
        await query.answer(tr("stale_button"))
        return
    created = await ActivityService(session, user).plan_weekdays(
        int(data["week_tpl"]), set(data.get("week_days", [])), weeks=4
    )
    await session.commit()
    await state.clear()
    await query.answer(tr("saved"))
    await msg(query).answer(tr("plan.week_saved", n=len(created)), reply_markup=main_menu(tr))


@router.callback_query(Ac.filter(F.action == "archive"))
async def archive_template(
    query: CallbackQuery, callback_data: Ac, session: AsyncSession, user: User, tr: Translator
) -> None:
    await ActivityService(session, user).archive_template(callback_data.id)
    await session.commit()
    await query.answer(tr("tpl.archived"), show_alert=True)


# --- programs --------------------------------------------------------------------------------


@router.callback_query(Ac.filter(F.action == "programs"))
async def programs(query: CallbackQuery, session: AsyncSession, user: User, tr: Translator) -> None:
    await query.answer()
    items = await ActivityService(session, user).list_programs()
    buttons = [(f"📚 {p.name}", Ac(action="program", id=p.id)) for p in items]
    buttons.append((tr("prog.new"), Ac(action="new_program")))
    await msg(query).answer(
        tr("prog.list") if items else tr("prog.none"), reply_markup=column(buttons)
    )


@router.callback_query(Ac.filter(F.action == "new_program"))
async def new_program(query: CallbackQuery, tr: Translator, state: FSMContext) -> None:
    await query.answer()
    await state.set_state(ProgramSG.name)
    await msg(query).answer(tr("prog.ask_name"), reply_markup=cancel_kb(tr))


@router.message(ProgramSG.name, F.text)
async def program_name(
    message: Message, session: AsyncSession, user: User, tr: Translator, state: FSMContext
) -> None:
    assert message.text is not None
    program = await ActivityService(session, user).create_program(message.text)
    await session.commit()
    await state.clear()
    await message.answer(
        tr("prog.saved", name=program.name),
        reply_markup=column([(tr("prog.open"), Ac(action="program", id=program.id))]),
    )


@router.callback_query(Ac.filter(F.action == "program"))
async def program_view(
    query: CallbackQuery, callback_data: Ac, session: AsyncSession, user: User, tr: Translator
) -> None:
    svc = ActivityService(session, user)
    program = await svc.get_program(callback_data.id)
    items = await svc.program_templates(program.id)
    await query.answer()
    lines = [f"📚 {program.name}"]
    lines += [f"• {t.name}" for t in items] or [tr("prog.empty")]
    buttons = [(t.name, Ac(action="tpl", id=t.id)) for t in items]
    buttons.append((tr("prog.archive"), Ac(action="program_archive", id=program.id)))
    await msg(query).answer("\n".join(lines), reply_markup=column(buttons))


@router.callback_query(Ac.filter(F.action == "program_archive"))
async def program_archive(
    query: CallbackQuery, callback_data: Ac, session: AsyncSession, user: User, tr: Translator
) -> None:
    await ActivityService(session, user).archive_program(callback_data.id)
    await session.commit()
    await query.answer(tr("prog.archived"), show_alert=True)


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
    await query.answer()
    if not items:
        await msg(query).answer(
            tr("prog.none"), reply_markup=column([(tr("prog.new"), Ac(action="new_program"))])
        )
        return
    await state.update_data(assign_tpl=callback_data.id)
    buttons = [(p.name, Ac(action="assign", id=p.id)) for p in items]
    await msg(query).answer(tr("prog.choose"), reply_markup=column(buttons))


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
        await query.answer(tr("stale_button"))
        return
    await ActivityService(session, user).assign_template(int(data["assign_tpl"]), callback_data.id)
    await session.commit()
    await state.clear()
    await query.answer(tr("saved"))


# --- planned -------------------------------------------------------------------------------------


@router.callback_query(Ac.filter(F.action == "planned"))
async def planned(query: CallbackQuery, session: AsyncSession, user: User, tr: Translator) -> None:
    await query.answer()
    today = local_today(user)
    items = await ActivityService(session, user).list_planned(today, today + dt.timedelta(days=7))
    if not items:
        await msg(query).answer(tr("plan.none"))
        return
    buttons = [
        (f"{p.planned_date.strftime('%d.%m')} · {v.name}", Ac(action="rec_plan", id=p.id))
        for p, v in items
    ]
    await msg(query).answer(tr("plan.list"), reply_markup=column(buttons))


# --- start / record -------------------------------------------------------------------------------


@router.callback_query(Ac.filter(F.action == "start"))
async def start_menu(
    query: CallbackQuery, session: AsyncSession, user: User, tr: Translator
) -> None:
    await query.answer()
    svc = ActivityService(session, user)
    today = local_today(user)
    buttons = [
        (f"📅 {v.name}", Ac(action="rec_plan", id=p.id))
        for p, v in await svc.list_planned(today, today)
    ]
    buttons += [(f"📋 {t.name}", Ac(action="rec_tpl", id=t.id)) for t in await svc.list_templates()]
    if not buttons:
        await msg(query).answer(
            tr("act.start_none"),
            reply_markup=column(
                [(tr("tpl.new"), Ac(action="new_tpl")), (tr("act.create"), Ac(action="create"))]
            ),
        )
        return
    await msg(query).answer(tr("act.start_ask"), reply_markup=column(buttons))


@router.callback_query(Ac.filter(F.action == "rec_menu"))
async def record_menu(
    query: CallbackQuery, session: AsyncSession, user: User, tr: Translator
) -> None:
    await query.answer()
    svc = ActivityService(session, user)
    today = local_today(user)
    buttons = [(tr("act.by_text"), Ac(action="by_text"))]
    buttons += [
        (f"📅 {v.name}", Ac(action="rec_plan", id=p.id))
        for p, v in await svc.list_planned(today, today)
    ]
    buttons += [(f"📋 {t.name}", Ac(action="rec_tpl", id=t.id)) for t in await svc.list_templates()]
    buttons += [(f"🏷 {t.name}", Ac(action="rec_type", id=t.id)) for t in await svc.list_types()]
    await msg(query).answer(tr("act.choose_what"), reply_markup=column(buttons))


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
    source = {key: callback_data.id}
    ctx = await ActivityService(session, user).recording_context(**source)
    await query.answer()
    await state.clear()
    target_items = [
        {"b": bi, "block": b, "item": it}
        for bi, b in enumerate(ctx.target_blocks)
        for it in b.get("items", [])
    ]
    await state.update_data(
        mode="session",
        source=source,
        kind=ctx.kind,
        fields=[f.model_dump(mode="json", exclude_none=True) for f in ctx.schema.fields],
        targets=ctx.targets,
        idx=0,
        values={},
        title=ctx.template_name or ctx.activity_name,
        plan_blocks=list(ctx.target_blocks),
        items=target_items,
        iidx=0,
        actual={},
    )
    await msg(query).answer(tr("rec.intro", name=ctx.template_name or ctx.activity_name))
    if target_items:
        await _prompt_item(msg(query), tr, state)
    else:
        await _prompt_value(msg(query), tr, state)


async def _prompt_item(message: Message, tr: Translator, state: FSMContext) -> None:
    data = await state.get_data()
    items = data["items"]
    i = int(data["iidx"])
    if i >= len(items):
        await _prompt_value(message, tr, state)
        return
    await state.set_state(ValuesSG.item)
    target = Item.model_validate(items[i]["item"])
    await message.answer(
        tr("rec.item_prompt", n=i + 1, total=len(items), target=format_item(target, words(tr))),
        reply_markup=inline(
            [
                (tr("rec.as_planned"), Fd(action="item_same")),
                (tr("btn.skip"), Fd(action="item_skip")),
            ],
            [(tr("btn.cancel"), Fd(action="cancel"))],
        ),
    )


async def _store_item(
    message: Message, tr: Translator, state: FSMContext, sets: list[dict[str, Any]] | None
) -> None:
    data = await state.get_data()
    i = int(data["iidx"])
    actual = dict(data["actual"])
    if sets:
        actual[str(i)] = sets
    await state.update_data(actual=actual, iidx=i + 1)
    await _prompt_item(message, tr, state)


@router.message(ValuesSG.item, F.text)
async def item_text(message: Message, tr: Translator, state: FSMContext) -> None:
    assert message.text is not None
    data = await state.get_data()
    target = Item.model_validate(data["items"][int(data["iidx"])]["item"])
    loads = {s.load_kg for s in target.sets if not s.warmup and s.load_kg is not None}
    default_load = loads.pop() if len(loads) == 1 else None
    sets = _parse(parse_sets, message.text, default_load=default_load)
    warm = all(s.warmup for s in target.sets) and bool(target.sets)
    await _store_item(
        message,
        tr,
        state,
        [
            s.model_copy(update={"warmup": warm}).model_dump(
                mode="json", exclude_none=True, exclude_defaults=True
            )
            for s in sets
        ],
    )


@router.callback_query(ValuesSG.item, Fd.filter(F.action.in_({"item_same", "item_skip"})))
async def item_button(
    query: CallbackQuery, callback_data: Fd, tr: Translator, state: FSMContext
) -> None:
    await query.answer()
    sets = None
    if callback_data.action == "item_same":
        # Explicit user action: "done exactly as planned".
        data = await state.get_data()
        sets = list(data["items"][int(data["iidx"])]["item"].get("sets", []))
    await _store_item(msg(query), tr, state, sets)


async def _prompt_value(message: Message, tr: Translator, state: FSMContext) -> None:
    data = await state.get_data()
    fields = _schema(data["fields"]).fields
    idx = int(data["idx"])
    if idx >= len(fields):
        await _values_done(message, tr, state)
        return
    await state.set_state(ValuesSG.value)
    field = fields[idx]
    target = data.get("targets", {}).get(field.key)
    await message.answer(field_prompt(tr, field, target), reply_markup=field_input_kb(tr, field))


async def _store_value(
    message: Message, tr: Translator, state: FSMContext, raw: str | None
) -> None:
    data = await state.get_data()
    field = _schema(data["fields"]).fields[int(data["idx"])]
    values = dict(data["values"])
    if raw is not None:
        _parse(parse_field_value, field, raw)  # immediate feedback; the service re-validates
        values[field.key] = raw
    await state.update_data(values=values, idx=int(data["idx"]) + 1)
    await _prompt_value(message, tr, state)


@router.message(ValuesSG.value, F.text)
async def value_text(message: Message, tr: Translator, state: FSMContext) -> None:
    await _store_value(message, tr, state, message.text)


@router.callback_query(ValuesSG.value, Fd.filter(F.action.in_({"skip", "choice", "bool"})))
async def value_button(
    query: CallbackQuery, callback_data: Fd, tr: Translator, state: FSMContext
) -> None:
    await query.answer()
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
    await _store_value(msg(query), tr, state, raw)


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


async def _values_done(message: Message, tr: Translator, state: FSMContext) -> None:
    data = await state.get_data()
    schema = _schema(data["fields"])
    lines = []
    for f in schema.fields:
        raw = data["values"].get(f.key)
        if raw is not None:
            shown = format_field_value(f, parse_field_value(f, raw), tr("word.yes"), tr("word.no"))
            lines.append(f"• {f.label}: {shown}")
    if data["mode"] == "targets":
        await state.set_state(ValuesSG.review)
        plan = WorkoutBody.load(data.get("plan_blocks") or [])
        await message.answer(
            "\n".join([tr("tpl.review", name=data["tpl_name"]), *lines, *format_body(tr, plan)])
            if (lines or plan.blocks)
            else tr("tpl.review", name=data["tpl_name"]) + "\n—",
            reply_markup=inline(
                [(tr("btn.save"), Fd(action="save_tpl"))], [(tr("btn.cancel"), Fd(action="cancel"))]
            ),
        )
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
        await message.answer(
            tr("rec.extra_ask"),
            reply_markup=inline(
                [(tr("btn.skip"), Fd(action="extra_skip"))],
                [(tr("btn.cancel"), Fd(action="cancel"))],
            ),
        )
        return
    if body:
        lines.extend(format_body(tr, body))
    if not lines:
        await state.clear()
        await message.answer(tr("rec.empty"), reply_markup=main_menu(tr))
        return
    await state.set_state(ValuesSG.review)
    await message.answer(
        tr("rec.review", name=data["title"]) + "\n" + "\n".join(lines),
        reply_markup=inline(
            [(tr("btn.save"), Fd(action="save_session"))], [(tr("btn.cancel"), Fd(action="cancel"))]
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
    await query.answer()
    await _values_done(msg(query), tr, state)


@router.callback_query(ValuesSG.review, Fd.filter(F.action == "save_session"))
async def save_session(
    query: CallbackQuery, session: AsyncSession, user: User, tr: Translator, state: FSMContext
) -> None:
    data = await state.get_data()
    svc = ActivityService(session, user)
    ctx = await svc.recording_context(**{k: int(v) for k, v in data["source"].items()})
    await svc.record_session(ctx, dict(data["values"]), blocks=_actual_body(data))
    await session.commit()
    await state.clear()
    await query.answer(tr("saved"))
    await msg(query).answer(tr("rec.saved"), reply_markup=main_menu(tr))


@router.callback_query(ValuesSG.review, Fd.filter(F.action == "save_tpl"))
async def save_template(
    query: CallbackQuery, session: AsyncSession, user: User, tr: Translator, state: FSMContext
) -> None:
    data = await state.get_data()
    body = WorkoutBody.load(data.get("plan_blocks") or [])
    version = await ActivityService(session, user).create_template(
        int(data["type_id"]),
        data["tpl_name"],
        dict(data["values"]),
        blocks=body if body.blocks else None,
    )
    await session.commit()
    await state.clear()
    await query.answer(tr("saved"))
    await msg(query).answer(
        tr("tpl.saved", name=version.name),
        reply_markup=column(
            [
                (tr("act.start_now"), Ac(action="rec_tpl", id=version.template_id)),
                (tr("tpl.plan_today"), Ac(action="plan_today", id=version.template_id)),
            ]
        ),
    )


# --- free-text workout logging ---------------------------------------------------------------


@router.callback_query(Ac.filter(F.action == "by_text"))
async def by_text(
    query: CallbackQuery, user: User, tr: Translator, gateway: AIGateway, state: FSMContext
) -> None:
    await query.answer()
    await state.set_state(TextSG.workout)
    key = "wo.text_ask_ai" if gateway.text_available(user) else "wo.text_ask"
    await msg(query).answer(tr(key), reply_markup=cancel_kb(tr))


async def show_workout_draft(
    message: Message, session: AsyncSession, user: User, tr: Translator, draft: Draft
) -> None:
    svc = WorkoutDraftService(session, user)
    row, state = await svc.get(draft.id)
    lines = [tr("wo.draft_title")]
    types = await ActivityService(session, user).list_types()
    chosen = next((t for t in types if t.id == state.type_id), None)
    lines.append(tr("wo.draft_type", name=chosen.name if chosen else tr("wo.type_missing")))
    if state.duration_s is not None:
        lines.append(tr("wo.draft_duration", min=state.duration_s // 60))
    if state.distance_km is not None:
        lines.append(tr("wo.draft_distance", km=num(tr, state.distance_km, 2)))
    lines.extend(format_body(tr, WorkoutBody(blocks=tuple(state.blocks))))
    if state.notes:
        lines.append(tr("wo.draft_notes", text=state.notes))
    if state.clarification:
        lines.append("❓ " + state.clarification)
    if state.mock:
        lines.append(tr("draft.mock"))
    lines.append(tr("wo.draft_hint"))
    rows: list[list[tuple[str, Any]]] = []
    for t in types:
        if t.id != state.type_id:
            rows.append([(f"🏷 {t.name}", Wd(a="type", d=row.id, v=row.version, t=t.id))])
    if chosen is None:
        rows.append(
            [
                (
                    tr(
                        "wo.create_starter",
                        name=tr(
                            "starter."
                            + (
                                state.kind.value
                                if state.kind is not ActivityKind.CUSTOM
                                else "strength"
                            )
                        ),
                    ),
                    Wd(a="starter", d=row.id, v=row.version),
                )
            ]
        )
    else:
        rows.append([(tr("draft.confirm"), Wd(a="ok", d=row.id, v=row.version))])
    rows.append([(tr("draft.cancel"), Wd(a="no", d=row.id, v=row.version))])
    await message.answer("\n".join(lines), reply_markup=inline(*rows[:12]))


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
    draft = await WorkoutDraftService(session, user, gateway).draft_from_text(message.text)
    await session.commit()
    await state.clear()
    await show_workout_draft(message, session, user, tr, draft)


@router.callback_query(Wd.filter(F.a.in_({"type", "starter", "ok", "no"})))
async def workout_draft_action(
    query: CallbackQuery, callback_data: Wd, session: AsyncSession, user: User, tr: Translator
) -> None:
    svc = WorkoutDraftService(session, user)
    cb = callback_data
    if cb.a == "ok":
        await svc.confirm(cb.d, cb.v)
        await session.commit()
        await query.answer(tr("saved"))
        await msg(query).answer(tr("rec.saved"), reply_markup=main_menu(tr))
        return
    if cb.a == "no":
        await svc.cancel(cb.d, cb.v)
        await session.commit()
        await query.answer(tr("cancelled"))
        return
    if cb.a == "type":
        draft = await svc.set_type(cb.d, cb.v, cb.t)
    else:
        draft = await svc.create_starter_type(cb.d, cb.v, tr)
    await session.commit()
    await query.answer()
    await show_workout_draft(msg(query), session, user, tr, draft)


# --- Strong CSV import -------------------------------------------------------------------------


@router.callback_query(Ac.filter(F.action == "import"))
async def import_start(query: CallbackQuery, tr: Translator, state: FSMContext) -> None:
    await query.answer()
    await state.set_state(ImportSG.file)
    await msg(query).answer(tr("imp.ask"), reply_markup=cancel_kb(tr))


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
    await message.answer(
        "\n".join(lines),
        reply_markup=inline(
            [(tr("imp.confirm"), Ac(action="import_ok", id=batch.id))],
            [(tr("btn.cancel"), Fd(action="cancel"))],
        ),
    )


@router.callback_query(Ac.filter(F.action == "import_ok"))
async def import_confirm(
    query: CallbackQuery, callback_data: Ac, session: AsyncSession, user: User, tr: Translator
) -> None:
    count = await StrongImportService(session, user).confirm(callback_data.id, tr)
    await session.commit()
    await query.answer(tr("saved"))
    await msg(query).answer(tr("imp.done", n=count), reply_markup=main_menu(tr))
