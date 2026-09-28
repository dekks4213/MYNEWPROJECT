"""P0 acceptance flow through the real Dispatcher, middleware, services and PostgreSQL.

Telegram itself is replaced by a recording session: this is an integration test,
not a live Telegram verification.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from aiogram import Bot
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from fitcoach.ai.gateway import AIGateway
from fitcoach.ai.mock import MockProvider
from fitcoach.bot.app import build_dispatcher
from fitcoach.bot.ui import Ac, En
from fitcoach.config import Settings
from fitcoach.db.models import FoodEntry
from fitcoach.db.session import create_engine, create_sessionmaker
from fitcoach.services.activities import ActivityService
from fitcoach.services.users import resolve_user
from tests.bot_harness import RecordingSession, TgUser, detach_routers
from tests.conftest import new_telegram_id, requires_db

pytestmark = requires_db


class App:
    """One running bot process. `restart()` builds a fresh engine and dispatcher."""

    def __init__(self, app_url: str, gateway: AIGateway) -> None:
        self.app_url = app_url
        self.gateway = gateway
        self.bot = Bot("42:TEST", session=RecordingSession())
        self._start()

    def _start(self) -> None:
        detach_routers()
        self.engine = create_engine(self.app_url, pool_size=2)
        self.sm: async_sessionmaker[AsyncSession] = create_sessionmaker(self.engine)
        self.dp = build_dispatcher(self.sm, self.gateway, Settings())

    async def restart(self) -> None:
        await self.engine.dispose()
        self._start()

    def user(self, telegram_id: int) -> TgUser:
        return TgUser(telegram_id, self.dp, self.bot)


@pytest.fixture
async def app(database: dict[str, str]) -> AsyncIterator[App]:
    instance = App(database["app_url"], AIGateway(None, Settings()))
    yield instance
    await instance.engine.dispose()


async def onboard(u: TgUser, tz_text: str | None, target: str | None) -> None:
    out = await u.send("/start")
    assert any("Выберите язык" in t for t in out)
    await u.tap("Русский")
    await u.tap("Мне 18 или больше")
    out = await u.tap("Без ИИ")
    assert any("часовой пояс" in t for t in out)
    if tz_text:
        await u.send(tz_text)
    else:
        await u.tap("UTC")
    await u.tap("Метрическая")
    await u.tap("Просто наладить привычки")
    out = await u.send(target) if target else await u.tap("Без цели")
    assert any("Готово" in t for t in out)


def joined(out: list[str]) -> str:
    return "\n".join(out)


async def test_p0_acceptance_two_users_restart_and_isolation(app: App) -> None:
    a, b = app.user(new_telegram_id()), app.user(new_telegram_id())

    # Features are gated until onboarding is finished; progress is resumable.
    out = await a.send("🍽 Еда")
    assert "Выберите язык" in joined(out)
    await onboard(a, "Europe/Moscow", "2000")
    await onboard(b, None, None)

    # A: food (one-line manual form), weight.
    await a.send("🍽 Еда")
    await a.send("Овсянка; 350; 12/6/50")
    out = await a.tap("взвешено")
    assert "Записано: Овсянка — 350 ккал" in joined(out)
    await a.send("⚖️ Вес")
    assert "72.4 кг" in joined(await a.send("72,4"))
    await a.send("⚖️ Вес")
    assert "Вес должен быть" in joined(await a.send("7"))
    await a.send("/cancel")

    # A: custom activity with duration + one custom numeric field, no migration involved.
    await a.send("🏃 Тренировки")
    await a.tap("Новый вид активности")
    await a.send("Эндуро")
    await a.tap("Добавить поле")
    await a.send("Круги")
    await a.tap("Целое число")
    out = await a.send("кр")
    assert "Круги — Целое число, кр" in joined(out)
    assert "создан" in joined(await a.tap("Сохранить вид"))

    # A: template with targets, then plan it for today.
    await a.tap("Новый шаблон")
    await a.send("Трасса")
    await a.send("60")  # planned duration
    out = await a.send("10")  # planned laps
    assert "Круги: 10 кр" in joined(out)
    assert "сохранён" in joined(await a.tap("Сохранить"))
    await a.tap("Запланировать на сегодня")

    out = await a.send("📊 Сегодня")
    summary = joined(out)
    assert "Энергия: 350 ккал" in summary
    assert "осталось 1650" in summary
    assert "Тренировок сегодня не записано" in summary  # a plan is not completed work
    assert "Дальше по плану: Трасса (сегодня)" in summary

    # A: record the planned workout with actual values only.
    await a.send("🏃 Тренировки")
    await a.tap("Записать выполненную")
    out = await a.tap("📅 Трасса")
    assert "План: 1:00" in joined(out)
    out = await a.send("1:30")  # h:mm field -> 90 minutes
    assert "План: 10 кр" in joined(out)
    await a.tap("Пропустить")
    assert "Тренировка записана" in joined(await a.tap("Сохранить"))
    assert "неактуальна" in joined(await a.press(a.button("Сохранить")))  # double tap

    out = await a.send("📊 Сегодня")
    assert "Трасса: Длительность: 1:30" in joined(out)
    assert "Круги" not in joined(out)  # skipped value is unknown, not a copied target

    # B cannot see or act on A's records, even with forged callback data.
    async with app.sm() as s:
        ua = await resolve_user(s, a.id)
        a_template = (await ActivityService(s, ua).list_templates())[0].id
        a_food = (await s.execute(select(FoodEntry.id))).scalars().first()
    assert a_food is not None
    out = await b.press(Ac(action="rec_tpl", id=a_template).pack())
    assert "Запись не найдена" in joined(out)
    out = await b.press(En(action="del", kind="food", id=a_food).pack())
    assert "Запись не найдена" in joined(out)
    out = await b.send("📊 Сегодня")
    assert "записей пока нет" in joined(out)
    assert "Трасса" not in joined(out)

    # Restart the "service": data persists and stays separated.
    await app.restart()
    a, b = app.user(a.id), app.user(b.id)
    summary_a = joined(await a.send("📊 Сегодня"))
    assert "Энергия: 350 ккал" in summary_a and "Трасса" in summary_a
    assert "72.4" in summary_a
    summary_b = joined(await b.send("📊 Сегодня"))
    assert "записей пока нет" in summary_b and "72.4" not in summary_b


async def test_duplicate_update_is_processed_once(app: App) -> None:
    u = app.user(new_telegram_id())
    await onboard(u, None, None)
    await u.send("🍽 Еда")
    await u.send("Чай; ?", update_id=990_000_000 + u.id % 1_000_000)
    again = await u.send("Чай; ?", update_id=990_000_000 + u.id % 1_000_000)
    assert again == []
    async with app.sm() as s:
        user = await resolve_user(s, u.id)
        count = (
            await s.execute(select(func.count()).where(FoodEntry.owner_id == user.id))
        ).scalar_one()
    assert count == 1


async def test_manual_corrections_and_undo(app: App) -> None:
    u = app.user(new_telegram_id())
    await onboard(u, None, "1800")
    await u.send("🍽 Еда")
    await u.send("Суп")
    await u.tap("Не знаю")
    out = await u.tap("Пропустить")
    assert "Суп — ккал неизвестны (без данных)" in joined(out)
    await u.send("✏️ Исправить")
    await u.tap("✏️ 1")
    out = await u.send("abc")
    assert "Нужно число" in joined(out)
    out = await u.send("210")
    assert "Исправлено: Суп — 210 ккал" in joined(out)
    await u.send("✏️ Исправить")
    await u.tap("🗑 1")
    assert "записей пока нет" in joined(await u.send("📊 Сегодня"))
    await u.tap("Восстановить")
    assert "Энергия: 210 ккал" in joined(await u.send("📊 Сегодня"))


async def test_ai_draft_flow_with_mock_is_labelled_and_needs_confirmation(
    database: dict[str, str],
) -> None:
    app = App(database["app_url"], AIGateway(MockProvider(), Settings(ai_provider="mock")))
    try:
        u = app.user(new_telegram_id())
        await u.send("/start")
        await u.tap("Русский")
        await u.tap("Мне 18 или больше")
        await u.tap("Разрешить ИИ")
        await u.tap("UTC")
        await u.tap("Метрическая")
        await u.tap("Пропустить")
        await u.tap("Без цели")
        await u.send("🍽 Еда")
        await u.tap("Описать свободным текстом")
        out = joined(await u.send("гречка и котлета"))
        assert "Черновик" in out and "Тестовый режим ИИ" in out
        assert "записей пока нет" in joined(await u.send("📊 Сегодня"))  # not saved yet
        confirm = u.button("Сохранить")
        assert "Сохранено записей: 2" in joined(await u.press(confirm))
        assert "уже обработан" in joined(await u.press(confirm))
        assert "записей — 2" in joined(await u.send("📊 Сегодня"))
    finally:
        await app.engine.dispose()


async def test_manual_mode_without_ai_has_no_ai_button(app: App) -> None:
    u = app.user(new_telegram_id())
    await onboard(u, None, None)
    await u.send("🍽 Еда")
    with pytest.raises(AssertionError):
        u.button("свободным текстом")
    out = await u.send("🍽 Еда")
    assert "Что вы съели" in joined(out)


async def test_english_localization(app: App) -> None:
    u = app.user(new_telegram_id())
    await u.send("/start")
    out = await u.tap("English")
    assert "18 or older" in joined(out)
    await u.tap("I'm 18 or older")
    await u.tap("No AI")
    await u.send("Europe/London")
    await u.tap("Metric")
    await u.tap("Skip")
    out = await u.tap("No calorie target")
    assert "All set" in joined(out)
    assert "Food: no entries yet" in joined(await u.send("📊 Today"))
