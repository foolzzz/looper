"""Projects over HTTP: request validation, the project view and task creation.

Every task belongs to exactly one project and can move to another at any
time. A task is created in an existing project (``project_id``) or in a new
one made by the same request (``project``). Its work directory is the one
given, else its project's default; there is no global default. A project's
``background`` goes into the system prompt of its task agents (Planner,
Workers, Reviewers), read afresh when each prompt is built. Its execution
settings (``project_execution``) are top-level fields of the project too,
``null`` to inherit. Its agent settings (harness, model, effort, environment,
per-run cap) are layers of their own (``agent_settings``,
``/api/projects/<id>/agent-settings``).
"""

from __future__ import annotations

import json
import re
import uuid
from typing import Any

import asyncpg

from ..db import connection as db_connection, llm_records as db_llm_records, projects as db_projects
from ..db import tasks as db_tasks
from .project_execution import FIELDS as EXECUTION_FIELDS, ProjectExecution, execution_fields, format_usd
from .tiers.task import TaskRouter, resolve_work_dir

HTTP_400 = "400 Bad Request"
HTTP_404 = "404 Not Found"
HTTP_409 = "409 Conflict"

NAME_MAX_CHARS = 80
BACKGROUND_MAX_CHARS = 10000
NO_WORK_DIR = "a work directory is required: set one on the task or on its project"
# Fields a project used to hold that are agent settings now (its layer and its roles' layers).
MOVED_FIELDS = ("planner_harness_prompt", "worker_harness_prompt", "model", "run_budget_usd")

_LEADING_BLANK_LINES = re.compile(r"\A(?:[ \t\r\f\v]*\n)+")


class RequestError(Exception):
    """A request the API refuses, with the HTTP status to answer it with."""

    def __init__(self, status: str, message: str) -> None:
        super().__init__(message)
        self.status = status


def project_name(raw: Any) -> str:
    """A project name: trimmed, 1 to 80 characters. Raises ``ValueError`` otherwise."""
    name = raw.strip() if isinstance(raw, str) else ""
    if not name:
        raise ValueError("a project name is required")
    if len(name) > NAME_MAX_CHARS:
        raise ValueError(f"a project name has at most {NAME_MAX_CHARS} characters")
    return name


def project_work_dir(raw: Any) -> str:
    """A project's default directory: absolute (``~`` expanded), or ``''`` for none."""
    if not isinstance(raw, str):
        raise ValueError("work_dir must be a string")
    return resolve_work_dir(raw) if raw.strip() else ""


def project_background(raw: Any) -> str:
    """A project's background as stored: the text without the blank lines around it.

    The first line keeps its indentation. ``''`` means none. Raises
    ``ValueError`` for a non-string or more than 10000 characters.
    """
    if not isinstance(raw, str):
        raise ValueError("background must be a string")
    text = _LEADING_BLANK_LINES.sub("", raw).rstrip()
    if len(text) > BACKGROUND_MAX_CHARS:
        raise ValueError(f"a project background has at most {BACKGROUND_MAX_CHARS} characters, got {len(text)}")
    return text


def json_object(body: bytes) -> dict[str, Any]:
    try:
        data = json.loads(body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise RequestError(HTTP_400, "invalid JSON body") from None
    if not isinstance(data, dict):
        raise RequestError(HTTP_400, "body must be a JSON object")
    return data


def project_view(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "project_id": row["id"],
        "name": row["name"],
        "description": row["description"],
        "work_dir": row["work_dir"],
        "background": row["background"],
        **ProjectExecution.from_row(row).view(),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "task_count": row.get("task_count", 0),
        "active_count": row.get("active_count", 0),
    }


def _project_fields(data: dict[str, Any], *, update: bool) -> dict[str, Any]:
    """The project fields in ``data``, validated; a new project needs a name."""
    if moved := [name for name in MOVED_FIELDS if name in data]:
        raise RequestError(HTTP_400, f"{moved[0]} is an agent setting now: set it with PUT "
                                     "/api/projects/<id>/agent-settings (the project) or "
                                     ".../agent-settings/roles/<role> (one role in the project)")
    fields: dict[str, Any] = {}
    try:
        if not update or "name" in data:
            fields["name"] = project_name(data.get("name"))
        if "description" in data:
            if not isinstance(data["description"], str):
                raise ValueError("description must be a string")
            fields["description"] = data["description"].strip()
        if "work_dir" in data:
            fields["work_dir"] = project_work_dir(data["work_dir"])
        if "background" in data:
            fields["background"] = project_background(data["background"])
        if execution := execution_fields(data):
            fields["execution"] = execution
    except ValueError as exc:
        raise RequestError(HTTP_400, str(exc)) from None
    return fields


def _name_taken(name: str) -> RequestError:
    return RequestError(HTTP_409, f"a project named {name!r} already exists")


async def _existing_project(project_id: str) -> dict[str, Any]:
    project = await db_projects.get_project(project_id)
    if project is None:
        raise RequestError(HTTP_404, f"project {project_id!r} not found")
    return project


async def list_projects() -> dict[str, Any]:
    return {"projects": [project_view(row) for row in await db_projects.list_projects()]}


async def create_project(data: dict[str, Any]) -> dict[str, Any]:
    fields = _project_fields(data, update=False)
    try:
        row = await db_projects.create_project(**fields)
    except asyncpg.UniqueViolationError:
        raise _name_taken(fields["name"]) from None
    return project_view(row)


async def update_project(project_id: str, data: dict[str, Any]) -> dict[str, Any]:
    fields = _project_fields(data, update=True)
    if not fields:
        raise RequestError(
            HTTP_400, "give at least one of " + ", ".join(("name", "description", "work_dir", "background",
                                                          *EXECUTION_FIELDS)),
        )
    try:
        updated = await db_projects.update_project(project_id, **fields)
    except asyncpg.UniqueViolationError:
        raise _name_taken(fields["name"]) from None
    if not updated:
        raise RequestError(HTTP_404, f"project {project_id!r} not found")
    return project_view(await _existing_project(project_id))


async def delete_project(project_id: str) -> dict[str, Any]:
    project = await _existing_project(project_id)
    still_used = RequestError(
        HTTP_409, f"project {project['name']!r} still has tasks; move or delete them first",
    )
    if project["task_count"]:
        raise still_used
    try:
        await db_projects.delete_project(project_id)
    except asyncpg.ForeignKeyViolationError:  # a task was added meanwhile
        raise still_used from None
    return {"deleted": project_id}


def _task_work_dir(raw: Any, project_default: str) -> str:
    """The explicit directory if given, else the project's default."""
    if raw is not None and not isinstance(raw, str):
        raise RequestError(HTTP_400, "work_dir must be a string")
    if raw and raw.strip():
        try:
            return resolve_work_dir(raw)
        except ValueError as exc:
            raise RequestError(HTTP_400, str(exc)) from None
    if project_default:
        return project_default
    raise RequestError(HTTP_400, NO_WORK_DIR)


async def create_task(data: dict[str, Any]) -> dict[str, Any]:
    """Create a task in ``init``, in an existing project or a new one made in the same transaction."""
    requirement = data.get("requirement")
    requirement = requirement.strip() if isinstance(requirement, str) else ""
    if not requirement:
        raise RequestError(HTTP_400, "requirement is required")

    project_id = data.get("project_id") or None  # "" counts as absent
    new_project = data.get("project")
    if (project_id is None) == (new_project is None):
        raise RequestError(HTTP_400, "give exactly one of project_id (an existing project) and project (a new one)")
    project: dict[str, Any] | None = None
    new_fields: dict[str, str] = {}
    if new_project is None:
        if not isinstance(project_id, str):
            raise RequestError(HTTP_400, "project_id must be a string")
        project = await _existing_project(project_id)
        default_dir = project["work_dir"]
    else:
        if not isinstance(new_project, dict):
            raise RequestError(HTTP_400, "project must be an object with a name")
        new_fields = _project_fields(new_project, update=False)
        default_dir = new_fields.get("work_dir", "")

    config = data.get("config")
    if config is not None and not isinstance(config, dict):
        raise RequestError(HTTP_400, "config must be a JSON object")
    task = {
        "task_id": f"task-{uuid.uuid4().hex[:12]}",
        "requirement": requirement,
        "goal_id": data.get("goal_id", ""),
        "work_dir": _task_work_dir(data.get("work_dir"), default_dir),
        "config": config or {},
        "status": "init",
    }
    if project is not None:
        created = await db_tasks.create_task(**task, project_id=project["id"])
    else:
        try:
            async with db_connection.transaction() as conn:
                project = await db_projects.create_project(**new_fields, pool=conn)
                created = await db_tasks.create_task(**task, project_id=project["id"], pool=conn)
        except asyncpg.UniqueViolationError:
            raise _name_taken(new_fields["name"]) from None
    return {
        "task_id": task["task_id"],
        "status": "init",
        "created_at": created["created_at"],
        "project_id": project["id"],
    }


async def move_task(task_id: str, data: dict[str, Any], task_router: TaskRouter) -> dict[str, Any]:
    """Move a task, whatever its status, to another project; a live task follows."""
    project_id = data.get("project_id")
    if not isinstance(project_id, str) or not project_id:
        raise RequestError(HTTP_400, "project_id is required")
    project = await _existing_project(project_id)
    if not await db_tasks.set_task_project(task_id, project_id):
        raise RequestError(HTTP_404, "task not found")
    task_router.set_task_project(task_id, project_id)
    return {"task_id": task_id, "project_id": project_id, "project_name": project["name"]}


async def raise_cap_to_continue(task_id: str, data: dict[str, Any]) -> dict[str, Any]:
    """Raise the project's task spend cap so a task that failed at it can continue; the caller restarts it.

    Only a task that failed at its cap qualifies, and the new ``task_budget_usd``
    must be above what the task has spent, or it would stop again at once.
    The cap is the project's, so it applies to its other tasks too.
    """
    task = await db_tasks.get_task(task_id)
    if task is None:
        raise RequestError(HTTP_404, "task not found")
    if task.get("status") != "failed" or task.get("fail_kind") != db_tasks.FailKind.TASK_BUDGET.value:
        raise RequestError(HTTP_400, "only a task that failed at its spend cap can continue with a higher cap; "
                                     "start it again instead")
    if data.get("task_budget_usd") is None:
        raise RequestError(HTTP_400, "task_budget_usd is required: the new spend cap per task, in US dollars")
    try:
        cap = execution_fields({"task_budget_usd": data["task_budget_usd"]})["task_budget_usd"]
    except ValueError as exc:
        raise RequestError(HTTP_400, str(exc)) from None
    spent = float((await db_llm_records.task_costs([task_id]))[task_id]["cost_usd"])
    if cap <= spent:
        raise RequestError(HTTP_400, f"task_budget_usd must be above the ${format_usd(spent)} the task has "
                                     f"spent; got {format_usd(cap)}")
    if not await db_projects.update_project(task["project_id"], execution={"task_budget_usd": cap}):
        raise RequestError(HTTP_404, f"project {task['project_id']!r} not found")
    return {"task_id": task_id, "project_id": task["project_id"], "task_budget_usd": cap, "spent_usd": spent}
