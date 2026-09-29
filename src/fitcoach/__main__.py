"""Entrypoint: `python -m fitcoach` runs the bot in the configured mode."""

from __future__ import annotations

import asyncio
import logging

import uvicorn
from sqlalchemy import text

from fitcoach.ai.gateway import build_gateway, verify_provider
from fitcoach.bot.app import build_bot, build_dispatcher
from fitcoach.bot.scheduler import reminder_loop
from fitcoach.config import get_settings
from fitcoach.db.session import create_engine, create_sessionmaker


async def _polling() -> None:
    settings = get_settings()
    engine = create_engine(settings.database_url, settings.db_pool_size)
    sessionmaker = create_sessionmaker(engine)
    async with engine.begin() as conn:  # idempotency markers are short-lived
        await conn.execute(
            text("DELETE FROM processed_updates WHERE processed_at < now() - interval '7 days'")
        )
    bot = build_bot(settings)
    gateway = build_gateway(settings)
    logging.getLogger(__name__).info(await verify_provider(gateway))
    dp = build_dispatcher(sessionmaker, gateway, settings)
    # Only one polling consumer per token: delete any webhook first.
    await bot.delete_webhook(drop_pending_updates=False)
    jobs = []
    if settings.reminders_enabled:
        jobs.append(asyncio.create_task(reminder_loop(bot, sessionmaker)))
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        for job in jobs:
            job.cancel()
        await bot.session.close()
        await engine.dispose()


def main() -> None:
    settings = get_settings()
    logging.basicConfig(
        level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    # Never log request URLs: they contain the bot token.
    logging.getLogger("aiogram.event").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    if settings.bot_mode == "polling":
        asyncio.run(_polling())
    else:
        uvicorn.run(
            "fitcoach.api.main:create_app",
            factory=True,
            host="0.0.0.0",  # noqa: S104
            port=8080,
            proxy_headers=True,
            access_log=False,
        )


if __name__ == "__main__":
    main()
