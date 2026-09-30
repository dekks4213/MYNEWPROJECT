"""Presentation helpers: callback encoding, number/date formatting, workout comparison."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from aiogram.filters.callback_data import CallbackData

from fitcoach.bot.ui import (
    CALLBACK_CLASSES,
    REMINDER_TIMES,
    Ac,
    En,
    Fd,
    Fm,
    Fr,
    Go,
    Hs,
    Ob,
    Rm,
    St,
    Wd,
    hhmm_pack,
    hhmm_unpack,
    num,
    rel_day,
    sets_text,
    signed,
)
from fitcoach.domain.workout import SetSpec, compare_sets
from fitcoach.i18n import Translator
from tests.bot_harness import check_callback_data

BIG = 2**31 - 1
SAMPLES: list[CallbackData] = [
    Go(s="day", a="-365"),
    Ob(mode="set", action="tz", value="America/Argentina/Buenos_Aires"),
    En(action="restore", kind="session", id=BIG, v=BIG),
    Ac(action="program_archive", id=BIG),
    Fd(action="wsave", value="101.65"),
    Fr(a="amt", d=BIG, v=BIG, i=14, x="x1.5"),
    Fm(a="cp", id=BIG, x="2-breakfast"),
    Wd(a="type_ok", d=BIG, v=BIG, t=BIG),
    Hs(a="sessions"),
    St(a="rem_toggle", id=BIG, x="off"),
    Rm(a="snooze", id=BIG),
]


def test_every_factory_has_a_worst_case_sample() -> None:
    assert {type(s) for s in SAMPLES} == set(CALLBACK_CLASSES)
    assert len({c.__prefix__ for c in CALLBACK_CLASSES}) == len(CALLBACK_CLASSES)


@pytest.mark.parametrize("sample", SAMPLES, ids=lambda s: type(s).__name__)
def test_callbacks_round_trip_within_telegram_limit(sample: CallbackData) -> None:
    packed = sample.pack()
    check_callback_data(packed)  # <= 64 bytes and a matching factory
    assert type(sample).unpack(packed) == sample


def test_separator_in_a_value_is_rejected_at_pack_time() -> None:
    with pytest.raises(ValueError):
        St(a="rem_time", x="08:00").pack()


@pytest.mark.parametrize("time", REMINDER_TIMES)
def test_times_travel_without_the_separator(time: str) -> None:
    packed = St(a="rem_time", x=hhmm_pack(time)).pack()
    assert hhmm_unpack(St.unpack(packed).x) == time


def test_numbers_and_dates_are_localized() -> None:
    ru, en = Translator("ru"), Translator("en")
    assert num(ru, Decimal("1840")) == "1\xa0840" and num(en, Decimal("1840")) == "1,840"
    assert num(ru, Decimal("101.80"), 1) == "101,8"
    assert signed(ru, Decimal("-0.2")) == "−0,2" and signed(en, Decimal("0.5")) == "+0.5"
    today = dt.date(2026, 9, 30)
    assert rel_day(ru, today, today) == "сегодня"
    assert rel_day(ru, dt.date(2026, 9, 29), today) == "вчера"
    assert rel_day(ru, dt.date(2026, 9, 1), today) == "1 сентября"
    assert rel_day(en, dt.date(2026, 9, 1), today) == "September 1"


def test_sets_are_shown_in_plain_words() -> None:
    ru = Translator("ru")
    same = [SetSpec(load_kg=Decimal(60), reps=10)] * 3
    assert sets_text(ru, same) == "3 × 10 · 60 кг"
    mixed = [SetSpec(load_kg=Decimal(60), reps=10), SetSpec(load_kg=Decimal(60), reps=8)]
    assert sets_text(ru, mixed) == "60×10, 60×8"
    assert sets_text(ru, [SetSpec(distance_m=50)] * 4) == "4 × 50 м"
    assert sets_text(ru, [SetSpec(distance_m=200, warmup=True)]) == "200 м"


def test_progress_is_only_highlighted_when_better_or_equal() -> None:
    prev = (SetSpec(load_kg=Decimal(60), reps=10),) * 3
    more_reps = (SetSpec(load_kg=Decimal(60), reps=11), *prev[1:])
    heavier = (SetSpec(load_kg=Decimal("62.5"), reps=8),) * 3
    worse = (SetSpec(load_kg=Decimal(60), reps=8),) * 3
    reps = compare_sets(prev, more_reps)
    assert reps is not None and (reps.kind, reps.delta) == ("reps", Decimal(1))
    load = compare_sets(prev, heavier)
    assert load is not None and (load.kind, load.delta) == ("load", Decimal("2.5"))
    same = compare_sets(prev, prev)
    assert same is not None and same.kind == "same"
    assert compare_sets(prev, worse) is None
    assert compare_sets((), prev) is None
