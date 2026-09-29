"""Resumable onboarding (state kept in the DB) and settings."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from fitcoach.ai.gateway import AIGateway
from fitcoach.bot.ui import KCAL_PRESETS, TIMEZONES, Ob, column, inline, main_menu
from fitcoach.db.models import User
from fitcoach.i18n import Translator
from fitcoach.services.reminders import ReminderService
from fitcoach.services.users import GOALS, UserService

router = Router(name="onboarding")


class SettingsSG(StatesGroup):
    timezone = State()
    target = State()


def not_onboarded(user: User) -> bool:
    return user.onboarding_step != "done"


def step_view(tr: Translator, step: str, mode: str, gateway: AIGateway):  # type: ignore[no-untyped-def]
    """Question text and keyboard for an onboarding step (also reused by settings)."""
    if step == "language":
        return tr("ob.language"), inline(
            [
                ("Русский", Ob(mode=mode, action="lang", value="ru")),
                ("English", Ob(mode=mode, action="lang", value="en")),
            ]
        )
    if step == "age":
        return tr("ob.age"), inline(
            [
                (tr("ob.age_yes"), Ob(mode=mode, action="age", value="yes")),
                (tr("ob.age_no"), Ob(mode=mode, action="age", value="no")),
            ]
        )
    if step == "privacy":
        status = tr("ob.ai_status_on") if gateway.enabled else tr("ob.ai_status_off")
        if gateway.is_mock:
            status = tr("ob.ai_status_mock")
        return tr("ob.privacy", status=status), inline(
            [(tr("ob.ai_allow_all"), Ob(mode=mode, action="ai", value="all"))],
            [(tr("ob.ai_allow_text"), Ob(mode=mode, action="ai", value="text"))],
            [(tr("ob.ai_deny"), Ob(mode=mode, action="ai", value="no"))],
        )
    if step == "timezone":
        buttons = [(tr(key), Ob(mode=mode, action="tz", value=zone)) for zone, key in TIMEZONES]
        return tr("ob.timezone"), column(buttons, width=2)
    if step == "units":
        return tr("ob.units"), inline(
            [(tr("ob.units_metric"), Ob(mode=mode, action="units", value="metric"))]
        )
    if step == "goal":
        buttons = [(tr(f"goal.{g}"), Ob(mode=mode, action="goal", value=g)) for g in GOALS]
        buttons.append((tr("btn.skip"), Ob(mode=mode, action="goal", value="")))
        return tr("ob.goal"), column(buttons)
    if step == "target":
        presets = [(k, Ob(mode=mode, action="target", value=k)) for k in KCAL_PRESETS]
        return tr("ob.target"), inline(
            presets[:3],
            presets[3:],
            [(tr("ob.target_skip"), Ob(mode=mode, action="target", value=""))],
        )
    raise ValueError(step)


async def send_step(message: Message, user: User, tr: Translator, gateway: AIGateway) -> None:
    if user.onboarding_step == "done":
        await message.answer(tr("ob.done"), reply_markup=main_menu(tr))
        return
    text, kb = step_view(tr, user.onboarding_step, "ob", gateway)
    await message.answer(text, reply_markup=kb)


@router.message(CommandStart())
async def start(
    message: Message, user: User, tr: Translator, gateway: AIGateway, state: FSMContext
) -> None:
    await state.clear()
    if user.onboarding_step == "done":
        await message.answer(tr("start.back"), reply_markup=main_menu(tr))
        return
    if user.onboarding_step == "language":
        await message.answer(tr("start.hello"))
    await send_step(message, user, tr, gateway)


@router.callback_query(Ob.filter())
async def on_choice(
    query: CallbackQuery,
    callback_data: Ob,
    session: AsyncSession,
    user: User,
    tr: Translator,
    gateway: AIGateway,
    state: FSMContext,
) -> None:
    svc = UserService(session, user)
    action, value = callback_data.action, callback_data.value
    onboarding = callback_data.mode == "ob"
    if onboarding and not not_onboarded(user):
        await query.answer()
        return
    if action == "lang":
        await svc.set_language(value)
    elif action == "age":
        if value != "yes":
            await query.answer()
            assert isinstance(query.message, Message)
            await query.message.answer(tr("ob.adults_only"))
            return
        await svc.confirm_adult()
    elif action == "ai":
        await svc.set_ai_consent(value in ("all", "text"))
        await svc.set_media_consent(value == "all")
    elif action == "tz":
        await svc.set_timezone(value)
        await ReminderService(session, user).reschedule_all()
    elif action == "units":
        await svc.set_units(value)
    elif action == "goal":
        await svc.set_goal(value or None)
    elif action == "target":
        await svc.set_kcal_target(value if value in KCAL_PRESETS else None)
    elif action == "open" and not onboarding:
        await _open_setting(query, value, tr, gateway, state)
        return
    await session.commit()  # confirm only after the database accepted the change
    tr = Translator(user.language)
    await query.answer(tr("saved"))
    assert isinstance(query.message, Message)
    if onboarding:
        await send_step(query.message, user, tr, gateway)
    else:
        await query.message.answer(tr("settings.saved"), reply_markup=main_menu(tr))


async def _open_setting(
    query: CallbackQuery, what: str, tr: Translator, gateway: AIGateway, state: FSMContext
) -> None:
    await query.answer()
    assert isinstance(query.message, Message)
    if what == "timezone":
        await state.set_state(SettingsSG.timezone)
    elif what == "target":
        await state.set_state(SettingsSG.target)
    text, kb = step_view(tr, what, "set", gateway)
    await query.message.answer(text, reply_markup=kb)


@router.message(F.text, lambda m, user: not_onboarded(user))
async def onboarding_text(
    message: Message, session: AsyncSession, user: User, tr: Translator, gateway: AIGateway
) -> None:
    """Free-text answers during onboarding; anything else re-asks the current step."""
    svc = UserService(session, user)
    assert message.text is not None
    if user.onboarding_step == "timezone":
        await svc.set_timezone(message.text)
    elif user.onboarding_step == "target":
        await svc.set_kcal_target(message.text)
    else:
        await send_step(message, user, tr, gateway)
        return
    await session.commit()
    await send_step(message, user, tr, gateway)


@router.message(lambda m, user: not_onboarded(user))
async def onboarding_other(
    message: Message, user: User, tr: Translator, gateway: AIGateway
) -> None:
    await send_step(message, user, tr, gateway)


@router.callback_query(lambda q, user: not_onboarded(user))
async def onboarding_stray_callback(query: CallbackQuery, tr: Translator) -> None:
    await query.answer(tr("ob.finish_first"), show_alert=True)
