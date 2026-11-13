"""The service behind ``/api/managers``: scenario Managers and their task templates (scenario Managers RFC, D1, D5,
D7, D9, D13). HTTP-free: it validates, writes through ``looper.db.managers`` and answers the view the API shows;
a refusal is a ``projects.RequestError``.

The limit on enabled Managers and the write that could break it are one transaction holding ``lock_managers``, so
two requests enabling the 8th and the 9th queue on that lock and the second counts the first's row.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Any

import asyncpg

from ..db import connection as db_connection, llm_records as db_llm_records, managers as db_managers, projects as db_projects
from ..db import role_templates as db_role_templates
from ..db.tasks import TaskFilter
from . import projects, task_templates
from .profile_registry import RegistryError, check_selection
from .projects import HTTP_400, HTTP_404, HTTP_409, RequestError

logger = logging.getLogger(__name__)

MAX_ENABLED_MANAGERS = 8  # includes the default Manager
DEFAULT_MANAGER_ID = "manager"
MANAGER_KEY_PATTERN = r"^[a-z][a-z0-9-]{1,30}$"
MANAGER_FIELDS = ("key", "name", "description", "system_prompt", "profile_id", "tools", "task_scope", "task_policy",
                  "enabled", "idle_timeout_seconds", "scope_project_ids")
TEMPLATE_FIELDS = ("name", "description", "project_id", "work_dir", "workspace_mode", "requirement",
                   "planner_instructions", "profiles", "role_templates", "inputs")
SYSTEM_PROMPT_MAX = 20_000
NAME_MAX = 60
DESCRIPTION_MAX = 300

_DEFAULT_NO_TEMPLATES = ("the default Manager takes no task templates: its task tool is the free-form one; "
                         "put them on a scenario Manager")
_DEFAULT_ONLY = ("the default Manager's prompt and profile are in Settings → Roles; it uses every tool, sees every "
                 "task and is always on")


class ManagerTools(str, Enum):
    ALL = "all"
    TASKS = "tasks"


class TaskScope(str, Enum):
    """Which tasks the Manager sees and may act on; independent of its tools."""

    ALL = "all"
    OWN = "own"
    PROJECTS = "projects"


class TaskPolicy(str, Enum):
    FREE = "free"
    TEMPLATES_ONLY = "templates_only"


@dataclass(frozen=True)
class ManagerDefinition:
    id: str
    name: str
    description: str
    system_prompt: str | None
    profile_id: str | None
    tools: ManagerTools
    task_scope: TaskScope
    task_policy: TaskPolicy
    enabled: bool
    builtin: bool
    revision: int
    idle_timeout_seconds: int = 1800
    scope_project_ids: tuple[str, ...] = ()

    def tool_tasks(self) -> TaskFilter:
        if self.task_scope is TaskScope.PROJECTS:
            return TaskFilter(project_ids=self.scope_project_ids)
        if self.task_scope is TaskScope.OWN:
            return TaskFilter(manager_id=self.id)
        return TaskFilter()

    def listed_tasks(self) -> TaskFilter:
        if self.task_scope is TaskScope.PROJECTS:
            return self.tool_tasks()
        return TaskFilter(manager_id=self.id)

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "ManagerDefinition":
        return cls(id=row["id"], name=row["name"], description=row.get("description") or "",
                   system_prompt=row.get("system_prompt"), profile_id=row.get("profile_id"),
                   tools=ManagerTools(row["tools"]), task_scope=TaskScope(row["task_scope"]),
                   task_policy=TaskPolicy(row["task_policy"]), enabled=bool(row["enabled"]),
                   builtin=row["id"] == DEFAULT_MANAGER_ID, revision=int(row.get("revision") or 0),
                   idle_timeout_seconds=row.get("idle_timeout_seconds", 0 if row["id"] == DEFAULT_MANAGER_ID else 1800),
                   scope_project_ids=tuple(row.get("scope_project_ids") or ()))


_listener: Callable[[str], Awaitable[None]] | None = None


def set_change_listener(callback: Callable[[str], Awaitable[None]] | None) -> None:
    """Call ``callback(manager_id)`` after every committed write to a Manager or its templates (the fleet applies
    it, card 6); None: nothing happens. A listener that fails is logged and does not undo the write. That is not
    the end of it: the fleet records the failure and retries until the change is in force (``state.sync`` is
    ``"pending"`` in the view meanwhile), and until then the Manager's tool calls are checked against the committed
    ``revision``, so a narrowed scope is never left open by a failed sync."""
    global _listener
    _listener = callback


async def _on_change(manager_id: str) -> None:
    if _listener is None:
        return
    try:
        await _listener(manager_id)
    except Exception:
        logger.exception("applying the change of %s failed; the write stands", manager_id)


def _refuse(message: str, status: str = HTTP_400) -> RequestError:
    return RequestError(status, message)


def _enum_text(field: str, values: Mapping[str, str]) -> str:
    return f"{field} must be " + " or ".join(f"{v!r} ({what})" for v, what in values.items())


_ENUM_HELP = {
    "tools": ("tools", ManagerTools, {"all": "every tool", "tasks": "only the task tools"}),
    "task_scope": ("task_scope", TaskScope, {"all": "every task", "own": "only the tasks this Manager created",
                                          "projects": "only the tasks of the projects in scope_project_ids"}),
    "task_policy": ("task_policy", TaskPolicy, {"free": "any task", "templates_only": "only tasks made from a "
                                                                                     "template"}),
}


def _fields(data: Mapping[str, Any], allowed: tuple[str, ...]) -> None:
    unknown = sorted(set(data) - set(allowed))
    if unknown:
        raise _refuse(f"unknown field {unknown[0]!r}; the fields are {', '.join(allowed)}")


def _line(field: str, value: Any, limit: int, *, required: bool) -> str:
    if not isinstance(value, str):
        raise _refuse(f"{field} must be a string")
    text = " ".join(value.split())
    if required and not text:
        raise _refuse(f"{field} is required")
    if len(text) > limit:
        raise _refuse(f"{field} must be at most {limit} characters")
    return text


async def _unique_name(name: str, *, own_id: str | None, conn: asyncpg.Connection | None = None) -> None:
    folded = name.casefold()
    for row in await db_managers.list_managers(pool=conn):
        if row["id"] != own_id and " ".join(str(row["name"]).split()).casefold() == folded:
            raise _refuse(f"name {name!r} is used by another Manager: pick another", HTTP_409)


async def checked_values(data: Mapping[str, Any], *, own_id: str | None,
                         conn: asyncpg.Connection | None = None,
                         check_references: bool = True, allow_empty_scope: bool = False) -> dict[str, Any]:
    """Checked columns (not ``key``). Import previews check references in their projected graph; writes use
    ``conn`` so profiles created in the same transaction are visible. Only imports may restore an empty scope."""
    values: dict[str, Any] = {}
    if "name" in data:
        name = data["name"]
        if not isinstance(name, str) or "\n" in name or "\r" in name or not 1 <= len(name.strip()) <= NAME_MAX:
            raise _refuse(f"name must be one line of 1 to {NAME_MAX} characters")
        values["name"] = " ".join(name.split())
        if check_references:
            await _unique_name(values["name"], own_id=own_id, conn=conn)
    if "description" in data:
        values["description"] = _line("description", data["description"] or "", DESCRIPTION_MAX, required=False)
    if "system_prompt" in data:
        prompt = data["system_prompt"]
        if not isinstance(prompt, str) or not prompt.strip():
            raise _refuse("system_prompt is required: say who this Manager is and how it works")
        if len(prompt) > SYSTEM_PROMPT_MAX:
            raise _refuse(f"system_prompt has {len(prompt)} characters; at most {SYSTEM_PROMPT_MAX}")
        values["system_prompt"] = prompt
    if "profile_id" in data:
        wanted = data["profile_id"]
        if wanted is None or wanted == "":
            values["profile_id"] = None
        else:
            if not isinstance(wanted, str):
                raise _refuse("profile_id must be an Agent Profile's id, or null for the default")
            if check_references:
                try:
                    await check_selection(wanted, ["manager"], pool=conn)
                except RegistryError as exc:
                    raise _refuse(f"profile_id: {exc}") from None
            values["profile_id"] = wanted
    for field, (_, enum, help_text) in _ENUM_HELP.items():
        if field in data:
            try:
                values[field] = enum(data[field]).value
            except ValueError:
                raise _refuse(_enum_text(field, help_text)) from None
    if "scope_project_ids" in data:
        ids = data["scope_project_ids"]
        if (not isinstance(ids, list) or not (0 if allow_empty_scope else 1) <= len(ids) <= 20
                or any(not isinstance(item, str) or not item.strip() for item in ids)):
            raise _refuse("scope_project_ids must list 1 to 20 project ids")
        values["scope_project_ids"] = sorted({item.strip() for item in ids})
    if "enabled" in data:
        if not isinstance(data["enabled"], bool):
            raise _refuse("enabled must be true or false")
        values["enabled"] = data["enabled"]
    if "idle_timeout_seconds" in data:
        timeout = data["idle_timeout_seconds"]
        if type(timeout) is not int or not (timeout == 0 or 60 <= timeout <= 86400):
            raise _refuse("idle_timeout_seconds must be an integer: 0 (off), or 60 to 86400 seconds")
        if own_id == DEFAULT_MANAGER_ID and timeout != 0:
            raise _refuse("the default Manager's idle_timeout_seconds must be 0")
        values["idle_timeout_seconds"] = timeout
    return values


def final_scope(current: Mapping[str, Any] | None, values: dict[str, Any], *,
                allow_empty_scope: bool = False) -> tuple[TaskScope, tuple[str, ...]]:
    """Validate the resulting scope, including partial updates and scopes emptied by deletion."""
    stored = current or {}
    scope = TaskScope(values.get("task_scope", stored.get("task_scope", "own")))
    ids = tuple(values.get("scope_project_ids", stored.get("scope_project_ids") or ()))
    if not {"task_scope", "scope_project_ids", "tools"}.intersection(values):
        return scope, ids
    if "scope_project_ids" in values and scope is not TaskScope.PROJECTS:
        raise _refuse("scope_project_ids goes with task_scope projects")
    if scope is TaskScope.PROJECTS:
        if "task_scope" in values or "scope_project_ids" in values:
            if "scope_project_ids" not in values and stored.get("task_scope") != "projects":
                ids = ()
            if not ids and not allow_empty_scope:
                raise _refuse("task_scope projects needs scope_project_ids: the projects this Manager works in")
        if values.get("tools", stored.get("tools", "tasks")) != "tasks":
            raise _refuse("task_scope projects works only with tools tasks: a Manager with every tool would reach "
                          "projects and settings outside its projects")
    elif stored.get("task_scope") == "projects":
        values["scope_project_ids"] = []
        ids = ()
    return scope, ids


async def _lock_scope_projects(ids: tuple[str, ...], *, conn: asyncpg.Connection) -> None:
    found = await db_managers.lock_scope_projects(ids, conn=conn)
    missing = sorted(set(ids) - found)
    if missing:
        raise _refuse(f"no project {missing[0]!r}")


async def _check_scope_templates(manager_id: str, ids: tuple[str, ...], *, conn: asyncpg.Connection) -> None:
    for template in await db_managers.list_templates(manager_id, pool=conn):
        if template["project_id"] not in ids:
            project = await db_projects.get_project(template["project_id"], pool=conn) if template["project_id"] else None
            target = project["name"] if project else "no project"
            raise _refuse(f"template {template['key']!r} targets {target}, outside this Manager's projects: "
                          "change the template first", HTTP_409)


# --- Views ---


def template_view(row: Mapping[str, Any]) -> dict[str, Any]:
    """A template as the API takes it: ``profiles`` as ``{role: profile id}``."""
    return {
        "key": row["key"], "name": row["name"], "description": row["description"], "project_id": row["project_id"],
        "work_dir": row["work_dir"], "workspace_mode": row["workspace_mode"], "requirement": row["requirement"],
        "planner_instructions": row["planner_instructions"],
        "profiles": {role: row[f"{role}_profile_id"] for role in projects.TASK_PROFILE_ROLES
                     if row.get(f"{role}_profile_id")},
        "role_templates": list(row["role_templates"]), "inputs": list(row["inputs"]), "updated_at": row["updated_at"],
    }


def manager_view(row: Mapping[str, Any], templates: list[Mapping[str, Any]]) -> dict[str, Any]:
    return {
        "id": row["id"], "name": row["name"], "description": row["description"],
        "system_prompt": row["system_prompt"], "profile_id": row["profile_id"],
        "profile_name": row.get("profile_name"), "tools": row["tools"], "task_scope": row["task_scope"],
        "scope_project_ids": list(row.get("scope_project_ids") or []),
        "task_policy": row["task_policy"], "enabled": row["enabled"], "builtin": row["id"] == DEFAULT_MANAGER_ID,
        "idle_timeout_seconds": row.get("idle_timeout_seconds", 0 if row["id"] == DEFAULT_MANAGER_ID else 1800),
        "revision": row["revision"], "created_at": row["created_at"], "updated_at": row["updated_at"],
        "templates": [template_view(t) for t in templates],
    }


async def _one_view(manager_id: str, extra: Callable[[str], Mapping[str, Any]] | None = None) -> dict[str, Any]:
    row = await db_managers.get_manager(manager_id)
    if row is None:
        raise _refuse(f"no Manager {manager_id!r}", HTTP_404)
    view = manager_view(row, await db_managers.list_templates(manager_id))
    if extra is not None:
        view["state"] = extra(manager_id)
    return view


async def managers_view(extra: Callable[[str], Mapping[str, Any]] | None = None) -> dict[str, Any]:
    """Every Manager not deleted with its templates; ``extra(manager_id)`` is each Manager's view's ``state``
    (the fleet's); ``spend`` is counted from the runs, one query for all."""
    views = []
    from .overview import local_midnight  # overview imports the Manager tier, which imports this module's users

    rows = await db_managers.list_managers()
    spend = await db_llm_records.manager_spend([row["id"] for row in rows], since=local_midnight())
    for row in rows:
        view = manager_view(row, await db_managers.list_templates(row["id"]))
        view["spend"] = spend[row["id"]]
        if extra is not None:
            view["state"] = extra(row["id"])
        views.append(view)
    return {"managers": views, "max_enabled": MAX_ENABLED_MANAGERS}


# --- Managers ---


def _too_many() -> RequestError:
    return _refuse(f"at most {MAX_ENABLED_MANAGERS} Managers can be enabled at once: disable one first", HTTP_409)


async def create_manager(data: Mapping[str, Any]) -> dict[str, Any]:
    """Create a scenario Manager (201); 400 for a field Looper refuses, 409 for a key or name in use, a key a
    deleted Manager used, or a ninth enabled Manager. Answers ``{"manager": view}``."""
    _fields(data, MANAGER_FIELDS)
    key = data.get("key")
    if not isinstance(key, str) or not re.fullmatch(MANAGER_KEY_PATTERN, key):
        raise _refuse(f"key is required: 2 to 31 characters, lowercase letters, digits and '-', starting with a "
                      f"letter ({MANAGER_KEY_PATTERN})")
    manager_id = f"manager-{key}"
    if "name" not in data:
        raise _refuse("name is required")
    values = await checked_values(data, own_id=None)
    if "system_prompt" not in data:
        raise _refuse("system_prompt is required: say who this Manager is and how it works")
    scope, ids = final_scope(None, values)
    values.setdefault("enabled", True)
    if await db_managers.key_used(manager_id):
        used_by_deleted = await db_managers.get_manager(manager_id) is None
        raise _refuse(f"key {key} was used by a deleted Manager: pick another" if used_by_deleted
                      else f"key {key} is used by another Manager", HTTP_409)
    try:
        async with db_connection.transaction() as conn:
            if scope is TaskScope.PROJECTS:
                await _lock_scope_projects(ids, conn=conn)
            await db_managers.lock_managers(conn)
            if values["enabled"] and await db_managers.count_enabled(conn=conn) >= MAX_ENABLED_MANAGERS:
                raise _too_many()
            await db_managers.insert_manager({"id": manager_id, **values}, conn=conn)
            if scope is TaskScope.PROJECTS:
                await _check_scope_templates(manager_id, ids, conn=conn)
    except asyncpg.UniqueViolationError:
        raise _refuse("a Manager with this key or name exists: pick another", HTTP_409) from None
    except asyncpg.ForeignKeyViolationError:
        raise _refuse("the Agent Profile was removed meanwhile", HTTP_409) from None
    await _on_change(manager_id)
    return {"manager": await _one_view(manager_id)}


async def open_chats(manager_id: str, *, conn: asyncpg.Connection | None = None) -> list[tuple[str, str]]:
    rows = await (conn or db_connection.get_pool()).fetch(
        "SELECT channel_id, chat_id FROM manager_chat_bindings "
        "WHERE manager_id = $1 AND sender_policy = 'chat' ORDER BY channel_id, chat_id", manager_id)
    return [(row["channel_id"], row["chat_id"]) for row in rows]


async def update_manager(manager_id: str, data: Mapping[str, Any], *,
                         expected_open_chats: list[tuple[str, str]] | None = None) -> dict[str, Any]:
    """Change the fields given of a Manager (404 when unknown or deleted). The default Manager takes ``name`` and
    ``description`` and ``idle_timeout_seconds: 0`` only. A write of ``enabled: true`` takes ``lock_managers`` and re-reads the row under
    ``FOR UPDATE``, whatever an earlier read said, before it counts the enabled Managers. Answers
    ``{"manager": view}``."""
    row = await db_managers.get_manager(manager_id)
    if row is None:
        raise _refuse(f"no Manager {manager_id!r}", HTTP_404)
    if "key" in data:
        raise _refuse("a Manager's key cannot change")
    _fields(data, MANAGER_FIELDS)
    if not data:
        raise _refuse(f"nothing to change; the fields are {', '.join(f for f in MANAGER_FIELDS if f != 'key')}")
    if manager_id == DEFAULT_MANAGER_ID and set(data) - {"name", "description", "idle_timeout_seconds"}:
        raise _refuse(_DEFAULT_ONLY)
    checked = await checked_values(data, own_id=manager_id)
    values = dict(checked)
    scope, ids = final_scope(row, values)
    try:
        async with db_connection.transaction() as conn:
            if scope is TaskScope.PROJECTS and "scope_project_ids" in values:
                await _lock_scope_projects(ids, conn=conn)
            if values.get("enabled") is True:
                await db_managers.lock_managers(conn)
            current = await db_managers.lock_for_update(manager_id, conn=conn)
            if current is None or current["deleted_at"] is not None:
                raise _refuse(f"no Manager {manager_id!r}", HTTP_404)
            # Binding writes take a share lock on this Manager before opening a chat.
            # Recheck under FOR UPDATE so consent cannot silently cover more chats.
            if (checked.get("tools") == "all" and expected_open_chats is not None
                    and await open_chats(manager_id, conn=conn) != expected_open_chats):
                raise _refuse("Open chats changed; submit the Manager update again", HTTP_409)
            values = dict(checked)
            scope, ids = final_scope(current, values)
            if values.get("enabled") is True and not current["enabled"]:
                if await db_managers.count_enabled(conn=conn) >= MAX_ENABLED_MANAGERS:
                    raise _too_many()
            if await db_managers.update_manager(manager_id, values, conn=conn,
                                                only_if_changed="scope_project_ids" in values) is None:
                raise _refuse(f"no Manager {manager_id!r}", HTTP_404)
            if scope is TaskScope.PROJECTS:
                await _check_scope_templates(manager_id, ids, conn=conn)
    except asyncpg.UniqueViolationError:
        raise _refuse("a Manager with this name exists: pick another", HTTP_409) from None
    except asyncpg.ForeignKeyViolationError:
        raise _refuse("the Agent Profile was removed meanwhile", HTTP_409) from None
    await _on_change(manager_id)
    return {"manager": await _one_view(manager_id)}


def _has_tasks(row: Mapping[str, Any]) -> RequestError:
    return _refuse(f"Manager {row['name']} has tasks: disable it instead", HTTP_409)


async def delete_manager(manager_id: str, *, fleet: Any = None) -> dict[str, Any]:
    """Delete (soft) a scenario Manager that has no task: its templates go, its id stays taken. 409 for the
    default Manager, an active one, or one with tasks, schedules, bots or chat bindings; 404 when absent."""
    if manager_id == DEFAULT_MANAGER_ID:
        raise _refuse("the default Manager cannot be deleted", HTTP_409)
    row = await db_managers.get_manager(manager_id)
    if row is None:
        raise _refuse(f"no Manager {manager_id!r}", HTTP_404)
    if await db_managers.has_tasks(manager_id):  # early, for the message; delete_manager decides
        raise _has_tasks(row)
    result = await (fleet.delete_manager(manager_id) if fleet is not None
                    else db_managers.delete_manager(manager_id))
    if result == "busy":
        raise _refuse(f"Manager {row['name']} is active: retry after its work finishes, or stop it and wait before deleting",
                      HTTP_409)
    if result == "has_tasks":
        raise _has_tasks(row)
    if result == "has_schedules":
        raise _refuse(f"Manager {row['name']} has schedules: delete those schedules first, or disable the Manager",
                      HTTP_409)
    if result == "has_bots":
        raise _refuse(f"Manager {row['name']} has bots: remove them in Settings → Channels first", HTTP_409)
    if result == "has_bindings":
        raise _refuse(f"Manager {row['name']} has chat bindings: unbind them first", HTTP_409)
    if result == "missing":
        raise _refuse(f"no Manager {manager_id!r}", HTTP_404)
    await _on_change(manager_id)
    return await managers_view()


# --- Templates ---


async def _template_profiles(raw: Any, project_id: str | None, *,
                              conn: asyncpg.Connection | None = None) -> dict[str, str]:
    chosen = await projects.profile_choices(raw, clearing=False, field="profiles", pool=conn)
    profiles = {role: profile for role, profile in chosen.items() if profile}
    if project_id and profiles:
        allowed, restricted = await projects.choosable_profiles(project_id, pool=conn)
        if restricted:
            ids = {p["id"] for p in allowed}
            for role, profile in profiles.items():
                if profile not in ids:
                    raise _refuse(f"profiles.{role}: Agent Profile {profile!r} is not on project {project_id!r}'s "
                                  f"list; it allows: {', '.join(str(p['name']) for p in allowed)}")
    return profiles


def _text(data: Mapping[str, Any], field: str) -> str:
    value = data.get(field, "")
    if value is None:
        return ""
    if not isinstance(value, str):
        raise _refuse(f"{field} must be a string")
    return value


async def checked_template_values(manager_id: str, key: str, data: Mapping[str, Any], *,
                                  conn: asyncpg.Connection | None = None,
                                  check_references: bool = True) -> dict[str, Any]:
    """Validate a template without writing; check every reference on ``conn`` when supplied.

    Previews pass ``check_references=False`` and check their projected identities and role settings instead.
    Input regexes still run through ``task_templates.check_patterns``' bounded subprocess.
    """
    if manager_id == DEFAULT_MANAGER_ID:
        raise _refuse(_DEFAULT_NO_TEMPLATES)
    if not re.fullmatch(task_templates.KEY_PATTERN, key):
        raise _refuse(f"template key must match {task_templates.KEY_PATTERN}")
    _fields(data, TEMPLATE_FIELDS)
    project_id = data.get("project_id") or None
    if project_id is not None and not isinstance(project_id, str):
        raise _refuse("project_id must be a project's id, or left out")
    role_templates = data.get("role_templates") or []
    if not isinstance(role_templates, list) or not all(isinstance(k, str) for k in role_templates):
        raise _refuse("role_templates must be a list of role template keys")
    workspace_mode = data.get("workspace_mode") or None
    requirement, instructions = _text(data, "requirement"), _text(data, "planner_instructions")
    try:
        name, description = task_templates.check_template_fields(
            name=data.get("name"), description=_text(data, "description"), planner_instructions=instructions,
            workspace_mode=workspace_mode, role_templates=role_templates, project_id=project_id)
        inputs = task_templates.parse_inputs(data.get("inputs", []))
        task_templates.check_requirement(requirement, inputs)
        await task_templates.check_patterns(inputs)
    except task_templates.TemplateError as exc:
        raise _refuse(str(exc)) from None
    if check_references and project_id and await db_projects.get_project(project_id, pool=conn) is None:
        raise _refuse(f"no project {project_id!r}")
    work_dir = _text(data, "work_dir")
    if work_dir.strip():
        try:
            work_dir = projects.resolve_work_dir(work_dir)
        except ValueError as exc:
            raise _refuse(f"work_dir: {exc}") from None
    else:
        work_dir = ""
    if check_references:
        profiles = await _template_profiles(data.get("profiles"), project_id, conn=conn)
        for role_key in role_templates:
            if await db_role_templates.get_template(project_id, role_key, pool=conn) is None:
                raise _refuse(f"project {project_id!r} has no role template {role_key!r}")
    else:
        profiles = data.get("profiles") or {}
        if not isinstance(profiles, dict):
            raise _refuse('profiles must be an object: {"planner" | "worker" | "reviewer": profile}')
        for role, profile in profiles.items():
            if role not in projects.TASK_PROFILE_ROLES or not isinstance(profile, str):
                raise _refuse(f"profiles.{role}: give an Agent Profile for a planner, worker or reviewer")
    return {
        "name": name, "description": description, "project_id": project_id, "work_dir": work_dir,
        "workspace_mode": workspace_mode, "requirement": requirement, "planner_instructions": instructions,
        "planner_profile_id": profiles.get("planner"), "worker_profile_id": profiles.get("worker"),
        "reviewer_profile_id": profiles.get("reviewer"), "role_templates": role_templates,
        "inputs": [{"key": i.key, "label": i.label, "kind": i.kind.value, "required": i.required,
                    "pattern": i.pattern, "hint": i.hint} for i in inputs]}


async def put_template(manager_id: str, key: str, data: Mapping[str, Any]) -> dict[str, Any]:
    """Create template ``key`` of the Manager or replace it whole (404 for an unknown Manager, 400 for a template
    Looper refuses, or for the default Manager, whose task tool never takes a template). Answers ``managers_view()``."""
    if await db_managers.get_manager(manager_id) is None:
        raise _refuse(f"no Manager {manager_id!r}", HTTP_404)
    values = await checked_template_values(manager_id, key, data)
    try:
        async with db_connection.transaction() as conn:
            current = await db_managers.lock_for_update(manager_id, conn=conn)
            if current is None or current["deleted_at"] is not None:
                raise _refuse(f"no Manager {manager_id!r}", HTTP_404)
            if current["task_scope"] == "projects" and values["project_id"] not in current["scope_project_ids"]:
                raise _refuse("this Manager works only in its projects: give the template one of them")
            stored = await db_managers.put_template(manager_id, key, values, conn=conn)
    except asyncpg.ForeignKeyViolationError:
        raise _refuse("the project or an Agent Profile was removed meanwhile", HTTP_409) from None
    if stored is None:  # deleted after the early check
        raise _refuse(f"no Manager {manager_id!r}", HTTP_404)
    await _on_change(manager_id)
    return await managers_view()


async def delete_template(manager_id: str, key: str) -> dict[str, Any]:
    """Delete template ``key`` of the Manager; 404 for an unknown Manager or template. Answers
    ``managers_view()``."""
    try:
        result = await db_managers.delete_template(manager_id, key)
    except asyncpg.ForeignKeyViolationError:
        schedule = await db_managers.template_schedule(manager_id, key)
        raise _refuse(f"template {key!r} is used by schedule {schedule or 'being created'!r}; "
                      "change or delete that schedule first", HTTP_409) from None
    if result == "no_manager":
        raise _refuse(f"no Manager {manager_id!r}", HTTP_404)
    if result == "missing":
        raise _refuse(f"Manager {manager_id!r} has no template {key!r}", HTTP_404)
    await _on_change(manager_id)
    return await managers_view()
