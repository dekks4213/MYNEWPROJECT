"""Food logging pipeline.

input (text / photo / voice / copy / saved meal / catalog food)
  -> FoodParse (AI or deterministic parser)
  -> resolution against the user's catalog, optional external sources, labelled AI estimates
  -> deterministic arithmetic (domain.food)
  -> editable draft (drafts table, optimistic version)
  -> explicit confirmation -> food_entries

Nothing here trusts IDs coming from the model or from the client: catalog/meal IDs are
looked up with the owner filter (and RLS) before use.
"""

from __future__ import annotations

import datetime as dt
import re
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from fitcoach.ai.gateway import AIGateway
from fitcoach.ai.types import CopyRequest, FoodItemAI, FoodParse
from fitcoach.db.models import Draft, Food, FoodEntry, SavedMeal, User
from fitcoach.domain.food import (
    MAX_GRAMS,
    MAX_ITEMS,
    Basis,
    FixedTotals,
    MealType,
    Nutrients,
    NutrientSource,
    Per100,
    Unit,
    default_meal_type,
    detect_meal_type,
    normalize_name,
    parse_amount,
    parse_food_line,
    recipe_portion,
    scale,
    sum_nutrients,
    to_grams,
    to_ml,
    validate_per100,
)
from fitcoach.domain.nutrition import Precision, parse_kcal, parse_macros
from fitcoach.domain.units import ParseError, parse_decimal
from fitcoach.services.diary import DiaryService
from fitcoach.services.errors import Conflict, NotFound, ServiceError
from fitcoach.services.food_sources import FoodCandidate, FoodSource
from fitcoach.services.users import user_zone, utcnow

MAX_CATALOG = 1000
MAX_MEALS = 300


class DraftItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=120)
    name_en: str | None = None
    brand: str | None = None
    amount: Decimal | None = None
    unit: Unit | None = None
    amount_text: str | None = None
    grams: Decimal | None = None
    ml: Decimal | None = None
    quantity_estimated: bool = False
    grams_estimate: Decimal | None = None
    preparation: str | None = None
    uncertain: bool = False
    # Nutrient provenance and basis
    nutrient_source: NutrientSource | None = None
    source_id: str | None = None
    source_label: str | None = None  # e.g. matched database name, shown to the user
    basis: Basis | None = None
    per: Per100 | None = None
    serving_g: Decimal | None = None
    food_id: int | None = None
    fixed: FixedTotals | None = None  # totals for items without a per-basis (stated, copied)
    # Computed (deterministic) totals for this item
    energy_kcal: Decimal | None = None
    protein_g: Decimal | None = None
    fat_g: Decimal | None = None
    carbs_g: Decimal | None = None
    fiber_g: Decimal | None = None
    precision: Precision = Precision.UNKNOWN

    def nutrients(self) -> Nutrients:
        return Nutrients(self.energy_kcal, self.protein_g, self.fat_g, self.carbs_g, self.fiber_g)


class FoodDraftState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    origin: Literal["text", "photo", "voice", "copy", "saved", "catalog", "recipe"]
    meal_type: MealType
    items: list[DraftItem] = Field(default_factory=list, max_length=MAX_ITEMS)
    clarification: str | None = None
    transcript: str | None = None
    ai: bool = False
    mock: bool = False

    def totals(self) -> Nutrients:
        return sum_nutrients([i.nutrients() for i in self.items])


# --- deterministic computation ------------------------------------------------------


def compute(item: DraftItem) -> DraftItem:
    """Fill quantities and totals from amount/unit + basis. Pure function."""
    data = item.model_dump()
    grams = to_grams(item.amount, item.unit)
    ml = to_ml(item.amount, item.unit)
    estimated = False
    if grams is None and ml is None and item.amount is not None:
        if item.unit in (Unit.PIECE, Unit.SERVING, Unit.SLICE) and item.serving_g:
            grams = item.amount * item.serving_g  # label/catalog serving size
        elif item.grams_estimate is not None:
            grams, estimated = item.grams_estimate, True
    elif grams is None and ml is None and item.grams_estimate is not None:
        grams, estimated = item.grams_estimate, True
    if grams is not None and grams > MAX_GRAMS:
        raise ServiceError("amount_too_large")

    nutrients: Nutrients | None = None
    approximate_basis = False
    if item.per is not None and item.basis is not None:
        if item.basis is Basis.PER_100G and grams is None and ml is not None:
            grams, approximate_basis = ml, True  # density ~1 assumption, shown as estimate
        elif item.basis is Basis.PER_100ML and ml is None and grams is not None:
            ml, approximate_basis = grams, True
        servings = item.amount if item.unit in (Unit.SERVING, Unit.PIECE) else None
        nutrients = scale(item.per, item.basis, grams=grams, ml=ml, servings=servings)
    elif item.fixed is not None:
        f = item.fixed
        nutrients = Nutrients(f.energy_kcal, f.protein_g, f.fat_g, f.carbs_g, f.fiber_g)

    if nutrients is None or all(
        v is None
        for v in (nutrients.energy_kcal, nutrients.protein_g, nutrients.fat_g, nutrients.carbs_g)
    ):
        precision = Precision.UNKNOWN
        nutrients = Nutrients()
    elif item.nutrient_source is NutrientSource.RECIPE:
        precision = Precision.RECIPE
    elif (
        item.nutrient_source in (NutrientSource.LABEL, NutrientSource.USDA, NutrientSource.OFF)
        and not estimated
        and not approximate_basis
    ):
        precision = Precision.MEASURED
    else:
        precision = Precision.APPROXIMATE
    data.update(
        grams=grams,
        ml=ml,
        quantity_estimated=estimated or approximate_basis,
        energy_kcal=nutrients.energy_kcal,
        protein_g=nutrients.protein_g,
        fat_g=nutrients.fat_g,
        carbs_g=nutrients.carbs_g,
        fiber_g=nutrients.fiber_g,
        precision=precision,
    )
    return DraftItem.model_validate(data)


_KCAL_EDIT = re.compile(r"^\s*(\d+(?:[.,]\d+)?)\s*(ккал|kcal|кал|cal)\s*$", re.IGNORECASE)


class FoodService:
    def __init__(
        self,
        session: AsyncSession,
        user: User,
        *,
        gateway: AIGateway | None = None,
        sources: list[FoodSource] | None = None,
        ai_estimates: bool = True,
    ) -> None:
        self.session = session
        self.user = user
        self.gateway = gateway
        self.sources = sources or []
        self.ai_estimates = ai_estimates

    # --- drafts: creation ------------------------------------------------------------

    def _meal_type(self, now: dt.datetime | None = None) -> MealType:
        local = (now or utcnow()).astimezone(user_zone(self.user))
        return default_meal_type(local.hour)

    async def draft_from_text(self, text: str, *, use_ai: bool = True) -> Draft:
        text = text.strip()
        if not text or len(text) > 1500:
            raise ServiceError("bad_food_text")
        if use_ai and self.gateway is not None and self.gateway.text_available(self.user):
            parse = await self.gateway.parse_food_text(self.session, self.user, text)
            return await self.draft_from_parse(parse, "text", ai=True, user_text=text)
        items = [
            FoodItemAI(
                name=p.name,
                amount=p.amount,
                unit=p.unit,
                amount_text=p.amount_text,
                missing=[] if p.amount is not None else ["amount"],
            )
            for p in parse_food_line(text)
        ]
        if not items:
            raise ServiceError("bad_food_text")
        return await self.draft_from_parse(FoodParse(items=items), "text", ai=False, user_text=text)

    async def draft_from_photo(self, image: bytes, mime: str, caption: str | None) -> Draft:
        if self.gateway is None:
            raise ServiceError("ai_required")
        parse = await self.gateway.analyze_food_image(self.session, self.user, image, mime, caption)
        return await self.draft_from_parse(parse, "photo", ai=True, user_text=caption)

    async def draft_from_voice(self, audio: bytes, mime: str) -> Draft:
        if self.gateway is None:
            raise ServiceError("ai_required")
        parse = await self.gateway.parse_food_voice(self.session, self.user, audio, mime)
        return await self.draft_from_parse(parse, "voice", ai=True)

    async def draft_from_parse(
        self,
        parse: FoodParse,
        origin: Literal["text", "photo", "voice"],
        *,
        ai: bool,
        user_text: str | None = None,
    ) -> Draft:
        items: list[DraftItem] = []
        if parse.copy_request is not None:
            items.extend(await self._copied_items(parse.copy_request))
        for ai_item in parse.items[: MAX_ITEMS - len(items)]:
            items.append(compute(await self._resolve(ai_item, allow_estimate=ai)))
        if not items and not parse.clarification:
            raise ServiceError("nothing_recognized")
        # Meal type: named by the user (text, caption or transcript), or the copied meal's,
        # otherwise derived from local time. The model's own guess is not used.
        meal_type = (
            detect_meal_type(user_text or parse.transcript)
            or (parse.copy_request.meal_type if parse.copy_request else None)
            or self._meal_type()
        )
        state = FoodDraftState(
            origin=origin,
            meal_type=meal_type,
            items=items,
            clarification=parse.clarification,
            transcript=parse.transcript,
            ai=ai,
            mock=bool(self.gateway and self.gateway.is_mock and ai),
        )
        return await self._store(state)

    async def draft_copy(
        self, day: dt.date, meal_type: MealType | None, exclude: list[str] | None = None
    ) -> Draft:
        req = CopyRequest(day_offset=0, meal_type=meal_type, exclude=exclude or [])
        items = await self._copied_items(req, day=day)
        if not items:
            raise ServiceError("nothing_to_copy")
        return await self._store(
            FoodDraftState(origin="copy", meal_type=meal_type or self._meal_type(), items=items)
        )

    async def draft_from_saved(
        self, meal_id: int, *, grams: Decimal | None = None, fraction: Decimal | None = None
    ) -> Draft:
        meal = await self.get_meal(meal_id)
        stored = [DraftItem.model_validate(i) for i in meal.items]
        if meal.kind == "recipe":
            total = sum_nutrients([i.nutrients() for i in stored])
            try:
                portion, frac = recipe_portion(
                    total, cooked_yield_g=meal.cooked_yield_g, consumed_g=grams, fraction=fraction
                )
            except ParseError as exc:
                raise ServiceError(exc.code) from exc
            item = compute(
                DraftItem(
                    name=meal.name,
                    amount=grams,
                    unit=Unit.G if grams is not None else None,
                    amount_text=None if grams is not None else f"× {format(frac.normalize(), 'f')}",
                    nutrient_source=NutrientSource.RECIPE,
                    source_label=meal.name,
                    fixed=FixedTotals(
                        energy_kcal=portion.energy_kcal,
                        protein_g=portion.protein_g,
                        fat_g=portion.fat_g,
                        carbs_g=portion.carbs_g,
                        fiber_g=portion.fiber_g,
                    ),
                )
            )
            items = [item]
        else:
            items = [compute(i) for i in stored]
        return await self._store(
            FoodDraftState(
                origin="saved" if meal.kind == "meal" else "recipe",
                meal_type=self._meal_type(),
                items=items,
            )
        )

    async def draft_from_catalog(self, food_id: int, amount_text: str) -> Draft:
        food = await self.get_food(food_id)
        parsed = parse_amount(amount_text)
        if parsed is None:
            raise ServiceError("bad_amount")
        item = compute(
            self._item_from_food(
                food,
                DraftItem(
                    name=food.name,
                    amount=parsed[0],
                    unit=parsed[1],
                    amount_text=amount_text.strip(),
                ),
            )
        )
        return await self._store(
            FoodDraftState(origin="catalog", meal_type=self._meal_type(), items=[item])
        )

    # --- resolution ----------------------------------------------------------------

    def _item_from_food(self, food: Food, base: DraftItem) -> DraftItem:
        per = Per100(
            energy_kcal=food.energy_kcal,
            protein_g=food.protein_g,
            fat_g=food.fat_g,
            carbs_g=food.carbs_g,
            fiber_g=food.fiber_g,
        )
        source = (
            NutrientSource(food.source)
            if food.source in ("usda", "off")
            else (NutrientSource.LABEL)
        )
        return base.model_copy(
            update={
                "per": per,
                "basis": Basis(food.basis),
                "serving_g": food.serving_g,
                "food_id": food.id,
                "nutrient_source": source,
                "source_id": food.source_id,
                "source_label": food.name + (f" ({food.brand})" if food.brand else ""),
                "brand": base.brand or food.brand,
            }
        )

    async def _catalog_match(self, name: str, brand: str | None) -> Food | None:
        key = normalize_name(name)
        foods = (
            (
                await self.session.execute(
                    select(Food)
                    .where(Food.owner_id == self.user.id, Food.archived_at.is_(None))
                    .order_by(Food.favorite.desc(), Food.id.desc())
                    .limit(MAX_CATALOG)
                )
            )
            .scalars()
            .all()
        )
        for food in foods:
            if normalize_name(food.name) == key and (
                brand is None or (food.brand or "").lower() == brand.lower()
            ):
                return food
        return None

    async def _external(self, ai_item: FoodItemAI) -> FoodCandidate | None:
        for source in self.sources:
            if ai_item.brand and source.name == "off":
                found = await source.search(ai_item.name, brand=ai_item.brand)
                brand = ai_item.brand.lower()
                found = [c for c in found if c.brand and brand in c.brand.lower()]
            elif not ai_item.brand and source.name == "usda" and ai_item.name_en:
                query = ai_item.name_en
                if ai_item.preparation and ai_item.preparation.lower() not in query.lower():
                    query = f"{query} {ai_item.preparation}"
                found = await source.search(query)
            else:
                continue
            if found:
                return found[0]
        return None

    async def _resolve(self, ai_item: FoodItemAI, *, allow_estimate: bool) -> DraftItem:
        base = DraftItem(
            name=ai_item.name,
            name_en=ai_item.name_en,
            brand=ai_item.brand,
            amount=ai_item.amount,
            unit=ai_item.unit,
            amount_text=ai_item.amount_text,
            grams_estimate=ai_item.grams_estimate,
            preparation=ai_item.preparation,
            uncertain=ai_item.uncertain or bool(ai_item.missing),
        )
        food = await self._catalog_match(ai_item.name, ai_item.brand)
        if food is not None:
            return self._item_from_food(food, base)
        candidate = await self._external(ai_item)
        if candidate is not None:
            return base.model_copy(
                update={
                    "per": candidate.per,
                    "basis": candidate.basis,
                    "serving_g": candidate.serving_g,
                    "nutrient_source": candidate.source,
                    "source_id": candidate.source_id,
                    "source_label": candidate.name
                    + (f" ({candidate.brand})" if candidate.brand else ""),
                }
            )
        if ai_item.stated_energy_kcal is not None:
            return base.model_copy(
                update={
                    "fixed": FixedTotals(energy_kcal=ai_item.stated_energy_kcal),
                    "nutrient_source": NutrientSource.USER,
                }
            )
        if (
            allow_estimate
            and self.ai_estimates
            and ai_item.reference_per_100g is not None
            and not ai_item.brand
            and not ai_item.reference_per_100g.is_empty()
        ):
            try:
                validate_per100(ai_item.reference_per_100g, Basis.PER_100G)
            except ParseError:
                return base
            return base.model_copy(
                update={
                    "per": ai_item.reference_per_100g,
                    "basis": Basis.PER_100G,
                    "nutrient_source": NutrientSource.AI_ESTIMATE,
                }
            )
        return base

    async def _copied_items(self, req: CopyRequest, day: dt.date | None = None) -> list[DraftItem]:
        from fitcoach.services.users import local_today

        target = day or (local_today(self.user) + dt.timedelta(days=req.day_offset))
        query = select(FoodEntry).where(
            FoodEntry.owner_id == self.user.id,
            FoodEntry.local_date == target,
            FoodEntry.deleted_at.is_(None),
        )
        if req.meal_type is not None:
            query = query.where(FoodEntry.meal_type == req.meal_type.value)
        entries = (await self.session.execute(query.order_by(FoodEntry.eaten_at))).scalars().all()
        excludes = [normalize_name(e) for e in req.exclude if e.strip()]
        items = []
        for entry in entries:
            key = normalize_name(entry.name)
            if any(x and (x in key or key in x) for x in excludes):
                continue
            items.append(compute(self._item_from_entry(entry)))
        return items[:MAX_ITEMS]

    def _item_from_entry(self, e: FoodEntry) -> DraftItem:
        source = NutrientSource(e.nutrient_source) if e.nutrient_source else NutrientSource.USER
        base = DraftItem(
            name=e.name,
            amount=e.amount,
            unit=Unit(e.unit) if e.unit else None,
            nutrient_source=source,
            food_id=e.food_id,
        )
        if e.grams and e.grams > 0:
            factor = Decimal(100) / e.grams

            def per(v: Decimal | None) -> Decimal | None:
                return None if v is None else (v * factor).quantize(Decimal("0.01"))

            try:
                # Re-derive per-100 g so the user can change the amount before saving.
                per100 = Per100(
                    energy_kcal=per(e.energy_kcal),
                    protein_g=per(e.protein_g),
                    fat_g=per(e.fat_g),
                    carbs_g=per(e.carbs_g),
                    fiber_g=per(e.fiber_g),
                )
                return base.model_copy(
                    update={
                        "amount": e.grams,
                        "unit": Unit.G,
                        "basis": Basis.PER_100G,
                        "per": per100,
                    }
                )
            except ValidationError:
                pass
        return base.model_copy(
            update={
                "grams": e.grams,
                "fixed": FixedTotals(
                    energy_kcal=e.energy_kcal,
                    protein_g=e.protein_g,
                    fat_g=e.fat_g,
                    carbs_g=e.carbs_g,
                    fiber_g=e.fiber_g,
                ),
            }
        )

    # --- drafts: storage and editing -------------------------------------------------

    async def _store(self, state: FoodDraftState) -> Draft:
        row = Draft(owner_id=self.user.id, kind="food", payload=state.model_dump(mode="json"))
        self.session.add(row)
        await self.session.flush()
        return row

    async def get_draft(self, draft_id: int) -> tuple[Draft, FoodDraftState]:
        row = (
            await self.session.execute(
                select(Draft).where(
                    Draft.id == draft_id, Draft.owner_id == self.user.id, Draft.kind == "food"
                )
            )
        ).scalar_one_or_none()
        if row is None:
            raise NotFound
        try:
            state = FoodDraftState.model_validate(row.payload)
        except ValidationError as exc:
            raise ServiceError("bad_draft") from exc
        return row, state

    async def _save_state(self, row: Draft, expected_version: int, state: FoodDraftState) -> Draft:
        if row.status != "pending":
            raise Conflict("already_resolved")
        result = await self.session.execute(
            update(Draft)
            .where(
                Draft.id == row.id,
                Draft.owner_id == self.user.id,
                Draft.status == "pending",
                Draft.version == expected_version,
            )
            .values(payload=state.model_dump(mode="json"), version=Draft.version + 1)
            .execution_options(synchronize_session=False)
        )
        if result.rowcount != 1:  # type: ignore[attr-defined]
            raise Conflict("draft_changed")
        await self.session.refresh(row)
        return row

    async def _editable(self, draft_id: int, version: int) -> tuple[Draft, FoodDraftState]:
        row, state = await self.get_draft(draft_id)
        if row.status != "pending":
            raise Conflict("already_resolved")
        if row.version != version:
            raise Conflict("draft_changed")
        return row, state

    async def edit_item(self, draft_id: int, version: int, index: int, text: str) -> Draft:
        """'150 г' / '0,5 л' / '2 шт' changes the amount; '350 ккал' sets stated calories."""
        row, state = await self._editable(draft_id, version)
        if not 0 <= index < len(state.items):
            raise NotFound
        item = state.items[index]
        kcal = _KCAL_EDIT.match(text)
        if kcal:
            try:
                value = parse_kcal(kcal.group(1))
            except ParseError as exc:
                raise ServiceError(exc.code) from exc
            item = item.model_copy(
                update={
                    "per": None,
                    "basis": None,
                    "fixed": FixedTotals(energy_kcal=value),
                    "nutrient_source": NutrientSource.USER,
                    "food_id": None,
                    "source_id": None,
                    "source_label": None,
                }
            )
        else:
            parsed = parse_amount(text)
            if parsed is None:
                raise ServiceError("bad_amount")
            amount, unit = parsed
            if item.per is None and item.fixed is not None and item.grams:
                # Rescale fixed totals proportionally only when the old mass is known.
                new_g = to_grams(amount, unit)
                if new_g is None:
                    raise ServiceError("bad_amount")
                factor = new_g / item.grams
                f = item.fixed
                item = item.model_copy(
                    update={
                        "fixed": FixedTotals(
                            **{
                                k: (None if v is None else (v * factor).quantize(Decimal("0.1")))
                                for k, v in f.model_dump().items()
                            }
                        )
                    }
                )
            item = item.model_copy(
                update={
                    "amount": amount,
                    "unit": unit,
                    "amount_text": text.strip()[:60],
                    "grams_estimate": None,
                    "uncertain": False,
                }
            )
        state.items[index] = compute(item)
        return await self._save_state(row, version, state)

    async def scale_item(self, draft_id: int, version: int, index: int, factor: Decimal) -> Draft:
        """ "½", "×2" buttons: scale the stated amount (or known grams / fixed totals)."""
        if not Decimal("0.1") <= factor <= Decimal(10):
            raise ServiceError("bad_amount")
        row, state = await self._editable(draft_id, version)
        if not 0 <= index < len(state.items):
            raise NotFound
        item = state.items[index]

        def mul(v: Decimal | None) -> Decimal | None:
            return None if v is None else (v * factor).quantize(Decimal("0.01"))

        if item.amount is not None and item.unit is not None:
            update: dict[str, object] = {
                "amount": mul(item.amount),
                "grams_estimate": mul(item.grams_estimate),
            }
        elif item.grams is not None:
            update = {"amount": mul(item.grams), "unit": Unit.G, "grams_estimate": None}
        else:
            raise ServiceError("bad_amount")
        if item.per is None and item.fixed is not None:
            update["fixed"] = FixedTotals(**{k: mul(v) for k, v in item.fixed.model_dump().items()})
        update["amount_text"] = None
        state.items[index] = compute(item.model_copy(update=update))
        return await self._save_state(row, version, state)

    async def remove_item(self, draft_id: int, version: int, index: int) -> Draft:
        row, state = await self._editable(draft_id, version)
        if not 0 <= index < len(state.items):
            raise NotFound
        state.items.pop(index)
        return await self._save_state(row, version, state)

    async def add_items(self, draft_id: int, version: int, text: str) -> Draft:
        row, state = await self._editable(draft_id, version)
        parsed = parse_food_line(text)
        if not parsed:
            raise ServiceError("bad_food_text")
        if len(state.items) + len(parsed) > MAX_ITEMS:
            raise ServiceError("too_many_items")
        for p in parsed:
            ai_item = FoodItemAI(
                name=p.name, amount=p.amount, unit=p.unit, amount_text=p.amount_text
            )
            state.items.append(compute(await self._resolve(ai_item, allow_estimate=False)))
        return await self._save_state(row, version, state)

    async def set_meal_type(self, draft_id: int, version: int, meal_type: MealType) -> Draft:
        row, state = await self._editable(draft_id, version)
        state.meal_type = meal_type
        return await self._save_state(row, version, state)

    async def confirm(
        self, draft_id: int, version: int, now: dt.datetime | None = None
    ) -> list[FoodEntry]:
        """Atomic pending->confirmed transition guarded by the shown version, then save."""
        row, state = await self.get_draft(draft_id)
        if not state.items:
            raise ServiceError("empty_draft")
        result = await self.session.execute(
            update(Draft)
            .where(
                Draft.id == draft_id,
                Draft.owner_id == self.user.id,
                Draft.status == "pending",
                Draft.version == version,
            )
            .values(status="confirmed", resolved_at=utcnow())
            .execution_options(synchronize_session=False)
        )
        if result.rowcount != 1:  # type: ignore[attr-defined]
            await self.session.refresh(row)
            raise Conflict("already_resolved" if row.status != "pending" else "draft_changed")
        diary = DiaryService(self.session, self.user)
        entries = []
        source = {
            "text": "text",
            "photo": "ai_draft",
            "voice": "ai_draft",
            "copy": "copy",
            "saved": "saved",
            "recipe": "saved",
            "catalog": "manual",
        }[state.origin]
        if state.ai:
            source = "ai_draft"
        for item in state.items:
            food_id = await self._ensure_catalog_link(item)
            entries.append(
                await diary.add_food(
                    _display_name(item),
                    energy_kcal=item.energy_kcal,
                    protein_g=item.protein_g,
                    fat_g=item.fat_g,
                    carbs_g=item.carbs_g,
                    fiber_g=item.fiber_g,
                    precision=item.precision,
                    source=source,
                    draft_id=row.id,
                    meal_type=state.meal_type.value,
                    amount=item.amount,
                    unit=item.unit.value if item.unit else None,
                    grams=item.grams.quantize(Decimal("0.1")) if item.grams is not None else None,
                    nutrient_source=item.nutrient_source.value if item.nutrient_source else None,
                    food_id=food_id,
                    now=now,
                )
            )
        return entries

    async def _ensure_catalog_link(self, item: DraftItem) -> int | None:
        """Keep provenance: external records chosen by the user are cached in *their*
        catalog. Catalog IDs from the payload are re-checked against the owner."""
        if item.food_id is not None:
            owned = (
                await self.session.execute(
                    select(Food.id).where(Food.id == item.food_id, Food.owner_id == self.user.id)
                )
            ).scalar_one_or_none()
            return owned
        if item.nutrient_source in (NutrientSource.USDA, NutrientSource.OFF) and item.per:
            existing = (
                await self.session.execute(
                    select(Food.id).where(
                        Food.owner_id == self.user.id,
                        Food.source == item.nutrient_source.value,
                        Food.source_id == item.source_id,
                    )
                )
            ).scalar_one_or_none()
            if existing is not None:
                return existing
            food = Food(
                owner_id=self.user.id,
                name=(item.source_label or item.name)[:120],
                brand=item.brand,
                basis=(item.basis or Basis.PER_100G).value,
                serving_g=item.serving_g,
                energy_kcal=item.per.energy_kcal,
                protein_g=item.per.protein_g,
                fat_g=item.per.fat_g,
                carbs_g=item.per.carbs_g,
                fiber_g=item.per.fiber_g,
                source=item.nutrient_source.value,
                source_id=item.source_id,
                source_fetched_at=utcnow(),
            )
            self.session.add(food)
            await self.session.flush()
            return food.id
        return None

    async def cancel(self, draft_id: int) -> None:
        result = await self.session.execute(
            update(Draft)
            .where(Draft.id == draft_id, Draft.owner_id == self.user.id, Draft.status == "pending")
            .values(status="cancelled", resolved_at=utcnow())
            .execution_options(synchronize_session=False)
        )
        if result.rowcount != 1:  # type: ignore[attr-defined]
            await self.get_draft(draft_id)  # NotFound for foreign/missing ids
            raise Conflict("already_resolved")

    # --- catalog (user-confirmed foods) ------------------------------------------------

    async def add_catalog_food(self, line: str) -> Food:
        """'Творог 5%; 121; 17/5/3; 180' = name; kcal per 100 g; P/F/C per 100 g; serving g."""
        parts = [p.strip() for p in line.split(";")]
        if len(parts) < 2 or not parts[0] or len(parts[0]) > 120:
            raise ServiceError("bad_catalog_line")
        try:
            kcal = parse_kcal(parts[1])
            p, f, c = parse_macros(parts[2]) if len(parts) > 2 and parts[2] else (None,) * 3
            serving = parse_decimal(parts[3]) if len(parts) > 3 and parts[3] else None
            per = Per100(energy_kcal=kcal, protein_g=p, fat_g=f, carbs_g=c)
            validate_per100(per, Basis.PER_100G)
        except (ParseError, ValidationError) as exc:
            raise ServiceError(getattr(exc, "code", "implausible_nutrients")) from exc
        if serving is not None and not Decimal(0) < serving <= MAX_GRAMS:
            raise ServiceError("bad_amount")
        count = len(
            (await self.session.execute(select(Food.id).where(Food.owner_id == self.user.id))).all()
        )
        if count >= MAX_CATALOG:
            raise ServiceError("limit_reached")
        food = Food(
            owner_id=self.user.id,
            name=" ".join(parts[0].split()),
            basis="100g",
            serving_g=serving,
            energy_kcal=kcal,
            protein_g=p,
            fat_g=f,
            carbs_g=c,
            source="label",
            favorite=True,
        )
        self.session.add(food)
        await self.session.flush()
        return food

    async def get_food(self, food_id: int) -> Food:
        food = (
            await self.session.execute(
                select(Food).where(
                    Food.id == food_id, Food.owner_id == self.user.id, Food.archived_at.is_(None)
                )
            )
        ).scalar_one_or_none()
        if food is None:
            raise NotFound
        return food

    async def list_foods(self, *, favorites_only: bool = False) -> list[Food]:
        query = select(Food).where(Food.owner_id == self.user.id, Food.archived_at.is_(None))
        if favorites_only:
            query = query.where(Food.favorite.is_(True))
        return list((await self.session.execute(query.order_by(Food.name).limit(50))).scalars())

    async def toggle_favorite_food(self, food_id: int) -> Food:
        food = await self.get_food(food_id)
        food.favorite = not food.favorite
        await self.session.flush()
        return food

    # --- saved meals and recipes -------------------------------------------------------

    async def save_meal_from_draft(self, draft_id: int, name: str) -> SavedMeal:
        _, state = await self.get_draft(draft_id)
        return await self._save_meal(name, "meal", state.items, None)

    async def create_recipe(
        self, name: str, ingredients_text: str, cooked_yield_g: Decimal | None
    ) -> SavedMeal:
        items = []
        for p in parse_food_line(ingredients_text):
            ai_item = FoodItemAI(
                name=p.name, amount=p.amount, unit=p.unit, amount_text=p.amount_text
            )
            items.append(compute(await self._resolve(ai_item, allow_estimate=False)))
        if not items:
            raise ServiceError("bad_food_text")
        if cooked_yield_g is not None and not Decimal(0) < cooked_yield_g <= Decimal(20000):
            raise ServiceError("bad_amount")
        return await self._save_meal(name, "recipe", items, cooked_yield_g)

    async def _save_meal(
        self, name: str, kind: str, items: list[DraftItem], cooked_yield_g: Decimal | None
    ) -> SavedMeal:
        name = " ".join(name.split())
        if not name or len(name) > 80:
            raise ServiceError("bad_name")
        if not items:
            raise ServiceError("empty_draft")
        count = len(
            (
                await self.session.execute(
                    select(SavedMeal.id).where(SavedMeal.owner_id == self.user.id)
                )
            ).all()
        )
        if count >= MAX_MEALS:
            raise ServiceError("limit_reached")
        clean = [i.model_copy(update={"uncertain": False}).model_dump(mode="json") for i in items]
        meal = SavedMeal(
            owner_id=self.user.id,
            name=name,
            kind=kind,
            items=clean,
            cooked_yield_g=cooked_yield_g,
            favorite=True,
        )
        self.session.add(meal)
        await self.session.flush()
        return meal

    async def get_meal(self, meal_id: int) -> SavedMeal:
        meal = (
            await self.session.execute(
                select(SavedMeal).where(
                    SavedMeal.id == meal_id,
                    SavedMeal.owner_id == self.user.id,
                    SavedMeal.archived_at.is_(None),
                )
            )
        ).scalar_one_or_none()
        if meal is None:
            raise NotFound
        return meal

    async def list_meals(self, kind: str | None = None) -> list[SavedMeal]:
        query = select(SavedMeal).where(
            SavedMeal.owner_id == self.user.id, SavedMeal.archived_at.is_(None)
        )
        if kind:
            query = query.where(SavedMeal.kind == kind)
        return list(
            (await self.session.execute(query.order_by(SavedMeal.name).limit(50))).scalars()
        )

    async def archive_meal(self, meal_id: int) -> None:
        meal = await self.get_meal(meal_id)
        meal.archived_at = utcnow()
        await self.session.flush()


def _display_name(item: DraftItem) -> str:
    name = item.name
    if item.amount_text and item.amount_text not in name:
        name = f"{name} ({item.amount_text})"
    return name[:120]
