"""Deterministic mock provider for development and tests.

It never produces nutrient numbers or invented sets. The UI labels its output as mock; it
must never be presented as a live integration.
"""

from __future__ import annotations

import asyncio

from fitcoach.ai.types import (
    ActivitySchemaDraft,
    FieldDraftAI,
    FoodItemAI,
    FoodParse,
    ProviderResult,
    WorkoutParse,
)
from fitcoach.domain.fields import FieldType
from fitcoach.domain.food import parse_food_line
from fitcoach.domain.units import ParseError
from fitcoach.domain.workout import ActivityKind, parse_strength_text


class MockProvider:
    name = "mock"
    is_mock = True

    def __init__(self, fail: bool = False, delay: float = 0.0, garbage: bool = False) -> None:
        self.fail = fail
        self.delay = delay
        self.garbage = garbage
        self.calls = 0

    async def _tick(self) -> None:
        self.calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.fail:
            raise RuntimeError("mock provider failure")
        if self.garbage:
            from fitcoach.ai.types import AIUnavailableError

            raise AIUnavailableError("invalid_output")

    def _food(self, text: str) -> FoodParse:
        items = [
            FoodItemAI(
                name=p.name,
                amount=p.amount,
                unit=p.unit,
                amount_text=p.amount_text,
                missing=[] if p.amount is not None else ["amount"],
            )
            for p in parse_food_line(text)
        ]
        return FoodParse(items=items)

    async def parse_food_text(self, text: str, language: str) -> ProviderResult[FoodParse]:
        await self._tick()
        return ProviderResult(self._food(text), provider=self.name, model=None)

    async def analyze_food_image(
        self, image: bytes, mime: str, caption: str | None, language: str
    ) -> ProviderResult[FoodParse]:
        await self._tick()
        name = "mock: блюдо на фото" if language == "ru" else "mock: dish in photo"
        parse = FoodParse(items=[FoodItemAI(name=name, uncertain=True, missing=["amount"])])
        return ProviderResult(parse, provider=self.name, model=None)

    async def parse_food_voice(
        self, audio: bytes, mime: str, language: str
    ) -> ProviderResult[FoodParse]:
        await self._tick()
        transcript = "гречка 200 г и котлета"
        parse = self._food(transcript).model_copy(update={"transcript": transcript})
        return ProviderResult(parse, provider=self.name, model=None)

    async def parse_workout_text(self, text: str, language: str) -> ProviderResult[WorkoutParse]:
        await self._tick()
        try:
            body = parse_strength_text(text)
            parse = WorkoutParse(kind=ActivityKind.STRENGTH, blocks=list(body.blocks))
        except ParseError:
            parse = WorkoutParse(notes=text[:300])
        return ProviderResult(parse, provider=self.name, model=None)

    async def build_activity_draft(
        self, text: str, language: str
    ) -> ProviderResult[ActivitySchemaDraft]:
        await self._tick()
        name = text.strip().split(".")[0][:60] or "Activity"
        draft = ActivitySchemaDraft(
            name=name,
            fields=[
                FieldDraftAI(
                    label="Дистанция" if language == "ru" else "Distance",
                    type=FieldType.DECIMAL,
                    unit="km",
                )
            ],
        )
        return ProviderResult(draft, provider=self.name, model=None)

    async def check(self) -> list[str]:
        return []
