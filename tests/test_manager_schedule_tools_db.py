"""The Manager's schedules tool on a real PostgreSQL: it lists and reads what the Schedules page reads, enables and
disables as the page's switch does, enables, runs and deletes only on the user's word in the next message, names a
schedule by id or by name, and leaves the trail.

Each call goes through ``ManagerOps`` in a turn of a Lark chat (``tests.test_ops_tasks_db._Chat``); what changed is
read back through the dashboard's HTTP API. Runs only when LOOPER_TEST_DATABASE_URL points at a disposable database.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from tests.db_support import DSN, run_db
from tests.test_manager_config_tools_db import _ok, _trail
from tests.test_manager_ops import _Clock
from tests.test_ops_tasks_db import _Chat, _error, _project, _task
from tests.test_catalog_db import _call

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")

TOOL = "looper_schedules"


def test_schedule_tool_delete_description_distinguishes_command_and_agent_schedules():
    from looper.agents.ops.service import MANAGER_TOOLS

    tool = next(tool for tool in MANAGER_TOOLS if tool.name == TOOL)
    delete = tool.description.split("; delete ", 1)[1].split("; reset_planner_session", 1)[0]
    assert delete == (
        "(the user confirms first; an agent schedule's tasks stay; deleting a command schedule is refused only "
        "while its command is running: disable it and wait for the run to end; otherwise the delete removes "
        "its runs and messages, and a queued run will not execute; a command schedule has no tasks)")


@pytest.mark.parametrize("action", ["enable", "run"])
@pytest.mark.parametrize("env_file", [False, True])
def test_command_schedule_creation_and_start_show_the_command_before_confirmation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, action: str, env_file: bool,
) -> None:
    async def test(pool: Any) -> None:
        await _project(pool, work_dir=str(tmp_path))
        chat = _Chat(tmp_path)
        (tmp_path / "pull.env").write_text("TOKEN=synthetic-value\n")
        command = "bash scripts/pull.sh # `literal`\n# ````" + (" long command " * 30 + "end" if action == "run" else "")
        fenced = f"\n`````\n{command}\n`````\n"
        created, refused = await chat.say("每分钟运行脚本", (TOOL, {
            "action": "create", "project": "calc", "fields": {
                "name": "pull", "kind": "command", "command": command, "cron": "* * * * *",
                "overlap": "skip", "run_timeout_seconds": 180, "env": {"MODE": "private-value"},
                "env_file": str(tmp_path / "pull.env") if env_file else None, "success_deadline": "09:00"}}),
            (TOOL, {"action": action, "schedule": "pull"}))
        assert created["status"] == "ok", created
        assert created["result"]["schedule"]["kind"] == "command"
        sid = created["result"]["schedule"]["schedule_id"]
        assert await _starting_state(pool, sid) == (False, 0)
        assert _error(refused) == "conflict", refused
        (block,) = chat.turns[-1].blocks
        for text in ("不花模型额度", "- 环境变量：MODE",
                     "- 最晚成功时间：09:00", "- 告警阈值：连续失败 3 次、长时间没成功按默认、连续跳过 10 次（0 为不发）"):
            assert text in block
        for text in ("需求", "前置检查", "用户确认", "花费上限", "Profile", "任务并发", "private-value"):
            assert text not in block
        assert f"- 命令：{fenced}" in block

        (asked,) = await chat.say("启用或立即运行", (TOOL, {"action": action, "schedule": "pull"}))
        assert asked["status"] == "needs_confirmation", asked
        assert await _starting_state(pool, sid) == (False, 0)
        (block,) = chat.turns[-1].blocks
        assert f"- 命令：{fenced}" in block
        assert "不花模型额度" in block
        assert "- 目录：" in block
        assert "- 运行超时：180 秒" in block
        assert "- 环境变量：MODE" in block
        assert (f"- 环境文件：{tmp_path / 'pull.env'}" in block) is env_file
        assert ("- 环境文件：" in block) is env_file
        assert all(text not in block for text in ("private-value", "synthetic-value", "需求", "建任务"))
        assert ("- 最晚成功时间：09:00" in block) is (action == "enable")
        assert ("- 告警阈值：连续失败 3 次" in block) is (action == "enable")
        assert ("- 接下来 3 次：" in block) is (action == "enable")
        if action == "enable":
            enabled = await _confirm(chat, asked)
            assert enabled["status"] == "ok", enabled
            assert await _starting_state(pool, sid) == (True, 0)

        # Read a command's stored exit result through the compact list and get projections.
        from looper.db import schedules as db_schedules

        run_id = await _run_of(pool, sid, "failed")
        await db_schedules.update_run(pool, run_id, exit_code=3)
        (got,) = await chat.say("查看 pull", (TOOL, {"action": "get", "schedule": "pull"}))
        assert got["result"]["schedule"]["kind"] == "command"
        assert got["result"]["latest_runs"][0]["exit_code"] == 3
        assert got["result"]["latest_runs"][0]["kind"] == "command"
        (listed,) = await chat.say("列出定时任务", (TOOL, {"action": "list"}))
        schedule = listed["result"]["schedules"][0]
        assert (schedule["kind"], schedule["last_run"]["exit_code"]) == ("command", 3)
        assert schedule["last_run"]["kind"] == "command"

    run_db(test, monkeypatch)


@pytest.mark.parametrize("kind", ["command", "agent"])
def test_schedule_update_shows_verbatim_command_diff_and_model_spend_by_kind(monkeypatch, tmp_path, kind):
    async def test(pool):
        before, after = "printf '`before`'\n# ```", "printf '`after`'\n# ````"
        sid = (await _schedule(await _project(pool, work_dir=str(tmp_path)), "pull", kind=kind,
                               **({"command": before, "requirement": ""} if kind == "command" else {})))["schedule_id"]
        chat = _Chat(tmp_path)
        fields = {"command": after} if kind == "command" else {"requirement": "Read updated reports."}
        (result,) = await chat.say("修改定时任务", (TOOL, {"action": "update", "schedule": sid, "fields": fields}))
        assert result["status"] == "ok", result
        (block,) = chat.turns[-1].blocks
        assert block.count("不经过 agent，不花模型额度") == int(kind == "command")
        if kind == "command":
            assert f"\n````\n{before}\n````\n" in block
            assert f"\n`````\n{after}\n`````\n" in block
            assert "→" in block
            (result,) = await chat.say("名称不变", (TOOL, {"action": "update", "schedule": sid, "fields": {"name": "pull"}}))
            assert result["status"] == "ok", result
            assert chat.turns[-1].blocks[0].count("不经过 agent，不花模型额度") == 1
    run_db(test, monkeypatch, timeout=30)


async def _confirm(chat: _Chat, asked: dict[str, Any]) -> dict[str, Any]:
    """The user confirms in the very next message, whose note names what will run."""
    assert asked["status"] == "needs_confirmation", asked
    (answer,) = await chat.say("确认", ("looper_confirm", {"confirmation_id": asked["confirmation_id"]}))
    assert asked["confirmation_id"] in chat.notes[-1]
    return answer


@pytest.mark.parametrize("action", ["enable", "run"])
@pytest.mark.parametrize("when", ["before-confirm", "before-lock", "name-reused"])
def test_command_confirmation_is_bound_to_the_schedule_and_configuration(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, action: str, when: str,
) -> None:
    async def test(pool: Any) -> None:
        from looper.agents import schedules
        from looper.agents.scheduler import Scheduler
        from looper.db import schedules as db_schedules

        project = await _project(pool, work_dir=str(tmp_path))
        sid = (await _schedule(project, "pull", kind="command", command="exit 0", requirement=""))["schedule_id"]
        chat = _Chat(tmp_path)
        scheduler = Scheduler(bus=chat.bus, runtime_root=tmp_path / "runtime")
        scheduler._commands.prepare()
        chat.ops._platform = replace(chat.ops._platform, scheduler=scheduler)
        try:
            arguments = {"action": action, "schedule": "pull"}
            (asked,) = await chat.say("启用或立即运行", (TOOL, arguments))
            assert "\n```\nexit 0\n```\n" in chat.turns[-1].blocks[0]
            if when == "before-lock":
                lock = db_schedules.lock_schedule

                async def change_before_lock(conn: Any, schedule_id: str) -> Any:
                    # Edit after the operation resolves its target, just before its execution lock.
                    monkeypatch.setattr(db_schedules, "lock_schedule", lock)
                    await schedules.update_schedule(sid, {"command": "exit 7"})
                    return await lock(conn, schedule_id)

                monkeypatch.setattr(db_schedules, "lock_schedule", change_before_lock)
            elif when == "name-reused":
                await schedules.update_schedule(sid, {"name": "renamed"})
                await _schedule(project, "pull", kind="command", command="exit 7", requirement="")
            else:
                await schedules.update_schedule(sid, {"command": "exit 7"})

            refused = await _confirm(chat, asked)
            assert _error(refused) == "conflict", refused
            assert "confirm" in refused["error"]["message"].lower()
            assert await pool.fetchval("SELECT count(*) FROM schedules WHERE enabled") == 0
            assert await pool.fetchval("SELECT count(*) FROM schedule_runs") == 0

            # A fresh confirmation describes the new configuration and can execute it.
            (again,) = await chat.say("重新确认", (TOOL, arguments))
            assert "\n```\nexit 7\n```\n" in chat.turns[-1].blocks[0]
            done = await _confirm(chat, again)
            assert done["status"] == "ok", done
            if action == "enable":
                assert done["result"]["schedule"]["enabled"] is True
            else:
                assert done["result"]["run"]["kind"] == "command"
                run = await db_schedules.get_run(done["result"]["run"]["run_id"])
                process = scheduler._commands._owned[run["id"]]
                await asyncio.to_thread(process.wait, timeout=10)
                await scheduler.tick()
                run = await db_schedules.get_run(run["id"])
                assert (run["status"], run["exit_code"]) == ("failed", 7)
        finally:
            await scheduler.stop()
            for process in list(scheduler._commands._owned.values()):
                if process.poll() is None:
                    process.terminate()
                await asyncio.to_thread(process.wait, timeout=10)

    run_db(test, monkeypatch, timeout=30)


@pytest.mark.parametrize("action", ["enable", "run"])
def test_client_command_confirmation_rejects_changed_environment_values(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, action: str,
) -> None:
    async def test(pool: Any) -> None:
        from looper.agents import schedules
        from looper.agents.api_v1 import OPERATOR
        from looper.agents.ops.clients import ClientOps
        from looper.agents.scheduler import Scheduler

        sid = (await _schedule(await _project(pool, work_dir=str(tmp_path)), "pull", kind="command",
                               command="exit 0", requirement="", env={"MODE": "dry-run"}))["schedule_id"]
        chat = _Chat(tmp_path)
        scheduler = Scheduler(bus=chat.bus)
        client = ClientOps(replace(chat.ops._platform, scheduler=scheduler))
        try:
            asked = await client.call(OPERATOR, "s1", TOOL, {"action": action, "schedule": sid})
            assert asked["status"] == "needs_confirmation", asked
            await schedules.update_schedule(sid, {"env": {"MODE": "apply"}})
            refused = await client.confirm(OPERATOR, "s1", asked["confirmation_id"])
            assert _error(refused) == "conflict", refused
            assert await _starting_state(pool, sid) == (False, 0)
        finally:
            await scheduler.stop()

    run_db(test, monkeypatch, timeout=30)


@asynccontextmanager
async def _starting_chat(pool: Any, tmp_path: Path) -> AsyncIterator[tuple[_Chat, str]]:
    """A disabled schedule and a chat with a real scheduler, stopped even if an assertion fails."""
    from looper.agents.scheduler import Scheduler

    sid = (await _schedule(await _project(pool, work_dir=str(tmp_path)), "nightly"))["schedule_id"]
    chat = _Chat(tmp_path)
    scheduler = Scheduler(bus=chat.bus)
    chat.ops._platform = replace(chat.ops._platform, scheduler=scheduler)
    try:
        yield chat, sid
    finally:
        await scheduler.stop()


async def _starting_state(pool: Any, sid: str) -> tuple[bool, int]:
    """Read the switch through the dashboard, and count every run, including one not yet started."""
    schedule = (await _ok("POST", "/api/v1/schedule_admin/get", {"schedule_id": sid}))["schedule"]
    return schedule["enabled"], await pool.fetchval("SELECT count(*) FROM schedule_runs WHERE schedule_id = $1", sid)


async def _schedule(project: dict[str, Any], name: str, cron: str = "0 9 * * 1-5", *, enabled: bool = False,
                    **fields: Any) -> dict[str, Any]:
    """A schedule made as the project's Schedules tab makes it, enabled when asked."""
    from looper.agents import schedules

    created = (await schedules.create_schedule(project["id"], {
        "name": name, "cron": cron, "timezone": "America/Los_Angeles", "requirement": "Look at the alerts.",
        **fields}))["schedule"]
    if enabled:
        created = (await schedules.update_schedule(created["schedule_id"], {"enabled": True}))["schedule"]
    return created


async def _run_of(pool: Any, schedule_id: str, status: str, *, task_id: str | None = None,
                  summary: str | None = None) -> str:
    from looper.db import schedules as db_schedules

    now = datetime.now(timezone.utc)
    run_id = await db_schedules.insert_run(
        pool, schedule_id=schedule_id, scheduled_for=now, trigger="scheduled", status=status, fired_at=now,
        task_id=task_id, finished_at=None if status in db_schedules.OPEN_STATUSES else now)
    if summary is not None:
        await db_schedules.update_run(pool, run_id, summary=summary)
    return run_id


def test_schedules_are_listed_and_read_as_the_schedules_page_reads_them(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    async def test(pool: Any) -> None:
        calc, ops = await _project(pool), await _project(pool, "ops", "/srv/ops")
        nightly = await _schedule(calc, "nightly", "0 2 * * *", enabled=True)
        await _schedule(calc, "weekly", "0 9 * * 1")
        await _schedule(ops, "triage", "*/5 * * * *", enabled=True, gate_command="./new-alerts")
        await _schedule(ops, "archive", "0 4 * * *")
        await _run_of(pool, nightly["schedule_id"], "completed", summary="All green.")
        await _run_of(pool, nightly["schedule_id"], "skipped_overlap")
        chat = _Chat(tmp_path)

        (every,) = await chat.say("我有哪些定时任务", (TOOL, {"action": "list"}))
        page = (await _ok("POST", "/api/v1/schedule_admin/list", {}))["schedules"]
        assert [(s["name"], s["project"], s["enabled"]) for s in every["result"]["schedules"]] == \
            [(s["name"], s["project_name"], s["enabled"]) for s in page] == \
            [("nightly", "calc", True), ("weekly", "calc", False), ("archive", "ops", False), ("triage", "ops", True)]
        listed = every["result"]["schedules"][0]
        assert (listed["description"], listed["last_run"]["status"]) == ("Daily at 02:00 (America/Los_Angeles)",
                                                                         "skipped_overlap")
        (calcs,) = await chat.say("calc 里停用的", (TOOL, {"action": "list", "project": "CALC", "enabled": False}))
        assert [s["name"] for s in calcs["result"]["schedules"]] == ["weekly"]

        (got,) = await chat.say("看看 nightly", (TOOL, {"action": "get", "schedule": "Nightly"}))
        schedule = got["result"]["schedule"]
        assert (schedule["schedule_id"], schedule["cron"], schedule["timezone"]) == (
            nightly["schedule_id"], "0 2 * * *", "America/Los_Angeles")
        # The latest runs as the schedule's page shows them: the routine skips left out, and counted.
        assert [(r["status"], r["summary"]) for r in got["result"]["latest_runs"]] == [("completed", "All green.")]
        assert got["result"]["routine_skips"] == {"skipped_gate": 0, "skipped_overlap": 1}
        assert await _trail(pool) == [(TOOL, "read", "ok")] * 3
        assert await pool.fetchval("SELECT count(*) FROM manager_actions WHERE result IS NOT NULL") == 0

    run_db(test, monkeypatch)


def test_a_schedule_is_enabled_and_disabled_as_its_switch_does_in_a_turn_answering_the_user(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    async def test(pool: Any) -> None:
        sid = (await _schedule(await _project(pool), "nightly", "0 2 * * *"))["schedule_id"]
        chat = _Chat(tmp_path)

        # A turn Claude runs on its own only reads.
        refused = await chat.ops.call(TOOL, {"action": "enable", "schedule": "nightly"})
        assert _error(refused) == "needs_user_turn"
        assert (await _ok("POST", "/api/v1/schedule_admin/get", {"schedule_id": sid}))["schedule"]["enabled"] is False

        (asked,) = await chat.say("启用 nightly", (TOOL, {"action": "enable", "schedule": "nightly"}))
        assert await _starting_state(pool, sid) == (False, 0)
        on = await _confirm(chat, asked)
        assert on["status"] == "ok", on
        schedule = (await _ok("POST", "/api/v1/schedule_admin/get", {"schedule_id": sid}))["schedule"]
        assert schedule["enabled"] is True and "note" not in on["result"]
        assert datetime.fromisoformat(on["result"]["schedule"]["next_fire_at"]) == \
            datetime.fromisoformat(schedule["next_fire_at"])
        (again,) = await chat.say("再启用一次", (TOOL, {"action": "enable", "schedule": sid}))
        assert again["status"] == "ok" and "confirmation_id" not in again
        assert again["result"]["note"] == "it was enabled already"
        assert chat.turns[-1].blocks == []
        (off,) = await chat.say("停用 nightly", (TOOL, {"action": "disable", "schedule": "NIGHTLY"}))
        schedule = (await _ok("POST", "/api/v1/schedule_admin/get", {"schedule_id": sid}))["schedule"]
        assert (schedule["enabled"], schedule["next_fire_at"], off["result"]["schedule"]["enabled"]) == (
            False, None, False)
        assert await _trail(pool) == [(TOOL, "write", "error"), (TOOL, "high", "needs_confirmation"),
                                      (TOOL, "high", "ok"), *[(TOOL, "write", "ok")] * 2]
        assert await pool.fetchval("SELECT count(*) FROM manager_actions WHERE status = 'ok' AND result IS NULL") == 0

    run_db(test, monkeypatch)


def test_enabling_is_refused_with_the_dashboards_message(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    async def test(pool: Any) -> None:
        project = await _project(pool)
        sid = (await _schedule(project, "nightly"))["schedule_id"]
        await pool.execute("UPDATE projects SET work_dir = '' WHERE id = $1", project["id"])
        chat = _Chat(tmp_path)
        (asked,) = await chat.say("启用 nightly", (TOOL, {"action": "enable", "schedule": "nightly"}))
        assert await _starting_state(pool, sid) == (False, 0)
        answer = await _confirm(chat, asked)
        assert _error(answer) == "invalid"
        assert "a work directory is required" in answer["error"]["message"]
        status, dashboard = await _call("POST", "/api/v1/schedule_admin/update", {"schedule_id": sid, "fields": {"enabled": True}})
        assert status == 400 and dashboard["error"]["message"] == answer["error"]["message"]
        assert await _starting_state(pool, sid) == (False, 0)
        assert await _trail(pool) == [(TOOL, "high", "needs_confirmation"), (TOOL, "high", "error"),
                                     ("schedule_admin.update", "high", "error")]
        assert tuple(await pool.fetchrow(
            "SELECT confirmation_id, error_code FROM manager_actions WHERE tool = 'looper_schedules' ORDER BY id DESC LIMIT 1")) == (
                asked["confirmation_id"], "invalid")

    run_db(test, monkeypatch)


@pytest.mark.parametrize("action", ["enable", "run"])
def test_starting_a_schedule_asks_first_and_runs_only_on_the_next_messages_confirmation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, action: str,
) -> None:
    """Asking has no effect; confirming executes the original high-tier call exactly once."""
    async def test(pool: Any) -> None:
        async with _starting_chat(pool, tmp_path) as (chat, sid):
            (asked,) = await chat.say("启用或运行 nightly", (TOOL, {"action": action, "schedule": "nightly"}))
            assert asked["status"] == "needs_confirmation", asked
            assert await _starting_state(pool, sid) == (False, 0)
            assert await pool.fetchval("SELECT count(*) FROM tasks") == 0
            assert await _trail(pool) == [(TOOL, "high", "needs_confirmation")]
            (block,) = chat.turns[-1].blocks
            assert "定时任务「nightly」（项目 calc）" in block
            assert "回复「确认」就执行" in block
            assert "- 需求：" in block and "- 花费上限：" in block
            assert "不花模型额度" not in block and "- 环境变量：" not in block
            assert ("它会立刻建出任务并开始执行，花费计入它的上限。" in block) is (action == "run")
            assert ("启用后它按规则无人值守地建任务运行。" in block) is (action == "enable")

            done = await _confirm(chat, asked)
            assert done["status"] == "ok", done
            assert await _starting_state(pool, sid) == (action == "enable", int(action == "run"))
            if action == "run":
                runs = (await _ok("POST", "/api/v1/schedule_admin/runs", {"schedule_id": sid}))["runs"]
                assert [(r["trigger"], r["status"]) for r in runs] == [("manual", "running")]
                assert runs[0]["run_id"] == done["result"]["run"]["run_id"]
            assert await _trail(pool) == [(TOOL, "high", "needs_confirmation"), (TOOL, "high", "ok")]
            assert [tuple(r) for r in await pool.fetch(
                "SELECT confirmation_id, message_id FROM manager_actions ORDER BY id")] == [
                    (asked["confirmation_id"], turn.message_id) for turn in chat.turns]

            (reused,) = await chat.say("再确认", ("looper_confirm", {"confirmation_id": asked["confirmation_id"]}))
            assert _error(reused) == "not_found"
            assert await _starting_state(pool, sid) == (action == "enable", int(action == "run"))

    run_db(test, monkeypatch)


@pytest.mark.parametrize("action", ["enable", "run"])
def test_starting_a_schedule_lapses_when_the_next_message_does_not_confirm(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, action: str,
) -> None:
    """An unrelated message uses up the one chance to confirm, with no schedule change."""
    async def test(pool: Any) -> None:
        async with _starting_chat(pool, tmp_path) as (chat, sid):
            (asked,) = await chat.say("启用或运行 nightly", (TOOL, {"action": action, "schedule": sid}))
            assert asked["status"] == "needs_confirmation", asked
            assert await chat.say("算了，先留着") == []
            assert asked["confirmation_id"] in chat.notes[-1]
            assert await _starting_state(pool, sid) == (False, 0)
            (late,) = await chat.say("确认", ("looper_confirm", {"confirmation_id": asked["confirmation_id"]}))
            assert _error(late) == "not_found"
            assert await _starting_state(pool, sid) == (False, 0)
            assert await _trail(pool) == [(TOOL, "high", "needs_confirmation"), ("looper_confirm", "high", "error")]

    run_db(test, monkeypatch)


@pytest.mark.parametrize("action", ["enable", "run"])
@pytest.mark.parametrize("when", ["asking-message", "clock-expired"])
def test_starting_a_schedule_refuses_a_confirmation_outside_its_turn_or_time(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, action: str, when: str,
) -> None:
    """A confirmation in the asking turn, or past the clock limit in the next turn, is expired."""
    from looper.agents.ops import Turn
    from looper.agents.ops.service import CONFIRMATION_SECONDS

    async def test(pool: Any) -> None:
        async with _starting_chat(pool, tmp_path) as (chat, sid):
            clock = _Clock()
            monkeypatch.setattr(chat.ops, "_clock", clock)
            turn = Turn(channel="lark", chat="oc_1", message_id="asking")
            chat.ops.begin_turn(turn)
            try:
                asked = await chat.ops.call(TOOL, {"action": action, "schedule": sid})
                assert asked["status"] == "needs_confirmation", asked
                if when == "clock-expired":
                    chat.ops.end_turn(turn)
                    turn = Turn(channel="lark", chat="oc_1", message_id="answering")
                    assert asked["confirmation_id"] in chat.ops.begin_turn(turn)
                    # Advance after the next turn starts, so _confirm itself sees an expired id.
                    clock.now += CONFIRMATION_SECONDS + 1
                refused = await chat.ops.call("looper_confirm", {"confirmation_id": asked["confirmation_id"]})
                assert _error(refused) == "expired"
            finally:
                chat.ops.end_turn(turn)
            assert await _starting_state(pool, sid) == (False, 0)
            assert await _trail(pool) == [(TOOL, "high", "needs_confirmation"), ("looper_confirm", "high", "error")]

    run_db(test, monkeypatch)


def test_deleting_a_schedule_asks_first_and_runs_only_on_the_next_messages_confirmation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    async def test(pool: Any) -> None:
        project = await _project(pool)
        sid = (await _schedule(project, "nightly", "0 2 * * *", enabled=True))["schedule_id"]
        await _task(pool, "task-run", "running", project)
        await pool.execute("UPDATE tasks SET schedule_id = $1 WHERE id = 'task-run'", sid)
        await _run_of(pool, sid, "running", task_id="task-run")
        chat = _Chat(tmp_path)

        (asked,) = await chat.say("删掉 nightly", (TOOL, {"action": "delete", "schedule": "nightly"}))
        assert asked["status"] == "needs_confirmation"
        (block,) = chat.turns[0].blocks
        assert block == (
            "需要你确认：\n要删除定时任务「nightly」（项目 calc）吗？\n"
            "- 时间规则：0 2 * * *（Daily at 02:00 (America/Los_Angeles)），时区 America/Los_Angeles\n- 状态：已启用\n"
            "- 正在进行的运行：1 次（任务运行中）。运行中的任务会继续，照常结束，之后不再记入这个定时任务，也不发通知；"
            "排队中和前置检查中的不会再开始任务。\n它建出的任务都保留；它的运行记录和通知记录会一起删除，不能恢复。\n"
            "回复「确认」就执行；回复别的就不执行。")
        assert await chat.say("算了，先留着") == []  # a message that does not confirm it: it lapses
        assert (await _call("POST", "/api/v1/schedule_admin/get", {"schedule_id": sid}))[0] == 200
        (lapsed,) = await chat.say("确认", ("looper_confirm", {"confirmation_id": asked["confirmation_id"]}))
        assert _error(lapsed) == "not_found"
        assert (await _call("POST", "/api/v1/schedule_admin/get", {"schedule_id": sid}))[0] == 200

        (again,) = await chat.say("还是删掉 nightly", (TOOL, {"action": "delete", "schedule": "nightly"}))
        (done,) = await chat.say("确认", ("looper_confirm", {"confirmation_id": again["confirmation_id"]}))
        assert done == {"status": "ok", "result": {
            "deleted": "nightly", "schedule_id": sid, "project": "calc",
            "note": "the tasks it created stay; its run history was deleted with it"}}
        assert (await _call("POST", "/api/v1/schedule_admin/get", {"schedule_id": sid}))[0] == 404
        task = await _ok("POST", "/api/v1/tasks/detail", {"task_id": "task-run"})
        assert (task["status"], task["schedule_id"]) == ("running", None)
        assert await pool.fetchval("SELECT count(*) FROM schedule_runs") == 0
        assert await _trail(pool) == [(TOOL, "high", "needs_confirmation"), ("looper_confirm", "high", "error"),
                                      (TOOL, "high", "needs_confirmation"), (TOOL, "high", "ok")]

    run_db(test, monkeypatch)


def test_deleting_a_command_schedule_with_a_running_run_is_refused_before_asking(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    async def test(pool: Any) -> None:
        sid = (await _schedule(await _project(pool), "pull", kind="command",
                               command="exit 0", requirement=""))["schedule_id"]
        await _run_of(pool, sid, "running")
        chat = _Chat(tmp_path)

        (refused,) = await chat.say("删掉 pull", (TOOL, {"action": "delete", "schedule": "pull"}))
        assert _error(refused) == "conflict", refused
        assert "disable" in refused["error"]["message"] and "wait" in refused["error"]["message"]
        assert not chat.turns[-1].blocks
        assert (await _call("POST", "/api/v1/schedule_admin/get", {"schedule_id": sid}))[0] == 200

    run_db(test, monkeypatch, timeout=30)


def test_deleting_a_command_schedule_with_a_queued_run_says_it_will_not_execute(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    async def test(pool: Any) -> None:
        sid = (await _schedule(await _project(pool), "pull", kind="command",
                               command="exit 0", requirement=""))["schedule_id"]
        await _run_of(pool, sid, "queued")
        chat = _Chat(tmp_path)

        (asked,) = await chat.say("删掉 pull", (TOOL, {"action": "delete", "schedule": "pull"}))
        assert asked["status"] == "needs_confirmation", asked
        (block,) = chat.turns[-1].blocks
        assert "排队中的运行不会再执行" in block
        assert "它建出的任务都保留" not in block
        done = await _confirm(chat, asked)
        assert done["status"] == "ok", done
        assert done["result"]["note"] == "its run history was deleted with it"
        assert (await _call("POST", "/api/v1/schedule_admin/get", {"schedule_id": sid}))[0] == 404
        assert await pool.fetchval("SELECT count(*) FROM schedule_runs WHERE schedule_id = $1", sid) == 0

    run_db(test, monkeypatch, timeout=30)


def test_a_schedule_without_a_run_in_progress_says_so_when_asked_for(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    async def test(pool: Any) -> None:
        await _schedule(await _project(pool), "weekly", "0 9 * * 1")
        chat = _Chat(tmp_path)
        (asked,) = await chat.say("删掉 weekly", (TOOL, {"action": "delete", "schedule": "weekly"}))
        assert asked["status"] == "needs_confirmation"
        (block,) = chat.turns[0].blocks
        assert "- 状态：已停用\n- 正在进行的运行：没有\n它建出的任务都保留" in block

    run_db(test, monkeypatch)


@pytest.mark.parametrize(("arguments", "code", "message"), [
    ({"action": "get", "schedule": "nightly"}, "invalid",
     "2 projects have a schedule named 'nightly': nightly in project calc (sched-"),
    ({"action": "delete", "schedule": "nightly"}, "invalid", "Give project, or the schedule's id"),
    ({"action": "get", "schedule": "nope"}, "not_found", "no schedule 'nope'; there are: nightly (calc), Nightly (ops)"),
    ({"action": "get", "schedule": "nope", "project": "ops"}, "not_found",
     "no schedule 'nope' in project ops; there are: Nightly (ops)"),
    ({"action": "get", "schedule": "nightly", "project": "nope"}, "not_found", "no project named 'nope'"),
    ({"action": "disable"}, "invalid", "schedule is required for action disable"),
])
def test_a_schedule_named_ambiguously_or_not_at_all_is_refused_naming_the_candidates(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, arguments: dict[str, Any], code: str, message: str,
) -> None:
    async def test(pool: Any) -> None:
        await _schedule(await _project(pool), "nightly", enabled=True)
        await _schedule(await _project(pool, "ops", "/srv/ops"), "Nightly", enabled=True)
        chat = _Chat(tmp_path)
        (answer,) = await chat.say("nightly", (TOOL, arguments))
        assert _error(answer) == code and message in answer["error"]["message"], answer
        assert chat.turns[0].blocks == []  # nothing was asked
        assert await pool.fetchval("SELECT count(*) FROM schedules WHERE enabled") == 2

    run_db(test, monkeypatch)


def test_a_name_two_projects_have_is_resolved_by_its_project_or_its_id(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    async def test(pool: Any) -> None:
        await _schedule(await _project(pool), "nightly", enabled=True)
        theirs = await _schedule(await _project(pool, "ops", "/srv/ops"), "Nightly", enabled=True)
        chat = _Chat(tmp_path)
        (by_project,) = await chat.say("停用 ops 的 nightly", (TOOL, {
            "action": "disable", "schedule": "nightly", "project": "ops"}))
        assert by_project["result"]["schedule"]["schedule_id"] == theirs["schedule_id"]
        (by_id,) = await chat.say("看看它", (TOOL, {"action": "get", "schedule": theirs["schedule_id"]}))
        assert by_id["result"]["schedule"]["enabled"] is False
        enabled = await pool.fetch("SELECT p.name FROM schedules s JOIN projects p ON p.id = s.project_id "
                                   "WHERE s.enabled")
        assert [row["name"] for row in enabled] == ["calc"]

    run_db(test, monkeypatch)


@pytest.mark.parametrize(("channel_id", "target"), [
    ("channel-lark", {"channel": "lark", "chat": "ou_0123456789abcdef"}),
    ("channel-lark-3", None),
], ids=["built-in", "added-bot"])
def test_a_schedule_made_through_an_added_bot_gets_no_default_notify_target(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, channel_id: str, target: Any,
) -> None:
    async def test(pool: Any) -> None:
        import json

        from looper.agents.serve import seed_channel_configs
        from tests.test_manager_schedule_changes_db import _create, _view

        await _project(pool, "calc", str(tmp_path))
        await seed_channel_configs(pool)
        sender = "ou_0123456789abcdef"
        await pool.execute("UPDATE channel_configs SET config = config || $1::jsonb WHERE channel_id = 'channel-lark'",
                           json.dumps({"allowed_open_ids": [sender]}))
        chat = _Chat(tmp_path, sender=sender, channel_id=channel_id)
        (created,) = await chat.say("建定时任务", _create())

        assert created["status"] == "ok", created
        assert (await _view("deps"))["notify_target"] == target

    run_db(test, monkeypatch)
