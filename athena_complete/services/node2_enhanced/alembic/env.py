"""Alembic environment.

Reads the URL from the application settings rather than alembic.ini, so the
migration always targets the same database the service does. Runs the async
engine through ``connection.run_sync`` because Alembic's own machinery is
synchronous.
"""
from __future__ import annotations

import asyncio
import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy.ext.asyncio import create_async_engine

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import get_settings   # noqa: E402
from app.db import Base               # noqa: E402
from app import models                # noqa: E402,F401 - registers the mappers

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata
DB_URL = get_settings().database_url


def run_migrations_offline() -> None:
    context.configure(url=DB_URL, target_metadata=target_metadata,
                      literal_binds=True, compare_type=True,
                      dialect_opts={"paramstyle": "named"})
    with context.begin_transaction():
        context.run_migrations()


def _do_run(connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata,
                      compare_type=True, compare_server_default=True)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    engine = create_async_engine(DB_URL, future=True)
    async with engine.connect() as connection:
        await connection.run_sync(_do_run)
    await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
