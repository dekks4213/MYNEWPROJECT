""" "📊 Мой день" with day paging, ⚖️ weight and corrections."""

from __future__ import annotations

import datetime as dt
import re
from decimal import Decimal

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from fitcoach.bot.handlers.common import Event
from fitcoach.bot.screen import answer, render
from fitcoach.bot.ui import (
    WEIGHT_STEPS,
    En,
    Fd,
    Fm,
    Go,
    Row,
    day_month,
    food_line,
    format_day,
    inline,
    nav,
    num,
    rel_day,
    session_line,
    signed,
)
from fitcoach.db.models import FoodEntry, User, WeightEntry
from fitcoach.i18n import Translator, all_labels
from fitcoach.services.diary import DiaryService, EntryKind, parse_weight
from fitcoach.services.errors import Conflict, ServiceError
from fitcoach.services.summary import build_day_summary
from fitcoach.services.users import local_today

router = Router(name="day")
MAX_PAST_DAYS = 365
MAX_FUTURE_DAYS = 7
_WEIGHT_RE = re.compile(r"^\d{2,3}(\.\d{1,2})?$")


class WeightSG(StatesGroup):
    value = State()


class EditSG(StatesGroup):
    kcal = State()


# --- my day ------------------------------------------------------------------------------------


def _short(tr: Translator, day: dt.date, today: dt.date) -> str:
    delta = (today - day).days
    if delta in (-1, 0, 1):
        return tr(("rel.tomorrow_cap", "rel.today_cap", "rel.yesterday_cap")[delta + 1])
    return day.strftime("%d.%m")


async def show_day(
    event: Event,
    session: AsyncSession,
    user: User,
    tr: Translator,
    state: FSMContext,
    offset: int = 0,
) -> None:
    await state.clear()
    today = local_today(user)
    day = today + dt.timedelta(days=offset)
    summary = await build_day_summary(session, user, day=day)
    text = format_day(
        tr,
        summary,
        today,
        protein=user.protein_target_g,
        fat=user.fat_target_g,
        carbs=user.carbs_target_g,
    )
    rows: list[Row] = []
    if offset == 0:
        rows.append(
            [
                (tr("day.add_food"), Fm(a="menu")),
                (tr("day.add_training"), Go(s="train")),
                (tr("day.add_weight"), Go(s="weight")),
            ]
        )
    paging: Row = []
    if offset > -MAX_PAST_DAYS:
        paging.append(
            ("‹ " + _short(tr, day - dt.timedelta(days=1), today), Go(s="day", a=str(offset - 1)))
        )
    if offset < MAX_FUTURE_DAYS:
        paging.append(
            (_short(tr, day + dt.timedelta(days=1), today) + " ›", Go(s="day", a=str(offset + 1)))
        )
    rows.append(paging)
    if offset == 0:
        rows.append([(tr("day.fix"), Fd(action="fix")), (tr("nav.home"), Go(s="home"))])
    else:
        rows.append([(tr("day.to_today"), Go(s="day", a="0")), (tr("nav.home"), Go(s="home"))])
    await answer(event)
    await render(event, state, text, inline(*rows))


@router.message(Command("today"))
@router.message(F.text.in_(all_labels("menu.day") | all_labels("menu.old_day")))
async def my_day(
    message: Message, session: AsyncSession, user: User, tr: Translator, state: FSMContext
) -> None:
    await show_day(message, session, user, tr, state)


@router.callback_query(Go.filter(F.s == "day"))
async def my_day_cb(
    query: CallbackQuery,
    callback_data: Go,
    session: AsyncSession,
    user: User,
    tr: Translator,
    state: FSMContext,
) -> None:
    try:
        offset = int(callback_data.a or "0")
    except ValueError:
        offset = 0
    offset = max(-MAX_PAST_DAYS, min(MAX_FUTURE_DAYS, offset))
    await show_day(query, session, user, tr, state, offset)


# --- weight ------------------------------------------------------------------------------------


def _delta_text(tr: Translator, value: Decimal, last: WeightEntry | None, today: dt.date) -> str:
    if last is None:
        return ""
    delta = signed(tr, value - last.weight_kg, 1)
    days = (today - last.local_date).days
    if days == 0:
        return tr("weight.delta_today", delta=delta)
    if days == 1:
        return tr("weight.delta_yesterday", delta=delta)
    return tr("weight.delta_date", delta=delta, date=day_month(tr, last.local_date))


async def show_weight(
    event: Event,
    session: AsyncSession,
    user: User,
    tr: Translator,
    state: FSMContext,
    proposal: Decimal | None = None,
) -> None:
    await state.clear()
    await state.set_state(WeightSG.value)
    last = await DiaryService(session, user).latest_weight()
    await answer(event)
    if last is None:
        await render(event, state, tr("weight.first"), inline(nav(tr)))
        return
    value = proposal if proposal is not None else last.weight_kg
    text = tr(
        "weight.screen",
        kg=num(tr, last.weight_kg, 1),
        when=rel_day(tr, last.local_date, local_today(user)),
    )
    steps: Row = []
    for step in WEIGHT_STEPS:
        shifted = value + Decimal(step)
        if Decimal(20) <= shifted <= Decimal(400):
            label = step.replace("-", "−").replace(".", tr("fmt.decimal"))
            steps.append((label, Fd(action="wadj", value=str(shifted))))
    await render(
        event,
        state,
        text,
        inline(
            [(f"{num(tr, value, 1)} {tr('unit.kg')}", Fd(action="wq", value=str(value)))],
            steps,
            [(tr("weight.type_other"), Fd(action="wtype"))],
            nav(tr),
        ),
    )


def _weight_value(raw: str) -> Decimal:
    if not _WEIGHT_RE.match(raw):
        raise ServiceError("not_a_number")
    return parse_weight(raw)


@router.message(Command("weight"))
async def weight_cmd(
    message: Message, session: AsyncSession, user: User, tr: Translator, state: FSMContext
) -> None:
    await show_weight(message, session, user, tr, state)


@router.callback_query(Go.filter(F.s == "weight"))
async def weight_cb(
    query: CallbackQuery, session: AsyncSession, user: User, tr: Translator, state: FSMContext
) -> None:
    await show_weight(query, session, user, tr, state)


@router.callback_query(Fd.filter(F.action == "wadj"))
async def weight_adjust(
    query: CallbackQuery,
    callback_data: Fd,
    session: AsyncSession,
    user: User,
    tr: Translator,
    state: FSMContext,
) -> None:
    value = _weight_value(callback_data.value)
    await show_weight(query, session, user, tr, state, value)


@router.callback_query(Fd.filter(F.action == "wtype"))
async def weight_type(query: CallbackQuery, tr: Translator, state: FSMContext) -> None:
    await state.set_state(WeightSG.value)
    await answer(query)
    await render(query, state, tr("weight.type_ask"), inline(nav(tr, Go(s="weight"))))


async def confirm_weight(
    event: Event,
    session: AsyncSession,
    user: User,
    tr: Translator,
    state: FSMContext,
    value: Decimal,
) -> None:
    """A proposed value (quick button, typed text outside the weight screen) is shown first."""
    await state.set_state(WeightSG.value)
    last = await DiaryService(session, user).latest_weight()
    lines = [f"⚖️ {num(tr, value, 1)} {tr('unit.kg')}"]
    delta = _delta_text(tr, value, last, local_today(user))
    if delta:
        lines.append(delta)
    await answer(event)
    await render(
        event,
        state,
        "\n".join(lines),
        inline(
            [
                (tr("btn.save"), Fd(action="wsave", value=str(value))),
                (tr("btn.change"), Fd(action="wadj", value=str(value))),
            ],
            nav(tr),
        ),
    )


@router.callback_query(Fd.filter(F.action == "wq"))
async def weight_quick(
    query: CallbackQuery,
    callback_data: Fd,
    session: AsyncSession,
    user: User,
    tr: Translator,
    state: FSMContext,
) -> None:
    await confirm_weight(query, session, user, tr, state, _weight_value(callback_data.value))


async def _saved(
    event: Event,
    session: AsyncSession,
    user: User,
    tr: Translator,
    state: FSMContext,
    raw: str,
) -> None:
    diary = DiaryService(session, user)
    last = await diary.latest_weight()
    entry = await diary.add_weight(raw)
    await session.commit()
    await state.clear()
    lines = [tr("weight.saved", kg=num(tr, entry.weight_kg, 1))]
    delta = _delta_text(tr, entry.weight_kg, last, local_today(user))
    if delta:
        lines.append(delta)
    if isinstance(event, CallbackQuery):
        await answer(event, tr("saved"))
    await render(
        event,
        state,
        "\n".join(lines),
        inline(
            [
                (tr("btn.undo"), En(action="del", kind="weight", id=entry.id)),
                (tr("home.btn_day"), Go(s="day", a="0")),
            ],
            nav(tr),
        ),
    )


@router.callback_query(WeightSG.value, Fd.filter(F.action == "wsave"))
async def weight_save(
    query: CallbackQuery,
    callback_data: Fd,
    session: AsyncSession,
    user: User,
    tr: Translator,
    state: FSMContext,
) -> None:
    await _saved(query, session, user, tr, state, str(_weight_value(callback_data.value)))


@router.message(WeightSG.value, F.text)
async def weight_value(
    message: Message, session: AsyncSession, user: User, tr: Translator, state: FSMContext
) -> None:
    assert message.text is not None
    await _saved(message, session, user, tr, state, message.text)


# --- corrections -------------------------------------------------------------------------------


async def show_fix(
    event: Event, session: AsyncSession, user: User, tr: Translator, state: FSMContext
) -> None:
    await state.clear()
    food, weights, sessions = await DiaryService(session, user).entries_for_day()
    await answer(event)
    back = inline(nav(tr, Go(s="day", a="0")))
    if not (food or weights or sessions):
        await render(event, state, tr("fix.empty"), back)
        return
    lines = [tr("fix.title"), ""]
    rows: list[Row] = []
    n = 0
    for entry in food:
        n += 1
        lines.append(f"{n}. 🍽 {food_line(tr, entry)}")
        rows.append(
            [
                (f"✏️ {n}", En(action="edit", kind="food", id=entry.id, v=entry.version)),
                (f"🗑 {n}", En(action="del", kind="food", id=entry.id)),
            ]
        )
    for w in weights:
        n += 1
        lines.append(f"{n}. ⚖️ {num(tr, w.weight_kg, 1)} {tr('unit.kg')}")
        rows.append([(f"🗑 {n}", En(action="del", kind="weight", id=w.id))])
    for s in sessions:
        n += 1
        lines.append(f"{n}. 🏋️ {session_line(tr, s)}")
        rows.append([(f"🗑 {n}", En(action="del", kind="session", id=s.id))])
    lines += ["", tr("fix.hint")]
    rows.append(nav(tr, Go(s="day", a="0")))
    await render(event, state, "\n".join(lines), inline(*rows))


@router.message(Command("fix"))
async def fix_cmd(
    message: Message, session: AsyncSession, user: User, tr: Translator, state: FSMContext
) -> None:
    await show_fix(message, session, user, tr, state)


@router.callback_query(Fd.filter(F.action == "fix"))
async def fix_cb(
    query: CallbackQuery, session: AsyncSession, user: User, tr: Translator, state: FSMContext
) -> None:
    await show_fix(query, session, user, tr, state)


@router.callback_query(En.filter(F.action.in_({"del", "restore"})))
async def entry_delete_restore(
    query: CallbackQuery,
    callback_data: En,
    session: AsyncSession,
    user: User,
    tr: Translator,
    state: FSMContext,
) -> None:
    if callback_data.kind not in ("food", "weight", "session"):
        raise ServiceError("not_found")
    kind: EntryKind = callback_data.kind  # type: ignore[assignment]
    diary = DiaryService(session, user)
    await state.clear()
    to_list = (tr("fix.to_list"), Fd(action="fix"))
    if callback_data.action == "del":
        await diary.delete(kind, callback_data.id)
        await session.commit()
        await answer(query, tr("fix.deleted"))
        await render(
            query,
            state,
            tr("fix.deleted_screen"),
            inline(
                [(tr("btn.restore"), En(action="restore", kind=kind, id=callback_data.id))],
                [to_list, (tr("nav.home"), Go(s="home"))],
            ),
        )
    else:
        await diary.restore(kind, callback_data.id)
        await session.commit()
        await answer(query, tr("fix.restored"))
        await render(
            query,
            state,
            tr("fix.restored_screen"),
            inline([to_list, (tr("nav.home"), Go(s="home"))]),
        )


@router.callback_query(En.filter(F.action == "edit"))
async def entry_edit(
    query: CallbackQuery, callback_data: En, tr: Translator, state: FSMContext
) -> None:
    await answer(query)
    await state.set_state(EditSG.kcal)
    await state.update_data(entry_id=callback_data.id, version=callback_data.v)
    await render(
        query,
        state,
        tr("fix.ask_kcal"),
        inline(
            [(tr("food.kcal_unknown_btn"), Fd(action="edit_unknown"))],
            nav(tr, Fd(action="fix"), home=False),
        ),
    )


async def _apply_edit(
    event: Event,
    session: AsyncSession,
    user: User,
    tr: Translator,
    state: FSMContext,
    raw: str | None,
) -> None:
    data = await state.get_data()
    try:
        entry: FoodEntry = await DiaryService(session, user).update_food_energy(
            int(data["entry_id"]), int(data["version"]), raw
        )
    except Conflict:
        await state.clear()  # a conflict needs a fresh view, not a retry with a stale version
        raise
    await session.commit()
    await state.clear()
    if isinstance(event, CallbackQuery):
        await answer(event, tr("saved"))
    await render(
        event,
        state,
        tr("fix.updated", line=food_line(tr, entry)),
        inline([(tr("fix.to_list"), Fd(action="fix")), (tr("nav.home"), Go(s="home"))]),
    )


@router.message(EditSG.kcal, F.text)
async def entry_edit_value(
    message: Message, session: AsyncSession, user: User, tr: Translator, state: FSMContext
) -> None:
    await _apply_edit(message, session, user, tr, state, message.text)


@router.callback_query(EditSG.kcal, Fd.filter(F.action == "edit_unknown"))
async def entry_edit_unknown(
    query: CallbackQuery, session: AsyncSession, user: User, tr: Translator, state: FSMContext
) -> None:
    await _apply_edit(query, session, user, tr, state, None)
