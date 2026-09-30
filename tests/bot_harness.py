"""Drive the real aiogram Dispatcher with synthetic updates and a recording Bot session.

The recording session behaves like a small Telegram: it remembers every bot message's text
and live inline keyboard, applies edits, and checks invariants on every request:
  * every callback_data fits 64 bytes and unpacks with one of the bot's factories;
  * each button press is answered exactly once.
`TgUser.tap(label)` presses a button that is visible *now* on one of the bot's messages,
so buttons removed by an edit cannot be pressed, as in a real client.
"""

from __future__ import annotations

import datetime as dt
import itertools
from typing import Any

from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.methods import (
    AnswerCallbackQuery,
    EditMessageReplyMarkup,
    EditMessageText,
    GetFile,
    SendMessage,
    TelegramMethod,
)
from aiogram.types import (
    CallbackQuery,
    Chat,
    Document,
    File,
    InlineKeyboardMarkup,
    Message,
    PhotoSize,
    Update,
    User,
    Voice,
)

_update_ids = itertools.count(1_000_000)
_message_ids = itertools.count(1)


def norm(text: str) -> str:
    """Tests compare with plain spaces; the bot uses no-break spaces in numbers."""
    return text.replace("\xa0", " ")


def detach_routers() -> None:
    """Routers attach to one Dispatcher per process; emulate a fresh process in tests."""
    from fitcoach.bot.app import ROUTERS

    for router in ROUTERS:
        router._parent_router = None


def check_callback_data(data: str) -> None:
    """Regression guard for callback encoding: size limit and a matching factory."""
    from fitcoach.bot.ui import CALLBACK_CLASSES

    assert len(data.encode()) <= 64, f"callback_data too long: {data!r}"
    prefix = data.split(":", 1)[0]
    factories = [c for c in CALLBACK_CLASSES if c.__prefix__ == prefix]
    assert factories, f"no factory for {data!r}"
    factories[0].unpack(data)


class RecordingSession(BaseSession):
    def __init__(self) -> None:
        super().__init__()
        self.requests: list[TelegramMethod[Any]] = []
        self.files: dict[str, bytes] = {}
        # message_id -> (chat_id, text, live inline keyboard)
        self.messages: dict[int, tuple[int, str | None, InlineKeyboardMarkup | None]] = {}
        self.answers: dict[str, int] = {}

    def _message(self, bot: Bot, chat_id: int, message_id: int, text: str | None) -> Message:
        return Message(
            message_id=message_id,
            date=dt.datetime.now(dt.UTC),
            chat=Chat(id=chat_id, type="private"),
            text=text,
        ).as_(bot)

    @staticmethod
    def _inline(markup: Any) -> InlineKeyboardMarkup | None:
        if not isinstance(markup, InlineKeyboardMarkup):
            return None
        for row in markup.inline_keyboard:
            for button in row:
                if button.callback_data is not None:
                    check_callback_data(button.callback_data)
        return markup

    async def make_request(
        self,
        bot: Bot,
        method: TelegramMethod[Any],
        timeout: int | None = None,  # noqa: ASYNC109
    ) -> Any:
        self.requests.append(method)
        if isinstance(method, GetFile):
            data = self.files[method.file_id]
            return File(
                file_id=method.file_id,
                file_unique_id="u" + method.file_id,
                file_size=len(data),
                file_path=f"files/{method.file_id}",
            )
        if isinstance(method, AnswerCallbackQuery):
            self.answers[method.callback_query_id] = (
                self.answers.get(method.callback_query_id, 0) + 1
            )
            return True
        if isinstance(method, EditMessageText):
            assert method.message_id is not None
            chat_id = int(method.chat_id or 0)
            # Editing text without reply_markup removes the inline keyboard, as in Telegram.
            markup = self._inline(method.reply_markup)
            self.messages[method.message_id] = (chat_id, method.text, markup)
            return self._message(bot, chat_id, method.message_id, method.text)
        if isinstance(method, EditMessageReplyMarkup):
            assert method.message_id is not None
            chat_id, text, _ = self.messages.get(method.message_id, (0, None, None))
            self.messages[method.message_id] = (chat_id, text, self._inline(method.reply_markup))
            return True
        if getattr(type(method), "__returning__", None) is Message:
            chat_id = int(getattr(method, "chat_id", 0))
            message_id = next(_message_ids)
            text = getattr(method, "text", None)
            markup = self._inline(getattr(method, "reply_markup", None))
            self.messages[message_id] = (chat_id, text, markup)
            return self._message(bot, chat_id, message_id, text)
        return True

    async def stream_content(self, url: str, *args: Any, **kwargs: Any) -> Any:
        file_id = url.rsplit("/", 1)[-1]
        yield self.files[file_id]

    async def close(self) -> None:
        pass


class TgUser:
    """A synthetic Telegram user chatting with the bot."""

    def __init__(self, telegram_id: int, dp: Dispatcher, bot: Bot) -> None:
        self.id = telegram_id
        self.dp = dp
        self.bot = bot
        self._user = User(id=telegram_id, is_bot=False, first_name="Анна", language_code="ru")
        self._chat = Chat(id=telegram_id, type="private")

    @property
    def session(self) -> RecordingSession:
        session = self.bot.session
        assert isinstance(session, RecordingSession)
        return session

    def _message(self, text: str | None, message_id: int | None = None) -> Message:
        return Message(
            message_id=message_id if message_id is not None else next(_message_ids),
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

    async def send_photo(self, data: bytes, caption: str | None = None) -> list[str]:
        file_id = f"photo{next(_message_ids)}"
        self.session.files[file_id] = data
        message = self._message(None).model_copy(
            update={
                "photo": [
                    PhotoSize(
                        file_id=file_id,
                        file_unique_id="u" + file_id,
                        width=640,
                        height=480,
                        file_size=len(data),
                    )
                ],
                "caption": caption,
            }
        )
        return await self._feed(Update(update_id=next(_update_ids), message=message))

    async def send_voice(self, data: bytes, duration: int = 5) -> list[str]:
        file_id = f"voice{next(_message_ids)}"
        self.session.files[file_id] = data
        message = self._message(None).model_copy(
            update={
                "voice": Voice(
                    file_id=file_id,
                    file_unique_id="u" + file_id,
                    duration=duration,
                    file_size=len(data),
                )
            }
        )
        return await self._feed(Update(update_id=next(_update_ids), message=message))

    async def send_document(self, data: bytes, name: str) -> list[str]:
        file_id = f"doc{next(_message_ids)}"
        self.session.files[file_id] = data
        message = self._message(None).model_copy(
            update={
                "document": Document(
                    file_id=file_id,
                    file_unique_id="u" + file_id,
                    file_name=name,
                    file_size=len(data),
                )
            }
        )
        return await self._feed(Update(update_id=next(_update_ids), message=message))

    def sent_documents(self) -> list[Any]:
        from aiogram.methods import SendDocument

        return [r for r in self.session.requests if isinstance(r, SendDocument)]

    async def press(
        self, data: str, message_id: int | None = None, *, answered: bool = True
    ) -> list[str]:
        """Press callback data. Without `message_id` it comes from an unknown old message."""
        text = "old screen"
        if message_id is not None:
            text = self.session.messages[message_id][1] or text
        query = CallbackQuery(
            id=str(next(_update_ids)),
            from_user=self._user,
            chat_instance="test",
            data=data,
            message=self._message(text, message_id),
        )
        out = await self._feed(Update(update_id=next(_update_ids), callback_query=query))
        if answered:
            count = self.session.answers.get(query.id, 0)
            assert count == 1, f"button {data!r} answered {count} times"
        return out

    def find(self, label: str) -> tuple[str, int]:
        """(callback data, message id) of a *live* button containing `label`, newest first."""
        for message_id in sorted(self.session.messages, reverse=True):
            chat_id, _, markup = self.session.messages[message_id]
            if chat_id != self.id or markup is None:
                continue
            for row in markup.inline_keyboard:
                for button in row:
                    if label in norm(button.text) and button.callback_data:
                        return button.callback_data, message_id
        raise AssertionError(f"no live button containing {label!r}")

    def button(self, label: str) -> str:
        return self.find(label)[0]

    def buttons(self) -> list[str]:
        """Labels of the newest message that has buttons (the live screen)."""
        for message_id in sorted(self.session.messages, reverse=True):
            chat_id, _, markup = self.session.messages[message_id]
            if chat_id == self.id and markup is not None:
                return [norm(b.text) for row in markup.inline_keyboard for b in row]
        return []

    def screen(self) -> str:
        """Text of the newest message that has buttons."""
        for message_id in sorted(self.session.messages, reverse=True):
            chat_id, text, markup = self.session.messages[message_id]
            if chat_id == self.id and markup is not None:
                return norm(text or "")
        return ""

    def live_screens(self) -> int:
        return sum(
            1
            for chat_id, _, markup in self.session.messages.values()
            if chat_id == self.id and markup is not None
        )

    async def tap(self, label: str) -> list[str]:
        data, message_id = self.find(label)
        return await self.press(data, message_id)


def _text(method: TelegramMethod[Any]) -> str:
    if isinstance(method, SendMessage | EditMessageText | AnswerCallbackQuery):
        return norm(method.text or "")
    return ""
