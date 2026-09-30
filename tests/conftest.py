"""Test fixtures.

Database tests need TEST_PG_ADMIN_URL pointing at a disposable PostgreSQL server where the
given role can create roles and databases, e.g.
    TEST_PG_ADMIN_URL=postgresql://postgres@127.0.0.1:5433/postgres
A fresh database is created per test run, migrated with the owner role, and accessed by
tests through the non-owner runtime role, exactly as in production.
"""

from __future__ import annotations

import asyncio
import os
import secrets
from collections.abc import AsyncIterator
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import asyncpg
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from fitcoach.config import get_settings
from fitcoach.db.models import User
from fitcoach.db.session import create_engine, create_sessionmaker
from fitcoach.services.users import UserService, resolve_user

ROOT = Path(__file__).resolve().parents[1]
OWNER_ROLE = "fitcoach_owner_test"
APP_ROLE = "fitcoach_app_test"
PASSWORD = "test-only-password"

ADMIN_URL = os.environ.get("TEST_PG_ADMIN_URL")
requires_db = pytest.mark.skipif(not ADMIN_URL, reason="TEST_PG_ADMIN_URL not set")


def _with(url: str, *, user: str, password: str, db: str, driver: str = "") -> str:
    parts = urlsplit(url)
    host = parts.hostname or "localhost"
    netloc = f"{user}:{password}@{host}:{parts.port or 5432}"
    scheme = "postgresql+asyncpg" if driver else "postgresql"
    return urlunsplit((scheme, netloc, f"/{db}", "", ""))


async def _ensure_role(conn: asyncpg.Connection, name: str, extra: str) -> None:
    exists = await conn.fetchval("SELECT 1 FROM pg_roles WHERE rolname = $1", name)
    if not exists:
        await conn.execute(f"CREATE ROLE {name} LOGIN PASSWORD '{PASSWORD}' {extra}")


@pytest.fixture(scope="session")
async def database() -> AsyncIterator[dict[str, str]]:
    if not ADMIN_URL:
        pytest.skip("TEST_PG_ADMIN_URL not set")
    db_name = f"fitcoach_test_{secrets.token_hex(4)}"
    admin = await asyncpg.connect(ADMIN_URL)
    await _ensure_role(admin, OWNER_ROLE, "NOSUPERUSER NOBYPASSRLS")
    await _ensure_role(admin, APP_ROLE, "NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE")
    await admin.execute(f"CREATE DATABASE {db_name} OWNER {OWNER_ROLE}")
    await admin.close()

    owner_url = _with(ADMIN_URL, user=OWNER_ROLE, password=PASSWORD, db=db_name, driver="a")
    app_url = _with(ADMIN_URL, user=APP_ROLE, password=PASSWORD, db=db_name, driver="a")
    admin_db_url = _with(
        ADMIN_URL,
        user=urlsplit(ADMIN_URL).username or "postgres",
        password=urlsplit(ADMIN_URL).password or "",
        db=db_name,
    )

    os.environ["APP_DB_ROLE"] = APP_ROLE
    get_settings.cache_clear()
    owner_admin = await asyncpg.connect(admin_db_url)
    await owner_admin.execute(f"ALTER SCHEMA public OWNER TO {OWNER_ROLE}")
    await owner_admin.execute(f"REVOKE CREATE ON SCHEMA public FROM {APP_ROLE}")
    await owner_admin.close()

    await asyncio.to_thread(migrate, owner_url, "head")
    try:
        yield {"owner_url": owner_url, "app_url": app_url, "name": db_name}
    finally:
        admin = await asyncpg.connect(ADMIN_URL)
        await admin.execute(f"DROP DATABASE IF EXISTS {db_name} WITH (FORCE)")
        await admin.close()


def migrate(url: str, target: str, downgrade: bool = False) -> None:
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.attributes["url"] = url
    if downgrade:
        command.downgrade(cfg, target)
    else:
        command.upgrade(cfg, target)


@pytest.fixture(scope="session")
async def engine(database: dict[str, str]) -> AsyncIterator[AsyncEngine]:
    eng = create_engine(database["app_url"], pool_size=2)
    yield eng
    await eng.dispose()


@pytest.fixture(scope="session")
def sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return create_sessionmaker(engine)


_next_tid = [7_000_000_000 + secrets.randbelow(1_000_000) * 100]


def new_telegram_id() -> int:
    _next_tid[0] += 1
    return _next_tid[0]


async def make_user(
    sm: async_sessionmaker[AsyncSession], tz: str = "Europe/Berlin", language: str = "ru"
) -> int:
    """Create an onboarded synthetic user; returns the Telegram ID."""
    tid = new_telegram_id()
    async with sm() as session:
        user = await resolve_user(session, tid)
        svc = UserService(session, user)
        await svc.set_language(language)
        await svc.confirm_adult()
        await svc.set_goal("habits")
        await svc.set_timezone(tz)
        await svc.set_kcal_target(None)
        await session.commit()
    return tid


async def open_user(session: AsyncSession, tid: int) -> User:
    return await resolve_user(session, tid)
