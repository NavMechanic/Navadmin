"""Engine/session factories. Each app instance owns its engine (see main.create_app)."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterator

from fastapi import Request
from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.pool import StaticPool


class Base(DeclarativeBase):
    pass


def utcnow() -> datetime:
    """Naive UTC timestamp (SQLite drops tzinfo, so store everything naive-UTC)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def make_engine(url: str) -> Engine:
    kwargs: dict = {}
    if url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
        if url in ("sqlite://", "sqlite:///:memory:"):
            kwargs["poolclass"] = StaticPool
    engine = create_engine(url, **kwargs)
    if url.startswith("sqlite"):
        @event.listens_for(engine, "connect")
        def _enable_fk(dbapi_conn, _record):  # pragma: no cover - driver hook
            dbapi_conn.execute("PRAGMA foreign_keys=ON")
    return engine


def make_sessionmaker(engine: Engine) -> sessionmaker:
    return sessionmaker(bind=engine, expire_on_commit=False)


@contextmanager
def scratch_session() -> Iterator[Session]:
    """Isolated in-memory database used by self-test gate checks (never touches real data)."""
    from . import models  # noqa: F401  (register tables)

    engine = make_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = make_sessionmaker(engine)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def get_db(request: Request) -> Iterator[Session]:
    session: Session = request.app.state.sessionmaker()
    try:
        yield session
    finally:
        session.close()
