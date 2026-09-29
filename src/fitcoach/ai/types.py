"""Typed AI task contracts. Everything a provider returns is a *draft* validated here and
again by services. Models never supply database IDs or owners."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from fitcoach.domain.fields import FieldType
from fitcoach.domain.food import MealType, Per100, Unit
from fitcoach.domain.workout import ActivityKind, Block

Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]
Short = Annotated[str, StringConstraints(strip_whitespace=True, max_length=60)]
Question = Annotated[str, StringConstraints(strip_whitespace=True, max_length=300)]


class _Draft(BaseModel):
    model_config = ConfigDict(extra="forbid")


class FoodItemAI(_Draft):
    name: Name = Field(description="Food as the user said it, in the user's language")
    name_en: Short | None = Field(
        default=None, description="Generic English name for database lookup, e.g. 'rice, cooked'"
    )
    amount: Decimal | None = Field(
        default=None, ge=0, le=10000, description="Stated amount, null if not stated"
    )
    unit: Unit | None = Field(default=None, description="Unit of the stated amount")
    amount_text: Short | None = Field(default=None, description="Amount wording as stated")
    grams_estimate: Decimal | None = Field(
        default=None,
        ge=0,
        le=5000,
        description="Rough gram estimate ONLY for non-mass amounts (cup, piece, 'a little') "
        "or for a photo; null when unknown",
    )
    preparation: Short | None = Field(default=None, description="raw, boiled, fried, ...")
    brand: Short | None = Field(default=None, description="Only if the user named a brand")
    uncertain: bool = Field(default=False, description="True if identification is unsure")
    missing: list[Literal["amount", "preparation", "identity"]] = Field(
        default_factory=list, max_length=3
    )
    stated_energy_kcal: Decimal | None = Field(
        default=None, ge=0, le=10000, description="Only calories the user explicitly stated"
    )
    reference_per_100g: Per100 | None = Field(
        default=None,
        description="Typical generic composition per 100 g for common unbranded foods in the "
        "stated preparation; null for branded, mixed or unknown foods",
    )


class CopyRequest(_Draft):
    day_offset: int = Field(ge=-14, le=0, description="0 today, -1 yesterday")
    meal_type: MealType | None = None
    exclude: list[Short] = Field(default_factory=list, max_length=10)


class FoodParse(_Draft):
    items: list[FoodItemAI] = Field(default_factory=list, max_length=15)
    meal_type: MealType | None = None
    copy_request: CopyRequest | None = Field(
        default=None, description="Set when the user refers to a previous meal"
    )
    clarification: Question | None = Field(
        default=None, description="One short question if something important is missing"
    )
    transcript: Annotated[str, StringConstraints(max_length=2000)] | None = Field(
        default=None, description="Voice input only: verbatim transcript"
    )


class WorkoutParse(_Draft):
    kind: ActivityKind = ActivityKind.CUSTOM
    title: Short | None = None
    duration_min: Decimal | None = Field(default=None, ge=0, le=1440)
    distance_km: Decimal | None = Field(default=None, ge=0, le=2000)
    blocks: list[Block] = Field(default_factory=list, max_length=20)
    notes: Annotated[str, StringConstraints(max_length=300)] | None = None
    clarification: Question | None = None


class FieldDraftAI(_Draft):
    label: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=40)]
    type: FieldType
    unit: Annotated[str, StringConstraints(max_length=12)] | None = None
    choices: list[Annotated[str, StringConstraints(min_length=1, max_length=40)]] | None = Field(
        default=None, max_length=20
    )
    duration_format: Literal["h:mm", "mm:ss"] | None = None


class ActivitySchemaDraft(_Draft):
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=60)]
    kind: ActivityKind = ActivityKind.CUSTOM
    fields: list[FieldDraftAI] = Field(default_factory=list, max_length=19)
    uses_blocks: bool = Field(default=False, description="True if intervals/sets matter")


class Media(StrEnum):
    NONE = "none"
    IMAGE = "image"
    AUDIO = "audio"


@dataclass(frozen=True)
class ProviderResult[T]:
    value: T
    provider: str
    model: str | None
    input_tokens: int | None = None
    output_tokens: int | None = None


class AIUnavailableError(Exception):
    """Manual flows must keep working when this is raised. `code` maps to `ai.<code>`.

    `billable=False` means the provider certainly did not process the request (connection
    failure, rate limit), so the reserved budget may be released.
    """

    def __init__(self, code: str, *, billable: bool = True) -> None:
        super().__init__(code)
        self.code = code
        self.billable = billable


class AIProvider(Protocol):
    name: str
    is_mock: bool

    async def parse_food_text(self, text: str, language: str) -> ProviderResult[FoodParse]: ...

    async def analyze_food_image(
        self, image: bytes, mime: str, caption: str | None, language: str
    ) -> ProviderResult[FoodParse]: ...

    async def parse_food_voice(
        self, audio: bytes, mime: str, language: str
    ) -> ProviderResult[FoodParse]: ...

    async def parse_workout_text(
        self, text: str, language: str
    ) -> ProviderResult[WorkoutParse]: ...

    async def build_activity_draft(
        self, text: str, language: str
    ) -> ProviderResult[ActivitySchemaDraft]: ...

    async def check(self) -> list[str]:
        """Verify configuration; return the configured models that are unavailable."""
        ...
