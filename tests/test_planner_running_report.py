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
    assert len(lines[3]) == len("  - [tool] ") + 160
    assert '…' in lines[3]
    assert lines[-1] == "  - room update"
    assert not any("(no activity yet)" in line or "old activity" in line for line in lines)
    recent.assert_awaited_once_with("task", worker, limit=6)


@pytest.mark.parametrize('outside', [False, True])
def test_running_report_keeps_filename_after_a_long_absolute_path(tmp_path, monkeypatch, outside):
    root = tmp_path / ('workspace-' * 25)
    path = (tmp_path if outside else root) / ('nested-' * 30) / 'important_source.py'
    monkeypatch.setattr(activity, 'list_worker_activity', AsyncMock(return_value=[
        {'kind': 'tool', 'summary': f'Read {path}'}]))
    agent = PlannerAgent(task_id='task', runtime_root=tmp_path)
    monkeypatch.setattr(agent, '_room_lines', AsyncMock(return_value=[]))
    step = PlanStep('step', 0, 'Inspect', worker_id='worker')
    plan = TaskPlan('plan', 'goal', 'Inspect', str(root), steps=[step])

    lines = asyncio.run(agent._running_report(plan, step))

    assert 'important_source.py' in lines[1] and lines[1].startswith('  - [tool] Read ')
    assert len(lines[1]) <= len('  - [tool] ') + 160
    if not outside:
        assert str(root) not in lines[1]


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
