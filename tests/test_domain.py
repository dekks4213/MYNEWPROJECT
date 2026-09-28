from decimal import Decimal

import pytest
from pydantic import ValidationError

from fitcoach.domain.fields import (
    Aggregation,
    FieldDefinition,
    FieldSchema,
    FieldType,
    default_duration_field,
    next_custom_key,
    parse_field_value,
    validate_values,
)
from fitcoach.domain.nutrition import NutrientValues, day_totals, parse_kcal, parse_macros
from fitcoach.domain.units import ParseError, format_duration, parse_decimal, parse_duration


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("72,4", Decimal("72.4")), ("72.4", Decimal("72.4")), (" 1 250 ", Decimal(1250)), ("0", 0)],
)
def test_parse_decimal_accepts_comma_and_spaces(raw: str, expected: Decimal) -> None:
    assert parse_decimal(raw) == expected


@pytest.mark.parametrize("raw", ["", "abc", "1e5", "NaN", "inf", "1,2,3", "1.2.3", "--1"])
def test_parse_decimal_rejects_garbage(raw: str) -> None:
    with pytest.raises(ParseError):
        parse_decimal(raw)


def test_mmss_field_interprets_1_30_as_90_seconds() -> None:
    assert parse_duration("1:30", "mm:ss") == 90


def test_hmm_field_interprets_1_30_as_90_minutes() -> None:
    assert parse_duration("1:30", "h:mm") == 5400


def test_bare_number_in_mmss_is_ambiguous() -> None:
    with pytest.raises(ParseError) as err:
        parse_duration("90", "mm:ss")
    assert err.value.code == "ambiguous_duration"


@pytest.mark.parametrize(
    ("raw", "fmt", "seconds"),
    [
        ("45", "h:mm", 2700),
        ("45 мин", "mm:ss", 2700),
        ("1ч 30мин", "h:mm", 5400),
        ("90 sec", "h:mm", 90),
        ("1:02:03", "mm:ss", 3723),
        ("1,5 h", "h:mm", 5400),
    ],
)
def test_duration_explicit_forms(raw: str, fmt: str, seconds: int) -> None:
    assert parse_duration(raw, fmt) == seconds


@pytest.mark.parametrize("raw", ["1:75", "abc", "1:2:3:4", "5 parsecs", "-5 min"])
def test_duration_rejects_invalid(raw: str) -> None:
    with pytest.raises(ParseError):
        parse_duration(raw, "h:mm")


def test_format_duration() -> None:
    assert format_duration(90) == "1:30"
    assert format_duration(5400) == "1:30"
    assert format_duration(5401) == "1:30:01"


def test_unknown_nutrients_are_not_zero() -> None:
    totals = day_totals(
        [
            NutrientValues(energy_kcal=Decimal(300), protein_g=Decimal(10)),
            NutrientValues(energy_kcal=None),
            NutrientValues(energy_kcal=Decimal("150.5")),
        ]
    )
    assert totals.energy_kcal.value == Decimal("450.5")
    assert totals.energy_kcal.known_entries == 2
    assert totals.energy_kcal.unknown_entries == 1
    assert totals.protein_g.unknown_entries == 2


def test_parse_macros_supports_unknown_component_and_commas() -> None:
    assert parse_macros("20/10,5/-") == (Decimal(20), Decimal("10.5"), None)
    with pytest.raises(ParseError):
        parse_macros("20/10")
    with pytest.raises(ParseError):
        parse_kcal("-5")


def _custom_number() -> FieldDefinition:
    return FieldDefinition(key="f1", label="Laps", type=FieldType.INTEGER, unit="laps")


def test_field_schema_rejects_duplicate_keys_and_code_like_extras() -> None:
    with pytest.raises(ValidationError):
        FieldSchema(fields=(_custom_number(), _custom_number()))
    with pytest.raises(ValidationError):
        FieldDefinition.model_validate(
            {"key": "f1", "label": "x", "type": "decimal", "formula": "__import__('os')"}
        )
    with pytest.raises(ValidationError):
        FieldDefinition.model_validate({"key": "F-1; drop", "label": "x", "type": "decimal"})
    with pytest.raises(ValidationError):
        FieldDefinition.model_validate({"key": "f1", "label": "x", "type": "python"})


def test_field_definition_consistency() -> None:
    with pytest.raises(ValidationError):
        FieldDefinition(key="f1", label="x", type=FieldType.DURATION)  # no format
    with pytest.raises(ValidationError):
        FieldDefinition(key="f1", label="x", type=FieldType.TEXT, aggregation=Aggregation.SUM)
    with pytest.raises(ValidationError):
        FieldDefinition(key="f1", label="x", type=FieldType.SELECTION, choices=())


def test_parse_field_values() -> None:
    dec = FieldDefinition(key="f1", label="Distance", type=FieldType.DECIMAL, unit="km")
    assert parse_field_value(dec, "12,50") == "12.5"
    boolean = FieldDefinition(key="f2", label="Rain", type=FieldType.BOOLEAN)
    assert parse_field_value(boolean, "да") is True
    sel = FieldDefinition(
        key="f3", label="Surface", type=FieldType.SELECTION, choices=("sand", "mud")
    )
    assert parse_field_value(sel, "MUD") == "mud"
    with pytest.raises(ParseError):
        parse_field_value(sel, "ice")
    with pytest.raises(ParseError):
        parse_field_value(dec, "-3")


def test_validate_values_requires_required_and_known_keys() -> None:
    schema = FieldSchema(
        fields=(
            default_duration_field("Duration"),
            FieldDefinition(key="f1", label="Laps", type=FieldType.INTEGER, required=True),
        )
    )
    assert validate_values(schema, {"duration": 60, "f1": 3}) == {"duration": 60, "f1": 3}
    with pytest.raises(ParseError):
        validate_values(schema, {"duration": 60})
    with pytest.raises(ParseError):
        validate_values(schema, {"f1": 1, "hack": 1})
    with pytest.raises(ParseError):
        validate_values(schema, {"f1": True})


def test_next_custom_key_never_depends_on_label() -> None:
    fields = [default_duration_field("D"), _custom_number()]
    assert next_custom_key(fields) == "f2"
