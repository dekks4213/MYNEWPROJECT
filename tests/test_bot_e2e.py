"""User journeys through the real Dispatcher, middleware, services and PostgreSQL.

Telegram is replaced by a recording session (see bot_harness): these are integration tests,
not a live Telegram verification. They check both behaviour and the actual texts and buttons
people see. AI uses the deterministic mock provider.
"""

from __future__ import annotations

import datetime as dt
import io
import json
from collections.abc import AsyncIterator
from decimal import Decimal

import pytest
from aiogram import Bot
from PIL import Image
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from fitcoach.ai.gateway import AIGateway
from fitcoach.ai.mock import MockProvider
from fitcoach.bot.app import build_dispatcher
from fitcoach.bot.ui import Ac, En, Fd, Fm, Fr
from fitcoach.config import Settings
from fitcoach.db.models import FoodEntry, PlannedWorkout, WorkoutSession
from fitcoach.db.session import create_engine, create_sessionmaker
from fitcoach.domain.nutrition import Precision
from fitcoach.services.activities import ActivityService
from fitcoach.services.diary import DiaryService
from fitcoach.services.users import resolve_user, utcnow
from tests.bot_harness import RecordingSession, TgUser, detach_routers
from tests.conftest import new_telegram_id, requires_db
from tests.test_p1_services import STRONG_CSV

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
        self.dp = build_dispatcher(self.sm, self.gateway, Settings(), food_sources=[])

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


@pytest.fixture
async def ai_app(database: dict[str, str]) -> AsyncIterator[App]:
    instance = App(database["app_url"], AIGateway(MockProvider(), Settings(ai_provider="mock")))
    yield instance
    await instance.engine.dispose()


def joined(out: list[str]) -> str:
    return "\n".join(out)


async def onboard(
    u: TgUser, *, city: str = "Москва", tz_text: str | None = None, target: str | None = None
) -> None:
    """Russian onboarding: hello -> goal -> city -> calories -> home."""
    await u.send("/start")
    await u.tap("Начать")
    await u.tap("Просто вести дневник")
    if tz_text:
        await u.send(tz_text)
    else:
        await u.tap(city)
    if target is None:
        await u.tap("Пока без цели")
    elif target.isdigit():
        await u.send(target)
    else:
        await u.tap(target)
    assert u.screen().startswith("🏠 РИТМ")


async def log_food(u: TgUser, text: str) -> str:
    await u.send("🍽 Еда")
    await u.tap("✍️ Написать")
    out = joined(await u.send(text))
    await u.tap("✓ Сохранить")
    return out


# --- 1. onboarding -----------------------------------------------------------------------------


async def test_journey_onboarding(app: App) -> None:
    u = app.user(new_telegram_id())
    out = joined(await u.send("/start"))
    assert "Привет. Я РИТМ." in out and "18 лет" in out
    assert u.buttons() == ["Начать", "English", "Мне нет 18"]

    # Features wait for the setup; the current step is shown again.
    await u.send("🍽 Еда")
    assert "Привет. Я РИТМ." in u.screen()

    await u.tap("Начать")
    assert u.screen().startswith("Чего хотите сейчас?")
    assert u.buttons() == [
        "Поддерживать форму",
        "Снизить вес",
        "Набрать мышцы",
        "Просто вести дневник",
    ]
    await u.tap("Снизить вес")
    assert u.screen().startswith("Где вы сейчас?")
    assert "🌍 Другой город…" in u.buttons() and len(u.buttons()) == 6
    await u.tap("Другой город")
    assert "Владивосток" in u.buttons()
    await u.tap("Владивосток")
    assert u.screen().startswith("🎯 Сколько калорий в день?")
    assert u.buttons() == ["1 800", "2 000", "2 200", "2 500", "Пока без цели"]
    out = joined(await u.tap("2 200"))
    assert "✓ Настройка завершена" in out and "Быстрые кнопки теперь всегда внизу" in out
    home = u.screen()
    assert home.startswith("🏠 РИТМ\n\n") and ", Анна." in home
    assert "0 / 2 200 ккал" not in home  # no fake zero: a call to action instead
    assert "Еды пока нет" in home and "Ещё не записан" in home
    assert u.buttons() == [
        "🍽 Записать еду",
        "🏋️ Тренировка",
        "⚖️ Вес",
        "📊 Мой день",
        "🗓 План",
        "⋯ Ещё",
    ]
    assert u.live_screens() == 1  # one message = one screen
    async with app.sm() as s:
        user = await resolve_user(s, u.id)
        assert user.onboarding_step == "done" and user.timezone == "Asia/Vladivostok"
        assert user.goal == "lose" and user.daily_kcal_target == 2200
        assert user.ai_text_consent_at is None  # asked on first use, not upfront


# --- 2. weight ---------------------------------------------------------------------------------


async def test_journey_weight(app: App) -> None:
    u = app.user(new_telegram_id())
    await onboard(u)
    await u.tap("⚖️ Вес")
    assert "Какой вес сегодня? Напишите число" in u.screen()
    assert "Вес должен быть от 20 до 400 кг" in joined(await u.send("7"))
    out = joined(await u.send("101,8"))
    assert "✓ Вес записан · 101,8 кг" in out

    # Next time: the last value is one tap away, steps adjust it, nothing is saved silently.
    await u.send("🏠 Главное")
    await u.tap("⚖️ Вес")
    screen = u.screen()
    assert "Последний:\n101,8 кг · сегодня" in screen and "Какой вес сегодня?" in screen
    assert u.buttons() == [
        "101,8 кг",
        "−0,5",
        "−0,2",
        "+0,2",
        "+0,5",
        "⌨️ Ввести другой",
        "🏠 Главное",
    ]
    await u.tap("−0,2")
    assert u.buttons()[0] == "101,6 кг"
    await u.tap("101,6 кг")
    assert u.screen() == "⚖️ 101,6 кг\n−0,2 кг к прошлой записи"
    assert u.buttons() == ["✓ Сохранить", "✏️ Изменить", "🏠 Главное"]
    save = u.find("✓ Сохранить")
    out = joined(await u.tap("✓ Сохранить"))
    assert "✓ Вес записан · 101,6 кг" in out
    assert "неактуальна" in joined(await u.press(*save))  # a double tap saves once
    async with app.sm() as s:
        user = await resolve_user(s, u.id)
        latest = await DiaryService(s, user).latest_weight()
        assert latest is not None and latest.weight_kg == Decimal("101.60")
    out = joined(await u.tap("↶ Отменить"))  # undo right from the result screen
    assert "🗑 Запись удалена." in out


# --- 3. food, manual ---------------------------------------------------------------------------


async def test_journey_food_manual(app: App) -> None:
    u = app.user(new_telegram_id())
    await onboard(u, target="2 000")
    await u.send("🍽 Еда")
    assert u.screen() == "🍽 Как запишем?"
    assert u.buttons() == [
        "✍️ Написать",
        "⭐ Избранное",
        "🕘 Недавнее",
        "📋 Мои блюда",
        "🏠 Главное",
    ]  # no photo/voice without AI
    await u.tap("📋 Мои блюда")
    await u.tap("＋ Свой продукт")
    assert "⭐ «Овсянка» (370 ккал на 100 г)" in joined(await u.send("Овсянка; 370; 13/7/60"))
    await u.send("🍽 Еда")
    await u.tap("✍️ Написать")
    assert "✍️ Что вы съели?" in u.screen()
    out = joined(await u.send("овсянка 100 г, банан"))
    assert "овсянка — 100 г\nбанан — ? · ккал неизвестны" in out
    assert "\n\n370 ккал\nБ 13 · Ж 7 · У 60" in out and "≈" not in out
    assert "Без калорий: 1 — не входят в итог" in out
    assert u.buttons() == ["✓ Сохранить", "✏️ Изменить", "＋ Добавить", "✕ Отмена"]

    # Change: one item -> amount buttons -> back to the preview.
    await u.tap("✏️ Изменить")
    assert "Что изменить?" in u.screen()
    await u.tap("✏️ овсянка")
    assert u.buttons()[:3] == ["½", "×1,5", "×2"]
    out = joined(await u.tap("×2"))
    assert "овсянка — 200 г" in out and "740 ккал" in out
    await u.tap("✏️ Изменить")
    await u.tap("✏️ банан")
    out = joined(await u.send("105 ккал"))
    assert "\n≈ 845 ккал" in out and "Без калорий" not in out  # a stated number is approximate
    out = joined(await u.tap("✓ Сохранить"))
    assert "✓ Записано ·" in out and "845 ккал" in out and "За сегодня: 845 / 2 000 ккал" in out
    assert u.buttons() == ["＋ Ещё еда", "📊 Мой день", "🏠 Главное"]
    await u.tap("📊 Мой день")
    day = u.screen()
    assert day.startswith("📊 Сегодня · ") and "🍽 Питание\n845 / 2 000 ккал" in day
    assert "Белки  26 г" in day


# --- 4. repeat yesterday's meal ----------------------------------------------------------------


async def test_journey_copy_yesterday(app: App) -> None:
    u = app.user(new_telegram_id())
    await onboard(u)
    async with app.sm() as s:
        user = await resolve_user(s, u.id)
        diary = DiaryService(s, user)
        yesterday = utcnow() - dt.timedelta(days=1)
        for name, kcal in (("омлет", 320), ("кофе", 40)):
            await diary.add_food(
                name,
                energy_kcal=Decimal(kcal),
                precision=Precision.MEASURED,
                meal_type="breakfast",
                now=yesterday,
            )
        await s.commit()
    await u.send("🍽 Еда")
    await u.tap("🕘 Недавнее")
    screen = u.screen()
    assert screen.startswith("🕘 Недавние приёмы пищи") and "Вчера\n🍳 Завтрак · 360 ккал" in screen
    out = joined(await u.tap("🍳 Вчерашний завтрак"))
    assert "🍳 Вчерашний завтрак\n\nомлет · 320 ккал\nкофе · 40 ккал\n\n≈ 360 ккал" in out
    assert u.buttons() == ["✓ Добавить сегодня", "✏️ Изменить", "← Назад"]
    await u.tap("← Назад")  # the copy is discarded, back to the list
    assert u.screen().startswith("🕘 Недавние приёмы пищи")
    await u.tap("🍳 Вчерашний завтрак")
    out = joined(await u.tap("✓ Добавить сегодня"))
    assert "✓ Записано" in out and "За сегодня: 360 ккал" in out


# --- 5-8. workouts -----------------------------------------------------------------------------


async def test_journey_create_strength_and_complete_workout(app: App) -> None:
    u = app.user(new_telegram_id())
    await onboard(u)
    await u.send("🏋️ Тренировка")
    assert u.screen() == "🏋️ Тренировки\n\nСегодня тренировки нет."
    assert u.buttons()[:2] == ["＋ Создать", "📋 Выбрать шаблон"]
    await u.tap("＋ Создать")
    assert u.screen() == "＋ Новая тренировка\n\nКак хотите создать?"
    assert u.buttons() == [
        "🏋️ Зал",
        "🏊 Плавание",
        "🏍 Эндуро",
        "＋ Другое",
        "← Назад",
        "🏠 Главное",
    ]
    await u.tap("🏋️ Зал")
    assert "Как назовём тренировку?" in u.screen()
    await u.tap("Верх тела")
    assert "Какие упражнения?" in u.screen()
    out = joined(await u.send("Жим лёжа 60x10 60x10 60x10\nТяга блока 50x12 50x12"))
    assert "🏋️ Верх тела\n\n• Жим лёжа — 3 × 10 · 60 кг\n• Тяга блока — 2 × 12 · 50 кг" in out
    assert u.buttons()[:2] == ["✓ Сохранить", "▶️ Начать"]
    out = joined(await u.tap("✓ Сохранить"))
    assert "✓ Тренировка «Верх тела» сохранена" in out
    await u.tap("🗓 Запланировать")
    today = u.find("Сегодня")
    assert "✓ Запланировано: сегодня" in joined(await u.tap("Сегодня"))
    await u.press(*today)  # a double tap plans it once

    # Home and training home show today's plan.
    await u.tap("🏠 Главное")
    assert "Тренировка:\nВерх тела · сегодня" in u.screen()
    await u.send("🏋️ Тренировка")
    assert u.screen() == "🏋️ Тренировки\n\nСегодня\nВерх тела"
    await u.tap("▶️ Начать")
    assert u.screen() == "🏋️ Верх тела\nУпражнение 1 из 2\n\nЖим лёжа\nПлан: 3 × 10 · 60 кг"
    assert u.buttons() == [
        "✓ 3 × 10 · 60 кг",
        "✏️ Другой результат",
        "Пропустить",
        "Завершить тренировку",
    ]
    out = joined(await u.tap("✓ 3 × 10 · 60 кг"))
    assert "✓ Жим лёжа\n3 × 10 · 60 кг" in out
    await u.tap("Следующее упражнение →")
    await u.tap("✏️ Другой результат")
    out = joined(await u.send("50x12 50x10"))
    assert "✓ Тяга блока\n50×12, 50×10" in out
    await u.tap("Дальше →")
    assert "⏱ Длительность" in u.screen() and "Шаг 1 из 2" in u.screen()
    await u.tap("45 мин")
    assert "💪 Насколько было тяжело" in u.screen()
    assert u.buttons()[:4] == ["Легко · 3", "Нормально · 5", "Тяжело · 7", "Очень тяжело · 9"]
    assert "RPE" not in u.screen() and "1–10" not in joined(u.buttons())
    out = joined(await u.tap("Тяжело · 7"))
    assert "🏋️ Верх тела · проверьте" in out
    assert "Не понял" in joined(await u.send("ок"))  # typed where a button is expected
    assert u.find("✓ Сохранить")  # ...and the review screen keeps its buttons
    assert "• Жим лёжа — 3 × 10 · 60 кг" in out and "⏱ Длительность: 45 мин" in out
    assert "💪 Насколько было тяжело: Тяжело · 7/10" in out
    save = u.find("✓ Сохранить")
    assert "✓ Тренировка записана\n🏋️ Верх тела · 45 мин" in joined(await u.tap("✓ Сохранить"))
    assert "неактуальна" in joined(await u.press(*save))

    # Second time: "last time" and progress against it.
    await u.send("🏋️ Тренировка")
    await u.tap("📋 Мои тренировки")
    await u.tap("📋 Верх тела")
    await u.tap("▶️ Начать")
    assert "Последний раз: 3 × 10 · 60 кг" in u.screen()
    await u.tap("✏️ Другой результат")
    out = joined(await u.send("60x11 60x10 60x10"))
    assert "+1 повт. к прошлой тренировке" in out
    await u.tap("Следующее упражнение →")
    assert "↻ Как в прошлый раз" in u.buttons()
    await u.tap("Завершить тренировку")
    await u.tap("Пропустить остальное")
    await u.tap("✓ Сохранить")
    async with app.sm() as s:
        user = await resolve_user(s, u.id)
        assert len(await ActivityService(s, user).list_sessions()) == 2
        plans = select(func.count()).where(PlannedWorkout.owner_id == user.id)
        assert (await s.execute(plans)).scalar_one() == 1


async def test_journey_create_swimming(app: App) -> None:
    u = app.user(new_telegram_id())
    await onboard(u)
    await u.send("🏋️ Тренировка")
    await u.tap("＋ Создать")
    await u.tap("🏊 Плавание")
    assert u.screen() == "🏊 Где плаваем?"
    await u.tap("25 м")
    assert "Опишите тренировку по частям" in u.screen()
    out = joined(
        await u.send(
            "Разминка\nКроль 200м\nТехника\nУпражнения 4x50м\nОсновная\nКроль 8x100м\n"
            "Заминка\nНа спине 200м"
        )
    )
    assert out.endswith(
        "🏊 Плавание 1 400 м · бассейн 25 м\n\nРазминка\n• Кроль — 200 м\nТехника\n"
        "• Упражнения — 4 × 50 м\nОсновная часть\n• Кроль — 8 × 100 м\nЗаминка\n"
        "• На спине — 200 м\n\nВсего: 1 400 м"
    )
    await u.tap("▶️ Начать")  # saves the workout and starts it
    assert "Упражнение 1 из 4" in u.screen() and "Кроль" in u.screen()
    async with app.sm() as s:
        user = await resolve_user(s, u.id)
        (tpl,) = await ActivityService(s, user).list_templates()
        tv = await ActivityService(s, user).current_template_version(tpl.id)
        assert tv.targets["f2"] == 25  # pool length is a target, not performed work


async def test_journey_custom_activity(app: App) -> None:
    u = app.user(new_telegram_id())
    await onboard(u)
    await u.send("🏋️ Тренировка")
    await u.tap("＋ Создать")
    await u.tap("＋ Другое")
    await u.tap("🛠 Своя")
    await u.send("Скалолазание")
    screen = u.screen()
    assert screen.startswith("🛠 Скалолазание\n\nЧто будем записывать:\n⏱ Длительность")
    assert "Что ещё хотите отслеживать?" in screen
    assert u.buttons()[:6] == [
        "⏱ Время",
        "📏 Дистанцию",
        "🔢 Количество",
        "⭐ Оценку",
        "📝 Заметку",
        "＋ Другое",
    ]
    await u.tap("🔢 Количество")
    assert "Как назовём показатель?" in u.screen()
    await u.send("Трассы")
    await u.tap("⭐ Оценку")
    await u.tap("✓ Оценка")
    await u.tap("＋ Другое")
    await u.tap("Вариант из списка")
    await u.send("Сложность")
    out = joined(await u.send("лёгкая, средняя, сложная"))
    assert "🔢 Трассы\n⭐ Оценка\n🔘 Сложность" in out
    for word in ("integer", "decimal", "boolean", "Целое", "Число"):
        assert word not in out
    out = joined(await u.tap("✓ Готово"))
    assert "✓ «Скалолазание» готово" in out
    await u.tap("▶️ Записать сейчас")
    await u.tap("60 мин")
    await u.send("5")
    assert u.buttons()[:3] == ["⭐", "⭐⭐", "⭐⭐⭐"]
    await u.tap("⭐⭐⭐⭐")
    await u.tap("средняя")
    assert "✓ Тренировка записана" in joined(await u.tap("✓ Сохранить"))


async def test_journey_enduro_preview(app: App) -> None:
    u = app.user(new_telegram_id())
    await onboard(u)
    await u.send("🏋️ Тренировка")
    await u.tap("＋ Создать")
    await u.tap("🏍 Эндуро")
    screen = u.screen()
    assert screen.startswith("🏍 Эндуро / мотокросс\n\nЧто будем записывать:")
    assert "⏱ Время в движении" in screen and "📏 Дистанция" in screen
    assert "💪 Нагрузка" in screen and "📝 Заметка" in screen and "ккал" not in screen
    assert screen.count("\n") == 7  # five things to note, not a long form
    await u.tap("▶️ Записать сейчас")
    await u.send("1:10")  # riding time
    await u.send("42,5")  # distance
    await u.tap("Лес")
    await u.tap("Тяжело · 7")
    await u.tap("Пропустить")  # note
    review = u.screen()
    assert "⏱ Время в движении: 1 ч 10 мин" in review and "📏 Дистанция: 42.5 км" in review
    assert "🔘 Покрытие: Лес" in review and "💪 Нагрузка: Тяжело · 7/10" in review
    assert "ккал" not in review  # no calories from a motorcycle
    assert u.buttons()[:2] == ["✓ Сохранить", "✏️ Изменить"]


# --- 9. reminder -------------------------------------------------------------------------------


async def test_journey_reminder(app: App) -> None:
    u = app.user(new_telegram_id())
    await onboard(u)
    await u.tap("⋯ Ещё")
    await u.tap("⚙️ Настройки")
    assert u.buttons() == [
        "👤 Профиль",
        "🎯 Цели",
        "🔔 Напоминания",
        "🌍 Язык и регион",
        "🤖 ИИ-функции",
        "🔐 Данные",
        "❓ Помощь",
        "← Назад",
        "🏠 Главное",
    ]
    await u.tap("🔔 Напоминания")
    await u.tap("＋ Добавить")
    await u.tap("⚖️ Взвеситься")
    assert u.screen().startswith("⏰ Когда напоминать?")
    assert u.buttons()[:4] == [
        "Утром · 08:00",
        "Днём · 13:00",
        "Вечером · 19:00",
        "🕐 Выбрать время",
    ]
    await u.tap("Утром · 08:00")
    out = joined(await u.tap("По будням"))
    assert "✓ Напоминание: 08:00 · ⚖️ Взвеситься · по будням" in out
    await u.tap("🔔 Все напоминания")
    await u.tap("🌙 Не беспокоить")
    assert u.buttons()[:4] == ["22:00–08:00", "23:00–07:00", "Настроить", "Не использовать"]
    await u.tap("22:00–08:00")
    screen = u.screen()
    assert "🔔 08:00 · ⚖️ Взвеситься · по будням" in screen
    assert "🌙 Не беспокоить: 22:00–08:00" in screen
    # A typed time works too; wrong input explains itself.
    await u.tap("＋ Добавить")
    await u.tap("🍽 Записать еду")
    await u.tap("🕐 Выбрать время")
    assert "Не понял время" in joined(await u.send("25:99"))
    await u.send("20:30")
    assert "20:30 · 🍽 Записать еду · каждый день" in joined(await u.tap("Каждый день"))


# --- 10-11. cancel halfway, back navigation ----------------------------------------------------


async def test_journey_cancel_halfway_restart_and_stale_buttons(app: App) -> None:
    u = app.user(new_telegram_id())
    await onboard(u)
    await u.send("🍽 Еда")
    await u.tap("✍️ Написать")
    await u.send("суп 300 г")
    old_confirm = u.find("✓ Сохранить")
    out = joined(await u.tap("✕ Отмена"))
    assert "Черновик удалён — ничего не сохранено." in out and "🏠 РИТМ" in out
    assert "уже обработан" in joined(await u.press(*old_confirm))

    # /cancel in the middle of a multi-step flow; the flow does not resume by itself.
    await u.send("🏋️ Тренировка")
    await u.tap("＋ Создать")
    await u.tap("＋ Другое")
    await u.tap("🛠 Своя")
    out = joined(await u.send("/cancel"))
    assert "Отменено." in out and "🏠 РИТМ" in out
    out = joined(await u.send("Йога"))  # no longer a name: asked what it is
    assert "Что записать?" in out and "«Йога»" in out
    async with app.sm() as s:
        user = await resolve_user(s, u.id)
        assert await ActivityService(s, user).list_types() == []

    # A restart loses the in-memory step, never the data; old buttons fail gracefully.
    await u.tap("🍽 Это еда")
    draft_confirm = u.find("✓ Сохранить")
    await app.restart()
    u = app.user(u.id)
    assert "✓ Записано" in joined(await u.press(*draft_confirm))  # drafts live in the DB
    assert "неактуальна" in joined(await u.press(Fd(action="wsave", value="80").pack()))
    assert "Напишите ещё раз" in joined(await u.press(Fd(action="guess", value="food").pack()))


async def test_journey_back_navigation(app: App) -> None:
    u = app.user(new_telegram_id())
    await onboard(u)
    await u.tap("📊 Мой день")
    assert u.screen().startswith("📊 Сегодня")
    await u.tap("‹ Вчера")
    assert u.screen().startswith("📊 Вчера")
    assert "Ничего не записано" in u.screen()  # no fake progress for missing data
    await u.tap("📊 Сегодня")
    await u.tap("🏠 Главное")
    await u.tap("⋯ Ещё")
    await u.tap("⚙️ Настройки")
    await u.tap("🎯 Цели")
    await u.tap("2 000")
    assert "Калории: 2 000 ккал" in u.screen()
    await u.tap("← Назад")
    assert u.screen() == "⚙️ Настройки"
    await u.tap("← Назад")
    assert u.screen() == "⋯ Ещё"
    await u.tap("🏠 Главное")
    assert u.screen().startswith("🏠 РИТМ")
    assert u.live_screens() == 1  # every screen above was the same message, edited

    # Food: prompt -> menu; item -> edit list -> preview.
    await u.send("🍽 Еда")
    await u.tap("✍️ Написать")
    await u.tap("← Назад")
    assert u.screen().startswith("🍽 Как запишем?")
    await u.tap("✍️ Написать")
    await u.send("рис 150 г")
    await u.tap("✏️ Изменить")
    await u.tap("✏️ рис")
    await u.tap("← Назад")
    assert "Что изменить?" in u.screen()
    await u.tap("← Назад")
    assert u.buttons()[0] == "✓ Сохранить"
    assert u.live_screens() == 1


# --- 12-13. languages --------------------------------------------------------------------------


async def test_journey_russian_has_no_jargon(app: App) -> None:
    u = app.user(new_telegram_id())
    await onboard(u)
    seen: list[str] = []
    for label in ("🍽 Еда", "🏋️ Тренировка", "📊 Мой день", "🏠 Главное"):
        seen += await u.send(label)
        seen += u.buttons()
    await u.tap("⋯ Ещё")
    await u.tap("⚙️ Настройки")
    await u.tap("🎯 Цели")
    seen.append(u.screen())
    text = "\n".join(seen).lower()
    for jargon in ("rpe", "rir", "кбжу", "макрос", "пресет", "схема", "метрик", "integer"):
        assert jargon not in text, jargon


async def test_journey_english(app: App) -> None:
    u = app.user(new_telegram_id())
    await u.send("/start")
    await u.tap("English")
    assert u.screen().startswith("Hi. I'm RITM.")
    await u.tap("Start")
    await u.tap("Just keep a diary")
    await u.tap("London")
    out = joined(await u.tap("No target for now"))
    assert "All set!" in out and "🏠 RITM" in u.screen() and ", Анна." in u.screen()
    await u.send("📊 My day")
    assert "Nothing logged" in u.screen()
    await u.send("🍽 Food")
    assert u.screen() == "🍽 How do you want to log it?"
    await u.tap("✍️ Type")
    out = joined(await u.send("rice 150 g"))
    assert "rice — 150 g · kcal unknown" in out
    assert u.buttons() == ["✓ Save", "✏️ Change", "＋ Add", "✕ Cancel"]


# --- settings, free text, AI -------------------------------------------------------------------


async def test_reset_onboarding_keeps_data(app: App) -> None:
    u = app.user(new_telegram_id())
    await onboard(u)
    await log_food(u, "суп 300 г")
    await u.tap("🏠 Главное")
    await u.tap("⋯ Ещё")
    await u.tap("⚙️ Настройки")
    await u.tap("👤 Профиль")
    await u.tap("↺ Пройти настройку заново")
    assert "Все записи останутся" in u.screen()
    await u.tap("✓ Да, заново")
    assert u.screen().startswith("Чего хотите сейчас?")
    await u.tap("Набрать мышцы")
    await u.tap("Москва")
    await u.tap("2 500")
    async with app.sm() as s:
        user = await resolve_user(s, u.id)
        assert user.goal == "gain" and user.onboarding_step == "done"
        count = select(func.count()).where(FoodEntry.owner_id == user.id)
        assert (await s.execute(count)).scalar_one() == 1


async def test_free_text_is_classified_not_rejected(app: App) -> None:
    u = app.user(new_telegram_id())
    await onboard(u)
    out = joined(await u.send("82,4"))
    assert "Что записать?" in out
    assert u.buttons()[:3] == ["⚖️ Вес 82,4 кг", "🍽 Это еда", "🏋️ Это тренировка"]
    await u.tap("⚖️ Вес 82,4 кг")
    assert u.screen() == "⚖️ 82,4 кг"  # shown first, never saved silently
    assert "✓ Вес записан · 82,4 кг" in joined(await u.tap("✓ Сохранить"))


async def test_ai_consent_is_asked_on_first_use(ai_app: App) -> None:
    u = ai_app.user(new_telegram_id())
    await onboard(u)
    await u.send("🍽 Еда")
    assert "📷 Фото" in u.buttons() and "🎙 Голос" in u.buttons()
    await u.tap("✍️ Написать")
    assert "с помощью ИИ" in u.screen() and "🤖 Разрешить ИИ" in u.buttons()
    await u.tap("🤖 Разрешить ИИ")
    assert "Напишите как удобно" in u.screen()
    out = joined(await u.send("гречка 200 г и котлета"))
    assert "⏳ Разбираю…" in out and "похоже, это:" in out and "Тестовый режим ИИ" in out
    assert u.buttons()[0] == "✓ Всё верно"
    await u.tap("✏️ Изменить")
    await u.tap("⭐ Запомнить как блюдо")
    assert "«Обед на работе» — в «Моих блюдах»" in joined(await u.send("Обед на работе"))
    confirm = u.find("✓ Всё верно")
    assert "✓ Записано" in joined(await u.tap("✓ Всё верно"))
    assert "уже обработан" in joined(await u.press(*confirm))

    # Photos need a separate consent; a photo is never saved without a preview.
    img = io.BytesIO()
    Image.new("RGB", (64, 64), (10, 200, 10)).save(img, format="JPEG")
    out = joined(await u.send_photo(img.getvalue(), caption="обед"))
    assert "📷 Фото и голос" in out and "Google Gemini" in out
    await u.tap("🤖 Разрешить ИИ")
    out = joined(await u.send_photo(img.getvalue(), caption="обед"))
    assert "⏳ Смотрю на фото…" in out and "mock: блюдо на фото" in out
    assert "не удалось прочитать" in joined(await u.send_photo(b"not an image")).lower()
    out = joined(await u.send_voice(b"OggS" + b"\x00" * 100))
    assert "🎙 «гречка 200 г и котлета»" in out
    assert "слишком" in joined(await u.send_voice(b"OggS", duration=600)).lower()

    # An AI-drafted activity needs confirmation.
    await u.send("🏋️ Тренировка")
    await u.tap("＋ Создать")
    await u.tap("✨ Описать словами")
    out = joined(await u.send("Плавание. Хочу учитывать дистанцию"))
    assert "✨ Вот что получилось" in out and "• Дистанция" in out
    assert "готово" in joined(await u.tap("✓ Создать"))


async def test_media_without_ai(app: App) -> None:
    u = app.user(new_telegram_id())
    await onboard(u)
    out = joined(await u.send_photo(b"x"))
    assert "только с ИИ" in out and "✍️ Написать" in u.buttons()


# --- security and robustness (P0 acceptance) ---------------------------------------------------


async def test_isolation_restart_and_forged_callbacks(app: App) -> None:
    a, b = app.user(new_telegram_id()), app.user(new_telegram_id())
    await onboard(a, tz_text="Europe/Moscow", target="2000")
    await onboard(b)
    await log_food(a, "суп 300 г")
    await a.send("/weight")
    await a.send("72,4")
    await a.send("🏋️ Тренировка")
    await a.tap("＋ Создать")
    await a.tap("🏋️ Зал")
    await a.tap("Всё тело")
    await a.send("присед 80x5 80x5")
    await a.tap("✓ Сохранить")

    async with app.sm() as s:
        ua = await resolve_user(s, a.id)
        a_template = (await ActivityService(s, ua).list_templates())[0].id
        a_food = (await s.execute(select(FoodEntry.id))).scalars().first()
        a_draft = (await s.execute(select(FoodEntry.draft_id))).scalars().first()
    assert a_food is not None and a_draft is not None
    for forged in (
        Ac(action="rec_tpl", id=a_template),
        En(action="del", kind="food", id=a_food),
        Fr(a="ok", d=a_draft, v=1),
        Fr(a="edit", d=a_draft, v=1),
        Fm(a="meal", id=a_template),
        Ac(action="tpl", id=a_template),
    ):
        out = joined(await b.press(forged.pack()))
        assert "не найдена" in out or "уже обработан" in out, forged
    await b.send("📊 Мой день")
    assert "Ничего не записано" in b.screen() and "72,4" not in b.screen()

    await app.restart()
    a, b = app.user(a.id), app.user(b.id)
    await a.send("📊 Мой день")
    assert "Записей: 1, калории неизвестны" in a.screen() and "72,4 кг" in a.screen()
    await b.send("📊 Мой день")
    assert "Ничего не записано" in b.screen()


async def test_duplicate_update_is_processed_once(app: App) -> None:
    u = app.user(new_telegram_id())
    await onboard(u)
    await u.send("🍽 Еда")
    await u.tap("✍️ Написать")
    update_id = 990_000_000 + u.id % 1_000_000
    await u.send("чай 200 мл", update_id=update_id)
    assert await u.send("чай 200 мл", update_id=update_id) == []
    confirm = u.find("✓ Сохранить")
    await u.press(*confirm)
    await u.press(*confirm)
    async with app.sm() as s:
        user = await resolve_user(s, u.id)
        count = select(func.count()).where(FoodEntry.owner_id == user.id)
        assert (await s.execute(count)).scalar_one() == 1


async def test_corrections_undo_and_restore(app: App) -> None:
    u = app.user(new_telegram_id())
    await onboard(u, target="1 800")
    await log_food(u, "суп 300 г, хлеб")
    await u.send("/fix")
    assert u.screen().startswith("✏️ Записи за сегодня")
    await u.tap("✏️ 2")
    assert "Нужно число" in joined(await u.send("abc"))
    assert "✓ Исправлено: хлеб — 210 ккал" in joined(await u.send("210"))
    await u.tap("← К записям")
    await u.tap("🗑 1")
    await u.tap("↶ Восстановить")
    await u.send("📊 Мой день")
    assert "210 / 1 800 ккал" in u.screen() and "Без калорий: 1 — итог неполный" in u.screen()


async def test_strong_import_export_and_delete(app: App) -> None:
    u = app.user(new_telegram_id())
    await onboard(u)
    await u.tap("⋯ Ещё")
    await u.tap("⚙️ Настройки")
    await u.tap("🔐 Данные")
    await u.tap("📥 Импорт из Strong")
    assert "слишком большой" in joined(await u.send_document(b"x" * 3_000_001, "big.csv"))
    assert "Не похоже на CSV" in joined(await u.send_document(b"MZ\x90\x00", "evil.exe"))
    out = joined(await u.send_document(STRONG_CSV.encode(), "strong.csv"))
    assert "Найдено тренировок: 2" in out and "Строк с ошибками: 2" in out
    assert "Импортировано тренировок: 2" in joined(await u.tap("📥 Импортировать"))

    await u.send("🏠 Главное")
    await u.tap("⋯ Ещё")
    await u.tap("⚙️ Настройки")
    await u.tap("🔐 Данные")
    await u.tap("📤 Скачать мои данные")
    payload = json.loads(u.sent_documents()[-1].document.data)
    assert len(payload["workout_sessions"]) == 2 and payload["format"] == "ritm-export-v1"

    await u.tap("🗑 Удалить всё")
    assert "не подтверждено" in joined(await u.send("да"))
    assert "удалены" in joined(await u.send("УДАЛИТЬ"))
    async with app.sm() as s:
        user = await resolve_user(s, u.id)
        assert user.onboarding_step == "language"
        assert (await s.execute(select(WorkoutSession))).scalars().all() == []
