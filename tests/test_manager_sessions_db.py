"""The Manager group/DM session table: its store, what the schema refuses and the upgrade of a database without it."""

import asyncpg
import pytest

from looper.db import connection, manager_sessions
from tests.db_support import DSN, dropped_tables, run_db

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")


def _row(chat: str, session_id: str = "sess-1", **extra):
    return {"manager_id": "manager", "session_key": f"group:channel-lark-1:{chat}", "kind": "group",
            "channel_id": "channel-lark-1", "chat": chat, "session_id": session_id,
            "session_totals": {"cost_usd": 0.5, "turns": 2}, "runtime_type": "claude-code",
            "profile_id": "profile-1", "provider_id": "provider-1", "clean_start": False,
            "pending_handoff": None, **extra}


def test_a_session_is_stored_replaced_listed_touched_and_deleted(monkeypatch):
    async def check(pool):
        first, second = _row("oc_synthetic_1", context_tokens=1234, last_turn_cost_usd=0.25), _row("oc_synthetic_2")
        await manager_sessions.save(first)
        stored = await manager_sessions.get("manager", first["session_key"])
        assert {k: stored[k] for k in first} == first
        assert stored["created_at"] and stored["last_active_at"]
        await manager_sessions.save({k: v for k, v in first.items() if k not in ("context_tokens",
                                                                                  "last_turn_cost_usd")}
                                    | {"session_id": "sess-3"})
        stored = await manager_sessions.get("manager", first["session_key"])
        assert stored["session_id"] == "sess-3"
        assert (stored["context_tokens"], stored["last_turn_cost_usd"]) == (1234, 0.25)  # omitted: kept
        await manager_sessions.save(second)
        assert [r["chat"] for r in await manager_sessions.list("manager")] == ["oc_synthetic_2", "oc_synthetic_1"]
        before = (await manager_sessions.get("manager", first["session_key"]))["last_active_at"]
        await manager_sessions.touch("manager", first["session_key"])
        assert (await manager_sessions.get("manager", first["session_key"]))["last_active_at"] > before
        assert [r["chat"] for r in await manager_sessions.list("manager")] == ["oc_synthetic_1", "oc_synthetic_2"]
        await manager_sessions.delete("manager", first["session_key"])
        assert await manager_sessions.get("manager", first["session_key"]) is None
        assert await manager_sessions.get("manager", "group:none:none") is None

    run_db(check, monkeypatch, timeout=30)


@pytest.mark.parametrize("override", [
    {"session_key": "group:channel-lark-1:other"},   # the key is not kind:channel_id:chat
    {"kind": "main", "session_key": "main:channel-lark-1:oc_synthetic_1"},   # main lives in agent_sessions
])
def test_the_schema_refuses_a_key_that_does_not_match_the_session(monkeypatch, override):
    async def check(pool):
        with pytest.raises(asyncpg.CheckViolationError):
            await manager_sessions.save(_row("oc_synthetic_1") | override)

    run_db(check, monkeypatch, timeout=20)


def test_a_database_without_the_table_gets_it_on_upgrade(monkeypatch):
    async def check(pool):
        async with dropped_tables(pool, "manager_sessions"):
            await pool.execute("DELETE FROM schema_migrations WHERE name = 'manager_sessions'")
            async with pool.acquire() as conn:
                assert await connection.schema_change_pending(conn)
            await connection.ensure_schema(pool)
        async with pool.acquire() as conn:
            assert not await connection.schema_change_pending(conn)
        await manager_sessions.save(_row("oc_synthetic_1"))
        await connection.ensure_schema(pool)  # and a second upgrade changes nothing
        assert len(await manager_sessions.list("manager")) == 1

    run_db(check, monkeypatch, timeout=30)
