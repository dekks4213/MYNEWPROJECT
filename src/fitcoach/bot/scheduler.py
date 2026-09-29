"""The single background job loop (reminders). Runs inside the bot process; database
claiming makes it safe even if two processes run it by mistake."""

from __future__ import annotations

import asyncio
import logging

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramForbiddenError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from fitcoach.bot.ui import Rm, inline
from fitcoach.i18n import Translator
from fitcoach.services.diary import DiaryService
from fitcoach.services.reminders import (
    DeliveryBlockedError,
    DeliveryFailedError,
    DueReminder,
    Sender,
    has_open_plan_today,
    process_due,
)

log = logging.getLogger(__name__)


def telegram_sender(bot: Bot) -> Sender:
    async def send(due: DueReminder, session: AsyncSession) -> bool:
        reminder, user = due.reminder, due.user
        tr = Translator(user.language)
        if reminder.kind == "workout" and not await has_open_plan_today(session, user):
            return False
        if reminder.kind == "weigh_in":
            _, weights, _ = await DiaryService(session, user).entries_for_day()
            if weights:
                return False  # already weighed today: no nagging
        text = (
            reminder.text
            if reminder.kind == "custom" and reminder.text
            else tr("rem.msg." + reminder.kind)
        )
        markup = inline(
            [
                (tr("rem.snooze"), Rm(a="snooze", id=reminder.id)),
                (tr("rem.turn_off"), Rm(a="off", id=reminder.id)),
            ]
        )
        try:
            await bot.send_message(user.telegram_id, "🔔 " + text, reply_markup=markup)
        except TelegramForbiddenError as exc:
            raise DeliveryBlockedError from exc
        except TelegramAPIError as exc:
            raise DeliveryFailedError(type(exc).__name__) from exc
        return True

    return send


async def reminder_loop(
    bot: Bot, sessionmaker: async_sessionmaker[AsyncSession], interval: float = 30.0
) -> None:
    send = telegram_sender(bot)
    while True:
        try:
            counts = await process_due(sessionmaker, send)
            if any(counts.values()):
                log.info("reminders: %s", counts)
        except Exception:  # never let the loop die; details stay in logs without content
            log.exception("reminder tick failed")
        await asyncio.sleep(interval)
