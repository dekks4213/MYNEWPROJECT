"""Food and weight diary with soft delete, restore and optimistic-version edits."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Literal

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from fitcoach.db.models import FoodEntry, User, WeightEntry, WorkoutSession
from fitcoach.domain.nutrition import Precision, parse_kcal
from fitcoach.domain.units import ParseError, parse_decimal
from fitcoach.services.errors import Conflict, NotFound, ServiceError
from fitcoach.services.users import local_today, user_zone, utcnow

WEIGHT_MIN_KG = Decimal(20)
WEIGHT_MAX_KG = Decimal(400)
MAX_NAME_LEN = 120

EntryKind = Literal["food", "weight", "session"]
Entry = FoodEntry | WeightEntry | WorkoutSession
_MODELS: dict[str, type[FoodEntry] | type[WeightEntry] | type[WorkoutSession]] = {
    "food": FoodEntry,
    "weight": WeightEntry,
    "session": WorkoutSession,
}


def parse_weight(raw: str) -> Decimal:
    try:
        value = parse_decimal(raw)
    except ParseError as exc:
        raise ServiceError("not_a_number") from exc
    if not WEIGHT_MIN_KG <= value <= WEIGHT_MAX_KG:
        raise ServiceError("weight_out_of_range")
    return value.quantize(Decimal("0.01"))


class DiaryService:
    def __init__(self, session: AsyncSession, user: User) -> None:
        self.session = session
        self.user = user

    async def add_food(
        self,
        name: str,
        *,
        energy_kcal: Decimal | None,
        protein_g: Decimal | None = None,
        fat_g: Decimal | None = None,
        carbs_g: Decimal | None = None,
        precision: Precision,
        source: str = "manual",
        draft_id: int | None = None,
        now: dt.datetime | None = None,
    ) -> FoodEntry:
        name = " ".join(name.split())
        if not name or len(name) > MAX_NAME_LEN:
            raise ServiceError("bad_name")
        if all(v is None for v in (energy_kcal, protein_g, fat_g, carbs_g)):
            precision = Precision.UNKNOWN
        now = now or utcnow()
        entry = FoodEntry(
            owner_id=self.user.id,
            local_date=local_today(self.user, now),
            eaten_at=now,
            name=name,
            energy_kcal=energy_kcal,
            protein_g=protein_g,
            fat_g=fat_g,
            carbs_g=carbs_g,
            precision=precision.value,
            source=source,
            draft_id=draft_id,
        )
        self.session.add(entry)
        await self.session.flush()
        return entry

    async def add_weight(self, raw: str, now: dt.datetime | None = None) -> WeightEntry:
        now = now or utcnow()
        entry = WeightEntry(
            owner_id=self.user.id,
            local_date=local_today(self.user, now),
            measured_at=now,
            weight_kg=parse_weight(raw),
        )
        self.session.add(entry)
        await self.session.flush()
        return entry

    async def _get(self, kind: EntryKind, entry_id: int) -> Entry:
        model = _MODELS[kind]
        row = (
            await self.session.execute(
                select(model).where(model.id == entry_id, model.owner_id == self.user.id)
            )
        ).scalar_one_or_none()
        if row is None:
            raise NotFound
        assert isinstance(row, FoodEntry | WeightEntry | WorkoutSession)
        return row

    async def delete(self, kind: EntryKind, entry_id: int) -> Entry:
        """Soft delete. Idempotent: deleting an already deleted entry is a no-op."""
        row = await self._get(kind, entry_id)
        if row.deleted_at is None:
            row.deleted_at = utcnow()
            await self.session.flush()
        return row

    async def restore(self, kind: EntryKind, entry_id: int) -> Entry:
        row = await self._get(kind, entry_id)
        if row.deleted_at is not None:
            row.deleted_at = None
            await self.session.flush()
        return row

    async def update_food_energy(
        self, entry_id: int, expected_version: int, raw_kcal: str | None
    ) -> FoodEntry:
        """Correct calories. Fails with Conflict if the entry changed since it was shown."""
        try:
            kcal = None if raw_kcal is None else parse_kcal(raw_kcal)
        except ParseError as exc:
            raise ServiceError(exc.code) from exc
        row = await self._get("food", entry_id)
        assert isinstance(row, FoodEntry)
        if row.deleted_at is not None:
            raise NotFound
        result = await self.session.execute(
            update(FoodEntry)
            .where(
                FoodEntry.id == entry_id,
                FoodEntry.owner_id == self.user.id,
                FoodEntry.version == expected_version,
            )
            .values(energy_kcal=kcal, version=FoodEntry.version + 1)
            .execution_options(synchronize_session=False)
        )
        if result.rowcount != 1:  # type: ignore[attr-defined]
            raise Conflict
        await self.session.refresh(row)
        return row

    async def entries_for_day(
        self, day: dt.date | None = None
    ) -> tuple[list[FoodEntry], list[WeightEntry], list[WorkoutSession]]:
        day = day or local_today(self.user)
        food = (
            (
                await self.session.execute(
                    select(FoodEntry)
                    .where(
                        FoodEntry.owner_id == self.user.id,
                        FoodEntry.local_date == day,
                        FoodEntry.deleted_at.is_(None),
                    )
                    .order_by(FoodEntry.eaten_at, FoodEntry.id)
                )
            )
            .scalars()
            .all()
        )
        weights = (
            (
                await self.session.execute(
                    select(WeightEntry)
                    .where(
                        WeightEntry.owner_id == self.user.id,
                        WeightEntry.local_date == day,
                        WeightEntry.deleted_at.is_(None),
                    )
                    .order_by(WeightEntry.measured_at, WeightEntry.id)
                )
            )
            .scalars()
            .all()
        )
        sessions = (
            (
                await self.session.execute(
                    select(WorkoutSession)
                    .where(
                        WorkoutSession.owner_id == self.user.id,
                        WorkoutSession.local_date == day,
                        WorkoutSession.deleted_at.is_(None),
                    )
                    .order_by(WorkoutSession.completed_at, WorkoutSession.id)
                )
            )
            .scalars()
            .all()
        )
        return list(food), list(weights), list(sessions)

    async def latest_weight(self) -> WeightEntry | None:
        return (
            await self.session.execute(
                select(WeightEntry)
                .where(WeightEntry.owner_id == self.user.id, WeightEntry.deleted_at.is_(None))
                .order_by(WeightEntry.measured_at.desc(), WeightEntry.id.desc())
                .limit(1)
            )
        ).scalar_one_or_none()

    def local_time(self, moment: dt.datetime) -> dt.datetime:
        return moment.astimezone(user_zone(self.user))
