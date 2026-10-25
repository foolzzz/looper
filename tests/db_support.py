"""What the PostgreSQL tests share: the disposable database's URL, a schema of its own for a test, and a wait.

Import this module from a test file; do not copy these into it, or import them from another test file. A test
module imports it after ``conftest`` has pointed ``LOOPER_TEST_DATABASE_URL`` at the xdist worker's database, so
``DSN`` is the worker's.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

import pytest

DSN = os.environ.get("LOOPER_TEST_DATABASE_URL")


@asynccontextmanager
async def fresh_schema(*, max_size: int = 3) -> AsyncIterator[Any]:
    """A pool whose tables live in a new, empty schema, dropped afterwards."""
    import asyncpg

    schema = f"looper_test_{uuid.uuid4().hex[:12]}"
    admin = await asyncpg.connect(DSN)
    await admin.execute(f'CREATE SCHEMA "{schema}"')
    pool = await asyncpg.create_pool(DSN, min_size=1, max_size=max_size, server_settings={"search_path": schema})
    try:
        yield pool
    finally:
        await pool.close()
        await admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
        await admin.close()


@asynccontextmanager
async def fresh_connection(*, command_timeout: float = 15) -> AsyncIterator[Any]:
    """A direct connection in a fresh schema, for migration and constraint tests."""
    import asyncpg

    conn = await asyncpg.connect(DSN, command_timeout=command_timeout)
    schema = f"looper_test_{uuid.uuid4().hex}"
    await conn.execute(f'CREATE SCHEMA "{schema}"')
    await conn.execute(f'SET search_path TO "{schema}"')
    try:
        yield conn
    finally:
        await conn.execute(f'DROP SCHEMA "{schema}" CASCADE')
        await conn.close()


def run_db(
    test: Callable[[Any], Awaitable[None]], monkeypatch: pytest.MonkeyPatch | None = None, *,
    before: str | Callable[[Any], Awaitable[None]] | None = None, setup: Callable[[Any], Awaitable[None]] | None = None,
    timeout: float | None = None, max_size: int = 3,
) -> None:
    """Run ``test(pool)`` on a new, migrated schema. With ``monkeypatch`` the pool is also the server's pool.
    ``before`` (SQL, or a coroutine function of the pool) runs ahead of the migration, ``setup(pool)`` after it and
    ahead of the test; with ``timeout`` the whole run is cut off after that many seconds."""
    if not DSN:
        pytest.skip("LOOPER_TEST_DATABASE_URL is not set")
    from looper.db import connection

    async def _main() -> None:
        async with fresh_schema(max_size=max_size) as pool:
            if callable(before):
                await before(pool)
            elif before:
                await pool.execute(before)
            await connection.ensure_schema(pool)
            if monkeypatch is not None:
                monkeypatch.setattr(connection, "_pool", pool)
            if setup is not None:
                await setup(pool)
            await test(pool)

    asyncio.run(_main() if timeout is None else asyncio.wait_for(_main(), timeout))


def run_with_channel_configs(test: Callable[[Any], Awaitable[None]], monkeypatch: pytest.MonkeyPatch) -> None:
    """``run_db`` on a schema seeded the way ``looper serve`` seeds the channel configs."""
    from looper.agents.serve import seed_channel_configs

    run_db(test, monkeypatch, setup=seed_channel_configs)


def run_with_models(test: Callable[[Any], Awaitable[None]], monkeypatch: pytest.MonkeyPatch) -> None:
    """Run a registry test with two accounts and their protocol endpoints."""
    from looper.agents import profile_registry as service

    async def setup(pool):
        monkeypatch.setattr(service, "_login_modules", lambda: {})
        await pool.execute("""
            INSERT INTO llm_providers (id, name, vendor_type, auth) VALUES
              ('prov-test', 'Test account', 'custom', 'api_key'),
              ('prov-other', 'Other account', 'custom', 'api_key');
            INSERT INTO llm_provider_endpoints (provider_id, protocol, base_url) VALUES
              ('prov-test', 'anthropic-messages', 'https://messages.example.invalid'),
              ('prov-test', 'openai-chat', 'https://chat.example.invalid'),
              ('prov-other', 'anthropic-messages', 'https://other.example.invalid');
        """)

    run_db(test, monkeypatch, setup=setup, timeout=30)


async def until(check: Callable[[], Any], what: str = "the condition", timeout: float = 15.0) -> None:
    """Wait until ``check()`` holds, looking every 10 ms; fail after ``timeout`` seconds."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not check():
        assert loop.time() < deadline, f"timed out waiting for {what}"
        await asyncio.sleep(0.01)


async def call_v1(server, path, body):
    """Exercise v1 dispatch on existing service fixtures; retain their decoded legacy assertions."""
    import json
    from looper.agents import api_v1
    from looper.agents.ops.clients import ClientOps
    from looper.agents.ops.service import Platform

    if server._client_ops is None:
        server._client_ops = ClientOps(Platform(bus=getattr(server._supervisor, "bus", None), task_router=server._task_router))
    status, answer = await server._answer_v1("POST", path, api_v1.find("POST", path), {},
                                             json.dumps(body).encode(), api_v1.OPERATOR)
    return int(status.split()[0]), answer.get("result", {"error": answer.get("error", {}).get("message", "")})
