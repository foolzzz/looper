"""The dashboard's side of Planner-defined roles: a project's role templates, and the user's edits of a task's roles
(Planner-defined roles RFC, section 18).

- **Templates** (``project_role_templates``): checked as a task role is (``task_roles.check_definition``,
  ``allowed_profile``), whatever the project's ``planner_roles``; its tasks' Planners copy one as a task role
  (``ops.planner``, ``looper_task_role_set``'s ``template``).
- **The summary**: the roles' numbers summed by id across the project's tasks (``role_stats``).
- **Editing a task's role**: a new version written by the user (``Via.USER``), with its ``role_defined`` entry and a
  supplement that tells the task's Planner, in one transaction; a running Planner hears of it at once
  (``TASK_CHANGE``). Its Planner may not change a role whose current version is the user's.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime
from typing import Any

import asyncpg

from ..db import projects as db_projects, role_templates as db_templates, task_roles as db_roles, tasks as db_tasks
from .bus import MessageBus
from .message import AgentAddress, MessageEnvelope, MessageType, planner_address
from .project_execution import ProjectExecution
from .projects import AGENTS_EDITABLE_STATUSES, HTTP_400, HTTP_404, HTTP_409, VIA_TASK_PAGE, RequestError
from .task_roles import (
    DESCRIPTION_MAX_CHARS,
    RoleError,
    RoleKind,
    TaskRole,
    Via,
    allowed_profile,
    check_definition,
    check_items,
    named_items,
    project_role_stats,
    role_items,
    role_key,
)
from .tiers.journal import SUMMARY_MAX_CHARS, JournalKind, clip
from .tiers.task import TaskRouter

TEMPLATE_FIELDS = ("kind", "description", "role_prompt", "harness", "profile_id", "items")
EDIT_FIELDS = ("role_prompt", "harness", "profile_id", "items")


async def template_view(row: Mapping[str, Any]) -> dict[str, Any]:
    """A template as the API shows it: its profile with whether the project allows it now, and its items."""
    return {
        "key": row["template_key"], "kind": row["kind"], "description": row["description"],
        "role_prompt": row["role_prompt"], "harness": row["harness"],
        "profile": ({"profile_id": row["profile_id"], "name": row["profile_name"],
                     "allowed": bool(row["profile_allowed"])} if row["profile_id"] else None),
        "items": await named_items(row.get("items") or ()),
        "updated_at": row["updated_at"],
    }


async def _project(project_id: str) -> dict[str, Any]:
    project = await db_projects.get_project(project_id)
    if project is None:
        raise RequestError(HTTP_404, f"project {project_id!r} not found")
    return project


async def templates_view(project_id: str) -> dict[str, Any]:
    """Project ``project_id``'s role templates, by key; 404 for an unknown project."""
    project = await _project(project_id)
    return {"project_id": project_id, "role_text_max_chars": ProjectExecution.from_row(project).role_text_limit,
            "templates": [await template_view(row) for row in await db_templates.list_templates(project_id)],
            # What a role or a template may name in items: the catalog items installed for roles here.
            "role_items": await role_items(project_id)}


def _fields(data: Mapping[str, Any], allowed: tuple[str, ...]) -> None:
    unknown = sorted(set(data) - set(allowed))
    if unknown:
        raise RequestError(HTTP_400, f"unknown field {unknown[0]!r}; the fields are {', '.join(allowed)}")


async def _profile_id(project_id: str | None, wanted: Any, kind: RoleKind) -> str | None:
    """The profile a template or an edit names, by id or name, checked against the project's list; None for none."""
    if wanted is None or wanted == "":
        return None
    if not isinstance(wanted, str):
        raise RequestError(HTTP_400, "profile_id must be an Agent Profile's id or name, or null for none")
    try:
        return str((await allowed_profile(project_id, wanted, kind))["id"])
    except RoleError as exc:
        raise RequestError(HTTP_400, f"profile_id: {exc}") from None


async def _items(project_id: str | None, raw: Any) -> tuple[str, ...]:
    """The items a template or an edit names, as ids, each installed for roles in the project."""
    try:
        return await check_items(project_id, raw)
    except RoleError as exc:
        raise RequestError(HTTP_400, f"items: {exc}") from None


async def put_template(project_id: str, key: str, data: Mapping[str, Any]) -> dict[str, Any]:
    """Create template ``key`` of project ``project_id`` or replace it whole with ``data`` (``kind``,
    ``role_prompt``, and optionally ``description``, ``harness``, ``profile_id``, ``items``); 400 for a definition a
    task role would be refused, 404 for an unknown project. Answers the project's templates."""
    _fields(data, TEMPLATE_FIELDS)
    project = await _project(project_id)
    limit = ProjectExecution.from_row(project).role_text_limit
    try:
        checked_key = role_key(key)
        kind, prompt, harness = check_definition(data.get("kind"), data.get("role_prompt"), data.get("harness"), limit)
    except RoleError as exc:
        raise RequestError(HTTP_400, str(exc)) from None
    description = data.get("description") or ""
    if not isinstance(description, str) or len(" ".join(description.split())) > DESCRIPTION_MAX_CHARS:
        raise RequestError(HTTP_400, f"description must be a line of at most {DESCRIPTION_MAX_CHARS} characters")
    profile_id = await _profile_id(project_id, data.get("profile_id"), kind)
    items = await _items(project_id, data.get("items"))
    try:
        await db_templates.put_template(project_id, checked_key, {
            "kind": kind.value, "description": " ".join(description.split()), "role_prompt": prompt,
            "harness": harness, "profile_id": profile_id, "items": items})
    except asyncpg.ForeignKeyViolationError:
        raise RequestError(HTTP_409, "the project or the profile was removed meanwhile") from None
    return await templates_view(project_id)


async def role_stats(project_id: str, since: str | None = None) -> dict[str, Any]:
    """The numbers of the roles project ``project_id``'s Planners defined, summed by role id across its tasks
    (``task_roles.project_role_stats``), of the tasks created on or after day ``since`` (YYYY-MM-DD) when given; 404
    for an unknown project, 400 for a day that does not read as one."""
    await _project(project_id)
    start = None
    if since:
        try:
            start = datetime.combine(date.fromisoformat(since), datetime.min.time()).astimezone()
        except ValueError:
            raise RequestError(HTTP_400, f"since must be a day (YYYY-MM-DD), not {since[:40]!r}") from None
    return await project_role_stats(project_id, start)


async def delete_template(project_id: str, key: str) -> dict[str, Any]:
    """Delete template ``key`` of project ``project_id`` (the task roles copied from it keep their versions); 404
    when there is none. Answers the project's templates."""
    await _project(project_id)
    if not await db_templates.delete_template(project_id, key):
        raise RequestError(HTTP_404, f"project {project_id!r} has no role template {key!r}")
    return await templates_view(project_id)


def _changed(before: TaskRole, prompt: str, harness: str | None, profile_id: str | None,
             items: tuple[str, ...]) -> list[str]:
    return [name for name, changed in (("role prompt", prompt != before.role_prompt),
                                       ("harness", harness != before.harness),
                                       ("profile", profile_id != before.profile_id),
                                       ("items", items != before.items)) if changed]


async def edit_task_role(
    task_id: str, key: str, data: Mapping[str, Any], *, task_router: TaskRouter, bus: MessageBus, source: AgentAddress,
) -> dict[str, Any]:
    """Write ``data`` (``role_prompt``, ``harness``, ``profile_id``: the whole definition, and ``items``, the
    current ones when left out; the kind stays) as a new version of task ``task_id``'s role ``key``, by the user:
    404 for an unknown task or role, 409 for a task that completed, 400 for a definition a task role would be
    refused or one that changes nothing. Its Planner gets a supplement that says so, in a turn when it runs. Answers
    the version."""
    _fields(data, EDIT_FIELDS)
    task = await db_tasks.get_task(task_id)
    if task is None:
        raise RequestError(HTTP_404, "task not found")
    if task["status"] not in AGENTS_EDITABLE_STATUSES:
        raise RequestError(HTTP_409, f"the task is {task['status']}: the roles of a completed task do not change")
    current = await db_roles.get(task_id, key)
    if current is None:
        raise RequestError(HTTP_404, f"task {task_id} has no role {key!r}")
    before = TaskRole.from_row(current)
    project = await db_projects.get_project(task["project_id"]) if task.get("project_id") else None
    try:
        kind, prompt, harness = check_definition(before.kind.value, data.get("role_prompt"), data.get("harness"),
                                                 ProjectExecution.from_row(project).role_text_limit)
    except RoleError as exc:
        raise RequestError(HTTP_400, str(exc)) from None
    profile_id = await _profile_id(task.get("project_id"), data.get("profile_id"), kind)
    items = await _items(task.get("project_id"), data["items"]) if "items" in data else before.items
    changed = _changed(before, prompt, harness, profile_id, items)
    if not changed:
        raise RequestError(HTTP_400, f"role {key} already reads so: nothing to change")
    version = before.version + 1
    summary = f"Role {key} ({kind.value}) v{version} by the user: changed {', '.join(changed)}"
    note = (f"The user edited role {key} on the task page: it is now version {version} ({', '.join(changed)} "
            "changed); the attempts that start from now on run it, and you cannot change it.")
    entries = [{"step_id": "", "kind": JournalKind.ROLE_DEFINED.value, "summary": clip(summary, SUMMARY_MAX_CHARS),
                "learnings": []},
               {"step_id": "", "kind": JournalKind.SUPPLEMENT.value, "summary": note, "learnings": [],
                "via": VIA_TASK_PAGE}]
    try:
        version = await db_roles.add_version(task_id, key, {
            "kind": kind.value, "profile_id": profile_id, "role_prompt": prompt, "harness": harness,
            "via": Via.USER.value, "template_key": current.get("template_key"), "items": items}, entries,
            after=before.version)
    except asyncpg.UniqueViolationError:
        version = None
    except asyncpg.ForeignKeyViolationError:
        raise RequestError(HTTP_409, "the profile was removed meanwhile") from None
    if version is None:
        raise RequestError(HTTP_409, f"role {key} changed meanwhile; look again")
    if task_router.has_planner(task_id):
        await bus.publish(MessageEnvelope.create(source=source, target=planner_address(task_id),
                                                 message_type=MessageType.TASK_CHANGE, payload={"task_id": task_id}))
    return {"task_id": task_id, "id": key, "kind": kind.value, "version": version, "via": Via.USER.value,
            "changed": changed}
