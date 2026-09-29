"""SQLAlchemy models. Every personal table has `owner_id` protected by PostgreSQL RLS."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    SmallInteger,
    String,
    Text,
    Time,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, declared_attr, mapped_column


class Base(DeclarativeBase):
    pass


def _now() -> Mapped[dt.datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, nullable=False)
    language: Mapped[str] = mapped_column(String(5), nullable=False, server_default="ru")
    timezone: Mapped[str | None] = mapped_column(String(64))
    units: Mapped[str | None] = mapped_column(String(10))
    onboarding_step: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default="language"
    )
    age_confirmed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    ai_text_consent_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    goal: Mapped[str | None] = mapped_column(String(20))
    daily_kcal_target: Mapped[Decimal | None] = mapped_column(Numeric(7, 1))
    protein_target_g: Mapped[Decimal | None] = mapped_column(Numeric(6, 1))
    fat_target_g: Mapped[Decimal | None] = mapped_column(Numeric(6, 1))
    carbs_target_g: Mapped[Decimal | None] = mapped_column(Numeric(6, 1))
    target_set_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    # Separate consent for sending photos/voice to the external AI provider.
    ai_media_consent_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    quiet_start: Mapped[dt.time | None] = mapped_column(Time)
    quiet_end: Mapped[dt.time | None] = mapped_column(Time)
    created_at: Mapped[dt.datetime] = _now()
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")

    __mapper_args__ = {"version_id_col": version}  # noqa: RUF012


class Owned:
    @declared_attr
    def owner_id(cls) -> Mapped[int]:
        return mapped_column(
            BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
        )


class FoodEntry(Owned, Base):
    __tablename__ = "food_entries"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    local_date: Mapped[dt.date] = mapped_column(Date, nullable=False)
    eaten_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    energy_kcal: Mapped[Decimal | None] = mapped_column(Numeric(8, 1))
    protein_g: Mapped[Decimal | None] = mapped_column(Numeric(7, 1))
    fat_g: Mapped[Decimal | None] = mapped_column(Numeric(7, 1))
    carbs_g: Mapped[Decimal | None] = mapped_column(Numeric(7, 1))
    fiber_g: Mapped[Decimal | None] = mapped_column(Numeric(7, 1))
    precision: Mapped[str] = mapped_column(String(16), nullable=False)
    source: Mapped[str] = mapped_column(String(16), nullable=False, server_default="manual")
    meal_type: Mapped[str | None] = mapped_column(String(12))
    amount: Mapped[Decimal | None] = mapped_column(Numeric(9, 2))
    unit: Mapped[str | None] = mapped_column(String(12))
    grams: Mapped[Decimal | None] = mapped_column(Numeric(8, 1))
    # Where the nutrient values came from: user, label, catalog, usda, off, ai_estimate, recipe
    nutrient_source: Mapped[str | None] = mapped_column(String(16))
    food_id: Mapped[int | None] = mapped_column(BigInteger)
    draft_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("drafts.id", ondelete="SET NULL")
    )
    created_at: Mapped[dt.datetime] = _now()
    deleted_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")

    __mapper_args__ = {"version_id_col": version}  # noqa: RUF012


class WeightEntry(Owned, Base):
    __tablename__ = "weight_entries"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    local_date: Mapped[dt.date] = mapped_column(Date, nullable=False)
    measured_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    weight_kg: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=False)
    created_at: Mapped[dt.datetime] = _now()
    deleted_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")

    __mapper_args__ = {"version_id_col": version}  # noqa: RUF012


class ActivityType(Owned, Base):
    __tablename__ = "activity_types"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(60), nullable=False)
    # custom | strength | swimming | enduro | moto_ride. Only affects starter UI and
    # analytics grouping; fields remain user data.
    kind: Mapped[str] = mapped_column(String(16), nullable=False, server_default="custom")
    counts_as_training: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    current_version: Mapped[int] = mapped_column(Integer, nullable=False)
    archived_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[dt.datetime] = _now()
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")

    __mapper_args__ = {"version_id_col": version}  # noqa: RUF012


class ActivityTypeVersion(Owned, Base):
    """Immutable snapshot of an activity type's field definitions."""

    __tablename__ = "activity_type_versions"
    __table_args__ = (UniqueConstraint("activity_type_id", "version"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    activity_type_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("activity_types.id", ondelete="CASCADE"), nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    name: Mapped[str] = mapped_column(String(60), nullable=False)
    fields: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[dt.datetime] = _now()


class WorkoutTemplate(Owned, Base):
    __tablename__ = "workout_templates"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    activity_type_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("activity_types.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(60), nullable=False)
    program_id: Mapped[int | None] = mapped_column(BigInteger)
    current_version: Mapped[int] = mapped_column(Integer, nullable=False)
    archived_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[dt.datetime] = _now()
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")

    __mapper_args__ = {"version_id_col": version}  # noqa: RUF012


class WorkoutTemplateVersion(Owned, Base):
    """Immutable template revision. Sessions reference the exact revision they used."""

    __tablename__ = "workout_template_versions"
    __table_args__ = (UniqueConstraint("template_id", "version"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    template_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("workout_templates.id", ondelete="CASCADE"), nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    name: Mapped[str] = mapped_column(String(60), nullable=False)
    activity_type_version_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("activity_type_versions.id", ondelete="CASCADE"), nullable=False
    )
    # Planned targets per field key. Targets are never copied into performed values.
    targets: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    # Structured planned blocks (domain.workout.WorkoutBody). Targets only.
    blocks: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, server_default="[]")
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[dt.datetime] = _now()


class PlannedWorkout(Owned, Base):
    __tablename__ = "planned_workouts"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    template_version_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("workout_template_versions.id", ondelete="CASCADE"), nullable=False
    )
    planned_date: Mapped[dt.date] = mapped_column(Date, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="planned")
    session_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("workout_sessions.id", ondelete="SET NULL")
    )
    created_at: Mapped[dt.datetime] = _now()
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")

    __mapper_args__ = {"version_id_col": version}  # noqa: RUF012


class WorkoutSession(Owned, Base):
    """Actually performed work, with a snapshot of the field meaning used at the time."""

    __tablename__ = "workout_sessions"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    activity_type_version_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("activity_type_versions.id", ondelete="CASCADE"), nullable=False
    )
    template_version_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("workout_template_versions.id", ondelete="SET NULL")
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="completed")
    local_date: Mapped[dt.date] = mapped_column(Date, nullable=False)
    completed_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    activity_name: Mapped[str] = mapped_column(String(60), nullable=False)
    template_name: Mapped[str | None] = mapped_column(String(60))
    field_snapshot: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    values: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    raw_values: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    # Structured performed blocks; never copied from targets without user action.
    blocks: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, server_default="[]")
    source: Mapped[str] = mapped_column(String(16), nullable=False, server_default="manual")
    # Idempotency key for imports (e.g. "strong:<hash>").
    source_ref: Mapped[str | None] = mapped_column(String(80))
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[dt.datetime] = _now()
    deleted_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")

    __mapper_args__ = {"version_id_col": version}  # noqa: RUF012


class Draft(Owned, Base):
    """AI- or import-produced proposal awaiting explicit user confirmation."""

    __tablename__ = "drafts"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="pending")
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    created_at: Mapped[dt.datetime] = _now()
    resolved_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))


class AiUserBudget(Owned, Base):
    __tablename__ = "ai_user_budget"

    owner_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    day: Mapped[dt.date] = mapped_column(Date, primary_key=True)
    calls: Mapped[int] = mapped_column(Integer, nullable=False)
    media_calls: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")


class AiCall(Owned, Base):
    """Metering log: counts and provider-reported token usage. Never stores content."""

    __tablename__ = "ai_calls"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    task: Mapped[str] = mapped_column(String(32), nullable=False)
    provider: Mapped[str] = mapped_column(String(16), nullable=False)
    model: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    media: Mapped[str] = mapped_column(String(8), nullable=False, server_default="none")
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    refunded: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    created_at: Mapped[dt.datetime] = _now()


class Food(Owned, Base):
    """User-confirmed food composition (own label, recipe or a chosen database record).

    Nutrients are per `basis` (100g, 100ml or one serving). Unknown values are NULL.
    """

    __tablename__ = "foods"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    brand: Mapped[str | None] = mapped_column(String(80))
    basis: Mapped[str] = mapped_column(String(8), nullable=False)
    serving_g: Mapped[Decimal | None] = mapped_column(Numeric(8, 1))
    energy_kcal: Mapped[Decimal | None] = mapped_column(Numeric(8, 1))
    protein_g: Mapped[Decimal | None] = mapped_column(Numeric(7, 2))
    fat_g: Mapped[Decimal | None] = mapped_column(Numeric(7, 2))
    carbs_g: Mapped[Decimal | None] = mapped_column(Numeric(7, 2))
    fiber_g: Mapped[Decimal | None] = mapped_column(Numeric(7, 2))
    preparation: Mapped[str | None] = mapped_column(String(16))
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    source_id: Mapped[str | None] = mapped_column(String(64))
    source_fetched_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    favorite: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    archived_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[dt.datetime] = _now()


class SavedMeal(Owned, Base):
    """Reusable meal or recipe. Item nutrients are frozen at save time."""

    __tablename__ = "saved_meals"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    kind: Mapped[str] = mapped_column(String(8), nullable=False)  # meal | recipe
    cooked_yield_g: Mapped[Decimal | None] = mapped_column(Numeric(8, 1))
    servings: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    favorite: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    items: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    archived_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[dt.datetime] = _now()


class Program(Owned, Base):
    __tablename__ = "programs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(60), nullable=False)
    archived_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[dt.datetime] = _now()


class ImportBatch(Owned, Base):
    __tablename__ = "import_batches"
    __table_args__ = (UniqueConstraint("owner_id", "kind", "file_sha256"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    file_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(12), nullable=False)
    summary: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[dt.datetime] = _now()


class Reminder(Owned, Base):
    __tablename__ = "reminders"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String(12), nullable=False)
    text: Mapped[str | None] = mapped_column(String(200))
    local_time: Mapped[dt.time] = mapped_column(Time, nullable=False)
    days_mask: Mapped[int] = mapped_column(SmallInteger, nullable=False)  # bit 0 = Monday
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    next_fire_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[dt.datetime] = _now()
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")

    __mapper_args__ = {"version_id_col": version}  # noqa: RUF012


class ReminderDelivery(Owned, Base):
    __tablename__ = "reminder_deliveries"
    __table_args__ = (UniqueConstraint("reminder_id", "scheduled_for"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    reminder_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("reminders.id", ondelete="CASCADE"), nullable=False
    )
    scheduled_for: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String(12), nullable=False)
    error: Mapped[str | None] = mapped_column(String(40))
    created_at: Mapped[dt.datetime] = _now()


class AiGlobalBudget(Base):
    """Operational, non-personal counter."""

    __tablename__ = "ai_global_budget"

    day: Mapped[dt.date] = mapped_column(Date, primary_key=True)
    calls: Mapped[int] = mapped_column(Integer, nullable=False)


class ProcessedUpdate(Base):
    """Telegram update idempotency marker. Contains no message content."""

    __tablename__ = "processed_updates"

    update_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    processed_at: Mapped[dt.datetime] = _now()


OWNED_TABLES: tuple[str, ...] = (
    "food_entries",
    "weight_entries",
    "activity_types",
    "activity_type_versions",
    "workout_templates",
    "workout_template_versions",
    "planned_workouts",
    "workout_sessions",
    "drafts",
    "ai_user_budget",
    "ai_calls",
    "foods",
    "saved_meals",
    "programs",
    "import_batches",
    "reminders",
    "reminder_deliveries",
)
IMMUTABLE_TABLES: tuple[str, ...] = ("activity_type_versions", "workout_template_versions")
