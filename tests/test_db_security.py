"""RLS, pooled-connection isolation, same-owner FKs, immutability, migrations."""

from __future__ import annotations

import asyncio
import secrets
from decimal import Decimal

import asyncpg
import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from fitcoach.db.models import OWNED_TABLES
from fitcoach.db.session import create_engine, create_sessionmaker
from fitcoach.domain.fields import FieldDefinition, FieldType, default_duration_field
from fitcoach.domain.nutrition import Precision
from fitcoach.services.activities import ActivityService
from fitcoach.services.diary import DiaryService
from fitcoach.services.errors import NotFound
from fitcoach.services.users import local_today
from tests.conftest import (
    ADMIN_URL,
    APP_ROLE,
    OWNER_ROLE,
    PASSWORD,
    _with,
    make_user,
    migrate,
    open_user,
    requires_db,
)

pytestmark = requires_db


async def _food_count(session: AsyncSession) -> int:
    return int((await session.execute(text("SELECT count(*) FROM food_entries"))).scalar_one())


async def test_runtime_role_is_not_owner_and_cannot_bypass_rls(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with sessionmaker() as s:
        row = (
            await s.execute(
                text("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user")
            )
        ).one()
        assert row == (False, False)
        owners = (
            (
                await s.execute(
                    text("SELECT DISTINCT tableowner FROM pg_tables WHERE schemaname = 'public'")
                )
            )
            .scalars()
            .all()
        )
        assert owners == [OWNER_ROLE]
        rls = dict(
            (
                await s.execute(
                    text("SELECT relname, relrowsecurity FROM pg_class WHERE relname = ANY(:t)"),
                    {"t": [*OWNED_TABLES, "users"]},
                )
            ).all()
        )
        assert len(rls) == len(OWNED_TABLES) + 1
        assert all(rls.values())


async def test_users_cannot_see_or_write_each_others_rows(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    a, b = await make_user(sessionmaker), await make_user(sessionmaker)
    async with sessionmaker() as s:
        ua = await open_user(s, a)
        await DiaryService(s, ua).add_food(
            "A-only", energy_kcal=Decimal(100), precision=Precision.MEASURED
        )
        a_id = ua.id
        await s.commit()
    async with sessionmaker() as s:
        ub = await open_user(s, b)
        names = (await s.execute(text("SELECT name FROM food_entries"))).scalars().all()
        assert "A-only" not in names
        users = (await s.execute(text("SELECT telegram_id FROM users"))).scalars().all()
        assert users == [b]
        assert ub.id != a_id
    async with sessionmaker() as s:
        await open_user(s, b)
        with pytest.raises(DBAPIError, match="row-level security"):
            await s.execute(
                text(
                    "INSERT INTO food_entries (owner_id, local_date, eaten_at, name, precision) "
                    "VALUES (:o, current_date, now(), 'forged', 'unknown')"
                ),
                {"o": a_id},
            )


async def test_context_is_transaction_scoped_on_pooled_connection(database: dict[str, str]) -> None:
    engine = create_engine(database["app_url"], pool_size=1)
    sm = create_sessionmaker(engine)
    try:
        a = await make_user(sm)
        async with sm() as s:
            ua = await open_user(s, a)
            await DiaryService(s, ua).add_food("x", energy_kcal=None, precision=Precision.UNKNOWN)
            await s.commit()
            # New transaction in the same session keeps the bound identity.
            assert await _food_count(s) >= 1
        # Next checkout of the single pooled connection, no identity bound.
        async with sm() as s:
            assert await _food_count(s) == 0
            users = await s.execute(text("SELECT count(*) FROM users"))
            assert users.scalar_one() == 0
            settings = await s.execute(
                text(
                    "SELECT current_setting('app.user_id', true), "
                    "current_setting('app.telegram_id', true)"
                )
            )
            assert settings.one() == ("", "")
        # Rolled-back identity must not survive either.
        async with sm() as s:
            await open_user(s, a)
            await s.rollback()
        async with sm() as s:
            assert await _food_count(s) == 0
    finally:
        await engine.dispose()


async def test_forged_nested_ids_are_rejected(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    a, b = await make_user(sessionmaker), await make_user(sessionmaker)
    async with sessionmaker() as s:
        ua = await open_user(s, a)
        svc = ActivityService(s, ua)
        tv = await svc.create_type(
            "Enduro",
            [
                default_duration_field("Time"),
                FieldDefinition(key="f1", label="Laps", type=FieldType.INTEGER),
            ],
        )
        wv = await svc.create_template(tv.activity_type_id, "Track day")
        a_ctx = await svc.recording_context(template_id=wv.template_id)
        await s.commit()

    async with sessionmaker() as s:
        ub = await open_user(s, b)
        svc_b = ActivityService(s, ub)
        with pytest.raises(NotFound):
            await svc_b.recording_context(template_id=wv.template_id)
        with pytest.raises(NotFound):
            await svc_b.recording_context(type_id=tv.activity_type_id)
        with pytest.raises(NotFound):
            await svc_b.record_session(a_ctx, {"duration": "30"})  # forged client state
        with pytest.raises(NotFound):
            await svc_b.plan(wv.template_id, local_today(ub))

    # Even bypassing services, the same-owner composite FK refuses cross-user references.
    async with sessionmaker() as s:
        ub = await open_user(s, b)
        with pytest.raises(IntegrityError):
            await s.execute(
                text(
                    "INSERT INTO planned_workouts (owner_id, template_version_id, planned_date) "
                    "VALUES (:o, :tv, current_date)"
                ),
                {"o": ub.id, "tv": wv.id},
            )


async def test_version_rows_are_immutable(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    a = await make_user(sessionmaker)
    async with sessionmaker() as s:
        ua = await open_user(s, a)
        tv = await ActivityService(s, ua).create_type("Swim", [default_duration_field("Time")])
        await s.commit()
    async with sessionmaker() as s:
        await open_user(s, a)
        with pytest.raises(DBAPIError, match="permission denied"):
            await s.execute(
                text("UPDATE activity_type_versions SET name = 'x' WHERE id = :id"), {"id": tv.id}
            )


async def test_migrations_downgrade_and_reapply_on_existing_database(
    database: dict[str, str],
) -> None:
    assert ADMIN_URL
    name = f"fitcoach_mig_{secrets.token_hex(4)}"
    admin = await asyncpg.connect(ADMIN_URL)
    await admin.execute(f"CREATE DATABASE {name} OWNER {OWNER_ROLE}")
    await admin.close()
    url = _with(ADMIN_URL, user=OWNER_ROLE, password=PASSWORD, db=name, driver="a")
    try:
        await asyncio.to_thread(migrate, url, "head")
        await asyncio.to_thread(migrate, url, "head")  # no-op on an up-to-date database
        await asyncio.to_thread(migrate, url, "base", True)
        await asyncio.to_thread(migrate, url, "head")
        conn = await asyncpg.connect(url.replace("+asyncpg", ""))
        assert await conn.fetchval("SELECT version_num FROM alembic_version") == "0001"
        assert await conn.fetchval(
            "SELECT has_table_privilege($1, 'food_entries', 'SELECT')", APP_ROLE
        )
        await conn.close()
    finally:
        admin = await asyncpg.connect(ADMIN_URL)
        await admin.execute(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")
        await admin.close()
