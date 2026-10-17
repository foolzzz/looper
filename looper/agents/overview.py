"""What is happening now: the dashboard's one read (``GET /api/overview``).

- **Tasks**: how many are in each group (``db.tasks.TaskGroup``), every task
  counted, an unknown status under ``other`` with its name.
- **Agents running now**: whether the Manager is in a turn, the live
  Planners (TaskRouter) and the Worker and Reviewer runs, running or waiting
  for room under their project's limit (WorkerPool).
- **Concurrency**: for each project with a task in progress, queued or
  ready, its running tasks, Workers and Reviewers against each role's limit
  there, read now (``agent_settings.load_concurrency``).
- **Today's spend**: what the runs started since the server's local midnight
  cost, money and subscription equivalent apart (``llm_records.spend_since``).
- **Health**: the supervisor's components that are unhealthy.
- **Schedules**: how many there are and are enabled, the next 5 fires, the
  failing ones (failures in a row or an alert active) and the runs skipped
  today by reason (``schedules.schedule_overview``).
"""

from __future__ import annotations

import asyncio
from datetime import date, datetime, time, timezone
from typing import Any

from ..db import llm_records as db_llm_records, tasks as db_tasks
from ..db.tasks import TaskGroup
from .agent_settings import AgentType, load_concurrency
from .schedules import schedule_overview
from .supervisor import AgentSupervisor
from .tiers.manager import ManagerAgent
from .tiers.task import TaskRouter
from .tiers.worker import WorkerPool

# A project with a task in one of these groups is shown with its limits; the others are idle.
_ACTIVE_GROUPS = (TaskGroup.NEEDS_YOU, TaskGroup.IN_PROGRESS, TaskGroup.QUEUED, TaskGroup.READY)


def group_counts(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """``db.tasks.status_counts`` rows as a count per group, a ``total``, and ``other_statuses``: the count of
    each status no group knows."""
    counts: dict[str, Any] = {group.value: 0 for group in TaskGroup}
    other: dict[str, int] = {}
    for row in rows:
        if row["status"] is None:
            continue  # a project without tasks
        group = db_tasks.task_group(row["status"])
        counts[group.value] += row["count"]
        if group is TaskGroup.OTHER:
            other[row["status"]] = other.get(row["status"], 0) + row["count"]
    counts["total"] = sum(counts[group.value] for group in TaskGroup)
    counts["other_statuses"] = other
    return counts


def local_midnight() -> datetime:
    """The start of today in the server's time zone."""
    return datetime.combine(date.today(), time.min).astimezone()


def _runs_in(runs: list[dict[str, Any]], role: AgentType, project_id: str, limit: int) -> dict[str, int]:
    """How many of ``role``'s runs in the project run and wait, against its ``limit`` there."""
    mine = [r for r in runs if r["role"] == role.value and r["project_id"] == project_id]
    return {"running": sum(r["state"] == "running" for r in mine),
            "waiting": sum(r["state"] == "waiting" for r in mine), "limit": limit}


async def build_overview(
    *,
    task_router: TaskRouter,
    worker_pool: WorkerPool | None,
    manager: ManagerAgent | None,
    supervisor: AgentSupervisor,
) -> dict[str, Any]:
    """The overview, read now; raises when the database cannot be read."""
    rows = await db_tasks.status_counts()
    since = local_midnight()
    spend = await db_llm_records.spend_since(since)

    names = {row["project_id"]: row["project_name"] for row in rows if row["project_id"] is not None}
    by_project: dict[str, dict[TaskGroup, int]] = {}
    for row in rows:
        if row["project_id"] is not None and row["status"] is not None:
            groups = by_project.setdefault(row["project_id"], {})
            group = db_tasks.task_group(row["status"])
            groups[group] = groups.get(group, 0) + row["count"]

    now = datetime.now(timezone.utc)
    planners = [
        {"agent_id": p["planner_id"], "task_id": p["task_id"], "project_id": p["project_id"],
         "project_name": names.get(p["project_id"]), "requirement": p["requirement"], "name": p["name"]}
        for p in task_router.running_planners()
    ]
    runs = sorted(worker_pool.runs() if worker_pool else [], key=lambda r: (r["state"] != "running", r["since"]))
    step_runs = [
        {"agent_id": r["agent_id"], "role": r["role"], "task_id": r["task_id"], "project_id": r["project_id"],
         "project_name": names.get(r["project_id"]), "step": r["step"], "instruction": r["instruction"],
         "state": r["state"], "elapsed_seconds": max(0, int((now - r["since"]).total_seconds()))}
        for r in runs
    ]

    active = {project_id for project_id, groups in by_project.items() if any(groups.get(g) for g in _ACTIVE_GROUPS)}
    active |= {p["project_id"] for p in planners if p["project_id"]}
    active |= {r["project_id"] for r in step_runs if r["project_id"]}
    shown = sorted(active, key=lambda project_id: (names.get(project_id) or project_id).lower())
    roles = (AgentType.PLANNER, AgentType.WORKER, AgentType.REVIEWER)
    limits = await asyncio.gather(*(load_concurrency(role, project_id) for project_id in shown for role in roles))
    projects = []
    for index, project_id in enumerate(shown):
        planner_limit, worker_limit, reviewer_limit = limits[index * 3:index * 3 + 3]
        groups = by_project.get(project_id, {})
        projects.append({
            "project_id": project_id,
            "name": names.get(project_id) or project_id,
            "tasks": {"running": sum(p["project_id"] == project_id for p in planners),
                      "queued": groups.get(TaskGroup.QUEUED, 0), "ready": groups.get(TaskGroup.READY, 0),
                      "limit": planner_limit},
            "workers": _runs_in(step_runs, AgentType.WORKER, project_id, worker_limit),
            "reviewers": _runs_in(step_runs, AgentType.REVIEWER, project_id, reviewer_limit),
        })

    return {
        "tasks": group_counts(rows),
        "agents": {
            "manager": {"agent_id": manager.agent_id, "state": "busy" if manager.busy else "idle"} if manager else None,
            "planners": planners,
            "workers": [r for r in step_runs if r["role"] == AgentType.WORKER.value],
            "reviewers": [r for r in step_runs if r["role"] == AgentType.REVIEWER.value],
        },
        "projects": projects,
        "idle_projects": len(set(names) - active),
        "spend_today": {"since": since.isoformat(), **spend},
        "schedules": await schedule_overview(since),
        # Every component not simply running: down and being restarted, or crash-looping (up or down now).
        "health": {"unhealthy": [
            {"name": name, **{key: entry[key] for key in ("state", "up", "restart_count", "last_crash_at",
                                                         "next_restart_at")}}
            for name, entry in supervisor.agent_status().items() if not entry["healthy"]
        ]},
    }
