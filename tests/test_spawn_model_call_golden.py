"""The generic tool-less spawn's launch, records and failures against main."""

from __future__ import annotations

import asyncio
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any

import pytest

from tests.db_support import DSN, run_db
from tests.model_call_support import assert_golden, golden_cli, read_launches, table_rows

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")


@pytest.mark.parametrize("mode", ["success", "runtime", "budget", "unavailable", "timeout", "refused", "rate_limited"])
def test_generic_spawn_keeps_its_launch_records_and_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, mode: str,
) -> None:
    from looper.agents import spawn
    from looper.agents.agent_settings import AgentType, resolve
    from looper.agents.profile_registry import builtin_resolved_profile
    from looper.agents.system_prompt import SystemPrompt
    from looper.db import llm_records

    executable = golden_cli(monkeypatch, tmp_path, runs=[{"mode": mode, "reply": "Use a bounded process pool."}])

    async def test(pool: Any) -> None:
        await pool.execute("INSERT INTO projects (id, name) VALUES ('project-golden', 'Golden project')")
        await pool.execute("INSERT INTO tasks (id, project_id, requirement, status) "
                           "VALUES ('task-golden', 'project-golden', 'Reduce latency.', 'running')")
        await pool.execute("INSERT INTO task_steps (id, task_id, step_index, step_key, instruction) "
                           "VALUES ('step-golden', 'task-golden', 0, 's1', 'Improve reads.')")
        parent = await llm_records.start_record(agent_id="planner-task-golden", task_id="task-golden")
        settings = replace(resolve(AgentType.ADVISOR, {}), profile=builtin_resolved_profile())
        if mode == "unavailable":
            settings = replace(settings, error="settings unavailable")
        child = spawn.ChildRun(spawn.SpawnKind.ADVISOR, SystemPrompt("Answer from the request alone.", {}),
                               "Which execution strategy bounds CPU concurrency?", timeout_s=10 if mode == "timeout" else 20,
                               role=AgentType.ADVISOR)
        result = await spawn.spawn(
            spawn.SpawnKind.ADVISOR, spawn.SpawnParent("task-golden", "step-golden", parent),
            {"why": "Choose an execution strategy."}, {"language": "Python"}, settings, 0.2,
            child=child, runtime_root=tmp_path / "runtime", claude_bin=executable)
        assert result.child.failure is (None if mode == "success" else spawn.ChildFailure(mode))
        assert result.row["state"] == ("answered" if mode == "success" else
                                       "over_budget" if mode == "budget" else "failed")
        launches = read_launches(tmp_path)
        assert len(launches) == (0 if mode == "unavailable" else 1)
        assert not spawn._directory(child.kind, child.run_id).exists()
        assert_golden(f"spawn-{mode}", {
            "launches": launches,
            "tables": await table_rows(pool, ("llm_records", "llm_record_events", "agent_spawns")),
            "outcome": asdict(result),
        }, tmp_path)

    run_db(test, monkeypatch)


def test_a_cancelled_spawn_keeps_its_failed_row_and_propagates_cancellation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    from looper.agents import spawn
    from looper.agents.agent_settings import AgentType, resolve
    from looper.agents.profile_registry import builtin_resolved_profile
    from looper.agents.system_prompt import SystemPrompt

    async def cancelled(*args: Any, **kwargs: Any) -> Any:
        raise asyncio.CancelledError

    monkeypatch.setattr(spawn, "run_child", cancelled)

    async def test(pool: Any) -> None:
        await pool.execute("INSERT INTO projects (id, name) VALUES ('project-cancel', 'Cancellation')")
        await pool.execute("INSERT INTO tasks (id, project_id, requirement, status) "
                           "VALUES ('task-cancel', 'project-cancel', 'Read data.', 'running')")
        settings = replace(resolve(AgentType.ADVISOR, {}), profile=builtin_resolved_profile())
        child = spawn.ChildRun(spawn.SpawnKind.ADVISOR, SystemPrompt("Read the input.", {}), "Example input", 2)
        with pytest.raises(asyncio.CancelledError):
            await spawn.spawn(spawn.SpawnKind.ADVISOR, spawn.SpawnParent("task-cancel"), {}, {}, settings, 0.2,
                              child=child, runtime_root=tmp_path)
        row = await pool.fetchrow("SELECT state, detail, result FROM agent_spawns")
        assert (row["state"], row["detail"]) == ("failed", "its run was stopped")
        assert '"failure": "runtime"' in row["result"]

    run_db(test, monkeypatch)
