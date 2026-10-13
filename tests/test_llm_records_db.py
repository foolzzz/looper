"""llm_records run recording against a real PostgreSQL.

Runs only when LOOPX_TEST_DATABASE_URL points at a disposable database; the
schema is applied to it and the test's rows are removed afterwards.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

import pytest

DSN = os.environ.get("LOOPX_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not DSN, reason="LOOPX_TEST_DATABASE_URL is not set")


def test_run_is_recorded_end_to_end() -> None:
    import asyncpg

    from loopx.db import llm_records
    from loopx.db.connection import ensure_schema

    async def _run() -> None:
        pool = await asyncpg.create_pool(DSN, min_size=1, max_size=2)
        try:
            await ensure_schema(pool)
            record_id = await llm_records.start_record(
                agent_id="worker-test", task_id="task-test", input_text="do it",
                metadata={"command": ["claude", "--print", "do it"], "cwd": "/tmp/p"}, pool=pool,
            )
            running = await llm_records.get_record(record_id, pool=pool)
            assert running["status"] == "running"
            assert running["metadata"]["command"] == ["claude", "--print", "do it"]

            await llm_records.append_events(record_id, [
                (1, {"type": "assistant", "message": {"content": [{"type": "text", "text": "你好"}]}}),
                (2, {"type": "result", "subtype": "success"}),
            ], pool=pool)
            assert [e["seq"] for e in await llm_records.list_events(record_id, after_seq=1, pool=pool)] == [2]

            await llm_records.update_event(record_id, 2, {"type": "result", "subtype": "success", "n": 2}, pool=pool)
            (updated,) = await llm_records.list_events(record_id, after_seq=1, pool=pool)
            assert updated["event"]["n"] == 2

            await llm_records.finish_record(
                record_id, status="completed", output_text="done", session_id="s-1",
                input_tokens=10, output_tokens=5, cost_usd=0.25, duration_ms=1200,
                model="claude-test", pool=pool,
            )
            done = await llm_records.get_record(record_id, pool=pool)
            assert done["status"] == "completed"
            assert (done["output_text"], done["session_id"], done["cost_usd"]) == ("done", "s-1", 0.25)
            assert done["finished_at"] is not None
            events = await llm_records.list_events(record_id, pool=pool)
            assert events[0]["event"]["message"]["content"][0]["text"] == "你好"

            reviewer_id = await llm_records.start_record(agent_id="reviewer-test", task_id="task-test", pool=pool)
            manager_id = await llm_records.start_record(agent_id="manager", pool=pool)
            planner_id = await llm_records.start_record(agent_id="planner-task-test", task_id="task-test", pool=pool)
            ours = {record_id, reviewer_id, manager_id, planner_id}
            by_role = {
                role: {r["id"] for r in await llm_records.list_records(role=role, pool=pool)} & ours
                for role in ("worker", "reviewer", "manager", "planner")
            }
            assert by_role == {"worker": {record_id}, "reviewer": {reviewer_id}, "manager": {manager_id},
                               "planner": {planner_id}}
            # The Chat page's step agents are Workers and Reviewers only.
            runs = await llm_records.list_task_runs(["task-test"], pool=pool)
            assert {r["agent_id"] for r in runs} == {"worker-test", "reviewer-test"}

            await pool.execute(
                "DELETE FROM llm_records WHERE id = ANY($1::bigint[])", [reviewer_id, manager_id, planner_id],
            )
            await pool.execute("DELETE FROM llm_records WHERE id = $1", record_id)
            assert await llm_records.list_events(record_id, pool=pool) == []  # cascade
        finally:
            await pool.close()

    asyncio.run(_run())


# --- Usage, context and cost (each test in a new schema of its own, dropped afterwards) ---


def _in_fresh_schema(test: Any) -> None:
    """Run ``test(pool)`` against a new schema holding LoopX's tables."""
    import uuid

    import asyncpg

    from loopx.db.connection import ensure_schema

    async def _run() -> None:
        schema = f"loopx_test_{uuid.uuid4().hex[:12]}"
        admin = await asyncpg.connect(DSN)
        await admin.execute(f'CREATE SCHEMA "{schema}"')
        pool = await asyncpg.create_pool(DSN, min_size=1, max_size=2, server_settings={"search_path": schema})
        try:
            await ensure_schema(pool)
            await test(pool)
        finally:
            await pool.close()
            await admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
            await admin.close()

    asyncio.run(_run())


async def _run_record(pool: Any, agent_id: str, *, task_id: str | None = None, cost: float | None = None,
                      status: str = "completed", session_id: str | None = None, context: int | None = None,
                      compactions: list[dict[str, Any]] | None = None, at: str | None = None) -> int:
    from loopx.db import llm_records

    record_id = await llm_records.start_record(agent_id=agent_id, task_id=task_id, session_id=session_id, pool=pool)
    if status != "running":
        await llm_records.finish_record(record_id, status=status, cost_usd=cost, context_tokens=context,
                                        compactions=compactions if compactions is not None else [], pool=pool)
    if at is not None:
        await pool.execute("UPDATE llm_records SET created_at = $2::text::timestamptz WHERE id = $1", record_id, at)
    return record_id


def test_a_runs_tokens_context_and_compactions_are_read_back() -> None:
    from loopx.db import llm_records

    async def test(pool: Any) -> None:
        record_id = await llm_records.start_record(agent_id="manager", pool=pool)
        compacted = [{"trigger": "auto", "pre_tokens": 25120, "post_tokens": 1359, "duration_ms": 13135}]
        await llm_records.finish_record(
            record_id, status="completed", input_tokens=7985, output_tokens=1342, cache_read_tokens=33420,
            cache_creation_tokens=2449, cost_usd=0.022812, context_tokens=17395, compactions=compacted, pool=pool,
        )
        record = await llm_records.get_record(record_id, pool=pool)
        assert (record["input_tokens"], record["output_tokens"], record["cache_read_tokens"],
                record["cache_creation_tokens"], record["context_tokens"]) == (7985, 1342, 33420, 2449, 17395)
        assert record["compactions"] == compacted
        (listed,) = await llm_records.list_records(agent_id="manager", pool=pool)
        assert listed["compactions"] == compacted and listed["context_tokens"] == 17395
        # A run recorded before these columns existed says nothing about them.
        old_id = await llm_records.start_record(agent_id="manager", pool=pool)
        await pool.execute("UPDATE llm_records SET status = 'completed' WHERE id = $1", old_id)
        old = await llm_records.get_record(old_id, pool=pool)
        assert (old["cache_read_tokens"], old["context_tokens"], old["compactions"]) == (None, None, None)
        # Cache reads over a long run can pass 2^31.
        big = await llm_records.start_record(agent_id="worker-x", pool=pool)
        await llm_records.finish_record(big, status="completed", cache_read_tokens=3_000_000_000, pool=pool)
        assert (await llm_records.get_record(big, pool=pool))["cache_read_tokens"] == 3_000_000_000

    _in_fresh_schema(test)

