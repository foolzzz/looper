"""A Claude Code stream with a Skill call, recorded as a Worker's run and read back by the usage query.

The stream is the one ``tests/test_skill_invocation_stream.py`` captured from the real CLI (Claude Code 2.1.330,
scripted loopback API, paths replaced). Runs only when LOOPER_TEST_DATABASE_URL points at a disposable database.
"""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from tests.db_support import DSN, run_db

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")
STREAM = Path(__file__).parent / "fixtures" / "skill_invocation" / "claude_skill_call.jsonl"


def test_the_worker_recorder_keeps_the_skill_call_intact_and_the_usage_query_reads_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from looper.agents import skill_usage
    from looper.agents.agent_settings import AgentSettings, Launch
    from looper.agents.claude_cli import ClaudeRunRecorder
    from looper.agents.profile_registry import builtin_resolved_profile
    from looper.db import llm_records
    from tests.test_skill_usage_db import NOW, World, _world

    events = [json.loads(line) for line in STREAM.read_text().splitlines()]
    sent = next(b for e in events if e["type"] == "assistant" for b in e["message"]["content"]
                if b["type"] == "tool_use")
    assert sent["name"] == "Skill" and sent["input"] == {"skill": "project:probe-skill"}

    async def _test(pool: Any) -> None:
        world: World = await _world(pool, name="probe-skill")
        task = await world.task()
        launch = Launch(AgentSettings(profile=builtin_resolved_profile()))
        # The Worker's recorder: whole process, no ``keep`` filter.
        async with ClaudeRunRecorder(agent_id="worker-probe", task_id=task, command=["claude"], cwd="/scratch/cwd",
                                     input_text="use the skill", launch=launch, whole_process=True) as recorder:
            for event in events:
                await recorder.record(event)
            result = next(e for e in events if e["type"] == "result")
            await recorder.finish(output_text=None, result_event=result, error=None, outcome="completed")
            record_id = recorder.record_id
        stored = [json.loads(row["event"]) if isinstance(row["event"], str) else row["event"] for row in
                  await pool.fetch("SELECT event FROM llm_record_events WHERE record_id = $1 ORDER BY seq", record_id)]
        calls = [b for e in stored if e["type"] == "assistant" for b in e["message"]["content"]
                 if b["type"] == "tool_use"]
        assert calls == [sent]  # the block, intact: name, id and input

        # Give the stored record what a launch records, and read it with the query.
        library = {"project_id": world.project_id, "items": world.items()}
        at = NOW - timedelta(days=1)
        attempt = await world.attempt(task, "s1", 1, "first", "accept", at)
        await pool.execute(
            "UPDATE llm_records SET metadata = COALESCE(metadata, '{}'::jsonb) || $2::jsonb, created_at = $3, "
            "attempt_id = $4 WHERE id = $1", record_id, json.dumps({"library": library, "profile": {"runtime": {"type": "claude-code"}}}),
            at, attempt)
        row = (await skill_usage.usage(world.project_id, now=NOW))[0]
        assert row.current["loaded_attempts"] == 1
        assert row.current["invoked"] == {"count": 1, "n": 1, "unknown": 0, "rate": None}
        assert await llm_records.get_record(record_id) is not None

    run_db(_test, monkeypatch)
