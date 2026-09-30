"""Short, progressive onboarding: hello -> goal -> city -> calorie target -> home.

Progress lives in the DB (users.onboarding_step), so it survives restarts. The language is
taken from Telegram and can be switched on the first screen; AI consent and everything else
is asked later, when a feature needs it.
"""

from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from fitcoach.ai.gateway import AIGateway
from fitcoach.bot.handlers.common import Event
from fitcoach.bot.handlers.home import show_home
from fitcoach.bot.screen import answer, render
from fitcoach.bot.ui import (
    KCAL_PRESETS,
    TIMEZONES,
    TZ_SHORTLIST,
    Ob,
    Row,
    grid,
    inline,
    main_menu,
    num,
)
from fitcoach.db.models import User
from fitcoach.i18n import Translator
from fitcoach.services.reminders import ReminderService
from fitcoach.services.users import GOALS, UserService

router = Router(name="onboarding")
RU_FAMILY = ("ru", "uk", "be", "kk", "ky", "uz", "tg", "hy", "az")


def not_onboarded(user: User) -> bool:
    return user.onboarding_step != "done"


def tz_label(tr: Translator, zone: str | None) -> str:
    for iana, key in TIMEZONES:
        if iana == zone:
            return tr(key)
    return zone or tr("profile.not_set")


def tz_rows(tr: Translator, mode: str, *, full: bool) -> list[Row]:
    if full:
        return grid([(tr(key), Ob(mode=mode, action="tz", value=zone)) for zone, key in TIMEZONES])
    short = TZ_SHORTLIST.get(tr.language, TZ_SHORTLIST["en"])
    buttons = [(tz_label(tr, zone), Ob(mode=mode, action="tz", value=zone)) for zone in short]
    buttons.append((tr("tz.other"), Ob(mode=mode, action="tzall")))
    return grid(buttons)


def _step_view(tr: Translator, step: str, *, full_tz: bool = False) -> tuple[str, list[Row]]:
    if step in ("language", "age"):
        other = "en" if tr.language == "ru" else "ru"
        return tr("ob.hello"), [
            [(tr("ob.start"), Ob(mode="ob", action="age", value="yes"))],
            [
                (tr("ob.switch_language"), Ob(mode="ob", action="lang", value=other)),
                (tr("ob.age_no"), Ob(mode="ob", action="age", value="no")),
            ],
        ]
    if step == "goal":
        return tr("ob.goal"), [
            [(tr(f"goal.{g}"), Ob(mode="ob", action="goal", value=g))] for g in GOALS
        ]
    if step == "timezone":
        return tr("ob.timezone"), tz_rows(tr, "ob", full=full_tz)
    if step == "target":
        presets: Row = [
            (num(tr, int(k)), Ob(mode="ob", action="target", value=k)) for k in KCAL_PRESETS
        ]
        return tr("ob.target"), [
            presets[:2],
            presets[2:],
            [(tr("ob.target_skip"), Ob(mode="ob", action="target", value=""))],
        ]
    raise ValueError(step)


async def send_step(
    event: Event,
    user: User,
    tr: Translator,
    gateway: AIGateway,
    state: FSMContext,
    *,
    full_tz: bool = False,
) -> None:
    await answer(event)
    text, rows = _step_view(tr, user.onboarding_step, full_tz=full_tz)
    await render(event, state, text, inline(*rows))


async def _finish(
    event: Event, session: AsyncSession, user: User, tr: Translator, state: FSMContext
) -> None:
    """Install the quick-access keyboard once, then show the home card as the last message."""
    await answer(event, tr("saved"))
    target = event.message if isinstance(event, CallbackQuery) else event
    if isinstance(target, Message):
        await render(event, state, tr("ob.done_short"))
        await target.answer(tr("ob.done"), reply_markup=main_menu(tr))
    await show_home(event, session, user, tr, state, fresh=True)


def _language_from(event: Event) -> str:
    code = (event.from_user.language_code or "") if event.from_user else ""
    return "ru" if code.split("-")[0].lower() in RU_FAMILY or not code else "en"


@router.message(CommandStart())
async def start(
    message: Message,
    session: AsyncSession,
    user: User,
    tr: Translator,
    gateway: AIGateway,
    state: FSMContext,
) -> None:
    await state.clear()
    svc = UserService(session, user)
    await svc.normalize_onboarding()
    if user.onboarding_step == "done":
        await session.commit()
        await message.answer(tr("start.back"), reply_markup=main_menu(tr))
        await show_home(message, session, user, tr, state, fresh=True)
        return
    if user.onboarding_step == "language":
        await svc.set_language(_language_from(message))
    await session.commit()
    await send_step(message, user, Translator(user.language), gateway, state)


@router.callback_query(Ob.filter(F.mode == "ob"))
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
    await svc.normalize_onboarding()
    action, value = callback_data.action, callback_data.value
    if not not_onboarded(user):
        await answer(query, tr("stale_button"))
        return
    if action == "lang":
        await svc.set_language(value)
    elif action == "age":
        if value != "yes":
            await answer(query)
            await render(
                query,
                state,
                tr("ob.adults_only"),
                inline([(tr("ob.back_to_start"), Ob(mode="ob", action="restart"))]),
            )
            return
        if user.onboarding_step == "language":
            await svc.set_language(tr.language)
        await svc.confirm_adult()
    elif action == "goal":
        await svc.set_goal(value or None)
    elif action == "tzall":
        await send_step(query, user, tr, gateway, state, full_tz=True)
        return
    elif action == "tz":
        await svc.set_timezone(value)
        await ReminderService(session, user).reschedule_all()
    elif action == "target":
        await svc.set_kcal_target(value if value in KCAL_PRESETS else None)
    await session.commit()  # confirm only after the database accepted the change
    tr = Translator(user.language)
    if user.onboarding_step == "done":
        await _finish(query, session, user, tr, state)
        return
    await send_step(query, user, tr, gateway, state)


@router.message(F.text, lambda m, user: not_onboarded(user))
async def onboarding_text(
    message: Message,
    session: AsyncSession,
    user: User,
    tr: Translator,
    gateway: AIGateway,
    state: FSMContext,
) -> None:
    """Typed answers during onboarding; anything else shows the current step again."""
    svc = UserService(session, user)
    await svc.normalize_onboarding()
    assert message.text is not None
    if user.onboarding_step == "timezone":
        await svc.set_timezone(message.text)
    elif user.onboarding_step == "target":
        await svc.set_kcal_target(message.text)
    else:
        await session.commit()
        await send_step(message, user, tr, gateway, state)
        return
    await session.commit()
    if user.onboarding_step == "done":
        await _finish(message, session, user, tr, state)
        return
    await send_step(message, user, tr, gateway, state)


@router.message(lambda m, user: not_onboarded(user))
async def onboarding_other(
    message: Message, user: User, tr: Translator, gateway: AIGateway, state: FSMContext
) -> None:
    await send_step(message, user, tr, gateway, state)


@router.callback_query(lambda q, user: not_onboarded(user))
async def onboarding_stray_callback(
    query: CallbackQuery, user: User, tr: Translator, gateway: AIGateway, state: FSMContext
) -> None:
    """Old buttons (or 'back to start') during onboarding lead to the current step."""
    await answer(query, tr("ob.finish_first"))
    await send_step(query, user, tr, gateway, state)
