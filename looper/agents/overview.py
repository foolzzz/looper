"""What is happening now: the dashboard's one read (``GET /api/overview``).

- **Tasks**: how many are in each group (``db.tasks.TaskGroup``), every task
  counted, an unknown status under ``other`` with its name.
- **Agents running now**: whether the Manager is in a turn, the live
  Planners (TaskRouter) and the Worker and Reviewer runs, running or waiting
  for room under their task's limit (WorkerPool), each with the role its
  task's Planner defined that it runs as, when it runs as one.
- **Processes**: every agent run going on now (``live_runs``: a Manager's or
  a Planner's turn, a Worker's, a Reviewer's or a consult's run) with its
  runtime, model, pid and tokens per minute, and the Worker and Reviewer runs
  waiting for room; ``tpm`` is all agents' tokens in the last minute, runs
  that ended within it included.
- **Projects**: for each project with a task in progress or ready, or an
  agent running, its running and ready tasks and its running and waiting
  Workers and Reviewers. A project limits none of them: a task limits its own
  Workers and Reviewers.
- **Today's spend**: what the runs started since the server's local midnight
  cost, money and subscription equivalent apart (``llm_records.spend_since``).
- **Health**: the supervisor's components that are unhealthy.
- **Schedules**: how many there are and are enabled, the next 5 fires, the
  failing ones (failures in a row or an alert active) and the runs skipped
  today by reason (``schedules.schedule_overview``).
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime, time, timezone
from typing import Any

from .. import __version__
from ..db import llm_records as db_llm_records, tasks as db_tasks
from ..db.tasks import TaskGroup
from . import live_runs
from .agent_settings import AgentType
from .schedules import empty_schedule_overview, schedule_overview
from .supervisor import AgentSupervisor
from .task_roles import run_role
from .tiers.manager import ManagerAgent
from .tiers.task import TaskRouter
from .tiers.worker import WorkerPool

# A project with a task in one of these groups is shown; the others are idle.
_ACTIVE_GROUPS = (TaskGroup.NEEDS_YOU, TaskGroup.IN_PROGRESS, TaskGroup.READY)
# The order of the processes: by role, then running before waiting, the longest first.
_ROLE_ORDER = {role: index for index, role in enumerate(("manager", "planner", "worker", "reviewer", "consult"))}


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
    if counts[TaskGroup.DRAFT.value] == 0:
        del counts[TaskGroup.DRAFT.value]  # preserve existing responses when no draft exists
    counts["other_statuses"] = other
    return counts


def local_midnight() -> datetime:
    """The start of today in the server's time zone."""
    return datetime.combine(date.today(), time.min).astimezone()


def _runs_in(runs: list[dict[str, Any]], role: AgentType, project_id: str) -> dict[str, int]:
    """How many of ``role``'s runs in the project run and wait (for room under their task's limit)."""
    mine = [r for r in runs if r["role"] == role.value and r["project_id"] == project_id]
    return {"running": sum(r["state"] == "running" for r in mine),
            "waiting": sum(r["state"] == "waiting" for r in mine)}


def _processes(counted: list[live_runs.LiveRun], step_runs: list[dict[str, Any]],
               tasks: dict[str, dict[str, Any]], project_names: dict[str, str], manager_names: dict[str, str],
               now: datetime) -> list[dict[str, Any]]:
    """The running runs among ``counted`` (the visible ones), each with its task and, for a Worker or a Reviewer,
    its step; then the step runs no recorder follows: waiting for room, or just starting."""
    steps = {r["agent_id"]: r for r in step_runs}
    rows = []
    visible = set(counted)
    running = [run for run in live_runs.running() if run in visible]
    for run in running:
        step = steps.get(run.agent_id)
        task = tasks.get(run.task_id) if run.task_id else None
        project_id = task["project_id"] if task else None
        rows.append({
            "agent_id": run.agent_id, "role": run.role,
            "name": manager_names.get(run.agent_id, "Manager") if run.role == AgentType.MANAGER.value else None,
            "task_id": run.task_id, "task_name": task["name"] if task else run.task_id,
            "project_id": project_id, "project_name": project_names.get(project_id) if project_id else None,
            "step": step["step"] if step else None, "instruction": step["instruction"] if step else None,
            **({"task_role": step["task_role"]} if step and step.get("task_role") else {}),
            "runtime": run.runtime, "model": run.model, "pid": run.pid, "state": "running",
            "elapsed_seconds": max(0, int((now - run.started_at).total_seconds())), "tpm": run.tpm(),
        })
    followed = {run.agent_id for run in running}
    rows += [{"agent_id": r["agent_id"], "role": r["role"], "name": None, "task_id": r["task_id"],
              "task_name": r["task_name"], "project_id": r["project_id"], "project_name": r["project_name"],
              "step": r["step"], "instruction": r["instruction"],
              **({"task_role": r["task_role"]} if r.get("task_role") else {}),
              "runtime": None, "model": None, "pid": None, "state": r["state"],
              "elapsed_seconds": r["elapsed_seconds"], "tpm": None}
             for r in step_runs if r["agent_id"] not in followed]
    return sorted(rows, key=lambda row: (_ROLE_ORDER.get(row["role"], len(_ROLE_ORDER)), row["state"] != "running",
                                         -row["elapsed_seconds"]))


async def _fleet_managers(fleet: Any) -> list[dict[str, Any]]:
    """Every Manager of the fleet and whether it is working, waiting or off."""
    out = []
    for agent in fleet.agents():
        state = "busy" if agent.busy else "idle" if fleet.is_running(agent.agent_id) else "stopped"
        out.append({"agent_id": agent.agent_id, "name": agent.name, "state": state})
    return out


async def build_overview(
    *,
    task_router: TaskRouter,
    worker_pool: WorkerPool | None,
    manager: ManagerAgent | None,
    supervisor: AgentSupervisor,
    manager_id: str | None = None,
    project_ids: Sequence[str] | None = None,
    managers: Any = None,
) -> dict[str, Any]:
    """The overview, read now; raises when the database cannot be read. With ``manager_id``, the same keys for that
    Manager's tasks, its and its tasks' spend, and its own component only (no schedules)."""
    rows = await db_tasks.status_counts(manager_id=None if project_ids is not None else manager_id,
                                       project_ids=project_ids)
    since = local_midnight()
    spend = await db_llm_records.spend_since(since, manager_id=manager_id, project_ids=project_ids)

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
    counted = live_runs.counted()
    live_tasks = {run.task_id for run in counted if run.task_id}
    # A task can move while its Workers run; dispatch-time project IDs are no longer its scope.
    current_tasks = await db_tasks.names_and_projects_of({r["task_id"] for r in runs} | live_tasks)
    current_projects = {task_id: task["project_id"] for task_id, task in current_tasks.items()}
    runs = [{**r, "project_id": current_projects.get(r["task_id"], r["project_id"])} for r in runs]
    if project_ids is not None:
        planners = [p for p in planners if p["project_id"] in project_ids]
        runs = [r for r in runs if current_projects.get(r["task_id"]) in project_ids]
        counted = [run for run in counted if current_projects.get(run.task_id or "") in project_ids]
    elif manager_id is not None:
        owners = await db_tasks.manager_of({p["task_id"] for p in planners} | {r["task_id"] for r in runs} | live_tasks)
        own = {task_id for task_id, owner in owners.items() if owner == manager_id}
        planners = [p for p in planners if p["task_id"] in own]
        runs = [r for r in runs if r["task_id"] in own]
        counted = [run for run in counted if run.agent_id == manager_id or run.task_id in own]
    step_runs = [
        {"agent_id": r["agent_id"], "role": r["role"], "task_id": r["task_id"], "project_id": r["project_id"],
         "project_name": names.get(r["project_id"]),
         "task_name": current_tasks.get(r["task_id"], {}).get("name", r["task_id"]),
         "step": r["step"], "instruction": r["instruction"],
         "state": r["state"], "elapsed_seconds": max(0, int((now - r["since"]).total_seconds())),
         # The role its task's Planner defined that it runs as; a built-in role's run has none.
         **({"task_role": role} if (role := run_role(r.get("task_role"))) else {})}
        for r in runs
    ]

    fleet = await _fleet_managers(managers) if managers is not None else None
    manager_names = {m["agent_id"]: m["name"] for m in fleet or []} or \
        ({manager.agent_id: "Manager"} if manager else {})
    processes = _processes(counted, step_runs, current_tasks, names, manager_names, now)

    active = {project_id for project_id, groups in by_project.items() if any(groups.get(g) for g in _ACTIVE_GROUPS)}
    active |= {p["project_id"] for p in planners if p["project_id"]}
    active |= {r["project_id"] for r in step_runs if r["project_id"]}
    shown = sorted(active, key=lambda project_id: (names.get(project_id) or project_id).lower())
    projects = [{
        "project_id": project_id,
        "name": names.get(project_id) or project_id,
        "tasks": {"running": sum(p["project_id"] == project_id for p in planners),
                  "ready": by_project.get(project_id, {}).get(TaskGroup.READY, 0)},
        "workers": _runs_in(step_runs, AgentType.WORKER, project_id),
        "reviewers": _runs_in(step_runs, AgentType.REVIEWER, project_id),
    } for project_id in shown]

    return {
        "version": __version__,
        "tasks": group_counts(rows),
        "agents": {
            "manager": {"agent_id": manager.agent_id, "state": "busy" if manager.busy else "idle"} if manager else None,
            **({"managers": fleet} if fleet is not None else {}),
            "planners": planners,
            "workers": [r for r in step_runs if r["role"] == AgentType.WORKER.value],
            "reviewers": [r for r in step_runs if r["role"] == AgentType.REVIEWER.value],
        },
        "processes": processes,
        "tpm": {"window_seconds": int(live_runs.WINDOW_SEC), **live_runs.total_rate(run.window for run in counted)},
        "projects": projects,
        "idle_projects": len(set(names) - active),
        "spend_today": {"since": since.isoformat(), **spend},
        "schedules": await schedule_overview(since) if manager_id is None else empty_schedule_overview(),
        # Every component not simply running: down and being restarted, or crash-looping (up or down now).
        "health": {"unhealthy": [
            {"name": name, **{key: entry[key] for key in ("state", "up", "restart_count", "last_crash_at",
                                                         "next_restart_at")}}
            for name, entry in supervisor.agent_status().items()
            if not entry["healthy"] and (manager_id is None or name == manager_id)
        ]},
    }
