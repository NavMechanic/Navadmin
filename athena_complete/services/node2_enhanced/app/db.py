"""Async engine, session factory and the declarative base.

One engine per process, sessions per request. ``expire_on_commit=False`` so a
committed ORM object is still readable while building the response — without it
every attribute access after commit fires a lazy load against a closed
greenlet context, which is the single most common async-SQLAlchemy bug.
"""
from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from typing import Any

from sqlalchemy import event
from sqlalchemy.ext.asyncio import (AsyncEngine, AsyncSession, async_sessionmaker,
                                    create_async_engine)
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.pool import NullPool

from .config import get_settings

log = logging.getLogger(__name__)


class Base(DeclarativeBase):
    """Declarative base for every ORM model in this service."""


_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def _engine_kwargs(settings: Any) -> dict[str, Any]:
    if settings.is_sqlite:
        # SQLite has no server-side pool to size, and NullPool avoids holding a
        # file handle open across the test suite's engine disposals.
        return {"poolclass": NullPool}
    return {
        "pool_size": settings.db_pool_size,
        "max_overflow": settings.db_max_overflow,
        "pool_recycle": settings.db_pool_recycle_seconds,
        "pool_pre_ping": True,      # a recycled connection behind a proxy is dead silently
    }


def get_engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        s = get_settings()
        _engine = create_async_engine(s.database_url, echo=s.db_echo, future=True,
                                      **_engine_kwargs(s))
        if s.is_sqlite:
            # Foreign keys are OFF by default in SQLite, which would let the
            # test suite accept BOM lines PostgreSQL rejects. Turn them on so
            # both backends enforce the same integrity rules.
            @event.listens_for(_engine.sync_engine, "connect")
            def _fk_on(dbapi_conn, _record):  # pragma: no cover - trivial
                cur = dbapi_conn.cursor()
                cur.execute("PRAGMA foreign_keys=ON")
                cur.close()
        log.info("database engine created for %s", s.database_url.split("@")[-1])
    return _engine


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    global _sessionmaker
    if _sessionmaker is None:
        _sessionmaker = async_sessionmaker(
            bind=get_engine(), class_=AsyncSession,
            expire_on_commit=False, autoflush=False,
        )
    return _sessionmaker


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency. Rolls back on exception, always closes."""
    factory = get_sessionmaker()
    async with factory() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise


async def create_all() -> None:
    """Create the schema directly.

    Used by tests and the SQLite quick-start. On PostgreSQL prefer Alembic —
    ``alembic upgrade head`` — so schema changes are reviewable and reversible.
    """
    from . import models  # noqa: F401 - registers the mappers before create_all

    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def drop_all() -> None:
    from . import models  # noqa: F401

    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


async def dispose_engine() -> None:
    global _engine, _sessionmaker
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _sessionmaker = None
