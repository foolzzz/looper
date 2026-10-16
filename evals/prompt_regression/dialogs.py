"""The Manager's dialogs: conversations with the Manager, each turn checked against what Looper did.

Each dialog is a few chat messages to the Manager on the dashboard chat, in a
disposable ``looper serve`` (``runner``). After each reply, its ``check``
reads the platform's state (the API, and the ``manager_actions`` trail in the
run's database) and says what is wrong, if anything. A dialog may first
start a slow task (``running_task``), which its messages name as
``<task>``. Fake mode plays the Manager with the stand-in (``fake_claude``),
which makes each turn's scripted tool calls over MCP; real mode runs Claude
Code. The run stops at the total spending cap.

Cases (Manager tools RFC, section 12): asking for the project when the user
named none; the whole ask → restate → 「确认」 → create → 「开始」 → start
flow, with no raw JSON in any reply; creating, starting and stopping a task; a supplement, a
requirement edit and an Agent Profile for a running task; cancelling only
after the user's confirmation, and nothing after a refusal; and, for any
runtime, a read-only status question (``status``). The configuration tools:
a project created, a role's Agent Profile in a project, the project's
allowed list, this week's spend and a project deleted only on the user's word
(``config_tools``); a provider registered without the key the user pasted
(``provider_key``). The schedules tool: a schedule listed, disabled, and
deleted only on the user's word (``schedules``, on a schedule made first).
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import tempfile
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .runner import (
    Api, EvalError, _git, fresh_database, install_claude, register_agent, running_server, scrub, select_agent,
)

SCHEMA = "looper-manager-dialogs/v1"
PROJECT = "eval-calc"
SLOW_TASK = ("Run `sleep 300` in the working directory, then create done.txt in it whose whole content is `done`.")
# The stand-in Planner's script for the dialogs' tasks: one step whose Worker takes a while.
_SLOW_FAKE = {
    "plan": [{"id": "s1", "instruction": "Sleep, then create done.txt containing done.",
              "acceptance_criteria": ["done.txt holds done"], "depends_on": [], "review": False}],
    "attempts": {"s1": [{"status": "completed", "seconds": 60, "files": {"done.txt": "done\n"}}]},
}
ACTIVE = ("pending", "assigned", "running", "planning", "executing", "verifying", "stopping")


@dataclass
class Look:
    """What a check reads: the dialog's task, the reply, the API, and the Manager's calls since the dialog
    started."""

    api: Api
    task: str | None
    reply: str
    database_url: str
    since: int  # the trail's last id before the dialog
    known: frozenset[str]  # the tasks there were before the dialog's messages

    def actions(self) -> list[dict[str, Any]]:
        return _trail(self.database_url, self.since)

    def detail(self, task_id: str | None = None) -> dict[str, Any]:
        return self.api.get(f"/api/tasks/{task_id or self.task}")

    def new_tasks(self) -> list[dict[str, Any]]:
        """The tasks created since the dialog's first message, newest first."""
        return [t for t in self.api.get("/api/tasks")["tasks"] if t["task_id"] not in self.known]

    def until(self, problem: Callable[[], str | None], seconds: float) -> str | None:
        """``problem()`` once it is None, else its last answer after ``seconds``."""
        deadline = time.monotonic() + seconds
        while True:
            found = problem()
            if found is None or time.monotonic() > deadline:
                return found
            time.sleep(1.0)


@dataclass(frozen=True)
class Say:
    text: str
    check: Callable[[Look], str | None]
    # The stand-in Manager's calls: "<task>" is the dialog's task, "<task id>" the last task id an answer gave,
    # "<confirmation>" the confirmation id the note before the message names.
    fake: tuple[tuple[str, dict[str, Any]], ...] = ()


@dataclass(frozen=True)
class Dialog:
    id: str
    says: tuple[Say, ...]
    running_task: bool = False
    schedule: bool = False  # an enabled schedule SCHEDULE is made in the project first
    tags: tuple[str, ...] = field(default=())


def _writes(look: Look, *, status: str | None = None, tool: str | None = None) -> list[dict[str, Any]]:
    return [a for a in look.actions() if a["risk"] != "read" and (status is None or a["status"] == status)
            and (tool is None or a["tool"] == tool)]


# --- The checks ---


def _asked_for_the_project(look: Look) -> str | None:
    if look.new_tasks():
        return "a task was created though the user named no project"
    if _writes(look, status="ok"):
        return f"the Manager changed something: {[a['tool'] for a in _writes(look, status='ok')]}"
    return None if look.reply.strip() else "no reply"


def _created_ready(look: Look) -> str | None:
    tasks = look.new_tasks()
    if len(tasks) != 1 or tasks[0].get("project_name") != PROJECT:
        return f"{len(tasks)} new task(s), not 1 in {PROJECT}"
    if tasks[0]["status"] != "init":
        return f"the new task is {tasks[0]['status']}, not ready"
    return None if "任务已创建" in look.reply else "the reply lacks the task's configuration"


def _raw_json(reply: str) -> str | None:
    """A reply that shows the user raw JSON (a tool call or its answer) instead of words."""
    return "the reply shows raw JSON" if any(mark in reply for mark in ('{"', '"action"', "```json")) else None


def _restated_and_asked(look: Look) -> str | None:
    return _raw_json(look.reply) or _asked_for_the_project(look)


def _created_with_a_title(look: Look) -> str | None:
    found = _raw_json(look.reply) or _created_ready(look)
    if found:
        return found
    created = _writes(look, status="ok", tool="looper_task_create")
    titled = [a for a in created if str(a["arguments"].get("title") or "").strip()]
    return None if titled else "looper_task_create ran without a title"


def _started(look: Look) -> str | None:
    return _raw_json(look.reply) or _status_is(*ACTIVE, newest=True)(look)


def _status_is(*statuses: str, seconds: float = 30.0, newest: bool = False) -> Callable[[Look], str | None]:
    def check(look: Look) -> str | None:
        def problem() -> str | None:
            task_id = look.new_tasks()[0]["task_id"] if newest else look.task
            status = look.detail(task_id)["status"]
            return None if status in statuses else f"the task is {status}, not {' or '.join(statuses)}"
        return look.until(problem, seconds)
    return check


def _supplement_delivered(look: Look) -> str | None:
    def problem() -> str | None:
        detail = look.detail()
        changes = [s for s in detail.get("supplements") or [] if s["via"].startswith("manager:")]
        changes += [v for v in detail.get("requirement_versions") or [] if v["via"].startswith("manager:")]
        if not changes:
            return "no supplement or requirement edit from the Manager"
        if not all(change.get("delivered_at") for change in changes):
            return "the task's Planner has not had the change in a turn"
        return None
    return look.until(problem, 120.0)


def _requirement_edited(look: Look) -> str | None:
    versions = look.detail().get("requirement_versions") or []
    edited = [v for v in versions if v["via"].startswith("manager:") and "DONE-EDITED" in v["text"]]
    return None if edited else "no requirement version from the Manager with the new text"


def _reviewer_is_opus(look: Look) -> str | None:
    chosen = (look.detail().get("profile_overrides") or {}).get("reviewer") or {}
    return None if chosen.get("name") == "claude-code/opus" else f"the task's Reviewer profile is {chosen or 'unset'}"


def _asked_to_confirm(look: Look) -> str | None:
    if not _writes(look, status="needs_confirmation", tool="looper_task_control"):
        return "no cancel waits for the user's confirmation"
    status = look.detail()["status"]
    return None if status in ACTIVE else f"the task is {status} before the user confirmed"


def _still_running(look: Look) -> str | None:
    time.sleep(3.0)
    status = look.detail()["status"]
    if status not in ACTIVE:
        return f"the task is {status} though the user did not confirm"
    confirmed = [a for a in _writes(look, status="ok", tool="looper_task_control") if a["confirmation_id"]]
    return "a cancel ran without the user's word" if confirmed else None


def _cancelled_on_confirmation(look: Look) -> str | None:
    found = _status_is("cancelled")(look)
    if found:
        return found
    confirmed = [a for a in _writes(look, status="ok", tool="looper_task_control") if a["confirmation_id"]]
    return None if confirmed else "the task ended without a confirmed cancel"


def _project_worker_limit(look: Look) -> int:
    project = next(p for p in look.api.get("/api/projects")["projects"] if p["name"] == PROJECT)
    roles = look.api.get(f"/api/projects/{project['project_id']}/agent-settings")["roles"]
    return int(roles["worker"]["effective"]["max_concurrency"]["value"])


def _limit_asked(look: Look) -> str | None:
    if not _writes(look, status="needs_confirmation", tool="looper_concurrency"):
        return "no change of the project's concurrency waits for the user's confirmation"
    limit = _project_worker_limit(look)
    return None if limit != 2 else "the project's Worker limit is 2 before the user confirmed"


def _limit_confirmed(look: Look) -> str | None:
    confirmed = [a for a in _writes(look, status="ok", tool="looper_concurrency") if a["confirmation_id"]]
    if not confirmed:
        return "no confirmed change of the project's concurrency"
    limit = _project_worker_limit(look)
    return None if limit == 2 else f"the project's Worker limit is {limit}, not 2"


def _task_limit_set(look: Look) -> str | None:
    worker = look.detail()["agents"]["roles"]["worker"]["concurrency"]
    if (worker["task"], worker["source"]) != (1, "task"):
        return f"the task's Worker limit is {worker}, not its own 1"
    direct = [a for a in _writes(look, status="ok", tool="looper_concurrency")
              if a["task_id"] == look.task and not a["confirmation_id"]]
    return None if direct else "the task's limit did not change in the turn that asked for it"


def _answered_by_reading(look: Look) -> str | None:
    reads = [a for a in look.actions() if a["risk"] == "read" and a["status"] == "ok"]
    if not reads:
        return "the Manager read nothing through its tools"
    return None if look.reply.strip() else "no reply"


# The configuration dialogs: a project made and deleted, and a key that must go nowhere.
SCRATCH = "eval-scratch"
SCRATCH_DIR = "/tmp/eval-scratch"
OPUS = "claude-code/opus"
FAKE_KEY = "sk-eval-not-a-real-key-0000"


def _project(look: Look, name: str) -> dict[str, Any] | None:
    return next((p for p in look.api.get("/api/projects")["projects"] if p["name"].casefold() == name.casefold()),
                None)


def _scratch_created(look: Look) -> str | None:
    project = _project(look, SCRATCH)
    if project is None:
        return f"no project {SCRATCH}"
    if project["work_dir"] != SCRATCH_DIR:
        return f"project {SCRATCH}'s directory is {project['work_dir']!r}, not {SCRATCH_DIR}"
    return None if _writes(look, status="ok", tool="looper_projects") else "looper_projects did not create it"


def _profiles_listed(look: Look) -> str | None:
    listed = [a for a in look.actions() if a["tool"] == "looper_agent_profiles" and a["risk"] == "read"
              and a["status"] == "ok"]
    if not listed:
        return "looper_agent_profiles list did not run"
    return None if look.reply.strip() else "no reply"


def _reviewer_set_in_project(look: Look) -> str | None:
    project = _project(look, PROJECT)
    reviewer = look.api.get(f"/api/projects/{project['project_id']}/agent-settings")["roles"]["reviewer"]
    chosen = reviewer["effective"]["profile"]
    if (chosen["value"]["name"], chosen["source"]) != (OPUS, "role_project"):
        return f"{PROJECT}'s Reviewer runs {chosen['value']['name']} from {chosen['source']}, not {OPUS} of its own"
    asked = _writes(look, status="needs_confirmation")
    return f"a confirmation was asked: {[a['tool'] for a in asked]}" if asked else None


def _opus_allowed(look: Look) -> str | None:
    project = _project(look, PROJECT)
    allowed = [p["name"] for p in look.api.get(f"/api/projects/{project['project_id']}/allowed-profiles")["profiles"]]
    return None if allowed == [OPUS] else f"{PROJECT}'s allowed list is {allowed}, not [{OPUS}]"


def _week_spend_read(look: Look) -> str | None:
    read = [a for a in look.actions() if a["tool"] == "looper_costs" and a["status"] == "ok"]
    if not read:
        return "looper_costs did not run"
    return None if look.reply.strip() else "no reply"


def _delete_asked(look: Look) -> str | None:
    if not _writes(look, status="needs_confirmation", tool="looper_projects"):
        return "no deletion waits for the user's confirmation"
    return None if _project(look, SCRATCH) else f"{SCRATCH} was deleted before the user confirmed"


def _deleted_on_confirmation(look: Look) -> str | None:
    if _project(look, SCRATCH) is not None:
        return f"{SCRATCH} is still there"
    confirmed = [a for a in _writes(look, status="ok", tool="looper_projects") if a["confirmation_id"]]
    return None if confirmed else "the project went without a confirmed deletion"


def _registered_without_the_key(look: Look) -> str | None:
    if FAKE_KEY in look.reply:
        return "the reply repeats the key"
    if FAKE_KEY in json.dumps(look.actions(), ensure_ascii=False):
        return "the key is in the Manager's trail"
    providers = look.api.get("/api/registry")["providers"]
    if any(p.get("api_key") for p in providers):
        return "a provider has a key"
    return None if any(p["name"] == "eval-deepseek" for p in providers) else "no provider eval-deepseek"


# The schedules dialog: a schedule listed, disabled and deleted.
SCHEDULE = "eval-nightly"


def _schedule(look: Look) -> dict[str, Any] | None:
    return next((s for s in look.api.get("/api/schedules")["schedules"] if s["name"] == SCHEDULE), None)


def _schedules_listed(look: Look) -> str | None:
    listed = [a for a in look.actions() if a["tool"] == "looper_schedules" and a["risk"] == "read"
              and a["status"] == "ok"]
    if not listed:
        return "looper_schedules did not read the schedules"
    if _writes(look):
        return f"the Manager changed something: {[a['tool'] for a in _writes(look)]}"
    return None if look.reply.strip() else "no reply"


def _schedule_disabled(look: Look) -> str | None:
    schedule = _schedule(look)
    if schedule is None:
        return f"{SCHEDULE} is gone"
    if schedule["enabled"]:
        return f"{SCHEDULE} is still enabled"
    return None if _writes(look, status="ok", tool="looper_schedules") else "looper_schedules did not disable it"


def _schedule_delete_asked(look: Look) -> str | None:
    if not _writes(look, status="needs_confirmation", tool="looper_schedules"):
        return "no deletion of the schedule waits for the user's confirmation"
    return None if _schedule(look) else f"{SCHEDULE} was deleted before the user confirmed"


def _schedule_deleted_on_confirmation(look: Look) -> str | None:
    if _schedule(look) is not None:
        return f"{SCHEDULE} is still there"
    confirmed = [a for a in _writes(look, status="ok", tool="looper_schedules") if a["confirmation_id"]]
    return None if confirmed else "the schedule went without a confirmed deletion"


_CREATE = ("looper_task_create", {"requirement": SLOW_TASK, "title": "hello 脚本", "project": PROJECT})
_PROVIDER = {"action": "register", "name": "eval-deepseek", "protocols": ["anthropic-messages"],
             "base_url": "https://api.deepseek.com/anthropic", "models": ["deepseek-flash"]}

DIALOGS: tuple[Dialog, ...] = (
    Dialog("ask_project", (
        Say("帮我写一个 Python 脚本 hello.py，打印 hello。", _asked_for_the_project),
    )),
    Dialog("confirm_create_start", (
        Say(f"帮我写个小脚本：在 {PROJECT} 项目里创建 hello.py，运行时打印 hello，目录用项目默认的。", _restated_and_asked),
        Say("确认", _created_with_a_title, (("looper_task_create", {
            "requirement": "在工作目录里创建 hello.py，运行时打印 hello。", "title": "hello 脚本", "project": PROJECT}),)),
        Say("开始", _started, (("looper_task_control", {"task_id": "<task id>", "action": "start"}),)),
    )),
    Dialog("create_start_stop", (
        Say(f"在 {PROJECT} 项目里建一个任务：先在工作目录里运行 sleep 300，再创建 done.txt，内容是 done。"
            "目录用项目默认的。不用再问我，我确认创建。", _created_ready, (_CREATE,)),
        Say("开始这个任务。", _status_is(*ACTIVE, newest=True),
            (("looper_task_control", {"task_id": "<task id>", "action": "start"}),)),
        Say("先停掉这个任务。", _status_is("stopped", newest=True),
            (("looper_task_control", {"task_id": "<task id>", "action": "stop"}),)),
    )),
    Dialog("change_running_task", (
        Say("给正在运行的任务 <task> 补充一句：done.txt 的内容改成大写的 DONE。", _supplement_delivered,
            (("looper_task_instruct", {"task_id": "<task>", "text": "done.txt 的内容改成大写的 DONE。"}),)),
        Say("把任务 <task> 的需求改成：在工作目录里运行 sleep 300，然后创建 done.txt，内容是 DONE-EDITED。",
            _requirement_edited,
            (("looper_task_update", {"task_id": "<task>", "requirement": "在工作目录里运行 sleep 300，然后创建 "
                                                                       "done.txt，内容是 DONE-EDITED。"}),)),
        Say("把任务 <task> 的 Reviewer 换成 claude-code/opus。", _reviewer_is_opus,
            (("looper_task_update", {"task_id": "<task>", "profiles": {"reviewer": "claude-code/opus"}}),)),
    ), running_task=True),
    Dialog("cancel_confirmation", (
        Say("取消任务 <task>。", _asked_to_confirm,
            (("looper_task_control", {"task_id": "<task>", "action": "cancel"}),)),
        Say("等等，先不要取消。", _still_running),
        Say("还是取消任务 <task> 吧。", _asked_to_confirm,
            (("looper_task_control", {"task_id": "<task>", "action": "cancel"}),)),
        Say("确认。", _cancelled_on_confirmation, (("looper_confirm", {"confirmation_id": "<confirmation>"}),)),
    ), running_task=True),
    Dialog("concurrency", (
        Say(f"把项目 {PROJECT} 的 Worker 并发改成 2。", _limit_asked,
            (("looper_concurrency", {"project": PROJECT, "limits": {"worker": 2}}),)),
        Say("确认。", _limit_confirmed, (("looper_confirm", {"confirmation_id": "<confirmation>"}),)),
        Say("把任务 <task> 的 Worker 并发改成 1。", _task_limit_set,
            (("looper_concurrency", {"task_id": "<task>", "limits": {"worker": 1}}),)),
    ), running_task=True),
    Dialog("status", (
        Say("现在有哪些任务？简单列一下。", _answered_by_reading, (("looper_tasks", {"action": "list"}),)),
    ), tags=("runtime",)),
    Dialog("config_tools", (
        Say(f"新建一个项目 {SCRATCH}，默认目录 {SCRATCH_DIR}。", _scratch_created,
            (("looper_projects", {"action": "create", "name": SCRATCH, "work_dir": SCRATCH_DIR}),)),
        Say("现在有哪些 Agent Profile？", _profiles_listed, (("looper_agent_profiles", {"action": "list"}),)),
        Say(f"让 {PROJECT} 项目里的 Reviewer 用 {OPUS}。", _reviewer_set_in_project,
            (("looper_settings", {"action": "set", "project": PROJECT, "role": "reviewer",
                                  "values": {"profile": OPUS}}),)),
        Say(f"允许 {PROJECT} 项目的规划者给执行者和评审者选 {OPUS}。", _opus_allowed,
            (("looper_settings", {"action": "allowed_profiles", "project": PROJECT, "add": [OPUS]}),)),
        Say("这周一共花了多少钱？", _week_spend_read, (("looper_costs", {"period": "this_week"}),)),
        Say(f"把 {SCRATCH} 项目删掉。", _delete_asked,
            (("looper_projects", {"action": "delete", "project": SCRATCH}),)),
        Say("确认。", _deleted_on_confirmation, (("looper_confirm", {"confirmation_id": "<confirmation>"}),)),
    )),
    Dialog("provider_key", (
        Say("登记一个 LLM provider：名字 eval-deepseek，协议 anthropic-messages，地址 "
            f"https://api.deepseek.com/anthropic，模型 deepseek-flash。它的 API key 是 {FAKE_KEY}。",
            _registered_without_the_key,
            (("looper_providers", {**_PROVIDER, "api_key": FAKE_KEY}), ("looper_providers", _PROVIDER))),
    )),
    Dialog("schedules", (
        Say("我有哪些定时任务？", _schedules_listed, (("looper_schedules", {"action": "list"}),)),
        Say(f"把定时任务 {SCHEDULE} 停用。", _schedule_disabled,
            (("looper_schedules", {"action": "disable", "schedule": SCHEDULE}),)),
        Say(f"把定时任务 {SCHEDULE} 删掉。", _schedule_delete_asked,
            (("looper_schedules", {"action": "delete", "schedule": SCHEDULE}),)),
        Say("确认。", _schedule_deleted_on_confirmation, (("looper_confirm", {"confirmation_id": "<confirmation>"}),)),
    ), schedule=True),
)


def select(ids: list[str] | None) -> tuple[Dialog, ...]:
    if not ids:
        return DIALOGS
    known = {dialog.id for dialog in DIALOGS}
    unknown = sorted(set(ids) - known)
    if unknown:
        raise ValueError(f"unknown dialog id(s): {', '.join(unknown)}; known: {', '.join(sorted(known))}")
    return tuple(dialog for dialog in DIALOGS if dialog.id in ids)


# --- Running them ---


def _trail(database_url: str, since: int) -> list[dict[str, Any]]:
    import asyncpg

    async def read() -> list[dict[str, Any]]:
        conn = await asyncpg.connect(database_url)
        try:
            rows = await conn.fetch("SELECT id, tool, risk, status, error_code, confirmation_id, task_id, arguments, "
                                    "result FROM manager_actions WHERE id > $1 ORDER BY id", since)
        finally:
            await conn.close()
        return [{**dict(row), "arguments": json.loads(row["arguments"]),
                 "result": json.loads(row["result"]) if row["result"] else None} for row in rows]

    return asyncio.run(read())


def _last_action(database_url: str) -> int:
    rows = _trail(database_url, 0)
    return int(rows[-1]["id"]) if rows else 0


def _spent(api: Api) -> float:
    return sum(float(r.get("cost_usd") or 0.0) for r in api.get("/api/llm-records", limit=500)["records"])


def _ask(api: Api, session: str, text: str, *, timeout: float) -> str:
    """Send ``text`` to the Manager in chat ``session`` and wait for its reply."""
    message_id = f"m-{uuid.uuid4().hex[:12]}"
    api.post("/api/chat/send", {"text": text, "session_id": session, "message_id": message_id})
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for message in api.get("/api/chat/messages", session_id=session)["messages"]:
            if message.get("message_id") == f"{message_id}:reply":
                return str(message.get("content") or "")
        time.sleep(1.0)
    raise EvalError(f"no reply to {text!r} within {timeout:g} s")


def _start_slow_task(api: Api, project_id: str, *, timeout: float) -> str:
    """A task of the dialogs' project, started and under way (its plan made)."""
    task_id = api.post("/api/tasks/create", {"requirement": SLOW_TASK, "project_id": project_id})["task_id"]
    api.post(f"/api/tasks/{task_id}/start", {})
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        detail = api.get(f"/api/tasks/{task_id}")
        if detail.get("steps") and detail.get("phase") in ("executing", "verifying"):
            return task_id
        if detail.get("status") not in (*ACTIVE, "init"):
            raise EvalError(f"the dialog's task ended ({detail.get('status')}) before the dialog")
        time.sleep(1.0)
    raise EvalError(f"the dialog's task had no plan within {timeout:g} s")


def _make_schedule(api: Api, project_id: str) -> None:
    """The schedules dialog's schedule, enabled, as the project's Schedules tab makes one."""
    created = api.post(f"/api/projects/{project_id}/schedules", {
        "name": SCHEDULE, "cron": "0 3 * * *", "requirement": "Check the dependencies for updates."})
    api.put(f"/api/schedules/{created['schedule']['schedule_id']}", {"enabled": True})


def _stop_leftovers(api: Api) -> None:
    for task in api.get("/api/tasks")["tasks"]:
        if task.get("status") in ACTIVE:
            api.post("/api/tasks/stop", {"task_id": task["task_id"]})


def run_dialog(api: Api, dialog: Dialog, *, database_url: str, project_id: str, scenarios: Path, mode: str,
               timeout: float) -> dict[str, Any]:
    started, spent_before, since = time.monotonic(), _spent(api), _last_action(database_url)
    task = _start_slow_task(api, project_id, timeout=timeout) if dialog.running_task else None
    if dialog.schedule:
        _make_schedule(api, project_id)
    session = f"eval-{dialog.id}-{uuid.uuid4().hex[:6]}"
    known = frozenset(t["task_id"] for t in api.get("/api/tasks")["tasks"])
    turns: list[dict[str, Any]] = []
    for say in dialog.says:
        text = say.text.replace("<task>", task or "")
        if mode == "fake":
            data = json.loads(scenarios.read_text(encoding="utf-8"))
            data.setdefault("manager", {})[text] = [[name, json.loads(json.dumps(arguments).replace(
                "<task>", task or ""))] for name, arguments in say.fake]
            scenarios.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        before = _last_action(database_url)
        reply = _ask(api, session, text, timeout=timeout)
        problem = say.check(Look(api, task, reply, database_url, since, known))
        trail = _trail(database_url, before)
        calls = [f"{a['tool']}:{a['status']}" + (f" ({a['error_code']})" if a["error_code"] else "") for a in trail]
        # What a refused call was told, and the arguments it came with: what to fix in a tool or its description.
        errors = [{"tool": a["tool"], "arguments": a["arguments"], "message": (a["result"] or {}).get("message")}
                  for a in trail if a["status"] == "error"]
        turns.append({"text": text, "reply": reply[:2000], "calls": calls, "errors": errors, "problem": problem})
        if problem:
            break
    _stop_leftovers(api)
    return {"id": dialog.id, "success": all(t["problem"] is None for t in turns) and len(turns) == len(dialog.says),
            "problems": [t["problem"] for t in turns if t["problem"]], "turns": turns,
            "calls": sum(len(t["calls"]) for t in turns), "cost_usd": round(_spent(api) - spent_before, 6),
            "duration_s": round(time.monotonic() - started, 1)}


def _manager_profile(api: Api, model: str, effort: str | None) -> dict[str, Any]:
    """Claude Code with ``model`` and ``effort`` for the Manager alone (its role layer), registered if missing."""
    name = f"claude-code/{model}" + (f" · {effort}" if effort else "")
    profiles = {p["name"]: p for p in api.get("/api/registry")["profiles"]}
    chosen = profiles.get(name) or api.post("/api/agent-profiles", {
        "name": name, "runtime_id": "rt-claude-code", "provider_id": "prov-claude-subscription", "model": model,
        **({"effort": effort} if effort else {})})["profile"]
    api.put("/api/agent-settings/roles/manager", {"profile_id": chosen["profile_id"]})
    return chosen


def run(
    *, mode: str, dialogs: tuple[Dialog, ...], admin_url: str, out_dir: Path, model: str | None = None,
    agent: str | None = None, register: dict[str, Any] | None = None, max_budget_usd: float | None = None,
    total_cap_usd: float | None = None, timeout: float | None = None, keep_work_dirs: bool = False,
    manager_model: str | None = None, manager_effort: str | None = None,
) -> tuple[dict[str, Any], Path]:
    """Run ``dialogs`` in ``mode``; the run and the JSON file written to ``out_dir`` (``runner.run``'s setup). With
    ``manager_model`` the Manager alone runs Claude Code with that model and ``manager_effort``."""
    real_claude = shutil.which("claude") if mode == "real" else None
    git = _git()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_dir.mkdir(parents=True, exist_ok=True)
    log_path = out_dir / f"{stamp}-dialogs-{mode}.server.log"
    raw_root = Path(tempfile.mkdtemp(prefix="looper-dialogs-"))
    root = raw_root.resolve()
    timeout = timeout or (60.0 if mode == "fake" else 300.0)
    results: list[dict[str, Any]] = []
    stopped = False
    try:
        work_dir = root / "work" / "calc"
        work_dir.mkdir(parents=True)
        scenarios = root / "scenarios.json"
        scenarios.write_text(json.dumps({os.path.realpath(work_dir): _SLOW_FAKE, "manager": {}}), encoding="utf-8")
        install_claude(root / "bin", mode=mode, scenarios=scenarios,
                       max_budget_usd=None if register is not None else max_budget_usd, real_claude=real_claude)
        (root / "runtime").mkdir()
        home = root / "home" if mode == "fake" else None
        if home is not None:
            home.mkdir()
        with fresh_database(admin_url) as (database_url, _), running_server(
            database_url=database_url, runtime_root=root / "runtime", bin_dir=root / "bin", log_path=log_path,
            home=home,
        ) as api:
            if register is not None:
                agent = register_agent(api, register)["name"]
            chosen = select_agent(api, agent=agent, model=model)
            if register is not None:
                api.put("/api/agent-settings/system", {"profile_id": chosen["profile_id"],
                                                       "run_budget_usd": max_budget_usd})
            manager = _manager_profile(api, manager_model, manager_effort) if manager_model else chosen
            project = api.post("/api/projects", {"name": PROJECT, "work_dir": str(work_dir)})
            project_id = project["project_id"]
            for dialog in dialogs:
                if stopped or (total_cap_usd is not None and _spent(api) >= total_cap_usd):
                    stopped = True
                    results.append({"id": dialog.id, "skipped": "the spending cap was reached", "cost_usd": 0.0})
                    continue
                results.append(run_dialog(api, dialog, database_url=database_url, project_id=project_id,
                                          scenarios=scenarios, mode=mode, timeout=timeout))
            total = _spent(api)
    finally:
        if not keep_work_dirs:
            shutil.rmtree(root, ignore_errors=True)
    result = scrub({
        "schema": SCHEMA, "mode": mode, "model": chosen.get("model") if mode == "real" else None,
        "agent": chosen["name"], "manager_agent": manager["name"], "started_at": stamp, "git": git,
        "caps": {"max_budget_usd": max_budget_usd, "total_cap_usd": total_cap_usd}, "stopped_by_cap": stopped,
        "total_cost_usd": round(total, 6), "dialogs": results,
    }, {str(root): "<eval-root>", str(raw_root): "<eval-root>", str(Path.home()): "~"})
    path = out_dir / f"{stamp}-dialogs-{mode}.json"
    path.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return result, path


def markdown(result: dict[str, Any]) -> str:
    """The run as a table: each dialog's outcome, its tool calls per turn and its cost."""
    lines = [f"Manager dialogs: {result['mode']}/{result.get('manager_agent') or result['agent']} "
             f"(other roles {result.get('model') or result['agent']}) @ "
             f"{(result['git'].get('commit') or '')[:9]}{'+local changes' if result['git'].get('dirty') else ''}",
             "", "| dialog | success | turns | tool calls | cost (USD) |", "|---|---|---|---|---|"]
    for dialog in result["dialogs"]:
        if dialog.get("skipped"):
            lines.append(f"| {dialog['id']} | skipped | | | |")
            continue
        calls = "; ".join(", ".join(turn["calls"]) or "none" for turn in dialog["turns"])
        outcome = "pass" if dialog["success"] else f"fail: {'; '.join(dialog['problems'])}"
        lines.append(f"| {dialog['id']} | {outcome} | {len(dialog['turns'])} | {calls} | {dialog['cost_usd']:.4f} |")
    lines.append(f"| **total** | {sum(bool(d.get('success')) for d in result['dialogs'])}/{len(result['dialogs'])} "
                 f"| | | {result['total_cost_usd']:.4f} |")
    return "\n".join(lines)
