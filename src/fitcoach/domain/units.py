"""Deterministic parsing of user-entered numbers and durations.

All arithmetic uses Decimal. Unknown values are represented as None, never zero.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation


class ParseError(ValueError):
    """Raised when user input cannot be interpreted unambiguously.

    `code` is a stable i18n key suffix used by the UI layer.
    """

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


_NUMBER_RE = re.compile(r"^[+-]?\d{1,9}(?:[.,]\d{1,6})?$")


def parse_decimal(raw: str) -> Decimal:
    """Parse '72,4', '72.4', ' 1 250 ' into Decimal. Rejects NaN/inf/exponents."""
    text = raw.strip().replace(" ", "").replace(" ", "")
    if not _NUMBER_RE.match(text):
        raise ParseError("not_a_number")
    try:
        return Decimal(text.replace(",", "."))
    except InvalidOperation as exc:  # pragma: no cover - regex already guards
        raise ParseError("not_a_number") from exc


def parse_int(raw: str) -> int:
    value = parse_decimal(raw)
    if value != value.to_integral_value():
        raise ParseError("not_an_integer")
    return int(value)


DurationFormat = str  # "h:mm" (bare number = minutes) or "mm:ss" (bare number rejected)
DURATION_FORMATS: tuple[str, ...] = ("h:mm", "mm:ss")

_UNIT_PART_RE = re.compile(
    r"(\d+(?:[.,]\d+)?)\s*(ч|час|часа|часов|h|hr|hrs|мин|м|min|m|сек|с|sec|s)\b",
    re.IGNORECASE,
)
_UNIT_SECONDS = {
    "ч": 3600,
    "час": 3600,
    "часа": 3600,
    "часов": 3600,
    "h": 3600,
    "hr": 3600,
    "hrs": 3600,
    "мин": 60,
    "м": 60,
    "min": 60,
    "m": 60,
    "сек": 1,
    "с": 1,
    "sec": 1,
    "s": 1,
}
_MAX_DURATION_SECONDS = 7 * 24 * 3600


def parse_duration(raw: str, fmt: DurationFormat) -> int:
    """Return whole seconds.

    Explicit units ('1ч 30мин', '90 sec', '45 min') are always accepted.
    Colon notation depends on the field's declared format:
      - 'h:mm': '1:30' = 5400 s; bare '45' = 45 minutes.
      - 'mm:ss': '1:30' = 90 s; bare numbers are ambiguous and rejected.
    'h:mm:ss' with three parts is unambiguous for both formats.
    """
    if fmt not in DURATION_FORMATS:
        raise ValueError(f"unknown duration format {fmt!r}")
    text = raw.strip().lower().replace(",", ".")
    if not text:
        raise ParseError("empty")

    seconds: Decimal
    if ":" in text:
        parts = text.split(":")
        if not all(p.isdigit() for p in parts) or len(parts) > 3:
            raise ParseError("bad_duration")
        nums = [int(p) for p in parts]
        if len(nums) == 3:
            h, m, s = nums
            if m >= 60 or s >= 60:
                raise ParseError("bad_duration")
            seconds = Decimal(h * 3600 + m * 60 + s)
        else:
            a, b = nums
            if b >= 60:
                raise ParseError("bad_duration")
            seconds = Decimal(a * 3600 + b * 60) if fmt == "h:mm" else Decimal(a * 60 + b)
    else:
        matches = list(_UNIT_PART_RE.finditer(text))
        if matches:
            consumed = _UNIT_PART_RE.sub("", text).strip()
            if consumed:
                raise ParseError("bad_duration")
            seconds = sum(
                (Decimal(m.group(1)) * _UNIT_SECONDS[m.group(2).lower()] for m in matches),
                Decimal(0),
            )
        elif fmt == "h:mm":
            seconds = parse_decimal(text) * 60
        else:
            raise ParseError("ambiguous_duration")

    if seconds < 0:
        raise ParseError("negative")
    result = int(seconds.to_integral_value())
    if result > _MAX_DURATION_SECONDS:
        raise ParseError("too_large")
    return result


def format_duration(seconds: int) -> str:
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}" if s else f"{h}:{m:02d}"
    return f"{m}:{s:02d}"


def format_decimal(value: Decimal | None, places: int = 1) -> str:
    if value is None:
        return "—"
    q = value.quantize(Decimal(1).scaleb(-places))
    text = format(q, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text
