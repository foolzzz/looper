"""The Manager creating, changing and running schedules, and exporting the configuration, on a real PostgreSQL.

A new schedule is disabled, its configuration is shown with the question whether to enable it, and the turn that
created it can neither enable nor run it; a change is made at once and shown before and after, but one raising or
removing a spend cap, enabling or running now waits for the user's word in the next message; a confirmed Run now
fires it as the dashboard's button does.
The configuration export writes the file, without secrets, into the runtime directory. Each call goes through
``ManagerOps`` in a turn of a Lark chat (``tests.test_ops_tasks_db._Chat``); what changed is read back through the
dashboard's HTTP API. Runs only when LOOPER_TEST_DATABASE_URL points at a disposable database.
"""

from __future__ import annotations

import json
import re
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from tests.test_manager_config_tools_db import _ok, _trail
from tests.test_manager_schedule_tools_db import _confirm, _schedule, _starting_state
from tests.test_ops_tasks_db import _Chat, _error, _project
from tests.test_usage_api_db import DSN, _run

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")

TOOL = "looper_schedules"
DEPS = {"name": "deps", "cron": "0 9 * * 1-5", "timezone": "Asia/Shanghai",
        "requirement": "Check the deps and write deps.md."}
SECRET = "sk-test-0123456789"


def _create(**fields: Any) -> tuple[str, dict[str, Any]]:
    return TOOL, {"action": "create", "project": "calc", "fields": {**DEPS, **fields}}


async def _view(name: str) -> dict[str, Any]:
    return next(s for s in (await _ok("GET", "/api/schedules"))["schedules"] if s["name"] == name)


def _with(chat: _Chat, **platform: Any) -> _Chat:
    """``chat`` whose tools act through a platform with ``platform`` (a scheduler, a runtime directory)."""
    chat.ops._platform = replace(chat.ops._platform, **platform)
    return chat


# --- create ---


def test_a_schedule_is_created_disabled_with_its_configuration_and_enabled_only_in_a_later_turn(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    async def test(pool: Any) -> None:
        await _project(pool, "calc", str(tmp_path))
        chat = _Chat(tmp_path)
        created, enabled_now, run_now = await chat.say(
            "每个工作日早上 9 点检查依赖", _create(), (TOOL, {"action": "enable", "schedule": "deps"}),
            (TOOL, {"action": "run", "schedule": "deps"}))

        assert created["status"] == "ok" and created["result"]["enabled"] is False, created
        # The user has not seen it yet: neither enabled nor run in the turn that made it.
        for refused in (enabled_now, run_now):
            assert _error(refused) == "conflict" and "was created in this turn" in refused["error"]["message"]
        schedule = await _view("deps")
        assert (schedule["enabled"], schedule["created_via"], schedule["next_fire_at"]) == (False, "manager", None)
        (block,) = chat.turns[0].blocks
        lines = block.split("\n")
        assert lines[:2] == ["定时任务已创建，还没有启用：",
                             f"- 定时任务：deps（{schedule['schedule_id']}），项目 calc"]
        assert lines[2] == "- 时间规则：0 9 * * 1-5（Weekdays at 09:00 (Asia/Shanghai)），时区 Asia/Shanghai"
        assert re.fullmatch(r"- 接下来 3 次：(\d{4}-\d\d-\d\d 09:00 CST、){2}\d{4}-\d\d-\d\d 09:00 CST", lines[3])
        assert lines[4:] == [
            f"- 目录：项目的默认目录（{tmp_path}）", "- 需求：33 字：Check the deps and write deps.md.",
            "- 上一次还在运行时：跳过这次触发；错过的触发：补跑一次", "- 前置检查：没有",
            "- 花费上限：每次运行 项目的每任务上限，每天 不限", "- 通知：失败时 → 只在控制台", "",
            "要启用吗？回复「启用」就启用，之后按规则无人值守地运行；也可以稍后在 Schedules 页打开它的开关。"]

        (asked,) = await chat.say("启用", (TOOL, {"action": "enable", "schedule": "deps"}))
        assert await _starting_state(pool, schedule["schedule_id"]) == (False, 0)
        enabled = await _confirm(chat, asked)
        assert enabled["status"] == "ok"
        assert (await _view("deps"))["enabled"] is True
        assert await _trail(pool) == [(TOOL, "write", "ok"), (TOOL, "write", "error"), (TOOL, "write", "error"),
                                      (TOOL, "high", "needs_confirmation"), (TOOL, "high", "ok")]

    _run(test, monkeypatch)


@pytest.mark.parametrize("action", ["enable", "run"])
def test_a_schedule_created_this_turn_cannot_start_or_leave_a_pending_confirmation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, action: str,
) -> None:
    """The creation's configuration must reach the user before a start can even ask for confirmation."""
    async def test(pool: Any) -> None:
        await _project(pool, "calc", str(tmp_path))
        chat = _Chat(tmp_path)
        created, refused = await chat.say("建好并启用或运行", _create(), (TOOL, {"action": action, "schedule": "deps"}))
        assert created["status"] == "ok", created
        assert _error(refused) == "conflict" and "was created in this turn" in refused["error"]["message"]
        sid = (await _view("deps"))["schedule_id"]
        assert await _starting_state(pool, sid) == (False, 0)
        (block,) = chat.turns[-1].blocks
        assert block.startswith("定时任务已创建，还没有启用：")
        assert await pool.fetchval("SELECT count(*) FROM manager_actions WHERE confirmation_id IS NOT NULL") == 0

        # No id was issued, and the next message carries no pending-confirmation note.
        (missing,) = await chat.say("确认", ("looper_confirm", {"confirmation_id": "c-not-issued"}))
        assert _error(missing) == "not_found" and chat.notes[-1] == ""
        assert await _starting_state(pool, sid) == (False, 0)
        assert await _trail(pool) == [(TOOL, "write", "ok"), (TOOL, "write", "error"),
                                      ("looper_confirm", "high", "error")]

    _run(test, monkeypatch)


def test_creating_takes_every_setting_the_dashboard_takes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    async def test(pool: Any) -> None:
        await _project(pool, "calc", str(tmp_path))
        await pool.execute("INSERT INTO agent_profiles (id, name, runtime_id, provider_id, model) VALUES "
                           "('agent-sonnet', 'claude-code/sonnet', 'rt-claude-code', 'prov-claude-subscription', "
                           "'sonnet')")
        chat = _Chat(tmp_path)
        (created,) = await chat.say("建定时任务", _create(
            overlap="parallel", max_parallel_runs=2, catch_up="skip", run_timeout_seconds=1800, run_budget_usd=1.5,
            daily_budget_usd=3, gate_command="./new-items", gate_timeout_seconds=30, notify="always",
            profiles={"worker": "claude-code/sonnet"}, task_limits={"worker": 2}, work_dir=str(tmp_path)))

        assert created["status"] == "ok", created
        schedule = await _view("deps")
        assert {k: schedule[k] for k in ("overlap", "max_parallel_runs", "catch_up", "run_timeout_seconds",
                                         "run_budget_usd", "daily_budget_usd", "gate_command",
                                         "gate_timeout_seconds", "notify", "profile_overrides", "task_limits",
                                         "work_dir")} == {
            "overlap": "parallel", "max_parallel_runs": 2, "catch_up": "skip", "run_timeout_seconds": 1800,
            "run_budget_usd": 1.5, "daily_budget_usd": 3.0, "gate_command": "./new-items", "gate_timeout_seconds": 30,
            "notify": "always", "profile_overrides": {"worker": "agent-sonnet"},
            "task_limits": {"worker": 2, "reviewer": None}, "work_dir": str(tmp_path)}
        (block,) = chat.turns[0].blocks
        for line in (f"- 目录：{tmp_path}", "- 上一次还在运行时：并行运行，最多 2 个同时运行；错过的触发：跳过",
                     "- 前置检查：`./new-items`", "- 花费上限：每次运行 $1.5，每天 $3", "- 通知：每次运行 → 只在控制台",
                     "- 运行超时：1800 秒", "- Agent Profile：worker：claude-code/sonnet", "- 每次运行的任务并发：worker 2"):
            assert line in block.split("\n"), (line, block)

    _run(test, monkeypatch)


@pytest.mark.parametrize("allowed", [True, False], ids=["allowed", "not-allowed"])
def test_a_schedule_made_from_a_lark_chat_tells_whoever_asked_when_the_allowlist_lets_them(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, allowed: bool,
) -> None:
    async def test(pool: Any) -> None:
        from looper.agents.serve import seed_channel_configs

        await _project(pool, "calc", str(tmp_path))
        await seed_channel_configs(pool)
        sender = "ou_0123456789abcdef"
        await pool.execute("UPDATE channel_configs SET config = config || $1::jsonb WHERE channel_id = 'channel-lark'",
                           json.dumps({"allowed_open_ids": [sender if allowed else "ou_someone0else"]}))
        chat = _Chat(tmp_path, sender=sender)
        (created,) = await chat.say("建定时任务", _create())

        assert created["status"] == "ok", created
        target = (await _view("deps"))["notify_target"]
        assert target == ({"channel": "lark", "chat": sender} if allowed else None)
        (block,) = chat.turns[0].blocks
        assert (f"- 通知：失败时 → 飞书 · {sender}" if allowed else "- 通知：失败时 → 只在控制台") in block

    _run(test, monkeypatch)


@pytest.mark.parametrize(("arguments", "code", "message"), [
    ({"action": "create", "fields": DEPS}, "invalid", "project is required for action create"),
    ({"action": "create", "project": "nope", "fields": DEPS}, "not_found", "no project named 'nope'"),
    ({"action": "create", "project": "calc"}, "invalid", "fields is required for action create"),
    ({"action": "create", "project": "calc", "fields": {**DEPS, "cron": "*/3 * * * *"}}, "invalid",
     "fires 3 minutes apart"),
    ({"action": "create", "project": "calc", "fields": {**DEPS, "enabled": True}}, "invalid",
     "fields has an unknown key 'enabled'"),
    ({"action": "create", "project": "calc", "fields": {**DEPS, "name": "Nightly"}}, "conflict",
     "this project already has a schedule named 'Nightly'"),
    ({"action": "create", "project": "calc", "fields": {**DEPS, "task_limits": {"planner": 2}}}, "invalid",
     "task_limits: one Planner runs a task"),
])
def test_a_schedule_is_not_created_when_the_dashboard_would_refuse_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, arguments: dict[str, Any], code: str, message: str,
) -> None:
    async def test(pool: Any) -> None:
        await _schedule(await _project(pool, "calc", str(tmp_path)), "nightly")
        chat = _Chat(tmp_path)
        (answer,) = await chat.say("建定时任务", (TOOL, arguments))

        assert _error(answer) == code and message in answer["error"]["message"], answer
        assert chat.turns[0].blocks == []
        assert await pool.fetchval("SELECT count(*) FROM schedules") == 1

    _run(test, monkeypatch)


# --- update ---


def test_a_change_is_made_at_once_and_shown_before_and_after(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    async def test(pool: Any) -> None:
        await _schedule(await _project(pool, "calc", str(tmp_path)), "nightly", "0 2 * * *", enabled=True,
                        daily_budget_usd=3)
        chat = _Chat(tmp_path)
        (changed,) = await chat.say("改成每天 10 点，并行跑两个，Worker 并发 2，每日上限降到 1 美元", (TOOL, {
            "action": "update", "schedule": "nightly", "fields": {
                "cron": "0 10 * * *", "overlap": "parallel", "max_parallel_runs": 2, "task_limits": {"worker": 2},
                "daily_budget_usd": 1}}))

        assert changed["status"] == "ok", changed
        assert changed["result"]["changed"] == ["cron", "overlap", "max_parallel_runs", "daily_budget_usd",
                                                "task_limits"]
        schedule = await _view("nightly")
        assert (schedule["cron"], schedule["overlap"], schedule["max_parallel_runs"], schedule["daily_budget_usd"],
                schedule["task_limits"], schedule["enabled"]) == (
            "0 10 * * *", "parallel", 2, 1.0, {"worker": 2, "reviewer": None}, True)
        (block,) = chat.turns[0].blocks
        lines = block.split("\n")
        assert lines[:6] == [
            "已修改定时任务「nightly」（项目 calc）：",
            "- 时间规则：0 2 * * *（Daily at 02:00 (America/Los_Angeles)） → 0 10 * * *（Daily at 10:00 "
            "(America/Los_Angeles)）",
            "- 上一次还在运行时：跳过这次触发 → 并行运行", "- 同时运行的上限：3 → 2", "- 每日花费上限：$3 → $1",
            "- 每次运行的任务并发：按角色的默认值 → worker 2"]
        assert lines[6].startswith("- 接下来 3 次：") and lines[7] == "它现在已启用。"
        assert await _trail(pool) == [(TOOL, "write", "ok")]

    _run(test, monkeypatch)


@pytest.mark.parametrize(("before", "after", "line"), [
    ({"daily_budget_usd": 3}, {"daily_budget_usd": 5}, "- 每日花费上限：$3 → $5"),
    ({"daily_budget_usd": 3}, {"daily_budget_usd": None}, "- 每日花费上限：$3 → 不限"),
    ({"run_budget_usd": 1}, {"run_budget_usd": 2}, "- 每次运行的花费上限：$1 → $2"),
])
def test_raising_or_removing_a_spend_cap_waits_for_the_users_word_in_the_next_message(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, before: dict[str, Any], after: dict[str, Any], line: str,
) -> None:
    async def test(pool: Any) -> None:
        await _schedule(await _project(pool, "calc", str(tmp_path)), "nightly", **before)
        (key, value), = after.items()
        update = (TOOL, {"action": "update", "schedule": "nightly", "fields": after})
        chat = _Chat(tmp_path)

        (asked,) = await chat.say("提高上限", update)
        assert asked["status"] == "needs_confirmation", asked
        (block,) = chat.turns[0].blocks
        assert block == (f"需要你确认：\n要修改定时任务「nightly」（项目 calc）吗？\n{line}\n这会提高（或取消）它的花费上限。\n"
                         "回复「确认」就执行；回复别的就不执行。")
        assert (await _view("nightly"))[key] == next(iter(before.values()))
        assert await chat.say("算了") == []  # not confirmed: it lapses
        assert (await _view("nightly"))[key] == next(iter(before.values()))

        (again,) = await chat.say("还是提高吧", update)
        (done,) = await chat.say("确认", ("looper_confirm", {"confirmation_id": again["confirmation_id"]}))
        assert done["status"] == "ok", done
        assert (await _view("nightly"))[key] == value
        assert await _trail(pool) == [(TOOL, "high", "needs_confirmation"), (TOOL, "high", "needs_confirmation"),
                                      (TOOL, "high", "ok")]

    _run(test, monkeypatch)


@pytest.mark.parametrize(("before", "after"), [
    ({"daily_budget_usd": 3}, {"daily_budget_usd": 1}),  # lowered
    ({}, {"daily_budget_usd": 2}),  # a cap where there was none
    ({}, {"run_budget_usd": None}),  # none removed: nothing rises
], ids=["lowered", "added", "none-removed"])
def test_a_spend_cap_lowered_or_added_changes_at_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, before: dict[str, Any], after: dict[str, Any],
) -> None:
    async def test(pool: Any) -> None:
        await _schedule(await _project(pool, "calc", str(tmp_path)), "nightly", **before)
        (answer,) = await _Chat(tmp_path).say("改上限", (TOOL, {"action": "update", "schedule": "nightly",
                                                               "fields": after}))
        assert answer["status"] == "ok", answer
        (key, value), = after.items()
        assert (await _view("nightly"))[key] == value

    _run(test, monkeypatch)


@pytest.mark.parametrize(("project_cap", "fields", "asks"), [
    (5, {"run_budget_usd": 100}, True),  # null is the project's cap: $100 raises it
    (5, {"run_budget_usd": 3}, False),  # below the project's cap
    (5, {"run_budget_usd": 5}, False),
    (None, {"run_budget_usd": 100}, False),  # the project has none either: any value is a decrease
], ids=["above-project", "below-project", "equal-project", "no-project-cap"])
def test_a_run_cap_the_schedule_does_not_set_is_the_projects_when_raising_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, project_cap: float | None, fields: dict[str, Any], asks: bool,
) -> None:
    async def test(pool: Any) -> None:
        project = await _project(pool, "calc", str(tmp_path))
        await pool.execute("UPDATE projects SET task_budget_usd = $1 WHERE id = $2", project_cap, project["id"])
        await _schedule(project, "nightly")
        (answer,) = await _Chat(tmp_path).say("改上限", (TOOL, {"action": "update", "schedule": "nightly",
                                                               "fields": fields}))
        assert answer["status"] == ("needs_confirmation" if asks else "ok"), answer
        assert (await _view("nightly"))["run_budget_usd"] == (None if asks else fields["run_budget_usd"])

    _run(test, monkeypatch)


def test_a_change_the_dashboard_would_refuse_is_refused_before_anything_is_asked(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    async def test(pool: Any) -> None:
        await _schedule(await _project(pool, "calc", str(tmp_path)), "nightly", daily_budget_usd=3)
        chat = _Chat(tmp_path)
        (answer,) = await chat.say("每 3 分钟跑一次，上限 5 美元", (TOOL, {
            "action": "update", "schedule": "nightly", "fields": {"cron": "*/3 * * * *", "daily_budget_usd": 5}}))

        assert _error(answer) == "invalid" and "fires 3 minutes apart" in answer["error"]["message"], answer
        assert chat.turns[0].blocks == []
        assert (await _view("nightly"))["cron"] == "0 9 * * 1-5"

    _run(test, monkeypatch)


# --- run ---


@pytest.mark.parametrize(("fields", "status"), [({}, "running"), ({"gate_command": "sleep 5"}, "gating")],
                         ids=["task", "gate"])
def test_run_now_fires_the_schedule_once_as_the_dashboards_button_does(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, fields: dict[str, Any], status: str,
) -> None:
    async def test(pool: Any) -> None:
        from looper.agents.scheduler import Scheduler

        await _schedule(await _project(pool, "calc", str(tmp_path)), "nightly", **fields)  # disabled: it runs too
        chat = _Chat(tmp_path)
        scheduler = Scheduler(bus=chat.bus)
        _with(chat, scheduler=scheduler)
        try:
            (asked,) = await chat.say("现在跑一次 nightly", (TOOL, {"action": "run", "schedule": "nightly"}))
            assert await _starting_state(pool, (await _view("nightly"))["schedule_id"]) == (False, 0)
            ran = await _confirm(chat, asked)
        finally:
            await scheduler.stop()

        assert ran["status"] == "ok", ran
        assert ran["result"]["run"]["status"] == status and ran["result"]["run"]["trigger"] == "manual"
        runs = (await _ok("GET", f"/api/schedules/{(await _view('nightly'))['schedule_id']}/runs"))["runs"]
        assert [(r["status"], r["trigger"]) for r in runs] == [(status, "manual")]
        assert ((await _view("nightly"))["enabled"], (await _view("nightly"))["next_fire_at"]) == (False, None)
        assert await _trail(pool) == [(TOOL, "high", "needs_confirmation"), (TOOL, "high", "ok")]

    _run(test, monkeypatch)


def test_run_now_without_a_scheduler_is_unavailable(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    async def test(pool: Any) -> None:
        await _schedule(await _project(pool, "calc", str(tmp_path)), "nightly")
        chat = _Chat(tmp_path)
        (asked,) = await chat.say("跑一次", (TOOL, {"action": "run", "schedule": "nightly"}))
        assert await pool.fetchval("SELECT count(*) FROM schedule_runs") == 0
        answer = await _confirm(chat, asked)
        assert _error(answer) == "unavailable" and "runs no scheduler" in answer["error"]["message"]
        assert await pool.fetchval("SELECT count(*) FROM schedule_runs") == 0
        assert await _trail(pool) == [(TOOL, "high", "needs_confirmation"), (TOOL, "high", "error")]

    _run(test, monkeypatch)


@pytest.mark.parametrize("arguments", [
    {"action": "create", "project": "calc", "fields": DEPS},
    {"action": "update", "schedule": "nightly", "fields": {"cron": "0 10 * * *"}},
    {"action": "enable", "schedule": "nightly"},
    {"action": "run", "schedule": "nightly"},
], ids=["create", "update", "enable", "run"])
def test_a_turn_claude_runs_on_its_own_creates_changes_and_runs_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, arguments: dict[str, Any],
) -> None:
    async def test(pool: Any) -> None:
        await _schedule(await _project(pool, "calc", str(tmp_path)), "nightly")
        chat = _Chat(tmp_path)
        answer = await chat.ops.call(TOOL, arguments)
        assert _error(answer) == "needs_user_turn"
        assert [tuple(r) for r in await pool.fetch("SELECT name, cron FROM schedules")] == [("nightly", "0 9 * * 1-5")]
        assert await pool.fetchval("SELECT count(*) FROM schedule_runs") == 0
        assert (await _view("nightly"))["enabled"] is False
        assert await _trail(pool) == [(TOOL, "write", "error")]

    _run(test, monkeypatch)


# --- looper_config_export ---


def test_the_configuration_is_exported_into_the_runtime_directory_without_secrets(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    async def test(pool: Any) -> None:
        await _ok("POST", "/api/providers", {"name": "keyed", "protocols": ["anthropic-messages"], "auth": "api_key",
                                             "api_key": SECRET, "base_url": "https://api.example.test"})
        await _schedule(await _project(pool, "calc", str(tmp_path)), "nightly", enabled=True)
        runtime = tmp_path / "runtime"
        chat = _with(_Chat(tmp_path), runtime_root=runtime)

        (answer,) = await chat.say("把配置导出来，包括项目", ("looper_config_export", {"include_projects": True}))

        assert answer["status"] == "ok", answer
        result = answer["result"]
        path = Path(result["path"])
        assert path.parent == runtime / "exports" and path.name.startswith("looper-config-")
        text = path.read_text()
        assert SECRET not in text
        data = json.loads(text)
        assert data["format"] == result["format"] == "looper-config/v2" and data["secrets"] == "omitted"
        (project,) = data["projects"]
        assert [s["name"] for s in project["schedules"]] == ["nightly"]
        assert result["counts"]["schedules"] == 1 and result["secrets_left_out"] >= 1
        assert result["bytes"] == path.stat().st_size
        # A read: Claude's own turns may export too, and the trail keeps no result.
        assert (await chat.ops.call("looper_config_export", {}))["status"] == "ok"
        assert len(list((runtime / "exports").iterdir())) == 2
        assert await _trail(pool) == [("looper_config_export", "read", "ok")] * 2
        assert await pool.fetchval("SELECT count(*) FROM manager_actions WHERE result IS NOT NULL") == 0

    _run(test, monkeypatch)


def test_without_projects_the_export_leaves_them_and_their_schedules_out(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    async def test(pool: Any) -> None:
        await _schedule(await _project(pool, "calc", str(tmp_path)), "nightly")
        chat = _with(_Chat(tmp_path), runtime_root=tmp_path / "runtime")
        (answer,) = await chat.say("导出配置", ("looper_config_export", {}))
        data = json.loads(Path(answer["result"]["path"]).read_text())
        assert "projects" not in data and "schedules" not in answer["result"]["counts"]
        assert datetime.fromisoformat(data["exported_at"])

    _run(test, monkeypatch)


@pytest.mark.parametrize(("arguments", "runtime", "code"), [
    ({"include_secrets": True}, True, "forbidden"),
    ({"path": "/etc/looper.json"}, True, "invalid"),
    ({}, False, "unavailable"),
], ids=["secrets", "path", "no-runtime-directory"])
def test_the_export_takes_neither_secrets_nor_a_path_and_needs_a_runtime_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, arguments: dict[str, Any], runtime: bool, code: str,
) -> None:
    async def test(pool: Any) -> None:
        chat = _Chat(tmp_path)
        if runtime:
            _with(chat, runtime_root=tmp_path / "runtime")
        (answer,) = await chat.say("导出配置", ("looper_config_export", arguments))
        assert _error(answer) == code, answer
        assert not (tmp_path / "runtime" / "exports").exists() or not list((tmp_path / "runtime" / "exports").iterdir())

    _run(test, monkeypatch)
