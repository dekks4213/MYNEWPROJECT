"""Per-update unit of work.

For each private-chat update from a verified Telegram user:
  1. open a DB session and bind the RLS identity (transaction-scoped);
  2. record the update_id (duplicate deliveries are skipped);
  3. run the handler; commit; on ServiceError roll back and show a localized message.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Chat, Message, TelegramObject, Update
from aiogram.types import User as TgUser
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from fitcoach.ai.types import AIUnavailableError
from fitcoach.i18n import Translator
from fitcoach.services.errors import ServiceError
from fitcoach.services.users import resolve_user

log = logging.getLogger(__name__)

_MARK_UPDATE = text(
    "INSERT INTO processed_updates (update_id) VALUES (:id) "
    "ON CONFLICT DO NOTHING RETURNING update_id"
)

Handler = Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]]


class UnitOfWorkMiddleware(BaseMiddleware):
    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
        self.sessionmaker = sessionmaker

    async def __call__(self, handler: Handler, event: TelegramObject, data: dict[str, Any]) -> Any:
        assert isinstance(event, Update)
        tg_user: TgUser | None = data.get("event_from_user")
        chat: Chat | None = data.get("event_chat")
        if tg_user is None or tg_user.is_bot or chat is None or chat.type != "private":
            return None  # personal data only in private chats

        async with self.sessionmaker() as session:
            user = await resolve_user(session, tg_user.id)
            marked = await session.execute(_MARK_UPDATE, {"id": event.update_id})
            if marked.scalar_one_or_none() is None:
                log.info("duplicate update skipped")
                return None
            tr = Translator(user.language)
            data.update(session=session, user=user, tr=tr)
            try:
                result = await handler(event, data)
                if session.in_transaction():
                    await session.commit()
                return result
            except (ServiceError, AIUnavailableError) as exc:
                await session.rollback()
                prefix = "ai" if isinstance(exc, AIUnavailableError) else "err"
                await _notify(event, tr(f"{prefix}.{exc.code}"))
                # Keep the update marked so a retry doesn't repeat side effects.
                await self._mark_after_rollback(tg_user.id, event.update_id)
                return None
            except Exception:
                await session.rollback()
                log.exception("unhandled error in handler")
                await _notify(event, tr("err.generic"))
                return None

    async def _mark_after_rollback(self, telegram_id: int, update_id: int) -> None:
        async with self.sessionmaker() as session:
            await resolve_user(session, telegram_id)
            await session.execute(_MARK_UPDATE, {"id": update_id})
            await session.commit()


async def _notify(update: Update, text_out: str) -> None:
    try:
        if update.callback_query is not None:
            cq: CallbackQuery = update.callback_query
            await cq.answer(text_out, show_alert=True)
        elif update.message is not None:
            msg: Message = update.message
            await msg.answer(text_out)
    except Exception:
        log.warning("failed to deliver error message")
