"""Test fixtures.

The suite runs against SQLite by default so it needs no server. Point
``ATHENA_TEST_DATABASE_URL`` at a PostgreSQL instance to run the identical suite
against the real target — the recursive CTEs and the constraint behaviour are
the parts most worth checking on both, which is why nothing here is written to
be dialect-specific.

    ATHENA_TEST_DATABASE_URL=postgresql+asyncpg://athena:athena@localhost:5432/athena_test pytest
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_TMP = tempfile.mkdtemp(prefix="athena-node2-test-")
os.environ.setdefault(
    "ATHENA_DATABASE_URL",
    os.environ.get("ATHENA_TEST_DATABASE_URL", f"sqlite+aiosqlite:///{_TMP}/plm.db"),
)
os.environ.setdefault("ATHENA_ENVIRONMENT", "test")
# 32+ bytes: PyJWT warns below the RFC 7518 minimum for HS256
os.environ.setdefault("ATHENA_JWT_SECRET", "athena-node2-test-secret-0123456789abcdef")
os.environ.setdefault("ATHENA_CAD_VAULT_WEBHOOK_SECRET", "test-webhook-secret")
os.environ.setdefault("ATHENA_LOG_LEVEL", "WARNING")


def _seed_payload() -> dict:
    return json.loads((ROOT / "app" / "data" / "node2_seed.json").read_text())


@pytest.fixture(scope="session")
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(scope="module")
def fresh_db():
    """Drop, recreate and re-seed the database for each test module.

    Module scope rather than session scope on purpose: the ECN tests raise
    changes that legitimately block gates, and a gate test asserting the seeded
    blocking set must not depend on whether the change tests ran first. Sharing
    one database across modules makes the suite order-dependent, which is a
    slower failure to diagnose than the second of setup this costs.
    """
    from app.db import create_all, dispose_engine, drop_all, get_sessionmaker
    from scripts.seed import seed

    async def _setup():
        await drop_all()
        await create_all()
        factory = get_sessionmaker()
        async with factory() as session:
            await seed(session, _seed_payload())
        # Dispose before the loop closes. asyncpg connections are bound to the
        # loop that opened them, so leaving a warm pool here would hand the
        # TestClient's loop connections belonging to a dead one.
        await dispose_engine()

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(_setup())
    finally:
        loop.close()
    return True


@pytest.fixture(scope="module")
def client(fresh_db):
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="session")
def api() -> str:
    from app.config import get_settings

    return get_settings().api_prefix


def _token(client, api: str, username: str) -> dict[str, str]:
    resp = client.post(f"{api}/auth/token", json={"username": username})
    assert resp.status_code == 200, resp.text
    return {"authorization": f"Bearer {resp.json()['access_token']}"}


@pytest.fixture(scope="module")
def gatekeeper(client, api) -> dict[str, str]:
    return _token(client, api, "p.nair")


@pytest.fixture(scope="module")
def engineer(client, api) -> dict[str, str]:
    return _token(client, api, "a.mehta")


@pytest.fixture(scope="module")
def quality(client, api) -> dict[str, str]:
    return _token(client, api, "s.khan")


@pytest.fixture(scope="module")
def change_board(client, api) -> dict[str, str]:
    return _token(client, api, "ccb.chair")


@pytest.fixture(scope="module")
def integration(client, api) -> dict[str, str]:
    return _token(client, api, "cad.vault")


@pytest.fixture(scope="module")
def admin(client, api) -> dict[str, str]:
    return _token(client, api, "admin")


@pytest.fixture(scope="module")
def token_for(client, api):
    """Token factory, so a test can name the account whose privilege it is testing."""
    cache: dict[str, dict[str, str]] = {}

    def _for(username: str) -> dict[str, str]:
        if username not in cache:
            cache[username] = _token(client, api, username)
        return cache[username]

    return _for
