"""Telegram presentation: callback data, keyboards and screen texts. No business rules here.

Callback data rules (Telegram allows 64 bytes):
  * payloads carry only ids, short action codes, indexes and plain numbers;
  * never user text, names or anything containing ":" (aiogram's separator);
  * times travel as "0800", fractions as "0.5", weights as "101.6".
`CALLBACK_CLASSES` lists every factory; tests pack/unpack them and the E2E harness checks
every button the bot sends against them.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from decimal import Decimal
from typing import Any

from aiogram.filters.callback_data import CallbackData
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)

from fitcoach.db.models import FoodEntry, WorkoutSession
from fitcoach.domain.fields import (
    DURATION_KEY,
    FieldDefinition,
    FieldSchema,
    FieldType,
    format_field_value,
)
from fitcoach.domain.food import NutrientSource
from fitcoach.domain.nutrition import NutrientTotal, Precision
from fitcoach.domain.workout import Item, SetSpec, SetWords, WorkoutBody, format_set, totals
from fitcoach.i18n import Translator
from fitcoach.services.food import DraftItem, FoodDraftState
from fitcoach.services.summary import DaySummary

Row = list[tuple[str, CallbackData]]

# --- callback data -----------------------------------------------------------------------------


class Go(CallbackData, prefix="go"):
    """Stateless navigation: screen code + optional short argument (offset, section)."""

    s: str
    a: str = ""


class Ob(CallbackData, prefix="ob"):
    """Onboarding (mode 'ob') and shared profile choices (mode 'set')."""

    mode: str
    action: str
    value: str = ""


class En(CallbackData, prefix="en"):
    action: str  # del | restore | edit
    kind: str
    id: int
    v: int = 0


class Ac(CallbackData, prefix="ac"):
    action: str
    id: int = 0


class Fd(CallbackData, prefix="fd"):
    action: str
    value: str = ""


class Fr(CallbackData, prefix="fr"):
    """Food draft actions. `v` is the draft version shown to the user."""

    a: str
    d: int
    v: int
    i: int = 0
    x: str = ""


class Fm(CallbackData, prefix="fm"):
    a: str
    id: int = 0
    x: str = ""


class Wd(CallbackData, prefix="wd"):
    a: str
    d: int
    v: int
    t: int = 0


class Hs(CallbackData, prefix="hs"):
    a: str


class St(CallbackData, prefix="st"):
    a: str
    id: int = 0
    x: str = ""


class Rm(CallbackData, prefix="rm"):
    a: str
    id: int
    x: str = ""


CALLBACK_CLASSES: tuple[type[CallbackData], ...] = (Go, Ob, En, Ac, Fd, Fr, Fm, Wd, Hs, St, Rm)


def hhmm_pack(value: str) -> str:
    """'08:00' -> '0800' (':' is the callback separator)."""
    return value.replace(":", "")


def hhmm_unpack(value: str) -> str:
    return f"{value[:2]}:{value[2:]}" if len(value) == 4 and value.isdigit() else value


# --- constants ---------------------------------------------------------------------------------

# Persistent reply keyboard: the four things people do every day.
MENU_KEYS = ("menu.food", "menu.training", "menu.day", "menu.home")
# Labels of older keyboards that may still be on users' screens.
LEGACY_MENU = {
    "menu.old_day": "day",
    "menu.old_food": "food",
    "menu.old_training": "train",
    "menu.old_history": "hist",
    "menu.old_profile": "set",
    "menu.old_settings": "set",
}
# (IANA zone, i18n label key). Users pick a city; the IANA name is stored.
TIMEZONES = (
    ("Europe/Kaliningrad", "tz.kaliningrad"),
    ("Europe/Moscow", "tz.moscow"),
    ("Europe/Samara", "tz.samara"),
    ("Asia/Yekaterinburg", "tz.yekaterinburg"),
    ("Asia/Omsk", "tz.omsk"),
    ("Asia/Novosibirsk", "tz.novosibirsk"),
    ("Asia/Krasnoyarsk", "tz.krasnoyarsk"),
    ("Asia/Irkutsk", "tz.irkutsk"),
    ("Asia/Vladivostok", "tz.vladivostok"),
    ("Europe/Minsk", "tz.minsk"),
    ("Europe/Kyiv", "tz.kyiv"),
    ("Asia/Almaty", "tz.almaty"),
    ("Asia/Tashkent", "tz.tashkent"),
    ("Asia/Tbilisi", "tz.tbilisi"),
    ("Europe/Berlin", "tz.berlin"),
    ("Europe/London", "tz.london"),
)
# The first screen shows a few likely cities per language; "Другой город…" shows all.
TZ_SHORTLIST = {
    "ru": (
        "Europe/Moscow",
        "Europe/Kaliningrad",
        "Europe/Samara",
        "Asia/Yekaterinburg",
        "Asia/Novosibirsk",
    ),
    "en": ("Europe/London", "Europe/Berlin", "Europe/Moscow"),
}
KCAL_PRESETS = ("1800", "2000", "2200", "2500")
DURATION_PRESETS_MIN = (30, 45, 60, 90)
REMINDER_PRESETS = (("rem.morning", "08:00"), ("rem.day", "13:00"), ("rem.evening", "19:00"))
REMINDER_TIMES = (
    "07:00",
    "08:00",
    "09:00",
    "10:00",
    "12:00",
    "14:00",
    "18:00",
    "20:00",
    "21:00",
    "22:00",
)
QUIET_PRESETS = ("22:00-08:00", "23:00-07:00")
GRAM_PRESETS = ("50", "100", "150", "200", "250", "300")
MEAL_ORDER = ("breakfast", "lunch", "dinner", "snack")
MEAL_ICONS = {"breakfast": "🍳", "lunch": "🍲", "dinner": "🍽", "snack": "🍎"}
WEIGHT_STEPS = ("-0.5", "-0.2", "+0.2", "+0.5")
# A 1–10 scale is "how hard was it"; people answer with words, the number is kept.
EFFORT_PRESETS = (
    (3, "effort.easy"),
    (5, "effort.normal"),
    (7, "effort.hard"),
    (9, "effort.very_hard"),
)


# --- keyboards ---------------------------------------------------------------------------------


def main_menu(tr: Translator) -> ReplyKeyboardMarkup:
    labels = [tr(k) for k in MENU_KEYS]
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=t) for t in labels[:2]],
            [KeyboardButton(text=t) for t in labels[2:]],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


def inline(*rows: Sequence[tuple[str, CallbackData]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=t, callback_data=cb.pack()) for t, cb in row]
            for row in rows
            if row
        ]
    )


def grid(buttons: Sequence[tuple[str, CallbackData]], width: int = 2) -> list[Row]:
    return [list(buttons[i : i + width]) for i in range(0, len(buttons), width)]


def nav(
    tr: Translator,
    back: CallbackData | None = None,
    *,
    home: bool = True,
    cancel: bool = False,
) -> Row:
    """The standard bottom row: ← Назад / 🏠 Главное / ✕ Отмена."""
    row: Row = []
    if back is not None:
        row.append((tr("nav.back"), back))
    if cancel:
        row.append((tr("nav.cancel"), Fd(action="cancel")))
    elif home:
        row.append((tr("nav.home"), Go(s="home")))
    return row


def cancel_kb(tr: Translator, back: CallbackData | None = None) -> InlineKeyboardMarkup:
    return inline(nav(tr, back, cancel=True))


# --- numbers and dates -------------------------------------------------------------------------


def num(tr: Translator, value: Decimal | int | None, places: int = 0) -> str:
    """Locale-aware number: '1 840', '101,8' (ru) / '1,840', '101.8' (en)."""
    if value is None:
        return "—"
    q = (
        Decimal(value).quantize(Decimal(1).scaleb(-places))
        if places
        else Decimal(value).quantize(Decimal(1))
    )
    sign = "-" if q < 0 else ""
    whole, _, frac = format(abs(q), "f").partition(".")
    frac = frac.rstrip("0")
    groups: list[str] = []
    while len(whole) > 3:
        groups.insert(0, whole[-3:])
        whole = whole[:-3]
    groups.insert(0, whole)
    text = tr("fmt.thousands").join(groups)
    if frac:
        text += tr("fmt.decimal") + frac
    return sign + text


def signed(tr: Translator, value: Decimal, places: int = 1) -> str:
    text = num(tr, abs(value), places)
    if value > 0:
        return "+" + text
    if value < 0:
        return "−" + text
    return text


def day_month(tr: Translator, day: dt.date) -> str:
    return tr("fmt.day_month", d=day.day, m=tr(f"month.{day.month}"))


def rel_day(tr: Translator, day: dt.date, today: dt.date) -> str:
    """'сегодня', 'вчера', '3 дн. назад' or '28 сентября'."""
    delta = (today - day).days
    if delta == 0:
        return tr("rel.today")
    if delta == 1:
        return tr("rel.yesterday")
    if delta == -1:
        return tr("rel.tomorrow")
    if 1 < delta < 7:
        return tr("rel.days_ago", n=delta)
    return day_month(tr, day)


def minutes_text(tr: Translator, seconds: int | None) -> str:
    if seconds is None:
        return "—"
    minutes = round(seconds / 60)
    if minutes < 60:
        return tr("fmt.minutes", n=minutes)
    h, m = divmod(minutes, 60)
    return tr("fmt.hours_minutes", h=h, m=m) if m else tr("fmt.hours", h=h)


def words(tr: Translator) -> SetWords:
    return SetWords(
        tr("unit.m"),
        tr("unit.sec"),
        tr("unit.min"),
        tr("word.rest"),
        tr("word.warmup_short"),
        tr("word.effort"),
        tr("word.reserve"),
    )


def sets_text(tr: Translator, sets: Sequence[SetSpec]) -> str:
    """'3 × 10 · 60 кг' for identical sets, otherwise '60×10, 60×9, 60×8'."""
    work = [s for s in sets if not s.warmup] or [
        s.model_copy(update={"warmup": False}) for s in sets
    ]
    if not work:
        return "—"
    first = work[0]
    same = len(work) > 1 and all(s == first for s in work)
    if same and first.reps is not None and first.distance_m is None:
        text = f"{len(work)} × {first.reps}"
        if first.load_kg is not None:
            text += f" · {num(tr, first.load_kg, 2)} {tr('unit.kg')}"
        return text
    w = words(tr)
    if same:
        return f"{len(work)} × {format_set(first, w)}"
    return ", ".join(format_set(s, w) for s in work)


def item_line(tr: Translator, item: Item) -> str:
    """'• Жим лёжа — 3 × 10 · 60 кг'; a bare distance ('200м') shows only the sets."""
    if item.name[:1].isdigit():
        return f"• {sets_text(tr, item.sets)}"
    return f"• {item.name} — {sets_text(tr, item.sets)}"


# --- diary lines -------------------------------------------------------------------------------


def food_line(tr: Translator, entry: FoodEntry) -> str:
    kcal = (
        f"{num(tr, entry.energy_kcal)} {tr('unit.kcal')}"
        if entry.energy_kcal is not None
        else tr("food.kcal_unknown")
    )
    return f"{entry.name} — {kcal}"


def session_duration(session: WorkoutSession) -> int | None:
    value = session.values.get(DURATION_KEY)
    return value if isinstance(value, int) else None


def session_title(session: WorkoutSession) -> str:
    return session.template_name or session.activity_name


def session_line(tr: Translator, session: WorkoutSession) -> str:
    schema = FieldSchema.model_validate({"fields": session.field_snapshot})
    parts = []
    for field in schema.fields:
        value = session.values.get(field.key)
        if value is not None:
            parts.append(f"{field.label}: {field_value_text(tr, field, value)}")
    if session.blocks:
        t = totals(WorkoutBody.load(session.blocks))
        if t.volume_kg is not None:
            parts.append(tr("wo.volume", kg=num(tr, t.volume_kg)))
        if t.distance_m is not None:
            parts.append(tr("wo.distance_m", m=num(tr, t.distance_m)))
        if t.working_sets:
            parts.append(tr("wo.sets", n=t.working_sets))
    title = session_title(session)
    if session.template_name and session.template_name != session.activity_name:
        title = f"{session.activity_name} — {session.template_name}"
    return title + (": " + ", ".join(parts) if parts else "")


def format_body(tr: Translator, body: WorkoutBody) -> list[str]:
    """Blocks as short lines: a header per block, one line per exercise."""
    lines: list[str] = []
    for block in body.blocks:
        header = block.title or tr("block." + block.kind.value)
        if block.rounds > 1:
            header += f" × {block.rounds}"
        if len(body.blocks) > 1 or block.kind.value != "main" or block.title:
            lines.append(header)
        lines.extend(item_line(tr, item) for item in block.items)
    return lines


# --- home and day ------------------------------------------------------------------------------


def greeting(tr: Translator, hour: int, name: str | None) -> str:
    key = (
        "home.morning"
        if 5 <= hour < 12
        else "home.afternoon"
        if 12 <= hour < 18
        else "home.evening"
        if 18 <= hour < 23
        else "home.night"
    )
    return tr(key + "_named", name=name) if name else tr(key)


def _kcal_line(tr: Translator, total: NutrientTotal, target: Decimal | None) -> str:
    text = num(tr, total.value)
    if target is not None:
        text += f" / {num(tr, target)}"
    return f"{text} {tr('unit.kcal')}"


def _gram_line(tr: Translator, total: NutrientTotal, target: Decimal | None) -> str:
    text = num(tr, total.value) if total.known_entries else "—"
    if target is not None:
        text += f" / {num(tr, target)}"
    return f"{text} {tr('unit.g')}"


def format_home(
    tr: Translator,
    s: DaySummary,
    *,
    name: str | None,
    hour: int,
    protein_target: Decimal | None,
) -> str:
    lines = [tr("home.title"), "", greeting(tr, hour, name), ""]
    t = s.totals
    lines.append(tr("home.today"))
    if t.energy_kcal.known_entries:
        lines.append(_kcal_line(tr, t.energy_kcal, s.kcal_target))
        if protein_target is not None or t.protein_g.known_entries:
            lines.append(f"{tr('day.protein')}: {_gram_line(tr, t.protein_g, protein_target)}")
    elif t.entries:
        lines.append(tr("home.food_no_kcal", n=t.entries))
    else:
        lines.append(tr("home.food_cta"))
    lines += ["", tr("home.training")]
    if s.sessions:
        last = s.sessions[-1]
        duration = session_duration(last)
        extra = f" · {minutes_text(tr, duration)}" if duration else ""
        lines.append(f"✓ {session_title(last)}{extra}")
    elif s.planned_open:
        lines.append(tr("home.planned", name=s.planned_open[0][1].name))
    else:
        lines.append(tr("home.training_cta"))
    lines += ["", tr("home.weight")]
    w = s.weights_today[-1] if s.weights_today else s.latest_weight
    if w is not None:
        lines.append(
            f"{num(tr, w.weight_kg, 1)} {tr('unit.kg')} · {rel_day(tr, w.local_date, s.day)}"
        )
    else:
        lines.append(tr("home.weight_cta"))
    return "\n".join(lines)


def home_kb(tr: Translator) -> InlineKeyboardMarkup:
    return inline(
        [(tr("home.btn_food"), Fm(a="menu")), (tr("home.btn_training"), Go(s="train"))],
        [(tr("home.btn_weight"), Go(s="weight")), (tr("home.btn_day"), Go(s="day", a="0"))],
        [(tr("home.btn_plan"), Go(s="plan")), (tr("home.btn_more"), Go(s="more"))],
    )


def day_title(tr: Translator, day: dt.date, today: dt.date) -> str:
    delta = (today - day).days
    label = (
        tr("rel.today_cap")
        if delta == 0
        else tr("rel.yesterday_cap")
        if delta == 1
        else tr("rel.tomorrow_cap")
        if delta == -1
        else tr(
            ("wd.mon", "wd.tue", "wd.wed", "wd.thu", "wd.fri", "wd.sat", "wd.sun")[day.weekday()]
        )
    )
    return f"📊 {label} · {day_month(tr, day)}"


def format_day(
    tr: Translator,
    s: DaySummary,
    today: dt.date,
    *,
    protein: Decimal | None = None,
    fat: Decimal | None = None,
    carbs: Decimal | None = None,
) -> str:
    lines = [day_title(tr, s.day, today), "", tr("day.nutrition")]
    t = s.totals
    if not t.entries:
        lines.append(tr("day.no_food"))
    elif not t.energy_kcal.known_entries:
        lines.append(tr("home.food_no_kcal", n=t.entries))
    else:
        lines.append(_kcal_line(tr, t.energy_kcal, s.kcal_target))
        macros = (
            (t.protein_g, protein, "day.protein"),
            (t.fat_g, fat, "day.fat"),
            (t.carbs_g, carbs, "day.carbs"),
        )
        if any(total.known_entries or target is not None for total, target, _ in macros):
            lines.append("")
            lines += [
                f"{tr(key)}  {_gram_line(tr, total, target)}" for total, target, key in macros
            ]
        if t.energy_kcal.unknown_entries:
            lines.append(tr("day.incomplete", n=t.energy_kcal.unknown_entries))
    lines += ["", tr("day.training")]
    for session in s.sessions:
        lines.append(f"✓ {session_title(session)}")
        duration = session_duration(session)
        if duration:
            lines.append(minutes_text(tr, duration))
    for _, v in s.planned_open:
        lines.append(tr("day.planned", name=v.name))
    if not s.sessions and not s.planned_open:
        lines.append(tr("day.no_training"))
    lines += ["", tr("day.weight")]
    if s.weights_today:
        lines.append(f"{num(tr, s.weights_today[-1].weight_kg, 1)} {tr('unit.kg')}")
    else:
        lines.append(tr("day.weight_none"))
    return "\n".join(lines)


# --- food draft preview ------------------------------------------------------------------------


def _amount(tr: Translator, item: DraftItem) -> str | None:
    if item.amount is not None and item.unit is not None:
        text = f"{num(tr, item.amount, 2)} {tr('funit.' + item.unit.value)}"
    elif item.grams is not None:
        text = f"{num(tr, item.grams)} {tr('unit.g')}"
    else:
        return None
    if item.uncertain or item.quantity_estimated:
        text += "?"
    return text


def draft_item_line(tr: Translator, item: DraftItem) -> str:
    """'Овсянка — 100 г'; a copied entry without an amount shows its calories instead."""
    name = item.name + (f" ({item.brand})" if item.brand else "")
    amount = _amount(tr, item)
    if amount is not None:
        line = f"{name} — {amount}"
        if item.energy_kcal is None:
            line += " · " + tr("food.kcal_unknown")
        return line
    if item.energy_kcal is not None:
        return f"{name} · {num(tr, item.energy_kcal)} {tr('unit.kcal')}"
    return f"{name} — ? · {tr('food.kcal_unknown')}"


def _approximate(state: FoodDraftState) -> bool:
    """Only items that contribute calories can make the total approximate."""
    return any(
        i.energy_kcal is not None
        and (i.precision is Precision.APPROXIMATE or i.quantity_estimated or i.uncertain)
        for i in state.items
    )


def format_food_draft(tr: Translator, state: FoodDraftState, *, title: str | None = None) -> str:
    meal = state.meal_type.value
    header = title or f"{MEAL_ICONS[meal]} {tr('meal.' + meal)}"
    lines = [header + (" · " + tr("draft.looks_like") if state.ai and not title else "")]
    if state.transcript:
        lines.append(tr("draft.transcript", text=state.transcript[:300]))
    lines.append("")
    lines.extend(draft_item_line(tr, item) for item in state.items)
    if not state.items:
        lines.append(tr("draft.empty"))
    t = state.totals()
    unknown = sum(1 for i in state.items if i.energy_kcal is None)
    if t.energy_kcal is None and state.items:
        lines += ["", tr("draft.kcal_unknown_hint")]
    if t.energy_kcal is not None:
        approx = "≈ " if _approximate(state) else ""
        lines += ["", f"{approx}{num(tr, t.energy_kcal)} {tr('unit.kcal')}"]
        if any(v is not None for v in (t.protein_g, t.fat_g, t.carbs_g)):
            lines.append(
                f"{tr('macro.P')} {num(tr, t.protein_g)} · {tr('macro.F')} {num(tr, t.fat_g)}"
                f" · {tr('macro.C')} {num(tr, t.carbs_g)}"
            )
        if unknown:
            lines.append(tr("draft.total_unknown", n=unknown))
    notes = []
    if state.clarification:
        notes.append("❓ " + state.clarification)
    sources = {i.nutrient_source for i in state.items}
    if _approximate(state):
        notes.append(tr("draft.note_estimate"))
    if NutrientSource.USDA in sources:
        notes.append(tr("draft.note_usda"))
    if NutrientSource.OFF in sources:
        notes.append(tr("draft.note_off"))
    if state.mock:
        notes.append(tr("draft.mock"))
    if notes:
        lines.append("")
        lines.extend(notes)
    return "\n".join(lines)


def food_draft_kb(
    tr: Translator, draft_id: int, version: int, state: FoodDraftState
) -> InlineKeyboardMarkup:
    d, v = draft_id, version
    if not state.items:
        return inline(
            [(tr("draft.add"), Fr(a="add", d=d, v=v)), (tr("nav.cancel"), Fr(a="no", d=d, v=v))]
        )
    if state.origin == "copy":
        return inline(
            [
                (tr("draft.add_today"), Fr(a="ok", d=d, v=v)),
                (tr("draft.edit"), Fr(a="edits", d=d, v=v)),
            ],
            [(tr("nav.back"), Fr(a="back", d=d, v=v))],
        )
    confirm = tr("draft.confirm_ai") if state.ai else tr("draft.confirm")
    return inline(
        [(confirm, Fr(a="ok", d=d, v=v)), (tr("draft.edit"), Fr(a="edits", d=d, v=v))],
        [(tr("draft.add"), Fr(a="add", d=d, v=v)), (tr("nav.cancel"), Fr(a="no", d=d, v=v))],
    )


def food_edit_kb(
    tr: Translator, draft_id: int, version: int, state: FoodDraftState
) -> InlineKeyboardMarkup:
    d, v = draft_id, version
    rows: list[Row] = []
    for i, item in enumerate(state.items):
        label = item.name if len(item.name) <= 22 else item.name[:21] + "…"
        amount = _amount(tr, item) or "?"
        rows.append([(f"✏️ {label} · {amount}", Fr(a="edit", d=d, v=v, i=i))])
    meal = state.meal_type.value
    rows.append([(f"{MEAL_ICONS[meal]} {tr('meal.' + meal)} ↻", Fr(a="meal", d=d, v=v))])
    rows.append([(tr("draft.save_meal"), Fr(a="fav", d=d, v=v))])
    rows.append([(tr("nav.back"), Fr(a="show", d=d, v=v))])
    return inline(*rows)


def amount_kb(tr: Translator, draft_id: int, version: int, index: int) -> InlineKeyboardMarkup:
    d, v, i = draft_id, version, index
    scale = [("½", "x0.5"), ("×1,5" if tr.language == "ru" else "×1.5", "x1.5"), ("×2", "x2")]
    grams = [(f"{g} {tr('unit.g')}", f"g{g}") for g in GRAM_PRESETS]
    return inline(
        [(label, Fr(a="amt", d=d, v=v, i=i, x=x)) for label, x in scale],
        [(label, Fr(a="amt", d=d, v=v, i=i, x=x)) for label, x in grams[:3]],
        [(label, Fr(a="amt", d=d, v=v, i=i, x=x)) for label, x in grams[3:]],
        [
            (tr("draft.remove"), Fr(a="del", d=d, v=v, i=i)),
            (tr("nav.back"), Fr(a="edits", d=d, v=v)),
        ],
    )


# --- activity fields ---------------------------------------------------------------------------


def is_effort(field: FieldDefinition) -> bool:
    return (
        field.type is FieldType.INTEGER
        and field.min_value == 1
        and field.max_value == 10
        and not field.unit
    )


def is_stars(field: FieldDefinition) -> bool:
    return (
        field.type is FieldType.INTEGER
        and field.min_value == 1
        and field.max_value == 5
        and not field.unit
    )


_DISTANCE_UNITS = {"км", "km", "м", "m", "mi"}


def field_icon(field: FieldDefinition) -> str:
    if field.type is FieldType.DURATION:
        return "⏱"
    if is_effort(field):
        return "💪"
    if is_stars(field):
        return "⭐"
    if field.type in (FieldType.DECIMAL, FieldType.INTEGER):
        return "📏" if (field.unit or "").lower() in _DISTANCE_UNITS else "🔢"
    if field.type is FieldType.SELECTION:
        return "🔘"
    if field.type is FieldType.BOOLEAN:
        return "☑️"
    return "📝"


def effort_word(tr: Translator, value: int) -> str:
    for limit, key in EFFORT_PRESETS:
        if value <= limit:
            return tr(key)
    return tr(EFFORT_PRESETS[-1][1])


def field_value_text(tr: Translator, field: FieldDefinition, value: Any) -> str:
    if (
        field.type is FieldType.DURATION
        and isinstance(value, int)
        and field.duration_format == "h:mm"
    ):
        return minutes_text(tr, value)
    if is_effort(field) and isinstance(value, int):
        return f"{effort_word(tr, value)} · {value}/10"
    if is_stars(field) and isinstance(value, int):
        return "⭐" * value
    return format_field_value(field, value, tr("word.yes"), tr("word.no"))


def field_line(tr: Translator, field: FieldDefinition) -> str:
    return f"{field_icon(field)} {field.label}"


def field_prompt(
    tr: Translator,
    title: str,
    field: FieldDefinition,
    target: Any | None,
    step: int,
    total: int,
) -> str:
    lines = [title, tr("rec.step", n=step, total=total), "", f"{field_icon(field)} {field.label}"]
    if field.type is FieldType.DURATION:
        lines.append(
            tr("hint.duration_hmm" if field.duration_format == "h:mm" else "hint.duration_mmss")
        )
    elif field.type is FieldType.DECIMAL:
        lines.append(tr("hint.decimal"))
    elif field.type is FieldType.TEXT:
        lines.append(tr("hint.text"))
    elif field.type is FieldType.INTEGER and not (is_effort(field) or is_stars(field)):
        lines.append(tr("hint.integer"))
    if target is not None:
        lines.append(tr("hint.target", value=field_value_text(tr, field, target)))
    return "\n".join(lines)


def field_input_kb(
    tr: Translator, field: FieldDefinition, *, timer_min: int | None, more: bool
) -> InlineKeyboardMarkup:
    """Answers as buttons; typing stays possible for everything."""
    rows: list[Row] = []
    if field.type is FieldType.SELECTION and field.choices:
        rows += grid([(c, Fd(action="choice", value=str(i))) for i, c in enumerate(field.choices)])
    elif field.type is FieldType.BOOLEAN:
        rows.append(
            [
                (tr("word.yes_cap"), Fd(action="bool", value="1")),
                (tr("word.no_cap"), Fd(action="bool", value="0")),
            ]
        )
    elif field.type is FieldType.DURATION and field.duration_format == "h:mm":
        if timer_min:
            rows.append([(tr("rec.timer", n=timer_min), Fd(action="val", value=str(timer_min)))])
        minutes: Row = [
            (tr("fmt.minutes", n=m), Fd(action="val", value=str(m))) for m in DURATION_PRESETS_MIN
        ]
        rows.append(minutes)
    elif is_effort(field):
        buttons = [
            (f"{tr(key)} · {n}", Fd(action="val", value=str(n))) for n, key in EFFORT_PRESETS
        ]
        rows += grid(buttons)
    elif is_stars(field):
        stars: Row = [("⭐" * n, Fd(action="val", value=str(n))) for n in range(1, 6)]
        rows += [stars[:3], stars[3:]]
    last: Row = [(tr("btn.skip"), Fd(action="skip"))] if not field.required else []
    if more:
        last.append((tr("btn.skip_rest"), Fd(action="skip_rest")))
    rows.append(last)
    rows.append(nav(tr, cancel=True))
    return inline(*rows)
