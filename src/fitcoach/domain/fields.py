"""Declarative, versioned metric field definitions for user-defined activities.

Definitions are data only: there is no formula, expression, or code execution.
Values are validated server-side and stored normalized alongside the original input.
"""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from fitcoach.domain.units import (
    DURATION_FORMATS,
    ParseError,
    format_decimal,
    format_duration,
    parse_decimal,
    parse_duration,
    parse_int,
)

MAX_FIELDS = 20
MAX_TEXT_LEN = 200
MAX_CHOICES = 20
DURATION_KEY = "duration"


class FieldType(StrEnum):
    DECIMAL = "decimal"
    INTEGER = "integer"
    DURATION = "duration"
    SELECTION = "selection"
    BOOLEAN = "boolean"
    TEXT = "text"


class Aggregation(StrEnum):
    """How values may be combined across *compatible* records. Never 'better/worse'."""

    NONE = "none"
    SUM = "sum"
    MAX = "max"
    MIN = "min"
    LAST = "last"


ShortStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=40)]
FieldKey = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{0,31}$")]


class FieldDefinition(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    key: FieldKey
    label: ShortStr
    type: FieldType
    unit: Annotated[str, StringConstraints(strip_whitespace=True, max_length=12)] | None = None
    required: bool = False
    aggregation: Aggregation = Aggregation.NONE
    duration_format: Literal["h:mm", "mm:ss"] | None = None
    choices: tuple[ShortStr, ...] | None = None
    min_value: Decimal | None = None
    max_value: Decimal | None = None

    @model_validator(mode="after")
    def _check_consistency(self) -> FieldDefinition:
        if self.type is FieldType.DURATION:
            if self.duration_format not in DURATION_FORMATS:
                raise ValueError("duration fields need duration_format")
        elif self.duration_format is not None:
            raise ValueError("duration_format only applies to duration fields")
        if self.type is FieldType.SELECTION:
            if not self.choices or len(self.choices) > MAX_CHOICES:
                raise ValueError("selection fields need 1..20 choices")
            if len(set(self.choices)) != len(self.choices):
                raise ValueError("duplicate choices")
        elif self.choices is not None:
            raise ValueError("choices only apply to selection fields")
        numeric = self.type in (FieldType.DECIMAL, FieldType.INTEGER)
        if not numeric and (self.min_value is not None or self.max_value is not None):
            raise ValueError("bounds only apply to numeric fields")
        if not numeric and self.type is not FieldType.DURATION and self.unit:
            raise ValueError("unit only applies to numeric fields")
        summable = (FieldType.DECIMAL, FieldType.INTEGER, FieldType.DURATION)
        if self.aggregation in (Aggregation.SUM, Aggregation.MAX, Aggregation.MIN) and (
            self.type not in summable
        ):
            raise ValueError("numeric aggregation on non-numeric field")
        return self


class FieldSchema(BaseModel):
    """An ordered set of field definitions for one activity-type version."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    fields: tuple[FieldDefinition, ...] = Field(max_length=MAX_FIELDS)

    @model_validator(mode="after")
    def _unique_keys(self) -> FieldSchema:
        keys = [f.key for f in self.fields]
        if len(keys) != len(set(keys)):
            raise ValueError("duplicate field keys")
        return self

    def get(self, key: str) -> FieldDefinition | None:
        return next((f for f in self.fields if f.key == key), None)


def default_duration_field(label: str) -> FieldDefinition:
    return FieldDefinition(
        key=DURATION_KEY,
        label=label,
        type=FieldType.DURATION,
        duration_format="h:mm",
        aggregation=Aggregation.SUM,
    )


def next_custom_key(schema_fields: tuple[FieldDefinition, ...] | list[FieldDefinition]) -> str:
    """Stable, label-independent keys: f1, f2, ... Never reuse a key within a type."""
    used = {f.key for f in schema_fields}
    n = 1
    while f"f{n}" in used:
        n += 1
    return f"f{n}"


JsonValue = str | int | bool | None

_TRUE = {"да", "д", "yes", "y", "true", "1", "+"}
_FALSE = {"нет", "н", "no", "n", "false", "0", "-"}


def parse_field_value(field: FieldDefinition, raw: str) -> JsonValue:
    """Parse one user-entered value. Decimals are stored as strings to stay exact in JSON."""
    text = raw.strip()
    if not text:
        raise ParseError("empty")
    match field.type:
        case FieldType.DECIMAL:
            value = parse_decimal(text)
            _check_bounds(field, value)
            return format(value.normalize(), "f")
        case FieldType.INTEGER:
            ivalue = parse_int(text)
            _check_bounds(field, Decimal(ivalue))
            return ivalue
        case FieldType.DURATION:
            assert field.duration_format is not None
            return parse_duration(text, field.duration_format)
        case FieldType.BOOLEAN:
            low = text.lower()
            if low in _TRUE:
                return True
            if low in _FALSE:
                return False
            raise ParseError("not_boolean")
        case FieldType.SELECTION:
            assert field.choices is not None
            for choice in field.choices:
                if choice.lower() == text.lower():
                    return choice
            raise ParseError("not_a_choice")
        case FieldType.TEXT:
            if len(text) > MAX_TEXT_LEN:
                raise ParseError("too_long")
            return text
    raise AssertionError(field.type)  # pragma: no cover


def _check_bounds(field: FieldDefinition, value: Decimal) -> None:
    if value < 0 and field.min_value is None:
        raise ParseError("negative")
    if field.min_value is not None and value < field.min_value:
        raise ParseError("below_min")
    if field.max_value is not None and value > field.max_value:
        raise ParseError("above_max")


def validate_values(schema: FieldSchema, values: dict[str, Any]) -> dict[str, JsonValue]:
    """Validate already-normalized values (e.g. from storage or an API) against a schema."""
    if len(values) > MAX_FIELDS:
        raise ParseError("too_many_values")
    out: dict[str, JsonValue] = {}
    for key, value in values.items():
        field = schema.get(key)
        if field is None:
            raise ParseError("unknown_field")
        if value is None:
            out[key] = None
            continue
        if field.type is FieldType.BOOLEAN:
            if not isinstance(value, bool):
                raise ParseError("not_boolean")
            out[key] = value
        elif field.type in (FieldType.INTEGER, FieldType.DURATION):
            if isinstance(value, bool) or not isinstance(value, int):
                raise ParseError("not_an_integer")
            if field.type is FieldType.INTEGER:
                out[key] = parse_field_value(field, str(value))
            elif value < 0:
                raise ParseError("negative")
            else:
                out[key] = value
        elif isinstance(value, str):
            out[key] = parse_field_value(field, value)
        else:
            raise ParseError("bad_value")
    for field in schema.fields:
        if field.required and out.get(field.key) is None:
            raise ParseError("missing_required")
    return out


def format_field_value(field: FieldDefinition, value: JsonValue, yes: str, no: str) -> str:
    if value is None:
        return "—"
    if field.type is FieldType.DURATION and isinstance(value, int):
        return format_duration(value)
    if field.type is FieldType.BOOLEAN:
        return yes if value else no
    if field.type is FieldType.DECIMAL and isinstance(value, str):
        text = format_decimal(Decimal(value), places=3)
    else:
        text = str(value)
    return f"{text} {field.unit}" if field.unit else text
