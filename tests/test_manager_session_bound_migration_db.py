"""Nullable Manager turn measurements on fresh and upgraded PostgreSQL schemas."""

import pytest

from looper.db import agent_sessions, connection
from tests.db_support import DSN, run_db

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")


def test_fresh_measurements_replace_unknown_values_without_changing_the_session(monkeypatch):
    async def check(pool):
        await agent_sessions.save_session("manager", "retained", {"cost_usd": 1}, pool=pool)
        await agent_sessions.save_measurements("manager", context_tokens=120_000, last_turn_cost_usd=.7,
                                               pool=pool)
        row = await pool.fetchrow("SELECT * FROM agent_sessions WHERE agent_id='manager'")
        assert row["session_id"] == "retained"
        assert (row["context_tokens"], row["last_turn_cost_usd"]) == (120_000, .7)
        await agent_sessions.save_measurements("manager", context_tokens=None, last_turn_cost_usd=None,
                                               pool=pool)
        row = await pool.fetchrow("SELECT * FROM agent_sessions WHERE agent_id='manager'")
        assert row["context_tokens"] is None and row["last_turn_cost_usd"] is None

    run_db(check, monkeypatch)


@pytest.mark.parametrize("remaining", [None, "context_tokens"])
def test_old_database_adds_missing_nullable_columns_idempotently(monkeypatch, remaining):
    async def check(pool):
        await agent_sessions.save_session("manager", "retained", {}, pool=pool)
        if remaining:
            await pool.execute("ALTER TABLE agent_sessions ADD COLUMN IF NOT EXISTS context_tokens INTEGER")
        for column in ("context_tokens", "last_turn_cost_usd"):
            if column != remaining:
                await pool.execute(f"ALTER TABLE agent_sessions DROP COLUMN IF EXISTS {column}")
        async with pool.acquire() as conn:
            assert await connection.schema_change_pending(conn)
        await connection.ensure_schema(pool)
        await connection.ensure_schema(pool)
        row = await pool.fetchrow("SELECT * FROM agent_sessions WHERE agent_id='manager'")
        assert row["session_id"] == "retained"
        assert row["context_tokens"] is None and row["last_turn_cost_usd"] is None
        columns = await pool.fetch(
            "SELECT is_nullable, column_default FROM information_schema.columns "
            "WHERE table_schema=current_schema() AND table_name='agent_sessions' "
            "AND column_name IN ('context_tokens','last_turn_cost_usd')")
        assert len(columns) == 2
        assert all(row["is_nullable"] == "YES" and row["column_default"] is None for row in columns)
        assert await pool.fetchval("SELECT count(*) FROM schema_migrations WHERE name='manager_session_bounds'") == 1

    run_db(check, monkeypatch)
