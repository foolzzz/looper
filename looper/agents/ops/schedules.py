"""The Manager's schedules tool (Manager tools RFC, section 7, T3): list, read, enable, disable and delete schedules.

Each action calls the code the dashboard's API calls (``agents.schedules``:
the Schedules page's list, a schedule's view and its runs, ``update_schedule``
and ``delete_schedule``), so both check alike and a refusal carries the
dashboard's message. A schedule is named by its id or its name, ignoring case,
within ``project`` when given; a name more than one project has is refused,
naming each. Listing and reading are ``read``, enabling and disabling
``write``; deleting is ``high``: the user is shown the schedule, whether a run
of it is in progress and what goes with it, and confirms first.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ...db import connection as db_connection, schedules as db_schedules
from .. import schedules
from ..tiers.journal import clip
from . import Call, ErrorCode, OpError, Risk, Tool
from .config import _PROJECT, _required, answered, project_named

# How many of a schedule's latest runs looper_schedules get shows (the routine skips left out, as on its page).
RUNS_SHOWN = 5
# The schedules a refusal names, at most.
NAMED = 30
OPEN_WORDS = {db_schedules.RUNNING: "任务运行中", db_schedules.GATING: "前置检查中", db_schedules.QUEUED: "排队中"}


def _run_listed(run: Mapping[str, Any]) -> dict[str, Any]:
    return {"run_id": run["run_id"], "status": run["status"], "trigger": run["trigger"],
            "at": run["fired_at"] or run["scheduled_for"], "task_id": run["task_id"],
            "summary": clip(run["summary"] or run["error"] or "", 200) or None, "cost_usd": run["cost_usd"],
            **({"firings": run["firings"]} if run["firings"] > 1 else {})}


def _listed(view: Mapping[str, Any]) -> dict[str, Any]:
    return {"schedule_id": view["schedule_id"], "name": view["name"], "project": view["project_name"],
            "cron": view["cron"], "timezone": view["timezone"], "description": view["description"],
            "enabled": view["enabled"], "next_fire_at": view["next_fire_at"],
            "last_run": _run_listed(view["last_run"]) if view["last_run"] else None,
            "consecutive_failures": view["consecutive_failures"]}


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
        return {"schedules": [_listed(v) for v in views]}
    row = await _schedule(arguments)
    if action == "get":
        view = (await answered(schedules.get_schedule(row["id"])))["schedule"]
        runs = await answered(schedules.list_runs(row["id"], {"limit": str(RUNS_SHOWN), "skips": "hide"}))
        return {"schedule": {**{k: v for k, v in view.items() if k != "last_run"},
                             "requirement": clip(view["requirement"], 1_000)},
                "latest_runs": [_run_listed(run) for run in runs["runs"]],
                "routine_skips": runs["routine_skips"]}
    if action in ("enable", "disable"):
        enabled = action == "enable"
        answer = await answered(schedules.update_schedule(row["id"], {"enabled": enabled}))
        result = {"schedule": _listed(answer["schedule"])}
        if row["enabled"] is enabled:
            result["note"] = f"it was {action}d already"
        return result
    await answered(schedules.delete_schedule(row["id"]))
    return {"deleted": row["name"], "schedule_id": row["id"], "project": row["project_name"],
            "note": "the tasks it created stay; its run history was deleted with it"}


async def _deleting(call: Call) -> str | None:
    """What deleting a schedule does, for the user to confirm; None for the other actions."""
    if call.arguments["action"] != "delete":
        return None
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


TOOLS: tuple[Tool, ...] = (
    Tool(
        name="looper_schedules",
        description="Schedules (a project's task fired on a time rule): list; get one with its latest runs; enable "
                    "(its runs then start tasks unattended) or disable; delete (the user confirms first; its tasks "
                    "stay). Creating or editing one: the project's Schedules tab.",
        schema={
            "type": "object", "required": ["action"], "additionalProperties": False,
            "properties": {
                "action": {"type": "string", "enum": ["list", "get", "enable", "disable", "delete"]},
                "schedule": {"type": "string", "minLength": 1,
                             "description": "Its id (sched-...) or name (get, enable, disable, delete)."},
                "project": {**_PROJECT, "description": "list: only its schedules; with a schedule's name: its "
                                                       "project."},
                "enabled": {"type": "boolean", "description": "list: only the enabled (true) or disabled ones."},
            },
        },
        risk=Risk.WRITE, run=_schedules, escalate=_deleting, read_actions=frozenset({"list", "get"}),
    ),
)
