"""Small helpers shared by handler modules."""

from __future__ import annotations

from aiogram.types import CallbackQuery, Message

Event = Message | CallbackQuery


def msg(query: CallbackQuery) -> Message:
    assert isinstance(query.message, Message)
    return query.message


def first_name(event: Event) -> str | None:
    """The name Telegram shows for the user; never stored."""
    user = event.from_user
    name = (user.first_name or "").strip() if user else ""
    return name[:40] or None
