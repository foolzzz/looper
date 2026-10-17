"""The agents the Chat page shows, and where a chat message to one of them goes.

The Manager is global. Each task has one Planner for its whole life, and
every step of the task runs in its own Worker (plus a Reviewer when the step
is verified). All of them stay listed after they finish so their messages
can be read; only the Manager and a running task's Planner take messages.
"""

from __future__ import annotations

import logging
from typing import Any

from ..db import llm_records, steps as db_steps, tasks as db_tasks
from .message import MANAGER_ADDRESS, AgentAddress, planner_address
from .task_roles import run_role
from .tiers.task import TaskRouter

logger = logging.getLogger(__name__)

# How many of the most recent tasks the Chat page lists; running tasks always appear.
RECENT_TASKS = 30

HTTP_400 = "400 Bad Request"
HTTP_409 = "409 Conflict"


class ChatTargetError(Exception):
    def __init__(self, status: str, message: str) -> None:
        super().__init__(message)
        self.status = status


def resolve_chat_target(raw: Any, task_router: TaskRouter) -> AgentAddress:
    """Map a ``/api/chat/send`` target to a bus address; the Manager is the default."""
    target = raw or {"kind": "manager"}
    kind = target.get("kind", "manager") if isinstance(target, dict) else None
    if kind == "manager":
        return MANAGER_ADDRESS
    if kind == "planner":
        task_id = str(target.get("task_id") or "")
        if not task_router.has_planner(task_id):
            raise ChatTargetError(HTTP_409, f"task '{task_id}' has no running planner")
        return planner_address(task_id)
    raise ChatTargetError(HTTP_400, "target.kind must be 'manager' or 'planner'")


def build_chat_targets(
    *,
    tasks: list[dict[str, Any]],
    live_planners: list[dict[str, Any]],
    steps: list[dict[str, Any]],
    runs: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """The Manager, then per task (newest first) its Planner and its step agents.

    ``tasks`` are task rows, ``live_planners`` is ``TaskRouter.running_planners()``,
    ``steps`` are task_steps rows and ``runs`` are Worker/Reviewer llm_records
    rows (with the run's ``step_id`` and ``task_role``), oldest first. A step
    agent that ran as a role its task's Planner defined names it (``task_role``).
    """
    targets: list[dict[str, Any]] = [{"kind": "manager", "id": MANAGER_ADDRESS.agent_id, "can_send": True}]

    live = {p["task_id"]: p for p in live_planners}
    known = {t["id"] for t in tasks}
    # A task that has only just started may not be among the recent rows yet.
    rows = [
        {"id": p["task_id"], "requirement": p["requirement"], "name": p["name"], "status": p["status"],
         "created_at": None}
        for p in live_planners if p["task_id"] not in known
    ]
    rows += sorted(tasks, key=lambda t: t["created_at"], reverse=True)

    step_by_id = {s["id"]: s for s in steps}
    step_by_worker = {s["worker_id"]: s for s in steps if s.get("worker_id")}
    agents_by_task: dict[str, dict[str, dict[str, Any]]] = {}
    for run in runs:
        agents = agents_by_task.setdefault(run["task_id"], {})
        agent = agents.get(run["agent_id"])
        if agent is None:
            step = step_by_id.get(run.get("step_id") or "") or step_by_worker.get(run["agent_id"])
            agents[run["agent_id"]] = {
                "kind": "reviewer" if run["agent_id"].startswith("reviewer-") else "worker",
                "id": run["agent_id"],
                "task_id": run["task_id"],
                "status": run["status"],
                "step_index": step["step_index"] if step else None,
                "instruction": step["instruction"] if step else "",
                "started_at": run["created_at"],
                "can_send": False,
                **({"task_role": role} if (role := run_role(run.get("task_role"))) else {}),
            }
        else:
            agent["status"] = run["status"]

    for row in rows:
        task_id = row["id"]
        planner = live.get(task_id)
        targets.append({
            "kind": "planner",
            "id": planner["planner_id"] if planner else planner_address(task_id).agent_id,
            "task_id": task_id,
            "requirement": row["requirement"],
            "name": row["name"],
            "status": planner["status"] if planner else row["status"],
            "can_send": planner is not None,
        })
        targets.extend(agents_by_task.get(task_id, {}).values())
    return targets


async def list_chat_targets(task_router: TaskRouter) -> list[dict[str, Any]]:
    """Read the recent tasks and their step agents; without the database, list the live ones."""
    live_planners = task_router.running_planners()
    tasks: list[dict[str, Any]] = []
    steps: list[dict[str, Any]] = []
    runs: list[dict[str, Any]] = []
    try:
        tasks = await db_tasks.list_tasks(limit=RECENT_TASKS)
        task_ids = sorted({t["id"] for t in tasks} | {p["task_id"] for p in live_planners})
        steps = await db_steps.list_steps_for_tasks(task_ids)
        runs = await llm_records.list_task_runs(task_ids)
    except Exception:
        logger.warning("chat targets: task history unavailable, listing live agents only", exc_info=True)
    return build_chat_targets(tasks=tasks, live_planners=live_planners, steps=steps, runs=runs)
