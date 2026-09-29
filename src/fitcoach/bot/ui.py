"""Telegram presentation: callback data, keyboards and formatting. No business rules here."""

from __future__ import annotations

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
from aiogram.utils.keyboard import InlineKeyboardBuilder

from fitcoach.db.models import FoodEntry, WorkoutSession
from fitcoach.domain.fields import (
    DURATION_KEY,
    FieldDefinition,
    FieldSchema,
    FieldType,
    format_field_value,
)
from fitcoach.domain.food import NutrientSource
from fitcoach.domain.nutrition import NutrientTotal
from fitcoach.domain.units import format_duration
from fitcoach.domain.workout import SetWords, WorkoutBody, format_item, totals
from fitcoach.i18n import Translator
from fitcoach.services.food import DraftItem, FoodDraftState
from fitcoach.services.summary import DaySummary

# --- callback data --------------------------------------------------------------------


class Ob(CallbackData, prefix="ob"):
    """Onboarding (mode 'ob') and settings (mode 'set') choices."""

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


MENU_KEYS = (
    "menu.day",
    "menu.food",
    "menu.training",
    "menu.history",
    "menu.profile",
    "menu.settings",
)
COMMON_TIMEZONES = (
    "Europe/Kaliningrad",
    "Europe/Moscow",
    "Europe/Samara",
    "Asia/Yekaterinburg",
    "Asia/Novosibirsk",
    "Asia/Vladivostok",
    "Europe/Berlin",
    "UTC",
)
MEAL_ORDER = ("breakfast", "lunch", "dinner", "snack")


def main_menu(tr: Translator) -> ReplyKeyboardMarkup:
    labels = [tr(k) for k in MENU_KEYS]
    rows = [
        [KeyboardButton(text=a), KeyboardButton(text=b)]
        for a, b in zip(labels[::2], labels[1::2], strict=True)
    ]
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True, is_persistent=True)


def inline(*rows: Sequence[tuple[str, CallbackData]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=t, callback_data=cb.pack()) for t, cb in row] for row in rows
        ]
    )


def column(buttons: Sequence[tuple[str, CallbackData]], width: int = 1) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for label, cb in buttons:
        builder.button(text=label, callback_data=cb)
    builder.adjust(width)
    return builder.as_markup()


def cancel_kb(tr: Translator) -> InlineKeyboardMarkup:
    return inline([(tr("btn.cancel"), Fd(action="cancel"))])


# --- numbers ---------------------------------------------------------------------------


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


def _nutrient(tr: Translator, total: NutrientTotal, target: Decimal | None, unit: str) -> str:
    if not total.known_entries:
        return tr("day.unknown")
    text = num(tr, total.value)
    if target is not None:
        text += f" / {num(tr, target)}"
    return f"{text} {tr(unit)}"


def words(tr: Translator) -> SetWords:
    return SetWords(
        tr("unit.m"), tr("unit.sec"), tr("unit.min"), tr("word.rest"), tr("word.warmup_short")
    )


# --- diary lines ------------------------------------------------------------------------


def food_line(tr: Translator, entry: FoodEntry) -> str:
    kcal = (
        f"{num(tr, entry.energy_kcal)} {tr('unit.kcal')}"
        if entry.energy_kcal is not None
        else tr("food.kcal_unknown")
    )
    return f"{entry.name} — {kcal} · {tr('precision.' + entry.precision)}"


def session_duration(session: WorkoutSession) -> int | None:
    value = session.values.get(DURATION_KEY)
    return value if isinstance(value, int) else None


def session_line(tr: Translator, session: WorkoutSession) -> str:
    schema = FieldSchema.model_validate({"fields": session.field_snapshot})
    parts = []
    for field in schema.fields:
        value = session.values.get(field.key)
        if value is not None:
            shown = format_field_value(field, value, tr("word.yes"), tr("word.no"))
            parts.append(f"{field.label}: {shown}")
    if session.blocks:
        t = totals(WorkoutBody.load(session.blocks))
        if t.volume_kg is not None:
            parts.append(tr("wo.volume", kg=num(tr, t.volume_kg)))
        if t.distance_m is not None:
            parts.append(tr("wo.distance_m", m=num(tr, t.distance_m)))
        if t.working_sets:
            parts.append(tr("wo.sets", n=t.working_sets))
    title = session.template_name or session.activity_name
    if session.template_name and session.template_name != session.activity_name:
        title = f"{session.activity_name} — {session.template_name}"
    return title + (": " + ", ".join(parts) if parts else "")


def format_body(tr: Translator, body: WorkoutBody) -> list[str]:
    lines: list[str] = []
    w = words(tr)
    for block in body.blocks:
        header = block.title or tr("block." + block.kind.value)
        if block.rounds > 1:
            header += f" × {block.rounds}"
        if len(body.blocks) > 1 or block.kind.value != "main" or block.title:
            lines.append(header)
        lines.extend("  • " + format_item(item, w) for item in block.items)
    return lines


# --- day dashboard -----------------------------------------------------------------------


def format_day(
    tr: Translator,
    s: DaySummary,
    *,
    protein: Decimal | None = None,
    fat: Decimal | None = None,
    carbs: Decimal | None = None,
) -> str:
    lines = [tr("day.title", date=s.day.strftime("%d.%m"))]
    t = s.totals
    lines.append("")
    lines.append(tr("day.nutrition"))
    if t.entries:
        lines.append(_nutrient(tr, t.energy_kcal, s.kcal_target, "unit.kcal"))
        lines.append(tr("day.protein") + ": " + _nutrient(tr, t.protein_g, protein, "unit.g"))
        lines.append(tr("day.fat") + ": " + _nutrient(tr, t.fat_g, fat, "unit.g"))
        lines.append(tr("day.carbs") + ": " + _nutrient(tr, t.carbs_g, carbs, "unit.g"))
        if t.energy_kcal.unknown_entries:
            lines.append(tr("day.incomplete", n=t.energy_kcal.unknown_entries))
    else:
        lines.append(tr("day.no_food"))
    lines.append("")
    lines.append(tr("day.training"))
    if not s.sessions and not s.planned_open:
        lines.append(tr("day.no_training"))
    for session in s.sessions:
        duration = session_duration(session)
        extra = f" · {tr('fmt.minutes', n=duration // 60)}" if duration else ""
        title = session.template_name or session.activity_name
        if session.template_name and session.template_name != session.activity_name:
            title = f"{session.activity_name} — {session.template_name}"
        lines.append(f"✓ {title}{extra}")
    for p, v in s.planned_open:
        key = "day.planned_today" if p.planned_date == s.day else "day.planned_tomorrow"
        lines.append(tr(key, name=v.name))
    lines.append("")
    lines.append(tr("day.weight"))
    if s.weights_today:
        lines.append(f"{num(tr, s.weights_today[-1].weight_kg, 1)} {tr('unit.kg')}")
    elif s.latest_weight is not None:
        lines.append(
            tr(
                "day.weight_last",
                kg=num(tr, s.latest_weight.weight_kg, 1),
                date=s.latest_weight.local_date.strftime("%d.%m"),
            )
        )
    else:
        lines.append(tr("day.weight_none"))
    return "\n".join(lines)


# --- food draft preview --------------------------------------------------------------------


def _amount(tr: Translator, item: DraftItem) -> str:
    if item.amount is not None and item.unit is not None:
        text = f"{num(tr, item.amount, 2)} {tr('funit.' + item.unit.value)}"
        if item.grams is not None and item.unit.value not in ("g", "kg"):
            text += f" (≈{num(tr, item.grams)} {tr('unit.g')})"
        return text
    if item.grams is not None:
        return f"≈{num(tr, item.grams)} {tr('unit.g')}"
    return tr("draft.amount_missing")


def draft_item_line(tr: Translator, n: int, item: DraftItem) -> str:
    parts = [f"{n}. {item.name}" + (f" [{item.brand}]" if item.brand else ""), _amount(tr, item)]
    if item.energy_kcal is not None:
        macro = []
        for label, value in (("P", item.protein_g), ("F", item.fat_g), ("C", item.carbs_g)):
            if value is not None:
                macro.append(f"{tr('macro.' + label)}{num(tr, value)}")
        parts.append(
            f"{num(tr, item.energy_kcal)} {tr('unit.kcal')}"
            + (f" ({' '.join(macro)})" if macro else "")
        )
        parts.append(tr("precision." + item.precision.value))
    else:
        parts.append(tr("food.kcal_unknown"))
    if item.uncertain:
        parts.append("❓")
    return " · ".join(parts)


def format_food_draft(tr: Translator, state: FoodDraftState) -> str:
    lines = [tr("draft.title"), tr("draft.meal", meal=tr("meal." + state.meal_type.value))]
    if state.transcript:
        lines.append(tr("draft.transcript", text=state.transcript[:300]))
    lines.append("")
    for n, item in enumerate(state.items, start=1):
        lines.append(draft_item_line(tr, n, item))
    if not state.items:
        lines.append(tr("draft.empty"))
    t = state.totals()
    unknown = sum(1 for i in state.items if i.energy_kcal is None)
    if t.energy_kcal is not None:
        lines.append("")
        total = tr(
            "draft.total",
            kcal=num(tr, t.energy_kcal),
            p=num(tr, t.protein_g),
            f=num(tr, t.fat_g),
            c=num(tr, t.carbs_g),
        )
        if unknown:
            total += " " + tr("draft.total_unknown", n=unknown)
        lines.append(total)
    sources = {i.nutrient_source for i in state.items}
    notes = []
    if NutrientSource.AI_ESTIMATE in sources:
        notes.append(tr("draft.note_ai_estimate"))
    if NutrientSource.USDA in sources:
        notes.append(tr("draft.note_usda"))
    if NutrientSource.OFF in sources:
        notes.append(tr("draft.note_off"))
    if any(i.quantity_estimated for i in state.items):
        notes.append(tr("draft.note_estimated_amount"))
    if state.clarification:
        notes.append("❓ " + state.clarification)
    if state.mock:
        notes.append(tr("draft.mock"))
    if notes:
        lines.append("")
        lines.extend(notes)
    lines.append("")
    lines.append(tr("draft.hint"))
    return "\n".join(lines)


def food_draft_kb(
    tr: Translator, draft_id: int, version: int, state: FoodDraftState
) -> InlineKeyboardMarkup:
    rows: list[list[tuple[str, CallbackData]]] = []
    for i, item in enumerate(state.items):
        label = item.name if len(item.name) <= 18 else item.name[:17] + "…"
        rows.append(
            [
                (f"✏️ {i + 1}. {label}", Fr(a="edit", d=draft_id, v=version, i=i)),
                ("❌", Fr(a="del", d=draft_id, v=version, i=i)),
            ]
        )
    rows.append(
        [
            (tr("draft.add"), Fr(a="add", d=draft_id, v=version)),
            ("🍽 " + tr("meal." + state.meal_type.value), Fr(a="meal", d=draft_id, v=version)),
        ]
    )
    if state.items:
        rows.append(
            [
                (tr("draft.confirm"), Fr(a="ok", d=draft_id, v=version)),
                (tr("draft.cancel"), Fr(a="no", d=draft_id, v=version)),
            ]
        )
        rows.append([(tr("draft.save_meal"), Fr(a="fav", d=draft_id, v=version))])
    else:
        rows.append([(tr("draft.cancel"), Fr(a="no", d=draft_id, v=version))])
    return inline(*rows)


# --- field entry (workout recorder) --------------------------------------------------------


def field_prompt(tr: Translator, field: FieldDefinition, target: Any | None) -> str:
    hints = {
        FieldType.DECIMAL: "hint.decimal",
        FieldType.INTEGER: "hint.integer",
        FieldType.BOOLEAN: "hint.boolean",
        FieldType.TEXT: "hint.text",
        FieldType.SELECTION: "hint.selection",
    }
    if field.type is FieldType.DURATION:
        hint = tr("hint.duration_hmm" if field.duration_format == "h:mm" else "hint.duration_mmss")
    else:
        hint = tr(hints[field.type])
    label = f"{field.label} ({field.unit})" if field.unit else field.label
    text = f"{label}\n{hint}"
    if target is not None:
        shown = format_field_value(field, target, tr("word.yes"), tr("word.no"))
        text += "\n" + tr("hint.target", value=shown)
    return text


def field_input_kb(tr: Translator, field: FieldDefinition) -> InlineKeyboardMarkup:
    rows: list[list[tuple[str, CallbackData]]] = []
    if field.type is FieldType.SELECTION and field.choices:
        rows.extend([(c, Fd(action="choice", value=str(i)))] for i, c in enumerate(field.choices))
    if field.type is FieldType.BOOLEAN:
        rows.append(
            [
                (tr("word.yes"), Fd(action="bool", value="1")),
                (tr("word.no"), Fd(action="bool", value="0")),
            ]
        )
    buttons = [(tr("btn.skip"), Fd(action="skip"))] if not field.required else []
    rows.append([*buttons, (tr("btn.cancel"), Fd(action="cancel"))])
    return inline(*rows)


def format_duration_min(tr: Translator, seconds: int | None) -> str:
    if seconds is None:
        return "—"
    return format_duration(seconds)
