"""The overview's task groups: every status a task row holds has one group, and an unknown one is counted under
``other`` with its name, never dropped."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from looper.agents.overview import group_counts
from looper.agents.run_usage import Usage
from looper.db.tasks import STATUS_FILTERS, TaskGroup, task_group


def test_running_filter_covers_planner_phases_and_other_filters_cover_themselves() -> None:
    assert set(STATUS_FILTERS["running"]) == {
        "assigned", "running", "planning", "executing", "verifying", "stopping",
    }
    assert {name: statuses for name, statuses in STATUS_FILTERS.items() if name != "running"} == {
        name: (name,) for name in (
            "init", "waiting", "waiting_user", "awaiting_budget", "pending", "completed", "failed", "cancelled", "stopped",
        )
    }


@pytest.mark.parametrize(("status", "group"), [
    # "pending": started, its Planner starting (nothing waits for room under a project's limit any more).
    *[(status, TaskGroup.IN_PROGRESS) for status in ("pending", "assigned", "running", "planning", "executing",
                                                      "verifying", "stopping")],
    ("awaiting_budget", TaskGroup.NEEDS_YOU),
    ("init", TaskGroup.READY),
    ("failed", TaskGroup.ATTENTION),
    ("stopped", TaskGroup.ATTENTION),
    ("completed", TaskGroup.COMPLETED),
    ("cancelled", TaskGroup.CANCELLED),
    ("archived", TaskGroup.OTHER),
    ("", TaskGroup.OTHER),
])
def test_each_status_has_one_group(status: str, group: TaskGroup) -> None:
    assert task_group(status) is group


def test_counts_add_up_every_task_and_name_the_unknown_statuses() -> None:
    rows = [
        {"project_id": "proj-a", "project_name": "a", "status": "executing", "count": 2},
        {"project_id": "proj-b", "project_name": "b", "status": "planning", "count": 1},
        {"project_id": "proj-a", "project_name": "a", "status": "archived", "count": 3},
        {"project_id": None, "project_name": None, "status": "completed", "count": 4},  # a task of no project
        {"project_id": "proj-c", "project_name": "c", "status": None, "count": 0},  # a project without tasks
    ]
    counts = group_counts(rows)
    assert (counts["in_progress"], counts["completed"], counts["other"], counts["total"]) == (3, 4, 3, 10)
    assert counts["other_statuses"] == {"archived": 3}
    assert {group.value for group in TaskGroup} <= set(counts)


@pytest.mark.parametrize(("manager_id", "project_ids", "expected_tasks"), [
    (None, None, {"moving", "foreign"}),
    ("manager-own", None, {"moving"}),
    ("manager-own", ["proj-a"], set()),
    ("manager-own", ["proj-b"], {"moving", "foreign"}),
])
def test_live_runs_follow_the_tasks_current_project(monkeypatch, manager_id, project_ids, expected_tasks):
    import asyncio
    from datetime import datetime, timezone
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from looper.agents import overview

    rows = [{"project_id": project, "project_name": name, "status": "completed", "count": 1}
            for project, name in (("proj-a", "Alpha"), ("proj-b", "Beta"))]
    monkeypatch.setattr(overview.db_tasks, "status_counts", AsyncMock(return_value=rows))
    monkeypatch.setattr(overview.db_tasks, "names_and_projects_of", AsyncMock(return_value={
        task: {"project_id": "proj-b", "name": f"Task {task}"} for task in ("moving", "foreign")}))
    monkeypatch.setattr(overview.db_tasks, "manager_of", AsyncMock(return_value={
        "moving": "manager-own", "foreign": "manager-other"}))
    monkeypatch.setattr(overview.db_llm_records, "spend_since", AsyncMock(return_value={}))
    monkeypatch.setattr(overview, "schedule_overview", AsyncMock(return_value={}))
    runs = [{"agent_id": f"{role}-{task}", "task_id": task, "project_id": "proj-a", "role": role,
             "state": state, "since": datetime.now(timezone.utc), "step": "work", "instruction": "work"}
            for task in ("moving", "foreign") for role, state in (("worker", "running"), ("reviewer", "waiting"))]
    live = [_live(f"worker-{task}", "worker", task) for task in ("moving", "foreign")]
    for run in live:
        run.window.add(Usage(output_tokens=10))
    result = asyncio.run(overview.build_overview(
        task_router=SimpleNamespace(running_planners=lambda: []),
        worker_pool=SimpleNamespace(runs=lambda: runs), manager=None,
        supervisor=SimpleNamespace(agent_status=lambda: {}), manager_id=manager_id, project_ids=project_ids))

    for role in ("workers", "reviewers"):
        assert {run["task_id"] for run in result["agents"][role]} == expected_tasks
        assert all((run["project_id"], run["project_name"]) == ("proj-b", "Beta")
                   for run in result["agents"][role])
    assert [project["project_id"] for project in result["projects"]] == (["proj-b"] if expected_tasks else [])
    if expected_tasks:
        assert result["projects"][0]["workers"] == {"running": len(expected_tasks), "waiting": 0}
        assert result["projects"][0]["reviewers"] == {"running": 0, "waiting": len(expected_tasks)}
    assert all(run["project_id"] == "proj-a" for run in runs)
    assert {(p["task_id"], p["role"], p["state"]) for p in result["processes"]} == {
        (task, role, state) for task in expected_tasks for role, state in (("worker", "running"), ("reviewer", "waiting"))}
    assert result["tpm"]["total"] == 10 * len(expected_tasks)


def _live(agent_id: str, role: str, task_id: str | None, *, seconds: int = 0, streams: bool = True,
          pid: int | None = None) -> Any:
    """A run its recorder follows, started ``seconds`` ago (the caller holds it, as its recorder would)."""
    from looper.agents import live_runs

    run = live_runs.LiveRun(agent_id=agent_id, role=role, task_id=task_id, step_id=None, runtime="Claude Code",
                            model="claude-x", streams_usage=streams,
                            started_at=datetime.now(timezone.utc) - timedelta(seconds=seconds),
                            pid_of=(lambda: pid) if pid else None)
    live_runs.track(run)
    return run


def test_processes_are_the_running_runs_by_role_then_the_step_runs_waiting(monkeypatch):
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from looper.agents import live_runs, overview

    monkeypatch.setattr(overview.db_tasks, "status_counts", AsyncMock(return_value=[
        {"project_id": "proj-a", "project_name": "Alpha", "status": "executing", "count": 1}]))
    monkeypatch.setattr(overview.db_tasks, "names_and_projects_of", AsyncMock(return_value={
        "task-a": {"project_id": "proj-a", "name": "Build it"}}))
    monkeypatch.setattr(overview.db_llm_records, "spend_since", AsyncMock(return_value={}))
    monkeypatch.setattr(overview, "schedule_overview", AsyncMock(return_value={}))
    others = [_live("consult-1", "consult", "task-a", seconds=50),
              _live("planner-task-a", "planner", "task-a", seconds=20, pid=100),
              _live("manager", "manager", None, seconds=10, pid=99)]  # noqa: F841 (held while the overview reads them)
    worker = _live("worker-1", "worker", "task-a", seconds=40, pid=101)
    codex = _live("worker-2", "worker", "task-a", seconds=30, streams=False)
    worker.window.add(Usage(input_tokens=1, output_tokens=2, cache_read_tokens=300, cache_creation_tokens=4))
    codex.window.add(Usage(input_tokens=50))
    ended = _live("worker-0", "worker", "task-a", seconds=90)
    ended.window.add(Usage(output_tokens=5))
    live_runs.untrack(ended)  # it ended within the minute
    since = datetime.now(timezone.utc) - timedelta(seconds=5)
    steps = [{"agent_id": agent_id, "task_id": "task-a", "project_id": "proj-a", "role": role, "state": state,
              "since": since, "step": step, "instruction": f"Do {step}\nin detail",
              "task_role": {"id": "tester", "kind": "review", "version": 1} if role == "reviewer" else None}
             for agent_id, role, state, step in (("worker-1", "worker", "running", "build"),
                                                ("reviewer-1", "reviewer", "waiting", "check"))]
    fleet = SimpleNamespace(agents=lambda: [SimpleNamespace(agent_id="manager", name="Manager (main)", busy=True)],
                            is_running=lambda _: True)

    result = asyncio.run(overview.build_overview(
        task_router=SimpleNamespace(running_planners=lambda: []), worker_pool=SimpleNamespace(runs=lambda: steps),
        manager=None, supervisor=SimpleNamespace(agent_status=lambda: {}), managers=fleet))

    rows = result["processes"]
    assert [(p["agent_id"], p["state"]) for p in rows] == [
        ("manager", "running"), ("planner-task-a", "running"), ("worker-1", "running"), ("worker-2", "running"),
        ("reviewer-1", "waiting"), ("consult-1", "running")]
    manager, planner, built, turn_end, waiting, _ = rows
    assert (manager["name"], manager["pid"], manager["task_id"]) == ("Manager (main)", 99, None)
    assert (planner["task_name"], planner["project_name"], planner["step"]) == ("Build it", "Alpha", None)
    assert (built["step"], built["pid"], built["runtime"], built["model"], built["elapsed_seconds"] >= 40) == (
        "build", 101, "Claude Code", "claude-x", True)
    assert built["tpm"] == {"total": 307, "input": 1, "output": 2, "cache_read": 300, "cache_creation": 4}
    assert turn_end["tpm"] is None
    assert (waiting["task_role"]["id"], waiting["runtime"], waiting["pid"], waiting["tpm"]) == ("tester", None, None, None)
    # The total is every run's last minute: the Codex run's turn-end report and the run that ended included.
    assert result["tpm"] == {"window_seconds": 60, "total": 362, "input": 51, "output": 7, "cache_read": 300,
                             "cache_creation": 4}
