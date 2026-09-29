"""Opt-in live test: real Gemini -> gateway -> FoodService/WorkoutDraftService -> drafts.

Run with RITM_LIVE_GEMINI=1 plus GEMINI_API_KEY/GEMINI_MODEL/GEMINI_MEDIA_MODEL and
TEST_PG_ADMIN_URL. Optional RITM_LIVE_IMAGE / RITM_LIVE_AUDIO_WAV paths add media checks.
Never runs in normal test runs (costs money, needs network).
"""

from __future__ import annotations

import datetime as dt
import os
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from fitcoach.ai.gateway import build_gateway, verify_provider
from fitcoach.bot.ui import format_food_draft
from fitcoach.config import Settings
from fitcoach.domain.nutrition import Precision
from fitcoach.i18n import Translator
from fitcoach.services.food import FoodService
from fitcoach.services.media import prepare_image
from fitcoach.services.workout_drafts import WorkoutDraftService
from tests.conftest import make_user, open_user, requires_db

# Blocking file reads are fine in this opt-in, sequential live test.
pytestmark = [
    requires_db,
    pytest.mark.skipif(os.environ.get("RITM_LIVE_GEMINI") != "1", reason="live test opt-in"),
]


async def test_live_food_and_workout_drafts(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    settings = Settings(ai_provider="gemini", ai_user_daily_calls=10)
    gateway = build_gateway(settings)
    assert (await verify_provider(gateway)).endswith("ok")
    tr = Translator("ru")
    tid = await make_user(sessionmaker, "Europe/Moscow")
    async with sessionmaker() as s:
        user = await open_user(s, tid)
        user.ai_text_consent_at = user.ai_media_consent_at = dt.datetime.now(dt.UTC)
        food = FoodService(s, user, gateway=gateway)
        draft = await food.draft_from_text(
            "200 грамм курицы, риса примерно стакан и немного овощей"
        )
        _, state = await food.get_draft(draft.id)
        print("\n" + format_food_draft(tr, state))
        chicken = state.items[0]
        assert chicken.grams == 200 and chicken.energy_kcal is not None
        assert chicken.precision is Precision.APPROXIMATE  # AI reference, never "measured"
        assert any(i.quantity_estimated or i.energy_kcal is None for i in state.items[1:])

        workout = await WorkoutDraftService(s, user, gateway).draft_from_text(
            "сегодня плавал 1200 метров, из них 5 по 100 кролем, отдыхал примерно минуту"
        )
        _, wstate = await WorkoutDraftService(s, user).get(workout.id)
        assert wstate.kind.value == "swimming" and wstate.distance_km == Decimal("1.2")

        if image_path := os.environ.get("RITM_LIVE_IMAGE"):
            image, mime = prepare_image(Path(image_path).read_bytes(), 8_000_000)
            photo = await food.draft_from_photo(image, mime, None)
            _, pstate = await food.get_draft(photo.id)
            print(format_food_draft(tr, pstate))
            assert pstate.items and all(i.uncertain for i in pstate.items)
        if audio_path := os.environ.get("RITM_LIVE_AUDIO_WAV"):
            voice = await food.draft_from_voice(Path(audio_path).read_bytes(), "audio/wav")
            _, vstate = await food.get_draft(voice.id)
            print(format_food_draft(tr, vstate))
            assert vstate.transcript and len(vstate.items) >= 3
        await s.commit()
