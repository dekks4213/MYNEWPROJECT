"""Scripted AI provider for tests: returns exactly what a test prepares."""

from __future__ import annotations

from typing import Any

from fitcoach.ai.types import (
    ActivitySchemaDraft,
    FoodParse,
    ProviderResult,
    WorkoutParse,
)


class ScriptedProvider:
    name = "scripted"
    is_mock = False
    model = "scripted-model"

    def __init__(self, **responses: Any) -> None:
        self.responses = responses
        self.calls: list[str] = []
        self.last_media: bytes | None = None

    def _get(self, key: str) -> Any:
        self.calls.append(key)
        value = self.responses[key]
        if isinstance(value, Exception):
            raise value
        return value

    async def parse_food_text(self, text: str, language: str) -> ProviderResult[FoodParse]:
        return ProviderResult(self._get("food"), self.name, self.model, 100, 50)

    async def analyze_food_image(
        self, image: bytes, mime: str, caption: str | None, language: str
    ) -> ProviderResult[FoodParse]:
        self.last_media = image
        return ProviderResult(self._get("image"), self.name, self.model, 300, 60)

    async def parse_food_voice(
        self, audio: bytes, mime: str, language: str
    ) -> ProviderResult[FoodParse]:
        self.last_media = audio
        return ProviderResult(self._get("voice"), self.name, self.model, 200, 60)

    async def parse_workout_text(self, text: str, language: str) -> ProviderResult[WorkoutParse]:
        return ProviderResult(self._get("workout"), self.name, self.model, 100, 80)

    async def build_activity_draft(
        self, text: str, language: str
    ) -> ProviderResult[ActivitySchemaDraft]:
        return ProviderResult(self._get("activity"), self.name, self.model, 100, 80)

    async def check(self) -> list[str]:
        return []
