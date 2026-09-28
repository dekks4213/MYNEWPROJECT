"""Telegram presentation: callback data, keyboards and formatting. No business rules here."""

from __future__ import annotations

from collections.abc import Sequence

from aiogram.filters.callback_data import CallbackData
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

from fitcoach.db.models import FoodEntry, WorkoutSession
from fitcoach.domain.fields import FieldDefinition, FieldSchema, FieldType, format_field_value
from fitcoach.domain.nutrition import NutrientTotal
from fitcoach.domain.units import format_decimal
from fitcoach.i18n import Translator
from fitcoach.services.summary import DaySummary


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


class Fo(CallbackData, prefix="fo"):
    action: str
    value: str = ""


class Dr(CallbackData, prefix="dr"):
    action: str  # ok | no
    id: int


MENU_KEYS = ("menu.food", "menu.weight", "menu.training", "menu.today", "menu.fix", "menu.settings")
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


def _nutrient(tr: Translator, total: NutrientTotal, unit_key: str) -> str:
    text = f"{format_decimal(total.value)} {tr(unit_key)}"
    if total.unknown_entries:
        text += " " + tr("summary.unknown_part", n=total.unknown_entries)
    return text


def food_line(tr: Translator, entry: FoodEntry) -> str:
    kcal = (
        f"{format_decimal(entry.energy_kcal)} {tr('unit.kcal')}"
        if entry.energy_kcal is not None
        else tr("summary.kcal_unknown")
    )
    return f"{entry.name} — {kcal} ({tr('precision.' + entry.precision)})"


def session_line(tr: Translator, session: WorkoutSession) -> str:
    schema = FieldSchema.model_validate({"fields": session.field_snapshot})
    parts = []
    for field in schema.fields:
        value = session.values.get(field.key)
        if value is not None:
            shown = format_field_value(field, value, tr("word.yes"), tr("word.no"))
            parts.append(f"{field.label}: {shown}")
    title = session.template_name or session.activity_name
    return f"{title}: " + ", ".join(parts)


def format_summary(tr: Translator, s: DaySummary) -> str:
    lines = [tr("summary.title", date=s.day.strftime("%d.%m.%Y"))]
    t = s.totals
    if t.entries:
        lines.append(tr("summary.food", n=t.entries))
        lines.append("  " + tr("summary.energy") + ": " + _nutrient(tr, t.energy_kcal, "unit.kcal"))
        macros = " / ".join(_nutrient(tr, x, "unit.g") for x in (t.protein_g, t.fat_g, t.carbs_g))
        lines.append("  " + tr("summary.macros") + ": " + macros)
        if s.kcal_target is not None and t.energy_kcal.known_entries:
            lines.append(
                "  "
                + tr(
                    "summary.target",
                    target=format_decimal(s.kcal_target),
                    left=format_decimal(s.kcal_target - t.energy_kcal.value),
                )
            )
            if t.energy_kcal.unknown_entries:
                lines.append("  " + tr("summary.target_incomplete"))
    else:
        lines.append(tr("summary.no_food"))
    if s.weights_today:
        lines.append(
            tr("summary.weight_today", kg=format_decimal(s.weights_today[-1].weight_kg, 2))
        )
    elif s.latest_weight is not None:
        lines.append(
            tr(
                "summary.weight_last",
                kg=format_decimal(s.latest_weight.weight_kg, 2),
                date=s.latest_weight.local_date.strftime("%d.%m"),
            )
        )
    if s.sessions:
        lines.append(tr("summary.sessions"))
        lines.extend("  • " + session_line(tr, x) for x in s.sessions)
    else:
        lines.append(tr("summary.no_sessions"))
    if s.planned_open:
        p, v = s.planned_open[0]
        key = "summary.next_today" if p.planned_date == s.day else "summary.next_tomorrow"
        lines.append(tr(key, name=v.name))
    return "\n".join(lines)


def field_prompt(tr: Translator, field: FieldDefinition, target: object | None) -> str:
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
        shown = format_field_value(field, target, tr("word.yes"), tr("word.no"))  # type: ignore[arg-type]
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
