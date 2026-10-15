"""The overview's task groups: every status a task row holds has one group, and an unknown one is counted under
``other`` with its name, never dropped."""

from __future__ import annotations

import pytest

from looper.agents.overview import group_counts
from looper.db.tasks import TaskGroup, task_group


@pytest.mark.parametrize(("status", "group"), [
    *[(status, TaskGroup.IN_PROGRESS) for status in ("assigned", "running", "planning", "executing", "verifying",
                                                      "stopping")],
    ("pending", TaskGroup.QUEUED),
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
