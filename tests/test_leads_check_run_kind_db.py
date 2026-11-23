"""Leads-check records have their own kind, including records written before the additive upgrade."""

from typing import Any

import asyncpg
import pytest

from looper.db import connection, llm_records
from tests.db_support import DSN, run_db

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")

_CHECK = "llm_records_run_kind_check"
_MARKERS = ("leads_check_run_kind", "leads_check_run_kind_online")


async def _validated(pool: Any) -> bool:
    return await pool.fetchval(
        "SELECT convalidated FROM pg_constraint WHERE conrelid = 'llm_records'::regclass AND conname = $1", _CHECK)


async def _markers(pool: Any) -> set[str]:
    return {row["name"] for row in await pool.fetch(
        "SELECT name FROM schema_migrations WHERE name = ANY($1::text[])", list(_MARKERS))}


def test_leads_check_records_use_their_own_kind_and_old_null_rows_are_read_without_a_rewrite(monkeypatch):
    async def test(pool):
        assert await _validated(pool) and not await _markers(pool)
        check = await llm_records.start_record(agent_id="leads_check:synthetic-check")
        processor = await llm_records.start_record(agent_id="processor:synthetic-subscription")
        explicit = await llm_records.start_record(agent_id="synthetic-run", run_kind="leads_check")
        legacy = await pool.fetchval("INSERT INTO llm_records (agent_id, run_kind) "
                                     "VALUES ('leads_check:synthetic-legacy', NULL) RETURNING id")
        rows = {row["id"]: (row["run_kind"], row["effective_kind"]) for row in await pool.fetch(
            f"SELECT id, run_kind, {llm_records.RUN_KIND} AS effective_kind FROM llm_records")}
        assert rows == {check: ("leads_check", "leads_check"), processor: ("processor", "processor"),
                        explicit: ("leads_check", "leads_check"), legacy: (None, "leads_check")}
        with pytest.raises(asyncpg.CheckViolationError):
            await pool.execute("INSERT INTO llm_records (agent_id, run_kind) VALUES ('synthetic', 'unknown')")

    run_db(test, monkeypatch)


def test_an_existing_kind_constraint_is_widened_without_a_scan_then_validated_online_once(monkeypatch):
    async def stopped(conn):
        return None

    async def test(pool):
        fresh = await pool.fetchval("SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                                    "WHERE conrelid = 'llm_records'::regclass AND conname = $1", _CHECK)
        kinds = ", ".join(f"'{kind}'" for kind in llm_records.RUN_KINDS if kind != "leads_check")
        await pool.execute(f"ALTER TABLE llm_records DROP CONSTRAINT {_CHECK}, "
                           f"ADD CONSTRAINT {_CHECK} CHECK (run_kind IN ({kinds}))")
        await pool.execute("DELETE FROM schema_migrations WHERE name = ANY($1::text[])", list(_MARKERS))
        old = await pool.fetchval("INSERT INTO llm_records (agent_id, run_kind) "
                                  "VALUES ('leads_check:synthetic-legacy', NULL) RETURNING id")
        async with pool.acquire() as conn:
            assert await connection.schema_change_pending(conn)
        with monkeypatch.context() as patch:
            patch.setattr(connection, "_finish_online", stopped)
            await connection.ensure_schema(pool)
        assert not await _validated(pool) and await _markers(pool) == {_MARKERS[0]}
        # NOT VALID constrains new writes immediately; the old NULL record is untouched.
        new = await llm_records.start_record(agent_id="leads_check:synthetic-new")
        assert await pool.fetchval("SELECT run_kind FROM llm_records WHERE id = $1", new) == "leads_check"
        assert await pool.fetchval("SELECT run_kind FROM llm_records WHERE id = $1", old) is None
        async with pool.acquire() as conn:
            assert not await connection.schema_change_pending(conn)
            await connection._finish_online(conn)
        assert await _validated(pool) and await _markers(pool) == set(_MARKERS)
        assert await pool.fetchval("SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                                   "WHERE conrelid = 'llm_records'::regclass AND conname = $1", _CHECK) == fresh
        await connection.ensure_schema(pool)
        assert await _validated(pool) and await _markers(pool) == set(_MARKERS)
        assert await pool.fetchval("SELECT run_kind FROM llm_records WHERE id = $1", old) is None

    run_db(test, monkeypatch)


def test_subagents_upgrade_preserves_an_already_migrated_leads_check_kind():
    import asyncio

    from tests.db_support import fresh_connection
    from tests.subagents_schema_support import ensure_before_platform_subagents

    async def test():
        async with fresh_connection(command_timeout=60) as conn:
            await ensure_before_platform_subagents(conn)
            await conn.execute("INSERT INTO schema_migrations (name) SELECT unnest($1::text[])", list(_MARKERS))
            old = await conn.fetchval("INSERT INTO llm_records (agent_id, run_kind) "
                                      "VALUES ('leads_check:synthetic-legacy', 'leads_check') RETURNING id")
            witness = await conn.fetchrow("SELECT ctid::text, xmin::text FROM llm_records WHERE id=$1", old)
            await connection.ensure_schema(conn)
            assert await conn.fetchrow("SELECT ctid::text, xmin::text FROM llm_records WHERE id=$1", old) == witness
            for kind in ("leads_check", "explorer"):
                await llm_records.start_record(agent_id="synthetic-upgrade", run_kind=kind, pool=conn)
            assert await _validated(conn)
            assert not await connection.schema_change_pending(conn)
            await connection.ensure_schema(conn)
            assert await _validated(conn)

    asyncio.run(test())
