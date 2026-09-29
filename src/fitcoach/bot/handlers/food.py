"""Food logging UI: text / photo / voice -> editable draft -> confirm. Plus favourites,
saved meals, recipes and copying a previous meal. All logic lives in services.food."""

from __future__ import annotations

import datetime as dt
import io
from decimal import Decimal
from typing import Any

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from fitcoach.ai.gateway import AIGateway
from fitcoach.bot.handlers.common import msg
from fitcoach.bot.ui import (
    GRAM_PRESETS,
    MEAL_ORDER,
    Fd,
    Fm,
    Fr,
    St,
    amount_kb,
    cancel_kb,
    column,
    food_draft_kb,
    format_food_draft,
    inline,
    main_menu,
    num,
)
from fitcoach.config import Settings
from fitcoach.db.models import Draft, User
from fitcoach.domain.food import MealType
from fitcoach.domain.units import ParseError, parse_decimal
from fitcoach.i18n import Translator, all_labels
from fitcoach.services.errors import ServiceError
from fitcoach.services.food import FoodService
from fitcoach.services.food_sources import FoodSource
from fitcoach.services.media import check_voice, prepare_image
from fitcoach.services.users import local_today

router = Router(name="food")


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
    photo = State()
    voice = State()


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


async def show_draft(message: Message, svc: FoodService, draft: Draft, tr: Translator) -> None:
    row, state = await svc.get_draft(draft.id)
    await message.answer(
        format_food_draft(tr, state), reply_markup=food_draft_kb(tr, row.id, row.version, state)
    )


# --- menu --------------------------------------------------------------------------------


def food_menu_kb(tr: Translator, user: User, gateway: AIGateway) -> Any:
    rows: list[list[tuple[str, Any]]] = [[(tr("food.by_text"), Fm(a="text"))]]
    if gateway.enabled:
        rows.append([(tr("food.by_photo"), Fm(a="photo")), (tr("food.by_voice"), Fm(a="voice"))])
    rows.append([(tr("food.favorites"), Fm(a="favs")), (tr("food.my_meals"), Fm(a="meals"))])
    rows.append([(tr("food.copy"), Fm(a="copy"))])
    rows.append(
        [(tr("food.add_product"), Fm(a="catalog")), (tr("food.new_recipe"), Fm(a="recipe"))]
    )
    return inline(*rows)


@router.message(F.text.in_(all_labels("menu.food")))
async def food_menu(
    message: Message, user: User, tr: Translator, gateway: AIGateway, state: FSMContext
) -> None:
    await state.clear()
    await message.answer(tr("food.menu"), reply_markup=food_menu_kb(tr, user, gateway))


@router.callback_query(Fm.filter(F.a == "menu"))
async def food_menu_cb(
    query: CallbackQuery, user: User, tr: Translator, gateway: AIGateway, state: FSMContext
) -> None:
    await query.answer()
    await state.clear()
    await msg(query).answer(tr("food.menu"), reply_markup=food_menu_kb(tr, user, gateway))


# --- text ------------------------------------------------------------------------------------


@router.callback_query(Fm.filter(F.a == "text"))
async def text_start(
    query: CallbackQuery, user: User, tr: Translator, gateway: AIGateway, state: FSMContext
) -> None:
    await query.answer()
    await state.set_state(FoodSG.text)
    key = "food.text_ask_ai" if gateway.text_available(user) else "food.text_ask"
    await msg(query).answer(tr(key), reply_markup=cancel_kb(tr))


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
    svc = service(session, user, gateway, settings, food_sources)
    if gateway.text_available(user):
        await message.answer(tr("food.working"))
    draft = await svc.draft_from_text(message.text)
    await session.commit()
    await state.clear()
    await show_draft(message, svc, draft, tr)


# --- photo and voice ---------------------------------------------------------------------------


async def _media_gate(message: Message, user: User, tr: Translator, gateway: AIGateway) -> bool:
    if not gateway.enabled:
        await message.answer(tr("food.media_needs_ai"), reply_markup=main_menu(tr))
        return False
    if not gateway.media_available(user):
        await message.answer(
            tr("food.media_consent"),
            reply_markup=inline([(tr("settings.media_on"), St(a="media", x="on"))]),
        )
        return False
    return True


@router.callback_query(Fm.filter(F.a.in_({"photo", "voice"})))
async def media_start(
    query: CallbackQuery,
    callback_data: Fm,
    user: User,
    tr: Translator,
    gateway: AIGateway,
    state: FSMContext,
) -> None:
    await query.answer()
    if not await _media_gate(msg(query), user, tr, gateway):
        return
    if callback_data.a == "photo":
        await state.set_state(FoodSG.photo)
        await msg(query).answer(tr("food.photo_ask"), reply_markup=cancel_kb(tr))
    else:
        await state.set_state(FoodSG.voice)
        await msg(query).answer(tr("food.voice_ask"), reply_markup=cancel_kb(tr))


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
    if not await _media_gate(message, user, tr, gateway):
        return
    assert message.photo
    candidates = [p for p in message.photo if (p.file_size or 0) <= settings.max_photo_bytes]
    if not candidates:
        raise ServiceError("media_too_large")
    best = max(candidates, key=lambda p: p.width * p.height)
    raw = await _download(bot, best.file_id, best.file_size, settings.max_photo_bytes)
    image, mime = prepare_image(raw, settings.max_photo_bytes)
    await message.answer(tr("food.photo_working"))
    svc = service(session, user, gateway, settings, food_sources)
    draft = await svc.draft_from_photo(image, mime, message.caption)
    await session.commit()
    await show_draft(message, svc, draft, tr)


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
    if not await _media_gate(message, user, tr, gateway):
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
    await message.answer(tr("food.voice_working"))
    svc = service(session, user, gateway, settings, food_sources)
    draft = await svc.draft_from_voice(raw, mime)
    await session.commit()
    await show_draft(message, svc, draft, tr)


# --- draft actions ----------------------------------------------------------------------------


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
    message = msg(query)
    if cb.a == "ok":
        entries = await svc.confirm(cb.d, cb.v)
        await session.commit()
        await state.clear()
        await query.answer(tr("saved"))
        kcal = [e.energy_kcal for e in entries if e.energy_kcal is not None]
        text = tr("food.saved", n=len(entries), kcal=num(tr, sum(kcal, Decimal(0))))
        if len(kcal) < len(entries):
            text += "\n" + tr("food.saved_unknown", n=len(entries) - len(kcal))
        await message.answer(text, reply_markup=main_menu(tr))
        return
    if cb.a == "no":
        await svc.cancel(cb.d)
        await session.commit()
        await state.clear()
        await query.answer(tr("cancelled"))
        await message.answer(tr("draft.cancelled"), reply_markup=main_menu(tr))
        return
    if cb.a == "del":
        draft = await svc.remove_item(cb.d, cb.v, cb.i)
        await session.commit()
        await query.answer()
        await show_draft(message, svc, draft, tr)
        return
    if cb.a == "amt":
        if cb.x.startswith("g") and cb.x[1:].isdigit():
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
        await query.answer()
        await show_draft(message, svc, draft, tr)
        return
    if cb.a == "meal":
        _, current = await svc.get_draft(cb.d)
        nxt = MEAL_ORDER[(MEAL_ORDER.index(current.meal_type.value) + 1) % len(MEAL_ORDER)]
        draft = await svc.set_meal_type(cb.d, cb.v, MealType(nxt))
        await session.commit()
        await query.answer(tr("meal." + nxt))
        await show_draft(message, svc, draft, tr)
        return
    # Actions that need a text reply keep the draft id+version in FSM data.
    _, current_state = await svc.get_draft(cb.d)  # ownership check before asking anything
    await query.answer()
    await state.update_data(draft_id=cb.d, version=cb.v, index=cb.i)
    if cb.a == "edit":
        await state.set_state(FoodSG.edit)
        await message.answer(
            tr(
                "draft.edit_ask",
                n=current_state.items[cb.i].name
                if 0 <= cb.i < len(current_state.items)
                else cb.i + 1,
            ),
            reply_markup=amount_kb(tr, cb.d, cb.v, cb.i),
        )
    elif cb.a == "add":
        await state.set_state(FoodSG.add)
        await message.answer(tr("draft.add_ask"), reply_markup=cancel_kb(tr))
    elif cb.a == "fav":
        await state.set_state(FoodSG.meal_name)
        await message.answer(tr("draft.meal_name_ask"), reply_markup=cancel_kb(tr))


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
    await show_draft(message, svc, draft, tr)


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
    await show_draft(message, svc, draft, tr)


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
    await message.answer(tr("food.meal_saved", name=meal.name))
    await show_draft(message, svc, (await svc.get_draft(draft_id))[0], tr)


# --- favourites, saved meals, recipes ----------------------------------------------------------


@router.callback_query(Fm.filter(F.a == "favs"))
async def favorites(
    query: CallbackQuery,
    session: AsyncSession,
    user: User,
    tr: Translator,
    gateway: AIGateway,
    settings: Settings,
    food_sources: list[FoodSource],
) -> None:
    await query.answer()
    svc = service(session, user, gateway, settings, food_sources)
    foods = await svc.list_foods(favorites_only=True)
    if not foods:
        await msg(query).answer(
            tr("food.no_favorites"),
            reply_markup=inline([(tr("food.add_product"), Fm(a="catalog"))]),
        )
        return
    buttons = [(f"⭐ {f.name}", Fm(a="fav", id=f.id)) for f in foods]
    await msg(query).answer(tr("food.favorites_list"), reply_markup=column(buttons, width=2))


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
    await query.answer()
    await state.set_state(FoodSG.fav_amount)
    await state.update_data(food_id=food.id)
    hint = tr("food.serving_hint", g=num(tr, food.serving_g)) if food.serving_g else ""
    presets = [
        (f"{g} {tr('unit.g')}", Fm(a="favamt", id=food.id, x=f"{g} г")) for g in GRAM_PRESETS
    ]
    rows: list[list[tuple[str, Any]]] = [presets[:3], presets[3:]]
    if food.serving_g:
        rows.insert(
            0,
            [
                (tr("food.one_serving"), Fm(a="favamt", id=food.id, x="1 порция")),
                (tr("food.two_servings"), Fm(a="favamt", id=food.id, x="2 порции")),
            ],
        )
    rows.append([(tr("btn.cancel"), Fd(action="cancel"))])
    await msg(query).answer(
        tr("food.amount_ask", name=food.name) + hint, reply_markup=inline(*rows)
    )


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
    svc = service(session, user, gateway, settings, food_sources)
    draft = await svc.draft_from_catalog(callback_data.id, callback_data.x[:20])
    await session.commit()
    await state.clear()
    await query.answer()
    await show_draft(msg(query), svc, draft, tr)


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
    await show_draft(message, svc, draft, tr)


@router.callback_query(Fm.filter(F.a == "meals"))
async def meals(
    query: CallbackQuery,
    session: AsyncSession,
    user: User,
    tr: Translator,
    gateway: AIGateway,
    settings: Settings,
    food_sources: list[FoodSource],
) -> None:
    await query.answer()
    items = await service(session, user, gateway, settings, food_sources).list_meals()
    if not items:
        await msg(query).answer(tr("food.no_meals"))
        return
    buttons = [
        (("🥘 " if m.kind == "recipe" else "🍲 ") + m.name, Fm(a="meal", id=m.id)) for m in items
    ]
    await msg(query).answer(tr("food.meals_list"), reply_markup=column(buttons))


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
    await query.answer()
    if meal.kind == "recipe":
        await state.set_state(FoodSG.recipe_portion)
        await state.update_data(meal_id=meal.id)
        fractions = [("1", "1"), ("½", "0.5"), ("⅓", "0.333"), ("¼", "0.25")]
        await msg(query).answer(
            tr("food.recipe_portion_ask", name=meal.name),
            reply_markup=inline(
                [(label, Fm(a="portion", id=meal.id, x=value)) for label, value in fractions],
                [(tr("btn.cancel"), Fd(action="cancel"))],
            ),
        )
        return
    draft = await svc.draft_from_saved(meal.id)
    await session.commit()
    await show_draft(msg(query), svc, draft, tr)


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
    await query.answer()
    await show_draft(msg(query), svc, draft, tr)


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
    await show_draft(message, svc, draft, tr)


@router.callback_query(Fm.filter(F.a == "catalog"))
async def catalog_start(query: CallbackQuery, tr: Translator, state: FSMContext) -> None:
    await query.answer()
    await state.set_state(FoodSG.catalog)
    await msg(query).answer(tr("food.catalog_ask"), reply_markup=cancel_kb(tr))


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
    await message.answer(
        tr("food.catalog_saved", name=food.name, kcal=num(tr, food.energy_kcal)),
        reply_markup=inline([(tr("food.log_it"), Fm(a="fav", id=food.id))]),
    )


@router.callback_query(Fm.filter(F.a == "recipe"))
async def recipe_start(query: CallbackQuery, tr: Translator, state: FSMContext) -> None:
    await query.answer()
    await state.set_state(FoodSG.recipe_name)
    await msg(query).answer(tr("food.recipe_name_ask"), reply_markup=cancel_kb(tr))


@router.message(FoodSG.recipe_name, F.text)
async def recipe_name(message: Message, tr: Translator, state: FSMContext) -> None:
    assert message.text is not None
    name = " ".join(message.text.split())
    if not name or len(name) > 80:
        raise ServiceError("bad_name")
    await state.update_data(recipe_name=name)
    await state.set_state(FoodSG.recipe_ingredients)
    await message.answer(tr("food.recipe_ingredients_ask"), reply_markup=cancel_kb(tr))


@router.message(FoodSG.recipe_ingredients, F.text)
async def recipe_ingredients(message: Message, tr: Translator, state: FSMContext) -> None:
    assert message.text is not None
    if len(message.text) > 1500:
        raise ServiceError("too_long")
    await state.update_data(ingredients=message.text)
    await state.set_state(FoodSG.recipe_yield)
    await message.answer(
        tr("food.recipe_yield_ask"),
        reply_markup=inline(
            [(tr("btn.skip"), Fm(a="yield_skip"))], [(tr("btn.cancel"), Fd(action="cancel"))]
        ),
    )


async def _save_recipe(
    message: Message,
    session: AsyncSession,
    user: User,
    tr: Translator,
    svc: FoodService,
    state: FSMContext,
    cooked: Decimal | None,
) -> None:
    data = await state.get_data()
    recipe = await svc.create_recipe(data["recipe_name"], data["ingredients"], cooked)
    await session.commit()
    await state.clear()
    _, items = recipe.name, recipe.items
    unknown = sum(1 for i in items if i.get("energy_kcal") is None)
    text = tr("food.recipe_saved", name=recipe.name, n=len(items))
    if unknown:
        text += "\n" + tr("food.recipe_unknown", n=unknown)
    await message.answer(text, reply_markup=main_menu(tr))


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
    await _save_recipe(
        message,
        session,
        user,
        tr,
        service(session, user, gateway, settings, food_sources),
        state,
        cooked,
    )


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
    await query.answer()
    await _save_recipe(
        msg(query),
        session,
        user,
        tr,
        service(session, user, gateway, settings, food_sources),
        state,
        None,
    )


# --- copy a previous meal -------------------------------------------------------------------


@router.callback_query(Fm.filter(F.a == "copy"))
async def copy_menu(query: CallbackQuery, tr: Translator) -> None:
    await query.answer()
    rows = []
    for day_key, offset in (("day.yesterday", "1"), ("day.today_short", "0")):
        rows.append(
            [
                (f"{tr(day_key)}: {tr('meal.' + m)}", Fm(a="cp", x=f"{offset}-{m}"))
                for m in MEAL_ORDER[:2]
            ]
        )
        rows.append(
            [
                (f"{tr(day_key)}: {tr('meal.' + m)}", Fm(a="cp", x=f"{offset}-{m}"))
                for m in MEAL_ORDER[2:]
            ]
        )
    rows.append([(tr("food.copy_all_yesterday"), Fm(a="cp", x="1-"))])
    await msg(query).answer(tr("food.copy_ask"), reply_markup=inline(*rows))


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
) -> None:
    offset_text, _, meal = callback_data.x.partition("-")
    if offset_text not in ("0", "1") or (meal and meal not in MEAL_ORDER):
        raise ServiceError("bad_choice")
    day = local_today(user) - dt.timedelta(days=int(offset_text))
    svc = service(session, user, gateway, settings, food_sources)
    draft = await svc.draft_copy(day, MealType(meal) if meal else None)
    await session.commit()
    await query.answer()
    await show_draft(msg(query), svc, draft, tr)
