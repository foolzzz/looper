"""The Manager's concurrency tool: how many of one task's Workers and Reviewers run at once.

Concurrency is per task: nothing limits how many tasks run, and a project has
no limits. A task's limits are its own, set as the task page sets them
(``projects.set_task_agents``), so they take effect at once (``write``); without
one, a task runs the default of the Workers' or the Reviewers' role layer
(Settings → Roles, ``looper_settings``). A raised limit starts what waits at
once, and a lowered one stops nothing that runs. A task's Planner changes its
task's limits through the same code (``set_task_limits``), at once
(``ops.planner``).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ..agent_settings import CONCURRENCY_MAX, TASK_LIMIT_ROLES, AgentType, Layer
from ..projects import RequestError, agents_view, set_task_agents
from ..tiers.worker import recheck_limits
from . import Call, ErrorCode, OpError, Risk, Tool
from .tasks import _TASK_ID, SOURCE_WORDS, _request_error, _task, _task_argument

# What each role's limit counts, as a confirmation or a notice names it.
COUNTS = {AgentType.WORKER: "每个任务同时运行的 Worker 数", AgentType.REVIEWER: "每个任务同时运行的 Reviewer 数"}
NOTE = ("Lowering a limit stops nothing that runs: what is over it finishes, and new runs wait until fewer run. A "
        "raised limit starts what waits at once.")


def limits_argument(call: Any) -> dict[AgentType, int | None]:
    """The ``limits`` a call of the Manager's or the Planner's tool asks for, by role; refuses none."""
    limits = call.arguments["limits"]
    if not limits:
        raise OpError(ErrorCode.INVALID, "limits: give at least one role's limit, or null to clear it")
    return {AgentType(role): value for role, value in limits.items()}


def shown(limit: Mapping[str, Any]) -> str:
    """A limit as the user is shown it: its value, and where it comes from."""
    return f"{limit['value']}（{SOURCE_WORDS[Layer(limit['source'])]}）"


async def _set(call: Call) -> dict[str, Any]:
    result = await set_task_limits(call.arguments["task_id"], limits_argument(call))
    await recheck_limits(call.platform.worker_pool)
    return result


async def set_task_limits(task_id: str, limits: Mapping[AgentType, int | None]) -> dict[str, Any]:
    """Set task ``task_id``'s own limits of the roles in ``limits`` (None clears one), as the task page does; each
    role's before and after. The caller lets what waits check again."""
    task = await _task(task_id)
    before = (await agents_view(task))["roles"]
    try:
        after = await set_task_agents(task_id, {"concurrency": {role.value: value for role, value in limits.items()}})
    except RequestError as exc:
        raise _request_error(exc) from None
    changes = []
    for role in limits:
        now = after["agents"]["roles"][role.value]["concurrency"]
        changes.append({"role": role.value, "before": _value(before[role.value]["concurrency"]), "after": _value(now),
                        "task_limit": now["task"], "default": now["default"]})
    return {"task_id": task_id, "changes": changes, "note": NOTE}


def _value(view: Mapping[str, Any]) -> dict[str, Any]:
    return {"value": view["value"], "source": view["source"]}


_LIMIT = {"type": "integer", "minimum": 1, "maximum": CONCURRENCY_MAX}

TOOLS: tuple[Tool, ...] = (
    Tool(
        name="looper_concurrency",
        description="Change one task's own limit of how many of its Workers or Reviewers run at once, in any task "
                    "but a completed one (a stopped, failed or cancelled task's restart runs with it); it applies at "
                    "once, and a null limit clears it to the default. Concurrency is per task: projects have no "
                    "limits and nothing limits how many tasks run, so a project's limit cannot be changed; say so. "
                    "The default for every task is the worker or reviewer role's max_concurrency (looper_settings). "
                    "Lowering a limit stops nothing that runs. looper_tasks get shows a task's limits.",
        schema={
            "type": "object", "required": ["task_id", "limits"], "additionalProperties": False,
            "properties": {
                "task_id": {**_TASK_ID, "description": "The task (task-...)."},
                "limits": {"type": "object",
                           "description": f"The task's new limit of each role to change, from 1 to {CONCURRENCY_MAX}; "
                                          "null clears it.",
                           "properties": {role.value: _LIMIT for role in TASK_LIMIT_ROLES}},
            },
        },
        risk=Risk.WRITE, run=_set, task_id=_task_argument,
    ),
)
