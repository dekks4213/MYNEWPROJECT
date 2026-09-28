"""Factual daily summary. No AI, no inferred values."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from fitcoach.db.models import (
    FoodEntry,
    PlannedWorkout,
    User,
    WeightEntry,
    WorkoutSession,
    WorkoutTemplateVersion,
)
from fitcoach.domain.nutrition import DayTotals, NutrientValues, day_totals
from fitcoach.services.activities import ActivityService
from fitcoach.services.diary import DiaryService
from fitcoach.services.users import local_today


@dataclass(frozen=True)
class DaySummary:
    day: dt.date
    food: list[FoodEntry]
    totals: DayTotals
    kcal_target: Decimal | None
    weights_today: list[WeightEntry]
    latest_weight: WeightEntry | None
    sessions: list[WorkoutSession]
    planned_open: list[tuple[PlannedWorkout, WorkoutTemplateVersion]]


async def build_day_summary(
    session: AsyncSession, user: User, now: dt.datetime | None = None
) -> DaySummary:
    day = local_today(user, now)
    diary = DiaryService(session, user)
    food, weights, sessions = await diary.entries_for_day(day)
    totals = day_totals(
        NutrientValues(f.energy_kcal, f.protein_g, f.fat_g, f.carbs_g) for f in food
    )
    planned = await ActivityService(session, user).list_planned(day, day + dt.timedelta(days=1))
    return DaySummary(
        day=day,
        food=food,
        totals=totals,
        kcal_target=user.daily_kcal_target,
        weights_today=weights,
        latest_weight=await diary.latest_weight(),
        sessions=sessions,
        planned_open=planned,
    )
