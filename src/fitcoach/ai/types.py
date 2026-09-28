from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Annotated, Protocol

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]
Grams = Annotated[Decimal, Field(ge=0, le=2000)]


class MealItemDraft(BaseModel):
    """One food item as *stated by the user*. Numbers are null unless the user gave them."""

    model_config = ConfigDict(extra="forbid")

    name: Name
    quantity_text: Annotated[str, StringConstraints(max_length=60)] | None = None
    energy_kcal: Annotated[Decimal, Field(ge=0, le=10000)] | None = None
    protein_g: Grams | None = None
    fat_g: Grams | None = None
    carbs_g: Grams | None = None


class MealDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[MealItemDraft] = Field(min_length=1, max_length=10)


@dataclass(frozen=True)
class ProviderResult[T]:
    value: T
    provider: str
    model: str | None
    input_tokens: int | None = None
    output_tokens: int | None = None


class AIUnavailableError(Exception):
    """Manual flows must keep working when this is raised. `code` maps to `ai.<code>`."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class AIProvider(Protocol):
    name: str
    is_mock: bool

    async def parse_meal(self, text: str, language: str) -> ProviderResult[MealDraft]: ...
