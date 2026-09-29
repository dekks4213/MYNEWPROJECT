""" "Настройки": language, time zone, AI consents, reminders, quiet hours, export, deletion."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BufferedInputFile, CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from fitcoach.bot.handlers.common import msg
from fitcoach.bot.handlers.onboarding import SettingsSG
from fitcoach.bot.ui import Fd, Ob, Rm, St, cancel_kb, column, inline, main_menu
from fitcoach.db.models import User
from fitcoach.domain.schedule import ALL_DAYS, WEEKDAYS
from fitcoach.i18n import Translator, all_labels
from fitcoach.services.account import delete_account, export_json
from fitcoach.services.reminders import KINDS, ReminderService
from fitcoach.services.users import UserService

router = Router(name="settings")


class ReminderSG(StatesGroup):
    time = State()
    text = State()
    quiet = State()


class DeleteSG(StatesGroup):
    confirm = State()


def _yes_no(tr: Translator, value: object) -> str:
    return tr("word.yes") if value else tr("word.no")


@router.message(Command("settings"))
@router.message(F.text.in_(all_labels("menu.settings")))
async def settings_menu(message: Message, user: User, tr: Translator, state: FSMContext) -> None:
    await state.clear()
    quiet = (
        f"{user.quiet_start:%H:%M}–{user.quiet_end:%H:%M}"
        if user.quiet_start and user.quiet_end
        else "—"
    )
    text = tr(
        "settings.view",
        language=user.language,
        tz=user.timezone or "—",
        ai=_yes_no(tr, user.ai_text_consent_at),
        media=_yes_no(tr, user.ai_media_consent_at),
        quiet=quiet,
    )
    media_toggle = "off" if user.ai_media_consent_at else "on"
    await message.answer(
        text,
        reply_markup=column(
            [
                (tr("settings.language"), Ob(mode="set", action="open", value="language")),
                (tr("settings.timezone"), Ob(mode="set", action="open", value="timezone")),
                (tr("settings.privacy"), Ob(mode="set", action="open", value="privacy")),
                (tr("settings.media_" + media_toggle), St(a="media", x=media_toggle)),
                (tr("settings.reminders"), St(a="reminders")),
                (tr("settings.quiet"), St(a="quiet")),
                (tr("settings.export"), St(a="export")),
                (tr("settings.delete"), St(a="delete")),
            ],
            width=2,
        ),
    )


@router.message(SettingsSG.timezone, F.text)
async def settings_timezone(
    message: Message, session: AsyncSession, user: User, tr: Translator, state: FSMContext
) -> None:
    assert message.text is not None
    await UserService(session, user).set_timezone(message.text)
    await ReminderService(session, user).reschedule_all()
    await session.commit()
    await state.clear()
    await message.answer(tr("settings.saved"), reply_markup=main_menu(tr))


@router.message(SettingsSG.target, F.text)
async def settings_target(
    message: Message, session: AsyncSession, user: User, tr: Translator, state: FSMContext
) -> None:
    assert message.text is not None
    await UserService(session, user).set_kcal_target(message.text)
    await session.commit()
    await state.clear()
    await message.answer(tr("settings.saved"), reply_markup=main_menu(tr))


@router.callback_query(St.filter(F.a == "media"))
async def media_toggle(
    query: CallbackQuery, callback_data: St, session: AsyncSession, user: User, tr: Translator
) -> None:
    await UserService(session, user).set_media_consent(callback_data.x == "on")
    await session.commit()
    await query.answer(tr("settings.saved"))
    await msg(query).answer(
        tr("settings.media_on_done" if callback_data.x == "on" else "settings.media_off_done")
    )


# --- reminders ---------------------------------------------------------------------------------


@router.callback_query(St.filter(F.a == "reminders"))
async def reminders(
    query: CallbackQuery, session: AsyncSession, user: User, tr: Translator
) -> None:
    await query.answer()
    items = await ReminderService(session, user).list()
    lines = [tr("rem.title")]
    buttons = []
    for r in items:
        label = r.text if r.kind == "custom" and r.text else tr("rem.kind." + r.kind)
        days = (
            tr("rem.daily")
            if r.days_mask == ALL_DAYS
            else (tr("rem.weekdays") if r.days_mask == WEEKDAYS else tr("rem.custom_days"))
        )
        state_icon = "🔔" if r.enabled else "🔕"
        lines.append(f"{state_icon} {r.local_time:%H:%M} · {label} · {days}")
        toggle = "off" if r.enabled else "on"
        buttons.append(
            (
                f"{state_icon} {r.local_time:%H:%M} {label[:20]}",
                St(a="rem_toggle", id=r.id, x=toggle),
            )
        )
        buttons.append((f"🗑 {r.local_time:%H:%M}", St(a="rem_del", id=r.id)))
    if not items:
        lines.append(tr("rem.none"))
    buttons.append((tr("rem.add"), St(a="rem_add")))
    await msg(query).answer("\n".join(lines), reply_markup=column(buttons, width=2))


@router.callback_query(St.filter(F.a == "rem_add"))
async def reminder_add(query: CallbackQuery, tr: Translator) -> None:
    await query.answer()
    buttons = [(tr("rem.kind." + k), St(a="rem_kind", x=k)) for k in KINDS]
    await msg(query).answer(tr("rem.kind_ask"), reply_markup=column(buttons, width=2))


@router.callback_query(St.filter(F.a == "rem_kind"))
async def reminder_kind(
    query: CallbackQuery, callback_data: St, tr: Translator, state: FSMContext
) -> None:
    if callback_data.x not in KINDS:
        await query.answer(tr("stale_button"))
        return
    await query.answer()
    await state.update_data(rem_kind=callback_data.x)
    if callback_data.x == "custom":
        await state.set_state(ReminderSG.text)
        await msg(query).answer(tr("rem.text_ask"), reply_markup=cancel_kb(tr))
        return
    await state.set_state(ReminderSG.time)
    await msg(query).answer(tr("rem.time_ask"), reply_markup=cancel_kb(tr))


@router.message(ReminderSG.text, F.text)
async def reminder_text(message: Message, tr: Translator, state: FSMContext) -> None:
    assert message.text is not None
    await state.update_data(rem_text=message.text[:200])
    await state.set_state(ReminderSG.time)
    await message.answer(tr("rem.time_ask"), reply_markup=cancel_kb(tr))


@router.message(ReminderSG.time, F.text)
async def reminder_time(message: Message, tr: Translator, state: FSMContext) -> None:
    assert message.text is not None
    from fitcoach.domain.schedule import parse_hhmm
    from fitcoach.services.errors import ServiceError

    try:
        parse_hhmm(message.text)
    except ValueError as exc:
        raise ServiceError("bad_time") from exc
    await state.update_data(rem_time=message.text.strip())
    await message.answer(
        tr("rem.days_ask"),
        reply_markup=inline(
            [
                (tr("rem.daily"), St(a="rem_days", x=str(ALL_DAYS))),
                (tr("rem.weekdays"), St(a="rem_days", x=str(WEEKDAYS))),
            ],
            [(tr("btn.cancel"), Fd(action="cancel"))],
        ),
    )


@router.callback_query(St.filter(F.a == "rem_days"))
async def reminder_days(
    query: CallbackQuery,
    callback_data: St,
    session: AsyncSession,
    user: User,
    tr: Translator,
    state: FSMContext,
) -> None:
    data = await state.get_data()
    if "rem_kind" not in data or "rem_time" not in data or not callback_data.x.isdigit():
        await query.answer(tr("stale_button"))
        return
    reminder = await ReminderService(session, user).create(
        data["rem_kind"], data["rem_time"], int(callback_data.x), data.get("rem_text")
    )
    await session.commit()
    await state.clear()
    await query.answer(tr("saved"))
    await msg(query).answer(
        tr("rem.saved", time=f"{reminder.local_time:%H:%M}"), reply_markup=main_menu(tr)
    )


@router.callback_query(St.filter(F.a.in_({"rem_toggle", "rem_del"})))
async def reminder_change(
    query: CallbackQuery, callback_data: St, session: AsyncSession, user: User, tr: Translator
) -> None:
    svc = ReminderService(session, user)
    if callback_data.a == "rem_del":
        await svc.delete(callback_data.id)
    else:
        await svc.set_enabled(callback_data.id, callback_data.x == "on")
    await session.commit()
    await query.answer(tr("settings.saved"))


@router.callback_query(Rm.filter())
async def reminder_message_action(
    query: CallbackQuery, callback_data: Rm, session: AsyncSession, user: User, tr: Translator
) -> None:
    """Buttons under a delivered reminder: snooze or switch it off."""
    svc = ReminderService(session, user)
    if callback_data.a == "snooze":
        await svc.snooze(callback_data.id, 30)
        await session.commit()
        await query.answer(tr("rem.snoozed"))
    else:
        await svc.set_enabled(callback_data.id, False)
        await session.commit()
        await query.answer(tr("rem.disabled"))


@router.callback_query(St.filter(F.a == "quiet"))
async def quiet_start(query: CallbackQuery, tr: Translator, state: FSMContext) -> None:
    await query.answer()
    await state.set_state(ReminderSG.quiet)
    await msg(query).answer(
        tr("rem.quiet_ask"),
        reply_markup=inline(
            [(tr("rem.quiet_off"), St(a="quiet_off"))], [(tr("btn.cancel"), Fd(action="cancel"))]
        ),
    )


@router.message(ReminderSG.quiet, F.text)
async def quiet_value(
    message: Message, session: AsyncSession, user: User, tr: Translator, state: FSMContext
) -> None:
    assert message.text is not None
    start, _, end = message.text.replace("—", "-").replace("–", "-").partition("-")
    from fitcoach.services.errors import ServiceError

    if not end.strip():
        raise ServiceError("bad_time")
    await ReminderService(session, user).set_quiet_hours(start.strip(), end.strip())
    await session.commit()
    await state.clear()
    await message.answer(tr("settings.saved"), reply_markup=main_menu(tr))


@router.callback_query(St.filter(F.a == "quiet_off"))
async def quiet_off(
    query: CallbackQuery, session: AsyncSession, user: User, tr: Translator, state: FSMContext
) -> None:
    await ReminderService(session, user).set_quiet_hours(None, None)
    await session.commit()
    await state.clear()
    await query.answer(tr("settings.saved"))


# --- export and deletion -------------------------------------------------------------------------


@router.callback_query(St.filter(F.a == "export"))
async def export(query: CallbackQuery, session: AsyncSession, user: User, tr: Translator) -> None:
    await query.answer()
    data = await export_json(session, user)
    await msg(query).answer_document(
        BufferedInputFile(data, filename="ritm-export.json"), caption=tr("settings.export_done")
    )


@router.callback_query(St.filter(F.a == "delete"))
async def delete_start(query: CallbackQuery, tr: Translator, state: FSMContext) -> None:
    await query.answer()
    await state.set_state(DeleteSG.confirm)
    await msg(query).answer(tr("settings.delete_warn"), reply_markup=cancel_kb(tr))


@router.message(DeleteSG.confirm, F.text)
async def delete_confirm(
    message: Message, session: AsyncSession, user: User, tr: Translator, state: FSMContext
) -> None:
    assert message.text is not None
    await delete_account(session, user, message.text)
    await session.commit()
    await state.clear()
    await message.answer(tr("settings.deleted"))
