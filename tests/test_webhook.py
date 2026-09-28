"""Webhook secret validation through the real FastAPI app (fake Telegram Bot API session)."""

from __future__ import annotations

import datetime as dt

from aiogram import Bot
from fastapi.testclient import TestClient

from fitcoach.api.main import create_app, verify_secret
from fitcoach.config import Settings
from tests.bot_harness import RecordingSession, detach_routers
from tests.conftest import new_telegram_id, requires_db

SECRET = "s" * 40


def test_verify_secret() -> None:
    assert verify_secret(SECRET, SECRET)
    assert not verify_secret(SECRET, "wrong")
    assert not verify_secret(SECRET, None)
    assert not verify_secret(None, SECRET)


def _update(update_id: int, user_id: int) -> dict[str, object]:
    return {
        "update_id": update_id,
        "message": {
            "message_id": 1,
            "date": int(dt.datetime.now(dt.UTC).timestamp()),
            "chat": {"id": user_id, "type": "private"},
            "from": {"id": user_id, "is_bot": False, "first_name": "T"},
            "text": "/start",
        },
    }


@requires_db
def test_webhook_requires_secret_and_processes_valid_updates(database: dict[str, str]) -> None:
    settings = Settings(database_url=database["app_url"], bot_mode="webhook", webhook_secret=SECRET)
    session = RecordingSession()
    bot = Bot("42:TEST", session=session)
    detach_routers()
    with TestClient(create_app(settings, bot=bot)) as client:
        assert client.get("/healthz").json() == {"status": "ok"}
        uid = new_telegram_id()
        path = settings.webhook_path
        assert client.post(path, json=_update(1, uid)).status_code == 401
        headers = {"X-Telegram-Bot-Api-Secret-Token": "wrong"}
        assert client.post(path, json=_update(1, uid), headers=headers).status_code == 401
        assert session.requests == []
        headers = {"X-Telegram-Bot-Api-Secret-Token": SECRET}
        assert client.post(path, content=b"{bad", headers=headers).status_code == 400
        big = b"x" * 1_000_001
        assert client.post(path, content=big, headers=headers).status_code == 413
        response = client.post(path, json=_update(880_000_001, uid), headers=headers)
        assert response.status_code == 200
        assert any("Выберите язык" in getattr(r, "text", "") for r in session.requests)
