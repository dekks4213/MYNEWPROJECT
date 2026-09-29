"""Timezone-aware reminder schedule arithmetic (pure functions)."""

from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

ALL_DAYS = 0b1111111  # bit 0 = Monday
WEEKDAYS = 0b0011111


def parse_hhmm(text: str) -> dt.time:
    parts = text.strip().replace(".", ":").split(":")
    if len(parts) != 2 or not all(p.isdigit() for p in parts):
        raise ValueError("bad_time")
    h, m = int(parts[0]), int(parts[1])
    if not (0 <= h < 24 and 0 <= m < 60):
        raise ValueError("bad_time")
    return dt.time(h, m)


def in_quiet_hours(t: dt.time, start: dt.time | None, end: dt.time | None) -> bool:
    if start is None or end is None or start == end:
        return False
    if start < end:
        return start <= t < end
    return t >= start or t < end  # window crosses midnight


def next_fire(
    local_time: dt.time,
    days_mask: int,
    tz: ZoneInfo,
    after: dt.datetime,
    quiet_start: dt.time | None = None,
    quiet_end: dt.time | None = None,
) -> dt.datetime | None:
    """Next UTC instant strictly after `after` at `local_time` on an allowed weekday.

    A reminder that falls into quiet hours is moved to the end of the quiet window.
    DST: nonexistent local times resolve forward via zoneinfo normalization.
    """
    if not 1 <= days_mask <= ALL_DAYS:
        return None
    local_after = after.astimezone(tz)
    for offset in range(0, 9):
        day = local_after.date() + dt.timedelta(days=offset)
        if not days_mask & (1 << day.weekday()):
            continue
        fire_time = local_time
        if in_quiet_hours(fire_time, quiet_start, quiet_end):
            assert quiet_end is not None
            fire_time = quiet_end
        candidate = dt.datetime.combine(day, fire_time, tzinfo=tz)
        # Normalize (handles DST gaps) and compare in UTC.
        candidate_utc = candidate.astimezone(dt.UTC)
        if candidate_utc > after.astimezone(dt.UTC):
            return candidate_utc
    return None
