"""Factual history over the user's own records. Only compatible metrics are aggregated:
values are summed per activity type and per (field key, type, unit) as recorded in each
session's own field snapshot, and only for fields declared with SUM aggregation."""

from __future__ import annotations

import datetime as dt
from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from fitcoach.db.models import (
    ActivityType,
    ActivityTypeVersion,
    FoodEntry,
    User,
    WeightEntry,
    WorkoutSession,
)
from fitcoach.domain.fields import Aggregation, FieldSchema, FieldType
from fitcoach.domain.nutrition import DayTotals, NutrientValues, day_totals
from fitcoach.domain.workout import WorkoutBody, totals
from fitcoach.services.users import local_today

MAX_DAYS = 92


@dataclass(frozen=True)
class NutritionDay:
    day: dt.date
    totals: DayTotals


@dataclass
class MetricTotal:
    label: str
    unit: str | None
    type: FieldType
    value: Decimal = Decimal(0)
    sessions: int = 0


@dataclass
class ActivityStats:
    type_id: int
    name: str
    counts_as_training: bool
    sessions: int = 0
    metrics: dict[tuple[str, str, str | None], MetricTotal] = field(default_factory=dict)
    volume_kg: Decimal | None = None
    block_distance_m: int | None = None


def _window(user: User, days: int) -> tuple[dt.date, dt.date]:
    days = max(1, min(days, MAX_DAYS))
    today = local_today(user)
    return today - dt.timedelta(days=days - 1), today


async def weight_history(session: AsyncSession, user: User, days: int = 30) -> list[WeightEntry]:
    start, _ = _window(user, days)
    rows = await session.execute(
        select(WeightEntry)
        .where(
            WeightEntry.owner_id == user.id,
            WeightEntry.deleted_at.is_(None),
            WeightEntry.local_date >= start,
        )
        .order_by(WeightEntry.measured_at)
    )
    # One value per day: the last measurement of that local day.
    per_day: dict[dt.date, WeightEntry] = {}
    for entry in rows.scalars():
        per_day[entry.local_date] = entry
    return list(per_day.values())


async def nutrition_history(session: AsyncSession, user: User, days: int = 7) -> list[NutritionDay]:
    """Only days with entries are returned: a day without entries is unknown, not zero."""
    start, _ = _window(user, days)
    rows = (
        (
            await session.execute(
                select(FoodEntry).where(
                    FoodEntry.owner_id == user.id,
                    FoodEntry.deleted_at.is_(None),
                    FoodEntry.local_date >= start,
                )
            )
        )
        .scalars()
        .all()
    )
    by_day: dict[dt.date, list[FoodEntry]] = defaultdict(list)
    for e in rows:
        by_day[e.local_date].append(e)
    return [
        NutritionDay(
            day,
            day_totals(
                NutrientValues(e.energy_kcal, e.protein_g, e.fat_g, e.carbs_g) for e in entries
            ),
        )
        for day, entries in sorted(by_day.items(), reverse=True)
    ]


async def activity_stats(session: AsyncSession, user: User, days: int = 30) -> list[ActivityStats]:
    start, _ = _window(user, days)
    rows = (
        await session.execute(
            select(WorkoutSession, ActivityType)
            .join(
                ActivityTypeVersion,
                ActivityTypeVersion.id == WorkoutSession.activity_type_version_id,
            )
            .join(ActivityType, ActivityType.id == ActivityTypeVersion.activity_type_id)
            .where(
                WorkoutSession.owner_id == user.id,
                WorkoutSession.deleted_at.is_(None),
                WorkoutSession.local_date >= start,
            )
            .order_by(WorkoutSession.completed_at)
        )
    ).all()
    stats: dict[int, ActivityStats] = {}
    for ws, activity in rows:
        st = stats.setdefault(
            activity.id, ActivityStats(activity.id, activity.name, activity.counts_as_training)
        )
        st.sessions += 1
        schema = FieldSchema.model_validate({"fields": ws.field_snapshot})
        for f in schema.fields:
            value = ws.values.get(f.key)
            if value is None or f.aggregation is not Aggregation.SUM:
                continue
            key = (f.key, f.type.value, f.unit)
            metric = st.metrics.setdefault(key, MetricTotal(f.label, f.unit, f.type))
            metric.value += Decimal(str(value))
            metric.sessions += 1
        if ws.blocks:
            t = totals(WorkoutBody.load(ws.blocks))
            if t.volume_kg is not None:
                st.volume_kg = (st.volume_kg or Decimal(0)) + t.volume_kg
            if t.distance_m is not None:
                st.block_distance_m = (st.block_distance_m or 0) + t.distance_m
    return sorted(stats.values(), key=lambda s: -s.sessions)
