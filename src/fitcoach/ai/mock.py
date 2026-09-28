"""Deterministic mock provider for development and tests.

It splits text into item names and never produces nutrient numbers.
The UI labels its output as mock; it must never be presented as a live integration.
"""

from __future__ import annotations

import re

from fitcoach.ai.types import MealDraft, MealItemDraft, ProviderResult

_SPLIT = re.compile(r"\s*(?:,|;|\n|\bи\b|\band\b)\s*", re.IGNORECASE)


class MockProvider:
    name = "mock"
    is_mock = True

    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.calls = 0

    async def parse_meal(self, text: str, language: str) -> ProviderResult[MealDraft]:
        self.calls += 1
        if self.fail:
            raise RuntimeError("mock provider failure")
        names = [p for p in _SPLIT.split(text) if p][:10]
        items = [MealItemDraft(name=n[:120]) for n in names] or [MealItemDraft(name=text[:120])]
        return ProviderResult(MealDraft(items=items), provider=self.name, model=None)
