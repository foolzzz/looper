"""Project and task operations shared by HTTP routes and the Manager tools.

Every task belongs to exactly one project and can move to another at any
time. A new task may choose the Agent Profile of its Planner, Workers or Reviewers
(``profile_overrides``), which wins over every layer for that task only. A task is created in an existing project (``project_id``) or in a new
one made by the same request (``project``). Its work directory is the one
given, else its project's default; there is no global default. A project's
``background`` goes into the system prompt of its task agents (Planner,
Workers, Reviewers), read afresh when each prompt is built. Its execution
settings (``project_execution``) are top-level fields of the project too,
``null`` to inherit. Its agent settings (harness, Agent Profile with its model and
effort, environment, per-run cap) are layers of their own (``agent_settings``,
``POST /api/v1/project_agent_settings/update``). A task that can still run (any but a
completed one: a restart runs a failed, stopped or cancelled task again) may
change its own choices per role (``set_task_agents``): the Agent Profile of its
roles, and how many of its Workers or Reviewers run at once.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any

import asyncpg

from ..db import agent_setting_layers as db_layers, connection as db_connection, llm_records as db_llm_records
from ..db import projects as db_projects, tasks as db_tasks
from .agent_settings import CONCURRENCY_MAX, PLANNER_CHOICE_ROLES, TASK_LIMIT_ROLES, AgentType, SettingsError
from .agent_settings import concurrency, index_layers, task_agents_view
from .agent_settings import TASK_ROLES, AgentSettings, Layer, resolve, resolve_task_concurrency
from .bus import MessageBus
from .message import TASK_ROUTER_ADDRESS, AgentAddress, MessageEnvelope, MessageType, RestartMode, planner_address
from .project_execution import FIELDS as EXECUTION_FIELDS, ProjectExecution, TaskWorkspaces, execution_fields
from .project_execution import LessonsMode, ReviewPolicy, TaskBudgetAction, format_usd
from .step_workspaces import _size_text as size_text
from .task_workspaces import WarningKind, overwrites, task_branch
from .system_prompt import TASK_REQUIREMENT_LIMIT
from .tiers.task import TaskRouter, resolve_work_dir

logger = logging.getLogger(__name__)

HTTP_400 = "400 Bad Request"
HTTP_404 = "404 Not Found"
HTTP_409 = "409 Conflict"

NAME_MAX_CHARS = 80
BACKGROUND_MAX_CHARS = 10000
NO_WORK_DIR = "a work directory is required: set one on the task or on its project"
BASE_MAX_CHARS = 200
TASKS_PAGE = 50
TASKS_PAGE_MAX = 200
# The roles a task may choose the Agent Profile of; the Manager spans every task.
TASK_PROFILE_ROLES = ("planner", "worker", "reviewer")
# Fields a project used to hold that are agent settings now (its layer and its roles' layers).
MOVED_FIELDS = ("planner_harness_prompt", "worker_harness_prompt", "model", "run_budget_usd")

_LEADING_BLANK_LINES = re.compile(r"\A(?:[ \t\r\f\v]*\n)+")


class RequestError(Exception):
    """A request the API refuses, with the HTTP status to answer it with."""

    def __init__(self, status: str, message: str) -> None:
        super().__init__(message)
        self.status = status


def check_requirement(text: str) -> str:
    """``text``, a task requirement, when it is at most ``TASK_REQUIREMENT_LIMIT`` characters; else a 400 that
    says so (a longer one would be clipped where agents read it)."""
    if len(text) > TASK_REQUIREMENT_LIMIT:
        raise RequestError(HTTP_400, f"the requirement has {len(text)} characters; at most "
                                     f"{TASK_REQUIREMENT_LIMIT} are allowed")
    return text


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
        "schedule_count": row.get("schedule_count", 0),
    }


def _project_fields(data: dict[str, Any], *, update: bool) -> dict[str, Any]:
    """The project fields in ``data``, validated; a new project needs a name."""
    if moved := [name for name in MOVED_FIELDS if name in data]:
        raise RequestError(HTTP_400, f"{moved[0]} is an agent setting now: set it with POST "
                                     "/api/v1/project_agent_settings/update (the project) or "
                                     "/api/v1/project_agent_settings/update_role (one role in the project)")
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
    if not update and "task_workspaces" not in data:
        # A new project's tasks run in worktrees of their workspace (the task workspaces RFC, W1).
        fields["execution"] = {**fields.get("execution", {}), "task_workspaces": TaskWorkspaces.WORKTREE.value}
    return fields


class ProjectNameTaken(RequestError):
    """A concurrent or duplicate project name, distinguished from other conflicts."""

    def __init__(self, name: str) -> None:
        super().__init__(HTTP_409, f"a project named {name!r} already exists")


def _name_taken(name: str) -> RequestError:
    return ProjectNameTaken(name)


async def _existing_project(project_id: str, *, manager_id: str | None = None) -> dict[str, Any]:
    project = await db_projects.get_project(project_id, manager_id=manager_id)
    if project is None:
        raise RequestError(HTTP_404, f"project {project_id!r} not found")
    return project


async def list_projects(*, manager_id: str | None = None) -> dict[str, Any]:
    return {"projects": [project_view(row) for row in await db_projects.list_projects(manager_id=manager_id)]}


async def create_project(data: dict[str, Any]) -> dict[str, Any]:
    fields = _project_fields(data, update=False)
    try:
        row = await db_projects.create_project(**fields)
    except asyncpg.UniqueViolationError:
        raise _name_taken(fields["name"]) from None
    return project_view(row)


async def update_project(project_id: str, data: dict[str, Any], *, manager_id: str | None = None) -> dict[str, Any]:
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
    return project_view(await _existing_project(project_id, manager_id=manager_id))


def _still_used(project: dict[str, Any]) -> RequestError:
    return RequestError(HTTP_409, f"project {project['name']!r} still has tasks; move or delete them first")


async def deletable_project(project_id: str) -> dict[str, Any]:
    """The project, when nothing keeps it from being deleted: 404 for an unknown one, 409 while it has tasks or
    schedules."""
    project = await _existing_project(project_id)
    if project["task_count"]:
        raise _still_used(project)
    if count := project.get("schedule_count"):
        raise RequestError(HTTP_409, f"project {project['name']!r} still has {count} "
                                     f"schedule{'s' if count != 1 else ''}; delete them first")
    return project


def _used_by_template(project_id: str, used: tuple[str, str]) -> RequestError:
    return RequestError(HTTP_409, f"project {project_id!r} is used by template {used[0]!r} of Manager {used[1]!r}: "
                                  "change the template first")


async def delete_project(project_id: str) -> dict[str, Any]:
    from ..db import managers as db_managers

    project = await deletable_project(project_id)
    if used := await db_managers.template_using_project(project_id):
        raise _used_by_template(project_id, used)
    try:
        await db_projects.delete_project(project_id)
    except asyncpg.ForeignKeyViolationError:  # a task, a schedule or a template was added meanwhile
        if used := await db_managers.template_using_project(project_id):
            raise _used_by_template(project_id, used) from None
        raise _still_used(project) from None
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


async def create_task_core(
    data: dict[str, Any], *, task_fields: dict[str, Any] | None = None,
    profiles: dict[str, str | None] | None = None, roles: Sequence[dict[str, Any]] = (),
    template_name: str | None = None, lock_manager: bool = False, named: bool | None = None,
    visible_to: str | None = None, show_configuration: bool = False,
    template_display: tuple[str, Sequence[tuple[str, str]]] | None = None,
) -> dict[str, Any]:
    """Place, check and store a ready task for either entry point. Adapters supply channel/template metadata;
    console config, project fields and role overrides share the same persistence path."""
    requirement = data.get("requirement")
    requirement = requirement.strip() if isinstance(requirement, str) else ""
    if not requirement:
        raise RequestError(HTTP_400, "requirement is required")
    check_requirement(requirement)

    project_id = data.get("project_id") or None  # "" counts as absent
    new_project = data.get("project")
    if (project_id is None) == (new_project is None):
        raise RequestError(HTTP_400, "give exactly one of project_id (an existing project) and project (a new one)")
    project: dict[str, Any] | None = None
    new_fields: dict[str, Any] = {}
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
    profiles = await _profile_overrides(data.get("profile_overrides")) if profiles is None else profiles
    work_dir = _task_work_dir(data.get("work_dir"), default_dir)
    project_mode = ProjectExecution.from_row(project if project is not None else new_fields.get("execution")).task_mode
    named = bool(str(data.get("work_dir") or "").strip()) if named is None else named
    workspace = await task_workspace(data, work_dir=work_dir, named=named, project_dir=default_dir,
                                     project_mode=project_mode, visible_to=visible_to)
    task = {
        "task_id": f"task-{uuid.uuid4().hex[:12]}",
        "requirement": requirement,
        "goal_id": data.get("goal_id") or "",
        "work_dir": work_dir,
        "config": config or {},
        "status": "init",
        "workspace_dir": work_dir,
        "workspace_mode": workspace["mode"],
        "workspace_base": workspace["base"],
        "title": db_tasks.task_title(data.get("title")),
        **(task_fields or {}),
    }

    async def persist(conn: Any = None) -> dict[str, Any]:
        nonlocal project
        if lock_manager:
            from ..db import managers as db_managers
            if not await db_managers.lock_for_task(task.get("manager_id", "manager"), conn=conn):
                raise RequestError(HTTP_409, "this Manager was turned off or deleted; the task was not created")
        if project is None:
            project = await db_projects.create_project(**new_fields, pool=conn)
        created = await db_tasks.create_task(**task, project_id=project["id"], pool=conn)
        if profiles:
            await db_layers.set_profile_overrides(conn, task["task_id"], profiles)
        if roles:
            from ..db import task_roles as db_roles
            from .tiers.journal import JournalKind, SUMMARY_MAX_CHARS, clip
            for role in roles:
                summary = f"Role {role['role_key']} v1 from the task template {template_name}"
                await db_roles.add_first_version(conn, task["task_id"], role["role_key"], {
                    **role, "reason": f"from template {template_name}", "via": "template"},
                    {"step_id": "", "kind": JournalKind.ROLE_DEFINED.value,
                     "summary": clip(summary, SUMMARY_MAX_CHARS), "learnings": []})
        return created

    try:
        if project is not None and not profiles and not lock_manager and not roles:
            created = await persist()
        else:
            async with db_connection.transaction() as conn:
                created = await persist(conn)
    except asyncpg.UniqueViolationError:
        # The Manager adapter keeps its existing concurrent-create instruction.
        raise _name_taken(new_fields["name"]) from None
    except asyncpg.ForeignKeyViolationError:
        raise RequestError(HTTP_400, "an Agent Profile the task chose was removed meanwhile; choose again") from None
    shown = task["title"] or task["goal_id"] or task["task_id"]
    block = (await configuration_block(task["task_id"], shown, project, work_dir, new_project is not None,
                                       workspace=workspace["check"], template=template_display)
             if show_configuration else None)
    return {
        "task_id": task["task_id"],
        "status": "init",
        "created_at": created["created_at"],
        "project_id": project["id"],
        # How it runs in its workspace, and what to know before starting it (task_workspaces.check).
        "workspace": workspace["check"],
        "project": project,
        "project_created": new_project is not None,
        "work_dir": work_dir,
        "name": shown,
        "configuration": block,
    }


async def task_workspace(data: dict[str, Any], *, work_dir: str, named: bool, project_dir: str,
                         project_mode: TaskWorkspaces, visible_to: str | None = None) -> dict[str, Any]:
    """A new task's ``workspace_mode`` (as ``data`` asks, else by ``task_workspaces.resolve_mode``) and ``base``,
    checked: the mode, the base and the workspace check, or a 400. ``visible_to``: the Manager the check's
    warnings name tasks to; another Manager's tasks stay anonymous."""
    from . import task_workspaces

    requested, base = data.get("workspace_mode"), data.get("base")
    if requested is not None and not isinstance(requested, str):
        raise RequestError(HTTP_400, "workspace_mode must be a string")
    if base is not None and (not isinstance(base, str) or len(base) > BASE_MAX_CHARS or "\n" in base):
        raise RequestError(HTTP_400, f"base must be a branch, tag or commit of at most {BASE_MAX_CHARS} characters")
    base = (base or "").strip()
    try:
        mode = task_workspaces.resolve_mode(requested=requested or None, named=work_dir if named else "",
                                            project_dir=project_dir, project_mode=project_mode)
    except ValueError as exc:
        raise RequestError(HTTP_400, str(exc)) from None
    if base and mode is not TaskWorkspaces.WORKTREE:
        raise RequestError(HTTP_400, "base is where a task's worktree starts: give it with workspace_mode worktree")
    try:
        check = await task_workspaces.check(work_dir, mode=mode, base=base, asked=bool(requested),
                                            visible_to=visible_to)
    except task_workspaces.TaskWorkspaceError as exc:
        raise RequestError(HTTP_400, str(exc)) from None
    return {"mode": mode.value, "base": base, "check": check}


async def check_workspace(query: dict[str, str]) -> dict[str, Any]:
    """``workspaces.check``: what a new task in ``dir`` (else ``project_id``'s directory) would run in,
    with ``mode`` and ``base`` as the create form has them, and the warnings."""
    project = await _existing_project(query["project_id"]) if query.get("project_id") else None
    named = (query.get("dir") or "").strip()
    default_dir = str((project or {}).get("work_dir") or "")
    work_dir = _task_work_dir(named or None, default_dir)
    execution = ProjectExecution.from_row(project) if project is not None else ProjectExecution(
        task_workspaces=TaskWorkspaces.WORKTREE)  # a project the form is about to create
    from . import task_workspaces

    workspace = await task_workspace({"workspace_mode": query.get("mode") or None, "base": query.get("base") or None},
                                     work_dir=work_dir, named=bool(named), project_dir=default_dir,
                                     project_mode=execution.task_mode)
    default = task_workspaces.resolve_mode(requested=None, named=work_dir if named else "", project_dir=default_dir,
                                           project_mode=execution.task_mode)
    return {**workspace["check"], "default_mode": default.value, "notes_on": execution.notes_on}


async def _profile_overrides(raw: Any) -> dict[str, str]:
    """``profile_overrides``, a new task's (or a schedule's) choice of Agent Profile per role, checked as
    ``profile_choices`` checks a task's choice (by id or name), as role -> profile id; an empty or null entry
    chooses nothing."""
    if isinstance(raw, dict):
        raw = {role: None if profile == "" else profile for role, profile in raw.items()}
    chosen = await profile_choices(raw, clearing=True, field="profile_overrides")
    return {role: profile_id for role, profile_id in chosen.items() if profile_id is not None}


def task_listed(task: Mapping[str, Any]) -> dict[str, Any]:
    """A task's compact list row, also the common fields of its compact detail."""
    return {"task_id": task["id"], "name": task["name"], "status": task["status"],
            "group": db_tasks.task_group(task.get("status")).value,
            "project": task.get("project_name"), "created_at": task.get("created_at")}


async def list_tasks_page(
    *, limit: int = TASKS_PAGE, project_id: str | None = None, status: str | None = None,
    group: db_tasks.TaskGroup | None = None, schedule_id: str | None = None, before: str | None = None,
    manager_id: str | None = None, query: str = "", compact: bool = False, search_limit: int = 200,
) -> dict[str, Any]:
    """A console page or the Manager's compact list; keyword searches retain its bounded recent-task search."""
    from ..db.llm_records import task_costs

    query = " ".join(query.split()).casefold()
    if before is not None and not await db_tasks.task_exists(before):
        raise RequestError(HTTP_400, f"before must be a task id; {before!r} is not one")
    filters = {"project_id": project_id, "group": group, "schedule_id": schedule_id}
    fetch_limit = (search_limit if query else limit) if compact else limit + 1
    search = {"query": query} if query and not compact else {}
    page = await db_tasks.list_tasks(limit=fetch_limit, status=status, before=before,
                                     manager_id=manager_id, **filters, **search)
    if compact:
        if query:
            page = [t for t in page if query in " ".join(
                str(t.get(key) or "") for key in ("id", "name", "requirement", "project_name", "goal_id")).casefold()]
        return {"tasks": [task_listed(task) for task in page[:limit]], "more": len(page) > limit}

    tasks = page[:limit]
    counts = await db_tasks.count_by_status(**filters, **search)
    costs = await task_costs([t["id"] for t in tasks])
    from ..db import managers as db_managers
    manager_names = {m["id"]: m["name"] for m in await db_managers.list_managers(include_deleted=True)}
    result = []
    for t in tasks:
        result.append({
            "task_id": t.get("id", ""),
            "requirement": t.get("requirement", ""),
            "name": t.get("name", ""),
            "goal_id": t.get("goal_id", ""),
            "project_id": t.get("project_id", ""),
            "project_name": t.get("project_name", ""),
            "work_dir": t.get("work_dir", ""),
            # The Manager the task belongs to.
            "manager_id": t.get("manager_id") or "manager",
            "manager_name": manager_names.get(t.get("manager_id") or "manager"),
            # The task template it came from, as it said then (null for none).
            "template": t.get("template"),
            "status": t.get("status", "pending"),
            "group": db_tasks.task_group(t.get("status")).value,
            "phase": t.get("phase", "pending"),
            "planner_id": t.get("planner_id"),
            "correlation_id": t.get("correlation_id", ""),
            "created_at": t.get("created_at"),
            "completed_at": t.get("completed_at"),
            # The schedule that created it (null for any other task, or once the schedule is deleted).
            "schedule_id": t.get("schedule_id"),
            "schedule_name": t.get("schedule_name"),
            "continues_schedule_session": t.get("continues_schedule_session", False),
            # What the task's Planner, Workers and Reviewers spent so far.
            "cost": costs.get(t.get("id", "")),
        })
    return {"tasks": result, "next_before": tasks[-1]["id"] if len(page) > limit else None, "counts": counts}


async def task_detail(task_id: str, *, compact: bool = False) -> dict[str, Any]:
    """A task's detail for the console, or its compact Manager view with the same tool contract."""
    from .wait_service import waits_view

    raw = await db_tasks.get_task_with_steps(task_id)
    if raw is None:
        raise RequestError(HTTP_404, "task not found")
    cost = (await db_llm_records.task_costs([task_id])).get(task_id)
    agents = await agents_view(raw)
    waits = await waits_view(task_id)
    if compact:
        return await _compact_task_detail(raw, cost=cost, agents=agents, waits=waits)

    from ..db import api_clients as db_api_clients, managers as db_managers
    from ..db.agent_setting_layers import profile_overrides
    from ..db.llm_records import list_task_runs
    from ..db.manager_actions import for_task as manager_actions
    from ..db.task_changes import requirement_versions
    from . import gate_service, profile_registry
    from .task_roles import roles_view, run_roles
    from .tiers.journal import progress_notes

    ran_as = run_roles(await list_task_runs([task_id]), raw.get("steps", []))
    overrides = await profile_overrides(task_id)
    versions = await requirement_versions(task_id)
    actions = await manager_actions(task_id)
    owner = await db_managers.get_manager(raw.get("manager_id") or "manager", include_deleted=True)
    client_id = raw.get("client_id")
    client_name = (await db_api_clients.names([client_id])).get(client_id) if client_id else None
    roles = await roles_view(task_id, raw.get("steps", []))
    gates = await gate_service.gates_view(task_id)
    task_workspace = await task_workspace_view(raw)
    workspaces = await step_workspaces_view(task_id, raw.get("steps", []),
                                            in_worktree=bool(task_workspace["worktree"]))
    try:
        from .room import task_view
        room = await task_view(task_id, limit=1)
    except Exception:
        # The room's numbers are help on the page: without them, the task still shows.
        logger.warning("cannot read the room of task %s", task_id, exc_info=True)
        room = None
    return {
        "task_id": raw.get("id", ""),
        "requirement": raw.get("requirement", ""),
        "name": raw.get("name", ""),
        "title": raw.get("title") or "",
        # Every version of an edited requirement, oldest first (none for one never edited): version, text, via,
        # created_at, delivered_at (when the Planner had the edit in a turn).
        "requirement_versions": versions,
        # What the user added to the task while it ran, oldest first: text, via, created_at, delivered_at.
        "supplements": [{"text": entry["summary"], "via": entry.get("via") or "", "created_at": entry["created_at"],
                         "delivered_at": entry.get("delivered_at")}
                        for entry in raw.get("journal", []) if entry.get("kind") == "supplement"],
        # What the Manager did to the task through its tools, or asked the user to confirm, newest first.
        "manager_actions": actions,
        # The Manager the task belongs to, and the task template it was created from (as it said then), or null.
        "manager_id": raw.get("manager_id") or "manager",
        "manager_name": owner["name"] if owner else None,
        "template": raw.get("template"),
        # The API client the task was created through (also a revoked one), or null.
        "client": {"id": client_id, "name": client_name} if client_id and client_name else None,
        "goal_id": raw.get("goal_id", ""),
        "project_id": raw.get("project_id", ""),
        "project_name": raw.get("project_name", ""),
        "status": raw.get("status", "pending"),
        "group": db_tasks.task_group(raw.get("status")).value,
        "phase": raw.get("phase", "pending"),
        "planner_id": raw.get("planner_id"),
        "correlation_id": raw.get("correlation_id", ""),
        "work_dir": raw.get("work_dir", ""),
        "config": raw.get("config", {}),
        # The Agent Profile the task chose for some of its roles, over the layers (agent_settings.Layer.TASK).
        "profile_overrides": await profile_registry.profile_views(overrides),
        # Each task role's Agent Profile, and its Workers' and Reviewers' concurrency, with where each comes from;
        # the task's own choices are source "task" (projects.agents_view).
        "agents": agents,
        # The roles the task's Planner defined for its steps (current version, earlier versions, the steps that
        # name each, its numbers), and the built-in Worker's and Reviewer's numbers (task_roles.roles_view).
        "roles": roles["roles"],
        "builtin_role_stats": roles["builtin"],
        # The user gates of its plan, in plan order: state, agenda, findings, how each closed, and whom each
        # waiting one waits for (gate_service.gate_view); their discussions are at .../gates/<step>/messages.
        "gates": gates,
        # Wait steps in plan order, with their check, timing and latest result (wait_service.wait_view).
        # Only waiting rows accept .../waits/<step>/resume or /cancel; the Planner settles moved rows.
        "waits": waits,
        # Its workspace: the directory it names, how it runs there, and the worktree it runs in (its branch,
        # where it started, the commands that take the work over), null for none (task_workspaces).
        "task_workspace": task_workspace,
        # Its steps' worktrees, in a project with step workspaces (null when it had none): where its branch
        # started, each worktree oldest first, and the commands that undo the task's commits (none for a task
        # in a worktree of its own: they would change its branch, not the user's).
        "step_workspaces": workspaces,
        # Its agent room in numbers: whether its project has it on, its messages by kind and its unanswered
        # questions, and each step's (sent, received, unanswered); the messages are at .../room.
        "room": {**{key: room[key] for key in ("enabled", "counts", "by_step")},
                 "consults": {k: v for k, v in room["consults"].items() if k != "list"}} if room else None,
        # The role each Worker and Reviewer run that ran as one ran as, by agent id (task_roles.run_roles); only
        # in a task with such runs.
        **({"run_roles": ran_as} if ran_as else {}),
        "origin_channel_id": raw.get("origin_channel_id", ""),
        "result_summary": raw.get("result_summary"),
        "error": raw.get("error"),
        "fail_kind": raw.get("fail_kind"),  # "task_budget": it failed at its project's task spend cap
        # The task's spend cap as the Planner enforces it (its own, else its project's); null for none.
        "task_budget_usd": ProjectExecution.from_row(
            {"id": raw.get("project_id"), "task_budget_usd": raw.get("task_budget_usd")}).task_budget_usd,
        # The task's own cap (a scheduled run's), raised alone when it continues at its cap; null for none.
        "own_budget_usd": raw.get("budget_usd"),
        "schedule_id": raw.get("schedule_id"),
        "schedule_name": raw.get("schedule_name"),
        "continues_schedule_session": raw.get("continues_schedule_session", False),
        "created_at": raw.get("created_at"),
        "updated_at": raw.get("updated_at"),
        "completed_at": raw.get("completed_at"),
        "attempt_ended_at": raw.get("attempt_ended_at"),
        "steps": raw.get("steps", []),
        # The task journal, oldest first: an entry per accepted step with its summary and learnings.
        "journal": raw.get("journal", []),
        # The Planner's progress notes (journal kind progress), newest first: done, now, next, risks, estimate, at.
        "progress": progress_notes(raw.get("journal", [])),
        # What the task's Planner, Workers and Reviewers spent: the total, by role, and the run count.
        "cost": cost,
    }


async def _compact_task_detail(task: dict[str, Any], *, cost: dict[str, Any] | None, agents: dict[str, Any],
                               waits: list[dict[str, Any]]) -> dict[str, Any]:
    from ..db import task_changes as db_changes, task_roles as db_roles, user_gates as db_user_gates
    from .tiers.journal import clip

    task_id = task["id"]
    journal = task.get("journal") or []
    supplements = [entry for entry in journal if entry.get("kind") == db_changes.SUPPLEMENT]
    progress = [entry for entry in journal if entry.get("kind") == "progress"]
    return {
        **task_listed(task),
        "phase": task.get("phase"),
        "work_dir": task.get("work_dir"),
        "requirement": task.get("requirement"),
        "steps": [{"step": s.get("step_key") or s.get("id"), "status": s.get("status"),
                   "kind": s.get("kind") or "work",
                   "instruction": clip(s.get("instruction") or "", 200),
                   # The roles its Planner defined that the step runs as and is reviewed by, when it has them.
                   **({"role": s["role_key"]} if s.get("role_key") else {}),
                   **({"review_roles": [item["role"] if item.get("required") is not False
                                        else {"role": item["role"], "required": False}
                                        for item in s["review_roles"]]} if s.get("review_roles") else {})}
                  for s in task.get("steps") or []],
        "waits": waits,
        # The roles the task's Planner defined, when it has any (task_roles).
        **({"roles": roles} if (roles := [{"id": r["role_key"], "kind": r["kind"], "version": r["version"]}
                                          for r in await db_roles.current(task_id)]) else {}),
        "supplements": [{"text": clip(entry["summary"], 300), "delivered": entry.get("delivered_at") is not None}
                        for entry in supplements],
        "latest_progress": progress[-1]["summary"] if progress else None,
        "result": clip(task.get("result_summary") or task.get("error") or "", 1_000) or None,
        "cost_usd": (cost or {}).get("cost_usd"),
        # How many of its Workers and Reviewers run at once, with the source; its own limits (task).
        "concurrency": {role: view["concurrency"] for role, view in agents["roles"].items()
                        if "concurrency" in view},
        # Its user gates (looper_task_gate_reply answers one that waits for the user), when it has any.
        **({"gates": gates} if (gates := [task_gate_view(g) for g in await db_user_gates.list_for_task(task_id)
                                          if g.get("state") != "pending"]) else {}),
    }


def task_gate_view(gate: Mapping[str, Any]) -> dict[str, Any]:
    """A gate as looper_tasks shows it: its label, state, topic, questions (the open ones' options), and how it
    closed."""
    from .tiers.journal import clip

    view: dict[str, Any] = {"gate": gate["step_key"], "state": gate["state"], "topic": clip(gate["topic"], 200),
                            "questions": [{"id": item.get("id"), "question": clip(str(item.get("question") or ""), 200),
                                           "status": item.get("status"),
                                           **({"options": item["options"]} if item.get("options") else {}),
                                           **({"answer": clip(str(item["answer"]), 200)} if item.get("answer")
                                              else {})}
                                          for item in gate.get("agenda") or []]}
    if gate["state"] in ("open", "discussing"):
        view["waiting_for"] = "planner" if gate.get("waiting_for_planner") else "user"
    if gate.get("summary"):
        view["summary"] = gate["summary"]
    if gate.get("resolution"):
        view["resolution"] = clip(gate["resolution"], 300)
    return view


async def task_workspace_view(task: dict[str, Any]) -> dict[str, Any]:
    from ..db import task_worktrees as db_task_worktrees
    from .task_workspaces import TaskWorktree

    row = await db_task_worktrees.get(str(task["id"]))
    return {"workspace_dir": task.get("workspace_dir") or task.get("work_dir") or "",
            "mode": task.get("workspace_mode") or "in_place", "base": task.get("workspace_base") or "",
            "worktree": TaskWorktree.from_row(row).view() if row is not None else None}


async def step_workspaces_view(task_id: str, steps: list[dict[str, Any]], *,
                               in_worktree: bool = False) -> dict[str, Any] | None:
    from ..db import step_workspaces as db_step_workspaces
    from . import step_workspaces

    rows = await db_step_workspaces.list_for_task(task_id)
    if not rows:
        return None
    start = await db_step_workspaces.task_start(task_id)
    view = step_workspaces.task_view(task_id, rows, start_branch=start[0], start_commit=start[1],
                                     step_keys={str(s["id"]): str(s.get("step_key") or "") for s in steps})
    return {**view, "undo": []} if in_worktree else view


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


async def start_ready_task(task_id: str, bus: MessageBus, source: AgentAddress) -> dict[str, Any]:
    """Have the TaskRouter start a ready task (created, never started; ``TASK_START``), from the dashboard or the
    Manager: 404 for an unknown task, 400 for one that is not ready. It runs at once."""
    task = await db_tasks.get_task(task_id)
    if task is None:
        raise RequestError(HTTP_404, "task not found")
    if task["status"] != db_tasks.READY:
        raise RequestError(HTTP_400, f"the task is {task['status']}, not ready: only a task that was created and "
                                     "never started can be started")
    await bus.publish(MessageEnvelope.create(
        source=source, target=TASK_ROUTER_ADDRESS, message_type=MessageType.TASK_START, payload={"task_id": task_id},
    ))
    return {"started": task_id, "project_id": task["project_id"]}


async def check_task_control(task_id: str, action: str) -> dict[str, Any]:
    """The task when its state allows a control action, also used before asking for confirmation."""
    task = await db_tasks.get_task(task_id)
    if task is None:
        raise RequestError(HTTP_404, f"no task {task_id}: find its id with looper_tasks")
    status = task["status"]
    if action == "stop":
        if status not in db_tasks.ACTIVE_STATUSES:
            raise RequestError(HTTP_409, f"task {task_id} is {status}: only a running task can be stopped")
    elif action == "cancel":
        if db_tasks.task_group(status) not in (db_tasks.TaskGroup.IN_PROGRESS, db_tasks.TaskGroup.NEEDS_YOU):
            raise RequestError(HTTP_409, f"task {task_id} is {status}: only a running task can be cancelled")
    elif action in ("restart", "start_over"):
        if status not in db_tasks.RESTARTABLE_STATUSES:
            raise RequestError(HTTP_409, f"task {task_id} is {status}: restart and start_over run a stopped, "
                                         "failed or cancelled task again (a ready one starts with action start)")
        if not (task.get("work_dir") or "").strip():
            raise RequestError(HTTP_400, f"task {task_id} has no directory: set one with looper_task_update first")
    else:
        raise RequestError(HTTP_400, f"unknown task control action {action!r}")
    return task


async def control_task(task_id: str, action: str, bus: MessageBus, source: AgentAddress) -> None:
    """Check a stop, cancel or restart and send it once to the TaskRouter."""
    await check_task_control(task_id, action)
    message_type = {"stop": MessageType.TASK_STOP, "cancel": MessageType.TASK_CANCEL,
                    "restart": MessageType.TASK_RESTART, "start_over": MessageType.TASK_RESTART}[action]
    payload: dict[str, Any] = {"task_id": task_id}
    if action in ("restart", "start_over"):
        payload["mode"] = (RestartMode.START_OVER if action == "start_over" else RestartMode.CONTINUE).value
    await bus.publish(MessageEnvelope.create(
        source=source, target=TASK_ROUTER_ADDRESS, message_type=message_type, payload=payload,
    ))


# How a change to a task came: on the task page, or in a message sent to its Planner on the Chat page.
VIA_TASK_PAGE = "task_page"
VIA_CHAT = "chat"
# The statuses a task's requirement can be edited in: not started, or started (whatever its Planner is doing).
EDITABLE_STATUSES = (db_tasks.READY, *db_tasks.ACTIVE_STATUSES)
# The statuses a task's own agents can change in: those its requirement can, and those of a task that ended without
# completing, which a restart runs again with its new agents. Only a completed task's agents are final.
AGENTS_EDITABLE_STATUSES = (*EDITABLE_STATUSES, *db_tasks.RESTARTABLE_STATUSES)


async def add_task_supplement(
    task_id: str, text: str, *, via: str, wake: Callable[[int | None], Awaitable[Any]], best_effort: bool = False,
) -> Any:
    """Store a supplement before waking its Planner through the entry's existing delivery protocol.

    Chat wakes it with USER_MESSAGE, carrying the supplement id and reply context; the Manager's tool uses
    TASK_CHANGE. Chat may fall back to the Planner's durable write if storage fails, as it did before.
    """
    from ..db import task_changes
    from .tiers.journal import SUPPLEMENTS_MAX_CHARS, kept_text

    supplement_id = None
    try:
        supplement_id = await task_changes.add_supplement(task_id, kept_text(text, SUPPLEMENTS_MAX_CHARS), via=via)
    except Exception:
        if not best_effort:
            raise
        logger.exception("could not store a chat message to %s as a supplement", planner_address(task_id).agent_id)
    return await wake(supplement_id)


async def store_chat_supplement(target: AgentAddress, text: str) -> int | None:
    """The durable supplement id for a Planner chat, or None when the Planner must store it itself."""
    if target.agent_type != "planner":
        return None

    async def stored(supplement_id: int | None) -> int | None:
        return supplement_id

    return await add_task_supplement(target.agent_id.removeprefix("planner-"), text, via=VIA_CHAT,
                                     wake=stored, best_effort=True)


async def edit_requirement(
    task_id: str, data: dict[str, Any], *, task_router: TaskRouter, bus: MessageBus, source: AgentAddress, via: str,
) -> dict[str, Any]:
    """Make ``data["requirement"]`` the task's requirement, keeping the old text as a version: 404 for an unknown
    task, 409 for one that has ended, 400 for an empty or unchanged text. A running task's Planner gets the edit in
    a turn (``TASK_CHANGE``), and so does one whose Planner is starting, at its PLAN turn; a task not started
    plans the new text."""
    from ..db import task_changes

    requirement = data.get("requirement")
    if not isinstance(requirement, str) or not requirement.strip():
        raise RequestError(HTTP_400, "requirement must be a non-empty string")
    check_requirement(requirement)
    task = await db_tasks.get_task(task_id)
    if task is None:
        raise RequestError(HTTP_404, "task not found")
    if task["status"] not in EDITABLE_STATUSES:
        raise RequestError(HTTP_409, f"the task is {task['status']}: only a task that has not ended can change; "
                                     "start it again to run the requirement it has")
    if requirement == task["requirement"]:
        raise RequestError(HTTP_400, "the requirement is unchanged")
    not_started = task["status"] == db_tasks.READY
    version = await task_changes.edit_requirement(task_id, requirement, via=via, delivered=not_started)
    if version is None:
        raise RequestError(HTTP_404, "task not found")
    if task_router.has_planner(task_id):
        await bus.publish(MessageEnvelope.create(
            source=source, target=planner_address(task_id), message_type=MessageType.TASK_CHANGE,
            payload={"task_id": task_id}))
    return {"task_id": task_id, "version": version, "requirement": requirement}


async def rename_task(task_id: str, data: dict[str, Any], *, via: str) -> dict[str, Any]:
    """Make ``data["title"]`` the task's title, in any status (a title is a label, not part of the run): 404 for an
    unknown task, 400 for a missing, non-string or empty title. A change is noted in the task's journal (a
    supplement the Planner needs no turn for); naming it what it is called changes nothing."""
    from ..db import task_changes

    title = data.get("title")
    if not isinstance(title, str):
        raise RequestError(HTTP_400, "title must be a string")
    stored = db_tasks.task_title(title)
    if stored is None:
        raise RequestError(HTTP_400, "title must not be empty")
    task = await db_tasks.get_task(task_id)
    if task is None:
        raise RequestError(HTTP_404, "task not found")
    if stored != task.get("title"):
        if not await db_tasks.set_task_title(task_id, stored):
            raise RequestError(HTTP_404, "task not found")
        old = db_tasks.task_name(title=task.get("title"), goal_id=task.get("goal_id"),
                                 requirement=task.get("requirement"), task_id=task_id)
        await task_changes.add_supplement(task_id, f"Renamed from {old!r} to {stored!r}.", via=via, delivered=True)
    return {"task_id": task_id, "title": stored}


# What a task's own choices per role are (``set_task_agents``), and when a new profile applies.
TASK_AGENT_FIELDS = ("profiles", "concurrency", "system_prompt_modes")
PROFILES_NOTE = ("a Worker or Reviewer uses its new profile from its next step on; the Planner moves to its new "
                 "profile at its next turn, after a handoff")


async def agents_view(task: dict[str, Any]) -> dict[str, Any]:
    """Task ``task``'s agents, read now: each task role's Agent Profile and concurrency with their sources
    (``agent_settings.task_agents_view``), the most a limit may be, and whether the task can change its own
    choices (it has not completed)."""
    project_id = task.get("project_id")
    layers = index_layers(await db_layers.list_layers(project_id, task_id=task["id"]), project_id)
    return {"roles": task_agents_view(layers, project_id), "concurrency_max": CONCURRENCY_MAX,
            "editable": task.get("status") in AGENTS_EDITABLE_STATUSES}


def _prompt_modes(raw: Any) -> dict[str, str | None]:
    """``system_prompt_modes``, a task's own append or replace per role (null drops it). 400 for another role or value."""
    if not isinstance(raw, dict):
        raise RequestError(HTTP_400, 'system_prompt_modes must be an object: {"planner" | "worker" | "reviewer": '
                                     '"append" | "replace" | null}')
    modes: dict[str, str | None] = {}
    for role, value in raw.items():
        if role not in TASK_PROFILE_ROLES:
            raise RequestError(HTTP_400, f"system_prompt_modes: a task sets the mode of its "
                                         f"{', '.join(TASK_PROFILE_ROLES)} only, not {role!r}")
        if value is not None and value not in ("append", "replace"):
            raise RequestError(HTTP_400, f'system_prompt_modes.{role} must be "append", "replace" or null; got {value!r}')
        modes[role] = value
    return modes


async def profile_choices(raw: Any, *, clearing: bool, field: str = "profiles",
                          pool: asyncpg.Pool | asyncpg.Connection | None = None) -> dict[str, str | None]:
    """``profiles`` (the request's ``field``), a task's choice of Agent Profile per role (by id or name), as role ->
    profile id (None: drop the task's choice, only when ``clearing``); each one registered and able to run its role.
    400 otherwise."""
    from ..db import profile_registry as db_profiles
    from .profile_registry import RegistryError, check_selection

    if not raw:
        return {}
    if not isinstance(raw, dict):
        raise RequestError(HTTP_400, f'{field} must be an object: {{"planner" | "worker" | "reviewer": profile}}')
    registered: list[dict[str, Any]] | None = None
    chosen: dict[str, str | None] = {}
    for role, wanted in raw.items():
        if role not in TASK_PROFILE_ROLES:
            raise RequestError(HTTP_400, f"{field}: a task chooses the Agent Profile of its "
                                         f"{', '.join(TASK_PROFILE_ROLES)} only, not {role!r}")
        if wanted is None:
            if not clearing:
                raise RequestError(HTTP_400, f"{field}.{role}: give an Agent Profile, or leave the role out")
            chosen[role] = None
            continue
        if not isinstance(wanted, str):
            raise RequestError(HTTP_400, f"{field}.{role}: give an Agent Profile's id or name, or null")
        if registered is None:
            registered = await db_profiles.list_profiles(pool=pool)
        match = next((p for p in registered if p["id"] == wanted), None) or next(
            (p for p in registered if str(p["name"]).casefold() == wanted.strip().casefold()), None)
        if match is None:
            names = ", ".join(str(p["name"]) for p in registered[:20])
            raise RequestError(HTTP_400, f"{field}.{role}: no Agent Profile {wanted!r}; registered: {names}")
        try:
            await check_selection(match["id"], [role], pool=pool)
        except RegistryError as exc:
            raise RequestError(HTTP_400, f"{field}.{role}: {exc}") from None
        chosen[role] = match["id"]
    return chosen


# The roles whose Agent Profile a task's Planner may choose, from the profiles its project allows (``choosable_profiles``, ``ops.planner``).
_CHOOSABLE = tuple(role.value for role in PLANNER_CHOICE_ROLES)


async def choosable_profiles(project_id: str | None, *,
                             pool: asyncpg.Pool | asyncpg.Connection | None = None) -> tuple[list[dict[str, Any]], bool]:
    """The Agent Profiles (``profile_registry.PROFILE_JSON``) project ``project_id``'s Planners may choose, and
    whether the project restricts them: with a list, the list; with an empty list, or no project, every registered
    profile that can run the Worker or the Reviewer. Whether one can run a given role is checked where it is
    chosen. The one place that decides; every Planner choice goes through it."""
    from ..db import profile_registry as db_profiles
    from .profile_registry import AgentProfile, servable_roles

    listed = await db_projects.allowed_profiles(project_id, pool=pool) if project_id else []
    if listed:
        return listed, True
    return [row for row in await db_profiles.list_profiles(pool=pool)
            if set(servable_roles((profile := AgentProfile.from_row(row)).runtime.capabilities,
                                  profile.runtime.type)) & set(_CHOOSABLE)], False


def allowed_profile_view(row: dict[str, Any]) -> dict[str, Any]:
    """A profile on a project's list (``profile_registry.PROFILE_JSON``) as the list shows it: what it runs, which of
    the Worker and Reviewer it can run, and its input and output price per million tokens (None where unknown)."""
    from .profile_registry import ResolvedAgentProfile, servable_roles
    from .runtimes.pricing import model_price

    profile = ResolvedAgentProfile.from_row(row)
    price = model_price(profile.resolved_model, profile.model)
    return {"profile_id": profile.id, "name": profile.name, "runtime": profile.runtime.type, "model": profile.model,
            "effort": profile.effort or None,
            "roles": [role for role in servable_roles(profile.runtime.capabilities, profile.runtime.type)
                      if role in _CHOOSABLE],
            "usd_per_million_tokens": {"input": price.input_usd_per_mtok, "output": price.output_usd_per_mtok}}


async def allowed_profiles_view(project_id: str) -> dict[str, Any]:
    """The Agent Profiles project ``project_id``'s Planners may choose for their tasks' Workers and Reviewers, by
    name (``profiles``; empty by default, which lets them choose any profile), and what they may choose
    (``choosable``) under ``restricted``. 404 for an unknown project."""
    if await db_projects.get_project(project_id) is None:
        raise RequestError(HTTP_404, f"project {project_id!r} not found")
    choosable, restricted = await choosable_profiles(project_id)
    return {"project_id": project_id, "restricted": restricted,
            "profiles": [allowed_profile_view(row) for row in await db_projects.allowed_profiles(project_id)],
            "choosable": [allowed_profile_view(row) for row in choosable]}


async def set_allowed_profiles(project_id: str, data: dict[str, Any]) -> dict[str, Any]:
    """Make ``profile_ids`` (ids or names) the whole list of the Agent Profiles project ``project_id``'s Planners may
    choose; ``[]`` lifts the restriction (they may choose any profile), and a non-empty list drops its tasks'
    Planners' choices of a profile it leaves out. Each profile must
    be registered and able to run the Worker or the Reviewer. 400 otherwise, and nothing changes; 404 for an
    unknown project. Answers the list as it is then."""
    from ..db import profile_registry as db_profiles
    from .profile_registry import AgentProfile, servable_roles

    unknown = sorted(set(data) - {"profile_ids"})
    if unknown or "profile_ids" not in data:
        raise RequestError(HTTP_400, 'give {"profile_ids": [...]}: the whole list, by id or name ([] empties it)')
    wanted = data["profile_ids"]
    if not isinstance(wanted, list) or any(not isinstance(item, str) for item in wanted):
        raise RequestError(HTTP_400, "profile_ids must be a list of Agent Profile ids or names")
    if await db_projects.get_project(project_id) is None:
        raise RequestError(HTTP_404, f"project {project_id!r} not found")
    registered = await db_profiles.list_profiles() if wanted else []
    ids: list[str] = []
    for item in wanted:
        match = next((p for p in registered if p["id"] == item), None) or next(
            (p for p in registered if str(p["name"]).casefold() == item.strip().casefold()), None)
        if match is None:
            names = ", ".join(str(p["name"]) for p in registered[:20])
            raise RequestError(HTTP_400, f"profile_ids: no Agent Profile {item!r}; registered: {names}")
        profile = AgentProfile.from_row(match)
        if not set(servable_roles(profile.runtime.capabilities, profile.runtime.type)) & set(_CHOOSABLE):
            raise RequestError(HTTP_400, f"profile_ids: Agent Profile {match['name']!r} can run neither the worker "
                                         "nor the reviewer, so a Planner could never choose it")
        if match["id"] not in ids:
            ids.append(match["id"])
    try:
        await db_projects.set_allowed_profiles(project_id, ids)
    except asyncpg.ForeignKeyViolationError:
        raise RequestError(HTTP_404, "the project, or an Agent Profile on the list, was removed meanwhile") from None
    return await allowed_profiles_view(project_id)


def _concurrency_limits(raw: Any, *, field: str = "concurrency") -> dict[str, int | None]:
    """``concurrency`` (the request's ``field``), how many of a task's Workers or Reviewers run at once (null drops
    the task's own)."""
    roles = [role.value for role in TASK_LIMIT_ROLES]
    if not isinstance(raw, dict):
        raise RequestError(HTTP_400, f'{field} must be an object: {{"worker" | "reviewer": limit}}')
    limits: dict[str, int | None] = {}
    for role, value in raw.items():
        if role == AgentType.PLANNER.value:
            raise RequestError(HTTP_400, f"{field}: one Planner runs a task, so a task has no Planner limit, and "
                                         "nothing limits how many tasks run at once")
        if role not in roles:
            raise RequestError(HTTP_400, f"{field}: unknown role {role!r}; a task limits its {' and '.join(roles)}")
        try:
            limits[role] = None if value is None else concurrency(value)
        except SettingsError as exc:
            raise RequestError(HTTP_400, f"{field}.{role}: {exc}") from None
    return limits


async def set_task_agents(task_id: str, data: dict[str, Any]) -> dict[str, Any]:
    """Change the task's own choices per role, together: ``profiles`` (an Agent Profile, by id or name, for its
    Planner, Workers or Reviewers) and ``concurrency`` (how many of its Workers or Reviewers run at once) and ``system_prompt_modes`` (append or replace); null drops
    the task's choice of that role, and its layers apply. 400 for a bad field, role or
    value, 404 for an unknown task, 409 for a completed one (a failed, stopped or cancelled task changes too, and its
    restart runs with them). Answers the task's agents as they are then (and when a new profile applies). The caller
    lets waiting runs check again; a lowered limit stops nothing that runs."""
    unknown = sorted(set(data) - set(TASK_AGENT_FIELDS))
    if unknown:
        raise RequestError(HTTP_400, f"unknown field {unknown[0]!r}; the fields are {', '.join(TASK_AGENT_FIELDS)}")
    if not any(data.get(field) for field in TASK_AGENT_FIELDS):
        raise RequestError(HTTP_400, f"give at least one role's {', '.join(TASK_AGENT_FIELDS)}")
    limits = _concurrency_limits(data["concurrency"]) if data.get("concurrency") else {}
    modes = _prompt_modes(data["system_prompt_modes"]) if "system_prompt_modes" in data else {}
    task = await db_tasks.get_task(task_id)
    if task is None:
        raise RequestError(HTTP_404, "task not found")
    if task["status"] not in AGENTS_EDITABLE_STATUSES:
        raise RequestError(HTTP_409, f"the task is {task['status']}: only a task that can still run, or run again "
                                     "after a restart, can change its agents")
    profiles = await profile_choices(data.get("profiles"), clearing=True)
    try:
        await db_layers.set_task_layer(task_id, profiles=profiles, limits=limits, modes=modes)
    except asyncpg.ForeignKeyViolationError:
        raise RequestError(HTTP_404, "the task, or an Agent Profile it chose, was removed meanwhile") from None
    return {"task_id": task_id, "agents": await agents_view(task), **({"note": PROFILES_NOTE} if profiles else {})}


async def check_cap_to_continue(task_id: str, data: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Validate continuation and return the task and proposed cap without writing or restarting.

    Only a task that failed at its cap qualifies, and the new ``task_budget_usd``
    must be above what the task has spent, or it would stop again at once.
    The cap is the project's, so it applies to its other tasks too, unless the
    task has its own (a scheduled run's ``run_budget_usd``): then only that one
    is raised.
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
    return task, {"task_id": task_id, "project_id": task["project_id"], "task_budget_usd": cap, "spent_usd": spent}


async def raise_cap_to_continue(task_id: str, data: dict[str, Any], *,
                                expected_scope: tuple[str, bool] | None = None) -> dict[str, Any]:
    """Validate and raise the task's own cap, or its project's per-task cap; the caller restarts it."""
    task, result = await check_cap_to_continue(task_id, data)
    if expected_scope is not None and expected_scope != (task["project_id"], task.get("budget_usd") is not None):
        raise RequestError(HTTP_409, "the budget scope changed; request continuation again")
    cap = result["task_budget_usd"]
    if task.get("budget_usd") is not None:
        if not await db_tasks.set_task_budget(task_id, cap):
            raise RequestError(HTTP_404, "task not found")
    elif not await db_projects.update_project(task["project_id"], execution={"task_budget_usd": cap}):
        raise RequestError(HTTP_404, f"project {task['project_id']!r} not found")
    return result


# Where a setting in a task's configuration came from (``agent_settings.Layer``), named as the dashboard names it.
SOURCE_WORDS = {
    Layer.BUILTIN: "内置默认",
    Layer.SYSTEM: "共用默认（Settings → Roles → Shared defaults）",
    Layer.ROLE: "角色默认",
    Layer.PROJECT: "本项目",
    Layer.ROLE_PROJECT: "本项目里的这个角色",
    Layer.PLANNER_TASK: "规划者为本任务剩下的步骤所选",
    Layer.PLANNER_STEP: "规划者为这一步所选",
    Layer.TASK: "本任务",
}
START_QUESTION = "现在开始吗？回复「开始」就会启动；也可以稍后在 Tasks 页面点 Start。"


def _workspace_lines(task_id: str, check: Mapping[str, Any]) -> list[str]:
    """How the task runs in its directory, and each warning, in the user's words (the RFC's W5)."""
    git = check.get("git") or {}
    if check.get("mode") == TaskWorkspaces.WORKTREE.value:
        start = str(git.get("base_commit") or "")[:12]
        lines = [f"- 运行方式：在这个目录所在仓库的 git worktree 里，开始时建分支 {task_branch(task_id)}"
                 f"（从 {git.get('branch') or 'HEAD'} 的 {start} 开始）；做完分支留给你，Looper 不合并"]
    else:
        lines = ["- 运行方式：直接在这个目录里（原地）"]
    for warning in check.get("warnings") or ():
        kind = warning.get("kind")
        if kind == WarningKind.NOT_GIT.value:
            lines.append("- 提醒：目录不在 git 仓库里（或仓库还没有提交），任务会直接在目录里运行")
        elif kind == WarningKind.UNCOMMITTED.value:
            files = "、".join(str(f) for f in (warning.get("files") or [])[:5])
            lines.append(f"- 提醒：目录里有 {len(warning.get('files') or [])} 个未提交的改动不会进入任务的 worktree"
                         f"（{files}），任务需要的话先提交")
        elif kind == WarningKind.SHARED.value:
            shared = warning.get("tasks") or []
            own = [t for t in shared if not t.get("foreign")]
            names = "、".join(f"{t['name']}（{t['status']}）" for t in own[:5])
            if foreign := len(shared) - len(own):
                names += ("、" if names else "") + f"另有 {foreign} 个其他管家的任务"
            effect = ("原地运行的任务会互相覆盖文件" if overwrites(check.get("mode") or "in_place", shared)
                      else "worktree 里的任务各在自己的分支上，不会互相干扰，合并时可能冲突")
            lines.append(f"- 提醒：这个仓库上还有进行中的任务：{names}；{effect}")
        elif kind == WarningKind.LARGE_CHECKOUT.value:
            lines.append(f"- 提醒：仓库检出有 {size_text(int(warning.get('bytes') or 0))}，任务的 worktree 是它的一份完整拷贝")
    return lines


def _role_line(role: AgentType, settings: AgentSettings) -> str:
    """One task role's line in a task's configuration: its Agent Profile with the profile's effort and where the
    profile came from, and, but for the Planner (which never has them), whether it has agent teams."""
    effort = f"effort {settings.effort}" if settings.effort else "effort 默认"
    line = (f"- {role.value.capitalize()}：{settings.profile.name}"
            f"（{effort}，来源：{SOURCE_WORDS[settings.profile_source]}）")
    if role is not AgentType.PLANNER:
        line += f"，agent teams {'开' if settings.profile.agent_teams else '关'}"
    return line


async def configuration_block(
    task_id: str, name: str | None, project: Mapping[str, Any], work_dir: str, created: bool, *,
    workspace: Mapping[str, Any] | None = None, template: tuple[str, Sequence[tuple[str, str]]] | None = None,
) -> str:
    """The new ready task's configuration, from the settings in effect for it (resolved as the project's agent
    settings view resolves them, with the task's own choices), with how it runs in its directory and what to know
    about it (``workspace``: ``task_workspaces.check``), ending with the question whether to start it."""
    project_id = project["id"]
    lines = [
        "任务已创建，还没有开始（Ready — not started）：",
        f"- 任务：{name}（{task_id}）" if name else f"- 任务：{task_id}",
        f"- 项目：{project['name']}（{'新建' if created else '已有'}）",
    ]
    if template is not None:
        template_name, template_inputs = template
        inputs = "；".join(f"{label}：{value}" for label, value in template_inputs)
        lines.append(f"- 模板：{template_name}（{inputs}）" if inputs else f"- 模板：{template_name}")
    lines.append(f"- 目录：{work_dir}")
    if workspace is not None:
        lines += _workspace_lines(task_id, workspace)
    limits: list[int] = []
    try:
        layers = index_layers(await db_layers.list_layers(project_id, task_id=task_id), project_id)
    except Exception as exc:
        logger.warning("could not read the agent settings of project %s", project_id, exc_info=True)
        lines.append(f"- Agent 设置：暂时读不到（{type(exc).__name__}），可以在项目的 Settings 页查看")
    else:
        lines += [_role_line(role, resolve(role, layers, project_id)) for role in TASK_ROLES]
        limits = [resolve_task_concurrency(role, layers).value for role in TASK_LIMIT_ROLES]
    execution = ProjectExecution.from_row(project)
    lines.append("- 评审：每一步都要 Reviewer 评审通过" if execution.review_policy is ReviewPolicy.ALL
                 else "- 评审：由 Planner 决定哪些步骤要 Reviewer 评审")
    if execution.task_budget_usd is None:
        lines.append("- 每个任务的花费上限：不限")
    else:
        at_cap = ("再让 Planner 总结一轮后结束" if execution.on_task_budget is TaskBudgetAction.WRAP_UP
                  else "立即失败")
        lines.append(f"- 每个任务的花费上限：${format_usd(execution.task_budget_usd)}，达到时{at_cap}")
    lines.append({
        LessonsMode.OFF: "- 经验沉淀：关",
        LessonsMode.DRAFT: "- 经验沉淀：任务完成后生成草稿，由你确认后写入项目知识",
        LessonsMode.WRITE: "- 经验沉淀：任务完成后直接写入项目知识",
    }[execution.distill_lessons])
    if limits:
        lines.append(f"- 并发：这个任务同时最多运行 {limits[0]} 个 Worker、{limits[1]} 个 Reviewer")
    return "\n".join([*lines, "", START_QUESTION])
