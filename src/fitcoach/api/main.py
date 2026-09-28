"""FastAPI app: health check and Telegram webhook with secret-token validation."""

from __future__ import annotations

import hmac
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from aiogram import Bot, Dispatcher
from aiogram.types import Update
from fastapi import FastAPI, Header, HTTPException, Request, Response
from pydantic import ValidationError
from sqlalchemy import text

from fitcoach.ai.gateway import build_gateway
from fitcoach.bot.app import build_bot, build_dispatcher
from fitcoach.config import Settings, get_settings
from fitcoach.db.session import create_engine, create_sessionmaker

log = logging.getLogger(__name__)
MAX_UPDATE_BYTES = 1_000_000


def verify_secret(expected: str | None, received: str | None) -> bool:
    if not expected or received is None:
        return False
    return hmac.compare_digest(expected.encode(), received.encode())


def create_app(
    settings: Settings | None = None, bot: Bot | None = None, dp: Dispatcher | None = None
) -> FastAPI:
    settings = settings or get_settings()
    engine = create_engine(settings.database_url, settings.db_pool_size)
    sessionmaker = create_sessionmaker(engine)
    dp = dp or build_dispatcher(sessionmaker, build_gateway(settings), settings)
    bot_ref: dict[str, Bot] = {}

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        bot_ref["bot"] = bot or build_bot(settings)
        if settings.bot_mode == "webhook" and bot is None and settings.webhook_base_url:
            assert settings.webhook_secret is not None
            await bot_ref["bot"].set_webhook(
                settings.webhook_base_url.rstrip("/") + settings.webhook_path,
                secret_token=settings.webhook_secret.get_secret_value(),
                drop_pending_updates=False,
            )
        yield
        if bot is None:
            await bot_ref["bot"].session.close()
        await engine.dispose()

    app = FastAPI(title=settings.app_name, lifespan=lifespan, docs_url=None, redoc_url=None)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return {"status": "ok"}

    @app.post(settings.webhook_path)
    async def telegram_webhook(
        request: Request,
        x_telegram_bot_api_secret_token: str | None = Header(default=None),
    ) -> Response:
        expected = settings.webhook_secret.get_secret_value() if settings.webhook_secret else None
        if not verify_secret(expected, x_telegram_bot_api_secret_token):
            raise HTTPException(status_code=401)
        body = await request.body()
        if len(body) > MAX_UPDATE_BYTES:
            raise HTTPException(status_code=413)
        try:
            update = Update.model_validate_json(body, context={"bot": bot_ref["bot"]})
        except ValidationError as exc:
            raise HTTPException(status_code=400) from exc
        await dp.feed_update(bot_ref["bot"], update)
        return Response(status_code=200)

    return app
