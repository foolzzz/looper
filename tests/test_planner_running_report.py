"""Running Worker reports use that Worker's recent activity, even on long tasks."""

import asyncio
from unittest.mock import AsyncMock

import pytest

from looper.agents.tiers.planner import PlannerAgent, PlanStep, TaskPlan
from looper.db import activity


def test_running_report_reads_the_workers_latest_rows(tmp_path, monkeypatch):
    worker = "worker-b"
    monkeypatch.setattr(activity, "list_activity", AsyncMock(return_value=[
        {"worker": "worker-a", "kind": "tool", "summary": "old activity"}
    ] * 400))
    recent = AsyncMock(return_value=[
        {"worker": worker, "kind": "tool", "summary": summary}
        for summary in ("read source", "ran checks", "x" * 200)
    ])
    monkeypatch.setattr(activity, "list_worker_activity", recent, raising=False)
    agent = PlannerAgent(task_id="task", runtime_root=tmp_path)
    monkeypatch.setattr(agent, "_room_lines", AsyncMock(return_value=["room update"]))
    step = PlanStep("step", 0, "Inspect the source", worker_id=worker)
    plan = TaskPlan("plan", "goal", "Inspect", str(tmp_path), steps=[step])

    lines = asyncio.run(agent._running_report(plan, step))

    assert lines[1:3] == ["  - [tool] read source", "  - [tool] ran checks"]
    assert lines[3] == "  - [tool] " + "x" * 159 + "…"
    assert lines[-1] == "  - room update"
    assert not any("(no activity yet)" in line or "old activity" in line for line in lines)
    recent.assert_awaited_once_with("task", worker, limit=6)


@pytest.mark.parametrize("worker,unavailable", [("worker-b", False), (None, False), ("", False), ("worker-b", True)])
def test_running_report_handles_missing_activity(tmp_path, monkeypatch, worker, unavailable):
    recent = AsyncMock(side_effect=RuntimeError("database unavailable")) if unavailable else AsyncMock(return_value=[])
    monkeypatch.setattr(activity, "list_worker_activity", recent, raising=False)
    monkeypatch.setattr(activity, "list_activity", AsyncMock(side_effect=AssertionError("unexpected task scan")))
    agent = PlannerAgent(task_id="task", runtime_root=tmp_path)
    room = AsyncMock(return_value=["room update"])
    monkeypatch.setattr(agent, "_room_lines", room)
    step = PlanStep("step", 0, "Inspect", worker_id=worker)
    plan = TaskPlan("plan", "goal", "Inspect", str(tmp_path), steps=[step])

    lines = asyncio.run(agent._running_report(plan, step))

    assert lines[1] == ("  - (recent activity is unavailable)" if unavailable else "  - (no activity yet)")
    if not worker:
        recent.assert_not_awaited()
    if unavailable:
        room.assert_not_awaited()
    else:
        assert lines[-1] == "  - room update"
