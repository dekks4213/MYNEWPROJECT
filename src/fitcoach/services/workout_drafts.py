"""Drafts for workouts described in text and for AI-proposed activity schemas.

text -> WorkoutParse (deterministic strength parser or AI) -> editable *actual-session*
draft -> user picks/keeps the activity type -> confirm -> WorkoutSession.
A described workout is never turned into a plan, and a plan is never marked done by it.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from fitcoach.ai.gateway import AIGateway
from fitcoach.ai.types import ActivitySchemaDraft, WorkoutParse
from fitcoach.db.models import ActivityTypeVersion, Draft, User, WorkoutSession
from fitcoach.domain.fields import (
    DURATION_KEY,
    Aggregation,
    FieldDefinition,
    FieldSchema,
    FieldType,
    default_duration_field,
)
from fitcoach.domain.starters import Label, starter_fields
from fitcoach.domain.units import ParseError
from fitcoach.domain.workout import ActivityKind, Block, WorkoutBody, parse_strength_text
from fitcoach.services.activities import ActivityService
from fitcoach.services.errors import Conflict, NotFound, ServiceError
from fitcoach.services.users import utcnow


class WorkoutDraftState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: ActivityKind = ActivityKind.CUSTOM
    title: str | None = None
    type_id: int | None = None
    duration_s: int | None = Field(default=None, ge=0, le=86_400)
    distance_km: Decimal | None = Field(default=None, ge=0, le=2000)
    blocks: list[Block] = Field(default_factory=list, max_length=20)
    notes: str | None = Field(default=None, max_length=500)
    clarification: str | None = None
    ai: bool = False
    mock: bool = False


# --- generic draft storage helpers -------------------------------------------------------


async def _load(session: AsyncSession, user: User, draft_id: int, kind: str) -> Draft:
    row = (
        await session.execute(
            select(Draft).where(Draft.id == draft_id, Draft.owner_id == user.id, Draft.kind == kind)
        )
    ).scalar_one_or_none()
    if row is None:
        raise NotFound
    return row


async def _transition(
    session: AsyncSession,
    user: User,
    row: Draft,
    version: int,
    *,
    status: str | None = None,
    payload: dict[str, Any] | None = None,
) -> None:
    values: dict[str, Any] = {"version": Draft.version + 1}
    if status is not None:
        values.update(status=status, resolved_at=utcnow())
    if payload is not None:
        values["payload"] = payload
    result = await session.execute(
        update(Draft)
        .where(
            Draft.id == row.id,
            Draft.owner_id == user.id,
            Draft.status == "pending",
            Draft.version == version,
        )
        .values(**values)
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:  # type: ignore[attr-defined]
        await session.refresh(row)
        raise Conflict("already_resolved" if row.status != "pending" else "draft_changed")
    await session.refresh(row)


class WorkoutDraftService:
    def __init__(self, session: AsyncSession, user: User, gateway: AIGateway | None = None) -> None:
        self.session = session
        self.user = user
        self.gateway = gateway
        self.activities = ActivityService(session, user)

    async def draft_from_text(self, text: str) -> Draft:
        text = text.strip()
        if not text or len(text) > 1500:
            raise ServiceError("bad_workout_text")
        try:
            body = parse_strength_text(text)
            parse = WorkoutParse(kind=ActivityKind.STRENGTH, blocks=list(body.blocks))
            ai = False
        except ParseError:
            if self.gateway is None or not self.gateway.text_available(self.user):
                raise ServiceError("bad_workout_text") from None
            parse = await self.gateway.parse_workout_text(self.session, self.user, text)
            ai = True
        state = WorkoutDraftState(
            kind=parse.kind,
            title=parse.title,
            duration_s=int(parse.duration_min * 60) if parse.duration_min is not None else None,
            distance_km=parse.distance_km,
            blocks=parse.blocks,
            notes=parse.notes,
            clarification=parse.clarification,
            ai=ai,
            mock=bool(ai and self.gateway and self.gateway.is_mock),
            type_id=await self._default_type(parse.kind),
        )
        if not state.blocks and state.duration_s is None and state.distance_km is None:
            raise ServiceError("bad_workout_text")
        row = Draft(owner_id=self.user.id, kind="workout", payload=state.model_dump(mode="json"))
        self.session.add(row)
        await self.session.flush()
        return row

    async def _default_type(self, kind: ActivityKind) -> int | None:
        for activity in await self.activities.list_types():
            if activity.kind == kind.value:
                return activity.id
        return None

    async def get(self, draft_id: int) -> tuple[Draft, WorkoutDraftState]:
        row = await _load(self.session, self.user, draft_id, "workout")
        try:
            return row, WorkoutDraftState.model_validate(row.payload)
        except ValidationError as exc:
            raise ServiceError("bad_draft") from exc

    async def set_type(self, draft_id: int, version: int, type_id: int) -> Draft:
        row, state = await self.get(draft_id)
        await self.activities.get_type(type_id)  # ownership: model/client ids are untrusted
        state.type_id = type_id
        await _transition(
            self.session, self.user, row, version, payload=state.model_dump(mode="json")
        )
        return row

    async def create_starter_type(self, draft_id: int, version: int, t: Label) -> Draft:
        _, state = await self.get(draft_id)
        kind = state.kind if state.kind is not ActivityKind.CUSTOM else ActivityKind.STRENGTH
        name, fields = starter_fields(kind, t)
        tv = await self.activities.create_type(name, fields, kind)
        return await self.set_type(draft_id, version, tv.activity_type_id)

    async def confirm(self, draft_id: int, version: int) -> WorkoutSession:
        row, state = await self.get(draft_id)
        if state.type_id is None:
            raise ServiceError("choose_type")
        ctx = await self.activities.recording_context(type_id=state.type_id)
        raw: dict[str, str] = {}
        if state.duration_s is not None and ctx.schema.get(DURATION_KEY) is not None:
            raw[DURATION_KEY] = f"{state.duration_s} sec"
        if state.distance_km is not None:
            field = _distance_field(ctx.schema)
            if field is not None:
                km = state.distance_km
                value = km * 1000 if (field.unit or "").lower() in ("m", "м") else km
                raw[field.key] = (
                    str(int(value))
                    if field.type is FieldType.INTEGER
                    else format(value.normalize(), "f")
                )
        await _transition(self.session, self.user, row, version, status="confirmed")
        return await self.activities.record_session(
            ctx,
            raw,
            blocks=WorkoutBody(blocks=tuple(state.blocks)),
            source="ai_draft" if state.ai else "text",
            notes=state.notes,
        )

    async def cancel(self, draft_id: int, version: int) -> None:
        row, _ = await self.get(draft_id)
        await _transition(self.session, self.user, row, version, status="cancelled")


def _distance_field(schema: FieldSchema) -> FieldDefinition | None:
    """The field that holds *total distance*: numeric, km/m, summed across sessions.
    (A pool length is also in metres but is not a distance total.)"""
    for field in schema.fields:
        if (
            field.type in (FieldType.DECIMAL, FieldType.INTEGER)
            and field.aggregation is Aggregation.SUM
            and (field.unit or "").lower() in ("km", "км", "m", "м")
        ):
            return field
    return None


class ActivityDraftService:
    """AI-proposed activity schema; the user confirms before anything is created."""

    def __init__(self, session: AsyncSession, user: User, gateway: AIGateway) -> None:
        self.session = session
        self.user = user
        self.gateway = gateway

    async def draft(self, text: str) -> Draft:
        proposal = await self.gateway.build_activity_draft(self.session, self.user, text)
        fields = to_field_definitions(proposal, duration_label="")  # validate early
        del fields
        row = Draft(
            owner_id=self.user.id, kind="activity", payload=proposal.model_dump(mode="json")
        )
        self.session.add(row)
        await self.session.flush()
        return row

    async def get(self, draft_id: int) -> tuple[Draft, ActivitySchemaDraft]:
        row = await _load(self.session, self.user, draft_id, "activity")
        return row, ActivitySchemaDraft.model_validate(row.payload)

    async def confirm(
        self, draft_id: int, version: int, duration_label: str
    ) -> ActivityTypeVersion:
        row, proposal = await self.get(draft_id)
        fields = to_field_definitions(proposal, duration_label)
        await _transition(self.session, self.user, row, version, status="confirmed")
        return await ActivityService(self.session, self.user).create_type(
            proposal.name, fields, proposal.kind
        )

    async def cancel(self, draft_id: int, version: int) -> None:
        row, _ = await self.get(draft_id)
        await _transition(self.session, self.user, row, version, status="cancelled")


def to_field_definitions(
    proposal: ActivitySchemaDraft, duration_label: str
) -> list[FieldDefinition]:
    """Server-side conversion: keys are assigned here (never taken from the model), and every
    field is validated by the same rules as the manual builder."""
    fields = [default_duration_field(duration_label or "Duration")]
    try:
        for n, f in enumerate(proposal.fields, start=1):
            fields.append(
                FieldDefinition(
                    key=f"f{n}",
                    label=f.label,
                    type=f.type,
                    unit=f.unit if f.type in (FieldType.DECIMAL, FieldType.INTEGER) else None,
                    choices=tuple(f.choices)
                    if f.type is FieldType.SELECTION and f.choices
                    else None,
                    duration_format=(f.duration_format or "h:mm")
                    if f.type is FieldType.DURATION
                    else None,
                )
            )
        FieldSchema(fields=tuple(fields))
    except ValidationError as exc:
        raise ServiceError("bad_field") from exc
    return fields
