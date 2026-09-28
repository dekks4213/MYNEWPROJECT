"""Engine/session factory with transaction-scoped RLS context.

The authenticated identity lives in `session.info`. On *every* transaction begin, it is
applied with `set_config(..., is_local => true)`, so it disappears at COMMIT/ROLLBACK and
can never leak to the next user of a pooled connection.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import event, text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import Session, SessionTransaction

TELEGRAM_ID = "telegram_id"
USER_ID = "user_id"

_SET_CONTEXT = text(
    "SELECT set_config('app.telegram_id', :tid, true), set_config('app.user_id', :uid, true)"
)


def create_engine(url: str, pool_size: int = 5) -> AsyncEngine:
    return create_async_engine(url, pool_size=pool_size, pool_pre_ping=True)


def create_sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


@event.listens_for(Session, "after_begin")
def _apply_rls_context(
    session: Session, transaction: SessionTransaction, connection: Connection
) -> None:
    tid: Any = session.info.get(TELEGRAM_ID)
    uid: Any = session.info.get(USER_ID)
    # Always executed, so an empty context is explicit even on a reused connection.
    connection.execute(
        _SET_CONTEXT,
        {"tid": "" if tid is None else str(int(tid)), "uid": "" if uid is None else str(int(uid))},
    )


async def bind_identity(session: AsyncSession, *, telegram_id: int, user_id: int | None) -> None:
    """Set identity for the current and all following transactions of this session."""
    session.info[TELEGRAM_ID] = telegram_id
    session.info[USER_ID] = user_id
    if session.in_transaction():
        await session.execute(
            _SET_CONTEXT,
            {"tid": str(telegram_id), "uid": "" if user_id is None else str(user_id)},
        )
