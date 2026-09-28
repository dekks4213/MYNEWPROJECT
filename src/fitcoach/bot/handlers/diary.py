"""Food, weight, today summary, corrections and AI meal drafts."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from fitcoach.ai.gateway import AIGateway
from fitcoach.ai.types import MealDraft
from fitcoach.bot.ui import Dr, En, Fd, Fo, cancel_kb, food_line, format_summary, inline, main_menu
from fitcoach.bot.ui import session_line as fmt_session
from fitcoach.db.models import FoodEntry, User
from fitcoach.domain.nutrition import Precision, parse_kcal, parse_macros
from fitcoach.domain.units import ParseError, format_decimal
from fitcoach.i18n import Translator, all_labels
from fitcoach.services.diary import DiaryService, EntryKind
from fitcoach.services.drafts import DraftService
from fitcoach.services.errors import Conflict, ServiceError
from fitcoach.services.summary import build_day_summary

router = Router(name="diary")


class FoodSG(StatesGroup):
    name = State()
    kcal = State()
    macros = State()
    precision = State()
    ai_text = State()


class WeightSG(StatesGroup):
    value = State()


class EditSG(StatesGroup):
    kcal = State()


def _parse(fn: Any, raw: str) -> Any:
    try:
        return fn(raw)
    except ParseError as exc:
        raise ServiceError(exc.code) from exc


def _dec(value: str | None) -> Decimal | None:
    return None if value is None else Decimal(value)


def _undo_kb(tr: Translator, kind: str, entry_id: int) -> Any:
    return inline([(tr("btn.undo"), En(action="del", kind=kind, id=entry_id))])


@router.message(Command("cancel"))
async def cancel_cmd(message: Message, tr: Translator, state: FSMContext) -> None:
    await state.clear()
    await message.answer(tr("cancelled"), reply_markup=main_menu(tr))


@router.callback_query(Fd.filter(F.action == "cancel"))
async def cancel_cb(query: CallbackQuery, tr: Translator, state: FSMContext) -> None:
    await state.clear()
    await query.answer()
    assert isinstance(query.message, Message)
    await query.message.answer(tr("cancelled"), reply_markup=main_menu(tr))


# --- today -----------------------------------------------------------------


@router.message(Command("today"))
@router.message(F.text.in_(all_labels("menu.today")))
async def today(message: Message, session: AsyncSession, user: User, tr: Translator) -> None:
    summary = await build_day_summary(session, user)
    await message.answer(format_summary(tr, summary), reply_markup=main_menu(tr))


# --- food ------------------------------------------------------------------


@router.message(F.text.in_(all_labels("menu.food")))
async def food_start(
    message: Message, user: User, tr: Translator, gateway: AIGateway, state: FSMContext
) -> None:
    await state.clear()
    await state.set_state(FoodSG.name)
    rows: list[list[tuple[str, Any]]] = []
    if gateway.enabled and user.ai_text_consent_at is not None:
        rows.append([(tr("food.ai_button"), Fo(action="ai"))])
    rows.append([(tr("btn.cancel"), Fd(action="cancel"))])
    await message.answer(tr("food.ask_name"), reply_markup=inline(*rows))


@router.message(FoodSG.name, F.text)
async def food_name(
    message: Message, session: AsyncSession, user: User, tr: Translator, state: FSMContext
) -> None:
    assert message.text is not None
    text = message.text.strip()
    if ";" in text:  # one-line form: name; kcal; P/F/C
        parts = [p.strip() for p in text.split(";")]
        if len(parts) > 3 or not parts[0]:
            raise ServiceError("bad_one_line")
        kcal = None if len(parts) < 2 or parts[1] in ("", "?") else _parse(parse_kcal, parts[1])
        macros = (None, None, None)
        if len(parts) == 3 and parts[2]:
            macros = _parse(parse_macros, parts[2])
        await state.update_data(
            name=parts[0],
            kcal=None if kcal is None else str(kcal),
            macros=[None if m is None else str(m) for m in macros],
        )
        await _ask_precision_or_save(message, session, user, tr, state)
        return
    if len(text) > 120:
        raise ServiceError("bad_name")
    await state.update_data(name=text)
    await state.set_state(FoodSG.kcal)
    await message.answer(
        tr("food.ask_kcal"),
        reply_markup=inline(
            [(tr("food.kcal_unknown"), Fo(action="kcal_unknown"))],
            [(tr("btn.cancel"), Fd(action="cancel"))],
        ),
    )


@router.message(FoodSG.kcal, F.text)
async def food_kcal(message: Message, tr: Translator, state: FSMContext) -> None:
    assert message.text is not None
    kcal = _parse(parse_kcal, message.text)
    await state.update_data(kcal=str(kcal))
    await _ask_macros(message, tr, state)


@router.callback_query(FoodSG.kcal, Fo.filter(F.action == "kcal_unknown"))
async def food_kcal_unknown(query: CallbackQuery, tr: Translator, state: FSMContext) -> None:
    await query.answer()
    await state.update_data(kcal=None)
    assert isinstance(query.message, Message)
    await _ask_macros(query.message, tr, state)


async def _ask_macros(message: Message, tr: Translator, state: FSMContext) -> None:
    await state.set_state(FoodSG.macros)
    await message.answer(
        tr("food.ask_macros"),
        reply_markup=inline(
            [(tr("btn.skip"), Fo(action="macros_skip"))],
            [(tr("btn.cancel"), Fd(action="cancel"))],
        ),
    )


@router.message(FoodSG.macros, F.text)
async def food_macros(
    message: Message, session: AsyncSession, user: User, tr: Translator, state: FSMContext
) -> None:
    assert message.text is not None
    macros = _parse(parse_macros, message.text)
    await state.update_data(macros=[None if m is None else str(m) for m in macros])
    await _ask_precision_or_save(message, session, user, tr, state)


@router.callback_query(FoodSG.macros, Fo.filter(F.action == "macros_skip"))
async def food_macros_skip(
    query: CallbackQuery, session: AsyncSession, user: User, tr: Translator, state: FSMContext
) -> None:
    await query.answer()
    await state.update_data(macros=[None, None, None])
    assert isinstance(query.message, Message)
    await _ask_precision_or_save(query.message, session, user, tr, state)


async def _ask_precision_or_save(
    message: Message, session: AsyncSession, user: User, tr: Translator, state: FSMContext
) -> None:
    data = await state.get_data()
    if data.get("kcal") is None and all(m is None for m in data.get("macros", [])):
        await _save_food(message, session, user, tr, state, Precision.UNKNOWN)
        return
    await state.set_state(FoodSG.precision)
    await message.answer(
        tr("food.ask_precision"),
        reply_markup=inline(
            [(tr("precision.measured"), Fo(action="prec", value="measured"))],
            [(tr("precision.approximate"), Fo(action="prec", value="approximate"))],
        ),
    )


@router.callback_query(FoodSG.precision, Fo.filter(F.action == "prec"))
async def food_precision(
    query: CallbackQuery,
    callback_data: Fo,
    session: AsyncSession,
    user: User,
    tr: Translator,
    state: FSMContext,
) -> None:
    await query.answer()
    assert isinstance(query.message, Message)
    await _save_food(query.message, session, user, tr, state, Precision(callback_data.value))


async def _save_food(
    message: Message,
    session: AsyncSession,
    user: User,
    tr: Translator,
    state: FSMContext,
    precision: Precision,
) -> None:
    data = await state.get_data()
    p, f, c = data.get("macros") or [None, None, None]
    entry = await DiaryService(session, user).add_food(
        data["name"],
        energy_kcal=_dec(data.get("kcal")),
        protein_g=_dec(p),
        fat_g=_dec(f),
        carbs_g=_dec(c),
        precision=precision,
    )
    await session.commit()
    await state.clear()
    await message.answer(
        tr("food.saved", line=food_line(tr, entry)), reply_markup=_undo_kb(tr, "food", entry.id)
    )
    await message.answer(tr("food.next"), reply_markup=main_menu(tr))


# --- AI meal drafts ----------------------------------------------------------


@router.callback_query(FoodSG.name, Fo.filter(F.action == "ai"))
async def food_ai_start(query: CallbackQuery, tr: Translator, state: FSMContext) -> None:
    await query.answer()
    await state.set_state(FoodSG.ai_text)
    assert isinstance(query.message, Message)
    await query.message.answer(tr("food.ai_ask"), reply_markup=cancel_kb(tr))


@router.message(FoodSG.ai_text, F.text)
async def food_ai_text(
    message: Message,
    session: AsyncSession,
    user: User,
    tr: Translator,
    gateway: AIGateway,
    state: FSMContext,
) -> None:
    assert message.text is not None
    await state.clear()
    meal = await gateway.parse_meal(session, user, message.text)
    draft = await DraftService(session, user).create_meal_draft(meal)
    await session.commit()
    await message.answer(
        _draft_text(tr, meal, gateway.is_mock),
        reply_markup=inline(
            [
                (tr("draft.confirm"), Dr(action="ok", id=draft.id)),
                (tr("draft.cancel"), Dr(action="no", id=draft.id)),
            ]
        ),
    )


def _draft_text(tr: Translator, meal: MealDraft, is_mock: bool) -> str:
    lines = [tr("draft.title")]
    for item in meal.items:
        qty = f" ({item.quantity_text})" if item.quantity_text else ""
        kcal = (
            f"{format_decimal(item.energy_kcal)} {tr('unit.kcal')}"
            if item.energy_kcal is not None
            else tr("summary.kcal_unknown")
        )
        lines.append(f"• {item.name}{qty} — {kcal}")
    lines.append(tr("draft.note"))
    if is_mock:
        lines.append(tr("draft.mock"))
    return "\n".join(lines)


@router.callback_query(Dr.filter())
async def draft_resolve(
    query: CallbackQuery, callback_data: Dr, session: AsyncSession, user: User, tr: Translator
) -> None:
    drafts = DraftService(session, user)
    assert isinstance(query.message, Message)
    if callback_data.action == "ok":
        entries = await drafts.confirm_meal(callback_data.id)
        await session.commit()
        await query.answer(tr("saved"))
        await query.message.answer(tr("draft.saved", n=len(entries)), reply_markup=main_menu(tr))
    else:
        await drafts.cancel(callback_data.id)
        await session.commit()
        await query.answer(tr("cancelled"))


# --- weight ------------------------------------------------------------------


@router.message(F.text.in_(all_labels("menu.weight")))
async def weight_start(message: Message, tr: Translator, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(WeightSG.value)
    await message.answer(tr("weight.ask"), reply_markup=cancel_kb(tr))


@router.message(WeightSG.value, F.text)
async def weight_value(
    message: Message, session: AsyncSession, user: User, tr: Translator, state: FSMContext
) -> None:
    assert message.text is not None
    entry = await DiaryService(session, user).add_weight(message.text)
    await session.commit()
    await state.clear()
    await message.answer(
        tr("weight.saved", kg=format_decimal(entry.weight_kg, 2)),
        reply_markup=_undo_kb(tr, "weight", entry.id),
    )


# --- corrections -------------------------------------------------------------


@router.message(F.text.in_(all_labels("menu.fix")))
async def fix_list(
    message: Message, session: AsyncSession, user: User, tr: Translator, state: FSMContext
) -> None:
    await state.clear()
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
        lines.append(f"{n}. ⚖️ {format_decimal(w.weight_kg, 2)} {tr('unit.kg')}")
        rows.append([(f"🗑 {n}", En(action="del", kind="weight", id=w.id))])
    for s in sessions:
        n += 1
        lines.append(f"{n}. 🏃 {fmt_session(tr, s)}")
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
    assert isinstance(query.message, Message)
    if callback_data.action == "del":
        await diary.delete(kind, callback_data.id)
        await session.commit()
        await query.answer(tr("fix.deleted"))
        await query.message.answer(
            tr("fix.deleted"),
            reply_markup=inline(
                [(tr("btn.restore"), En(action="restore", kind=kind, id=callback_data.id))]
            ),
        )
    else:
        await diary.restore(kind, callback_data.id)
        await session.commit()
        await query.answer(tr("fix.restored"))
        await query.message.answer(tr("fix.restored"))


@router.callback_query(En.filter(F.action == "edit"))
async def entry_edit(
    query: CallbackQuery, callback_data: En, tr: Translator, state: FSMContext
) -> None:
    await query.answer()
    await state.set_state(EditSG.kcal)
    await state.update_data(entry_id=callback_data.id, version=callback_data.v)
    assert isinstance(query.message, Message)
    await query.message.answer(
        tr("fix.ask_kcal"),
        reply_markup=inline(
            [(tr("food.kcal_unknown"), Fo(action="edit_unknown"))],
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


@router.callback_query(EditSG.kcal, Fo.filter(F.action == "edit_unknown"))
async def entry_edit_unknown(
    query: CallbackQuery, session: AsyncSession, user: User, tr: Translator, state: FSMContext
) -> None:
    await query.answer()
    assert isinstance(query.message, Message)
    await _apply_edit(query.message, session, user, tr, state, None)
