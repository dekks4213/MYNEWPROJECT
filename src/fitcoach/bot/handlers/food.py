"""🍽 Food: text / photo / voice -> preview -> confirm. Plus favourites, saved meals,
recipes and repeating a recent meal. All logic lives in services.food."""

from __future__ import annotations

import datetime as dt
import io
from collections import defaultdict
from decimal import Decimal

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession

from fitcoach.ai.gateway import AIGateway
from fitcoach.bot.handlers.common import Event
from fitcoach.bot.handlers.home import show_home
from fitcoach.bot.screen import answer, progress, render, replace
from fitcoach.bot.ui import (
    GRAM_PRESETS,
    MEAL_ICONS,
    MEAL_ORDER,
    Fm,
    Fr,
    Go,
    Row,
    amount_kb,
    draft_item_line,
    food_draft_kb,
    food_edit_kb,
    format_food_draft,
    grid,
    inline,
    nav,
    num,
)
from fitcoach.config import Settings
from fitcoach.db.models import User
from fitcoach.domain.food import MealType
from fitcoach.domain.units import ParseError, parse_decimal
from fitcoach.i18n import Translator, all_labels
from fitcoach.services.diary import DiaryService
from fitcoach.services.errors import ServiceError
from fitcoach.services.food import FoodService
from fitcoach.services.food_sources import FoodSource
from fitcoach.services.media import check_voice, prepare_image
from fitcoach.services.summary import build_day_summary
from fitcoach.services.users import UserService, local_today

router = Router(name="food")
RECENT_DAYS = 3
SERVINGS = {"s1": "1 порция", "s2": "2 порции"}


class FoodSG(StatesGroup):
    text = State()
    edit = State()
    add = State()
    meal_name = State()
    catalog = State()
    fav_amount = State()
    recipe_name = State()
    recipe_ingredients = State()
    recipe_yield = State()
    recipe_portion = State()


def service(
    session: AsyncSession,
    user: User,
    gateway: AIGateway,
    settings: Settings,
    food_sources: list[FoodSource],
) -> FoodService:
    return FoodService(
        session,
        user,
        gateway=gateway,
        sources=food_sources,
        ai_estimates=settings.ai_nutrient_estimates,
    )


async def draft_screen(
    svc: FoodService, draft_id: int, tr: Translator, title: str | None = None
) -> tuple[str, InlineKeyboardMarkup]:
    row, draft_state = await svc.get_draft(draft_id)
    return (
        format_food_draft(tr, draft_state, title=title),
        food_draft_kb(tr, row.id, row.version, draft_state),
    )


async def show_draft(
    event: Event,
    state: FSMContext,
    svc: FoodService,
    draft_id: int,
    tr: Translator,
    *,
    holder: Message | None = None,
    title: str | None = None,
) -> None:
    text, kb = await draft_screen(svc, draft_id, tr, title)
    await replace(holder, event, state, text, kb)


# --- menu --------------------------------------------------------------------------------------


async def show_food_menu(
    event: Event, user: User, tr: Translator, gateway: AIGateway, state: FSMContext
) -> None:
    await state.clear()
    rows: list[Row] = [[(tr("food.by_text"), Fm(a="text"))]]
    if gateway.enabled:
        rows[0] += [(tr("food.by_photo"), Fm(a="photo")), (tr("food.by_voice"), Fm(a="voice"))]
    rows.append(
        [
            (tr("food.favorites"), Fm(a="favs")),
            (tr("food.recent"), Fm(a="recent")),
            (tr("food.my_meals"), Fm(a="meals")),
        ]
    )
    rows.append(nav(tr))
    text = tr("food.menu") + ("\n\n" + tr("food.menu_media_hint") if gateway.enabled else "")
    await answer(event)
    await render(event, state, text, inline(*rows))


@router.message(F.text.in_(all_labels("menu.food") | all_labels("menu.old_food")))
async def food_menu(
    message: Message, user: User, tr: Translator, gateway: AIGateway, state: FSMContext
) -> None:
    await show_food_menu(message, user, tr, gateway, state)


@router.callback_query(Fm.filter(F.a == "menu"))
async def food_menu_cb(
    query: CallbackQuery, user: User, tr: Translator, gateway: AIGateway, state: FSMContext
) -> None:
    await show_food_menu(query, user, tr, gateway, state)


# --- text --------------------------------------------------------------------------------------


async def show_text_prompt(
    event: Event, user: User, tr: Translator, gateway: AIGateway, state: FSMContext
) -> None:
    await state.set_state(FoodSG.text)
    rows: list[Row] = []
    if gateway.text_available(user):
        text = tr("food.text_ask_ai")
    else:
        text = tr("food.text_ask")
        if gateway.enabled:
            text += "\n\n" + tr("food.ai_offer")
            rows.append([(tr("ai.allow_btn"), Fm(a="ai_on", x="text"))])
    rows.append(nav(tr, Fm(a="menu")))
    await render(event, state, text, inline(*rows))


@router.callback_query(Fm.filter(F.a == "text"))
async def text_start(
    query: CallbackQuery, user: User, tr: Translator, gateway: AIGateway, state: FSMContext
) -> None:
    await answer(query)
    await show_text_prompt(query, user, tr, gateway, state)


@router.callback_query(Fm.filter(F.a == "ai_on"))
async def ai_allow(
    query: CallbackQuery,
    callback_data: Fm,
    session: AsyncSession,
    user: User,
    tr: Translator,
    gateway: AIGateway,
    state: FSMContext,
) -> None:
    """Consent is asked where it is needed, not during onboarding."""
    svc = UserService(session, user)
    await svc.set_ai_consent(True)
    media = callback_data.x in ("photo", "voice")
    if media:
        await svc.set_media_consent(True)
    await session.commit()
    await answer(query, tr("ai.allowed"))
    if media:
        await _media_prompt(query, tr, state, callback_data.x)
    else:
        await show_text_prompt(query, user, tr, gateway, state)


async def draft_from_text(
    event: Event,
    text: str,
    session: AsyncSession,
    user: User,
    tr: Translator,
    gateway: AIGateway,
    settings: Settings,
    food_sources: list[FoodSource],
    state: FSMContext,
) -> None:
    svc = service(session, user, gateway, settings, food_sources)
    holder = (
        await progress(event, state, tr("food.working")) if gateway.text_available(user) else None
    )
    draft = await svc.draft_from_text(text)
    await session.commit()
    await state.clear()
    await show_draft(event, state, svc, draft.id, tr, holder=holder)


@router.message(FoodSG.text, F.text)
async def text_input(
    message: Message,
    session: AsyncSession,
    user: User,
    tr: Translator,
    gateway: AIGateway,
    settings: Settings,
    food_sources: list[FoodSource],
    state: FSMContext,
) -> None:
    assert message.text is not None
    await draft_from_text(
        message, message.text, session, user, tr, gateway, settings, food_sources, state
    )


# --- photo and voice ---------------------------------------------------------------------------


async def _media_gate(
    event: Event, user: User, tr: Translator, gateway: AIGateway, state: FSMContext, kind: str
) -> bool:
    if not gateway.enabled:
        await render(
            event,
            state,
            tr("food.media_needs_ai"),
            inline([(tr("food.by_text"), Fm(a="text"))], nav(tr)),
        )
        return False
    if not gateway.media_available(user):
        await render(
            event,
            state,
            tr("food.media_consent"),
            inline(
                [(tr("ai.allow_btn"), Fm(a="ai_on", x=kind)), (tr("ai.not_now"), Fm(a="menu"))],
            ),
        )
        return False
    return True


async def _media_prompt(query: CallbackQuery, tr: Translator, state: FSMContext, kind: str) -> None:
    await state.clear()
    key = "food.photo_ask" if kind == "photo" else "food.voice_ask"
    await render(query, state, tr(key), inline(nav(tr, Fm(a="menu"))))


@router.callback_query(Fm.filter(F.a.in_({"photo", "voice"})))
async def media_start(
    query: CallbackQuery,
    callback_data: Fm,
    user: User,
    tr: Translator,
    gateway: AIGateway,
    state: FSMContext,
) -> None:
    await answer(query)
    if await _media_gate(query, user, tr, gateway, state, callback_data.a):
        await _media_prompt(query, tr, state, callback_data.a)


async def _download(bot: Bot, file_id: str, size: int | None, limit: int) -> bytes:
    if size is not None and size > limit:
        raise ServiceError("media_too_large")
    buffer = io.BytesIO()
    await bot.download(file_id, destination=buffer, timeout=60)
    data = buffer.getvalue()
    if len(data) > limit:
        raise ServiceError("media_too_large")
    return data


@router.message(F.photo)
async def photo(
    message: Message,
    bot: Bot,
    session: AsyncSession,
    user: User,
    tr: Translator,
    gateway: AIGateway,
    settings: Settings,
    food_sources: list[FoodSource],
    state: FSMContext,
) -> None:
    """A food photo works from any screen; it never saves anything without confirmation."""
    await state.clear()
    if not await _media_gate(message, user, tr, gateway, state, "photo"):
        return
    assert message.photo
    candidates = [p for p in message.photo if (p.file_size or 0) <= settings.max_photo_bytes]
    if not candidates:
        raise ServiceError("media_too_large")
    best = max(candidates, key=lambda p: p.width * p.height)
    raw = await _download(bot, best.file_id, best.file_size, settings.max_photo_bytes)
    image, mime = prepare_image(raw, settings.max_photo_bytes)
    holder = await progress(message, state, tr("food.photo_working"))
    svc = service(session, user, gateway, settings, food_sources)
    draft = await svc.draft_from_photo(image, mime, message.caption)
    await session.commit()
    await show_draft(message, state, svc, draft.id, tr, holder=holder)


@router.message(F.voice)
async def voice(
    message: Message,
    bot: Bot,
    session: AsyncSession,
    user: User,
    tr: Translator,
    gateway: AIGateway,
    settings: Settings,
    food_sources: list[FoodSource],
    state: FSMContext,
) -> None:
    await state.clear()
    if not await _media_gate(message, user, tr, gateway, state, "voice"):
        return
    assert message.voice is not None
    v = message.voice
    if v.duration > settings.max_voice_seconds:
        raise ServiceError("media_too_large")
    raw = await _download(bot, v.file_id, v.file_size, settings.max_voice_bytes)
    mime = check_voice(
        raw,
        duration_s=v.duration,
        max_bytes=settings.max_voice_bytes,
        max_seconds=settings.max_voice_seconds,
    )
    holder = await progress(message, state, tr("food.voice_working"))
    svc = service(session, user, gateway, settings, food_sources)
    draft = await svc.draft_from_voice(raw, mime)
    await session.commit()
    await show_draft(message, state, svc, draft.id, tr, holder=holder)


# --- draft actions -----------------------------------------------------------------------------


async def _saved_screen(
    query: CallbackQuery,
    session: AsyncSession,
    user: User,
    tr: Translator,
    state: FSMContext,
    meal: str,
    kcal: Decimal | None,
    unknown: int,
) -> None:
    summary = await build_day_summary(session, user)
    lines = [tr("food.saved", meal=f"{MEAL_ICONS[meal]} {tr('meal.' + meal)}")]
    if kcal is not None:
        lines.append(f"{num(tr, kcal)} {tr('unit.kcal')}")
    if unknown:
        lines.append(tr("food.saved_unknown", n=unknown))
    total = summary.totals.energy_kcal
    if total.known_entries:
        today = num(tr, total.value)
        if summary.kcal_target is not None:
            today += f" / {num(tr, summary.kcal_target)}"
        lines += ["", tr("food.saved_today", kcal=today)]
    await render(
        query,
        state,
        "\n".join(lines),
        inline(
            [(tr("food.more"), Fm(a="menu")), (tr("home.btn_day"), Go(s="day", a="0"))],
            nav(tr),
        ),
    )


async def _edit_screen(
    event: Event, svc: FoodService, draft_id: int, tr: Translator, state: FSMContext
) -> None:
    row, draft_state = await svc.get_draft(draft_id)
    await render(
        event,
        state,
        format_food_draft(tr, draft_state) + "\n\n" + tr("draft.edit_title"),
        food_edit_kb(tr, row.id, row.version, draft_state),
    )


@router.callback_query(Fr.filter())
async def draft_action(
    query: CallbackQuery,
    callback_data: Fr,
    session: AsyncSession,
    user: User,
    tr: Translator,
    gateway: AIGateway,
    settings: Settings,
    food_sources: list[FoodSource],
    state: FSMContext,
) -> None:
    svc = service(session, user, gateway, settings, food_sources)
    cb = callback_data
    if cb.a == "ok":
        _, current = await svc.get_draft(cb.d)
        entries = await svc.confirm(cb.d, cb.v)
        await session.commit()
        await state.clear()
        await answer(query, tr("saved"))
        kcal = [e.energy_kcal for e in entries if e.energy_kcal is not None]
        await _saved_screen(
            query,
            session,
            user,
            tr,
            state,
            current.meal_type.value,
            sum(kcal, Decimal(0)) if kcal else None,
            len(entries) - len(kcal),
        )
        return
    if cb.a in ("no", "back"):
        await svc.cancel(cb.d)
        await session.commit()
        if cb.a == "back":
            await show_recent(query, session, user, tr, state)
        else:
            await show_home(query, session, user, tr, state, note=tr("draft.cancelled"))
        return
    if cb.a in ("del", "amt", "meal"):
        if cb.a == "del":
            draft = await svc.remove_item(cb.d, cb.v, cb.i)
        elif cb.a == "meal":
            _, current = await svc.get_draft(cb.d)
            nxt = MEAL_ORDER[(MEAL_ORDER.index(current.meal_type.value) + 1) % len(MEAL_ORDER)]
            draft = await svc.set_meal_type(cb.d, cb.v, MealType(nxt))
        elif cb.x.startswith("g") and cb.x[1:] in GRAM_PRESETS:
            draft = await svc.edit_item(cb.d, cb.v, cb.i, f"{cb.x[1:]} г")
        elif cb.x.startswith("x"):
            try:
                factor = parse_decimal(cb.x[1:])
            except ParseError as exc:
                raise ServiceError("bad_amount") from exc
            draft = await svc.scale_item(cb.d, cb.v, cb.i, factor)
        else:
            raise ServiceError("bad_amount")
        await session.commit()
        await state.clear()
        await answer(query)
        if cb.a == "meal":
            await _edit_screen(query, svc, draft.id, tr, state)
        else:
            await show_draft(query, state, svc, draft.id, tr)
        return
    row, current_state = await svc.get_draft(cb.d)  # ownership check before anything else
    await answer(query)
    if cb.a == "show":
        await state.clear()
        await show_draft(query, state, svc, row.id, tr)
    elif cb.a == "edits":
        await state.clear()
        await _edit_screen(query, svc, row.id, tr, state)
    elif cb.a == "edit":
        if not 0 <= cb.i < len(current_state.items):
            raise ServiceError("not_found")
        await state.set_state(FoodSG.edit)
        await state.update_data(draft_id=cb.d, version=cb.v, index=cb.i)
        item = current_state.items[cb.i]
        await render(
            query,
            state,
            draft_item_line(tr, item) + "\n\n" + tr("draft.edit_ask"),
            amount_kb(tr, cb.d, cb.v, cb.i),
        )
    elif cb.a == "add":
        await state.set_state(FoodSG.add)
        await state.update_data(draft_id=cb.d, version=cb.v)
        await render(
            query,
            state,
            tr("draft.add_ask"),
            inline(nav(tr, Fr(a="show", d=cb.d, v=cb.v), home=False)),
        )
    elif cb.a == "fav":
        await state.set_state(FoodSG.meal_name)
        await state.update_data(draft_id=cb.d, version=cb.v)
        await render(
            query,
            state,
            tr("draft.meal_name_ask"),
            inline(nav(tr, Fr(a="edits", d=cb.d, v=cb.v), home=False)),
        )
    else:
        raise ServiceError("bad_choice")


async def _draft_state_data(state: FSMContext) -> tuple[int, int, int]:
    data = await state.get_data()
    return int(data["draft_id"]), int(data["version"]), int(data.get("index", 0))


@router.message(FoodSG.edit, F.text)
async def draft_edit(
    message: Message,
    session: AsyncSession,
    user: User,
    tr: Translator,
    gateway: AIGateway,
    settings: Settings,
    food_sources: list[FoodSource],
    state: FSMContext,
) -> None:
    assert message.text is not None
    draft_id, version, index = await _draft_state_data(state)
    svc = service(session, user, gateway, settings, food_sources)
    draft = await svc.edit_item(draft_id, version, index, message.text)
    await session.commit()
    await state.clear()
    await show_draft(message, state, svc, draft.id, tr)


@router.message(FoodSG.add, F.text)
async def draft_add(
    message: Message,
    session: AsyncSession,
    user: User,
    tr: Translator,
    gateway: AIGateway,
    settings: Settings,
    food_sources: list[FoodSource],
    state: FSMContext,
) -> None:
    assert message.text is not None
    draft_id, version, _ = await _draft_state_data(state)
    svc = service(session, user, gateway, settings, food_sources)
    draft = await svc.add_items(draft_id, version, message.text)
    await session.commit()
    await state.clear()
    await show_draft(message, state, svc, draft.id, tr)


@router.message(FoodSG.meal_name, F.text)
async def draft_save_meal(
    message: Message,
    session: AsyncSession,
    user: User,
    tr: Translator,
    gateway: AIGateway,
    settings: Settings,
    food_sources: list[FoodSource],
    state: FSMContext,
) -> None:
    assert message.text is not None
    draft_id, _, _ = await _draft_state_data(state)
    svc = service(session, user, gateway, settings, food_sources)
    meal = await svc.save_meal_from_draft(draft_id, message.text)
    await session.commit()
    await state.clear()
    text, kb = await draft_screen(svc, draft_id, tr)
    await render(message, state, tr("food.meal_saved", name=meal.name) + "\n\n" + text, kb)


# --- favourites --------------------------------------------------------------------------------


@router.callback_query(Fm.filter(F.a == "favs"))
async def favorites(
    query: CallbackQuery,
    session: AsyncSession,
    user: User,
    tr: Translator,
    gateway: AIGateway,
    settings: Settings,
    food_sources: list[FoodSource],
    state: FSMContext,
) -> None:
    await answer(query)
    await state.clear()
    foods = await service(session, user, gateway, settings, food_sources).list_foods(
        favorites_only=True
    )
    add_row: Row = [(tr("food.add_product"), Fm(a="catalog"))]
    if not foods:
        await render(query, state, tr("food.no_favorites"), inline(add_row, nav(tr, Fm(a="menu"))))
        return
    buttons = [(f"⭐ {f.name}", Fm(a="fav", id=f.id)) for f in foods[:16]]
    await render(
        query,
        state,
        tr("food.favorites_list"),
        inline(*grid(buttons), add_row, nav(tr, Fm(a="menu"))),
    )


@router.callback_query(Fm.filter(F.a == "fav"))
async def favorite_pick(
    query: CallbackQuery,
    callback_data: Fm,
    session: AsyncSession,
    user: User,
    tr: Translator,
    gateway: AIGateway,
    settings: Settings,
    food_sources: list[FoodSource],
    state: FSMContext,
) -> None:
    food = await service(session, user, gateway, settings, food_sources).get_food(callback_data.id)
    await answer(query)
    await state.set_state(FoodSG.fav_amount)
    await state.update_data(food_id=food.id)
    hint = tr("food.serving_hint", g=num(tr, food.serving_g)) if food.serving_g else ""
    presets = [(f"{g} {tr('unit.g')}", Fm(a="favamt", id=food.id, x=g)) for g in GRAM_PRESETS]
    rows: list[Row] = grid(presets, 3)
    if food.serving_g:
        rows.insert(
            0,
            [
                (tr("food.one_serving"), Fm(a="favamt", id=food.id, x="s1")),
                (tr("food.two_servings"), Fm(a="favamt", id=food.id, x="s2")),
            ],
        )
    rows.append(nav(tr, Fm(a="favs"), home=False))
    await render(query, state, tr("food.amount_ask", name=food.name) + hint, inline(*rows))


@router.callback_query(Fm.filter(F.a == "favamt"))
async def favorite_amount_button(
    query: CallbackQuery,
    callback_data: Fm,
    session: AsyncSession,
    user: User,
    tr: Translator,
    gateway: AIGateway,
    settings: Settings,
    food_sources: list[FoodSource],
    state: FSMContext,
) -> None:
    x = callback_data.x
    if x in SERVINGS:
        amount = SERVINGS[x]
    elif x in GRAM_PRESETS:
        amount = f"{x} г"
    else:
        raise ServiceError("bad_amount")
    svc = service(session, user, gateway, settings, food_sources)
    draft = await svc.draft_from_catalog(callback_data.id, amount)
    await session.commit()
    await state.clear()
    await answer(query)
    await show_draft(query, state, svc, draft.id, tr)


@router.message(FoodSG.fav_amount, F.text)
async def favorite_amount(
    message: Message,
    session: AsyncSession,
    user: User,
    tr: Translator,
    gateway: AIGateway,
    settings: Settings,
    food_sources: list[FoodSource],
    state: FSMContext,
) -> None:
    assert message.text is not None
    data = await state.get_data()
    svc = service(session, user, gateway, settings, food_sources)
    draft = await svc.draft_from_catalog(int(data["food_id"]), message.text)
    await session.commit()
    await state.clear()
    await show_draft(message, state, svc, draft.id, tr)


# --- saved meals and recipes -------------------------------------------------------------------


@router.callback_query(Fm.filter(F.a == "meals"))
async def meals(
    query: CallbackQuery,
    session: AsyncSession,
    user: User,
    tr: Translator,
    gateway: AIGateway,
    settings: Settings,
    food_sources: list[FoodSource],
    state: FSMContext,
) -> None:
    await answer(query)
    await state.clear()
    items = await service(session, user, gateway, settings, food_sources).list_meals()
    buttons = [
        (("🥘 " if m.kind == "recipe" else "🍲 ") + m.name, Fm(a="meal", id=m.id))
        for m in items[:16]
    ]
    await render(
        query,
        state,
        tr("food.meals_list") if items else tr("food.no_meals"),
        inline(
            *grid(buttons),
            [(tr("food.new_recipe"), Fm(a="recipe")), (tr("food.add_product"), Fm(a="catalog"))],
            nav(tr, Fm(a="menu")),
        ),
    )


@router.callback_query(Fm.filter(F.a == "meal"))
async def meal_pick(
    query: CallbackQuery,
    callback_data: Fm,
    session: AsyncSession,
    user: User,
    tr: Translator,
    gateway: AIGateway,
    settings: Settings,
    food_sources: list[FoodSource],
    state: FSMContext,
) -> None:
    svc = service(session, user, gateway, settings, food_sources)
    meal = await svc.get_meal(callback_data.id)
    await answer(query)
    if meal.kind == "recipe":
        await state.set_state(FoodSG.recipe_portion)
        await state.update_data(meal_id=meal.id)
        fractions = [("1", "1"), ("½", "0.5"), ("⅓", "0.333"), ("¼", "0.25")]
        await render(
            query,
            state,
            tr("food.recipe_portion_ask", name=meal.name),
            inline(
                [(label, Fm(a="portion", id=meal.id, x=value)) for label, value in fractions],
                nav(tr, Fm(a="meals"), home=False),
            ),
        )
        return
    draft = await svc.draft_from_saved(meal.id)
    await session.commit()
    await state.clear()
    await show_draft(query, state, svc, draft.id, tr)


@router.callback_query(Fm.filter(F.a == "portion"))
async def recipe_fraction(
    query: CallbackQuery,
    callback_data: Fm,
    session: AsyncSession,
    user: User,
    tr: Translator,
    gateway: AIGateway,
    settings: Settings,
    food_sources: list[FoodSource],
    state: FSMContext,
) -> None:
    try:
        fraction = parse_decimal(callback_data.x)
    except ParseError as exc:
        raise ServiceError("bad_fraction") from exc
    svc = service(session, user, gateway, settings, food_sources)
    draft = await svc.draft_from_saved(callback_data.id, fraction=fraction)
    await session.commit()
    await state.clear()
    await answer(query)
    await show_draft(query, state, svc, draft.id, tr)


@router.message(FoodSG.recipe_portion, F.text)
async def recipe_grams(
    message: Message,
    session: AsyncSession,
    user: User,
    tr: Translator,
    gateway: AIGateway,
    settings: Settings,
    food_sources: list[FoodSource],
    state: FSMContext,
) -> None:
    assert message.text is not None
    data = await state.get_data()
    text = message.text.strip().lower().rstrip("гg").strip()
    try:
        grams = parse_decimal(text)
    except ParseError as exc:
        raise ServiceError("bad_amount") from exc
    svc = service(session, user, gateway, settings, food_sources)
    draft = await svc.draft_from_saved(int(data["meal_id"]), grams=grams)
    await session.commit()
    await state.clear()
    await show_draft(message, state, svc, draft.id, tr)


@router.callback_query(Fm.filter(F.a == "catalog"))
async def catalog_start(query: CallbackQuery, tr: Translator, state: FSMContext) -> None:
    await answer(query)
    await state.set_state(FoodSG.catalog)
    await render(query, state, tr("food.catalog_ask"), inline(nav(tr, Fm(a="meals"), home=False)))


@router.message(FoodSG.catalog, F.text)
async def catalog_add(
    message: Message,
    session: AsyncSession,
    user: User,
    tr: Translator,
    gateway: AIGateway,
    settings: Settings,
    food_sources: list[FoodSource],
    state: FSMContext,
) -> None:
    assert message.text is not None
    food = await service(session, user, gateway, settings, food_sources).add_catalog_food(
        message.text
    )
    await session.commit()
    await state.clear()
    await render(
        message,
        state,
        tr("food.catalog_saved", name=food.name, kcal=num(tr, food.energy_kcal)),
        inline([(tr("food.log_it"), Fm(a="fav", id=food.id))], nav(tr, Fm(a="menu"))),
    )


@router.callback_query(Fm.filter(F.a == "recipe"))
async def recipe_start(query: CallbackQuery, tr: Translator, state: FSMContext) -> None:
    await answer(query)
    await state.set_state(FoodSG.recipe_name)
    await render(
        query, state, tr("food.recipe_name_ask"), inline(nav(tr, Fm(a="meals"), home=False))
    )


@router.message(FoodSG.recipe_name, F.text)
async def recipe_name(message: Message, tr: Translator, state: FSMContext) -> None:
    assert message.text is not None
    name = " ".join(message.text.split())
    if not name or len(name) > 80:
        raise ServiceError("bad_name")
    await state.update_data(recipe_name=name)
    await state.set_state(FoodSG.recipe_ingredients)
    await render(message, state, tr("food.recipe_ingredients_ask"), inline(nav(tr, cancel=True)))


@router.message(FoodSG.recipe_ingredients, F.text)
async def recipe_ingredients(message: Message, tr: Translator, state: FSMContext) -> None:
    assert message.text is not None
    if len(message.text) > 1500:
        raise ServiceError("too_long")
    await state.update_data(ingredients=message.text)
    await state.set_state(FoodSG.recipe_yield)
    await render(
        message,
        state,
        tr("food.recipe_yield_ask"),
        inline([(tr("btn.skip"), Fm(a="yield_skip"))], nav(tr, cancel=True)),
    )


async def _save_recipe(
    event: Event,
    session: AsyncSession,
    tr: Translator,
    svc: FoodService,
    state: FSMContext,
    cooked: Decimal | None,
) -> None:
    data = await state.get_data()
    recipe = await svc.create_recipe(data["recipe_name"], data["ingredients"], cooked)
    await session.commit()
    await state.clear()
    unknown = sum(1 for i in recipe.items if i.get("energy_kcal") is None)
    text = tr("food.recipe_saved", name=recipe.name, n=len(recipe.items))
    if unknown:
        text += "\n" + tr("food.recipe_unknown", n=unknown)
    await render(
        event,
        state,
        text,
        inline([(tr("food.log_it"), Fm(a="meal", id=recipe.id))], nav(tr, Fm(a="meals"))),
    )


@router.message(FoodSG.recipe_yield, F.text)
async def recipe_yield(
    message: Message,
    session: AsyncSession,
    user: User,
    tr: Translator,
    gateway: AIGateway,
    settings: Settings,
    food_sources: list[FoodSource],
    state: FSMContext,
) -> None:
    assert message.text is not None
    try:
        cooked = parse_decimal(message.text.strip().lower().rstrip("гg").strip())
    except ParseError as exc:
        raise ServiceError("bad_amount") from exc
    svc = service(session, user, gateway, settings, food_sources)
    await _save_recipe(message, session, tr, svc, state, cooked)


@router.callback_query(FoodSG.recipe_yield, Fm.filter(F.a == "yield_skip"))
async def recipe_yield_skip(
    query: CallbackQuery,
    session: AsyncSession,
    user: User,
    tr: Translator,
    gateway: AIGateway,
    settings: Settings,
    food_sources: list[FoodSource],
    state: FSMContext,
) -> None:
    await answer(query)
    svc = service(session, user, gateway, settings, food_sources)
    await _save_recipe(query, session, tr, svc, state, None)


# --- recent meals ------------------------------------------------------------------------------


def _recent_label(tr: Translator, offset: int, meal: str) -> str:
    return f"{MEAL_ICONS[meal]} " + tr(f"recent.btn.{offset}", meal=tr("meal_l." + meal))


async def show_recent(
    event: Event, session: AsyncSession, user: User, tr: Translator, state: FSMContext
) -> None:
    """Meals of the last days, grouped; one tap repeats one as a draft."""
    await state.clear()
    today = local_today(user)
    diary = DiaryService(session, user)
    lines = [tr("recent.title")]
    buttons: Row = []
    for offset in range(RECENT_DAYS):
        food, _, _ = await diary.entries_for_day(today - dt.timedelta(days=offset))
        groups: dict[str, list[Decimal | None]] = defaultdict(list)
        for entry in food:
            if entry.meal_type in MEAL_ORDER:
                groups[entry.meal_type].append(entry.energy_kcal)
        if not groups:
            continue
        lines += ["", tr(("rel.today_cap", "rel.yesterday_cap", "rel.day_before_cap")[offset])]
        for meal in (m for m in MEAL_ORDER if m in groups):
            known = [k for k in groups[meal] if k is not None]
            kcal = (
                f"{num(tr, sum(known, Decimal(0)))} {tr('unit.kcal')}"
                if known
                else tr("food.kcal_unknown")
            )
            lines.append(f"{MEAL_ICONS[meal]} {tr('meal.' + meal)} · {kcal}")
            buttons.append((_recent_label(tr, offset, meal), Fm(a="cp", x=f"{offset}-{meal}")))
    await answer(event)
    if not buttons:
        await render(
            event,
            state,
            tr("recent.empty"),
            inline([(tr("food.by_text"), Fm(a="text"))], nav(tr, Fm(a="menu"))),
        )
        return
    await render(
        event,
        state,
        "\n".join(lines),
        inline(*[[b] for b in buttons[:8]], nav(tr, Fm(a="menu"))),
    )


@router.callback_query(Fm.filter(F.a.in_({"recent", "copy"})))
async def recent(
    query: CallbackQuery, session: AsyncSession, user: User, tr: Translator, state: FSMContext
) -> None:
    await show_recent(query, session, user, tr, state)


@router.callback_query(Fm.filter(F.a == "cp"))
async def copy_pick(
    query: CallbackQuery,
    callback_data: Fm,
    session: AsyncSession,
    user: User,
    tr: Translator,
    gateway: AIGateway,
    settings: Settings,
    food_sources: list[FoodSource],
    state: FSMContext,
) -> None:
    offset_text, _, meal = callback_data.x.partition("-")
    if offset_text not in ("0", "1", "2") or (meal and meal not in MEAL_ORDER):
        raise ServiceError("bad_choice")
    offset = int(offset_text)
    day = local_today(user) - dt.timedelta(days=offset)
    svc = service(session, user, gateway, settings, food_sources)
    draft = await svc.draft_copy(day, MealType(meal) if meal else None)
    await session.commit()
    await state.clear()
    await answer(query)
    title = _recent_label(tr, offset, meal) if meal else None
    await show_draft(query, state, svc, draft.id, tr, title=title)
