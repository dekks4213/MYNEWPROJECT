""" "Мой день": dashboard, weight and corrections."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from fitcoach.bot.handlers.common import msg
from fitcoach.bot.ui import (
    Ac,
    En,
    Fd,
    Fm,
    cancel_kb,
    food_line,
    format_day,
    inline,
    main_menu,
    num,
    session_line,
)
from fitcoach.db.models import FoodEntry, User
from fitcoach.i18n import Translator, all_labels
from fitcoach.services.diary import DiaryService, EntryKind
from fitcoach.services.errors import Conflict, ServiceError
from fitcoach.services.summary import build_day_summary

router = Router(name="day")


class WeightSG(StatesGroup):
    value = State()


class EditSG(StatesGroup):
    kcal = State()


def day_kb(tr: Translator) -> Any:
    return inline(
        [(tr("day.btn_food"), Fm(a="menu")), (tr("day.btn_weight"), Fd(action="weight"))],
        [(tr("day.btn_training"), Ac(action="menu")), (tr("day.btn_fix"), Fd(action="fix"))],
    )


async def send_day(message: Message, session: AsyncSession, user: User, tr: Translator) -> None:
    summary = await build_day_summary(session, user)
    text = format_day(
        tr, summary, protein=user.protein_target_g, fat=user.fat_target_g, carbs=user.carbs_target_g
    )
    await message.answer(text, reply_markup=day_kb(tr))


@router.message(Command("today"))
@router.message(F.text.in_(all_labels("menu.day")))
async def my_day(
    message: Message, session: AsyncSession, user: User, tr: Translator, state: FSMContext
) -> None:
    await state.clear()
    await send_day(message, session, user, tr)


# --- weight ------------------------------------------------------------------------------


async def _ask_weight(
    message: Message, session: AsyncSession, user: User, tr: Translator, state: FSMContext
) -> None:
    await state.set_state(WeightSG.value)
    last = await DiaryService(session, user).latest_weight()
    if last is None:
        await message.answer(tr("weight.ask"), reply_markup=cancel_kb(tr))
        return
    base = last.weight_kg
    steps = (Decimal("-0.5"), Decimal("-0.2"), Decimal(0), Decimal("0.2"), Decimal("0.5"))
    buttons = [(num(tr, base + d, 1), Fd(action="wq", value=str(base + d))) for d in steps]
    await message.answer(
        tr("weight.ask_quick", kg=num(tr, base, 1)),
        reply_markup=inline(buttons, [(tr("btn.cancel"), Fd(action="cancel"))]),
    )


@router.message(Command("weight"))
async def weight_cmd(
    message: Message, session: AsyncSession, user: User, tr: Translator, state: FSMContext
) -> None:
    await _ask_weight(message, session, user, tr, state)


@router.callback_query(Fd.filter(F.action == "weight"))
async def weight_start(
    query: CallbackQuery, session: AsyncSession, user: User, tr: Translator, state: FSMContext
) -> None:
    await query.answer()
    await _ask_weight(msg(query), session, user, tr, state)


@router.callback_query(WeightSG.value, Fd.filter(F.action == "wq"))
async def weight_quick(
    query: CallbackQuery,
    callback_data: Fd,
    session: AsyncSession,
    user: User,
    tr: Translator,
    state: FSMContext,
) -> None:
    entry = await DiaryService(session, user).add_weight(callback_data.value[:8])
    await session.commit()
    await state.clear()
    await query.answer(tr("saved"))
    await msg(query).answer(
        tr("weight.saved", kg=num(tr, entry.weight_kg, 2)),
        reply_markup=inline([(tr("btn.undo"), En(action="del", kind="weight", id=entry.id))]),
    )


@router.message(WeightSG.value, F.text)
async def weight_value(
    message: Message, session: AsyncSession, user: User, tr: Translator, state: FSMContext
) -> None:
    assert message.text is not None
    entry = await DiaryService(session, user).add_weight(message.text)
    await session.commit()
    await state.clear()
    await message.answer(
        tr("weight.saved", kg=num(tr, entry.weight_kg, 2)),
        reply_markup=inline([(tr("btn.undo"), En(action="del", kind="weight", id=entry.id))]),
    )


# --- corrections -----------------------------------------------------------------------------


@router.message(Command("fix"))
async def fix_cmd(
    message: Message, session: AsyncSession, user: User, tr: Translator, state: FSMContext
) -> None:
    await state.clear()
    await _fix_list(message, session, user, tr)


@router.callback_query(Fd.filter(F.action == "fix"))
async def fix_cb(
    query: CallbackQuery, session: AsyncSession, user: User, tr: Translator, state: FSMContext
) -> None:
    await query.answer()
    await state.clear()
    await _fix_list(msg(query), session, user, tr)


async def _fix_list(message: Message, session: AsyncSession, user: User, tr: Translator) -> None:
    food, weights, sessions = await DiaryService(session, user).entries_for_day()
    if not (food or weights or sessions):
        await message.answer(tr("fix.empty"))
        return
    lines = [tr("fix.title")]
    rows: list[list[tuple[str, Any]]] = []
    n = 0
    for entry in food:
        n += 1
        lines.append(f"{n}. 🍽 {food_line(tr, entry)}")
        rows.append(
            [
                (f"🗑 {n}", En(action="del", kind="food", id=entry.id)),
                (f"✏️ {n}", En(action="edit", kind="food", id=entry.id, v=entry.version)),
            ]
        )
    for w in weights:
        n += 1
        lines.append(f"{n}. ⚖️ {num(tr, w.weight_kg, 2)} {tr('unit.kg')}")
        rows.append([(f"🗑 {n}", En(action="del", kind="weight", id=w.id))])
    for s in sessions:
        n += 1
        lines.append(f"{n}. 🏋️ {session_line(tr, s)}")
        rows.append([(f"🗑 {n}", En(action="del", kind="session", id=s.id))])
    await message.answer("\n".join(lines), reply_markup=inline(*rows))


@router.callback_query(En.filter(F.action.in_({"del", "restore"})))
async def entry_delete_restore(
    query: CallbackQuery, callback_data: En, session: AsyncSession, user: User, tr: Translator
) -> None:
    if callback_data.kind not in ("food", "weight", "session"):
        raise ServiceError("not_found")
    kind: EntryKind = callback_data.kind  # type: ignore[assignment]
    diary = DiaryService(session, user)
    if callback_data.action == "del":
        await diary.delete(kind, callback_data.id)
        await session.commit()
        await query.answer(tr("fix.deleted"))
        await msg(query).answer(
            tr("fix.deleted"),
            reply_markup=inline(
                [(tr("btn.restore"), En(action="restore", kind=kind, id=callback_data.id))]
            ),
        )
    else:
        await diary.restore(kind, callback_data.id)
        await session.commit()
        await query.answer(tr("fix.restored"))
        await msg(query).answer(tr("fix.restored"))


@router.callback_query(En.filter(F.action == "edit"))
async def entry_edit(
    query: CallbackQuery, callback_data: En, tr: Translator, state: FSMContext
) -> None:
    await query.answer()
    await state.set_state(EditSG.kcal)
    await state.update_data(entry_id=callback_data.id, version=callback_data.v)
    await msg(query).answer(
        tr("fix.ask_kcal"),
        reply_markup=inline(
            [(tr("food.kcal_unknown_btn"), Fd(action="edit_unknown"))],
            [(tr("btn.cancel"), Fd(action="cancel"))],
        ),
    )


async def _apply_edit(
    message: Message,
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
    await message.answer(tr("fix.updated", line=food_line(tr, entry)), reply_markup=main_menu(tr))


@router.message(EditSG.kcal, F.text)
async def entry_edit_value(
    message: Message, session: AsyncSession, user: User, tr: Translator, state: FSMContext
) -> None:
    await _apply_edit(message, session, user, tr, state, message.text)


@router.callback_query(EditSG.kcal, Fd.filter(F.action == "edit_unknown"))
async def entry_edit_unknown(
    query: CallbackQuery, session: AsyncSession, user: User, tr: Translator, state: FSMContext
) -> None:
    await query.answer()
    await _apply_edit(msg(query), session, user, tr, state, None)
