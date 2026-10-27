"""The Manager's T1 tools (Manager tools RFC, section 7): the overview, tasks, and changing them.

Each tool calls the code the dashboard's API calls (``projects``, the
TaskRouter's messages, ``db.task_changes``), so the two check alike. A task
the Manager creates is ready, not started; Looper adds its configuration and
the question whether to start it to the Manager's reply. A new project, and
cancelling a task, are ``high``: the user confirms them first.
"""

from __future__ import annotations

import dataclasses
import logging
import operator
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from ...db import agent_setting_layers as db_layers, projects as db_projects
from ...db import steps as db_steps, tasks as db_tasks
from ...db import managers as db_managers, user_gates as db_user_gates
from .. import task_templates, task_workspaces
from ..managers_api import DEFAULT_MANAGER_ID, TaskPolicy
from ..task_templates import TaskTemplate, TemplateError
from ..gate_service import origin_chat, post_message as post_gate_message
from ..message import MessageEnvelope, MessageType, is_lark, planner_address
from ..projects import HTTP_404, HTTP_409, PROFILES_NOTE, TASK_PROFILE_ROLES, RequestError
from ..projects import edit_requirement, profile_choices, project_name, rename_task, set_task_agents
from ..projects import add_task_supplement, check_task_control, control_task
from ..projects import create_task_core, ProjectNameTaken
from ..projects import SOURCE_WORDS as SOURCE_WORDS, START_QUESTION as START_QUESTION
from ..projects import configuration_block as configuration_block
from ..system_prompt import TASK_REQUIREMENT_LIMIT
from ..task_notice import notify_target
from ..tiers.journal import clip
from ..tiers.task import resolve_work_dir
from . import API_CHANNEL, Call, ErrorCode, OpError, Risk, Tool, Turn

logger = logging.getLogger(__name__)

# What looper_tasks lists by default, at most, and how many recent tasks a query searches.
TASKS_LISTED = 20
TASKS_SEARCHED = 200
# The existing projects a new-project confirmation offers instead.
PROJECTS_OFFERED = 10
# The statuses a task can be run again from: "restart" goes on from its saved plan, as Start on the Tasks page does;
# "start_over" plans it anew.
RESTARTABLE = db_tasks.RESTARTABLE_STATUSES

_PROFILES_SCHEMA = {
    "type": "object",
    "description": "An Agent Profile (its id or its name) for some of the task's roles; null clears the task's own "
                   "choice of that role (looper_task_update only).",
    "properties": {role: {"type": "string"} for role in TASK_PROFILE_ROLES},
}


_PROMPT_MODES_SCHEMA = {
    "type": "object",
    "description": "The task's own append or replace choice per role; null returns it to the role setting.",
    "properties": {role: {"enum": ["append", "replace", None]} for role in TASK_PROFILE_ROLES},
}


def _via(turn: Turn) -> str:
    """How a change the Manager made came, as ``db.task_changes`` records it."""
    if turn.channel == API_CHANNEL:
        return f"client:{turn.client_name or 'operator'}"
    if turn.client_id:
        return "manager:loopercli"
    return f"manager:{turn.channel or 'unknown'}"


def _request_error(exc: RequestError) -> OpError:
    code = {HTTP_404: ErrorCode.NOT_FOUND, HTTP_409: ErrorCode.CONFLICT}.get(exc.status, ErrorCode.INVALID)
    return OpError(code, str(exc))


async def _task(task_id: str) -> dict[str, Any]:
    task = await db_tasks.get_task(task_id)
    if task is None:
        raise OpError(ErrorCode.NOT_FOUND, f"no task {task_id}: find its id with looper_tasks")
    return task


def _group(task: Mapping[str, Any]) -> db_tasks.TaskGroup:
    return db_tasks.task_group(task.get("status"))


def _ended(task: Mapping[str, Any]) -> bool:
    return _group(task) in (db_tasks.TaskGroup.COMPLETED, db_tasks.TaskGroup.CANCELLED, db_tasks.TaskGroup.ATTENTION)


# --- looper_overview ---


async def _overview(call: Call) -> dict[str, Any]:
    from ..overview import build_overview

    platform = call.platform
    manager_id = call.manager_id if call.scoped else None
    view = await build_overview(task_router=platform.task_router, worker_pool=platform.worker_pool,
                                manager=platform.manager, supervisor=platform.supervisor, manager_id=manager_id,
                                managers=getattr(platform, "managers", None) if manager_id is None else None)
    agents = view["agents"]
    own = await db_layers.task_concurrency([p["task_id"] for p in agents["planners"]])
    return {
        "tasks": view["tasks"],
        # A running task's own Worker or Reviewer limit, when it has one (looper_concurrency).
        "running_tasks": [{"task_id": p["task_id"], "name": p["name"], "project": p["project_name"],
                           **({"concurrency": own[p["task_id"]]} if p["task_id"] in own else {})}
                          for p in agents["planners"]],
        # A run as a role its task's Planner defined names it (task_role, the role's id).
        "step_runs": [{"task_id": r["task_id"], "role": r["role"],
                       **({"task_role": r["task_role"]["id"]} if r.get("task_role") else {}), "step": r["step"],
                       "state": r["state"], "elapsed_seconds": r["elapsed_seconds"]}
                      for r in agents["workers"] + agents["reviewers"]],
        "projects": view["projects"],
        # The user gates waiting for the user's answer in the running tasks ("Open gates" lists them).
        "waiting_gates": len(await db_user_gates.open_gates(TASKS_SEARCHED, manager_id=manager_id)),
        "spend_today": view["spend_today"],
        "unhealthy": view["health"]["unhealthy"],
    }


# --- looper_tasks ---


async def _tasks(call: Call) -> dict[str, Any]:
    arguments = call.arguments
    if arguments["action"] in ("get", "room"):
        task_id = arguments.get("task_id")
        if not task_id:
            raise OpError(ErrorCode.INVALID, f"task_id is required for action {arguments['action']}")
        return await _task_view(task_id) if arguments["action"] == "get" else await _room_view(call, task_id)
    project_id = None
    if arguments.get("project"):
        project = await db_projects.find_project_by_name(project_name(arguments["project"]))
        if project is None:
            raise OpError(ErrorCode.NOT_FOUND, f"no project named {arguments['project']!r}")
        project_id = project["id"]
    group = db_tasks.TaskGroup(arguments["group"]) if arguments.get("group") else None
    limit = int(arguments.get("limit") or TASKS_LISTED)
    from ..projects import list_tasks_page

    return await list_tasks_page(
        limit=limit, project_id=project_id, group=group,
        manager_id=call.manager_id if call.scoped else None,
        query=str(arguments.get("query") or ""), compact=True, search_limit=TASKS_SEARCHED)


# The newest room messages ``looper_tasks`` room shows, each clipped.
ROOM_MESSAGES_SHOWN = 40


async def _room_view(call: Call, task_id: str) -> dict[str, Any]:
    """A task's agent room, read only: its seats and their presence, its numbers, its newest messages."""
    from ..room import task_view

    pool = call.platform.worker_pool
    view = await task_view(task_id, runs=pool.room_runs(task_id) if pool is not None else None,
                           limit=ROOM_MESSAGES_SHOWN)
    if view is None:
        raise OpError(ErrorCode.NOT_FOUND, f"no task {task_id}: find its id with looper_tasks")
    return {"task_id": task_id, "enabled": view["enabled"], "counts": view["counts"],
            "seats": [{"seat": s["seat"], "step": s["step"], "presence": s["presence"]} for s in view["seats"]],
            "messages": [{**m, "body": clip(m["body"], 500)} for m in view["messages"]],
            "more": view["more"]}


async def _task_view(task_id: str) -> dict[str, Any]:
    from ..projects import task_detail

    try:
        return await task_detail(task_id, compact=True)
    except RequestError as exc:
        if exc.status == HTTP_404:
            raise OpError(ErrorCode.NOT_FOUND, f"no task {task_id}: find its id with looper_tasks") from None
        raise _request_error(exc) from None


def _gate_listed(gate: Mapping[str, Any]) -> dict[str, Any]:
    from ..projects import task_gate_view

    return task_gate_view(gate)


# --- looper_task_create ---


@dataclass(frozen=True)
class Placement:
    """Where a new task goes, the template's choice where it set one and the call's otherwise: the project's name,
    the project (None: it does not exist), the directory, whether the directory was named (by the template or the
    call) and the workspace mode (None: the project's)."""

    name: str
    project: dict[str, Any] | None
    work_dir: str
    named: bool
    workspace_mode: str | None


def _fixed(template_name: str, field: str, template_value: Any, given: Any, same: Callable[[Any, Any], bool]) -> Any:
    """What a field the template sets is: the template's, also when the call gives the same value; a different
    one is refused. A call's value never overrides a template's."""
    if given is None or given == "":
        return template_value
    if same(given, template_value):
        return template_value
    raise OpError(ErrorCode.INVALID, f"the template {template_name} sets {field} to {template_value}: leave "
                                     f"{field} out")


def _same_name(given: Any, name: str) -> bool:
    return isinstance(given, str) and given.strip().casefold() == name.casefold()


def _resolved(raw: str) -> str:
    try:
        return resolve_work_dir(raw)
    except ValueError as exc:
        raise OpError(ErrorCode.INVALID, f"work_dir: {exc}") from None


async def effective_placement(template: TaskTemplate | None, arguments: Mapping[str, Any]) -> Placement:
    """The new task's project, directory and workspace mode: for each, the template's when it sets one, else the
    call's, else the project's. The confirmation and the creation both read this one result. Without a template
    it is what the call alone says."""
    fixed = template.project_id if template is not None else None
    if fixed:
        project = await db_projects.get_project(fixed)
        if project is None:
            raise OpError(ErrorCode.CONFLICT, "the template's project no longer exists; tell the user to fix the "
                                              "template on the dashboard")
        given = arguments.get("project")
        _fixed(template.name, "project", project["name"], given.strip() if isinstance(given, str) else given,
               _same_name)
        name = project["name"]
    else:
        try:
            name = project_name(arguments.get("project"))
        except ValueError as exc:
            raise OpError(ErrorCode.INVALID, f"{exc}: ask the user which project the task belongs to") from None
        project = await db_projects.find_project_by_name(name)
    explicit = str(arguments.get("work_dir") or "").strip()
    if template is not None and template.work_dir:
        given_dir = _resolved(explicit) if explicit else None
        _fixed(template.name, "work_dir", template.work_dir, given_dir, task_workspaces.same_directory)
        work_dir, named = _resolved(template.work_dir), True
    elif explicit:
        work_dir, named = _resolved(explicit), True
    elif project is not None and project["work_dir"]:
        work_dir, named = project["work_dir"], False
    else:
        whose = "a new project" if project is None else f"project {project['name']!r}, which has no default"
        raise OpError(ErrorCode.INVALID, f"the task needs a directory ({whose}): ask the user for it, as an absolute "
                                         "path, and pass it as work_dir; never pick one yourself")
    mode = arguments.get("workspace_mode")
    if template is not None and template.workspace_mode:
        mode = _fixed(template.name, "workspace_mode", template.workspace_mode, mode, operator.eq)
    return Placement(name, project, work_dir, named, mode)


async def _template_of(call: Call) -> tuple[TaskTemplate | None, dict[str, str]]:
    """The template the call names and its checked inputs; ``(None, {})`` for a call without one (refused first
    when the Manager's policy is ``templates_only``). Read once per call."""
    cached = getattr(call, "_template", None)
    if cached is not None:
        return cached
    arguments = call.arguments
    key = arguments.get("template")
    if key is None:
        if arguments.get("inputs") is not None:
            raise OpError(ErrorCode.INVALID, "inputs go with a template")
        if call.scope.task_policy is TaskPolicy.TEMPLATES_ONLY:
            raise OpError(ErrorCode.INVALID, "this Manager creates tasks only from its templates "
                                             f"({', '.join(call.scope.template_keys)}): pick the one that fits, or "
                                             "tell the user none fits")
        found: tuple[TaskTemplate | None, dict[str, str]] = (None, {})
    else:
        row = await db_managers.get_template(call.manager_id, key)
        if row is None:
            keys = [t["key"] for t in await db_managers.list_templates(call.manager_id)]
            raise OpError(ErrorCode.INVALID, f"no template {key}; this Manager's templates are: "
                                             f"{', '.join(keys) or 'none'}")
        template = TaskTemplate.from_row(row)
        try:
            values = await task_templates.check_values(template, arguments.get("inputs"))
        except TemplateError as exc:
            raise OpError(ErrorCode.INVALID, str(exc)) from None
        found = (template, values)
    object.__setattr__(call, "_template", found)
    return found


async def _new_project(call: Call) -> str | None:
    """None when the task's project exists; else the question whether to create it (the call is ``high``)."""
    template, _ = await _template_of(call)
    placement = await effective_placement(template, call.arguments)
    await _profile_choices(call.arguments.get("profiles"), clearing=False)
    if placement.project is not None:
        return None
    name, work_dir = placement.name, placement.work_dir
    lines = [f"项目「{name}」还不存在。要新建这个项目（默认目录 {work_dir}），并在里面创建任务"
             f"「{db_tasks.task_title(call.arguments.get('title')) or ''}」吗？"]
    try:
        projects = await db_projects.list_projects()
    except Exception:
        logger.warning("could not list the projects for a new-project confirmation", exc_info=True)
        projects = []
    if projects:
        offered = "、".join(f"{p['name']}（{p['work_dir'] or '没有默认目录'}）" for p in projects[:PROJECTS_OFFERED])
        more = f"，共 {len(projects)} 个" if len(projects) > PROJECTS_OFFERED else ""
        lines.append(f"也可以改用已有项目：{offered}{more}。")
    return "\n".join(lines)


async def _merged_profiles(template: TaskTemplate | None, arguments: Mapping[str, Any]) -> dict[str, str | None]:
    """The task's Agent Profile per role: the template's, plus the call's for the other roles; each able to run
    its role (the call's were checked by ``_profile_choices``, the template's are checked again here)."""
    from ..task_template_execution import merged_profiles, TemplateConflict
    profiles = await _profile_choices(arguments.get("profiles"), clearing=False)
    try:
        return await merged_profiles(template, profiles)
    except TemplateConflict as exc:
        raise OpError(ErrorCode.CONFLICT, str(exc)) from None
    except TemplateError as exc:
        raise OpError(ErrorCode.INVALID, str(exc)) from None


async def _template_roles(template: TaskTemplate, project: Mapping[str, Any], fallbacks: list[str]) -> list[dict[str, Any]]:
    from ..task_template_execution import template_roles
    try:
        return await template_roles(template, project, interactive=True, fallbacks=fallbacks)
    except TemplateError as exc:
        raise OpError(ErrorCode.CONFLICT, str(exc)) from None


async def _create(call: Call) -> dict[str, Any]:
    arguments, turn = call.arguments, call.turn
    template, values = await _template_of(call)
    placement = await effective_placement(template, arguments)
    name, project, work_dir = placement.name, placement.project, placement.work_dir
    profiles = await _merged_profiles(template, arguments)
    fallbacks: list[str] = []
    roles = await _template_roles(template, project, fallbacks) if template is not None and project is not None else []
    requirement = arguments["requirement"].strip()
    if template is not None:
        try:
            requirement = task_templates.render_requirement(template, values, arguments["requirement"])
        except TemplateError as exc:
            raise OpError(ErrorCode.INVALID, str(exc)) from None
    envelope = turn.envelope
    origin = envelope.metadata.get("origin_channel_id") if envelope is not None else None
    target = notify_target(origin, sender=turn.sender, chat=turn.chat, group=turn.group)
    row = {
        "correlation_id": envelope.correlation_id if envelope is not None else "", "origin_channel_id": origin or "",
        **(target.columns() if target is not None else {}),
        "original_user_text": (str(envelope.payload.get("text") or "") or None) if envelope is not None else None,
        "manager_id": call.manager_id, "client_id": turn.client_id,
    }
    if origin == "channel-web" and envelope is not None and (call.manager_id != DEFAULT_MANAGER_ID or turn.client_id):
        thread = (envelope.payload.get("channel_context") or {}).get("session_id")
        row["origin_thread"] = thread if isinstance(thread, str) and thread else None
    if template is not None:
        row["template"] = template.snapshot(values, roles=roles)
    used = None if template is None else (template.name, [(i.label, clip(values[i.key], 80))
                                                         for i in template.inputs if i.key in values])
    data = {**arguments, "requirement": requirement, "work_dir": work_dir,
            "workspace_mode": placement.workspace_mode}
    # Names and template constraints are Manager parameters; storage uses the same project shape as the console.
    data.pop("project", None)
    if project is not None:
        data["project_id"] = project["id"]
    else:
        data["project"] = {"name": name, "work_dir": work_dir}
    try:
        result = await create_task_core(
            data, task_fields=row, profiles=profiles, roles=roles,
            template_name=template.name if template is not None else None,
            lock_manager=True, named=placement.named, visible_to=call.manager_id if call.scoped else None,
            show_configuration=True, template_display=used)
    except ProjectNameTaken:
        raise OpError(ErrorCode.CONFLICT, f"a project named {name!r} was created meanwhile: call again to use "
                                          "it") from None
    except RequestError as exc:
        raise _request_error(exc) from None
    task_id, shown = result["task_id"], result["name"]
    project, created = result["project"], result["project_created"]
    block = result["configuration"]
    if fallbacks:
        block = block.replace(f"\n\n{START_QUESTION}", f"\n- 模板角色回退：{clip('；'.join(fallbacks), 1000)}\n\n{START_QUESTION}")
    turn.blocks.append(block)
    turn.created_task_blocks[task_id] = block
    return {"task_id": task_id, "name": shown, "status": "ready", "project": project["name"],
            "project_created": created, "work_dir": work_dir, "workspace_mode": result["workspace"]["mode"],
            "shown_to_user": "Looper adds the task's configuration and the question whether to start it now after "
                             "your reply; do not repeat them."}


async def _profile_choices(raw: Any, *, clearing: bool) -> dict[str, str | None]:
    """``profiles`` as role -> Agent Profile id, checked as the task API checks them (``projects.profile_choices``)."""
    try:
        return await profile_choices(raw, clearing=clearing)
    except RequestError as exc:
        raise _request_error(exc) from None


# --- looper_task_control ---


async def _confirming(call: Call) -> str | None:
    """What cancelling a task or starting it over does, for the user to confirm; None for the other actions
    (``write``)."""
    action = call.arguments["action"]
    if action == "cancel":
        task = await _cancellable(call.arguments["task_id"])
        return (f"要取消任务「{task['name']}」（{task['id']}，现在 {task['status']}）吗？它的 Planner 和正在运行的 "
                "Worker、Reviewer 会被停止，任务记为已取消；已经改过的文件留在原处。取消后还可以重启，从保存的计划接着做。")
    if action != "start_over":
        return None
    task = await _restartable(call.arguments["task_id"])
    done = sum(1 for step in await db_steps.list_steps(task["id"]) if step.get("status") == "verified")
    return (f"要从头重新开始任务「{task['name']}」（{task['id']}，现在 {task['status']}）吗？它的计划、{done} 个已完成的"
            "步骤、Planner 的会话和各步骤的 worktree 都会删除，然后按现在的需求从头规划，已完成的步骤会重新执行；"
            "已经改过的文件和已合并的提交留在原处。只想接着做的话，用 restart。")


async def _cancellable(task_id: str) -> dict[str, Any]:
    try:
        return await check_task_control(task_id, "cancel")
    except RequestError as exc:
        raise _request_error(exc) from None


async def _restartable(task_id: str) -> dict[str, Any]:
    try:
        return await check_task_control(task_id, "restart")
    except RequestError as exc:
        raise _request_error(exc) from None


async def _control(call: Call) -> dict[str, Any]:
    task_id, action = call.arguments["task_id"], call.arguments["action"]
    if action == "start":
        task = await _task(task_id)
        status = task["status"]
        if status != db_tasks.READY:
            hint = " (a stopped, failed or cancelled task runs again with action restart)" if status in RESTARTABLE \
                else ""
            raise OpError(ErrorCode.CONFLICT, f"task {task_id} is {status}, not ready: start only starts a task that "
                                              f"was created and never started{hint}")
        from ..projects import start_ready_task
        try:
            await start_ready_task(task_id, call.platform.bus, call.manager_address)
        except RequestError as exc:
            raise _request_error(exc) from None
        # TaskRouter may not have consumed the start yet; the successful request is enough to stop asking.
        if block := call.turn.created_task_blocks.pop(task_id, None):
            configuration = block.partition("\n")[2].removesuffix(f"\n\n{START_QUESTION}")
            call.turn.blocks[call.turn.blocks.index(block)] = f"任务已创建：\n{configuration}"
        return {"task_id": task_id, "started": True}
    try:
        await control_task(task_id, action, call.platform.bus, call.manager_address)
    except RequestError as exc:
        raise _request_error(exc) from None
    if action == "stop":
        return {"task_id": task_id, "stopped": True}
    if action == "restart":
        return {"task_id": task_id, "restarted": True,
                "note": "it goes on from its saved plan: its accepted steps are kept and do not run again, and its "
                        "Planner decides how the steps that did not finish go on"}
    if action == "start_over":
        return {"task_id": task_id, "started_over": True,
                "note": "its steps, plan and Planner session were deleted; it plans anew from its current requirement"}
    return {"task_id": task_id, "cancelled": True}


# --- looper_task_instruct ---


async def _instruct(call: Call) -> dict[str, Any]:
    task_id = call.arguments["task_id"]
    task = await _task(task_id)
    if task["status"] == db_tasks.READY:
        raise OpError(ErrorCode.CONFLICT, f"task {task_id} has not started, so no Planner can take a supplement: "
                                          "change its requirement with looper_task_update instead")
    if task["status"] not in db_tasks.ACTIVE_STATUSES:
        raise OpError(ErrorCode.CONFLICT, f"task {task_id} is {task['status']}: only a running task takes a "
                                          "supplement")

    async def wake(supplement_id: int | None) -> int | None:
        if call.platform.task_router.has_planner(task_id):
            await call.platform.bus.publish(MessageEnvelope.create(
                source=call.manager_address, target=planner_address(task_id), message_type=MessageType.TASK_CHANGE,
                payload={"task_id": task_id}))
        return supplement_id

    supplement = await add_task_supplement(task_id, call.arguments["text"], via=_via(call.turn), wake=wake)
    return {"task_id": task_id, "supplement_id": supplement,
            "note": "the task's Planner gets it in its next turn and decides whether to change the plan; every later "
                    "Worker and Reviewer sees it too"}


# --- looper_task_update ---


async def _update(call: Call) -> dict[str, Any]:
    arguments = call.arguments
    task_id = arguments["task_id"]
    requirement, work_dir, title = arguments.get("requirement"), arguments.get("work_dir"), arguments.get("title")
    own_agents = {field: arguments[field] for field in ("profiles", "system_prompt_modes") if arguments.get(field)}
    if requirement is None and work_dir is None and title is None and not own_agents:
        raise OpError(ErrorCode.INVALID, "give at least one of requirement, work_dir, title, profiles and "
                                         "system_prompt_modes")
    task = await _task(task_id)
    if _ended(task) and (requirement is not None or work_dir is not None):
        # Its profiles still change (set_task_agents says which tasks can), so that a restart runs with them.
        raise OpError(ErrorCode.CONFLICT, f"task {task_id} is {task['status']}: an ended task's requirement and "
                                          "directory do not change (restart it to run its requirement again)")
    if work_dir is not None:
        if task["status"] != db_tasks.READY:
            raise OpError(ErrorCode.CONFLICT, f"task {task_id} has started: only a task not started can change its "
                                              "directory")
        try:
            work_dir = resolve_work_dir(work_dir)
        except ValueError as exc:
            raise OpError(ErrorCode.INVALID, f"work_dir: {exc}") from None
    changed: list[str] = []
    if own_agents:
        # As the task page sets them: one check and one write (projects.set_task_agents).
        try:
            await set_task_agents(task_id, own_agents)
        except RequestError as exc:
            raise _request_error(exc) from None
        changed.extend(own_agents)
    if work_dir is not None:
        if not await db_tasks.update_task_config(task_id, work_dir=work_dir):
            raise OpError(ErrorCode.CONFLICT, f"task {task_id} started meanwhile: its directory stays")
        changed.append("work_dir")
    result: dict[str, Any] = {"task_id": task_id}
    if title is not None:
        try:
            renamed = await rename_task(task_id, {"title": title}, via=_via(call.turn))
        except RequestError as exc:
            raise _request_error(exc) from None
        result["title"] = renamed["title"]
        changed.append("title")
    if requirement is not None:
        try:
            edited = await edit_requirement(task_id, {"requirement": requirement},
                                            task_router=call.platform.task_router, bus=call.platform.bus,
                                            source=call.manager_address, via=_via(call.turn))
        except RequestError as exc:
            raise _request_error(exc) from None
        result["requirement_version"] = edited["version"]
        changed.append("requirement")
    result["changed"] = changed
    if "profiles" in changed:
        result["profiles_note"] = PROFILES_NOTE
    if "requirement" in changed and task["status"] != db_tasks.READY:
        result["requirement_note"] = "the task's Planner gets the edit in its next turn and decides whether to replan"
    return result


# --- looper_task_gate_reply ---


def _relayed_text(turn: Turn, excerpt: Any) -> str:
    """What goes into the gate: the user's message this turn answers, or the ``excerpt`` of it the model names,
    which must be in it word for word (spaces aside). The model's own words never go."""
    message = str(turn.envelope.payload.get("text") or "") if turn.envelope is not None else ""
    if not message.strip():
        raise OpError(ErrorCode.NEEDS_USER_TURN, "this turn answers no user message: there is nothing of the "
                                                 "user's to pass on")
    if excerpt is None:
        return message
    if not isinstance(excerpt, str) or not excerpt.strip():
        raise OpError(ErrorCode.INVALID, "excerpt is a non-empty part of the user's message")
    match = re.search(r"\s+".join(re.escape(word) for word in excerpt.split()), message)
    if match is None:
        raise OpError(ErrorCode.INVALID, "excerpt must be copied word for word from the user's message; leave it "
                                         "out to pass on the whole message")
    return message[match.start():match.end()]


async def _gate_reply(call: Call) -> dict[str, Any]:
    """The user's message (or the excerpt of it the model names) into the discussion of a gate that waits for them,
    as the task page adds it (``gate_service.post_message``); the channel message it is, so the Planner's reply goes
    under it when it came from the gate's chat."""
    arguments, turn = call.arguments, call.turn
    task_id, label = arguments["task_id"], arguments["gate"]
    text = _relayed_text(turn, arguments.get("excerpt"))
    context = (turn.envelope.payload.get("channel_context") if turn.envelope is not None else None) or {}
    origin = None
    if is_lark(turn.channel) or turn.channel == "telegram":
        origin = {"author": (context.get("sender_name") or turn.sender) if turn.group else None,
                  "channel": turn.channel, "channel_id": turn.channel_id,
                  "chat": origin_chat(turn.channel, turn.chat or "", group=turn.group, sender=turn.sender or ""),
                  "external_id": str(context.get("message_id") or "") or None}
    has_planner = call.platform.task_router.has_planner(task_id)
    try:
        posted = await post_gate_message(task_id, label, {"text": text},
                                         has_planner=has_planner, bus=call.platform.bus, source=call.manager_address,
                                         via=_via(turn), end=bool(arguments.get("end")), origin=origin)
    except RequestError as exc:
        raise _request_error(exc) from None
    kept = posted["delivered"] == "kept"
    return {"task_id": task_id, "gate": label, "delivered": posted["delivered"],
            "note": "the task is not running: its Planner gets it when the task continues" if kept else
                    "its Planner answers in the gate's discussion: on the task page, and in the gate's Lark or "
                    "Telegram chat when it has one"}


def _task_argument(arguments: Mapping[str, Any]) -> str | None:
    task_id = arguments.get("task_id")
    return task_id if isinstance(task_id, str) and task_id else None


_TASK_ID = {"type": "string", "minLength": 1, "description": "The task's id (task-...), from looper_tasks."}

TOOLS: tuple[Tool, ...] = (
    Tool(
        name="looper_overview",
        description="What is happening now: task counts by group, the running tasks (with their own concurrency "
                    "limits) and step runs, each active project's running tasks and step runs, today's spend, "
                    "unhealthy components.",
        schema={"type": "object", "properties": {}, "additionalProperties": False},
        risk=Risk.READ, run=_overview,
    ),
    Tool(
        name="looper_tasks",
        description="List or search tasks (newest first), or get one task with its steps (including each kind), "
                    "wait conditions and states, supplements, latest "
                    "progress note, result, cost and concurrency limits; room: what the task's agents said to each "
                    "other in its room (read only).",
        schema={
            "type": "object", "required": ["action"], "additionalProperties": False,
            "properties": {
                "action": {"type": "string", "enum": ["list", "get", "room"]},
                "task_id": {**_TASK_ID, "description": "The task to get (action get, room)."},
                "query": {"type": "string", "description": "Words to find in a task's id, name, requirement or "
                                                          "project (action list)."},
                "group": {"type": "string", "enum": [g.value for g in db_tasks.TaskGroup],
                          "description": "Only the tasks in this group (action list)."},
                "project": {"type": "string", "description": "Only this project's tasks, by name (action list)."},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50,
                          "description": f"At most this many (action list; default {TASKS_LISTED})."},
            },
        },
        risk=Risk.READ, run=_tasks, task_id=_task_argument,
    ),
    Tool(
        name="looper_task_create",
        description="Create a task, ready and not started, once the user confirmed it. A project that does not "
                    "exist is created only after the user confirms it too (the answer is needs_confirmation). "
                    "Looper adds the task's configuration and the question whether to start it to your reply.",
        schema={
            "type": "object", "required": ["requirement", "title", "project"], "additionalProperties": False,
            "properties": {
                "requirement": {"type": "string", "minLength": 1, "maxLength": TASK_REQUIREMENT_LIMIT,
                                "description": "The clear requirement, with what the user clarified."},
                "title": {"type": "string", "minLength": 1,
                          "description": "A few words naming the task, in the user's language."},
                "project": {"type": "string", "minLength": 1,
                            "description": "The project's name: an existing one, or a new one the user named."},
                "work_dir": {"type": "string", "description": "The absolute directory the user named; leave it out "
                                                              "for the project's default. A new project needs one."},
                "workspace_mode": {"type": "string", "enum": ["worktree", "in_place"],
                                   "description": "Only when the user said so: worktree (a git worktree of the "
                                                  "directory, on a branch of the task's own, left to the user to "
                                                  "merge) or in_place (in the directory itself). Leave it out for "
                                                  "the default."},
                "base": {"type": "string", "description": "Only when the user named one: the branch, tag or "
                                                          "commit a worktree task starts from (default: what the "
                                                          "directory has checked out)."},
                "goal_id": {"type": "string", "description": "A short kebab-case id for the task's goal."},
                "profiles": {**_PROFILES_SCHEMA, "description": "An Agent Profile (id or name) the user chose for "
                                                                "some of the task's roles."},
            },
        },
        risk=Risk.WRITE, run=_create, escalate=_new_project,
    ),
    Tool(
        name="looper_task_control",
        description="start a ready task; stop a running one; restart a stopped, failed or cancelled one "
                    "(it goes on from its saved plan: accepted steps are kept and not run again); start_over one "
                    "instead only when the user asks to start from scratch (its steps and plan are deleted and it "
                    "plans anew; the user confirms it first); cancel a running one (the user confirms it first).",
        schema={
            "type": "object", "required": ["task_id", "action"], "additionalProperties": False,
            "properties": {"task_id": _TASK_ID,
                           "action": {"type": "string",
                                      "enum": ["start", "stop", "restart", "start_over", "cancel"]}},
        },
        risk=Risk.WRITE, run=_control, escalate=_confirming, task_id=_task_argument,
    ),
    Tool(
        name="looper_task_instruct",
        description="Add what the user said to a running task as a supplement: stored with the task, its "
                    "Planner gets it in a turn and decides whether to change the plan, and every later Worker and "
                    "Reviewer sees it. To change a task that has not started, use looper_task_update.",
        schema={
            "type": "object", "required": ["task_id", "text"], "additionalProperties": False,
            "properties": {"task_id": _TASK_ID,
                           "text": {"type": "string", "minLength": 1,
                                    "description": "The user's words for the task, as they said them."}},
        },
        risk=Risk.WRITE, run=_instruct, task_id=_task_argument,
    ),
    Tool(
        name="looper_task_gate_reply",
        description="Pass the user's message to a user gate waiting for their answer (\"Open gates\"): Looper "
                    "puts the message itself into the gate's discussion, and its Planner answers there. excerpt: "
                    "only when part of the message is about the gate, that part, copied word for word. end: true "
                    "when the user wants the Planner to go on with the conclusions so far.",
        schema={
            "type": "object", "required": ["task_id", "gate"], "additionalProperties": False,
            "properties": {
                "task_id": _TASK_ID,
                "gate": {"type": "string", "minLength": 1, "description": "The gate's id in its task (g1, ...)."},
                "excerpt": {"type": "string", "description": "The part of the user's message that is about the "
                                                             "gate, copied word for word; leave it out to pass on "
                                                             "the whole message."},
                "end": {"type": "boolean", "description": "The user ends the discussion: the Planner closes the gate "
                                                          "on what is settled and states its assumptions."},
            },
        },
        risk=Risk.WRITE, run=_gate_reply, task_id=_task_argument,
    ),
    Tool(
        name="looper_task_update",
        description="Change a task: its title in any task (a label, only the name it is shown by), its requirement while it has not ended (kept as a new version; a running "
                    "task's Planner gets the edit in a turn), its directory (a task not started only), or the Agent "
                    "Profile of some of its roles in any task but a completed one (a Worker or Reviewer from its "
                    "next step on; a stopped, failed or cancelled task's restart runs with it).",
        schema={
            "type": "object", "required": ["task_id"], "additionalProperties": False,
            "properties": {
                "task_id": _TASK_ID,
                "requirement": {"type": "string", "minLength": 1, "maxLength": TASK_REQUIREMENT_LIMIT,
                                "description": "The whole new requirement."},
                "work_dir": {"type": "string", "minLength": 1, "description": "The new absolute directory."},
                "title": {"type": "string", "minLength": 1, "maxLength": db_tasks.TITLE_MAX_CHARS,
                          "description": "The new title: a few words naming the task, in the user's language."},
                "profiles": _PROFILES_SCHEMA,
                "system_prompt_modes": _PROMPT_MODES_SCHEMA,
            },
        },
        risk=Risk.WRITE, run=_update, task_id=_task_argument,
    ),
)


_CREATE_TOOL = next(tool for tool in TOOLS if tool.name == "looper_task_create")


def task_create_tool(template_keys: Sequence[str]) -> Tool:
    """``looper_task_create`` as a Manager sees it: with templates it also takes ``template`` and ``inputs``, and
    ``project`` is judged by code; without, the tool itself."""
    if not template_keys:
        return _CREATE_TOOL
    schema = {
        **_CREATE_TOOL.schema,
        "required": ["requirement", "title"],
        "properties": {
            **_CREATE_TOOL.schema["properties"],
            "template": {"type": "string", "enum": list(template_keys),
                         "description": "The key of the task template that fits (see Task templates in the "
                                        "message); its project, roles and Planner instructions apply."},
            "inputs": {"type": "object", "description": "The template's inputs, by key, in the user's words (a link "
                                                        "as the user gave it)."},
        },
    }
    return dataclasses.replace(
        _CREATE_TOOL, schema=schema,
        description=_CREATE_TOOL.description + " With a template, give its inputs; the template sets the project "
                                               "unless it has none.")
