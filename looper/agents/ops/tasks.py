"""The Manager's T1 tools (Manager tools RFC, section 7): the overview, tasks, and changing them.

Each tool calls the code the dashboard's API calls (``projects``, the
TaskRouter's messages, ``db.task_changes``), so the two check alike. A task
the Manager creates is ready, not started; Looper adds its configuration and
the question whether to start it to the Manager's reply. A new project, and
cancelling a task, are ``high``: the user confirms them first.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Mapping
from typing import Any

import asyncpg

from ...db import agent_setting_layers as db_layers, connection as db_connection, projects as db_projects
from ...db import steps as db_steps, task_changes as db_changes, tasks as db_tasks
from ..agent_settings import TASK_ROLES, AgentSettings, AgentType, Layer, index_layers, load_concurrency, resolve
from ..agent_settings import resolve_concurrency
from ..message import MANAGER_ADDRESS, TASK_ROUTER_ADDRESS, MessageEnvelope, MessageType, RestartMode, planner_address
from ..project_execution import LessonsMode, ProjectExecution, ReviewPolicy, TaskBudgetAction, format_usd
from ..projects import HTTP_404, HTTP_409, PROFILES_NOTE, TASK_PROFILE_ROLES, RequestError, agents_view
from ..projects import edit_requirement, profile_choices, project_name, set_task_agents
from ..system_prompt import TASK_REQUIREMENT_LIMIT
from ..task_notice import notify_target
from ..tiers.journal import SUPPLEMENTS_MAX_CHARS, clip, kept_text
from ..tiers.task import resolve_work_dir
from . import Call, ErrorCode, OpError, Risk, Tool, Turn

logger = logging.getLogger(__name__)

# What looper_tasks lists by default, at most, and how many recent tasks a query searches.
TASKS_LISTED = 20
TASKS_SEARCHED = 200
# The existing projects a new-project confirmation offers instead.
PROJECTS_OFFERED = 10
# The statuses a task can be run again from: "restart" goes on from its saved plan, as Start on the Tasks page does;
# "start_over" plans it anew.
RESTARTABLE = ("stopped", "failed", "cancelled")

# Where a setting in a task's configuration came from (``agent_settings.Layer``), named as the dashboard names it.
SOURCE_WORDS = {
    Layer.BUILTIN: "内置默认",
    Layer.SYSTEM: "共用默认（Settings → Roles → Shared defaults）",
    Layer.ROLE: "角色默认",
    Layer.PROJECT: "本项目",
    Layer.ROLE_PROJECT: "本项目里的这个角色",
    Layer.TASK: "本任务",
}
START_QUESTION = "现在开始吗？回复「开始」就会启动；也可以稍后在 Tasks 页面点 Start。"

_PROFILES_SCHEMA = {
    "type": "object",
    "description": "An Agent Profile (its id or its name) for some of the task's roles; null clears the task's own "
                   "choice of that role (looper_task_update only).",
    "properties": {role: {"type": "string"} for role in TASK_PROFILE_ROLES},
}


def _via(turn: Turn) -> str:
    """How a change the Manager made came, as ``db.task_changes`` records it."""
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


async def _publish(call: Call, message_type: MessageType, task_id: str, **payload: Any) -> None:
    await call.platform.bus.publish(MessageEnvelope.create(
        source=MANAGER_ADDRESS, target=TASK_ROUTER_ADDRESS, message_type=message_type,
        payload={"task_id": task_id, **payload},
    ))


# --- looper_overview ---


async def _overview(call: Call) -> dict[str, Any]:
    from ..overview import build_overview

    platform = call.platform
    view = await build_overview(task_router=platform.task_router, worker_pool=platform.worker_pool,
                                manager=platform.manager, supervisor=platform.supervisor)
    agents = view["agents"]
    own = await db_layers.task_concurrency([p["task_id"] for p in agents["planners"]])
    return {
        "tasks": view["tasks"],
        # A running task's own Worker or Reviewer limit, when it has one (looper_concurrency).
        "running_tasks": [{"task_id": p["task_id"], "name": p["name"], "project": p["project_name"],
                           **({"concurrency": own[p["task_id"]]} if p["task_id"] in own else {})}
                          for p in agents["planners"]],
        "step_runs": [{"task_id": r["task_id"], "role": r["role"], "step": r["step"], "state": r["state"],
                       "elapsed_seconds": r["elapsed_seconds"]} for r in agents["workers"] + agents["reviewers"]],
        "projects": view["projects"],
        "spend_today": view["spend_today"],
        "unhealthy": view["health"]["unhealthy"],
    }


# --- looper_tasks ---


def _listed(task: Mapping[str, Any]) -> dict[str, Any]:
    return {"task_id": task["id"], "name": task["name"], "status": task["status"], "group": _group(task).value,
            "project": task.get("project_name"), "created_at": task.get("created_at")}


async def _tasks(call: Call) -> dict[str, Any]:
    arguments = call.arguments
    if arguments["action"] == "get":
        task_id = arguments.get("task_id")
        if not task_id:
            raise OpError(ErrorCode.INVALID, "task_id is required for action get")
        return await _task_view(task_id)
    project_id = None
    if arguments.get("project"):
        project = await db_projects.find_project_by_name(project_name(arguments["project"]))
        if project is None:
            raise OpError(ErrorCode.NOT_FOUND, f"no project named {arguments['project']!r}")
        project_id = project["id"]
    group = db_tasks.TaskGroup(arguments["group"]) if arguments.get("group") else None
    limit = int(arguments.get("limit") or TASKS_LISTED)
    query = " ".join(str(arguments.get("query") or "").split()).casefold()
    tasks = await db_tasks.list_tasks(limit=TASKS_SEARCHED if query else limit, project_id=project_id, group=group)
    if query:
        tasks = [t for t in tasks if query in " ".join(
            str(t.get(key) or "") for key in ("id", "name", "requirement", "project_name", "goal_id")).casefold()]
    return {"tasks": [_listed(task) for task in tasks[:limit]], "more": len(tasks) > limit}


async def _task_view(task_id: str) -> dict[str, Any]:
    from ...db.llm_records import task_costs

    task = await db_tasks.get_task_with_steps(task_id)
    if task is None:
        raise OpError(ErrorCode.NOT_FOUND, f"no task {task_id}: find its id with looper_tasks")
    journal = task.get("journal") or []
    supplements = [entry for entry in journal if entry.get("kind") == db_changes.SUPPLEMENT]
    progress = [entry for entry in journal if entry.get("kind") == "progress"]
    return {
        **_listed(task),
        "phase": task.get("phase"),
        "work_dir": task.get("work_dir"),
        "requirement": task.get("requirement"),
        "steps": [{"step": s.get("step_key") or s.get("id"), "status": s.get("status"),
                   "instruction": clip(s.get("instruction") or "", 200)}
                  for s in task.get("steps") or []],
        "supplements": [{"text": clip(entry["summary"], 300), "delivered": entry.get("delivered_at") is not None}
                        for entry in supplements],
        "latest_progress": progress[-1]["summary"] if progress else None,
        "result": clip(task.get("result_summary") or task.get("error") or "", 1_000) or None,
        "cost_usd": ((await task_costs([task_id])).get(task_id) or {}).get("cost_usd"),
        # How many of each role run at once for it, with the source; its own Worker and Reviewer limits (task).
        "concurrency": {role: view["concurrency"] for role, view in (await agents_view(task))["roles"].items()},
    }


# --- looper_task_create ---


async def _placement(arguments: Mapping[str, Any]) -> tuple[str, dict[str, Any] | None, str]:
    """The new task's project name, the project (None: it does not exist) and the directory it runs in."""
    try:
        name = project_name(arguments.get("project"))
    except ValueError as exc:
        raise OpError(ErrorCode.INVALID, f"{exc}: ask the user which project the task belongs to") from None
    project = await db_projects.find_project_by_name(name)
    explicit = str(arguments.get("work_dir") or "").strip()
    if not explicit:
        if project is not None and project["work_dir"]:
            return name, project, project["work_dir"]
        whose = "a new project" if project is None else f"project {project['name']!r}, which has no default"
        raise OpError(ErrorCode.INVALID, f"the task needs a directory ({whose}): ask the user for it, as an absolute "
                                         "path, and pass it as work_dir; never pick one yourself")
    try:
        return name, project, resolve_work_dir(explicit)
    except ValueError as exc:
        raise OpError(ErrorCode.INVALID, f"work_dir: {exc}") from None


async def _new_project(call: Call) -> str | None:
    """None when the task's project exists; else the question whether to create it (the call is ``high``)."""
    name, project, work_dir = await _placement(call.arguments)
    await _profile_choices(call.arguments.get("profiles"), clearing=False)
    if project is not None:
        return None
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


async def _create(call: Call) -> dict[str, Any]:
    arguments, turn = call.arguments, call.turn
    name, project, work_dir = await _placement(arguments)
    profiles = await _profile_choices(arguments.get("profiles"), clearing=False)
    envelope = turn.envelope
    origin = envelope.metadata.get("origin_channel_id") if envelope is not None else None
    task_id = f"task-{uuid.uuid4().hex[:12]}"
    title = db_tasks.task_title(arguments.get("title"))
    created = project is None
    target = notify_target(origin, sender=turn.sender, chat=turn.chat, group=turn.group)
    row = {
        "task_id": task_id, "requirement": arguments["requirement"].strip(), "goal_id": arguments.get("goal_id") or "",
        "work_dir": work_dir, "status": db_tasks.READY,
        "correlation_id": envelope.correlation_id if envelope is not None else "", "origin_channel_id": origin or "",
        # Who the TaskRouter tells when the task ends: in Telegram the chat, in Lark the sender or the group.
        **(target.columns() if target is not None else {}),
        "original_user_text": (str(envelope.payload.get("text") or "") or None) if envelope is not None else None,
        "title": title,
    }
    if project is not None and not profiles:
        await db_tasks.create_task(**row, project_id=project["id"])
    else:
        try:
            async with db_connection.transaction() as conn:
                if project is None:
                    project = await db_projects.create_project(name=name, work_dir=work_dir, pool=conn)
                await db_tasks.create_task(**row, project_id=project["id"], pool=conn)
                if profiles:
                    await db_layers.set_profile_overrides(conn, task_id, profiles)
        except asyncpg.UniqueViolationError:
            raise OpError(ErrorCode.CONFLICT, f"a project named {name!r} was created meanwhile: call again to use "
                                              "it") from None
    shown = title or arguments.get("goal_id") or task_id
    turn.blocks.append(await configuration_block(task_id, shown, project, work_dir, created))
    return {"task_id": task_id, "name": shown, "status": "ready", "project": project["name"],
            "project_created": created, "work_dir": work_dir,
            "shown_to_user": "Looper adds the task's configuration and the question whether to start it now after "
                             "your reply; do not repeat them."}


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
    task_id: str, name: str | None, project: Mapping[str, Any], work_dir: str, created: bool,
) -> str:
    """The new ready task's configuration, from the settings in effect for it (resolved as the project's agent
    settings view resolves them, with the task's own choices), ending with the question whether to start it."""
    project_id = project["id"]
    lines = [
        "任务已创建，还没有开始（Ready — not started）：",
        f"- 任务：{name}（{task_id}）" if name else f"- 任务：{task_id}",
        f"- 项目：{project['name']}（{'新建' if created else '已有'}）",
        f"- 目录：{work_dir}",
    ]
    limit: int | None = None
    try:
        layers = index_layers(await db_layers.list_layers(project_id, task_id=task_id), project_id)
    except Exception as exc:
        logger.warning("could not read the agent settings of project %s", project_id, exc_info=True)
        lines.append(f"- Agent 设置：暂时读不到（{type(exc).__name__}），可以在项目的 Settings 页查看")
    else:
        lines += [_role_line(role, resolve(role, layers, project_id)) for role in TASK_ROLES]
        limit = resolve_concurrency(AgentType.PLANNER, layers, project_id).value
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
    if limit is not None:
        lines.append(f"- 并发：这个项目同时最多运行 {limit} 个任务")
    return "\n".join([*lines, "", START_QUESTION])


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
    task = await _task(task_id)
    if _group(task) is not db_tasks.TaskGroup.IN_PROGRESS:
        raise OpError(ErrorCode.CONFLICT, f"task {task_id} is {task['status']}: only a running task can be "
                                          "cancelled (stop a queued one)")
    return task


async def _restartable(task_id: str) -> dict[str, Any]:
    task = await _task(task_id)
    if task["status"] not in RESTARTABLE:
        raise OpError(ErrorCode.CONFLICT, f"task {task_id} is {task['status']}: restart and start_over run a stopped, "
                                          "failed or cancelled task again (a ready one starts with action start)")
    if not (task.get("work_dir") or "").strip():
        raise OpError(ErrorCode.INVALID, f"task {task_id} has no directory: set one with looper_task_update first")
    return task


async def _control(call: Call) -> dict[str, Any]:
    task_id, action = call.arguments["task_id"], call.arguments["action"]
    task = await _task(task_id)
    status = task["status"]
    if action == "start":
        if status != db_tasks.READY:
            hint = " (a stopped, failed or cancelled task runs again with action restart)" if status in RESTARTABLE \
                else ""
            raise OpError(ErrorCode.CONFLICT, f"task {task_id} is {status}, not ready: start only starts a task that "
                                              f"was created and never started{hint}")
        from ..projects import start_ready_task
        try:
            await start_ready_task(task_id, call.platform.bus, MANAGER_ADDRESS)
        except RequestError as exc:
            raise _request_error(exc) from None
        limit = await load_concurrency(AgentType.PLANNER, task["project_id"])
        return {"task_id": task_id, "started": True,
                "note": f"its project runs at most {limit} task(s) at once; when full, it waits for one to end"}
    if action == "stop":
        if status not in db_tasks.ACTIVE_STATUSES:
            raise OpError(ErrorCode.CONFLICT, f"task {task_id} is {status}: only a queued or running task can be "
                                              "stopped")
        await _publish(call, MessageType.TASK_STOP, task_id)
        return {"task_id": task_id, "stopped": True}
    if action == "restart":
        await _restartable(task_id)
        await _publish(call, MessageType.TASK_RESTART, task_id, mode=RestartMode.CONTINUE.value)
        return {"task_id": task_id, "restarted": True,
                "note": "it goes on from its saved plan: its accepted steps are kept and do not run again, and its "
                        "Planner decides how the steps that did not finish go on"}
    if action == "start_over":
        await _restartable(task_id)
        await _publish(call, MessageType.TASK_RESTART, task_id, mode=RestartMode.START_OVER.value)
        return {"task_id": task_id, "started_over": True,
                "note": "its steps, plan and Planner session were deleted; it plans anew from its current requirement"}
    await _cancellable(task_id)
    await _publish(call, MessageType.TASK_CANCEL, task_id)
    return {"task_id": task_id, "cancelled": True}


# --- looper_task_instruct ---


async def _instruct(call: Call) -> dict[str, Any]:
    task_id = call.arguments["task_id"]
    task = await _task(task_id)
    if task["status"] == db_tasks.READY:
        raise OpError(ErrorCode.CONFLICT, f"task {task_id} has not started, so no Planner can take a supplement: "
                                          "change its requirement with looper_task_update instead")
    if task["status"] not in db_tasks.ACTIVE_STATUSES:
        raise OpError(ErrorCode.CONFLICT, f"task {task_id} is {task['status']}: only a queued or running task takes "
                                          "a supplement")
    supplement = await db_changes.add_supplement(
        task_id, kept_text(call.arguments["text"], SUPPLEMENTS_MAX_CHARS), via=_via(call.turn))
    if call.platform.task_router.has_planner(task_id):
        await call.platform.bus.publish(MessageEnvelope.create(
            source=MANAGER_ADDRESS, target=planner_address(task_id), message_type=MessageType.TASK_CHANGE,
            payload={"task_id": task_id}))
    return {"task_id": task_id, "supplement_id": supplement,
            "note": "the task's Planner gets it in its next turn and decides whether to change the plan; every later "
                    "Worker and Reviewer sees it too"}


# --- looper_task_update ---


async def _update(call: Call) -> dict[str, Any]:
    arguments = call.arguments
    task_id = arguments["task_id"]
    requirement, work_dir = arguments.get("requirement"), arguments.get("work_dir")
    if requirement is None and work_dir is None and not arguments.get("profiles"):
        raise OpError(ErrorCode.INVALID, "give at least one of requirement, work_dir and profiles")
    task = await _task(task_id)
    if _ended(task):
        raise OpError(ErrorCode.CONFLICT, f"task {task_id} is {task['status']}: an ended task does not change "
                                          "(restart it to run its requirement again)")
    if work_dir is not None:
        if task["status"] != db_tasks.READY:
            raise OpError(ErrorCode.CONFLICT, f"task {task_id} has started: only a task not started can change its "
                                              "directory")
        try:
            work_dir = resolve_work_dir(work_dir)
        except ValueError as exc:
            raise OpError(ErrorCode.INVALID, f"work_dir: {exc}") from None
    changed: list[str] = []
    if arguments.get("profiles"):
        # As the task page sets them: one check and one write (projects.set_task_agents).
        try:
            await set_task_agents(task_id, {"profiles": arguments["profiles"]})
        except RequestError as exc:
            raise _request_error(exc) from None
        changed.append("profiles")
    if work_dir is not None:
        if not await db_tasks.update_task_config(task_id, work_dir=work_dir):
            raise OpError(ErrorCode.CONFLICT, f"task {task_id} started meanwhile: its directory stays")
        changed.append("work_dir")
    result: dict[str, Any] = {"task_id": task_id}
    if requirement is not None:
        try:
            edited = await edit_requirement(task_id, {"requirement": requirement},
                                            task_router=call.platform.task_router, bus=call.platform.bus,
                                            source=MANAGER_ADDRESS, via=_via(call.turn))
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


def _task_argument(arguments: Mapping[str, Any]) -> str | None:
    task_id = arguments.get("task_id")
    return task_id if isinstance(task_id, str) and task_id else None


_TASK_ID = {"type": "string", "minLength": 1, "description": "The task's id (task-...), from looper_tasks."}

TOOLS: tuple[Tool, ...] = (
    Tool(
        name="looper_overview",
        description="What is happening now: task counts by group, the running tasks (with their own concurrency "
                    "limits) and step runs, each active project's load against its limits, today's spend, unhealthy "
                    "components.",
        schema={"type": "object", "properties": {}, "additionalProperties": False},
        risk=Risk.READ, run=_overview,
    ),
    Tool(
        name="looper_tasks",
        description="List or search tasks (newest first), or get one task with its steps, supplements, latest "
                    "progress note, result, cost and concurrency limits.",
        schema={
            "type": "object", "required": ["action"], "additionalProperties": False,
            "properties": {
                "action": {"type": "string", "enum": ["list", "get"]},
                "task_id": {**_TASK_ID, "description": "The task to get (action get)."},
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
                "goal_id": {"type": "string", "description": "A short kebab-case id for the task's goal."},
                "profiles": {**_PROFILES_SCHEMA, "description": "An Agent Profile (id or name) the user chose for "
                                                                "some of the task's roles."},
            },
        },
        risk=Risk.WRITE, run=_create, escalate=_new_project,
    ),
    Tool(
        name="looper_task_control",
        description="start a ready task; stop a queued or running one; restart a stopped, failed or cancelled one "
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
        description="Add what the user said to a queued or running task as a supplement: stored with the task, its "
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
        name="looper_task_update",
        description="Change a task that has not ended: its requirement (kept as a new version; a running task's "
                    "Planner gets the edit in a turn), its directory (a task not started only), or the Agent "
                    "Profile of some of its roles (a Worker or Reviewer from its next step on).",
        schema={
            "type": "object", "required": ["task_id"], "additionalProperties": False,
            "properties": {
                "task_id": _TASK_ID,
                "requirement": {"type": "string", "minLength": 1, "maxLength": TASK_REQUIREMENT_LIMIT,
                                "description": "The whole new requirement."},
                "work_dir": {"type": "string", "minLength": 1, "description": "The new absolute directory."},
                "profiles": _PROFILES_SCHEMA,
            },
        },
        risk=Risk.WRITE, run=_update, task_id=_task_argument,
    ),
)
