"""The overview's task groups: every status a task row holds has one group, and an unknown one is counted under
``other`` with its name, never dropped."""

from __future__ import annotations

import pytest

from looper.agents.overview import group_counts
from looper.db.tasks import STATUS_FILTERS, TaskGroup, task_group


def test_running_filter_covers_planner_phases_and_other_filters_cover_themselves() -> None:
    assert set(STATUS_FILTERS["running"]) == {
        "assigned", "running", "planning", "executing", "verifying", "stopping",
    }
    assert {name: statuses for name, statuses in STATUS_FILTERS.items() if name != "running"} == {
        name: (name,) for name in (
            "init", "waiting", "waiting_user", "pending", "completed", "failed", "cancelled", "stopped",
        )
    }


@pytest.mark.parametrize(("status", "group"), [
    # "pending": started, its Planner starting (nothing waits for room under a project's limit any more).
    *[(status, TaskGroup.IN_PROGRESS) for status in ("pending", "assigned", "running", "planning", "executing",
                                                      "verifying", "stopping")],
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
