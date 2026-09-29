""" "История": weight, nutrition, sessions and per-activity totals (compatible metrics only)."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from fitcoach.bot.handlers.common import msg
from fitcoach.bot.ui import Hs, inline, num, session_line
from fitcoach.db.models import User
from fitcoach.domain.fields import FieldType
from fitcoach.domain.units import format_duration
from fitcoach.i18n import Translator, all_labels
from fitcoach.services.activities import ActivityService
from fitcoach.services.history import activity_stats, nutrition_history, weight_history

router = Router(name="history")


def history_kb(tr: Translator) -> object:
    return inline(
        [(tr("hist.weight"), Hs(a="weight")), (tr("hist.food"), Hs(a="food"))],
        [(tr("hist.sessions"), Hs(a="sessions")), (tr("hist.stats"), Hs(a="stats"))],
    )


@router.message(F.text.in_(all_labels("menu.history")))
async def history_menu(message: Message, tr: Translator, state: FSMContext) -> None:
    await state.clear()
    await message.answer(tr("hist.menu"), reply_markup=history_kb(tr))  # type: ignore[arg-type]


@router.callback_query(Hs.filter())
async def history_view(
    query: CallbackQuery, callback_data: Hs, session: AsyncSession, user: User, tr: Translator
) -> None:
    await query.answer()
    lines: list[str]
    if callback_data.a == "weight":
        rows = await weight_history(session, user, 30)
        lines = [tr("hist.weight_title")]
        lines += [
            f"{w.local_date.strftime('%d.%m')}: {num(tr, w.weight_kg, 1)} {tr('unit.kg')}"
            for w in rows[-20:]
        ]
        if len(rows) >= 2:
            delta = rows[-1].weight_kg - rows[0].weight_kg
            sign = "+" if delta > 0 else ""
            lines.append(
                tr(
                    "hist.weight_delta",
                    delta=sign + num(tr, delta, 1),
                    days=(rows[-1].local_date - rows[0].local_date).days,
                )
            )
        if not rows:
            lines.append(tr("hist.empty"))
    elif callback_data.a == "food":
        days = await nutrition_history(session, user, 7)
        lines = [tr("hist.food_title")]
        for d in days:
            t = d.totals
            kcal = num(tr, t.energy_kcal.value) if t.energy_kcal.known_entries else "—"
            line = (
                f"{d.day.strftime('%d.%m')}: {kcal} {tr('unit.kcal')} · "
                f"{tr('macro.P')}{num(tr, t.protein_g.value)} "
                f"{tr('macro.F')}{num(tr, t.fat_g.value)} "
                f"{tr('macro.C')}{num(tr, t.carbs_g.value)}"
            )
            if t.energy_kcal.unknown_entries:
                line += " " + tr("hist.partial", n=t.energy_kcal.unknown_entries)
            lines.append(line)
        if not days:
            lines.append(tr("hist.empty"))
        else:
            lines.append(tr("hist.missing_days_note"))
    elif callback_data.a == "sessions":
        rows_s = await ActivityService(session, user).list_sessions(limit=10)
        lines = [tr("hist.sessions_title")]
        lines += [f"{s.local_date.strftime('%d.%m')} · {session_line(tr, s)}" for s in rows_s]
        if not rows_s:
            lines.append(tr("hist.empty"))
    else:
        stats = await activity_stats(session, user, 30)
        lines = [tr("hist.stats_title")]
        for st in stats:
            head = f"• {st.name}: {tr('hist.sessions_n', n=st.sessions)}"
            if not st.counts_as_training:
                head += " " + tr("type.not_training")
            parts = []
            for m in st.metrics.values():
                if m.type is FieldType.DURATION:
                    parts.append(f"{m.label} {format_duration(int(m.value))}")
                else:
                    unit = f" {m.unit}" if m.unit else ""
                    parts.append(f"{m.label} {num(tr, m.value, 1)}{unit}")
            if st.volume_kg is not None:
                parts.append(tr("wo.volume", kg=num(tr, st.volume_kg)))
            if st.block_distance_m is not None:
                parts.append(tr("wo.distance_m", m=num(tr, st.block_distance_m)))
            lines.append(head + (" — " + ", ".join(parts) if parts else ""))
        if not stats:
            lines.append(tr("hist.empty"))
        else:
            lines.append(tr("hist.stats_note"))
    await msg(query).answer("\n".join(lines))
