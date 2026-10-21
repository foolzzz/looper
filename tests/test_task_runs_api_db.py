"""``GET /api/tasks/<id>/runs``: compact rows of a task's runs, against a disposable PostgreSQL."""

from __future__ import annotations

from typing import Any

import pytest

from looper.db import llm_records
from tests.test_ops_tasks_db import _project, _task
from tests.test_usage_api_db import _get, _run
from tests.test_waits_api_db import _api

PROFILE = {"id": "profile-1", "name": "Fast", "model": "profile-model", "source": "project",
           "runtime": {"id": "rt-1", "name": "Claude Code", "type": "claude-code"},
           "provider": {"id": "prov", "name": "p", "auth": "oauth"}}


async def _record(pool: Any, agent_id: str, task_id: str, metadata: dict[str, Any] | None, *, model: str | None = None,
                  status: str = "completed") -> int:
    record_id = await llm_records.start_record(agent_id=agent_id, task_id=task_id, input_text="x" * 5000,
                                               metadata=metadata, pool=pool)
    await llm_records.finish_record(record_id, status=status, model=model, pool=pool)
    return record_id


def test_runs_are_compact_rows_matching_the_records(monkeypatch: pytest.MonkeyPatch) -> None:
    async def test(pool: Any) -> None:
        project = await _project(pool)
        await _task(pool, "task-a", "running", project)
        await _task(pool, "task-b", "running", project)
        big = {"system_prompt": "p" * 50_000, "step_id": "step-1", "profile": PROFILE,
               "model_choice": {"model": "sonnet", "source": "project", "effort": "high"}}
        planner = await _record(pool, "planner-task-a", "task-a", {"profile": PROFILE, "system_prompt": "s"})
        worker = await _record(pool, "worker-1", "task-a", big, model="claude-sonnet-x")
        reviewer = await _record(pool, "reviewer-2", "task-a", {**big, "step_id": "step-2", "profile": {
            **PROFILE, "source": "task_role"}}, status="failed")
        old = await _record(pool, "worker-3", "task-a", {"agent": {**PROFILE, "name": "Old"}})
        bare = await _record(pool, "worker-4", "task-a", None)
        await _record(pool, "manager", "task-a", {"profile": PROFILE})
        await _record(pool, "worker-9", "task-b", big)

        status, body = await _get("/api/tasks/task-a/runs")
        assert status == 200
        runs = body["runs"]
        assert [r["id"] for r in runs] == [planner, worker, reviewer, old, bare]
        assert [r["role"] for r in runs] == ["planner", "worker", "reviewer", "worker", "worker"]
        assert len(str(runs)) < 3000
        for run in runs:
            assert set(run) == {"id", "role", "agent_id", "step_id", "status", "created_at", "runtime", "model",
                                "effort", "profile"}

        by_id = {r["id"]: r for r in await llm_records.list_records(task_id="task-a", pool=pool)}
        for run in runs:
            record = by_id[run["id"]]
            meta = record["metadata"] or {}
            profile = meta.get("profile") or meta.get("agent") or {}
            assert run["agent_id"] == record["agent_id"] and run["status"] == record["status"]
            assert run["created_at"] == record["created_at"]
            assert run["step_id"] == meta.get("step_id")
            assert run["runtime"] == {"type": (profile.get("runtime") or {}).get("type"),
                                      "name": (profile.get("runtime") or {}).get("name")}
            assert run["profile"] == {"name": profile.get("name"), "source": profile.get("source")}
            assert run["effort"] == (meta.get("model_choice") or {}).get("effort")
        assert runs[1]["model"] == "claude-sonnet-x" and runs[1]["effort"] == "high"
        assert runs[2]["model"] == "sonnet" and runs[2]["status"] == "failed"
        assert runs[2]["profile"] == {"name": "Fast", "source": "task_role"}
        assert runs[3]["profile"]["name"] == "Old" and runs[3]["model"] == "profile-model"
        assert runs[4]["model"] is None and runs[4]["runtime"] == {"type": None, "name": None}

        status, body = await _get("/api/tasks/task-c/runs")
        assert (status, body) == (200, {"runs": []})

    _run(test, monkeypatch)


def test_runs_are_operator_only(monkeypatch: pytest.MonkeyPatch) -> None:
    async def test(pool: Any) -> None:
        await _task(pool, "task-a", "running", await _project(pool))
        status, _, _ = await _api("GET", "/api/tasks/task-a/runs", auth=False)
        assert status in (401, 403)
        status, body, _ = await _api("GET", "/api/tasks/task-a/runs")
        assert status == 200 and body == {"runs": []}

    _run(test, monkeypatch)
