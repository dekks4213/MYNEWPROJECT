"""Drive the real aiogram Dispatcher with synthetic updates and a recording Bot session."""

from __future__ import annotations

import datetime as dt
import itertools
from typing import Any

from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.methods import AnswerCallbackQuery, SendMessage, TelegramMethod
from aiogram.types import CallbackQuery, Chat, InlineKeyboardMarkup, Message, Update, User

_update_ids = itertools.count(1_000_000)


def detach_routers() -> None:
    """Routers attach to one Dispatcher per process; emulate a fresh process in tests."""
    from fitcoach.bot.app import fallback
    from fitcoach.bot.handlers import activities, diary, onboarding

    for router in (onboarding.router, diary.router, activities.router, fallback):
        router._parent_router = None


_message_ids = itertools.count(1)


class RecordingSession(BaseSession):
    def __init__(self) -> None:
        super().__init__()
        self.requests: list[TelegramMethod[Any]] = []

    async def make_request(
        self,
        bot: Bot,
        method: TelegramMethod[Any],
        timeout: int | None = None,  # noqa: ASYNC109
    ) -> Any:
        self.requests.append(method)
        if isinstance(method, SendMessage):
            return Message(
                message_id=next(_message_ids),
                date=dt.datetime.now(dt.UTC),
                chat=Chat(id=int(method.chat_id), type="private"),
                text=method.text,
            )
        return True

    async def stream_content(self, *args: Any, **kwargs: Any) -> Any:  # pragma: no cover
        raise NotImplementedError

    async def close(self) -> None:
        pass


class TgUser:
    """A synthetic Telegram user chatting with the bot."""

    def __init__(self, telegram_id: int, dp: Dispatcher, bot: Bot) -> None:
        self.id = telegram_id
        self.dp = dp
        self.bot = bot
        self._user = User(id=telegram_id, is_bot=False, first_name="Test")
        self._chat = Chat(id=telegram_id, type="private")

    @property
    def session(self) -> RecordingSession:
        session = self.bot.session
        assert isinstance(session, RecordingSession)
        return session

    def _message(self, text: str | None) -> Message:
        return Message(
            message_id=next(_message_ids),
            date=dt.datetime.now(dt.UTC),
            chat=self._chat,
            from_user=self._user,
            text=text,
        )

    async def _feed(self, update: Update) -> list[str]:
        start = len(self.session.requests)
        await self.dp.feed_update(self.bot, update)
        return [_text(r) for r in self.session.requests[start:] if _text(r)]

    async def send(self, text: str, update_id: int | None = None) -> list[str]:
        uid = update_id if update_id is not None else next(_update_ids)
        return await self._feed(Update(update_id=uid, message=self._message(text)))

    async def press(self, data: str) -> list[str]:
        query = CallbackQuery(
            id=str(next(_update_ids)),
            from_user=self._user,
            chat_instance="test",
            data=data,
            message=self._message("button host"),
        )
        return await self._feed(Update(update_id=next(_update_ids), callback_query=query))

    def button(self, label: str) -> str:
        """Callback data of the most recent inline button whose label contains `label`."""
        for request in reversed(self.session.requests):
            markup = getattr(request, "reply_markup", None)
            if isinstance(request, SendMessage) and int(request.chat_id) != self.id:
                continue
            if isinstance(markup, InlineKeyboardMarkup):
                for row in markup.inline_keyboard:
                    for button in row:
                        if label in button.text and button.callback_data:
                            return button.callback_data
        raise AssertionError(f"no button containing {label!r}")

    async def tap(self, label: str) -> list[str]:
        return await self.press(self.button(label))


def _text(method: TelegramMethod[Any]) -> str:
    if isinstance(method, SendMessage):
        return method.text
    if isinstance(method, AnswerCallbackQuery):
        return method.text or ""
    return ""
