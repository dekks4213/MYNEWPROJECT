"""Owner-only data export and account deletion."""

from __future__ import annotations

import datetime as dt
import json
from decimal import Decimal
from typing import Any

from sqlalchemy import delete, inspect, select
from sqlalchemy.ext.asyncio import AsyncSession

from fitcoach.db.models import (
    ActivityType,
    ActivityTypeVersion,
    AiCall,
    Draft,
    Food,
    FoodEntry,
    ImportBatch,
    PlannedWorkout,
    Program,
    Reminder,
    ReminderDelivery,
    SavedMeal,
    User,
    WeightEntry,
    WorkoutSession,
    WorkoutTemplate,
    WorkoutTemplateVersion,
)
from fitcoach.services.errors import ServiceError

EXPORT_MODELS: tuple[type[Any], ...] = (
    FoodEntry,
    WeightEntry,
    Food,
    SavedMeal,
    ActivityType,
    ActivityTypeVersion,
    Program,
    WorkoutTemplate,
    WorkoutTemplateVersion,
    PlannedWorkout,
    WorkoutSession,
    Reminder,
    ImportBatch,
    AiCall,
)
DELETE_CONFIRMATIONS = {"УДАЛИТЬ", "DELETE"}
_USER_FIELDS = (
    "language",
    "timezone",
    "units",
    "goal",
    "daily_kcal_target",
    "protein_target_g",
    "fat_target_g",
    "carbs_target_g",
    "age_confirmed_at",
    "ai_text_consent_at",
    "ai_media_consent_at",
    "quiet_start",
    "quiet_end",
    "created_at",
)
_SKIP_COLUMNS = {"owner_id"}


def _jsonable(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dt.datetime | dt.date | dt.time):
        return value.isoformat()
    return value


def _row(obj: Any) -> dict[str, Any]:
    return {
        c.key: _jsonable(getattr(obj, c.key))
        for c in inspect(obj).mapper.column_attrs
        if c.key not in _SKIP_COLUMNS
    }


async def export_json(session: AsyncSession, user: User) -> bytes:
    data: dict[str, Any] = {
        "format": "ritm-export-v1",
        "exported_at": dt.datetime.now(dt.UTC).isoformat(),
        "profile": {k: _jsonable(getattr(user, k)) for k in _USER_FIELDS},
    }
    for model in EXPORT_MODELS:
        rows: list[Any] = list(
            (
                await session.execute(
                    select(model).where(model.owner_id == user.id).order_by(model.id)
                )
            )
            .scalars()
            .all()
        )
        data[model.__tablename__] = [_row(r) for r in rows]
    return json.dumps(data, ensure_ascii=False, indent=1).encode("utf-8")


async def delete_account(session: AsyncSession, user: User, confirmation: str) -> None:
    """Deletes the user row; every personal table cascades via owner foreign keys."""
    if confirmation.strip().upper() not in DELETE_CONFIRMATIONS:
        raise ServiceError("delete_not_confirmed")
    # Drafts and deliveries first keeps the cascade shallow and explicit.
    await session.execute(delete(Draft).where(Draft.owner_id == user.id))
    await session.execute(delete(ReminderDelivery).where(ReminderDelivery.owner_id == user.id))
    result = await session.execute(delete(User).where(User.id == user.id))
    if result.rowcount != 1:  # type: ignore[attr-defined]
        raise ServiceError("not_found")
