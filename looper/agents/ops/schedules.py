"""The Manager's schedules tool (Manager tools RFC, sections 7 and 7.1, T3): list, read, create, change, enable,
disable, run now, reset a stored Planner session and delete schedules.

Each action calls the code the dashboard's API calls (``agents.schedules``:
the Schedules page's list, a schedule's view and its runs, ``create_schedule``,
``update_schedule``, ``run_now`` and ``delete_schedule``), so both check alike
and a refusal carries the dashboard's message. A schedule is named by its id or
its name, ignoring case, within ``project`` when given; a name more than one
project has is refused, naming each. Listing and reading are ``read``; the
changes are ``write``, but for deleting, enabling, running now and a change that
raises or removes a spend cap, and resetting a stored Planner session, which are ``high``: the user is shown what it does
and confirms in the next message.

A new schedule is disabled. Looper adds its configuration (the rule in words,
its next fire times, what it runs and what bounds it) to the reply, with the
question whether to enable it: enabling is the approval (scheduled-tasks RFC
D6), confirmed in the user's next message, so a schedule this turn created is neither enabled nor run in this turn
(``Turn.created``): the user has not seen it yet. A change adds what it
changed, before and after.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

from ...db import connection as db_connection, profile_registry as db_profiles, projects as db_projects, schedules as db_schedules
from ...db import tasks as db_tasks
from .. import cron, schedules
from ..message import is_lark, parse_channel_id
from ..project_execution import format_usd
from ..tiers.journal import clip
from . import Call, ErrorCode, OpError, Risk, Tool
from .config import _PROJECT, _required, answered, project_named

# How many of a schedule's latest runs looper_schedules get shows (the routine skips left out, as on its page).
RUNS_SHOWN = 5
# The schedules a refusal names, at most.
NAMED = 30
OPEN_WORDS = {db_schedules.RUNNING: "任务运行中", db_schedules.GATING: "前置检查中", db_schedules.QUEUED: "排队中"}
# How many next fire times a schedule's configuration shows.
NEXT_SHOWN = 3
ENABLE_QUESTION = "要启用吗？回复「启用」就启用，之后按规则无人值守地运行；也可以稍后在 Schedules 页打开它的开关。"
# A schedule's settings as the user is shown them, in this order; ``fields.profiles`` is the API's profile_overrides.
FIELD_WORDS = {
    "name": "名称", "cron": "时间规则", "timezone": "时区", "work_dir": "目录", "requirement": "需求",
    "overlap": "上一次还在运行时", "max_parallel_runs": "同时运行的上限", "catch_up": "错过的触发",
    "run_timeout_seconds": "运行超时", "gate_command": "前置检查", "gate_timeout_seconds": "前置检查超时",
    "run_budget_usd": "每次运行的花费上限", "daily_budget_usd": "每日花费上限", "notify": "通知",
    "notify_target": "通知对象", "profile_overrides": "Agent Profile", "task_limits": "每次运行的任务并发",
    "template_key": "任务模板", "template_inputs": "模板静态输入",
}
_OVERLAP_WORDS = {"skip": "跳过这次触发", "queue": "排队等它结束（最多一个）", "parallel": "并行运行"}
_NOTIFY_WORDS = {"failures": "失败时", "alerts": "只发告警", "always": "每次运行", "never": "不发"}
# The changes the user confirms first: a spend cap raised, or removed.
CAPS = ("run_budget_usd", "daily_budget_usd")
# A setting's name in the tool's arguments, where it differs from the API's.
_ARGUMENT = {"profile_overrides": "profiles"}
# What a run shows of its task, blanked where the task is another Manager's (``_runs_in_scope``).
_TASK_OF_RUN = {"task_id": None, "task_title": None, "task_status": None, "summary": None, "error": None,
                "cost_usd": None}


def _run_listed(run: Mapping[str, Any]) -> dict[str, Any]:
    return {"run_id": run["run_id"], "status": run["status"], "trigger": run["trigger"],
            "at": run["fired_at"] or run["scheduled_for"], "task_id": run["task_id"],
            "summary": clip(run["summary"] or run["error"] or "", 200) or None, "cost_usd": run["cost_usd"],
            **({"firings": run["firings"]} if run["firings"] > 1 else {})}


def _listed(view: Mapping[str, Any]) -> dict[str, Any]:
    return {"schedule_id": view["schedule_id"], "name": view["name"], "project": view["project_name"],
            "cron": view["cron"], "timezone": view["timezone"], "description": view["description"],
            "enabled": view["enabled"], "next_fire_at": view["next_fire_at"],
            "template_key": view.get("template_key"), "template_name": view.get("template_name"),
            "template_inputs": view.get("template_inputs") or {},
            "last_run": _run_listed(view["last_run"]) if view["last_run"] else None,
            "consecutive_failures": view["consecutive_failures"]}


async def _runs_in_scope(call: Call, runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """``runs`` as the Manager may see them: for a scoped one, a run whose task is not its own keeps its time,
    trigger and status, without its task's id, title, result or cost."""
    if not call.scoped:
        return runs
    owners = await db_tasks.manager_of({run["task_id"] for run in runs if run.get("task_id")})
    return [run if not run.get("task_id") or owners.get(run["task_id"]) == call.manager_id else {**run, **_TASK_OF_RUN}
            for run in runs]


async def _in_scope(call: Call, view: dict[str, Any]) -> dict[str, Any]:
    """A schedule's view as the Manager may see it: its last run as ``_runs_in_scope``, and for a scoped Manager
    none of its tasks' spend, another owner's template selection, nor its Planner session's last task unless
    that task is its own."""
    if not call.scoped:
        return view
    view = {**view, **({"spent_today_usd": None} if "spent_today_usd" in view else {})}
    if view.get("manager_id") != call.manager_id:
        view.update(template_key=None, template_name=None, template_inputs={})
    if view.get("last_run"):
        (view["last_run"],) = await _runs_in_scope(call, [view["last_run"]])
    session = view.get("planner_session")
    if session and session.get("last_task_id") and (
            (await db_tasks.manager_of({session["last_task_id"]})).get(session["last_task_id"]) != call.manager_id):
        view["planner_session"] = {**session, "last_task_id": None, "last_task_title": None}
    return view


async def _schedule(arguments: Mapping[str, Any]) -> dict[str, Any]:
    """The schedule the call names (its row), by id or by name within ``project``; refuses an unknown one, naming
    the schedules there are, and a name several projects have, naming each."""
    _required(arguments, arguments["action"], "schedule")
    wanted = arguments["schedule"].strip()
    project = await project_named(arguments["project"]) if arguments.get("project") is not None else None
    rows = await db_schedules.list_schedules(project["id"] if project else None)
    found = [r for r in rows if r["id"] == wanted] or [r for r in rows if r["name"].casefold() == wanted.casefold()]
    if len(found) > 1:
        named = "; ".join(f"{r['name']} in project {r['project_name']} ({r['id']})" for r in found)
        raise OpError(ErrorCode.INVALID, f"{len(found)} projects have a schedule named {wanted!r}: {named}. Give "
                                         "project, or the schedule's id")
    if not found:
        where = f" in project {project['name']}" if project else ""
        named = ", ".join(f"{r['name']} ({r['project_name']})" for r in rows[:NAMED])
        raise OpError(ErrorCode.NOT_FOUND, f"no schedule {wanted!r}{where}; there are: {named or 'none'}")
    return found[0]


async def _schedules(call: Call) -> dict[str, Any]:
    arguments, action = call.arguments, call.arguments["action"]
    if action == "list":
        views = (await schedules.list_all_schedules())["schedules"]
        if arguments.get("project") is not None:
            project = await project_named(arguments["project"])
            views = [v for v in views if v["project_id"] == project["id"]]
        if arguments.get("enabled") is not None:
            views = [v for v in views if v["enabled"] is arguments["enabled"]]
        return {"schedules": [_listed(await _in_scope(call, v)) for v in views]}
    if action == "create":
        return await _create(call)
    row = await _schedule(arguments)
    _not_created_now(call, row)
    if action == "update":
        return await _update(call, row)
    if action == "reset_planner_session":
        result = await answered(schedules.reset_planner_session(row["id"]))
        return {"schedule": row["name"], "schedule_id": row["id"], "project": row["project_name"], **result,
                "note": "the next run starts a new Planner session; a run in progress keeps its own session "
                        "until it ends and will not write it back"}
    if action == "run":
        if call.platform.scheduler is None:
            raise OpError(ErrorCode.UNAVAILABLE, "this server runs no scheduler; try again later")
        run = (await answered(schedules.run_now(row["id"], call.platform.scheduler)))["run"]
        (run,) = await _runs_in_scope(call, [run])
        return {"schedule": row["name"], "project": row["project_name"], "run": _run_listed(run),
                "note": "it runs once now, through the same overlap check as a fire; its next fire time stays"}
    if action == "get":
        view = await _in_scope(call, (await answered(schedules.get_schedule(row["id"])))["schedule"])
        runs = await answered(schedules.list_runs(row["id"], {"limit": str(RUNS_SHOWN), "skips": "hide"}))
        return {"schedule": {**{k: v for k, v in view.items() if k not in {"last_run", "manager_id", "manager_name"}},
                             "requirement": clip(view["requirement"], 1_000)},
                "latest_runs": [_run_listed(run) for run in await _runs_in_scope(call, runs["runs"])],
                "routine_skips": runs["routine_skips"]}
    if action in ("enable", "disable"):
        enabled = action == "enable"
        answer = await answered(schedules.update_schedule(row["id"], {"enabled": enabled}))
        result = {"schedule": _listed(await _in_scope(call, answer["schedule"]))}
        if row["enabled"] is enabled:
            result["note"] = f"it was {action}d already"
        return result
    await answered(schedules.delete_schedule(row["id"]))
    return {"deleted": row["name"], "schedule_id": row["id"], "project": row["project_name"],
            "note": "the tasks it created stay; its run history was deleted with it"}


def _fields(arguments: Mapping[str, Any], action: str) -> dict[str, Any]:
    """The call's ``fields`` as the API takes them (``profiles`` is its ``profile_overrides``)."""
    _required(arguments, action, "fields")
    data = dict(arguments["fields"])
    if not data:
        raise OpError(ErrorCode.INVALID, f"fields: give the settings to {'set' if action == 'create' else 'change'}")
    if "profiles" in data:
        data["profile_overrides"] = data.pop("profiles")
    return data


def _not_created_now(call: Call, row: Mapping[str, Any]) -> None:
    """Refuses enabling or running a schedule this turn created."""
    action = call.arguments["action"]
    if action in ("enable", "run") and row["id"] in call.turn.created:
        raise OpError(ErrorCode.CONFLICT, f"schedule {row['name']} was created in this turn: Looper shows the user its "
                                          "configuration after your reply, with the question whether to enable it. "
                                          f"Do not {action} it now; do it when the user answers")


async def _create(call: Call) -> dict[str, Any]:
    arguments, turn = call.arguments, call.turn
    _required(arguments, "create", "project")
    project = await project_named(arguments["project"])
    data = _fields(arguments, "create")
    data["manager_id"] = call.manager_id
    bot = parse_channel_id(turn.channel_id)
    if ("notify_target" not in data and (is_lark(turn.channel) or turn.channel == "telegram") and turn.sender
            and (bot is None or bot.builtin)):
        # Made from a Telegram or Lark chat: its messages go to whoever asked, when their channel's allowlist lets them.
        # Schedules notify through the built-in rows only: through an added bot the target stays unset.
        asked = {"channel": turn.channel, "chat": turn.sender}
        if any((t["channel"], t["chat"]) == (asked["channel"], asked["chat"]) for t in await schedules.notify_targets()):
            data["notify_target"] = asked
    view = (await answered(schedules.create_schedule(project["id"], data, created_via="manager")))["schedule"]
    turn.created.add(view["schedule_id"])
    turn.blocks.append("\n".join([
        "定时任务已创建，还没有启用：", *await _configuration(view, project), "", ENABLE_QUESTION]))
    return {"schedule": _listed(view), "enabled": False,
            "shown_to_user": "Looper adds the schedule's configuration and the question whether to enable it after "
                             "your reply; do not repeat them, and enable it only when the user answers."}


async def _update(call: Call, row: Mapping[str, Any]) -> dict[str, Any]:
    before = (await answered(schedules.get_schedule(row["id"])))["schedule"]
    view = (await answered(schedules.update_schedule(row["id"], _fields(call.arguments, "update"))))["schedule"]
    changed = await _changes(await _in_scope(call, before), await _in_scope(call, view))
    call.turn.blocks.append("\n".join([f"已修改定时任务「{view['name']}」（项目 {view['project_name']}）：",
                                       *(changed.values() or ["- 没有变化"]), *_next_line(before, view),
                                       f"它现在{'已启用' if view['enabled'] else '是停用的'}。"]))
    return {"schedule": _listed(await _in_scope(call, view)), "changed": [_ARGUMENT.get(key, key) for key in changed],
            "shown_to_user": "Looper adds what changed, before and after, after your reply; do not repeat it."}


def _raises_a_cap(row: Mapping[str, Any], data: Mapping[str, Any], project_cap: float | None) -> bool:
    """Whether a change raises a spend cap of the schedule, or removes one it has. A run's cap that is null is the
    project's per-task cap (``COALESCE(schedule, project)`` at run time), so that is what it is compared as."""
    for cap in CAPS:
        if cap not in data:
            continue
        before = row[cap] if row[cap] is not None or cap != "run_budget_usd" else project_cap
        if before is None:
            continue
        after = data[cap]
        if after is None and cap == "run_budget_usd":
            after = project_cap
        if after is None or (isinstance(after, (int, float)) and not isinstance(after, bool) and after > before):
            return True
    return False


async def _confirming(call: Call) -> str | None:
    """What deleting, enabling or running a schedule, or a change raising or removing its spend cap, does, for the
    user to confirm; None for any other call (its own tier, and enabling one already enabled)."""
    action = call.arguments["action"]
    if action == "delete":
        return await _deleting(call)
    if action == "reset_planner_session":
        row = await _schedule(call.arguments)
        return "\n".join([
            f"要重置定时任务「{row['name']}」（项目 {row['project_name']}）的 Planner 会话吗？",
            "已存会话会清掉，下一次运行从新会话开始。" if row.get("planner_session_id") else
            "当前没有已存会话；下一次运行从新会话开始。",
            "正在进行的运行留着自己的会话直到结束，并且不会写回。"])
    if action in ("enable", "run"):
        return await _starting(call)
    if action != "update":
        return None
    row = await _schedule(call.arguments)
    data = _fields(call.arguments, "update")
    # Refused as the update would refuse it, before anything is asked.
    fields = (await answered(schedules._fields(data, update=True)))[0]
    gated = bool(fields.get("gate_command", row["gate_command"]))
    try:
        schedules.checked_rule(fields.get("cron", row["cron"]), fields.get("timezone", row["timezone"]),
                               now=datetime.now(timezone.utc), gated=gated)
    except ValueError as exc:
        raise OpError(ErrorCode.INVALID, str(exc)) from None
    project = await db_projects.get_project(row["project_id"])
    if not _raises_a_cap(row, data, project["task_budget_usd"] if project else None):
        return None
    before = (await answered(schedules.get_schedule(row["id"])))["schedule"]
    after = {**before, **{key: data[key] for key in CAPS if key in data}}
    return "\n".join([f"要修改定时任务「{row['name']}」（项目 {row['project_name']}）吗？",
                      *(await _changes(before, after, only=data)).values(), "这会提高（或取消）它的花费上限。"])


async def _starting(call: Call) -> str | None:
    """What enabling a schedule (it then fires unattended) or running it now does, for the user to confirm."""
    row = await _schedule(call.arguments)
    _not_created_now(call, row)
    action = call.arguments["action"]
    if action == "enable" and row["enabled"]:
        return None
    view = await _in_scope(call, (await answered(schedules.get_schedule(row["id"])))["schedule"])
    names = await _profile_names()
    head = f"定时任务「{row['name']}」（项目 {row['project_name']}）"
    lines = [f"- 时间规则：{_shown('cron', view, names)}，时区 {view['timezone']}",
             f"- 需求：{_shown('requirement', view, names)}",
             f"- 花费上限：每次运行 {_shown('run_budget_usd', view, names)}，每天 {_shown('daily_budget_usd', view, names)}"]
    if view.get("template_key"):
        lines.extend(f"- {FIELD_WORDS[key]}：{_shown(key, view, names)}" for key in ("template_key", "template_inputs"))
    if action == "run":
        return "\n".join([f"要现在运行{head}一次吗？它会立刻建出任务并开始执行，花费计入它的上限。", *lines])
    lines.insert(1, f"- 接下来 {NEXT_SHOWN} 次：{_next_fires(view)}")
    return "\n".join([f"要启用{head}吗？启用后它按规则无人值守地建任务运行。", *lines])


async def _changes(before: Mapping[str, Any], after: Mapping[str, Any], *,
                   only: Mapping[str, Any] | None = None) -> dict[str, str]:
    """A line for each setting that differs, before → after, by its key (only those named in ``only`` when
    given)."""
    names = await _profile_names()
    lines = {}
    for key, label in FIELD_WORDS.items():
        if only is not None and key not in only:
            continue
        old, new = _shown(key, before, names), _shown(key, after, names)
        if old != new or (key == "template_inputs" and before.get(key) != after.get(key)):
            lines[key] = f"- {label}：{old} → {new}"
    return lines


def _next_line(before: Mapping[str, Any], view: Mapping[str, Any]) -> list[str]:
    if (before["cron"], before["timezone"]) == (view["cron"], view["timezone"]):
        return []
    return [f"- 接下来 {NEXT_SHOWN} 次：{_next_fires(view)}"]


async def _profile_names() -> dict[str, str]:
    return {row["id"]: row["name"] for row in await db_profiles.list_profiles()}


def _money(value: Any, unset: str) -> str:
    return unset if value is None else f"${format_usd(float(value))}"


def _shown(key: str, view: Mapping[str, Any], names: Mapping[str, str]) -> str:
    """A setting of a schedule's view as the user is shown it."""
    value = view.get(key)
    if key == "template_key":
        return f"{view.get('template_name') or value}（{value}）" if value else "没有"
    if key == "template_inputs":
        return "、".join(f"{k}={json.dumps(clip(v, 200), ensure_ascii=False)}" for k, v in sorted((value or {}).items())) or "没有"
    if key == "cron":
        return f"{value}（{view['description']}）"
    if key == "work_dir":
        return value or "项目的默认目录"
    if key == "requirement":
        return f"{len(value)} 字：{clip(value, 60)}"
    if key == "overlap":
        return _OVERLAP_WORDS.get(value, value)
    if key == "catch_up":
        return "补跑一次" if value == "once" else "跳过"
    if key == "run_timeout_seconds":
        if value == schedules.TIMEOUT_DEFAULT:
            effective = view["run_timeout_effective_seconds"]
            return f"系统默认（{'不限' if effective is None else f'{effective} 秒'}）"
        return "不限" if value is None else f"{value} 秒"
    if key == "gate_timeout_seconds":
        return "不限" if value is None else f"{value} 秒"
    if key == "gate_command":
        return f"`{value}`" if value else "没有"
    if key == "run_budget_usd":
        return _money(value, "项目的每任务上限")
    if key == "daily_budget_usd":
        return _money(value, "不限")
    if key == "notify":
        return _NOTIFY_WORDS.get(value, value)
    if key == "notify_target":
        return f"{'Telegram' if value['channel'] == 'telegram' else '飞书'} · {value['chat']}" if value else "只在控制台"
    if key == "profile_overrides":
        return "、".join(f"{role}：{names.get(pid, pid)}" for role, pid in sorted((value or {}).items())) or "按项目的设置"
    if key == "task_limits":
        set_ = {role: limit for role, limit in (value or {}).items() if limit is not None}
        return "、".join(f"{role} {limit}" for role, limit in sorted(set_.items())) or "按角色的默认值"
    return str(value)


def _next_fires(view: Mapping[str, Any]) -> str:
    rule, tz = cron.parse(view["cron"]), cron.zone(view["timezone"])
    fires = cron.next_fires(rule, tz, datetime.now(timezone.utc), NEXT_SHOWN)
    return "、".join(moment.astimezone(tz).strftime("%Y-%m-%d %H:%M %Z") for moment in fires) or "没有"


async def _configuration(view: Mapping[str, Any], project: Mapping[str, Any]) -> list[str]:
    """A new schedule's configuration, as the user is shown it."""
    names = await _profile_names()
    directory = view["work_dir"] or f"项目的默认目录（{project['work_dir']}）"
    lines = [f"- 定时任务：{view['name']}（{view['schedule_id']}），项目 {view['project_name']}",
             f"- 时间规则：{_shown('cron', view, names)}，时区 {view['timezone']}",
             f"- 接下来 {NEXT_SHOWN} 次：{_next_fires(view)}",
             f"- 目录：{directory}",
             f"- 需求：{_shown('requirement', view, names)}",
             f"- 上一次还在运行时：{_shown('overlap', view, names)}"
             + (f"，最多 {view['max_parallel_runs']} 个同时运行" if view["overlap"] == "parallel" else "")
             + f"；错过的触发：{_shown('catch_up', view, names)}",
             f"- 前置检查：{_shown('gate_command', view, names)}",
             f"- 花费上限：每次运行 {_shown('run_budget_usd', view, names)}，每天 {_shown('daily_budget_usd', view, names)}",
             f"- 通知：{_shown('notify', view, names)} → {_shown('notify_target', view, names)}"]
    lines.append(f"- 运行超时：{_shown('run_timeout_seconds', view, names)}")
    if view.get("template_key"):
        lines.extend(f"- {FIELD_WORDS[key]}：{_shown(key, view, names)}" for key in ("template_key", "template_inputs"))
    if view["profile_overrides"]:
        lines.append(f"- Agent Profile：{_shown('profile_overrides', view, names)}")
    if any(limit is not None for limit in view["task_limits"].values()):
        lines.append(f"- 每次运行的任务并发：{_shown('task_limits', view, names)}")
    return lines


async def _deleting(call: Call) -> str | None:
    """What deleting a schedule does, for the user to confirm."""
    row = await _schedule(call.arguments)
    running = await db_schedules.open_runs(db_connection.get_pool(), row["id"])
    lines = [f"要删除定时任务「{row['name']}」（项目 {row['project_name']}）吗？",
             f"- 时间规则：{row['cron']}（{schedules.describe(row['cron'], row['timezone'])}），时区 {row['timezone']}",
             f"- 状态：{'已启用' if row['enabled'] else '已停用'}"]
    if running:
        states = "、".join(OPEN_WORDS.get(run["status"], run["status"]) for run in running)
        lines.append(f"- 正在进行的运行：{len(running)} 次（{states}）。运行中的任务会继续，照常结束，之后不再记入这个"
                     "定时任务，也不发通知；排队中和前置检查中的不会再开始任务。")
    else:
        lines.append("- 正在进行的运行：没有")
    lines.append("它建出的任务都保留；它的运行记录和通知记录会一起删除，不能恢复。")
    return "\n".join(lines)


_FIELDS = {
    "type": "object",
    "description": "create, update (only those to change; null clears one).",
    "properties": {
        "name": {"type": "string"}, "cron": {"type": "string", "description": "5 fields."},
        "timezone": {"type": "string", "description": "IANA."}, "requirement": {"type": "string"},
        "work_dir": {"type": "string", "description": "'' for the project's."},
        "overlap": {"type": "string", "enum": [o.value for o in schedules.Overlap]},
        "max_parallel_runs": {"type": "integer"},
        "catch_up": {"type": "string", "enum": [c.value for c in schedules.CatchUp]},
        "run_timeout_seconds": {"description": "Seconds; null: no limit; 'default' (a new schedule's): the system "
                                               "default."},
        "run_budget_usd": {"type": "number"},
        "daily_budget_usd": {"type": "number"},
        "gate_command": {"type": "string", "description": "Shell; prints each run's input, nothing skips it."},
        "gate_timeout_seconds": {"type": "integer"},
        "notify": {"type": "string", "enum": [n.value for n in schedules.Notify]},
        "notify_target": {"type": "object", "description": "{channel, chat}; null: dashboard only."},
        "profiles": {"type": "object", "description": "{planner, worker, reviewer}: Agent Profile (id or name) per role; a role left out keeps the project's. Set only what the user asks for: a repetitive schedule's Planner may be a cheaper profile."},
        "task_limits": {"type": "object", "description": "{worker, reviewer}: each run's task's own limits."},
        "template_key": {"type": ["string", "null"], "description": "This schedule owner's template key; omitted: "
                         "keep selection; a new key resets inherited inputs; null clears selection and inputs."},
        "template_inputs": {"type": "object", "additionalProperties": {"type": "string"},
                            "description": "Static string values by template input key; omitted: keep inputs "
                                           "unless changing template_key. Supplied values replace all inputs."},
    },
}

TOOLS: tuple[Tool, ...] = (
    Tool(
        name="looper_schedules",
        description="Schedules (a project's task on a time rule): list; get (with latest runs); create (disabled; "
                    "Looper shows it and asks whether to enable it); update; enable (its runs then start tasks "
                    "unattended; the user confirms first) or disable; run now (the user confirms first); delete "
                    "(the user confirms first; its tasks stay); reset_planner_session (the user confirms first; "
                    "the next run starts fresh; a run in progress keeps its session and will not write it back).",
        schema={
            "type": "object", "required": ["action"], "additionalProperties": False,
            "properties": {
                "action": {"type": "string",
                           "enum": ["list", "get", "create", "update", "enable", "disable", "run", "delete",
                                    "reset_planner_session"]},
                "schedule": {"type": "string", "minLength": 1, "description": "Its id (sched-...) or name."},
                "project": {**_PROJECT, "description": "create: its project; list: only its schedules; with a "
                                                       "schedule's name: its project."},
                "enabled": {"type": "boolean", "description": "list: only those enabled (true) or not."},
                "fields": _FIELDS,
            },
        },
        risk=Risk.WRITE, run=_schedules, escalate=_confirming, read_actions=frozenset({"list", "get"}),
    ),
)
