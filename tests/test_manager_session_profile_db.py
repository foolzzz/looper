"""The Agent Profile chosen for the Manager's current session (``agent_sessions.selected_profile_id``).

The database tests run only when LOOPER_TEST_DATABASE_URL points at a disposable database.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

import asyncpg
import pytest

from tests.test_catalog_db import DSN, _fresh_schema

needs_db = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")


def _run(test: Callable[[Any], Awaitable[None]], monkeypatch: pytest.MonkeyPatch) -> None:
    from looper.db import connection

    async def _main() -> None:
        async with _fresh_schema() as pool:
            await connection.ensure_schema(pool)
            monkeypatch.setattr(connection, "_pool", pool)
            await test(pool)

    asyncio.run(_main())


async def _profile_id(pool: Any) -> str:
    return await pool.fetchval("SELECT id FROM agent_profiles WHERE builtin ORDER BY id LIMIT 1")


@needs_db
def test_schema_applies_twice_with_column_and_constraint(monkeypatch: pytest.MonkeyPatch) -> None:
    from looper.db import connection

    async def body(pool: Any) -> None:
        await connection.ensure_schema(pool)
        assert await pool.fetchval(
            "SELECT count(*) FROM information_schema.columns WHERE table_schema = current_schema() "
            "AND table_name = 'agent_sessions' AND column_name = 'selected_profile_id'") == 1
        assert await pool.fetchval(
            "SELECT count(*) FROM pg_constraint WHERE conname = 'agent_sessions_selected_profile_manager' "
            "AND conrelid = 'agent_sessions'::regclass") == 1

    _run(body, monkeypatch)


@needs_db
def test_select_profile_then_get_session(monkeypatch: pytest.MonkeyPatch) -> None:
    from looper.db import agent_sessions

    async def body(pool: Any) -> None:
        pid = await _profile_id(pool)
        await agent_sessions.select_profile("manager", pid)
        saved = await agent_sessions.get_session("manager")
        assert saved["selected_profile_id"] == pid and saved["session_id"] is None
        assert saved["session_totals"] == {}

    _run(body, monkeypatch)


@needs_db
def test_save_session_and_select_profile_leave_each_other(monkeypatch: pytest.MonkeyPatch) -> None:
    from looper.db import agent_sessions

    async def body(pool: Any) -> None:
        pid = await _profile_id(pool)
        await agent_sessions.select_profile("manager", pid)
        await agent_sessions.save_session("manager", "s-1", {"cost": 1})
        saved = await agent_sessions.get_session("manager")
        assert (saved["selected_profile_id"], saved["session_id"]) == (pid, "s-1")
        await agent_sessions.select_profile("manager", None)
        saved = await agent_sessions.get_session("manager")
        assert (saved["selected_profile_id"], saved["session_id"]) == (None, "s-1")
        assert saved["session_totals"] == {"cost": 1}

    _run(body, monkeypatch)


@needs_db
def test_only_the_manager_can_select_a_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    from looper.db import agent_sessions

    async def body(pool: Any) -> None:
        with pytest.raises(asyncpg.CheckViolationError):
            await agent_sessions.select_profile("planner-task-x", await _profile_id(pool))

    _run(body, monkeypatch)


@needs_db
def test_selected_profile_cannot_be_deleted(monkeypatch: pytest.MonkeyPatch) -> None:
    from looper.db import agent_sessions

    async def body(pool: Any) -> None:
        pid = await _profile_id(pool)
        await agent_sessions.select_profile("manager", pid)
        with pytest.raises(asyncpg.ForeignKeyViolationError):
            await pool.execute("DELETE FROM agent_profiles WHERE id = $1", pid)

    _run(body, monkeypatch)


@needs_db
def test_both_writes_share_one_transaction(monkeypatch: pytest.MonkeyPatch) -> None:
    from looper.db import agent_sessions, connection

    async def body(pool: Any) -> None:
        pid = await _profile_id(pool)
        async with connection.transaction() as conn:
            await agent_sessions.save_session("manager", "s-1", {}, pool=conn)
            await agent_sessions.select_profile("manager", pid, pool=conn)
        saved = await agent_sessions.get_session("manager")
        assert (saved["session_id"], saved["selected_profile_id"]) == ("s-1", pid)

        with pytest.raises(RuntimeError):
            async with connection.transaction() as conn:
                await agent_sessions.save_session("manager", "s-2", {}, pool=conn)
                await agent_sessions.select_profile("manager", None, pool=conn)
                raise RuntimeError("boom")
        saved = await agent_sessions.get_session("manager")
        assert (saved["session_id"], saved["selected_profile_id"]) == ("s-1", pid)

    _run(body, monkeypatch)


def test_agent_db_store_keeps_selected_profile_across_save_session(agent_db: Any) -> None:
    from looper.db import agent_sessions

    async def body() -> None:
        await agent_sessions.select_profile("manager", "p1")
        await agent_sessions.save_session("manager", "s-1", {})
        saved = await agent_sessions.get_session("manager")
        assert saved["selected_profile_id"] == "p1" and saved["session_id"] == "s-1"

    asyncio.run(body())
