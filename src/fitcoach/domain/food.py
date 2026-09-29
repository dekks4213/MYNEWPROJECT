"""Food amounts, units and deterministic nutrient arithmetic.

Rules:
- Nutrients are scaled from a stated basis (per 100 g, per 100 ml, per serving).
- Unknown inputs stay None. A total over items is computed only from known values and the
  number of unknown contributors is reported separately (see domain.nutrition).
- Amounts that are not mass (pieces, cups, "a little") are never silently turned into grams:
  a gram value must come from the user, a label/serving size, or an explicitly *estimated*
  source that is shown as an estimate.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from fitcoach.domain.units import ParseError, parse_decimal

MAX_ITEMS = 15
MAX_GRAMS = Decimal(5000)


class Unit(StrEnum):
    G = "g"
    KG = "kg"
    ML = "ml"
    L = "l"
    PIECE = "piece"
    SERVING = "serving"
    CUP = "cup"
    TBSP = "tbsp"
    TSP = "tsp"
    SLICE = "slice"


class Basis(StrEnum):
    PER_100G = "100g"
    PER_100ML = "100ml"
    SERVING = "serving"


class MealType(StrEnum):
    BREAKFAST = "breakfast"
    LUNCH = "lunch"
    DINNER = "dinner"
    SNACK = "snack"


class NutrientSource(StrEnum):
    USER = "user"  # typed by the user for this entry
    LABEL = "label"  # user's saved label / catalog item
    USDA = "usda"
    OFF = "off"
    RECIPE = "recipe"
    AI_ESTIMATE = "ai_estimate"  # generic reference estimate proposed by the model


class Per100(BaseModel):
    """Nutrients for one basis unit. None = unknown."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    # Upper bounds allow per-serving values; per-100 g sanity is checked in validate_per100.
    energy_kcal: Decimal | None = Field(default=None, ge=0, le=5000)
    protein_g: Decimal | None = Field(default=None, ge=0, le=500)
    fat_g: Decimal | None = Field(default=None, ge=0, le=500)
    carbs_g: Decimal | None = Field(default=None, ge=0, le=500)
    fiber_g: Decimal | None = Field(default=None, ge=0, le=500)

    def is_empty(self) -> bool:
        return all(
            v is None
            for v in (self.energy_kcal, self.protein_g, self.fat_g, self.carbs_g, self.fiber_g)
        )


class FixedTotals(BaseModel):
    """Totals for an eaten portion when no per-basis composition is known."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    energy_kcal: Decimal | None = Field(default=None, ge=0, le=100_000)
    protein_g: Decimal | None = Field(default=None, ge=0, le=10_000)
    fat_g: Decimal | None = Field(default=None, ge=0, le=10_000)
    carbs_g: Decimal | None = Field(default=None, ge=0, le=10_000)
    fiber_g: Decimal | None = Field(default=None, ge=0, le=10_000)


def validate_per100(per: Per100, basis: Basis) -> None:
    """Physical plausibility for per-100 g/ml values (900 kcal ~ pure fat)."""
    if basis is Basis.SERVING:
        return
    if per.energy_kcal is not None and per.energy_kcal > 950:
        raise ParseError("implausible_nutrients")
    macros = [v for v in (per.protein_g, per.fat_g, per.carbs_g) if v is not None]
    if any(v > 100 for v in macros) or sum(macros, Decimal(0)) > 105:
        raise ParseError("implausible_nutrients")


@dataclass(frozen=True)
class Nutrients:
    energy_kcal: Decimal | None = None
    protein_g: Decimal | None = None
    fat_g: Decimal | None = None
    carbs_g: Decimal | None = None
    fiber_g: Decimal | None = None


_MASS_TO_G = {Unit.G: Decimal(1), Unit.KG: Decimal(1000)}
_VOLUME_TO_ML = {Unit.ML: Decimal(1), Unit.L: Decimal(1000)}


def to_grams(amount: Decimal | None, unit: Unit | None) -> Decimal | None:
    """Exact conversion for mass units only."""
    if amount is None or unit is None or unit not in _MASS_TO_G:
        return None
    return amount * _MASS_TO_G[unit]


def to_ml(amount: Decimal | None, unit: Unit | None) -> Decimal | None:
    if amount is None or unit is None or unit not in _VOLUME_TO_ML:
        return None
    return amount * _VOLUME_TO_ML[unit]


def _q(value: Decimal | None) -> Decimal | None:
    return None if value is None else value.quantize(Decimal("0.1"))


def scale(
    per: Per100,
    basis: Basis,
    *,
    grams: Decimal | None = None,
    ml: Decimal | None = None,
    servings: Decimal | None = None,
) -> Nutrients | None:
    """Scale per-basis nutrients to a consumed quantity. Returns None if the quantity does not
    match the basis (e.g. grams given for a per-100 ml product)."""
    if basis is Basis.PER_100G:
        if grams is None:
            return None
        factor = grams / 100
    elif basis is Basis.PER_100ML:
        if ml is None:
            return None
        factor = ml / 100
    else:
        if servings is None:
            return None
        factor = servings

    def mul(v: Decimal | None) -> Decimal | None:
        return _q(v * factor) if v is not None else None

    return Nutrients(
        mul(per.energy_kcal),
        mul(per.protein_g),
        mul(per.fat_g),
        mul(per.carbs_g),
        mul(per.fiber_g),
    )


def sum_nutrients(items: list[Nutrients]) -> Nutrients:
    """Sum of known values; a field is None only when *no* item knows it."""

    def total(values: list[Decimal | None]) -> Decimal | None:
        known = [v for v in values if v is not None]
        return sum(known, Decimal(0)) if known else None

    return Nutrients(
        total([i.energy_kcal for i in items]),
        total([i.protein_g for i in items]),
        total([i.fat_g for i in items]),
        total([i.carbs_g for i in items]),
        total([i.fiber_g for i in items]),
    )


def recipe_portion(
    total: Nutrients,
    *,
    cooked_yield_g: Decimal | None,
    consumed_g: Decimal | None = None,
    fraction: Decimal | None = None,
) -> tuple[Nutrients, Decimal]:
    """Nutrients of a consumed part of a recipe. Returns (nutrients, fraction used)."""
    if fraction is None:
        if consumed_g is None or cooked_yield_g is None or cooked_yield_g <= 0:
            raise ParseError("portion_required")
        fraction = consumed_g / cooked_yield_g
    if not Decimal(0) < fraction <= Decimal(20):
        raise ParseError("bad_fraction")

    def mul(v: Decimal | None) -> Decimal | None:
        return _q(v * fraction) if v is not None else None

    return (
        Nutrients(
            mul(total.energy_kcal),
            mul(total.protein_g),
            mul(total.fat_g),
            mul(total.carbs_g),
            mul(total.fiber_g),
        ),
        fraction,
    )


# --- deterministic text parsing (no AI) ------------------------------------------------

_UNIT_WORDS: dict[str, Unit] = {
    "г": Unit.G,
    "гр": Unit.G,
    "грамм": Unit.G,
    "грамма": Unit.G,
    "граммов": Unit.G,
    "g": Unit.G,
    "gr": Unit.G,
    "gram": Unit.G,
    "grams": Unit.G,
    "кг": Unit.KG,
    "kg": Unit.KG,
    "мл": Unit.ML,
    "ml": Unit.ML,
    "миллилитров": Unit.ML,
    "л": Unit.L,
    "l": Unit.L,
    "литр": Unit.L,
    "литра": Unit.L,
    "шт": Unit.PIECE,
    "штук": Unit.PIECE,
    "штуки": Unit.PIECE,
    "штука": Unit.PIECE,
    "pcs": Unit.PIECE,
    "pc": Unit.PIECE,
    "piece": Unit.PIECE,
    "pieces": Unit.PIECE,
    "порция": Unit.SERVING,
    "порции": Unit.SERVING,
    "порций": Unit.SERVING,
    "serving": Unit.SERVING,
    "servings": Unit.SERVING,
    "стакан": Unit.CUP,
    "стакана": Unit.CUP,
    "стаканов": Unit.CUP,
    "cup": Unit.CUP,
    "cups": Unit.CUP,
    "ст.л": Unit.TBSP,
    "ст.л.": Unit.TBSP,
    "tbsp": Unit.TBSP,
    "ч.л": Unit.TSP,
    "ч.л.": Unit.TSP,
    "tsp": Unit.TSP,
    "ломтик": Unit.SLICE,
    "ломтика": Unit.SLICE,
    "кусок": Unit.SLICE,
    "куска": Unit.SLICE,
    "кусков": Unit.SLICE,
    "slice": Unit.SLICE,
    "slices": Unit.SLICE,
}
_NUM = r"(\d+(?:[.,]\d+)?)"
_UNIT_ALT = "|".join(sorted((re.escape(u) for u in _UNIT_WORDS), key=len, reverse=True))
_AMOUNT_RE = re.compile(rf"(?<![\w.]){_NUM}\s*({_UNIT_ALT})?(?![\w])", re.IGNORECASE)
_SPLIT_RE = re.compile(r"\s*(?:,|;|\n|\+|\bи\b|\band\b)\s*", re.IGNORECASE)


@dataclass(frozen=True)
class ParsedAmount:
    name: str
    amount: Decimal | None
    unit: Unit | None
    amount_text: str | None


def parse_amount(text: str) -> tuple[Decimal, Unit] | None:
    """'150', '150 г', '0,5 л', '2 шт' -> (amount, unit). A bare number means grams."""
    m = re.fullmatch(rf"\s*{_NUM}\s*({_UNIT_ALT})?\s*", text, re.IGNORECASE)
    if not m:
        return None
    value = parse_decimal(m.group(1))
    unit = _UNIT_WORDS[m.group(2).lower()] if m.group(2) else Unit.G
    if value <= 0:
        return None
    return value, unit


def parse_food_line(text: str) -> list[ParsedAmount]:
    """Split 'овсянка 100 г, молоко 250 мл, банан' into items with explicit amounts only."""
    items: list[ParsedAmount] = []
    for part in _SPLIT_RE.split(text.strip()):
        part = part.strip(" .")
        if not part:
            continue
        m = _AMOUNT_RE.search(part)
        amount: Decimal | None = None
        unit: Unit | None = None
        amount_text: str | None = None
        name = part
        if m:
            amount = parse_decimal(m.group(1))
            unit = _UNIT_WORDS[m.group(2).lower()] if m.group(2) else None
            amount_text = m.group(0).strip()
            name = (part[: m.start()] + " " + part[m.end() :]).strip(" ,.-")
            if unit is None:
                # A bare number without unit next to a food name is ambiguous ("2 яйца" vs
                # "200"); treat small integers as pieces, otherwise leave the unit unknown.
                unit = Unit.PIECE if amount == amount.to_integral_value() and amount <= 20 else None
        if not name:
            continue
        items.append(ParsedAmount(" ".join(name.split())[:120], amount, unit, amount_text))
        if len(items) >= MAX_ITEMS:
            break
    return items


def normalize_name(name: str) -> str:
    return " ".join(re.sub(r"[^\w\s%]", " ", name.lower().replace("ё", "е")).split())


def default_meal_type(local_hour: int) -> MealType:
    if 4 <= local_hour < 11:
        return MealType.BREAKFAST
    if 11 <= local_hour < 16:
        return MealType.LUNCH
    if 16 <= local_hour < 22:
        return MealType.DINNER
    return MealType.SNACK
