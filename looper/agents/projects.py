"""Projects over HTTP: request validation, the project view and task creation.

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
``/api/projects/<id>/agent-settings``). A task that has not ended may change
its own choices per role (``set_task_agents``): the Agent Profile of its roles,
and how many of its Workers or Reviewers run at once, within its project's
limit.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from typing import Any

import asyncpg

from ..db import agent_setting_layers as db_layers, connection as db_connection, llm_records as db_llm_records
from ..db import projects as db_projects, tasks as db_tasks
from .agent_settings import CONCURRENCY_MAX, TASK_LIMIT_ROLES, AgentType, SettingsError, concurrency, index_layers
from .agent_settings import task_agents_view
from .bus import MessageBus
from .message import TASK_ROUTER_ADDRESS, AgentAddress, MessageEnvelope, MessageType, planner_address
from .project_execution import FIELDS as EXECUTION_FIELDS, ProjectExecution, execution_fields, format_usd
from .system_prompt import TASK_REQUIREMENT_LIMIT
from .tiers.task import TaskRouter, resolve_work_dir

logger = logging.getLogger(__name__)

HTTP_400 = "400 Bad Request"
HTTP_404 = "404 Not Found"
HTTP_409 = "409 Conflict"

NAME_MAX_CHARS = 80
BACKGROUND_MAX_CHARS = 10000
NO_WORK_DIR = "a work directory is required: set one on the task or on its project"
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
    if count := project.get("schedule_count"):
        raise RequestError(HTTP_409, f"project {project['name']!r} still has {count} "
                                     f"schedule{'s' if count != 1 else ''}; delete them first")
    try:
        await db_projects.delete_project(project_id)
    except asyncpg.ForeignKeyViolationError:  # a task or a schedule was added meanwhile
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
    check_requirement(requirement)

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
    profiles = await _profile_overrides(data.get("profile_overrides"))
    task = {
        "task_id": f"task-{uuid.uuid4().hex[:12]}",
        "requirement": requirement,
        "goal_id": data.get("goal_id", ""),
        "work_dir": _task_work_dir(data.get("work_dir"), default_dir),
        "config": config or {},
        "status": "init",
    }
    if project is not None and not profiles:
        created = await db_tasks.create_task(**task, project_id=project["id"])
    else:
        try:
            async with db_connection.transaction() as conn:
                if project is None:
                    project = await db_projects.create_project(**new_fields, pool=conn)
                created = await db_tasks.create_task(**task, project_id=project["id"], pool=conn)
                if profiles:
                    await db_layers.set_profile_overrides(conn, task["task_id"], profiles)
        except asyncpg.UniqueViolationError:
            raise _name_taken(new_fields["name"]) from None
        except asyncpg.ForeignKeyViolationError:  # a profile removed meanwhile
            raise RequestError(HTTP_400, "an Agent Profile the task chose was removed meanwhile; choose again") \
                from None
    return {
        "task_id": task["task_id"],
        "status": "init",
        "created_at": created["created_at"],
        "project_id": project["id"],
    }


async def _profile_overrides(raw: Any) -> dict[str, str]:
    """A new task's choice of Agent Profile per role (``{"planner"|"worker"|"reviewer": profile_id}``), each
    registered and able to run its role; an empty or null entry chooses nothing."""
    from .profile_registry import RegistryError, check_selection

    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise RequestError(HTTP_400, 'profile_overrides must be an object: {"planner" | "worker" | "reviewer": '
                                     'profile_id}')
    profiles: dict[str, str] = {}
    for role, profile_id in raw.items():
        if role not in TASK_PROFILE_ROLES:
            raise RequestError(HTTP_400, f"a task chooses the Agent Profile of its {', '.join(TASK_PROFILE_ROLES)} "
                                         f"only, not {role!r}")
        if profile_id in (None, ""):
            continue
        try:
            await check_selection(profile_id, [role])
        except RegistryError as exc:
            raise RequestError(HTTP_400, f"{role}: {exc}") from None
        profiles[role] = profile_id
    return profiles


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
    Manager: 404 for an unknown task, 400 for one that is not ready. Like a new task, it waits while its project
    runs as many tasks as its limit."""
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


# How a change to a task came: on the task page, or in a message sent to its Planner on the Chat page.
VIA_TASK_PAGE = "task_page"
VIA_CHAT = "chat"
# The statuses a task's requirement can be edited in: not started, waiting for room, or running (whatever its
# Planner is doing).
EDITABLE_STATUSES = (db_tasks.READY, *db_tasks.ACTIVE_STATUSES)


async def store_chat_supplement(target: AgentAddress, text: str) -> int | None:
    """Store a chat message sent to a task's Planner as a supplement of the task, before the Planner gets it, so a
    restart meanwhile does not lose it; its id, or None for a message to another agent (or when it could not be
    stored: the Planner stores it then)."""
    if target.agent_type != "planner":
        return None
    from ..db import task_changes
    from .tiers.journal import SUPPLEMENTS_MAX_CHARS, kept_text

    try:
        return await task_changes.add_supplement(target.agent_id.removeprefix("planner-"),
                                                 kept_text(text, SUPPLEMENTS_MAX_CHARS), via=VIA_CHAT)
    except Exception:
        logger.exception("could not store a chat message to %s as a supplement", target.agent_id)
        return None


async def edit_requirement(
    task_id: str, data: dict[str, Any], *, task_router: TaskRouter, bus: MessageBus, source: AgentAddress, via: str,
) -> dict[str, Any]:
    """Make ``data["requirement"]`` the task's requirement, keeping the old text as a version: 404 for an unknown
    task, 409 for one that has ended, 400 for an empty or unchanged text. A running task's Planner gets the edit in
    a turn (``TASK_CHANGE``), and so does one waiting for room, at its PLAN turn; a task not started plans the
    new text."""
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


# What a task's own choices per role are (``set_task_agents``), and when a new profile applies.
TASK_AGENT_FIELDS = ("profiles", "concurrency")
PROFILES_NOTE = ("a Worker or Reviewer uses its new profile from its next step on; the Planner moves to its new "
                 "profile at its next turn, after a handoff")


async def agents_view(task: dict[str, Any]) -> dict[str, Any]:
    """Task ``task``'s agents, read now: each task role's Agent Profile and concurrency with their sources
    (``agent_settings.task_agents_view``), the most a limit may be, and whether the task can change its own
    choices (it has not ended)."""
    project_id = task.get("project_id")
    layers = index_layers(await db_layers.list_layers(project_id, task_id=task["id"]), project_id)
    return {"roles": task_agents_view(layers, project_id), "concurrency_max": CONCURRENCY_MAX,
            "editable": task.get("status") in EDITABLE_STATUSES}


async def profile_choices(raw: Any, *, clearing: bool) -> dict[str, str | None]:
    """``profiles``, a task's choice of Agent Profile per role (by id or name), as role -> profile id (None: drop the
    task's choice, only when ``clearing``); each one registered and able to run its role. 400 otherwise."""
    from ..db import profile_registry as db_profiles
    from .profile_registry import RegistryError, check_selection

    if not raw:
        return {}
    if not isinstance(raw, dict):
        raise RequestError(HTTP_400, 'profiles must be an object: {"planner" | "worker" | "reviewer": profile}')
    registered: list[dict[str, Any]] | None = None
    chosen: dict[str, str | None] = {}
    for role, wanted in raw.items():
        if role not in TASK_PROFILE_ROLES:
            raise RequestError(HTTP_400, f"profiles: a task chooses the Agent Profile of its "
                                         f"{', '.join(TASK_PROFILE_ROLES)} only, not {role!r}")
        if wanted is None:
            if not clearing:
                raise RequestError(HTTP_400, f"profiles.{role}: give an Agent Profile, or leave the role out")
            chosen[role] = None
            continue
        if not isinstance(wanted, str):
            raise RequestError(HTTP_400, f"profiles.{role}: give an Agent Profile's id or name, or null")
        if registered is None:
            registered = await db_profiles.list_profiles()
        match = next((p for p in registered if p["id"] == wanted), None) or next(
            (p for p in registered if str(p["name"]).casefold() == wanted.strip().casefold()), None)
        if match is None:
            names = ", ".join(str(p["name"]) for p in registered[:20])
            raise RequestError(HTTP_400, f"profiles.{role}: no Agent Profile {wanted!r}; registered: {names}")
        try:
            await check_selection(match["id"], [role])
        except RegistryError as exc:
            raise RequestError(HTTP_400, f"profiles.{role}: {exc}") from None
        chosen[role] = match["id"]
    return chosen


def _concurrency_limits(raw: Any) -> dict[str, int | None]:
    """``concurrency``, how many of a task's Workers or Reviewers run at once (null drops the task's own)."""
    roles = [role.value for role in TASK_LIMIT_ROLES]
    if not isinstance(raw, dict):
        raise RequestError(HTTP_400, 'concurrency must be an object: {"worker" | "reviewer": limit}')
    limits: dict[str, int | None] = {}
    for role, value in raw.items():
        if role == AgentType.PLANNER.value:
            raise RequestError(HTTP_400, "one Planner runs a task, so a task has no Planner limit; how many of a "
                                         "project's tasks run at once is the project's planner limit")
        if role not in roles:
            raise RequestError(HTTP_400, f"unknown role {role!r}; a task limits its {' and '.join(roles)}")
        try:
            limits[role] = None if value is None else concurrency(value)
        except SettingsError as exc:
            raise RequestError(HTTP_400, f"concurrency.{role}: {exc}") from None
    return limits


async def set_task_agents(task_id: str, data: dict[str, Any]) -> dict[str, Any]:
    """Change the task's own choices per role, together: ``profiles`` (an Agent Profile, by id or name, for its
    Planner, Workers or Reviewers) and ``concurrency`` (how many of its Workers or Reviewers run at once, within its
    project's limit); null drops the task's choice of that role, and its layers apply. 400 for a bad field, role or
    value, 404 for an unknown task, 409 for one that has ended. Answers the task's agents as they are then (and when
    a new profile applies). The caller lets waiting runs check again; a lowered limit stops nothing that runs."""
    unknown = sorted(set(data) - set(TASK_AGENT_FIELDS))
    if unknown:
        raise RequestError(HTTP_400, f"unknown field {unknown[0]!r}; the fields are {', '.join(TASK_AGENT_FIELDS)}")
    if not data.get("profiles") and not data.get("concurrency"):
        raise RequestError(HTTP_400, "give at least one role's profiles or concurrency")
    limits = _concurrency_limits(data["concurrency"]) if data.get("concurrency") else {}
    task = await db_tasks.get_task(task_id)
    if task is None:
        raise RequestError(HTTP_404, "task not found")
    if task["status"] not in EDITABLE_STATUSES:
        raise RequestError(HTTP_409, f"the task is {task['status']}: only a task that has not ended can change its "
                                     "agents")
    profiles = await profile_choices(data.get("profiles"), clearing=True)
    try:
        await db_layers.set_task_layer(task_id, profiles=profiles, limits=limits)
    except asyncpg.ForeignKeyViolationError:
        raise RequestError(HTTP_404, "the task, or an Agent Profile it chose, was removed meanwhile") from None
    return {"task_id": task_id, "agents": await agents_view(task), **({"note": PROFILES_NOTE} if profiles else {})}


async def raise_cap_to_continue(task_id: str, data: dict[str, Any]) -> dict[str, Any]:
    """Raise the task's spend cap so a task that failed at it can continue; the caller restarts it.

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
    if task.get("budget_usd") is not None:
        await db_tasks.set_task_budget(task_id, cap)
    elif not await db_projects.update_project(task["project_id"], execution={"task_budget_usd": cap}):
        raise RequestError(HTTP_404, f"project {task['project_id']!r} not found")
    return {"task_id": task_id, "project_id": task["project_id"], "task_budget_usd": cap, "spent_usd": spent}
