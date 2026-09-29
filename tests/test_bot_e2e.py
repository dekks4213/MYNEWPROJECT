"""Acceptance flows through the real Dispatcher, middleware, services and PostgreSQL.

Telegram itself is replaced by a recording session: these are integration tests, not a live
Telegram verification. AI uses the deterministic mock provider or a scripted provider.
"""

from __future__ import annotations

import io
import json
from collections.abc import AsyncIterator

import pytest
from aiogram import Bot
from PIL import Image
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from fitcoach.ai.gateway import AIGateway
from fitcoach.ai.mock import MockProvider
from fitcoach.bot.app import build_dispatcher
from fitcoach.bot.ui import Ac, En, Fm, Fr
from fitcoach.config import Settings
from fitcoach.db.models import FoodEntry, WorkoutSession
from fitcoach.db.session import create_engine, create_sessionmaker
from fitcoach.services.activities import ActivityService
from fitcoach.services.users import resolve_user
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


async def onboard(
    u: TgUser, tz_text: str | None = None, target: str | None = None, ai: str = "Без ИИ, вручную"
) -> None:
    out = await u.send("/start")
    assert any("РИТМ" in t for t in out)
    await u.tap("Русский")
    await u.tap("Мне 18 или больше")
    out = await u.tap(ai)
    assert any("Где вы живёте" in t for t in out)
    if tz_text:
        await u.send(tz_text)
    else:
        await u.tap("Лондон")
    await u.tap("Наладить привычки")
    out = await u.send(target) if target else await u.tap("Без цели")
    assert any("Готово" in t for t in out)


def joined(out: list[str]) -> str:
    return "\n".join(out)


async def test_p0_acceptance_two_users_restart_and_isolation(app: App) -> None:
    a, b = app.user(new_telegram_id()), app.user(new_telegram_id())

    # Features are gated until onboarding is finished; progress is resumable.
    assert "Выберите язык" in joined(await a.send("🍽 Записать еду"))
    await onboard(a, "Europe/Moscow", "2000")
    await onboard(b)

    # A: own label product, then a text entry resolved deterministically (no AI).
    await a.send("🍽 Записать еду")
    await a.tap("Мой продукт")
    assert "добавлен" in joined(await a.send("Овсянка; 370; 13/7/60"))
    await a.send("🍽 Записать еду")
    await a.tap("Текстом")
    out = joined(await a.send("овсянка 100 г, банан"))
    assert "1. овсянка · 100 г · 370 ккал" in out and "взвешено/этикетка" in out
    assert "2. банан · количество? · ккал неизвестны" in out
    await a.tap("✏️ 2.")
    out = joined(await a.send("105 ккал"))
    assert "Итого: 475 ккал" in out
    assert "Записано позиций: 2, 475 ккал" in joined(await a.tap("✅ Сохранить"))

    # A: weight with decimal comma; invalid input is rejected with a clear message.
    await a.send("📅 Мой день")
    await a.tap("⚖️ Вес")
    assert "72,4 кг" in joined(await a.send("72,4"))
    await a.send("/weight")
    assert "Вес должен быть" in joined(await a.send("7"))
    await a.send("/cancel")

    # A: custom activity with duration + one custom numeric field, no migration involved.
    await a.send("🏋️ Тренировки")
    await a.tap("Создать тренировку")
    await a.tap("Свой вид")
    await a.send("Эндуро")
    await a.tap("Добавить поле")
    await a.send("Круги")
    await a.tap("Целое число")
    assert "Круги — Целое число, кр" in joined(await a.send("кр"))
    assert "создан" in joined(await a.tap("Сохранить вид"))

    # A: template with targets, planned for today.
    await a.tap("Новый шаблон")
    await a.send("Трасса")
    await a.tap("Пропустить")  # no block plan
    await a.send("60")  # planned duration
    assert "Круги: 10 кр" in joined(await a.send("10"))
    assert "сохранён" in joined(await a.tap("Сохранить"))
    await a.tap("На сегодня")

    summary = joined(await a.send("📅 Мой день"))
    assert "475 / 2 000 ккал" in summary
    assert "✓" not in summary  # a plan is not completed work
    assert "По плану: Трасса" in summary

    # A: record the planned workout with actual values only.
    await a.send("🏋️ Тренировки")
    await a.tap("Начать тренировку")
    assert "План: 1:00" in joined(await a.tap("📅 Трасса"))
    assert "План: 10 кр" in joined(await a.send("1:30"))
    await a.tap("Пропустить")
    assert "Тренировка записана" in joined(await a.tap("Сохранить"))
    assert "неактуальна" in joined(await a.press(a.button("Сохранить")))  # double tap

    summary = joined(await a.send("📅 Мой день"))
    assert "✓ Эндуро — Трасса · 90 мин" in summary
    assert "По плану" not in summary

    # B cannot see or act on A's records, even with forged callback data.
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
    out = joined(await b.send("📅 Мой день"))
    assert "Пока ничего не записано" in out and "Трасса" not in out

    # Restart the "service": data persists and stays separated.
    await app.restart()
    a, b = app.user(a.id), app.user(b.id)
    summary_a = joined(await a.send("📅 Мой день"))
    assert "475 / 2 000 ккал" in summary_a and "Трасса" in summary_a
    assert "72,4 кг" in summary_a
    summary_b = joined(await b.send("📅 Мой день"))
    assert "Пока ничего не записано" in summary_b and "72,4" not in summary_b


async def test_duplicate_update_is_processed_once(app: App) -> None:
    u = app.user(new_telegram_id())
    await onboard(u)
    await u.send("🍽 Записать еду")
    await u.tap("Текстом")
    update_id = 990_000_000 + u.id % 1_000_000
    await u.send("чай 200 мл", update_id=update_id)
    assert await u.send("чай 200 мл", update_id=update_id) == []
    confirm = u.button("✅ Сохранить")
    await u.press(confirm)
    await u.press(confirm)
    async with app.sm() as s:
        user = await resolve_user(s, u.id)
        count = (
            await s.execute(select(func.count()).where(FoodEntry.owner_id == user.id))
        ).scalar_one()
    assert count == 1


async def test_corrections_undo_and_draft_editing(app: App) -> None:
    u = app.user(new_telegram_id())
    await onboard(u, target="1800")
    await u.send("🍽 Записать еду")
    await u.tap("Текстом")
    await u.send("суп 300 г, хлеб")
    await u.tap("❌")  # remove "суп"
    await u.tap("➕ Добавить")
    out = joined(await u.send("салат 150 г"))
    assert "1. хлеб" in out and "2. салат" in out and "суп" not in out
    out = joined(await u.tap("🍽 "))  # cycle meal type
    assert "Приём пищи:" in out
    await u.tap("✅ Сохранить")
    await u.send("/fix")
    await u.tap("✏️ 1")
    assert "Нужно число" in joined(await u.send("abc"))
    assert "Исправлено: хлеб — 210 ккал" in joined(await u.send("210"))
    await u.send("/fix")
    await u.tap("🗑 2")
    await u.tap("Восстановить")
    out = joined(await u.send("📅 Мой день"))
    assert "210 / 1 800 ккал" in out and "Ещё записей без калорий: 1" in out


async def test_ai_text_photo_voice_flows_are_drafts(ai_app: App) -> None:
    u = ai_app.user(new_telegram_id())
    await onboard(u, ai="С ИИ: текст, фото и голос")
    await u.send("🍽 Записать еду")
    await u.tap("Текстом")
    out = joined(await u.send("гречка 200 г и котлета"))
    assert "Черновик" in out and "Тестовый режим ИИ" in out
    assert "Пока ничего не записано" in joined(await u.send("📅 Мой день"))  # not saved yet
    await u.tap("⭐ Сохранить как блюдо")
    assert "сохранено" in joined(await u.send("Обед на работе"))
    confirm = u.button("✅ Сохранить")
    assert "Записано позиций: 2" in joined(await u.press(confirm))
    assert "уже обработан" in joined(await u.press(confirm))

    img = io.BytesIO()
    Image.new("RGB", (64, 64), (10, 200, 10)).save(img, format="JPEG")
    out = joined(await u.send_photo(img.getvalue(), caption="обед"))
    assert "Смотрю на фото" in out and "mock: блюдо на фото" in out and "количество?" in out
    assert "не удалось прочитать" in joined(await u.send_photo(b"not an image")).lower()

    out = joined(await u.send_voice(b"OggS" + b"\x00" * 100))
    assert "Распознано: «гречка 200 г и котлета»" in out
    assert "слишком" in joined(await u.send_voice(b"OggS", duration=600)).lower()

    await u.send("🍽 Записать еду")
    await u.tap("Мои блюда")
    out = joined(await u.tap("Обед на работе"))
    assert "1. гречка" in out and "2. котлета" in out


async def test_media_without_ai_or_consent(app: App, ai_app: App) -> None:
    u = app.user(new_telegram_id())
    await onboard(u)
    await u.send("🍽 Записать еду")
    with pytest.raises(AssertionError):
        u.button("📷 Фото")  # unfinished/unavailable entries are not shown
    assert "только с ИИ" in joined(await u.send_photo(b"x"))
    v = ai_app.user(new_telegram_id())
    await onboard(v, ai="С ИИ только текст")
    out = joined(await v.send_voice(b"OggS"))
    assert "Разрешить" in out
    await v.tap("Разрешить ИИ для фото")
    assert "Распознано" in joined(await v.send_voice(b"OggS" + b"\x00" * 10))


async def test_workout_text_blocks_programs_and_history(ai_app: App) -> None:
    u = ai_app.user(new_telegram_id())
    await onboard(u, ai="С ИИ: текст, фото и голос")
    await u.send("🏋️ Тренировки")
    await u.tap("Записать выполненную")
    await u.tap("Описать текстом")
    out = joined(await u.send("жим 60 кг 10 10 8, тяга вертикального блока 70 кг 12 12 10"))
    assert "жим: 2×(60×10); 60×8" in out and "Вид: не выбран" in out
    out = joined(await u.tap("Создать вид «Силовая»"))
    assert "Вид: Силовая" in out
    assert "Тренировка записана" in joined(await u.tap("✅ Сохранить"))

    # Template with a block plan, weekly planning, program, guided start.
    await u.send("🏋️ Тренировки")
    await u.tap("Мои программы")
    await u.tap("Новая программа")
    await u.send("Зал 3 раза в неделю")
    await u.send("🏋️ Тренировки")
    await u.tap("Мои шаблоны")
    await u.tap("Новый шаблон")
    await u.tap("Силовая")
    await u.send("Верх тела")
    out = joined(await u.send("Разминка\nжим 20x15 40x10\nОсновная\nжим 60x10 60x10 60x8"))
    assert "Разминка" in out and "жим: 20×15 (разм.); 40×10 (разм.)" in out
    await u.tap("Пропустить")  # duration target
    await u.tap("Пропустить")  # RPE target
    await u.tap("Сохранить")
    tpl = u.button("▶️ Начать")
    await u.send("🏋️ Тренировки")
    await u.tap("Мои шаблоны")
    await u.tap("Верх тела")
    await u.tap("В программу")
    await u.tap("Зал 3 раза в неделю")
    await u.tap("Верх тела")
    await u.tap("По дням недели")
    await u.tap("Пн")
    await u.tap("Чт")
    assert "Запланировано тренировок" in joined(await u.tap("✅ Запланировать"))

    assert "1/2. По плану: жим: 20×15 (разм.); 40×10 (разм.)" in joined(await u.press(tpl))
    out = joined(await u.tap("Пропустить"))  # warm-up not logged
    assert "2/2. По плану: жим: 2×(60×10); 60×8" in out
    await u.tap("✓ Как в плане")  # explicit "done as planned" for item 2
    await u.send("58")  # duration
    out = joined(await u.send("8"))  # RPE
    assert "жим: 2×(60×10); 60×8" in out
    assert "Тренировка записана" in joined(await u.tap("Сохранить"))

    await u.send("📈 История")
    out = joined(await u.tap("По видам"))
    assert "Силовая: 2 раз" in out and "объём" in out
    out = joined(await u.tap("Тренировки"))
    assert "Силовая — Верх тела" in out

    # AI-proposed activity schema requires confirmation.
    await u.send("🏋️ Тренировки")
    await u.tap("Создать тренировку")
    await u.tap("Описать словами")
    out = joined(await u.send("Плавание. Хочу учитывать дистанцию"))
    assert "Предлагаемая схема" in out and "Дистанция — Число, km" in out
    assert "создан" in joined(await u.tap("✅ Создать"))


async def test_strong_import_export_and_delete(app: App) -> None:
    u = app.user(new_telegram_id())
    await onboard(u)
    await u.send("🏋️ Тренировки")
    await u.tap("Импорт из Strong")
    assert "слишком большой" in joined(await u.send_document(b"x" * 3_000_001, "big.csv"))
    assert "Не похоже на CSV" in joined(await u.send_document(b"MZ\x90\x00", "evil.exe"))
    assert "Не похоже на CSV" in joined(await u.send_document(b"\x00\x01garbage", "a.csv"))
    out = joined(await u.send_document(STRONG_CSV.encode(), "strong.csv"))
    assert "Найдено тренировок: 2" in out and "Строк с ошибками: 2" in out
    assert "Импортировано тренировок: 2" in joined(await u.tap("Импортировать"))
    await u.send("🏋️ Тренировки")
    await u.tap("Импорт из Strong")
    assert "уже импортирован" in joined(await u.send_document(STRONG_CSV.encode(), "strong.csv"))

    await u.send("⚙️ Настройки")
    await u.tap("Экспорт данных")
    doc = u.sent_documents()[-1]
    payload = json.loads(doc.document.data)
    assert len(payload["workout_sessions"]) == 2 and payload["format"] == "ritm-export-v1"

    await u.send("⚙️ Настройки")
    await u.tap("Удалить аккаунт")
    assert "не подтверждено" in joined(await u.send("да"))
    assert "удалены" in joined(await u.send("УДАЛИТЬ"))
    async with app.sm() as s:
        user = await resolve_user(s, u.id)
        assert user.onboarding_step == "language"
        rows = (await s.execute(select(WorkoutSession))).scalars().all()
        assert rows == []


async def test_reminders_settings(app: App) -> None:
    u = app.user(new_telegram_id())
    await onboard(u, "Europe/Moscow")
    await u.send("⚙️ Настройки")
    await u.tap("Напоминания")
    await u.tap("Добавить")
    await u.tap("Взвешивание")
    assert "Не понял время" in joined(await u.send("25:99"))
    await u.send("08:30")
    assert "08:30 сохранено" in joined(await u.tap("По будням"))
    await u.send("⚙️ Настройки")
    await u.tap("Тихие часы")
    assert "сохранены" in joined(await u.send("22:00-07:00"))
    await u.send("⚙️ Настройки")
    out = joined(await u.tap("Напоминания"))
    assert "🔔 08:30 · Взвешивание · По будням" in out


async def test_english_localization(app: App) -> None:
    u = app.user(new_telegram_id())
    await u.send("/start")
    assert "18 or older" in joined(await u.tap("English"))
    await u.tap("I'm 18 or older")
    await u.tap("No AI")
    await u.tap("London")
    await u.tap("Skip")
    assert "All set" in joined(await u.tap("No calorie target"))
    out = joined(await u.send("📅 My day"))
    assert "Today" in out and "Nothing logged yet" in out
    await u.send("🍽 Log food")
    await u.tap("Text")
    out = joined(await u.send("rice 150 g"))
    assert "Draft" in out and "kcal unknown" in out


async def test_buttons_instead_of_typing(app: App) -> None:
    u = app.user(new_telegram_id())
    await u.send("/start")
    await u.tap("Русский")
    await u.tap("Мне 18 или больше")
    await u.tap("Без ИИ, вручную")
    out = joined(await u.tap("Москва"))  # city, not an IANA code; no "units" question
    assert "цель" in out.lower() and "Метрическ" not in out
    await u.tap("Наладить привычки")
    assert "Готово" in joined(await u.tap("2200"))  # kcal preset

    # Weight: first time typed, then quick buttons around the last value.
    await u.send("/weight")
    await u.send("80")
    await u.send("/weight")
    assert "80,2" in joined(await u.tap("80,2"))

    # Food draft amounts via buttons.
    await u.send("🍽 Записать еду")
    await u.tap("Мой продукт")
    await u.send("Рис; 130; 2,7/0,3/28")
    await u.send("🍽 Записать еду")
    await u.tap("Текстом")
    await u.send("рис 100 г")
    await u.tap("✏️ 1.")
    assert "260 ккал" in joined(await u.tap("×2"))
    await u.tap("✏️ 1.")
    assert "195 ккал" in joined(await u.tap("150 г"))
    await u.tap("✅ Сохранить")

    # Workout: duration preset, effort 1–10 buttons, skip the rest.
    await u.send("🏋️ Тренировки")
    await u.tap("Создать тренировку")
    await u.tap("Силовая")
    await u.tap("✅ Создать")
    await u.tap("✅ Записать выполненную")
    out = joined(await u.tap("45 мин"))
    assert "Насколько было тяжело" in out and "RPE" not in out
    await u.tap("7")
    await u.tap("Пропустить")  # no exercises
    assert "Тренировка записана" in joined(await u.tap("Сохранить"))
    out = joined(await u.send("📅 Мой день"))
    assert "✓ Силовая · 45 мин" in out and "2 200 ккал" in out

    # Reminder time and quiet hours via buttons.
    await u.send("⚙️ Настройки")
    await u.tap("Напоминания")
    await u.tap("Добавить")
    await u.tap("Записать еду")
    await u.tap("20:00")
    assert "20:00 сохранено" in joined(await u.tap("Каждый день"))
    await u.send("⚙️ Настройки")
    await u.tap("Тихие часы")
    assert "сохранены" in joined(await u.tap("23:00–08:00"))


async def test_copy_meal_buttons_pack_and_work(app: App) -> None:
    u = app.user(new_telegram_id())
    await onboard(u)
    await u.send("🍽 Записать еду")
    await u.tap("Текстом")
    await u.send("суп 300 г")
    await u.tap("✅ Сохранить")
    await u.send("🍽 Записать еду")
    out = joined(await u.tap("Скопировать приём пищи"))
    assert "Какой приём пищи" in out
    for label in ("Сегодня: Завтрак", "Сегодня: Обед", "Сегодня: Ужин", "Сегодня: Перекус"):
        out = joined(await u.tap(label))
        if "суп" in out:
            break
    assert "1. суп" in out
