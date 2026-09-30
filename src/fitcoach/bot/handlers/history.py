""" "📈 История": weight, nutrition, workouts and per-activity totals (compatible metrics only)."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from fitcoach.bot.handlers.common import Event
from fitcoach.bot.screen import answer, render
from fitcoach.bot.ui import Go, Hs, inline, minutes_text, nav, num, session_duration, session_title
from fitcoach.db.models import User
from fitcoach.domain.fields import FieldType
from fitcoach.i18n import Translator, all_labels
from fitcoach.services.activities import ActivityService
from fitcoach.services.history import activity_stats, nutrition_history, weight_history

router = Router(name="history")
SECTIONS = ("weight", "food", "sessions", "stats")


async def show_history(event: Event, tr: Translator, state: FSMContext) -> None:
    await state.clear()
    await answer(event)
    await render(
        event,
        state,
        tr("hist.menu"),
        inline(
            [
                (tr("hist.weight"), Go(s="hist", a="weight")),
                (tr("hist.food"), Go(s="hist", a="food")),
            ],
            [
                (tr("hist.sessions"), Go(s="hist", a="sessions")),
                (tr("hist.stats"), Go(s="hist", a="stats")),
            ],
            nav(tr, Go(s="more")),
        ),
    )


@router.message(F.text.in_(all_labels("menu.old_history")))
async def history_menu(message: Message, tr: Translator, state: FSMContext) -> None:
    await show_history(message, tr, state)


async def _section(session: AsyncSession, user: User, tr: Translator, section: str) -> list[str]:
    lines: list[str]
    if section == "weight":
        rows = await weight_history(session, user, 30)
        lines = [tr("hist.weight_title"), ""]
        lines += [
            f"{w.local_date.strftime('%d.%m')} · {num(tr, w.weight_kg, 1)} {tr('unit.kg')}"
            for w in rows[-20:]
        ]
        if len(rows) >= 2:
            delta = rows[-1].weight_kg - rows[0].weight_kg
            sign = "+" if delta > 0 else ""
            lines += [
                "",
                tr(
                    "hist.weight_delta",
                    delta=sign + num(tr, delta, 1),
                    days=(rows[-1].local_date - rows[0].local_date).days,
                ),
            ]
        if not rows:
            lines.append(tr("hist.empty"))
    elif section == "food":
        days = await nutrition_history(session, user, 7)
        lines = [tr("hist.food_title"), ""]
        for d in days:
            t = d.totals
            kcal = num(tr, t.energy_kcal.value) if t.energy_kcal.known_entries else "—"
            line = (
                f"{d.day.strftime('%d.%m')} · {kcal} {tr('unit.kcal')} · "
                f"{tr('macro.P')} {num(tr, t.protein_g.value)} "
                f"{tr('macro.F')} {num(tr, t.fat_g.value)} "
                f"{tr('macro.C')} {num(tr, t.carbs_g.value)}"
            )
            if t.energy_kcal.unknown_entries:
                line += " " + tr("hist.partial", n=t.energy_kcal.unknown_entries)
            lines.append(line)
        lines += ["", tr("hist.missing_days_note")] if days else [tr("hist.empty")]
    elif section == "sessions":
        items = await ActivityService(session, user).list_sessions(limit=10)
        lines = [tr("hist.sessions_title"), ""]
        for s in items:
            duration = session_duration(s)
            extra = f" · {minutes_text(tr, duration)}" if duration else ""
            lines.append(f"{s.local_date.strftime('%d.%m')} · {session_title(s)}{extra}")
        if not items:
            lines.append(tr("hist.empty"))
    else:
        stats = await activity_stats(session, user, 30)
        lines = [tr("hist.stats_title"), ""]
        for st in stats:
            head = f"• {st.name}: {tr('hist.sessions_n', n=st.sessions)}"
            if not st.counts_as_training:
                head += " " + tr("type.not_training")
            parts = []
            for m in st.metrics.values():
                if m.type is FieldType.DURATION:
                    parts.append(f"{m.label} {minutes_text(tr, int(m.value))}")
                else:
                    unit = f" {m.unit}" if m.unit else ""
                    parts.append(f"{m.label} {num(tr, m.value, 1)}{unit}")
            if st.volume_kg is not None:
                parts.append(tr("wo.volume", kg=num(tr, st.volume_kg)))
            if st.block_distance_m is not None:
                parts.append(tr("wo.distance_m", m=num(tr, st.block_distance_m)))
            lines.append(head + (" — " + ", ".join(parts) if parts else ""))
        lines += ["", tr("hist.stats_note")] if stats else [tr("hist.empty")]
    return lines


@router.callback_query(Go.filter(F.s == "hist"))
@router.callback_query(Hs.filter())
async def history_view(
    query: CallbackQuery,
    callback_data: Go | Hs,
    session: AsyncSession,
    user: User,
    tr: Translator,
    state: FSMContext,
) -> None:
    section = callback_data.a
    if section not in SECTIONS:
        await show_history(query, tr, state)
        return
    await state.clear()
    await answer(query)
    lines = await _section(session, user, tr, section)
    await render(query, state, "\n".join(lines), inline(nav(tr, Go(s="hist"))))
