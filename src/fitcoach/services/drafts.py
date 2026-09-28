"""Draft -> explicit confirmation -> records. Confirmation is idempotent."""

from __future__ import annotations

import datetime as dt

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from fitcoach.ai.types import MealDraft
from fitcoach.db.models import Draft, FoodEntry, User
from fitcoach.domain.nutrition import Precision
from fitcoach.services.diary import DiaryService
from fitcoach.services.errors import Conflict, NotFound
from fitcoach.services.users import utcnow


class DraftService:
    def __init__(self, session: AsyncSession, user: User) -> None:
        self.session = session
        self.user = user

    async def create_meal_draft(self, draft: MealDraft) -> Draft:
        row = Draft(owner_id=self.user.id, kind="meal", payload=draft.model_dump(mode="json"))
        self.session.add(row)
        await self.session.flush()
        return row

    async def _resolve(self, draft_id: int, status: str) -> Draft:
        result = await self.session.execute(
            update(Draft)
            .where(
                Draft.id == draft_id,
                Draft.owner_id == self.user.id,
                Draft.status == "pending",
            )
            .values(status=status, resolved_at=utcnow())
            .returning(Draft)
            .execution_options(synchronize_session=False)
        )
        row = result.scalar_one_or_none()
        if row is not None:
            return row
        exists = (
            await self.session.execute(
                select(Draft.id).where(Draft.id == draft_id, Draft.owner_id == self.user.id)
            )
        ).scalar_one_or_none()
        if exists is None:
            raise NotFound
        raise Conflict("already_resolved")

    async def confirm_meal(self, draft_id: int, now: dt.datetime | None = None) -> list[FoodEntry]:
        draft = await self._resolve(draft_id, "confirmed")
        meal = MealDraft.model_validate(draft.payload)  # re-validate stored payload
        diary = DiaryService(self.session, self.user)
        entries = []
        for item in meal.items:
            name = f"{item.name} ({item.quantity_text})" if item.quantity_text else item.name
            entries.append(
                await diary.add_food(
                    name[:120],
                    energy_kcal=item.energy_kcal,
                    protein_g=item.protein_g,
                    fat_g=item.fat_g,
                    carbs_g=item.carbs_g,
                    precision=Precision.APPROXIMATE,
                    source="ai_draft",
                    draft_id=draft.id,
                    now=now,
                )
            )
        return entries

    async def cancel(self, draft_id: int) -> None:
        await self._resolve(draft_id, "cancelled")
