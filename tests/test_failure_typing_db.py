"""Failure classes in llm_records, the engine settings and the failure counts, on disposable PostgreSQL."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import asyncpg
import pytest

from tests.db_support import DSN, run_db, v1_api

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")


async def _failed(pool, agent_id, failure_class, failure_cause, *, task_id="task-f", runtime="claude-code",
                  status="failed", days_ago=0):
    from looper.db import llm_records

    record_id = await llm_records.start_record(agent_id=agent_id, task_id=task_id, input_text="x",
                                               metadata={"profile": {"runtime": {"type": runtime}}}, pool=pool)
    await llm_records.finish_record(record_id, status=status, error="boom" if status == "failed" else None,
                                    failure_class=failure_class, failure_cause=failure_cause,
                                    metadata={"failure": {"signal": "typed"}} if failure_class else None, pool=pool)
    if days_ago:
        await pool.execute("UPDATE llm_records SET created_at = created_at - $2::interval WHERE id = $1",
                           record_id, timedelta(days=days_ago))
    return record_id


def test_the_columns_hold_only_a_cause_with_its_own_class():
    from looper.db import llm_records

    async def body(pool):
        record_id = await _failed(pool, "worker-a", "quota", "usage_limit")
        row = await llm_records.get_record(record_id, pool=pool)
        assert (row["failure_class"], row["failure_cause"]) == ("quota", "usage_limit")
        assert row["metadata"]["failure"] == {"signal": "typed"}
        plain = await _failed(pool, "worker-b", None, None, status="completed")
        assert (await llm_records.get_record(plain, pool=pool))["failure_class"] is None
        for pair in (("quota", "timeout"), ("quota", None), (None, "unknown"), ("other", "unknown")):
            with pytest.raises(asyncpg.CheckViolationError):
                await _failed(pool, "worker-c", *pair)

    run_db(body, timeout=30)


def test_an_existing_database_gets_the_columns_online_and_keeps_its_rows():
    from looper.db import connection

    async def body(pool):
        # As a database from before failure typing: no columns, no index, no markers; one old run.
        await pool.execute("DROP INDEX idx_llm_records_failures")
        await pool.execute("ALTER TABLE llm_records DROP CONSTRAINT llm_records_failure_check, "
                           "DROP COLUMN failure_class, DROP COLUMN failure_cause")
        await pool.execute("DELETE FROM schema_migrations WHERE name IN ('failure_typing', 'failure_typing_online')")
        old = await pool.fetchval("INSERT INTO llm_records (agent_id, status) VALUES ('worker-old', 'failed') "
                                  "RETURNING id")
        async with pool.acquire() as conn:
            assert await connection.schema_change_pending(conn)
        await connection.ensure_schema(pool)
        assert await pool.fetchval("SELECT failure_class IS NULL AND failure_cause IS NULL FROM llm_records "
                                   "WHERE id = $1", old)
        assert await pool.fetchval("SELECT convalidated FROM pg_constraint WHERE conname = 'llm_records_failure_check' "
                                   "AND conrelid = to_regclass('llm_records')")
        assert await pool.fetchval("SELECT indisvalid FROM pg_index WHERE indexrelid = "
                                   "to_regclass('idx_llm_records_failures')")
        markers = {row["name"] for row in await pool.fetch("SELECT name FROM schema_migrations")}
        assert {"failure_typing", "failure_typing_online"} <= markers
        async with pool.acquire() as conn:
            assert not await connection.schema_change_pending(conn)
        await connection.ensure_schema(pool)  # a second start changes nothing

    run_db(body, timeout=60)


def test_a_fresh_database_records_no_failure_typing_marker():
    async def body(pool):
        names = {row["name"] for row in await pool.fetch("SELECT name FROM schema_migrations")}
        assert not names & {"failure_typing", "failure_typing_online"}

    run_db(body, timeout=30)


def test_engine_settings_switch_failure_typing(tmp_path, monkeypatch):
    from looper.agents import failure_handling

    async def body(pool):
        service = failure_handling.FailureHandlingService(pool=pool)
        monkeypatch.setattr(failure_handling, "_service", service)
        try:
            async with v1_api(tmp_path, pool) as call:
                status, answer = await call("engine_settings.get")
                assert status == 200 and answer["result"] == {"failure_typing": {
                    "value": "off", "source": "built-in", "built_in": "off"}}
                status, answer = await call("engine_settings.get", client=True)
                assert status == 403
                for mode in ("record", "act", None):
                    status, answer = await call("engine_settings.update", {"failure_typing": mode})
                    assert status == 202
                    status, answer = await call("confirm", {"confirmation_id": answer["confirmation_id"]})
                    assert status == 200 and answer["result"]["failure_typing"]["value"] == (mode or "off")
                    assert service.mode.value == (mode or "off")
                assert await pool.fetchval("SELECT count(*) FROM system_settings WHERE key = 'engine.failure_typing'") == 0
                status, answer = await call("engine_settings.update", {"failure_typing": "sometimes"})
                assert status == 400
                status, answer = await call("engine_settings.update", {})
                assert status == 400
        finally:
            await service.close()

    run_db(body, monkeypatch, timeout=60)


def test_failure_counts_and_run_reads(tmp_path, monkeypatch):
    async def body(pool):
        await _failed(pool, "worker-1", "quota", "usage_limit")
        await _failed(pool, "worker-2", "transient", "connection_failed", runtime="codex")
        await _failed(pool, "reviewer-1", "judgment", "report", status="completed")
        await _failed(pool, "planner-task-f", "quota", "usage_limit")
        await _failed(pool, "worker-3", "environment", "runtime_missing", days_ago=10)  # outside the default week
        unclassified = await _failed(pool, "worker-4", None, None)
        async with v1_api(tmp_path, pool) as call:
            status, answer = await call("runs.failures")
            assert status == 200, answer
            stats = answer["result"]
            assert stats["failure_typing"] == "off"
            assert stats["runs"] == 4
            assert stats["by_class"] == {"quota": 2, "transient": 1, "environment": 0, "judgment": 1}
            # The Workers' and Reviewers' failures go to their Planner: two of their three were not a judgment.
            assert stats["steps"] == {"failed": 3, "not_judgment": 2}
            today = datetime.now(timezone.utc).date().isoformat()
            assert {(row["run_kind"], row["runtime"], row["failure_cause"], row["runs"]) for row in stats["rows"]} == {
                ("worker", "claude-code", "usage_limit", 1), ("worker", "codex", "connection_failed", 1),
                ("reviewer", "claude-code", "report", 1), ("planner", "claude-code", "usage_limit", 1)}
            assert {row["day"] for row in stats["rows"]} == {today}
            status, answer = await call("runs.failures", {"days": 30})
            assert answer["result"]["runs"] == 5
            status, answer = await call("runs.failures", {"days": 0})
            assert status == 400
            status, answer = await call("runs.get", {"record_id": unclassified})
            assert answer["result"]["record"]["failure_class"] is None
            status, answer = await call("runs.list", {"agent_id": "worker-1"})
            assert [(r["failure_class"], r["failure_cause"]) for r in answer["result"]["records"]] == [
                ("quota", "usage_limit")]

    run_db(body, monkeypatch, timeout=60)
