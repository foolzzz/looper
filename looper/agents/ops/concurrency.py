"""The Manager's concurrency tool: how many of a project's tasks, Workers and Reviewers run at once, or of one
task's Workers and Reviewers.

A project's limits are its roles' layers in the project, written as its
Settings tab writes them (``agent_settings.update_project``): the user
confirms them first (``high``). A task's are its own, set as the task page
sets them (``projects.set_task_concurrency``) and bounded by its project's,
so they take effect at once (``write``). Either way a raised limit starts
what waits at once, and a lowered one stops nothing that runs.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from typing import Any

from ...db import agent_setting_layers as db_layers, projects as db_projects
from ..agent_settings import CONCURRENCY_MAX, EMPTY_LAYER, TASK_ROLES, AgentType, Layer, SettingsError
from ..agent_settings import SettingsNotFound, index_layers, resolve_concurrency, update_project
from ..projects import RequestError, concurrency_view, project_name, set_task_concurrency
from ..tiers.task import recheck_limits
from . import Call, ErrorCode, OpError, Risk, Tool
from .tasks import _TASK_ID, SOURCE_WORDS, _request_error, _task, _task_argument

# What each role's limit counts, as the confirmation names it.
COUNTS = {AgentType.PLANNER: "同时运行的任务数", AgentType.WORKER: "同时运行的 Worker 数",
          AgentType.REVIEWER: "同时运行的 Reviewer 数"}
NOTE = ("Lowering a limit stops nothing that runs: what is over it finishes, and new runs wait until fewer run. A "
        "raised limit starts what waits at once.")


def _limits(call: Call) -> dict[AgentType, int | None]:
    limits = call.arguments["limits"]
    if not limits:
        raise OpError(ErrorCode.INVALID, "limits: give at least one role's limit, or null to clear it")
    return {AgentType(role): value for role, value in limits.items()}


async def _project(call: Call) -> dict[str, Any] | None:
    """The project a call changes (None: it changes a task); refuses a call naming both or neither."""
    project, task_id = call.arguments.get("project"), call.arguments.get("task_id")
    if (project is None) == (task_id is None):
        raise OpError(ErrorCode.INVALID, "give exactly one of project (a project's limits) and task_id (one task's "
                                         "own Worker and Reviewer limits)")
    if project is None:
        return None
    try:
        name = project_name(project)
    except ValueError as exc:
        raise OpError(ErrorCode.INVALID, f"project: {exc}") from None
    row = await db_projects.find_project_by_name(name)
    if row is None:
        raise OpError(ErrorCode.NOT_FOUND, f"no project named {name!r}")
    return row


async def _project_changes(project: Mapping[str, Any], limits: Mapping[AgentType, int | None]) -> list[dict[str, Any]]:
    """Each role's project limit now and once ``limits`` are set (a null one inherits the role's or the built-in)."""
    project_id = project["id"]
    layers = index_layers(await db_layers.list_layers(project_id), project_id)
    changes = []
    for role, value in limits.items():
        place = (Layer.ROLE_PROJECT, role)
        after = {**layers, place: replace(layers.get(place, EMPTY_LAYER), max_concurrency=value)}
        changes.append({"role": role.value, "before": resolve_concurrency(role, layers, project_id).view(),
                        "after": resolve_concurrency(role, after, project_id).view()})
    return changes


def _shown(limit: Mapping[str, Any]) -> str:
    return f"{limit['value']}（{SOURCE_WORDS[Layer(limit['source'])]}）"


async def _confirming(call: Call) -> str | None:
    """What changing a project's limits does, for the user to confirm; None for a task's (``write``)."""
    limits = _limits(call)
    project = await _project(call)
    if project is None:
        return None
    lines = [f"要修改项目「{project['name']}」的并发上限吗？"]
    for change in await _project_changes(project, limits):
        role = AgentType(change["role"])
        lines.append(f"- {role.value.capitalize()}（{COUNTS[role]}）：{_shown(change['before'])} → "
                     f"{_shown(change['after'])}")
    lines.append("调低不会停止正在运行的工作：超出的运行完为止，新的会等待；调高后排队的会立即开始。")
    return "\n".join(lines)


async def _set(call: Call) -> dict[str, Any]:
    limits = _limits(call)
    project = await _project(call)
    if project is None:
        result = await _set_task(call.arguments["task_id"], limits)
    else:
        changes = await _project_changes(project, limits)
        try:
            for role, value in limits.items():
                view = await update_project(project["id"], {"max_concurrency": value}, role=role.value)
        except SettingsNotFound as exc:
            raise OpError(ErrorCode.NOT_FOUND, str(exc)) from None
        except SettingsError as exc:
            raise OpError(ErrorCode.INVALID, str(exc)) from None
        for change in changes:
            change["after"] = view["roles"][change["role"]]["effective"]["max_concurrency"]
        result = {"scope": "project", "project": project["name"], "changes": changes,
                  "note": f"planner is how many of the project's tasks run at once. {NOTE}"}
    await recheck_limits(call.platform.task_router, call.platform.worker_pool)
    return result


async def _set_task(task_id: str, limits: Mapping[AgentType, int | None]) -> dict[str, Any]:
    task = await _task(task_id)
    before = (await concurrency_view(task))["roles"]
    try:
        after = await set_task_concurrency(task_id, {role.value: value for role, value in limits.items()})
    except RequestError as exc:
        raise _request_error(exc) from None
    changes = []
    for role in limits:
        now = after["concurrency"]["roles"][role.value]
        changes.append({"role": role.value, "before": _value(before[role.value]), "after": _value(now),
                        "task_limit": now["task"], "project_limit": now["project"]})
    capped = [c["role"] for c in changes if c["task_limit"] is not None and c["after"]["source"] != Layer.TASK.value]
    note = NOTE + (f" The project's own limit is lower than the task's for {', '.join(capped)}, so it applies."
                   if capped else "")
    return {"scope": "task", "task_id": task_id, "changes": changes, "note": note}


def _value(view: Mapping[str, Any]) -> dict[str, Any]:
    return {"value": view["value"], "source": view["source"]}


_LIMIT = {"type": "integer", "minimum": 1, "maximum": CONCURRENCY_MAX}

TOOLS: tuple[Tool, ...] = (
    Tool(
        name="looper_concurrency",
        description="Change how many run at once; a null limit clears it to what it inherits. With project: that "
                    "project's limits (planner: how many of its tasks run at once; worker, reviewer: how many of its "
                    "Workers, Reviewers), and the user confirms first. With task_id: that task's own worker or "
                    "reviewer limit, within its project's; it applies at once. Lowering a limit stops nothing that "
                    "runs. looper_tasks get and looper_overview show the limits now.",
        schema={
            "type": "object", "required": ["limits"], "additionalProperties": False,
            "properties": {
                "project": {"type": "string", "minLength": 1, "description": "The project's name, to change its "
                                                                             "limits."},
                "task_id": {**_TASK_ID, "description": "The task (task-...), to change its own limits."},
                "limits": {"type": "object",
                           "description": f"The new limit of each role to change, from 1 to {CONCURRENCY_MAX}; null "
                                          "clears it. A task takes worker and reviewer only.",
                           "properties": {role.value: _LIMIT for role in TASK_ROLES}},
            },
        },
        risk=Risk.WRITE, run=_set, escalate=_confirming, task_id=_task_argument,
    ),
)
