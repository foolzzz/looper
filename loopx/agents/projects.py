"""Projects over HTTP: request validation, the project view and task creation.

Every task belongs to exactly one project and can move to another at any
time. A task is created in an existing project (``project_id``) or in a new
one made by the same request (``project``). Its work directory is the one
given, else its project's default; there is no global default. A project's
``background`` goes into the system prompt of its task agents (Planner,
Workers, Reviewers), read afresh when each prompt is built, and so do its
harness overrides: ``planner_harness_prompt`` for its Planner and
``worker_harness_prompt`` for its Workers and Reviewers (``agent_settings``).
"""

from __future__ import annotations

import json
import re
import uuid
from typing import Any

import asyncpg

from ..db import connection as db_connection, projects as db_projects, tasks as db_tasks
from . import agent_settings
from .tiers.task import TaskRouter, resolve_work_dir

HTTP_400 = "400 Bad Request"
HTTP_404 = "404 Not Found"
HTTP_409 = "409 Conflict"

NAME_MAX_CHARS = 80
BACKGROUND_MAX_CHARS = 10000
NO_WORK_DIR = "a work directory is required: set one on the task or on its project"
# The project's harness overrides: its Planner's, and its Workers' and Reviewers'.
HARNESS_FIELDS = ("planner_harness_prompt", "worker_harness_prompt")

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
        "planner_harness_prompt": row.get("planner_harness_prompt", ""),
        "worker_harness_prompt": row.get("worker_harness_prompt", ""),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "task_count": row.get("task_count", 0),
        "active_count": row.get("active_count", 0),
    }


def _project_fields(data: dict[str, Any], *, update: bool) -> dict[str, str]:
    """The project fields in ``data``, validated; a new project needs a name."""
    fields: dict[str, str] = {}
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
        for field in HARNESS_FIELDS:
            if field in data:
                fields[field] = agent_settings.harness_prompt(data[field], field=field)
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
            HTTP_400, "give at least one of name, description, work_dir, background, " + " and ".join(HARNESS_FIELDS),
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

    task = {
        "task_id": f"task-{uuid.uuid4().hex[:12]}",
        "requirement": requirement,
        "goal_id": data.get("goal_id", ""),
        "work_dir": _task_work_dir(data.get("work_dir"), default_dir),
        "config": data.get("config", {}),
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
