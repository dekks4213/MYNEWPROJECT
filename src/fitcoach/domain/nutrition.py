"""Deterministic daily nutrition totals. Unknown values are counted, never treated as zero."""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from fitcoach.domain.units import ParseError, parse_decimal

MAX_KCAL_PER_ENTRY = Decimal(10000)
MAX_GRAMS_PER_ENTRY = Decimal(2000)


class Precision(StrEnum):
    """How the user obtained the numbers. Shown to the user; never a confidence score."""

    MEASURED = "measured"  # weighed or taken from a label
    APPROXIMATE = "approximate"
    UNKNOWN = "unknown"  # no nutrient numbers at all


@dataclass(frozen=True)
class NutrientValues:
    energy_kcal: Decimal | None = None
    protein_g: Decimal | None = None
    fat_g: Decimal | None = None
    carbs_g: Decimal | None = None


@dataclass(frozen=True)
class NutrientTotal:
    value: Decimal
    known_entries: int
    unknown_entries: int


@dataclass(frozen=True)
class DayTotals:
    entries: int
    energy_kcal: NutrientTotal
    protein_g: NutrientTotal
    fat_g: NutrientTotal
    carbs_g: NutrientTotal


def _total(values: list[Decimal | None]) -> NutrientTotal:
    known = [v for v in values if v is not None]
    return NutrientTotal(
        value=sum(known, Decimal(0)),
        known_entries=len(known),
        unknown_entries=len(values) - len(known),
    )


def day_totals(items: Iterable[NutrientValues]) -> DayTotals:
    rows = list(items)
    return DayTotals(
        entries=len(rows),
        energy_kcal=_total([r.energy_kcal for r in rows]),
        protein_g=_total([r.protein_g for r in rows]),
        fat_g=_total([r.fat_g for r in rows]),
        carbs_g=_total([r.carbs_g for r in rows]),
    )


def parse_kcal(raw: str) -> Decimal:
    value = parse_decimal(raw)
    if value < 0:
        raise ParseError("negative")
    if value > MAX_KCAL_PER_ENTRY:
        raise ParseError("above_max")
    return value


_MACRO_SPLIT = re.compile(r"\s*[/;]\s*|\s+")


def parse_macros(raw: str) -> tuple[Decimal | None, Decimal | None, Decimal | None]:
    """Parse 'P/F/C' grams, e.g. '20/10,5/45'. Use '-' or '?' for an unknown component."""
    parts = [p for p in _MACRO_SPLIT.split(raw.strip()) if p]
    if len(parts) != 3:
        raise ParseError("bad_macros")
    out: list[Decimal | None] = []
    for part in parts:
        if part in ("-", "?", "—"):
            out.append(None)
            continue
        value = parse_decimal(part)
        if value < 0:
            raise ParseError("negative")
        if value > MAX_GRAMS_PER_ENTRY:
            raise ParseError("above_max")
        out.append(value)
    return out[0], out[1], out[2]
