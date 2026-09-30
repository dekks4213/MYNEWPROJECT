"""Editable starter schemas for common activities. They are copied into the user's own
activity types at creation time (labels in the user's language); nothing here is a closed
sport enum and users can create any other activity without code changes."""

from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal

from fitcoach.domain.fields import (
    DURATION_KEY,
    Aggregation,
    FieldDefinition,
    FieldType,
)
from fitcoach.domain.workout import ActivityKind

Label = Callable[[str], str]

STARTER_KINDS: tuple[ActivityKind, ...] = (
    ActivityKind.STRENGTH,
    ActivityKind.SWIMMING,
    ActivityKind.ENDURO,
    ActivityKind.MOTO_RIDE,
)


def _duration(label: str) -> FieldDefinition:
    return FieldDefinition(
        key=DURATION_KEY,
        label=label,
        type=FieldType.DURATION,
        duration_format="h:mm",
        aggregation=Aggregation.SUM,
    )


def _scale_1_10(key: str, label: str) -> FieldDefinition:
    return FieldDefinition(
        key=key, label=label, type=FieldType.INTEGER, min_value=Decimal(1), max_value=Decimal(10)
    )


def starter_fields(kind: ActivityKind, t: Label) -> tuple[str, list[FieldDefinition]]:
    """Return (default name, fields). `t` maps 'starter.*' keys to localized labels."""
    if kind is ActivityKind.STRENGTH:
        return t("starter.strength"), [
            _duration(t("starter.f.duration")),
            _scale_1_10("f1", t("starter.f.session_rpe")),
        ]
    if kind is ActivityKind.SWIMMING:
        return t("starter.swimming"), [
            _duration(t("starter.f.duration")),
            FieldDefinition(
                key="f1",
                label=t("starter.f.water"),
                type=FieldType.SELECTION,
                choices=(t("starter.c.pool"), t("starter.c.open_water")),
            ),
            FieldDefinition(
                key="f2",
                label=t("starter.f.pool_length"),
                type=FieldType.INTEGER,
                unit=t("unit.m"),
                min_value=Decimal(10),
                max_value=Decimal(100),
            ),
            FieldDefinition(
                key="f3",
                label=t("starter.f.distance"),
                type=FieldType.INTEGER,
                unit=t("unit.m"),
                aggregation=Aggregation.SUM,
            ),
            FieldDefinition(key="f4", label=t("starter.f.equipment"), type=FieldType.TEXT),
        ]
    if kind is ActivityKind.ENDURO:
        # Five things riders actually note; anything else can be added as a custom activity.
        return t("starter.enduro"), [
            _duration(t("starter.f.riding_time")),
            FieldDefinition(
                key="f3",
                label=t("starter.f.distance"),
                type=FieldType.DECIMAL,
                unit=t("unit.km"),
                aggregation=Aggregation.SUM,
            ),
            FieldDefinition(
                key="f5",
                label=t("starter.f.surface"),
                type=FieldType.SELECTION,
                choices=(
                    t("starter.c.sand"),
                    t("starter.c.mud"),
                    t("starter.c.rocks"),
                    t("starter.c.forest"),
                    t("starter.c.track"),
                    t("starter.c.mixed"),
                ),
            ),
            _scale_1_10("f6", t("starter.f.effort")),
            FieldDefinition(key="f8", label=t("starter.f.note"), type=FieldType.TEXT),
        ]
    if kind is ActivityKind.MOTO_RIDE:
        return t("starter.moto_ride"), [
            _duration(t("starter.f.duration")),
            FieldDefinition(
                key="f1",
                label=t("starter.f.distance"),
                type=FieldType.DECIMAL,
                unit=t("unit.km"),
                aggregation=Aggregation.SUM,
            ),
        ]
    raise ValueError(kind)
