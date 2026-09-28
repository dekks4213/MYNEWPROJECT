from __future__ import annotations

from aiogram import Bot, Dispatcher, F, Router
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from fitcoach.ai.gateway import AIGateway
from fitcoach.bot.handlers import activities, diary, onboarding
from fitcoach.bot.middleware import UnitOfWorkMiddleware
from fitcoach.bot.ui import main_menu
from fitcoach.config import Settings
from fitcoach.i18n import Translator

fallback = Router(name="fallback")


@fallback.message(F.text)
async def unknown_text(message: Message, tr: Translator) -> None:
    await message.answer(tr("unknown"), reply_markup=main_menu(tr))


@fallback.message()
async def unsupported(message: Message, tr: Translator) -> None:
    await message.answer(tr("unsupported_media"), reply_markup=main_menu(tr))


def build_dispatcher(
    sessionmaker: async_sessionmaker[AsyncSession], gateway: AIGateway, settings: Settings
) -> Dispatcher:
    # FSM state is in memory: an interrupted multi-step input restarts after a process
    # restart. Onboarding progress and all records are in PostgreSQL.
    dp = Dispatcher(storage=MemoryStorage())
    dp["gateway"] = gateway
    dp["settings"] = settings
    dp.update.outer_middleware(UnitOfWorkMiddleware(sessionmaker))
    dp.include_routers(onboarding.router, diary.router, activities.router, fallback)
    return dp


def build_bot(settings: Settings) -> Bot:
    if settings.bot_token is None:
        raise RuntimeError("BOT_TOKEN is not set")
    # No parse mode: user-entered names are shown verbatim and can't inject markup.
    return Bot(settings.bot_token.get_secret_value())
