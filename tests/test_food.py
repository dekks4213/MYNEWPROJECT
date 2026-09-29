"""Nutrition pipeline: arithmetic, drafts, editing, confirmation, isolation."""

from __future__ import annotations

import asyncio
import datetime as dt
import io
from decimal import Decimal

import httpx
import pytest
from PIL import Image
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from fitcoach.ai.gateway import AIGateway
from fitcoach.ai.types import CopyRequest, FoodItemAI, FoodParse
from fitcoach.config import Settings
from fitcoach.db.models import Draft, FoodEntry
from fitcoach.domain.food import (
    Basis,
    FixedTotals,
    MealType,
    Nutrients,
    NutrientSource,
    Per100,
    Unit,
    recipe_portion,
    scale,
)
from fitcoach.domain.nutrition import Precision
from fitcoach.domain.units import ParseError
from fitcoach.services.errors import Conflict, NotFound, ServiceError
from fitcoach.services.food import DraftItem, FoodDraftState, FoodService, compute
from fitcoach.services.food_sources import OpenFoodFacts, UsdaFoodData
from fitcoach.services.media import check_voice, prepare_image
from fitcoach.services.users import local_today
from tests.conftest import make_user, open_user, requires_db
from tests.fakes import ScriptedProvider

D = Decimal
OATS = Per100(energy_kcal=D(370), protein_g=D(13), fat_g=D(7), carbs_g=D(60))


# --- pure arithmetic --------------------------------------------------------------------


def test_scale_per_100g_and_basis_mismatch() -> None:
    n = scale(OATS, Basis.PER_100G, grams=D(150))
    assert n == Nutrients(D("555.0"), D("19.5"), D("10.5"), D("90.0"), None)
    assert scale(OATS, Basis.PER_100ML, grams=D(100)) is None
    assert scale(OATS, Basis.SERVING, servings=D(2)).energy_kcal == D(740)


def test_compute_marks_estimates_and_unknowns() -> None:
    exact = compute(
        DraftItem(
            name="овсянка",
            amount=D(100),
            unit=Unit.G,
            per=OATS,
            basis=Basis.PER_100G,
            nutrient_source=NutrientSource.LABEL,
        )
    )
    assert (exact.energy_kcal, exact.precision) == (D("370.0"), Precision.MEASURED)

    density = compute(
        DraftItem(
            name="кефир",
            amount=D(250),
            unit=Unit.ML,
            per=OATS,
            basis=Basis.PER_100G,
            nutrient_source=NutrientSource.LABEL,
        )
    )
    assert density.quantity_estimated and density.precision is Precision.APPROXIMATE

    pieces = compute(
        DraftItem(
            name="яйцо",
            amount=D(3),
            unit=Unit.PIECE,
            serving_g=D(55),
            per=OATS,
            basis=Basis.PER_100G,
            nutrient_source=NutrientSource.LABEL,
        )
    )
    assert pieces.grams == D(165) and pieces.precision is Precision.MEASURED

    cup = compute(
        DraftItem(
            name="рис",
            amount=D(1),
            unit=Unit.CUP,
            grams_estimate=D(160),
            per=OATS,
            basis=Basis.PER_100G,
            nutrient_source=NutrientSource.AI_ESTIMATE,
        )
    )
    assert cup.quantity_estimated and cup.precision is Precision.APPROXIMATE

    unknown = compute(DraftItem(name="банан"))
    assert unknown.energy_kcal is None and unknown.precision is Precision.UNKNOWN
    no_amount = compute(
        DraftItem(
            name="банан", per=OATS, basis=Basis.PER_100G, nutrient_source=NutrientSource.LABEL
        )
    )
    assert no_amount.energy_kcal is None  # never assume a portion


def test_unknown_macros_stay_unknown_in_totals() -> None:
    state = FoodDraftState(
        origin="text",
        meal_type=MealType.LUNCH,
        items=[
            compute(
                DraftItem(
                    name="a",
                    fixed=FixedTotals(energy_kcal=D(100)),
                    nutrient_source=NutrientSource.USER,
                )
            ),
            compute(
                DraftItem(
                    name="b",
                    amount=D(50),
                    unit=Unit.G,
                    per=OATS,
                    basis=Basis.PER_100G,
                    nutrient_source=NutrientSource.LABEL,
                )
            ),
        ],
    )
    t = state.totals()
    assert t.energy_kcal == D("285.0") and t.protein_g == D("6.5")


def test_recipe_portion() -> None:
    total = Nutrients(D(2000), D(100), None, D(200))
    portion, fraction = recipe_portion(total, cooked_yield_g=D(1000), consumed_g=D(250))
    assert fraction == D("0.25") and portion.energy_kcal == D("500.0") and portion.fat_g is None
    with pytest.raises(ParseError):
        recipe_portion(total, cooked_yield_g=None, consumed_g=D(100))


# --- service with database ----------------------------------------------------------------


def _gateway(**responses: object) -> AIGateway:
    return AIGateway(ScriptedProvider(**responses), Settings(ai_provider="mock"))


async def _consenting_user(sm: async_sessionmaker[AsyncSession]) -> int:
    tid = await make_user(sm)
    async with sm() as s:
        user = await open_user(s, tid)
        user.ai_text_consent_at = user.ai_media_consent_at = dt.datetime.now(dt.UTC)
        await s.commit()
    return tid


@pytest.mark.usefixtures("database")
@requires_db
class TestFoodService:
    async def test_manual_text_uses_catalog_and_keeps_unknowns(
        self, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        tid = await make_user(sessionmaker)
        async with sessionmaker() as s:
            user = await open_user(s, tid)
            svc = FoodService(s, user)
            await svc.add_catalog_food("Овсянка; 370; 13/7/60")
            await svc.add_catalog_food("Молоко 2,5%; 52; 2,8/2,5/4,7")
            with pytest.raises(ServiceError):
                await svc.add_catalog_food("Масло; 1200")  # implausible per 100 g
            await svc.add_catalog_food("Молоко; 52")
            draft = await svc.draft_from_text("овсянка 100 г, молоко 250 мл, банан")
            _, state = await svc.get_draft(draft.id)
            names = [i.name for i in state.items]
            assert names == ["овсянка", "молоко", "банан"]
            oats, milk, banana = state.items
            assert oats.energy_kcal == D("370.0") and oats.precision is Precision.MEASURED
            assert milk.quantity_estimated  # ml on a per-100 g record
            assert banana.energy_kcal is None and banana.precision is Precision.UNKNOWN
            entries = await svc.confirm(draft.id, draft.version)
            assert [e.meal_type for e in entries] == [state.meal_type.value] * 3
            assert entries[2].energy_kcal is None
            await s.commit()

    async def test_ai_estimates_are_labelled_and_brands_never_estimated(
        self, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        tid = await _consenting_user(sessionmaker)
        parse = FoodParse(
            items=[
                FoodItemAI(
                    name="курица",
                    amount=D(200),
                    unit=Unit.G,
                    preparation="варёная",
                    reference_per_100g=Per100(energy_kcal=D(165), protein_g=D(31)),
                ),
                FoodItemAI(
                    name="рис",
                    amount=D(1),
                    unit=Unit.CUP,
                    grams_estimate=D(160),
                    uncertain=True,
                    reference_per_100g=Per100(energy_kcal=D(130)),
                ),
                FoodItemAI(
                    name="протеин",
                    brand="Optimum",
                    amount=D(30),
                    unit=Unit.G,
                    reference_per_100g=Per100(energy_kcal=D(400)),
                ),
                FoodItemAI(name="сок", stated_energy_kcal=D(120)),
                FoodItemAI(
                    name="бред",
                    amount=D(100),
                    unit=Unit.G,
                    reference_per_100g=Per100(energy_kcal=D(900), protein_g=D(90), carbs_g=D(90)),
                ),
            ]
        )
        async with sessionmaker() as s:
            user = await open_user(s, tid)
            svc = FoodService(s, user, gateway=_gateway(food=parse))
            draft = await svc.draft_from_text("200 грамм курицы, риса стакан, протеин, сок")
            _, state = await svc.get_draft(draft.id)
            chicken, rice, protein, juice, junk = state.items
            assert chicken.nutrient_source is NutrientSource.AI_ESTIMATE
            assert chicken.energy_kcal == D("330.0")
            assert chicken.precision is Precision.APPROXIMATE
            assert rice.quantity_estimated and rice.energy_kcal == D("208.0")
            assert protein.energy_kcal is None  # branded: no model estimate
            assert juice.energy_kcal == D(120) and juice.nutrient_source is NutrientSource.USER
            assert junk.energy_kcal is None  # physically implausible values rejected
            await s.commit()

    async def test_edit_remove_add_and_stale_versions(
        self, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        tid = await make_user(sessionmaker)
        async with sessionmaker() as s:
            user = await open_user(s, tid)
            svc = FoodService(s, user)
            await svc.add_catalog_food("Рис; 130; 2,7/0,3/28")
            draft = await svc.draft_from_text("рис 100 г, банан")
            v1 = draft.version
            draft = await svc.edit_item(draft.id, v1, 0, "250 г")
            _, state = await svc.get_draft(draft.id)
            assert state.items[0].energy_kcal == D("325.0")
            with pytest.raises(Conflict):
                await svc.edit_item(draft.id, v1, 0, "50 г")  # stale button
            draft = await svc.edit_item(draft.id, draft.version, 1, "105 ккал")
            draft = await svc.add_items(draft.id, draft.version, "кофе 200 мл")
            draft = await svc.remove_item(draft.id, draft.version, 2)
            draft = await svc.set_meal_type(draft.id, draft.version, MealType.SNACK)
            _, state = await svc.get_draft(draft.id)
            assert [i.name for i in state.items] == ["рис", "банан"]
            assert state.items[1].energy_kcal == D(105)
            assert state.totals().energy_kcal == D("430.0")
            with pytest.raises(ServiceError):
                await svc.edit_item(draft.id, draft.version, 0, "много")
            with pytest.raises(NotFound):
                await svc.edit_item(draft.id, draft.version, 9, "10 г")
            entries = await svc.confirm(draft.id, draft.version)
            assert {e.meal_type for e in entries} == {"snack"}
            with pytest.raises(Conflict):
                await svc.confirm(draft.id, draft.version)
            with pytest.raises(Conflict):
                await svc.remove_item(draft.id, draft.version, 0)
            await s.commit()

    async def test_concurrent_confirmations_save_once(
        self, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        tid = await make_user(sessionmaker)
        async with sessionmaker() as s:
            user = await open_user(s, tid)
            draft = await FoodService(s, user).draft_from_text("суп 300 г")
            draft_id, version = draft.id, draft.version
            await s.commit()

        async def confirm() -> bool:
            async with sessionmaker() as s:
                user = await open_user(s, tid)
                try:
                    await FoodService(s, user).confirm(draft_id, version)
                    await asyncio.sleep(0.05)
                    await s.commit()
                    return True
                except Conflict:
                    await s.rollback()
                    return False

        assert sorted(await asyncio.gather(confirm(), confirm(), confirm())) == [False, False, True]
        async with sessionmaker() as s:
            await open_user(s, tid)
            count = (
                (await s.execute(select(FoodEntry).where(FoodEntry.draft_id == draft_id)))
                .scalars()
                .all()
            )
            assert len(count) == 1

    async def test_copy_yesterday_breakfast_without_yogurt(
        self, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        tid = await _consenting_user(sessionmaker)
        async with sessionmaker() as s:
            user = await open_user(s, tid)
            svc = FoodService(s, user)
            await svc.add_catalog_food("Овсянка; 370; 13/7/60")
            yesterday = dt.datetime.now(dt.UTC) - dt.timedelta(days=1)
            for line in ("овсянка 80 г", "йогурт 150 г", "кофе"):
                d = await svc.draft_from_text(line)
                await svc.set_meal_type(d.id, d.version, MealType.BREAKFAST)
                d = (await svc.get_draft(d.id))[0]
                await svc.confirm(d.id, d.version, now=yesterday)
            parse = FoodParse(
                copy_request=CopyRequest(
                    day_offset=-1, meal_type=MealType.BREAKFAST, exclude=["йогурт"]
                )
            )
            svc_ai = FoodService(s, user, gateway=_gateway(food=parse))
            draft = await svc_ai.draft_from_text("такой же завтрак как вчера, только без йогурта")
            _, state = await svc_ai.get_draft(draft.id)
            assert [i.name for i in state.items] == ["овсянка (80 г)", "кофе"]
            assert state.meal_type is MealType.BREAKFAST
            assert state.items[0].energy_kcal == D("296.0")
            # Editable: copied item was re-derived to per-100 g.
            draft = await svc_ai.edit_item(draft.id, draft.version, 0, "100 г")
            assert (await svc_ai.get_draft(draft.id))[1].items[0].energy_kcal == D("370.0")
            with pytest.raises(ServiceError):
                await svc.draft_copy(local_today(user) - dt.timedelta(days=5), None)
            await s.commit()

    async def test_saved_meals_recipes_and_favorites(
        self, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        tid = await make_user(sessionmaker)
        async with sessionmaker() as s:
            user = await open_user(s, tid)
            svc = FoodService(s, user)
            await svc.add_catalog_food("Гречка; 340; 13/3/68")
            await svc.add_catalog_food("Курица; 110; 23/2/0")
            d = await svc.draft_from_text("гречка 80 г, курица 150 г")
            meal = await svc.save_meal_from_draft(d.id, "Мой обычный обед")
            again = await svc.draft_from_saved(meal.id)
            _, state = await svc.get_draft(again.id)
            assert state.totals().energy_kcal == D("437.0")

            recipe = await svc.create_recipe("Плов", "гречка 400 г, курица 600 г", D(2000))
            portion = await svc.draft_from_saved(recipe.id, grams=D(500))
            _, pstate = await svc.get_draft(portion.id)
            assert pstate.items[0].precision is Precision.RECIPE
            assert pstate.items[0].energy_kcal == D("505.0")  # (1360+660) * 500/2000
            half = await svc.draft_from_saved(recipe.id, fraction=D("0.5"))
            assert (await svc.get_draft(half.id))[1].items[0].energy_kcal == D("1010.0")

            foods = await svc.list_foods(favorites_only=True)
            assert {f.name for f in foods} == {"Гречка", "Курица"}
            toggled = await svc.toggle_favorite_food(foods[0].id)
            assert toggled.favorite is False
            fav = await svc.draft_from_catalog(foods[1].id, "200")
            assert (await svc.get_draft(fav.id))[1].items[0].energy_kcal is not None
            await s.commit()

    async def test_other_users_drafts_meals_and_foods_are_invisible(
        self, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        a, b = await make_user(sessionmaker), await make_user(sessionmaker)
        async with sessionmaker() as s:
            ua = await open_user(s, a)
            sa = FoodService(s, ua)
            food = await sa.add_catalog_food("Секретный продукт; 100")
            d = await sa.draft_from_text("секретный продукт 100 г")
            meal = await sa.save_meal_from_draft(d.id, "Секрет")
            await s.commit()
        async with sessionmaker() as s:
            ub = await open_user(s, b)
            sb = FoodService(s, ub)
            for call in (
                lambda: sb.get_draft(d.id),
                lambda: sb.confirm(d.id, d.version),
                lambda: sb.cancel(d.id),
                lambda: sb.edit_item(d.id, d.version, 0, "1 г"),
                lambda: sb.get_meal(meal.id),
                lambda: sb.draft_from_saved(meal.id),
                lambda: sb.get_food(food.id),
                lambda: sb.draft_from_catalog(food.id, "10"),
            ):
                with pytest.raises(NotFound):
                    await call()
            # A name match never reaches into another user's catalog.
            own = await sb.draft_from_text("секретный продукт 100 г")
            assert (await sb.get_draft(own.id))[1].items[0].energy_kcal is None

            # Tampered payload pointing at A's food id must not be linked on confirm.
            _, state = await sb.get_draft(own.id)
            state.items[0] = state.items[0].model_copy(update={"food_id": food.id})
            await s.execute(
                update(Draft)
                .where(Draft.id == own.id)
                .values(payload=state.model_dump(mode="json"))
            )
            entries = await sb.confirm(own.id, own.version)
            assert entries[0].food_id is None
            await s.commit()

    async def test_photo_and_voice_need_media_consent_and_budget(
        self, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        tid = await make_user(sessionmaker)
        parse = FoodParse(
            items=[FoodItemAI(name="паста", uncertain=True, missing=["amount"])],
            clarification="Какой был размер порции?",
        )
        async with sessionmaker() as s:
            user = await open_user(s, tid)
            user.ai_text_consent_at = dt.datetime.now(dt.UTC)
            provider = ScriptedProvider(image=parse, voice=parse)
            gw = AIGateway(provider, Settings(ai_provider="mock", ai_user_daily_media_calls=1))
            svc = FoodService(s, user, gateway=gw)
            from fitcoach.ai.types import AIUnavailableError

            with pytest.raises(AIUnavailableError, match="no_media_consent"):
                await svc.draft_from_photo(b"img", "image/jpeg", None)
            user.ai_media_consent_at = dt.datetime.now(dt.UTC)
            draft = await svc.draft_from_photo(b"img", "image/jpeg", "обед")
            _, state = await svc.get_draft(draft.id)
            assert state.clarification and state.items[0].uncertain
            assert state.items[0].energy_kcal is None
            with pytest.raises(AIUnavailableError, match="budget_exhausted"):
                await svc.draft_from_voice(b"OggS", "audio/ogg")
            media = (
                await s.execute(
                    text("SELECT media, input_tokens FROM ai_calls WHERE owner_id = :o"),
                    {"o": user.id},
                )
            ).all()
            assert media == [("image", 300)]
            await s.commit()


# --- external food sources (contract tests, no network) -----------------------------------


async def test_open_food_facts_adapter_parses_and_validates() -> None:
    payload = {
        "products": [
            {
                "code": "3017620422003",
                "product_name": "Nutella",
                "brands": "Ferrero, x",
                "nutriments": {
                    "energy-kcal_100g": 539,
                    "proteins_100g": 6.3,
                    "fat_100g": 30.9,
                    "carbohydrates_100g": 57.5,
                },
            },
            {"code": "bad-code", "product_name": "X", "nutriments": {"energy-kcal_100g": 10}},
            {"code": "123", "product_name": "No energy", "nutriments": {}},
            {"code": "124", "product_name": "Nonsense", "nutriments": {"energy-kcal_100g": 5000}},
        ]
    }
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=payload)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    found = await OpenFoodFacts(client).search("nutella", brand="Ferrero")
    assert len(found) == 1
    c = found[0]
    assert (c.source, c.source_id, c.brand, c.per.energy_kcal) == (
        "off",
        "3017620422003",
        "Ferrero",
        D(539),
    )
    assert seen[0].url.host == "world.openfoodfacts.org"
    assert "RITM" in seen[0].headers["user-agent"]


async def test_usda_adapter_parses_and_fails_softly() -> None:
    payload = {
        "foods": [
            {
                "fdcId": 171705,
                "description": "Rice, white, cooked",
                "foodNutrients": [
                    {"nutrientNumber": "208", "unitName": "KCAL", "value": 130},
                    {"nutrientNumber": "203", "unitName": "G", "value": 2.69},
                    {"nutrientNumber": "204", "unitName": "G", "value": 0.28},
                ],
            }
        ]
    }
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=payload))
    )
    found = await UsdaFoodData(client, "key").search("rice cooked")
    assert found[0].per.energy_kcal == D(130) and found[0].per.carbs_g is None
    assert await UsdaFoodData(client, "key").search("rice", brand="Uncle") == []

    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    down = httpx.AsyncClient(transport=httpx.MockTransport(boom))
    assert await UsdaFoodData(down, "key").search("rice") == []
    garbage = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, content=b"<html>"))
    )
    assert await OpenFoodFacts(garbage).search("x") == []


# --- media ----------------------------------------------------------------------------------


def _jpeg_with_exif() -> bytes:
    img = Image.new("RGB", (3000, 2000), (200, 100, 50))
    exif = Image.Exif()
    exif[0x010F] = "SecretCamera"  # Make
    buf = io.BytesIO()
    img.save(buf, format="JPEG", exif=exif.tobytes())
    return buf.getvalue()


def test_prepare_image_strips_metadata_and_downscales() -> None:
    raw = _jpeg_with_exif()
    assert b"SecretCamera" in raw
    out, mime = prepare_image(raw, 10_000_000)
    assert mime == "image/jpeg" and b"SecretCamera" not in out
    with Image.open(io.BytesIO(out)) as img:
        assert max(img.size) == 1280 and not img.getexif()


@pytest.mark.parametrize(
    "data", [b"", b"not an image", b"\xff\xd8\xff" + b"\x00" * 100, b"GIF89a....", b"%PDF-1.4"]
)
def test_prepare_image_rejects_malformed(data: bytes) -> None:
    with pytest.raises(ServiceError, match="bad_media"):
        prepare_image(data, 10_000_000)


def test_media_size_limits() -> None:
    with pytest.raises(ServiceError, match="media_too_large"):
        prepare_image(_jpeg_with_exif(), 1000)
    assert (
        check_voice(b"OggS" + b"0" * 10, duration_s=5, max_bytes=100, max_seconds=60) == "audio/ogg"
    )
    with pytest.raises(ServiceError, match="media_too_large"):
        check_voice(b"OggS", duration_s=500, max_bytes=100, max_seconds=60)
    with pytest.raises(ServiceError, match="bad_media"):
        check_voice(b"ID3xxxx", duration_s=5, max_bytes=100, max_seconds=60)
