"""Readback of interrupted records and resumed transcript positions."""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from typing import Any

import pytest

from looper.agents.claude_cli import ClaudeRunRecorder
from looper.db import llm_records
from tests.db_support import DSN, run_db

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")


def test_resume_appends_after_the_last_persisted_event(monkeypatch: pytest.MonkeyPatch) -> None:
    async def run(pool: Any) -> None:
        record_id = await llm_records.start_record(agent_id="worker-kept")
        assert await llm_records.max_event_seq(record_id) == 0
        await llm_records.append_events(record_id, [(1, {"type": "system"}), (2, {"type": "assistant"})])
        state = await llm_records.record_state(record_id)
        assert state["status"] == "running"
        assert isinstance(state["created_at"], datetime)
        recorder = await ClaudeRunRecorder.resume(
            record_id, agent_id="worker-kept", task_id=None, command=["claude"], cwd="/tmp", step_id=None,
            launch=SimpleNamespace(price=None, model="m", max_budget_usd=None), max_budget_usd=None,
        )
        async with recorder:
            await recorder.record({"type": "result", "subtype": "success"})
            recorder.detach()
        records = await pool.fetch("SELECT id, status FROM llm_records")
        assert [(r["id"], r["status"]) for r in records] == [(record_id, "running")]
        assert await pool.fetchval("SELECT array_agg(seq ORDER BY seq) FROM llm_record_events") == [1, 2, 3]
        assert await llm_records.record_state(record_id + 1) is None

    run_db(run, monkeypatch, timeout=15)


@pytest.mark.parametrize("with_output", [False, True])
def test_interrupting_an_unadoptable_record_preserves_its_last_output(
    monkeypatch: pytest.MonkeyPatch, with_output: bool,
) -> None:
    async def run(pool: Any) -> None:
        record_id = await llm_records.start_record(agent_id="worker-kept")
        if with_output:
            await pool.execute("UPDATE llm_records SET last_output_at=created_at + interval '2 seconds' WHERE id=$1",
                               record_id)
        before = await pool.fetchrow("SELECT created_at, last_output_at FROM llm_records WHERE id=$1", record_id)
        await llm_records.end_record_interrupted(record_id, "adoption failed")
        after = await pool.fetchrow("SELECT status, finished_at, error FROM llm_records WHERE id=$1", record_id)
        assert dict(after) == {"status": "interrupted", "finished_at": before["last_output_at"] or before["created_at"],
                               "error": "adoption failed"}
        await llm_records.end_record_interrupted(record_id, "another reason")
        assert await pool.fetchval("SELECT error FROM llm_records WHERE id=$1", record_id) == "adoption failed"

    run_db(run, monkeypatch, timeout=15)
