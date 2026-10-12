"""llm_records run recording against a real PostgreSQL.

Runs only when LOOPX_TEST_DATABASE_URL points at a disposable database; the
schema is applied to it and the test's rows are removed afterwards.
"""

from __future__ import annotations

import asyncio
import os

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

            await pool.execute("DELETE FROM llm_records WHERE id = $1", record_id)
            assert await llm_records.list_events(record_id, pool=pool) == []  # cascade
        finally:
            await pool.close()

    asyncio.run(_run())
