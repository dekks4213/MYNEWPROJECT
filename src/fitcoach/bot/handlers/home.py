"""🏠 Home card, "⋯ Ещё", help and cancel. Every flow can come back here."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from fitcoach.bot.handlers.common import Event, first_name
from fitcoach.bot.screen import answer, render
from fitcoach.bot.ui import Fd, Go, format_home, home_kb, inline, nav
from fitcoach.db.models import User
from fitcoach.i18n import Translator, all_labels
from fitcoach.services.summary import build_day_summary
from fitcoach.services.users import user_zone, utcnow

router = Router(name="home")


async def show_home(
    event: Event,
    session: AsyncSession,
    user: User,
    tr: Translator,
    state: FSMContext,
    *,
    note: str | None = None,
    fresh: bool = False,
) -> None:
    await state.clear()
    summary = await build_day_summary(session, user)
    hour = utcnow().astimezone(user_zone(user)).hour
    text = format_home(
        tr, summary, name=first_name(event), hour=hour, protein_target=user.protein_target_g
    )
    if note:
        text = f"{note}\n\n{text}"
    await answer(event)
    await render(event, state, text, home_kb(tr), fresh=fresh)


@router.message(Command("menu", "home"))
@router.message(F.text.in_(all_labels("menu.home")))
async def home_cmd(
    message: Message, session: AsyncSession, user: User, tr: Translator, state: FSMContext
) -> None:
    await show_home(message, session, user, tr, state)


@router.callback_query(Go.filter(F.s == "home"))
async def home_cb(
    query: CallbackQuery, session: AsyncSession, user: User, tr: Translator, state: FSMContext
) -> None:
    await show_home(query, session, user, tr, state)


@router.message(Command("cancel"))
async def cancel_cmd(
    message: Message, session: AsyncSession, user: User, tr: Translator, state: FSMContext
) -> None:
    await show_home(message, session, user, tr, state, note=tr("cancelled"))


@router.callback_query(Fd.filter(F.action == "cancel"))
async def cancel_cb(
    query: CallbackQuery, session: AsyncSession, user: User, tr: Translator, state: FSMContext
) -> None:
    await show_home(query, session, user, tr, state, note=tr("cancelled"))


@router.callback_query(Go.filter(F.s == "more"))
async def more(query: CallbackQuery, tr: Translator, state: FSMContext) -> None:
    await state.clear()
    await answer(query)
    await render(
        query,
        state,
        tr("more.title"),
        inline(
            [(tr("more.history"), Go(s="hist")), (tr("more.fix"), Fd(action="fix"))],
            [(tr("more.settings"), Go(s="set")), (tr("more.help"), Go(s="help"))],
            nav(tr),
        ),
    )


@router.message(Command("help"))
async def help_cmd(message: Message, tr: Translator, state: FSMContext) -> None:
    await render(message, state, tr("help.text"), inline(nav(tr)))


@router.callback_query(Go.filter(F.s == "help"))
async def help_cb(query: CallbackQuery, tr: Translator, state: FSMContext) -> None:
    await answer(query)
    await render(query, state, tr("help.text"), inline(nav(tr, Go(s="more"))))
