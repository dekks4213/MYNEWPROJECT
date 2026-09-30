"""⚙️ → 👤 Профиль (goal, weight, setup again) and 🎯 Цели (calorie and macro targets)."""

from __future__ import annotations

from decimal import Decimal

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from fitcoach.ai.gateway import AIGateway
from fitcoach.bot.handlers.common import Event
from fitcoach.bot.handlers.onboarding import send_step, tz_label
from fitcoach.bot.screen import answer, render
from fitcoach.bot.ui import KCAL_PRESETS, Go, Ob, St, inline, nav, num, rel_day
from fitcoach.db.models import User
from fitcoach.i18n import Translator
from fitcoach.services.diary import DiaryService
from fitcoach.services.users import GOALS, UserService, local_today

router = Router(name="profile")


class ProfileSG(StatesGroup):
    targets = State()


def _target(tr: Translator, value: Decimal | None, unit: str) -> str:
    return f"{num(tr, value)} {tr(unit)}" if value is not None else tr("profile.not_set")


async def show_profile(
    event: Event, session: AsyncSession, user: User, tr: Translator, state: FSMContext
) -> None:
    await state.clear()
    latest = await DiaryService(session, user).latest_weight()
    weight = (
        f"{num(tr, latest.weight_kg, 1)} {tr('unit.kg')} · "
        f"{rel_day(tr, latest.local_date, local_today(user))}"
        if latest
        else tr("profile.not_set")
    )
    text = tr(
        "profile.view",
        goal=tr("goal." + user.goal) if user.goal else tr("profile.not_set"),
        weight=weight,
        tz=tz_label(tr, user.timezone),
    )
    await answer(event)
    await render(
        event,
        state,
        text,
        inline(
            [
                (tr("profile.goal"), Ob(mode="set", action="open", value="goal")),
                (tr("home.btn_weight"), Go(s="weight")),
            ],
            [(tr("profile.reset"), St(a="reset"))],
            nav(tr, Go(s="set")),
        ),
    )


@router.callback_query(Go.filter((F.s == "set") & (F.a == "profile")))
async def profile_cb(
    query: CallbackQuery, session: AsyncSession, user: User, tr: Translator, state: FSMContext
) -> None:
    await show_profile(query, session, user, tr, state)


def goal_kb(tr: Translator, mode: str, back: Go | None) -> list[list[tuple[str, Ob | Go]]]:
    rows: list[list[tuple[str, Ob | Go]]] = [
        [(tr(f"goal.{g}"), Ob(mode=mode, action="goal", value=g))] for g in GOALS
    ]
    if back is not None:
        rows.append([(tr("nav.back"), back)])
    return rows


# --- setup again (keeps all records) -----------------------------------------------------------


@router.callback_query(St.filter(F.a == "reset"))
async def reset_ask(query: CallbackQuery, tr: Translator, state: FSMContext) -> None:
    await answer(query)
    await render(
        query,
        state,
        tr("profile.reset_ask"),
        inline(
            [(tr("profile.reset_yes"), St(a="reset_ok"))],
            nav(tr, Go(s="set", a="profile"), home=False),
        ),
    )


@router.callback_query(St.filter(F.a == "reset_ok"))
async def reset_confirm(
    query: CallbackQuery,
    session: AsyncSession,
    user: User,
    tr: Translator,
    gateway: AIGateway,
    state: FSMContext,
) -> None:
    await UserService(session, user).reset_onboarding()
    await session.commit()
    await state.clear()
    await answer(query)
    await send_step(query, user, tr, gateway, state)


# --- targets -----------------------------------------------------------------------------------


async def show_goals(event: Event, user: User, tr: Translator, state: FSMContext) -> None:
    await state.clear()
    text = tr(
        "goals.view",
        kcal=_target(tr, user.daily_kcal_target, "unit.kcal"),
        p=_target(tr, user.protein_target_g, "unit.g"),
        f=_target(tr, user.fat_target_g, "unit.g"),
        c=_target(tr, user.carbs_target_g, "unit.g"),
    )
    await answer(event)
    await render(
        event,
        state,
        text,
        inline(
            [(num(tr, Decimal(k)), St(a="kcal", x=k)) for k in KCAL_PRESETS],
            [(tr("goals.exact"), St(a="targets")), (tr("goals.clear"), St(a="targets_clear"))],
            nav(tr, Go(s="set")),
        ),
    )


@router.callback_query(Go.filter((F.s == "set") & (F.a == "goals")))
async def goals_cb(query: CallbackQuery, user: User, tr: Translator, state: FSMContext) -> None:
    await show_goals(query, user, tr, state)


@router.callback_query(St.filter(F.a == "targets"))
async def targets_start(query: CallbackQuery, tr: Translator, state: FSMContext) -> None:
    await answer(query)
    await state.set_state(ProfileSG.targets)
    await render(
        query, state, tr("goals.exact_ask"), inline(nav(tr, Go(s="set", a="goals"), home=False))
    )


@router.callback_query(St.filter(F.a == "kcal"))
async def kcal_preset(
    query: CallbackQuery,
    callback_data: St,
    session: AsyncSession,
    user: User,
    tr: Translator,
    state: FSMContext,
) -> None:
    if callback_data.x not in KCAL_PRESETS:
        await answer(query, tr("stale_button"))
        return
    await UserService(session, user).set_kcal_target(callback_data.x)
    await session.commit()
    await answer(query, tr("settings.saved"))
    await show_goals(query, user, tr, state)


@router.message(ProfileSG.targets, F.text)
async def targets_value(
    message: Message, session: AsyncSession, user: User, tr: Translator, state: FSMContext
) -> None:
    assert message.text is not None
    await UserService(session, user).set_targets(message.text)
    await session.commit()
    await show_goals(message, user, tr, state)


@router.callback_query(St.filter(F.a == "targets_clear"))
async def targets_clear(
    query: CallbackQuery, session: AsyncSession, user: User, tr: Translator, state: FSMContext
) -> None:
    await UserService(session, user).set_targets(None)
    await session.commit()
    await answer(query, tr("settings.saved"))
    await show_goals(query, user, tr, state)
