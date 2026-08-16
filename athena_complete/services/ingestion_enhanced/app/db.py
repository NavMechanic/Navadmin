"""Async engine and session factory.

One engine per process, sessions per request.
"""
from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from typing import Any

from sqlalchemy import event, text
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


# (table, time column) pairs that become hypertables.


def _engine_kwargs(settings: Any) -> dict[str, Any]:
    if settings.is_sqlite:
        return {"poolclass": NullPool}
    return {"pool_size": settings.db_pool_size, "max_overflow": 5,
            "pool_recycle": 1800, "pool_pre_ping": True}


def get_engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        s = get_settings()
        _engine = create_async_engine(s.database_url, echo=s.db_echo, future=True,
                                      **_engine_kwargs(s))
        if s.is_sqlite:
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
            expire_on_commit=False, autoflush=False)
    return _sessionmaker


async def get_session() -> AsyncIterator[AsyncSession]:
    factory = get_sessionmaker()
    async with factory() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise


async def create_all() -> None:
    from . import models  # noqa: F401 - register the mappings
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def dispose_engine() -> None:
    global _engine, _sessionmaker
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _sessionmaker = None


def storage_report():
    from .config import get_settings
    s = get_settings()
    return {"dialect": s.database_url.split("://")[0],
            "target": s.database_url.split("@")[-1]}
