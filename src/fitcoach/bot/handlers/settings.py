"""⚙️ Settings: language and region, AI features, reminders and quiet hours, data.
Profile and targets live in handlers.profile."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BufferedInputFile, CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from fitcoach.ai.gateway import AIGateway
from fitcoach.bot.handlers.common import Event
from fitcoach.bot.handlers.onboarding import tz_label, tz_rows
from fitcoach.bot.handlers.profile import goal_kb, show_profile
from fitcoach.bot.screen import answer, render
from fitcoach.bot.ui import (
    QUIET_PRESETS,
    REMINDER_PRESETS,
    REMINDER_TIMES,
    Ac,
    Go,
    Ob,
    Rm,
    Row,
    St,
    grid,
    hhmm_pack,
    hhmm_unpack,
    inline,
    main_menu,
    nav,
)
from fitcoach.db.models import Reminder, User
from fitcoach.domain.schedule import ALL_DAYS, WEEKDAYS, parse_hhmm
from fitcoach.i18n import Translator, all_labels
from fitcoach.services.account import delete_account, export_json
from fitcoach.services.errors import ServiceError
from fitcoach.services.reminders import KINDS, ReminderService
from fitcoach.services.users import UserService

router = Router(name="settings")
LANGUAGE_NAMES = {"ru": "Русский", "en": "English"}


class SettingsSG(StatesGroup):
    timezone = State()


class ReminderSG(StatesGroup):
    time = State()
    text = State()
    quiet = State()


class DeleteSG(StatesGroup):
    confirm = State()


# --- root --------------------------------------------------------------------------------------


async def show_settings(event: Event, tr: Translator, state: FSMContext) -> None:
    await state.clear()
    await answer(event)
    section = [
        ("profile", "settings.profile"),
        ("goals", "settings.goals"),
        ("rem", "settings.reminders"),
        ("region", "settings.region"),
        ("ai", "settings.ai"),
        ("data", "settings.data"),
    ]
    buttons: Row = [(tr(key), Go(s="set", a=code)) for code, key in section]
    await render(
        event,
        state,
        tr("settings.title"),
        inline(*grid(buttons), [(tr("more.help"), Go(s="help"))], nav(tr, Go(s="more"))),
    )


@router.message(Command("settings"))
@router.message(F.text.in_(all_labels("menu.old_settings") | all_labels("menu.old_profile")))
async def settings_cmd(message: Message, tr: Translator, state: FSMContext) -> None:
    await show_settings(message, tr, state)


@router.callback_query(Go.filter((F.s == "set") & (F.a == "")))
async def settings_cb(query: CallbackQuery, tr: Translator, state: FSMContext) -> None:
    await show_settings(query, tr, state)


# --- language and region -----------------------------------------------------------------------


async def show_region(event: Event, user: User, tr: Translator, state: FSMContext) -> None:
    await state.clear()
    await answer(event)
    other = "en" if user.language == "ru" else "ru"
    await render(
        event,
        state,
        tr(
            "settings.region_view",
            language=LANGUAGE_NAMES.get(user.language, user.language),
            tz=tz_label(tr, user.timezone),
        ),
        inline(
            [(LANGUAGE_NAMES[other], Ob(mode="set", action="lang", value=other))],
            [(tr("settings.change_tz"), Ob(mode="set", action="open", value="timezone"))],
            nav(tr, Go(s="set")),
        ),
    )


@router.callback_query(Go.filter((F.s == "set") & (F.a == "region")))
async def region_cb(query: CallbackQuery, user: User, tr: Translator, state: FSMContext) -> None:
    await show_region(query, user, tr, state)


@router.callback_query(Ob.filter(F.mode == "set"))
async def settings_choice(
    query: CallbackQuery,
    callback_data: Ob,
    session: AsyncSession,
    user: User,
    tr: Translator,
    state: FSMContext,
) -> None:
    svc = UserService(session, user)
    action, value = callback_data.action, callback_data.value
    back_region = Go(s="set", a="region")
    if action == "open" and value == "goal":
        await answer(query)
        await render(
            query, state, tr("ob.goal"), inline(*goal_kb(tr, "set", Go(s="set", a="profile")))
        )
        return
    if action in ("open", "tzall"):
        await answer(query)
        await state.set_state(SettingsSG.timezone)
        rows = tz_rows(tr, "set", full=action == "tzall")
        await render(
            query, state, tr("ob.timezone"), inline(*rows, nav(tr, back_region, home=False))
        )
        return
    if action == "lang":
        await svc.set_language(value)
    elif action == "tz":
        await svc.set_timezone(value)
        await ReminderService(session, user).reschedule_all()
    elif action == "goal":
        await svc.set_goal(value or None)
    else:
        raise ServiceError("bad_choice")
    await session.commit()  # confirm only after the database accepted the change
    tr = Translator(user.language)
    await answer(query, tr("settings.saved"))
    if action == "goal":
        await show_profile(query, session, user, tr, state)
        return
    if action == "lang":
        # The quick-access keyboard carries labels: resend it in the new language.
        assert isinstance(query.message, Message)
        await query.message.answer(tr("settings.language_done"), reply_markup=main_menu(tr))
        await render(query, state, tr("settings.saved"), inline(nav(tr, back_region)), fresh=True)
        return
    await show_region(query, user, tr, state)


@router.message(SettingsSG.timezone, F.text)
async def settings_timezone(
    message: Message, session: AsyncSession, user: User, tr: Translator, state: FSMContext
) -> None:
    assert message.text is not None
    await UserService(session, user).set_timezone(message.text)
    await ReminderService(session, user).reschedule_all()
    await session.commit()
    await show_region(message, user, tr, state)


# --- AI features -------------------------------------------------------------------------------


async def show_ai(
    event: Event, user: User, tr: Translator, gateway: AIGateway, state: FSMContext
) -> None:
    await state.clear()
    await answer(event)
    if not gateway.enabled:
        await render(event, state, tr("settings.ai_off_server"), inline(nav(tr, Go(s="set"))))
        return
    on, off = tr("settings.on"), tr("settings.off")
    text_on = user.ai_text_consent_at is not None
    media_on = user.ai_media_consent_at is not None
    lines = [
        tr("settings.ai_title"),
        "",
        tr("settings.ai_text", state=on if text_on else off),
        tr("settings.ai_media", state=on if media_on else off),
        "",
        tr("settings.ai_privacy"),
    ]
    if gateway.is_mock:
        lines += ["", tr("draft.mock")]
    await render(
        event,
        state,
        "\n".join(lines),
        inline(
            [
                (
                    tr("settings.ai_text_btn_" + ("off" if text_on else "on")),
                    St(a="ai_text", x="off" if text_on else "on"),
                )
            ],
            [
                (
                    tr("settings.media_" + ("off" if media_on else "on")),
                    St(a="media", x="off" if media_on else "on"),
                )
            ],
            nav(tr, Go(s="set")),
        ),
    )


@router.callback_query(Go.filter((F.s == "set") & (F.a == "ai")))
async def ai_cb(
    query: CallbackQuery, user: User, tr: Translator, gateway: AIGateway, state: FSMContext
) -> None:
    await show_ai(query, user, tr, gateway, state)


@router.callback_query(St.filter(F.a.in_({"ai_text", "media"})))
async def ai_toggle(
    query: CallbackQuery,
    callback_data: St,
    session: AsyncSession,
    user: User,
    tr: Translator,
    gateway: AIGateway,
    state: FSMContext,
) -> None:
    svc = UserService(session, user)
    allow = callback_data.x == "on"
    if callback_data.a == "ai_text":
        await svc.set_ai_consent(allow)
        if not allow:
            await svc.set_media_consent(False)  # photos and voice need text parsing too
    else:
        if allow:
            await svc.set_ai_consent(True)
        await svc.set_media_consent(allow)
    await session.commit()
    await answer(query, tr("settings.saved"))
    await show_ai(query, user, tr, gateway, state)


# --- reminders ---------------------------------------------------------------------------------


def _days(tr: Translator, mask: int) -> str:
    if mask == ALL_DAYS:
        return tr("rem.daily_l")
    return tr("rem.weekdays_l") if mask == WEEKDAYS else tr("rem.custom_days")


def _label(tr: Translator, r: Reminder) -> str:
    return r.text if r.kind == "custom" and r.text else tr("rem.kind." + r.kind)


async def show_reminders(
    event: Event, session: AsyncSession, user: User, tr: Translator, state: FSMContext
) -> None:
    await state.clear()
    await answer(event)
    items = await ReminderService(session, user).list()
    lines = [tr("rem.title"), ""]
    rows: list[Row] = []
    for r in items:
        icon = "🔔" if r.enabled else "🔕"
        lines.append(f"{icon} {r.local_time:%H:%M} · {_label(tr, r)} · {_days(tr, r.days_mask)}")
        rows.append(
            [(f"{icon} {r.local_time:%H:%M} {_label(tr, r)[:24]}", St(a="rem_one", id=r.id))]
        )
    if not items:
        lines.append(tr("rem.none"))
    quiet = (
        f"{user.quiet_start:%H:%M}–{user.quiet_end:%H:%M}"
        if user.quiet_start and user.quiet_end
        else tr("rem.quiet_none")
    )
    lines += ["", tr("rem.quiet_line", value=quiet)]
    rows.append([(tr("rem.add"), St(a="rem_add")), (tr("rem.quiet"), St(a="quiet"))])
    rows.append(nav(tr, Go(s="set")))
    await render(event, state, "\n".join(lines), inline(*rows))


@router.callback_query(Go.filter((F.s == "set") & (F.a == "rem")))
async def reminders(
    query: CallbackQuery, session: AsyncSession, user: User, tr: Translator, state: FSMContext
) -> None:
    await show_reminders(query, session, user, tr, state)


@router.callback_query(St.filter(F.a == "rem_one"))
async def reminder_one(
    query: CallbackQuery,
    callback_data: St,
    session: AsyncSession,
    user: User,
    tr: Translator,
    state: FSMContext,
) -> None:
    r = await ReminderService(session, user).get(callback_data.id)
    await answer(query)
    toggle = "off" if r.enabled else "on"
    await render(
        query,
        state,
        f"{'🔔' if r.enabled else '🔕'} {r.local_time:%H:%M} · {_label(tr, r)}\n"
        + _days(tr, r.days_mask),
        inline(
            [
                (tr("rem.turn_" + toggle), St(a="rem_toggle", id=r.id, x=toggle)),
                (tr("rem.delete"), St(a="rem_del", id=r.id)),
            ],
            nav(tr, Go(s="set", a="rem"), home=False),
        ),
    )


@router.callback_query(St.filter(F.a.in_({"rem_toggle", "rem_del"})))
async def reminder_change(
    query: CallbackQuery,
    callback_data: St,
    session: AsyncSession,
    user: User,
    tr: Translator,
    state: FSMContext,
) -> None:
    svc = ReminderService(session, user)
    if callback_data.a == "rem_del":
        await svc.delete(callback_data.id)
    else:
        await svc.set_enabled(callback_data.id, callback_data.x == "on")
    await session.commit()
    await answer(query, tr("settings.saved"))
    await show_reminders(query, session, user, tr, state)


@router.callback_query(St.filter(F.a == "rem_add"))
async def reminder_add(query: CallbackQuery, tr: Translator, state: FSMContext) -> None:
    await answer(query)
    await state.clear()
    buttons = [(tr("rem.kind." + k), St(a="rem_kind", x=k)) for k in KINDS]
    await render(
        query,
        state,
        tr("rem.kind_ask"),
        inline(*grid(buttons), nav(tr, Go(s="set", a="rem"), home=False)),
    )


@router.callback_query(St.filter(F.a == "rem_kind"))
async def reminder_kind(
    query: CallbackQuery, callback_data: St, tr: Translator, state: FSMContext
) -> None:
    if callback_data.x not in KINDS:
        await answer(query, tr("stale_button"))
        return
    await answer(query)
    await state.update_data(rem_kind=callback_data.x)
    if callback_data.x == "custom":
        await state.set_state(ReminderSG.text)
        await render(query, state, tr("rem.text_ask"), inline(nav(tr, St(a="rem_add"), home=False)))
        return
    await _ask_time(query, tr, state)


@router.message(ReminderSG.text, F.text)
async def reminder_text(message: Message, tr: Translator, state: FSMContext) -> None:
    assert message.text is not None
    await state.update_data(rem_text=message.text[:200])
    await _ask_time(message, tr, state)


async def _ask_time(event: Event, tr: Translator, state: FSMContext) -> None:
    await state.set_state(ReminderSG.time)
    presets = [(tr(key, time=t), St(a="rem_time", x=hhmm_pack(t))) for key, t in REMINDER_PRESETS]
    await render(
        event,
        state,
        tr("rem.time_ask"),
        inline(
            *[[b] for b in presets],
            [(tr("rem.pick_time"), St(a="rem_times"))],
            nav(tr, St(a="rem_add"), home=False),
        ),
    )


@router.callback_query(ReminderSG.time, St.filter(F.a == "rem_times"))
async def reminder_times(query: CallbackQuery, tr: Translator, state: FSMContext) -> None:
    await answer(query)
    buttons = [(t, St(a="rem_time", x=hhmm_pack(t))) for t in REMINDER_TIMES]
    await render(
        query,
        state,
        tr("rem.pick_time_ask"),
        inline(*grid(buttons, 5), nav(tr, St(a="rem_back_time"), home=False)),
    )


@router.callback_query(ReminderSG.time, St.filter(F.a == "rem_back_time"))
async def reminder_times_back(query: CallbackQuery, tr: Translator, state: FSMContext) -> None:
    await answer(query)
    await _ask_time(query, tr, state)


async def _reminder_time(event: Event, tr: Translator, state: FSMContext, text: str) -> None:
    try:
        parse_hhmm(text)
    except ValueError as exc:
        raise ServiceError("bad_time") from exc
    await state.update_data(rem_time=text.strip())
    await render(
        event,
        state,
        tr("rem.days_ask", time=text.strip()),
        inline(
            [
                (tr("rem.daily"), St(a="rem_days", x=str(ALL_DAYS))),
                (tr("rem.weekdays"), St(a="rem_days", x=str(WEEKDAYS))),
            ],
            nav(tr, St(a="rem_back_time"), home=False),
        ),
    )


@router.message(ReminderSG.time, F.text)
async def reminder_time(message: Message, tr: Translator, state: FSMContext) -> None:
    assert message.text is not None
    await _reminder_time(message, tr, state, message.text)


@router.callback_query(ReminderSG.time, St.filter(F.a == "rem_time"))
async def reminder_time_button(
    query: CallbackQuery, callback_data: St, tr: Translator, state: FSMContext
) -> None:
    await answer(query)
    await _reminder_time(query, tr, state, hhmm_unpack(callback_data.x))


@router.callback_query(ReminderSG.time, St.filter(F.a == "rem_days"))
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
        await answer(query, tr("stale_button"))
        return
    reminder = await ReminderService(session, user).create(
        data["rem_kind"], data["rem_time"], int(callback_data.x), data.get("rem_text")
    )
    await session.commit()
    await state.clear()
    await answer(query, tr("saved"))
    await render(
        query,
        state,
        tr(
            "rem.saved",
            time=f"{reminder.local_time:%H:%M}",
            what=_label(tr, reminder),
            days=_days(tr, reminder.days_mask),
        ),
        inline([(tr("rem.all"), Go(s="set", a="rem")), (tr("nav.home"), Go(s="home"))]),
    )


@router.callback_query(Rm.filter())
async def reminder_message_action(
    query: CallbackQuery, callback_data: Rm, session: AsyncSession, user: User, tr: Translator
) -> None:
    """Buttons under a delivered reminder: snooze or switch it off."""
    svc = ReminderService(session, user)
    if callback_data.a == "snooze":
        await svc.snooze(callback_data.id, 30)
        await session.commit()
        await answer(query, tr("rem.snoozed"))
    else:
        await svc.set_enabled(callback_data.id, False)
        await session.commit()
        await answer(query, tr("rem.disabled"))


@router.callback_query(St.filter(F.a == "quiet"))
async def quiet_start(query: CallbackQuery, tr: Translator, state: FSMContext) -> None:
    await answer(query)
    await state.clear()
    await render(
        query,
        state,
        tr("rem.quiet_ask"),
        inline(
            [
                (p.replace("-", "–"), St(a="quiet_set", x=str(i)))
                for i, p in enumerate(QUIET_PRESETS)
            ],
            [
                (tr("rem.quiet_custom"), St(a="quiet_custom")),
                (tr("rem.quiet_off"), St(a="quiet_off")),
            ],
            nav(tr, Go(s="set", a="rem"), home=False),
        ),
    )


@router.callback_query(St.filter(F.a == "quiet_custom"))
async def quiet_custom(query: CallbackQuery, tr: Translator, state: FSMContext) -> None:
    await answer(query)
    await state.set_state(ReminderSG.quiet)
    await render(
        query, state, tr("rem.quiet_custom_ask"), inline(nav(tr, St(a="quiet"), home=False))
    )


@router.message(ReminderSG.quiet, F.text)
async def quiet_value(
    message: Message, session: AsyncSession, user: User, tr: Translator, state: FSMContext
) -> None:
    assert message.text is not None
    start, _, end = message.text.replace("—", "-").replace("–", "-").partition("-")
    if not end.strip():
        raise ServiceError("bad_time")
    await ReminderService(session, user).set_quiet_hours(start.strip(), end.strip())
    await session.commit()
    await show_reminders(message, session, user, tr, state)


@router.callback_query(St.filter(F.a.in_({"quiet_set", "quiet_off"})))
async def quiet_preset(
    query: CallbackQuery,
    callback_data: St,
    session: AsyncSession,
    user: User,
    tr: Translator,
    state: FSMContext,
) -> None:
    svc = ReminderService(session, user)
    if callback_data.a == "quiet_off":
        await svc.set_quiet_hours(None, None)
    else:
        if not callback_data.x.isdigit() or int(callback_data.x) >= len(QUIET_PRESETS):
            raise ServiceError("bad_time")
        start, _, end = QUIET_PRESETS[int(callback_data.x)].partition("-")
        await svc.set_quiet_hours(start, end)
    await session.commit()
    await answer(query, tr("settings.saved"))
    await show_reminders(query, session, user, tr, state)


# --- data: export, import, delete --------------------------------------------------------------


@router.callback_query(Go.filter((F.s == "set") & (F.a == "data")))
async def data_menu(query: CallbackQuery, tr: Translator, state: FSMContext) -> None:
    await state.clear()
    await answer(query)
    await render(
        query,
        state,
        tr("settings.data_view"),
        inline(
            [(tr("settings.export"), St(a="export")), (tr("act.import"), Ac(action="import"))],
            [(tr("settings.delete"), St(a="delete"))],
            nav(tr, Go(s="set")),
        ),
    )


@router.callback_query(St.filter(F.a == "export"))
async def export(
    query: CallbackQuery, session: AsyncSession, user: User, tr: Translator, state: FSMContext
) -> None:
    await answer(query)
    data = await export_json(session, user)
    assert isinstance(query.message, Message)
    await query.message.answer_document(
        BufferedInputFile(data, filename="ritm-export.json"), caption=tr("settings.export_done")
    )
    await render(
        query, state, tr("settings.export_sent"), inline(nav(tr, Go(s="set", a="data"))), fresh=True
    )


@router.callback_query(St.filter(F.a == "delete"))
async def delete_start(query: CallbackQuery, tr: Translator, state: FSMContext) -> None:
    await answer(query)
    await state.set_state(DeleteSG.confirm)
    await render(
        query,
        state,
        tr("settings.delete_warn"),
        inline(nav(tr, Go(s="set", a="data"), cancel=True)),
    )


@router.message(DeleteSG.confirm, F.text)
async def delete_confirm(
    message: Message, session: AsyncSession, user: User, tr: Translator, state: FSMContext
) -> None:
    assert message.text is not None
    await delete_account(session, user, message.text)
    await session.commit()
    await state.clear()
    await render(message, state, tr("settings.deleted"))
