"""Universal activity builder: types with versioned fields, templates, plans and sessions.

No migration is needed for a new sport: activity types and fields are user data.
Plans and templates never count as performed work.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from fitcoach.db.models import (
    ActivityType,
    ActivityTypeVersion,
    PlannedWorkout,
    Program,
    User,
    WorkoutSession,
    WorkoutTemplate,
    WorkoutTemplateVersion,
)
from fitcoach.domain.fields import (
    DURATION_KEY,
    FieldDefinition,
    FieldSchema,
    JsonValue,
    parse_field_value,
)
from fitcoach.domain.units import ParseError
from fitcoach.domain.workout import ActivityKind, WorkoutBody
from fitcoach.services.errors import Conflict, NotFound, ServiceError
from fitcoach.services.users import local_today, utcnow

MAX_NAME_LEN = 60
MAX_TYPES_PER_USER = 100
MAX_TEMPLATES_PER_USER = 200
MAX_PLAN_DAYS_AHEAD = 366
MAX_PROGRAMS = 30


@dataclass(frozen=True)
class RecordingContext:
    """Everything needed to record a session. Targets are hints only."""

    activity_type_version_id: int
    activity_name: str
    schema: FieldSchema
    template_version_id: int | None
    template_name: str | None
    targets: dict[str, JsonValue]
    planned_workout_id: int | None
    target_blocks: tuple[dict[str, Any], ...] = ()
    kind: str = "custom"


def _clean_name(name: str) -> str:
    name = " ".join(name.split())
    if not name or len(name) > MAX_NAME_LEN:
        raise ServiceError("bad_name")
    return name


def _schema(fields: list[dict[str, Any]] | list[FieldDefinition]) -> FieldSchema:
    try:
        return FieldSchema(fields=tuple(fields))
    except ValidationError as exc:
        raise ServiceError("bad_schema") from exc


def _dump(schema: FieldSchema) -> list[dict[str, Any]]:
    return [f.model_dump(mode="json", exclude_none=True) for f in schema.fields]


class ActivityService:
    def __init__(self, session: AsyncSession, user: User) -> None:
        self.session = session
        self.user = user

    # --- activity types -------------------------------------------------

    async def create_type(
        self,
        name: str,
        fields: list[FieldDefinition],
        kind: ActivityKind = ActivityKind.CUSTOM,
    ) -> ActivityTypeVersion:
        name = _clean_name(name)
        schema = _schema(fields)
        if schema.get(DURATION_KEY) is None:
            raise ServiceError("duration_required")
        count = len(await self.list_types(include_archived=True))
        if count >= MAX_TYPES_PER_USER:
            raise ServiceError("limit_reached")
        activity = ActivityType(
            owner_id=self.user.id,
            name=name,
            current_version=1,
            kind=kind.value,
            # A motorcycle ride is logged, but never counted as human training effort.
            counts_as_training=kind is not ActivityKind.MOTO_RIDE,
        )
        self.session.add(activity)
        await self.session.flush()
        version = ActivityTypeVersion(
            owner_id=self.user.id,
            activity_type_id=activity.id,
            version=1,
            name=name,
            fields=_dump(schema),
        )
        self.session.add(version)
        await self.session.flush()
        return version

    async def revise_type(
        self, type_id: int, fields: list[FieldDefinition], name: str | None = None
    ) -> ActivityTypeVersion:
        """Create a new immutable version. Existing keys keep meaning; history is untouched."""
        activity = await self.get_type(type_id)
        current = await self.current_type_version(type_id)
        old_schema = _schema(current.fields)
        new_schema = _schema(fields)
        if new_schema.get(DURATION_KEY) is None:
            raise ServiceError("duration_required")
        for old in old_schema.fields:
            new = new_schema.get(old.key)
            if new is not None and new.type is not old.type:
                raise ServiceError("field_type_change")
        activity.current_version += 1
        if name is not None:
            activity.name = _clean_name(name)
        version = ActivityTypeVersion(
            owner_id=self.user.id,
            activity_type_id=activity.id,
            version=activity.current_version,
            name=activity.name,
            fields=_dump(new_schema),
        )
        self.session.add(version)
        await self.session.flush()
        return version

    async def get_type(self, type_id: int) -> ActivityType:
        row = (
            await self.session.execute(
                select(ActivityType).where(
                    ActivityType.id == type_id, ActivityType.owner_id == self.user.id
                )
            )
        ).scalar_one_or_none()
        if row is None:
            raise NotFound
        return row

    async def current_type_version(self, type_id: int) -> ActivityTypeVersion:
        activity = await self.get_type(type_id)
        return (
            await self.session.execute(
                select(ActivityTypeVersion).where(
                    ActivityTypeVersion.activity_type_id == activity.id,
                    ActivityTypeVersion.owner_id == self.user.id,
                    ActivityTypeVersion.version == activity.current_version,
                )
            )
        ).scalar_one()

    async def list_types(self, include_archived: bool = False) -> list[ActivityType]:
        query = select(ActivityType).where(ActivityType.owner_id == self.user.id)
        if not include_archived:
            query = query.where(ActivityType.archived_at.is_(None))
        return list((await self.session.execute(query.order_by(ActivityType.id))).scalars())

    # --- templates ------------------------------------------------------

    def _validate_targets(self, schema: FieldSchema, raw: dict[str, str]) -> dict[str, JsonValue]:
        targets: dict[str, JsonValue] = {}
        for key, text in raw.items():
            field = schema.get(key)
            if field is None:
                raise ServiceError("unknown_field")
            try:
                targets[key] = parse_field_value(field, text)
            except ParseError as exc:
                raise ServiceError(exc.code) from exc
        return targets

    async def create_template(
        self,
        type_id: int,
        name: str,
        raw_targets: dict[str, str] | None = None,
        *,
        blocks: WorkoutBody | None = None,
        program_id: int | None = None,
    ) -> WorkoutTemplateVersion:
        name = _clean_name(name)
        type_version = await self.current_type_version(type_id)
        if program_id is not None:
            await self.get_program(program_id)
        targets = self._validate_targets(_schema(type_version.fields), raw_targets or {})
        if len(await self.list_templates(include_archived=True)) >= MAX_TEMPLATES_PER_USER:
            raise ServiceError("limit_reached")
        template = WorkoutTemplate(
            owner_id=self.user.id,
            activity_type_id=type_id,
            name=name,
            current_version=1,
            program_id=program_id,
        )
        self.session.add(template)
        await self.session.flush()
        version = WorkoutTemplateVersion(
            owner_id=self.user.id,
            template_id=template.id,
            version=1,
            name=name,
            activity_type_version_id=type_version.id,
            targets=targets,
            blocks=blocks.dump() if blocks else [],
        )
        self.session.add(version)
        await self.session.flush()
        return version

    async def revise_template(
        self,
        template_id: int,
        raw_targets: dict[str, str],
        *,
        expected_version: int,
        name: str | None = None,
        blocks: WorkoutBody | None = None,
    ) -> WorkoutTemplateVersion:
        """New immutable revision bound to the type's current version; old sessions keep theirs."""
        template = await self.get_template(template_id)
        if template.current_version != expected_version:
            raise Conflict
        previous = await self.current_template_version(template_id)
        type_version = await self.current_type_version(template.activity_type_id)
        targets = self._validate_targets(_schema(type_version.fields), raw_targets)
        new_name = _clean_name(name) if name is not None else template.name
        result = await self.session.execute(
            update(WorkoutTemplate)
            .where(
                WorkoutTemplate.id == template.id,
                WorkoutTemplate.owner_id == self.user.id,
                WorkoutTemplate.current_version == expected_version,
            )
            .values(
                current_version=expected_version + 1,
                name=new_name,
                version=WorkoutTemplate.version + 1,
            )
            .execution_options(synchronize_session=False)
        )
        if result.rowcount != 1:  # type: ignore[attr-defined]
            raise Conflict
        await self.session.refresh(template)
        version = WorkoutTemplateVersion(
            owner_id=self.user.id,
            template_id=template.id,
            version=expected_version + 1,
            name=new_name,
            activity_type_version_id=type_version.id,
            targets=targets,
            blocks=blocks.dump() if blocks is not None else list(previous.blocks),
        )
        self.session.add(version)
        await self.session.flush()
        return version

    async def get_template(self, template_id: int) -> WorkoutTemplate:
        row = (
            await self.session.execute(
                select(WorkoutTemplate).where(
                    WorkoutTemplate.id == template_id, WorkoutTemplate.owner_id == self.user.id
                )
            )
        ).scalar_one_or_none()
        if row is None:
            raise NotFound
        return row

    async def current_template_version(self, template_id: int) -> WorkoutTemplateVersion:
        template = await self.get_template(template_id)
        return (
            await self.session.execute(
                select(WorkoutTemplateVersion).where(
                    WorkoutTemplateVersion.template_id == template.id,
                    WorkoutTemplateVersion.owner_id == self.user.id,
                    WorkoutTemplateVersion.version == template.current_version,
                )
            )
        ).scalar_one()

    async def list_templates(self, include_archived: bool = False) -> list[WorkoutTemplate]:
        query = select(WorkoutTemplate).where(WorkoutTemplate.owner_id == self.user.id)
        if not include_archived:
            query = query.where(WorkoutTemplate.archived_at.is_(None))
        return list((await self.session.execute(query.order_by(WorkoutTemplate.id))).scalars())

    async def archive_template(self, template_id: int) -> WorkoutTemplate:
        """Hide from lists; sessions and plans that used it are preserved."""
        template = await self.get_template(template_id)
        if template.archived_at is None:
            template.archived_at = utcnow()
            await self.session.flush()
        return template

    # --- planning -------------------------------------------------------

    async def plan(self, template_id: int, day: dt.date) -> PlannedWorkout:
        today = local_today(self.user)
        if not today <= day <= today + dt.timedelta(days=MAX_PLAN_DAYS_AHEAD):
            raise ServiceError("bad_date")
        template = await self.get_template(template_id)
        if template.archived_at is not None:
            raise NotFound
        version = await self.current_template_version(template_id)
        planned = PlannedWorkout(
            owner_id=self.user.id, template_version_id=version.id, planned_date=day
        )
        self.session.add(planned)
        await self.session.flush()
        return planned

    async def list_planned(
        self, start: dt.date, end: dt.date
    ) -> list[tuple[PlannedWorkout, WorkoutTemplateVersion]]:
        rows = await self.session.execute(
            select(PlannedWorkout, WorkoutTemplateVersion)
            .join(
                WorkoutTemplateVersion,
                WorkoutTemplateVersion.id == PlannedWorkout.template_version_id,
            )
            .where(
                PlannedWorkout.owner_id == self.user.id,
                PlannedWorkout.status == "planned",
                PlannedWorkout.planned_date.between(start, end),
            )
            .order_by(PlannedWorkout.planned_date, PlannedWorkout.id)
        )
        return [(p, v) for p, v in rows.all()]

    async def get_planned(self, planned_id: int) -> PlannedWorkout:
        row = (
            await self.session.execute(
                select(PlannedWorkout).where(
                    PlannedWorkout.id == planned_id, PlannedWorkout.owner_id == self.user.id
                )
            )
        ).scalar_one_or_none()
        if row is None:
            raise NotFound
        return row

    # --- recording ------------------------------------------------------

    async def _type_version(self, version_id: int) -> ActivityTypeVersion:
        row = (
            await self.session.execute(
                select(ActivityTypeVersion).where(
                    ActivityTypeVersion.id == version_id,
                    ActivityTypeVersion.owner_id == self.user.id,
                )
            )
        ).scalar_one_or_none()
        if row is None:
            raise NotFound
        return row

    async def _template_version(self, version_id: int) -> WorkoutTemplateVersion:
        row = (
            await self.session.execute(
                select(WorkoutTemplateVersion).where(
                    WorkoutTemplateVersion.id == version_id,
                    WorkoutTemplateVersion.owner_id == self.user.id,
                )
            )
        ).scalar_one_or_none()
        if row is None:
            raise NotFound
        return row

    async def recording_context(
        self,
        *,
        type_id: int | None = None,
        template_id: int | None = None,
        planned_id: int | None = None,
    ) -> RecordingContext:
        if sum(x is not None for x in (type_id, template_id, planned_id)) != 1:
            raise ValueError("exactly one source required")
        if type_id is not None:
            activity = await self.get_type(type_id)
            tv = await self.current_type_version(type_id)
            return RecordingContext(
                tv.id, tv.name, _schema(tv.fields), None, None, {}, None, (), activity.kind
            )
        if planned_id is not None:
            planned = await self.get_planned(planned_id)
            if planned.status != "planned":
                raise Conflict("already_done")
            wv = await self._template_version(planned.template_version_id)
        else:
            assert template_id is not None
            wv = await self.current_template_version(template_id)
        tv = await self._type_version(wv.activity_type_version_id)
        activity = await self.get_type(tv.activity_type_id)
        return RecordingContext(
            tv.id,
            tv.name,
            _schema(tv.fields),
            wv.id,
            wv.name,
            dict(wv.targets),
            planned_id,
            tuple(wv.blocks),
            activity.kind,
        )

    async def record_session(
        self,
        ctx: RecordingContext,
        raw_values: dict[str, str],
        now: dt.datetime | None = None,
        *,
        blocks: WorkoutBody | None = None,
        source: str = "manual",
        source_ref: str | None = None,
        notes: str | None = None,
    ) -> WorkoutSession:
        """Persist *performed* values only. Targets are never copied in as results."""
        # Re-resolve ownership server-side: the context may come from client-side state.
        tv = await self._type_version(ctx.activity_type_version_id)
        schema = _schema(tv.fields)
        if ctx.template_version_id is not None:
            wv = await self._template_version(ctx.template_version_id)
            if wv.activity_type_version_id != tv.id:
                raise ServiceError("bad_schema")
        values: dict[str, JsonValue] = {}
        raws: dict[str, str] = {}
        for key, text in raw_values.items():
            field = schema.get(key)
            if field is None:
                raise ServiceError("unknown_field")
            try:
                values[key] = parse_field_value(field, text)
            except ParseError as exc:
                raise ServiceError(exc.code) from exc
            raws[key] = text
        missing = [f.key for f in schema.fields if f.required and values.get(f.key) is None]
        if missing:
            raise ServiceError("missing_required")
        if not values and not (blocks and blocks.blocks):
            raise ServiceError("empty_session")
        if notes is not None and len(notes) > 500:
            raise ServiceError("too_long")

        now = now or utcnow()
        session = WorkoutSession(
            owner_id=self.user.id,
            activity_type_version_id=tv.id,
            template_version_id=ctx.template_version_id,
            local_date=local_today(self.user, now),
            completed_at=now,
            activity_name=tv.name,
            template_name=ctx.template_name,
            field_snapshot=list(tv.fields),
            values=values,
            raw_values=raws,
            blocks=blocks.dump() if blocks else [],
            source=source,
            source_ref=source_ref,
            notes=notes,
        )
        self.session.add(session)
        await self.session.flush()

        if ctx.planned_workout_id is not None:
            # Atomic transition: a plan can be completed once, even with duplicate submits.
            result = await self.session.execute(
                update(PlannedWorkout)
                .where(
                    PlannedWorkout.id == ctx.planned_workout_id,
                    PlannedWorkout.owner_id == self.user.id,
                    PlannedWorkout.status == "planned",
                )
                .values(
                    status="completed", session_id=session.id, version=PlannedWorkout.version + 1
                )
                .execution_options(synchronize_session=False)
            )
            if result.rowcount != 1:  # type: ignore[attr-defined]
                raise Conflict("already_done")
        return session

    # --- programs ---------------------------------------------------------------

    async def create_program(self, name: str) -> Program:
        name = _clean_name(name)
        count = len(
            (
                await self.session.execute(
                    select(Program.id).where(Program.owner_id == self.user.id)
                )
            ).all()
        )
        if count >= MAX_PROGRAMS:
            raise ServiceError("limit_reached")
        program = Program(owner_id=self.user.id, name=name)
        self.session.add(program)
        await self.session.flush()
        return program

    async def get_program(self, program_id: int) -> Program:
        row = (
            await self.session.execute(
                select(Program).where(
                    Program.id == program_id,
                    Program.owner_id == self.user.id,
                    Program.archived_at.is_(None),
                )
            )
        ).scalar_one_or_none()
        if row is None:
            raise NotFound
        return row

    async def list_programs(self) -> list[Program]:
        query = select(Program).where(
            Program.owner_id == self.user.id, Program.archived_at.is_(None)
        )
        return list((await self.session.execute(query.order_by(Program.id))).scalars())

    async def program_templates(self, program_id: int) -> list[WorkoutTemplate]:
        await self.get_program(program_id)
        query = select(WorkoutTemplate).where(
            WorkoutTemplate.owner_id == self.user.id,
            WorkoutTemplate.program_id == program_id,
            WorkoutTemplate.archived_at.is_(None),
        )
        return list((await self.session.execute(query.order_by(WorkoutTemplate.id))).scalars())

    async def assign_template(self, template_id: int, program_id: int | None) -> WorkoutTemplate:
        template = await self.get_template(template_id)
        if program_id is not None:
            await self.get_program(program_id)
        template.program_id = program_id
        await self.session.flush()
        return template

    async def archive_program(self, program_id: int) -> None:
        program = await self.get_program(program_id)
        program.archived_at = utcnow()
        await self.session.flush()

    async def plan_weekdays(
        self, template_id: int, weekdays: set[int], weeks: int = 4
    ) -> list[PlannedWorkout]:
        """Plan a template on given weekdays (0=Monday) for the next `weeks` weeks.

        Existing open plans for the same template and date are not duplicated.
        """
        if not weekdays or not weekdays <= set(range(7)) or not 1 <= weeks <= 12:
            raise ServiceError("bad_date")
        today = local_today(self.user)
        await self.get_template(template_id)
        existing = {
            p.planned_date
            for p, v in await self.list_planned(today, today + dt.timedelta(weeks=weeks))
            if v.template_id == template_id
        }
        created = []
        for offset in range(weeks * 7):
            day = today + dt.timedelta(days=offset)
            if day.weekday() in weekdays and day not in existing:
                created.append(await self.plan(template_id, day))
        return created

    async def list_sessions(
        self, *, since: dt.date | None = None, limit: int = 20
    ) -> list[WorkoutSession]:
        query = select(WorkoutSession).where(
            WorkoutSession.owner_id == self.user.id, WorkoutSession.deleted_at.is_(None)
        )
        if since is not None:
            query = query.where(WorkoutSession.local_date >= since)
        query = query.order_by(WorkoutSession.completed_at.desc()).limit(limit)
        return list((await self.session.execute(query)).scalars())

    async def type_of_version(self, version_id: int) -> ActivityType:
        tv = await self._type_version(version_id)
        return await self.get_type(tv.activity_type_id)
