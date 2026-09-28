"""Telegram UI for the universal activity builder. Works entirely without AI."""

from __future__ import annotations

import datetime as dt
from typing import Any

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from fitcoach.bot.ui import (
    Ac,
    Fd,
    cancel_kb,
    column,
    field_input_kb,
    field_prompt,
    inline,
    main_menu,
)
from fitcoach.db.models import User
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
from fitcoach.domain.units import ParseError
from fitcoach.i18n import Translator, all_labels
from fitcoach.services.activities import ActivityService
from fitcoach.services.errors import ServiceError
from fitcoach.services.users import local_today

router = Router(name="activities")


class TypeSG(StatesGroup):
    name = State()
    builder = State()
    field_label = State()
    field_type = State()
    field_unit = State()
    field_choices = State()
    field_format = State()


class TplSG(StatesGroup):
    name = State()


class ValuesSG(StatesGroup):
    value = State()
    review = State()


def _msg(query: CallbackQuery) -> Message:
    assert isinstance(query.message, Message)
    return query.message


def _schema(fields: list[dict[str, Any]]) -> FieldSchema:
    return FieldSchema.model_validate({"fields": fields})


# --- menu ------------------------------------------------------------------


@router.message(F.text.in_(all_labels("menu.training")))
async def training_menu(message: Message, tr: Translator, state: FSMContext) -> None:
    await state.clear()
    await message.answer(
        tr("act.menu"),
        reply_markup=column(
            [
                (tr("act.record"), Ac(action="rec_menu")),
                (tr("act.planned"), Ac(action="planned")),
                (tr("act.templates"), Ac(action="templates")),
                (tr("act.new_type"), Ac(action="new_type")),
            ]
        ),
    )


# --- activity type builder ---------------------------------------------------


@router.callback_query(Ac.filter(F.action == "new_type"))
async def new_type(query: CallbackQuery, tr: Translator, state: FSMContext) -> None:
    await query.answer()
    await state.clear()
    await state.set_state(TypeSG.name)
    await _msg(query).answer(tr("type.ask_name"), reply_markup=cancel_kb(tr))


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
    await _msg(query).answer(tr("type.ask_field_label"), reply_markup=cancel_kb(tr))


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
    pending = {**data["pending"], "type": ftype.value}
    await state.update_data(pending=pending)
    message = _msg(query)
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
    await _finish_field(_msg(query), tr, state, {})


@router.callback_query(TypeSG.field_format, Fd.filter(F.action == "dfmt"))
async def field_format(
    query: CallbackQuery, callback_data: Fd, tr: Translator, state: FSMContext
) -> None:
    await query.answer()
    extra = {"duration_format": callback_data.value, "aggregation": "sum"}
    await _finish_field(_msg(query), tr, state, extra)


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
    await _msg(query).answer(
        tr("type.saved", name=version.name),
        reply_markup=column(
            [
                (tr("act.record_now"), Ac(action="rec_type", id=version.activity_type_id)),
                (tr("tpl.new"), Ac(action="tpl_type", id=version.activity_type_id)),
            ]
        ),
    )


# --- templates -------------------------------------------------------------


@router.callback_query(Ac.filter(F.action == "templates"))
async def templates(
    query: CallbackQuery, session: AsyncSession, user: User, tr: Translator
) -> None:
    await query.answer()
    items = await ActivityService(session, user).list_templates()
    buttons = [(t.name, Ac(action="tpl", id=t.id)) for t in items]
    buttons.append((tr("tpl.new"), Ac(action="new_tpl")))
    text = tr("tpl.list") if items else tr("tpl.none")
    await _msg(query).answer(text, reply_markup=column(buttons))


@router.callback_query(Ac.filter(F.action == "new_tpl"))
async def new_template(
    query: CallbackQuery, session: AsyncSession, user: User, tr: Translator
) -> None:
    await query.answer()
    types = await ActivityService(session, user).list_types()
    if not types:
        await _msg(query).answer(
            tr("act.no_types"), reply_markup=column([(tr("act.new_type"), Ac(action="new_type"))])
        )
        return
    buttons = [(t.name, Ac(action="tpl_type", id=t.id)) for t in types]
    await _msg(query).answer(tr("tpl.choose_type"), reply_markup=column(buttons))


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
    await _msg(query).answer(tr("tpl.ask_name"), reply_markup=cancel_kb(tr))


@router.message(TplSG.name, F.text)
async def template_name(
    message: Message, session: AsyncSession, user: User, tr: Translator, state: FSMContext
) -> None:
    assert message.text is not None
    name = " ".join(message.text.split())
    if not name or len(name) > 60:
        raise ServiceError("bad_name")
    data = await state.get_data()
    tv = await ActivityService(session, user).current_type_version(int(data["type_id"]))
    await state.update_data(
        mode="targets", tpl_name=name, fields=tv.fields, targets={}, idx=0, values={}
    )
    await message.answer(tr("tpl.targets_intro"))
    await _prompt_value(message, tr, state)


@router.callback_query(Ac.filter(F.action == "tpl"))
async def template_view(
    query: CallbackQuery, callback_data: Ac, session: AsyncSession, user: User, tr: Translator
) -> None:
    svc = ActivityService(session, user)
    template = await svc.get_template(callback_data.id)
    ctx = await svc.recording_context(template_id=template.id)
    await query.answer()
    lines = [f"{template.name} · {ctx.activity_name}"]
    for f in ctx.schema.fields:
        if ctx.targets.get(f.key) is not None:
            value = format_field_value(f, ctx.targets[f.key], tr("word.yes"), tr("word.no"))
            lines.append(f"• {f.label}: {value}")
    lines.append(tr("tpl.plan_note"))
    await _msg(query).answer(
        "\n".join(lines),
        reply_markup=column(
            [
                (tr("act.record_now"), Ac(action="rec_tpl", id=template.id)),
                (tr("tpl.plan_today"), Ac(action="plan_today", id=template.id)),
                (tr("tpl.plan_tomorrow"), Ac(action="plan_tomorrow", id=template.id)),
                (tr("tpl.archive"), Ac(action="archive", id=template.id)),
            ]
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
    await _msg(query).answer(tr("plan.saved", date=day.strftime("%d.%m")))


@router.callback_query(Ac.filter(F.action == "archive"))
async def archive_template(
    query: CallbackQuery, callback_data: Ac, session: AsyncSession, user: User, tr: Translator
) -> None:
    await ActivityService(session, user).archive_template(callback_data.id)
    await session.commit()
    await query.answer(tr("tpl.archived"), show_alert=True)


# --- planned ---------------------------------------------------------------


@router.callback_query(Ac.filter(F.action == "planned"))
async def planned(query: CallbackQuery, session: AsyncSession, user: User, tr: Translator) -> None:
    await query.answer()
    today = local_today(user)
    items = await ActivityService(session, user).list_planned(today, today + dt.timedelta(days=7))
    if not items:
        await _msg(query).answer(tr("plan.none"))
        return
    buttons = [
        (f"{p.planned_date.strftime('%d.%m')} · {v.name}", Ac(action="rec_plan", id=p.id))
        for p, v in items
    ]
    await _msg(query).answer(tr("plan.list"), reply_markup=column(buttons))


# --- recording -------------------------------------------------------------


@router.callback_query(Ac.filter(F.action == "rec_menu"))
async def record_menu(
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
    buttons += [(f"🏷 {t.name}", Ac(action="rec_type", id=t.id)) for t in await svc.list_types()]
    if not buttons:
        await _msg(query).answer(
            tr("act.no_types"), reply_markup=column([(tr("act.new_type"), Ac(action="new_type"))])
        )
        return
    await _msg(query).answer(tr("act.choose_what"), reply_markup=column(buttons))


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
    await state.update_data(
        mode="session",
        source=source,
        fields=[f.model_dump(mode="json", exclude_none=True) for f in ctx.schema.fields],
        targets=ctx.targets,
        idx=0,
        values={},
        title=ctx.template_name or ctx.activity_name,
    )
    await _msg(query).answer(tr("rec.intro", name=ctx.template_name or ctx.activity_name))
    await _prompt_value(_msg(query), tr, state)


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
        try:
            parse_field_value(field, raw)  # immediate feedback; the service re-validates
        except ParseError as exc:
            raise ServiceError(exc.code) from exc
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
    await _store_value(_msg(query), tr, state, raw)


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
        await message.answer(
            tr("tpl.review", name=data["tpl_name"]) + "\n" + ("\n".join(lines) or "—"),
            reply_markup=inline(
                [(tr("btn.save"), Fd(action="save_tpl"))], [(tr("btn.cancel"), Fd(action="cancel"))]
            ),
        )
        return
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


@router.callback_query(ValuesSG.review, Fd.filter(F.action == "save_session"))
async def save_session(
    query: CallbackQuery, session: AsyncSession, user: User, tr: Translator, state: FSMContext
) -> None:
    data = await state.get_data()
    svc = ActivityService(session, user)
    ctx = await svc.recording_context(**{k: int(v) for k, v in data["source"].items()})
    await svc.record_session(ctx, dict(data["values"]))
    await session.commit()
    await state.clear()
    await query.answer(tr("saved"))
    await _msg(query).answer(tr("rec.saved"), reply_markup=main_menu(tr))


@router.callback_query(ValuesSG.review, Fd.filter(F.action == "save_tpl"))
async def save_template(
    query: CallbackQuery, session: AsyncSession, user: User, tr: Translator, state: FSMContext
) -> None:
    data = await state.get_data()
    version = await ActivityService(session, user).create_template(
        int(data["type_id"]), data["tpl_name"], dict(data["values"])
    )
    await session.commit()
    await state.clear()
    await query.answer(tr("saved"))
    await _msg(query).answer(
        tr("tpl.saved", name=version.name),
        reply_markup=column(
            [
                (tr("act.record_now"), Ac(action="rec_tpl", id=version.template_id)),
                (tr("tpl.plan_today"), Ac(action="plan_today", id=version.template_id)),
            ]
        ),
    )


@router.callback_query()
async def stale_callback(query: CallbackQuery, tr: Translator) -> None:
    """Buttons from finished flows (e.g. a second tap on Save) do nothing."""
    await query.answer(tr("stale_button"))
