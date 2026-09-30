"""Text typed outside any flow: ask what it is instead of answering "unknown command".

The text itself stays in FSM data (never in callback data). Nothing is saved until the user
confirms a preview.
"""

from __future__ import annotations

import re

from aiogram import F, Router
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from fitcoach.ai.gateway import AIGateway
from fitcoach.bot.handlers.day import confirm_weight
from fitcoach.bot.handlers.food import draft_from_text
from fitcoach.bot.handlers.training import workout_from_text
from fitcoach.bot.screen import answer, render
from fitcoach.bot.ui import Fd, Row, inline, nav, num
from fitcoach.config import Settings
from fitcoach.db.models import User
from fitcoach.i18n import Translator
from fitcoach.services.diary import parse_weight
from fitcoach.services.errors import ServiceError
from fitcoach.services.food_sources import FoodSource

router = Router(name="guess")
_NUMBER_RE = re.compile(r"^\s*\d{2,3}(?:[.,]\d{1,2})?\s*(?:кг|kg)?\s*$", re.IGNORECASE)
MAX_TEXT = 1500


@router.message(StateFilter(None), F.text, ~F.text.startswith("/"))
async def free_text(message: Message, tr: Translator, state: FSMContext) -> None:
    assert message.text is not None
    text = message.text.strip()[:MAX_TEXT]
    await state.update_data(pending_text=text)
    rows: list[Row] = []
    if _NUMBER_RE.match(text):
        try:
            kg = parse_weight(re.sub(r"[^\d.,]", "", text))
        except ServiceError:
            kg = None
        if kg is not None:
            rows.append(
                [(tr("guess.weight", kg=num(tr, kg, 1)), Fd(action="guess", value="weight"))]
            )
    rows.append(
        [
            (tr("guess.food"), Fd(action="guess", value="food")),
            (tr("guess.workout"), Fd(action="guess", value="workout")),
        ]
    )
    rows.append(nav(tr))
    shown = text if len(text) <= 120 else text[:119] + "…"
    await render(message, state, tr("guess.ask", text=shown), inline(*rows))


@router.callback_query(Fd.filter(F.action == "guess"))
async def guessed(
    query: CallbackQuery,
    callback_data: Fd,
    session: AsyncSession,
    user: User,
    tr: Translator,
    gateway: AIGateway,
    settings: Settings,
    food_sources: list[FoodSource],
    state: FSMContext,
) -> None:
    text = (await state.get_data()).get("pending_text")
    if not isinstance(text, str) or not text:
        await answer(query, tr("guess.expired"), show_alert=True)
        return
    await answer(query)
    if callback_data.value == "food":
        await draft_from_text(
            query, text, session, user, tr, gateway, settings, food_sources, state
        )
    elif callback_data.value == "workout":
        await workout_from_text(query, text, session, user, tr, gateway, state)
    elif callback_data.value == "weight":
        await state.clear()
        await confirm_weight(
            query, session, user, tr, state, parse_weight(re.sub(r"[^\d.,]", "", text))
        )
    else:
        raise ServiceError("bad_choice")
