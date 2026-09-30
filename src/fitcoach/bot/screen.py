"""One message = one screen.

A button press edits the message it belongs to. A typed answer cannot be edited, so the
next screen is sent as a new message and the previous screen loses its buttons, which keeps
exactly one live screen in the chat. The live screen's message id is kept in its own FSM
bucket (destiny "screen"), so clearing a flow never loses it.
"""

from __future__ import annotations

import dataclasses
import logging
from collections import OrderedDict

from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

log = logging.getLogger(__name__)
_SCREEN = "screen"


def _screen_ctx(state: FSMContext) -> FSMContext:
    return FSMContext(storage=state.storage, key=dataclasses.replace(state.key, destiny=_SCREEN))


async def _remember(state: FSMContext, message: Message | bool) -> None:
    if isinstance(message, Message):
        await _screen_ctx(state).set_data({"id": message.message_id})


async def _retire_previous(message: Message, state: FSMContext) -> None:
    """Remove buttons from the previous screen so only one screen is live."""
    data = await _screen_ctx(state).get_data()
    old = data.get("id")
    if not isinstance(old, int) or old == message.message_id:
        return
    try:
        await message.bot.edit_message_reply_markup(  # type: ignore[union-attr]
            chat_id=message.chat.id, message_id=old, reply_markup=None
        )
    except TelegramBadRequest:
        pass  # already gone, too old or unchanged: nothing to retire


async def render(
    event: Message | CallbackQuery,
    state: FSMContext,
    text: str,
    kb: InlineKeyboardMarkup | None = None,
    *,
    fresh: bool = False,
) -> None:
    """Show a screen: edit in place after a button, send after typed input.

    `fresh` always sends a new message (used when the screen must be the last one)."""
    if isinstance(event, CallbackQuery):
        target = event.message
        if not isinstance(target, Message):
            return
        await _retire_previous(target, state)
        if not fresh and target.text is not None:
            try:
                edited = await target.edit_text(text, reply_markup=kb)
            except TelegramBadRequest as exc:
                if "not modified" in str(exc):
                    return
            else:
                await _remember(state, edited if isinstance(edited, Message) else target)
                return
        if fresh:
            await _strip(target)
        sent = await target.answer(text, reply_markup=kb)
    else:
        await _retire_previous(event, state)
        sent = await event.answer(text, reply_markup=kb)
    await _remember(state, sent)


async def _strip(message: Message) -> None:
    if message.reply_markup is None:
        return
    try:
        await message.edit_reply_markup(reply_markup=None)
    except TelegramBadRequest:
        pass


async def progress(event: Message | CallbackQuery, state: FSMContext, text: str) -> Message | None:
    """Show a 'working…' screen for slow steps; the result later replaces it via `replace`."""
    if isinstance(event, CallbackQuery):
        target = event.message
        if isinstance(target, Message) and target.text is not None:
            try:
                await target.edit_text(text)
                return target
            except TelegramBadRequest:
                pass
        return None
    await _retire_previous(event, state)
    sent = await event.answer(text)
    await _remember(state, sent)
    return sent


async def replace(
    holder: Message | None,
    event: Message | CallbackQuery,
    state: FSMContext,
    text: str,
    kb: InlineKeyboardMarkup | None = None,
) -> None:
    """Put the result into the 'working…' message (or render normally without one)."""
    if holder is not None:
        try:
            edited = await holder.edit_text(text, reply_markup=kb)
            await _remember(state, edited if isinstance(edited, Message) else holder)
            return
        except TelegramBadRequest:
            pass
    await render(event, state, text, kb)


_answered: OrderedDict[str, None] = OrderedDict()
_ANSWERED_MAX = 4096


async def answer(
    event: Message | CallbackQuery, text: str | None = None, *, show_alert: bool = False
) -> None:
    """Answer a button press exactly once (Telegram rejects a second answer).

    Screens can be composed (save -> show list), so later answers are ignored."""
    if not isinstance(event, CallbackQuery) or event.id in _answered:
        return
    _answered[event.id] = None
    while len(_answered) > _ANSWERED_MAX:
        _answered.popitem(last=False)
    await event.answer(text, show_alert=show_alert)


def was_answered(query: CallbackQuery) -> bool:
    return query.id in _answered
