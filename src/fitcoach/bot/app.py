from __future__ import annotations

from aiogram import Bot, Dispatcher, F, Router
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from fitcoach.ai.gateway import AIGateway
from fitcoach.bot.handlers import (
    common,
    day,
    food,
    history,
    onboarding,
    profile,
    settings,
    training,
)
from fitcoach.bot.middleware import UnitOfWorkMiddleware
from fitcoach.bot.ui import main_menu
from fitcoach.config import Settings
from fitcoach.i18n import Translator
from fitcoach.services.food_sources import FoodSource, build_sources

fallback = Router(name="fallback")


@fallback.message(F.text)
async def unknown_text(message: Message, tr: Translator) -> None:
    await message.answer(tr("unknown"), reply_markup=main_menu(tr))


@fallback.message()
async def unsupported(message: Message, tr: Translator) -> None:
    await message.answer(tr("unsupported_media"), reply_markup=main_menu(tr))


@fallback.callback_query()
async def stale_callback(query: CallbackQuery, tr: Translator) -> None:
    """Buttons from finished flows (e.g. a second tap on Save) do nothing."""
    await query.answer(tr("stale_button"))


ROUTERS = (
    onboarding.router,
    common.router,
    day.router,
    food.router,
    training.router,
    history.router,
    profile.router,
    settings.router,
    fallback,
)


def build_dispatcher(
    sessionmaker: async_sessionmaker[AsyncSession],
    gateway: AIGateway,
    settings_: Settings,
    food_sources: list[FoodSource] | None = None,
) -> Dispatcher:
    # FSM state is in memory: an interrupted multi-step input restarts after a process
    # restart. Onboarding progress, drafts and all records are in PostgreSQL.
    dp = Dispatcher(storage=MemoryStorage())
    dp["gateway"] = gateway
    dp["settings"] = settings_
    dp["food_sources"] = (
        food_sources
        if food_sources is not None
        else build_sources(
            settings_.food_sources,
            usda_key=settings_.usda_api_key.get_secret_value() if settings_.usda_api_key else None,
            timeout=settings_.food_source_timeout_seconds,
        )
    )
    dp.update.outer_middleware(UnitOfWorkMiddleware(sessionmaker))
    dp.include_routers(*ROUTERS)
    return dp


def build_bot(settings_: Settings) -> Bot:
    if settings_.bot_token is None:
        raise RuntimeError("BOT_TOKEN is not set")
    # No parse mode: user-entered names are shown verbatim and can't inject markup.
    return Bot(settings_.bot_token.get_secret_value())
