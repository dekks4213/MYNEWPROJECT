"""Structured workout blocks shared by templates (targets) and sessions (actual results).

The same shape is used for gym, swimming, enduro drills and custom work. Targets live in
template versions; actual values live in sessions. Nothing here copies one into the other.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from fitcoach.domain.units import ParseError, parse_decimal, parse_duration

MAX_BLOCKS = 20
MAX_ITEMS_PER_BLOCK = 30
MAX_SETS_PER_ITEM = 50


class BlockKind(StrEnum):
    WARMUP = "warmup"
    MAIN = "main"
    TECHNIQUE = "technique"
    INTERVALS = "intervals"
    ROUNDS = "rounds"
    FINISH = "finish"
    CUSTOM = "custom"


class ActivityKind(StrEnum):
    CUSTOM = "custom"
    STRENGTH = "strength"
    SWIMMING = "swimming"
    ENDURO = "enduro"
    MOTO_RIDE = "moto_ride"  # not training: no human effort inferred from the vehicle


Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=60)]
Note = Annotated[str, StringConstraints(strip_whitespace=True, max_length=200)]


class LoadMode(StrEnum):
    TOTAL = "total"  # barbell / machine stack total in kg
    PER_HAND = "per_hand"  # one dumbbell weight
    BODYWEIGHT = "bodyweight"
    MACHINE_SETTING = "setting"  # machine level/pin, not kilograms


class SetSpec(BaseModel):
    """One set/repeat. All fields optional: only what applies is filled."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    reps: int | None = Field(default=None, ge=0, le=1000)
    load_kg: Decimal | None = Field(default=None, ge=0, le=1000)
    load_mode: LoadMode | None = None
    distance_m: int | None = Field(default=None, ge=0, le=500_000)
    duration_s: int | None = Field(default=None, ge=0, le=86_400)
    rest_s: int | None = Field(default=None, ge=0, le=3_600)
    rpe: Decimal | None = Field(default=None, ge=1, le=10)
    rir: int | None = Field(default=None, ge=0, le=10)
    warmup: bool = False
    stroke: Annotated[str, StringConstraints(max_length=20)] | None = None


class Item(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: Name
    sets: tuple[SetSpec, ...] = Field(default=(), max_length=MAX_SETS_PER_ITEM)
    notes: Note | None = None


class Block(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: BlockKind = BlockKind.MAIN
    title: Name | None = None
    rounds: int = Field(default=1, ge=1, le=50)
    items: tuple[Item, ...] = Field(default=(), max_length=MAX_ITEMS_PER_BLOCK)


class WorkoutBody(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    blocks: tuple[Block, ...] = Field(default=(), max_length=MAX_BLOCKS)

    def dump(self) -> list[dict[str, object]]:
        return [
            b.model_dump(mode="json", exclude_none=True, exclude_defaults=True) for b in self.blocks
        ]

    @classmethod
    def load(cls, raw: list[dict[str, object]]) -> WorkoutBody:
        return cls.model_validate({"blocks": raw})


@dataclass(frozen=True)
class BodyTotals:
    """Totals of *compatible* quantities only; each is None when nothing contributes."""

    sets: int
    working_sets: int
    reps: int | None
    volume_kg: Decimal | None  # sum(load*reps) of working sets with kg load (total mode)
    distance_m: int | None
    work_duration_s: int | None


def totals(body: WorkoutBody) -> BodyTotals:
    sets = working = 0
    reps: int | None = None
    volume: Decimal | None = None
    distance: int | None = None
    duration: int | None = None
    for block in body.blocks:
        for item in block.items:
            for s in item.sets:
                n = block.rounds
                sets += n
                if not s.warmup:
                    working += n
                if s.reps is not None:
                    reps = (reps or 0) + s.reps * n
                if (
                    not s.warmup
                    and s.reps is not None
                    and s.load_kg is not None
                    and s.load_mode in (None, LoadMode.TOTAL)
                ):
                    volume = (volume or Decimal(0)) + s.load_kg * s.reps * n
                if s.distance_m is not None:
                    distance = (distance or 0) + s.distance_m * n
                if s.duration_s is not None:
                    duration = (duration or 0) + s.duration_s * n
    return BodyTotals(sets, working, reps, volume, distance, duration)


def pace_per_100m(total_seconds: int | None, distance_m: int | None) -> int | None:
    """Pace from total time and total distance (never an average of per-set paces)."""
    if not total_seconds or not distance_m:
        return None
    return round(total_seconds * 100 / distance_m)


# --- deterministic set parsing ------------------------------------------------------

_KG = r"(?:кг|kg)"
_SET_TOKEN = re.compile(
    rf"^(?:(?P<n>\d+)\s*[x×х*]\s*)?(?P<a>\d+(?:[.,]\d+)?)\s*(?P<u>{_KG}|м|m)?"
    rf"(?:\s*[x×х*]\s*(?P<b>\d+))?$",
    re.IGNORECASE,
)


def parse_sets(text: str, *, default_load: Decimal | None = None) -> list[SetSpec]:
    """Parse set notation (space/comma separated tokens):

    without a load context:  '60x10' -> 60 kg x 10 reps;  '3x60x10' -> 3 such sets
    with default_load (e.g. 'жим 60 кг 3x10'): '3x10' -> 3 sets x 10 reps at that load
    '10'        -> 10 reps (at default_load, if any)
    '4x50м'     -> 4 repeats of 50 m;  '200м' -> one 200 m repeat
    """
    out: list[SetSpec] = []
    for token in re.split(r"[\s,;]+", text.strip()):
        if not token:
            continue
        m = _SET_TOKEN.match(token)
        if not m:
            raise ParseError("bad_sets")
        unit = (m.group("u") or "").lower()
        a = parse_decimal(m.group("a"))
        n = int(m.group("n")) if m.group("n") else 1
        b = int(m.group("b")) if m.group("b") else None
        if unit in ("м", "m"):
            if b is not None or a != a.to_integral_value():
                raise ParseError("bad_sets")
            out.extend(SetSpec(distance_m=int(a)) for _ in range(n))
        elif b is not None:  # count x load x reps
            out.extend(SetSpec(load_kg=a, reps=b) for _ in range(n))
        elif m.group("n") and unit == "":
            if a != a.to_integral_value():
                raise ParseError("bad_sets")
            if default_load is not None:  # sets x reps at the stated load
                out.extend(SetSpec(reps=int(a), load_kg=default_load) for _ in range(n))
            else:  # load x reps
                out.append(SetSpec(load_kg=Decimal(n), reps=int(a)))
        else:
            if a != a.to_integral_value():
                raise ParseError("bad_sets")
            out.append(SetSpec(reps=int(a), load_kg=default_load))
        if len(out) > MAX_SETS_PER_ITEM:
            raise ParseError("too_many_sets")
    if not out:
        raise ParseError("bad_sets")
    return out


_EXERCISE_RE = re.compile(
    rf"^(?P<name>[^\d]+?)\s+(?:(?P<load>\d+(?:[.,]\d+)?)\s*{_KG}\s*)?(?P<sets>[\d\s,xх×*.;кгkgмm]+)$",
    re.IGNORECASE,
)


def parse_strength_text(text: str) -> WorkoutBody:
    """'жим 60 кг 10 10 8, тяга блока 70 кг 12 12 10' -> one main block with exercises.

    Exercises are separated by newlines or by a comma that precedes a letter.
    """
    chunks = [
        c.strip() for c in re.split(r"\n|,(?=\s*[^\d\s,])|;(?=\s*[^\d\s;])", text) if c.strip()
    ]
    items: list[Item] = []
    for chunk in chunks:
        m = _EXERCISE_RE.match(chunk)
        if not m:
            raise ParseError("bad_workout_text")
        load = parse_decimal(m.group("load")) if m.group("load") else None
        sets = parse_sets(m.group("sets"), default_load=load)
        items.append(Item(name=m.group("name").strip(" -:")[:60], sets=tuple(sets)))
    if not items:
        raise ParseError("bad_workout_text")
    return WorkoutBody(blocks=(Block(kind=BlockKind.MAIN, items=tuple(items)),))


def parse_rest(text: str) -> int:
    return parse_duration(text, "mm:ss")


@dataclass(frozen=True)
class SetWords:
    """Localized unit words supplied by the presentation layer."""

    meters: str
    seconds: str
    minutes: str
    rest: str
    warmup: str


def format_set(s: SetSpec, w: SetWords) -> str:
    parts: list[str] = []
    if s.load_kg is not None and s.reps is not None:
        load = format(s.load_kg.normalize(), "f")
        parts.append(f"{load}×{s.reps}")
    elif s.reps is not None:
        parts.append(f"×{s.reps}")
    if s.distance_m is not None:
        parts.append(f"{s.distance_m} {w.meters}")
    if s.duration_s is not None:
        m, sec = divmod(s.duration_s, 60)
        parts.append(f"{m}:{sec:02d}")
    if s.stroke:
        parts.append(s.stroke)
    if s.rpe is not None:
        parts.append(f"RPE {format(s.rpe.normalize(), 'f')}")
    if s.rir is not None:
        parts.append(f"RIR {s.rir}")
    text = " ".join(parts) or "—"
    if s.warmup:
        text += f" ({w.warmup})"
    return text


def format_item(item: Item, w: SetWords) -> str:
    """Compact: identical consecutive sets are grouped as '5×(200 м)'."""
    groups: list[tuple[int, str, int | None]] = []
    for s in item.sets:
        label = format_set(s, w)
        if groups and groups[-1][1] == label and groups[-1][2] == s.rest_s:
            n, lab, rest = groups[-1]
            groups[-1] = (n + 1, lab, rest)
        else:
            groups.append((1, label, s.rest_s))
    parts = []
    for n, label, rest in groups:
        text = f"{n}×({label})" if n > 1 else label
        if rest:
            shown = f"{rest} {w.seconds}" if rest < 60 or rest % 60 else f"{rest // 60} {w.minutes}"
            text += f", {w.rest} {shown}"
        parts.append(text)
    return f"{item.name}: " + ("; ".join(parts) if parts else "—")


# --- plan text: blocks with headers ----------------------------------------------------

_BLOCK_HEADERS: dict[str, BlockKind] = {
    "разминка": BlockKind.WARMUP,
    "warm-up": BlockKind.WARMUP,
    "warmup": BlockKind.WARMUP,
    "основная": BlockKind.MAIN,
    "основное": BlockKind.MAIN,
    "основная часть": BlockKind.MAIN,
    "main": BlockKind.MAIN,
    "техника": BlockKind.TECHNIQUE,
    "technique": BlockKind.TECHNIQUE,
    "drills": BlockKind.TECHNIQUE,
    "интервалы": BlockKind.INTERVALS,
    "intervals": BlockKind.INTERVALS,
    "круги": BlockKind.ROUNDS,
    "раунды": BlockKind.ROUNDS,
    "rounds": BlockKind.ROUNDS,
    "заминка": BlockKind.FINISH,
    "finish": BlockKind.FINISH,
    "cooldown": BlockKind.FINISH,
    "cool-down": BlockKind.FINISH,
}
_REST_RE = re.compile(r"(?:отдых|rest)\s*(\d+)\s*(с|сек|s|sec|мин|min|м)?\b", re.IGNORECASE)


def parse_item_line(line: str, *, warmup: bool = False) -> Item:
    """'жим 60 кг 10 10 8', 'жим 20x15 40x10', '4x50м упражнения отдых 30с', '400м кроль'."""
    rest_s: int | None = None
    m = _REST_RE.search(line)
    if m:
        value = int(m.group(1))
        unit = (m.group(2) or "с").lower()
        rest_s = value * 60 if unit in ("мин", "min", "м") else value
        line = (line[: m.start()] + " " + line[m.end() :]).strip(" ,")
    sets: list[SetSpec] = []
    name_words: list[str] = []
    ex = _EXERCISE_RE.match(line.strip())
    if ex:
        load = parse_decimal(ex.group("load")) if ex.group("load") else None
        sets = parse_sets(ex.group("sets"), default_load=load)
        name = ex.group("name").strip(" -:")
    else:
        for token in line.split():
            if _SET_TOKEN.match(token):
                sets.extend(parse_sets(token))
            else:
                name_words.append(token)
        # A bare distance ("200м" in a cool-down) is its own label.
        name = " ".join(name_words).strip(" -:,") or line.strip()
    if not sets or not name:
        raise ParseError("bad_workout_text")
    if rest_s is not None or warmup:
        sets = [
            s.model_copy(
                update={
                    "rest_s": rest_s if rest_s is not None else s.rest_s,
                    "warmup": warmup or s.warmup,
                }
            )
            for s in sets
        ]
    return Item(name=name[:60], sets=tuple(sets))


def parse_plan_text(text: str) -> WorkoutBody:
    """Lines with optional block headers ("Разминка", "Основная", "Техника", "Заминка").
    Sets in a warm-up block are marked as warm-up sets."""
    blocks: list[Block] = []
    kind = BlockKind.MAIN
    items: list[Item] = []

    def flush() -> None:
        if items:
            blocks.append(Block(kind=kind, items=tuple(items)))

    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        header = _BLOCK_HEADERS.get(line.strip(" :-").lower())
        if header is not None:
            flush()
            kind, items = header, []
            continue
        for chunk in re.split(r",(?=\s*[^\d\s,])", line):
            if chunk.strip():
                items.append(parse_item_line(chunk, warmup=kind is BlockKind.WARMUP))
        if len(items) > MAX_ITEMS_PER_BLOCK:
            raise ParseError("too_many_sets")
    flush()
    if not blocks:
        raise ParseError("bad_workout_text")
    if len(blocks) > MAX_BLOCKS:
        raise ParseError("too_many_sets")
    return WorkoutBody(blocks=tuple(blocks))
