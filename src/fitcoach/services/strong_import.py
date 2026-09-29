"""Strong app CSV import (one-way; Strong has no public sync API).

Format status: the parser follows the column set of Strong's "Export data" CSV as it has
been published by users (Date, Workout Name, Duration, Exercise Name, Set Order, Weight,
Reps, Distance, Seconds, Notes, Workout Notes, RPE; comma or semicolon separated, optional
unit columns / unit suffixes). It could not be verified against a fresh export from the
current app version in this environment, so unknown headers are reported, not guessed.

Idempotency: a file (by SHA-256) is imported once per user, and every workout carries
source_ref = "strong:<hash of date+name>" with a unique index, so re-importing overlapping
exports never duplicates sessions.
"""

from __future__ import annotations

import csv
import dataclasses
import datetime as dt
import hashlib
import io
import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from fitcoach.db.models import ImportBatch, User, WorkoutSession
from fitcoach.domain.starters import Label, starter_fields
from fitcoach.domain.units import ParseError, parse_decimal
from fitcoach.domain.workout import ActivityKind, Block, Item, SetSpec, WorkoutBody
from fitcoach.services.activities import ActivityService
from fitcoach.services.errors import Conflict, NotFound, ServiceError
from fitcoach.services.users import user_zone

MAX_ROWS = 20_000
MAX_WORKOUTS = 2_000
LB_TO_KG = Decimal("0.45359237")
MILE_TO_M = Decimal("1609.344")

_HEADER_ALIASES = {
    "date": "date",
    "workout name": "workout",
    "workout": "workout",
    "duration": "duration",
    "workout duration": "duration",
    "exercise name": "exercise",
    "exercise": "exercise",
    "set order": "set_order",
    "set": "set_order",
    "weight": "weight",
    "weight unit": "weight_unit",
    "reps": "reps",
    "distance": "distance",
    "distance unit": "distance_unit",
    "seconds": "seconds",
    "notes": "notes",
    "workout notes": "workout_notes",
    "rpe": "rpe",
}
_UNIT_IN_HEADER = re.compile(r"^(.*?)\s*\((kg|lbs?|km|mi|m)\)\s*$", re.IGNORECASE)


@dataclass
class ParsedImport:
    sha256: str
    workouts: list[dict[str, Any]]
    sets: int
    errors: list[tuple[int, str]]
    unknown_columns: list[str]
    weight_unit: str  # kg | lb | unknown


def _normalize_headers(raw: list[str]) -> tuple[list[str | None], dict[str, str], list[str]]:
    keys: list[str | None] = []
    units: dict[str, str] = {}
    unknown: list[str] = []
    for header in raw:
        h = header.strip().strip('"').lower()
        m = _UNIT_IN_HEADER.match(h)
        if m:
            h = m.group(1)
            units[_HEADER_ALIASES.get(h, h)] = m.group(2).lower().rstrip("s")
        key = _HEADER_ALIASES.get(h)
        if key is None and h:
            unknown.append(header.strip()[:40])
        keys.append(key)
    return keys, units, unknown


def _duration(text: str) -> int | None:
    text = text.strip().lower()
    if not text:
        return None
    if text.isdigit():
        return int(text)
    total, matched = 0, False
    for num, unit in re.findall(r"(\d+)\s*([hms])", text):
        total += int(num) * {"h": 3600, "m": 60, "s": 1}[unit]
        matched = True
    return total if matched else None


def _date(text: str) -> dt.datetime:
    text = text.strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return dt.datetime.strptime(text, fmt)
        except ValueError:
            continue
    raise ValueError("bad date")


def _num(text: str | None) -> Decimal | None:
    if text is None or not text.strip():
        return None
    try:
        value = parse_decimal(text)
    except ParseError:
        return None
    return value if value >= 0 else None


def parse_strong_csv(data: bytes) -> ParsedImport:
    sha = hashlib.sha256(data).hexdigest()
    try:
        content = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ServiceError("bad_csv") from exc
    if "\x00" in content:
        raise ServiceError("bad_csv")
    first = content.split("\n", 1)[0]
    delimiter = ";" if first.count(";") > first.count(",") else ","
    reader = csv.reader(io.StringIO(content), delimiter=delimiter)
    try:
        header = next(reader)
    except (StopIteration, csv.Error) as exc:
        raise ServiceError("bad_csv") from exc
    keys, header_units, unknown = _normalize_headers(header)
    if not {"date", "exercise"} <= {k for k in keys if k}:
        raise ServiceError("bad_csv")

    workouts: dict[tuple[str, str], dict[str, Any]] = {}
    errors: list[tuple[int, str]] = []
    sets = 0
    weight_units: set[str] = set()
    try:
        for line_no, row in enumerate(reader, start=2):
            if line_no > MAX_ROWS + 1:
                raise ServiceError("csv_too_large")
            if not any(cell.strip() for cell in row):
                continue
            rec = {k: row[i].strip() for i, k in enumerate(keys) if k and i < len(row)}
            try:
                started = _date(rec.get("date", ""))
            except ValueError:
                errors.append((line_no, "date"))
                continue
            exercise = rec.get("exercise", "")[:60]
            if not exercise:
                errors.append((line_no, "exercise"))
                continue
            name = (rec.get("workout") or "Strong")[:60]
            key = (started.isoformat(), name)
            workout = workouts.setdefault(
                key,
                {
                    "started": started.isoformat(),
                    "name": name,
                    "exercises": {},
                    "duration_s": _duration(rec.get("duration", "")),
                    "notes": (rec.get("workout_notes") or None),
                },
            )
            unit = (rec.get("weight_unit") or header_units.get("weight") or "").lower()
            unit = {"lbs": "lb", "lb": "lb", "kg": "kg"}.get(unit, "")
            if unit:
                weight_units.add(unit)
            weight = _num(rec.get("weight"))
            if weight is not None and unit == "lb":
                weight = (weight * LB_TO_KG).quantize(Decimal("0.1"))
            reps = _num(rec.get("reps"))
            distance = _num(rec.get("distance"))
            d_unit = (rec.get("distance_unit") or header_units.get("distance") or "m").lower()
            if distance is not None:
                distance = distance * {"km": Decimal(1000), "mi": MILE_TO_M}.get(d_unit, Decimal(1))
            seconds = _num(rec.get("seconds"))
            rpe = _num(rec.get("rpe"))
            if all(v is None for v in (weight, reps, distance, seconds)):
                errors.append((line_no, "empty_set"))
                continue
            try:
                spec = SetSpec(
                    reps=int(reps) if reps is not None else None,
                    load_kg=weight if weight else None,
                    distance_m=int(distance) if distance else None,
                    duration_s=int(seconds) if seconds else None,
                    rpe=rpe if rpe is not None and 1 <= rpe <= 10 else None,
                    warmup=rec.get("set_order", "").upper().startswith("W"),
                )
            except ValueError:
                errors.append((line_no, "value"))
                continue
            workout["exercises"].setdefault(exercise, []).append(
                spec.model_dump(mode="json", exclude_none=True, exclude_defaults=True)
            )
            sets += 1
            if len(workouts) > MAX_WORKOUTS:
                raise ServiceError("csv_too_large")
    except csv.Error as exc:
        raise ServiceError("bad_csv") from exc

    result = []
    for (started_iso, name), w in sorted(workouts.items()):
        if not w["exercises"]:
            continue
        digest = hashlib.sha256(f"{started_iso}|{name}".encode()).hexdigest()[:32]
        w["source_ref"] = "strong:" + digest
        result.append(w)
    weight_unit = (
        "unknown"
        if not weight_units
        else (weight_units.pop() if len(weight_units) == 1 else "mixed")
    )
    return ParsedImport(sha, result, sets, errors, unknown, weight_unit)


class StrongImportService:
    def __init__(self, session: AsyncSession, user: User) -> None:
        self.session = session
        self.user = user

    async def preview(self, data: bytes) -> ImportBatch:
        parsed = parse_strong_csv(data)
        existing = (
            await self.session.execute(
                select(ImportBatch)
                .where(
                    ImportBatch.owner_id == self.user.id,
                    ImportBatch.kind == "strong_csv",
                    ImportBatch.file_sha256 == parsed.sha256,
                )
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if existing is not None:
            if existing.status == "imported":
                raise Conflict("already_imported")
            return existing
        if not parsed.workouts:
            raise ServiceError("nothing_to_import")
        refs = [w["source_ref"] for w in parsed.workouts]
        duplicates = set(
            (
                await self.session.execute(
                    select(WorkoutSession.source_ref).where(
                        WorkoutSession.owner_id == self.user.id, WorkoutSession.source_ref.in_(refs)
                    )
                )
            ).scalars()
        )
        summary = {
            "workouts": parsed.workouts,
            "sets": parsed.sets,
            "errors": parsed.errors[:50],
            "error_count": len(parsed.errors),
            "unknown_columns": parsed.unknown_columns,
            "weight_unit": parsed.weight_unit,
            "duplicates": len(duplicates),
            "exercises": sorted({e for w in parsed.workouts for e in w["exercises"]})[:200],
        }
        batch = ImportBatch(
            owner_id=self.user.id,
            kind="strong_csv",
            file_sha256=parsed.sha256,
            status="preview",
            summary=summary,
        )
        self.session.add(batch)
        await self.session.flush()
        return batch

    async def get(self, batch_id: int) -> ImportBatch:
        batch = (
            await self.session.execute(
                select(ImportBatch).where(
                    ImportBatch.id == batch_id, ImportBatch.owner_id == self.user.id
                )
            )
        ).scalar_one_or_none()
        if batch is None:
            raise NotFound
        return batch

    async def confirm(self, batch_id: int, t: Label) -> int:
        batch = await self.get(batch_id)
        result = await self.session.execute(
            update(ImportBatch)
            .where(
                ImportBatch.id == batch.id,
                ImportBatch.owner_id == self.user.id,
                ImportBatch.status == "preview",
            )
            .values(status="imported")
            .execution_options(synchronize_session=False)
        )
        if result.rowcount != 1:  # type: ignore[attr-defined]
            raise Conflict("already_imported")
        await self.session.refresh(batch)
        activities = ActivityService(self.session, self.user)
        strength = next((a for a in await activities.list_types() if a.kind == "strength"), None)
        if strength is None:
            name, fields = starter_fields(ActivityKind.STRENGTH, t)
            strength_id = (
                await activities.create_type(name, fields, ActivityKind.STRENGTH)
            ).activity_type_id
        else:
            strength_id = strength.id
        ctx = await activities.recording_context(type_id=strength_id)
        tz: ZoneInfo = user_zone(self.user)
        refs = [w["source_ref"] for w in batch.summary["workouts"]]
        existing = set(
            (
                await self.session.execute(
                    select(WorkoutSession.source_ref).where(
                        WorkoutSession.owner_id == self.user.id, WorkoutSession.source_ref.in_(refs)
                    )
                )
            ).scalars()
        )
        imported = 0
        for w in batch.summary["workouts"]:
            if w["source_ref"] in existing:
                continue
            items = tuple(
                Item(name=name, sets=tuple(SetSpec.model_validate(s) for s in sets))
                for name, sets in w["exercises"].items()
            )
            started = dt.datetime.fromisoformat(w["started"]).replace(tzinfo=tz)
            raw = {"duration": f"{w['duration_s']} sec"} if w.get("duration_s") else {}
            await activities.record_session(
                dataclasses.replace(ctx, template_name=w["name"]),
                raw,
                now=started,
                blocks=WorkoutBody(blocks=(Block(items=items),)),
                source="import",
                source_ref=w["source_ref"],
                notes=(w.get("notes") or None) and w["notes"][:500],
            )
            imported += 1
        return imported
