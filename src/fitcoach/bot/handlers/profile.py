""" "Профиль": goal and manual nutrition targets."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from fitcoach.bot.handlers.common import msg
from fitcoach.bot.ui import KCAL_PRESETS, Fd, Ob, St, inline, main_menu, num
from fitcoach.db.models import User
from fitcoach.i18n import Translator, all_labels
from fitcoach.services.diary import DiaryService
from fitcoach.services.users import UserService

router = Router(name="profile")


class ProfileSG(StatesGroup):
    targets = State()


def _target(tr: Translator, value: object, unit: str) -> str:
    return f"{num(tr, value)} {tr(unit)}" if value is not None else "—"  # type: ignore[arg-type]


@router.message(F.text.in_(all_labels("menu.profile")))
async def profile(
    message: Message, session: AsyncSession, user: User, tr: Translator, state: FSMContext
) -> None:
    await state.clear()
    latest = await DiaryService(session, user).latest_weight()
    goal = tr("goal." + user.goal) if user.goal else "—"
    text = tr(
        "profile.view",
        goal=goal,
        kcal=_target(tr, user.daily_kcal_target, "unit.kcal"),
        p=_target(tr, user.protein_target_g, "unit.g"),
        f=_target(tr, user.fat_target_g, "unit.g"),
        c=_target(tr, user.carbs_target_g, "unit.g"),
        weight=(
            f"{num(tr, latest.weight_kg, 1)} {tr('unit.kg')} "
            f"({latest.local_date.strftime('%d.%m')})"
        )
        if latest
        else "—",
        tz=user.timezone or "—",
    )
    await message.answer(
        text,
        reply_markup=inline(
            [
                (tr("profile.targets"), St(a="targets")),
                (tr("profile.goal"), Ob(mode="set", action="open", value="goal")),
            ],
            [(tr("day.btn_weight"), Fd(action="weight"))],
        ),
    )


@router.callback_query(St.filter(F.a == "targets"))
async def targets_start(query: CallbackQuery, tr: Translator, state: FSMContext) -> None:
    await query.answer()
    await state.set_state(ProfileSG.targets)
    await msg(query).answer(
        tr("profile.targets_ask"),
        reply_markup=inline(
            [(k, St(a="kcal", x=k)) for k in KCAL_PRESETS[:3]],
            [(k, St(a="kcal", x=k)) for k in KCAL_PRESETS[3:]],
            [(tr("profile.targets_clear"), St(a="targets_clear"))],
            [(tr("btn.cancel"), Fd(action="cancel"))],
        ),
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
        await query.answer(tr("stale_button"))
        return
    await UserService(session, user).set_kcal_target(callback_data.x)
    await session.commit()
    await state.clear()
    await query.answer(tr("settings.saved"))
    await msg(query).answer(
        tr("profile.kcal_saved", kcal=callback_data.x), reply_markup=main_menu(tr)
    )


@router.message(ProfileSG.targets, F.text)
async def targets_value(
    message: Message, session: AsyncSession, user: User, tr: Translator, state: FSMContext
) -> None:
    assert message.text is not None
    await UserService(session, user).set_targets(message.text)
    await session.commit()
    await state.clear()
    await message.answer(tr("settings.saved"), reply_markup=main_menu(tr))


@router.callback_query(St.filter(F.a == "targets_clear"))
async def targets_clear(
    query: CallbackQuery, session: AsyncSession, user: User, tr: Translator, state: FSMContext
) -> None:
    await UserService(session, user).set_targets(None)
    await session.commit()
    await state.clear()
    await query.answer(tr("settings.saved"))
