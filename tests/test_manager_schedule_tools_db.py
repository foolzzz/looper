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
        "(the user confirms first; an agent schedule's tasks stay; a command schedule is not deleted while its "
        "command runs: tell the user it can be deleted once the command finishes and that disabling it stops new "
        "fires; if it has a queued run, that run still starts after the running one, even when disabled; "
        "disable it only if the user asks (disabling it in the same turn after its delete was refused "
        "asks the user first); otherwise the delete "
        "removes "
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


@pytest.mark.parametrize("state", ["enabled", "queued", "enabled_and_queued"])
def test_changing_a_project_directory_asks_first_when_command_schedules_run_in_it(monkeypatch, tmp_path, state):
    async def test(pool):
        project = await _project(pool, work_dir="/srv/a")
        sid = (await _schedule(project, "pull", kind="command", command="git pull", requirement="",
                               enabled=state != "queued"))["schedule_id"]
        if state != "enabled":
            await _run_of(pool, sid, "queued")
        chat = _Chat(tmp_path)
        (asked,) = await chat.say("改默认目录", ("looper_projects", {
            "action": "update", "project": "calc", "work_dir": "/srv/b"}))
        assert asked["status"] == "needs_confirmation", asked
        shown_state = "已停用，有一次排队中的运行" if state == "queued" else "已启用"
        assert chat.turns[-1].blocks == [
            "需要你确认：\n要修改项目「calc」的默认目录吗？\n- 默认目录：/srv/a → /srv/b\n"
            "它的 1 个命令定时任务没有自己的目录，在项目的默认目录里运行；"
            "修改后，它们之后的运行都在新目录里执行（不经过 agent，不花模型额度）：\n"
            f"定时任务「pull」（{shown_state}）：\n- 命令：\n```\ngit pull\n```\n\n"
            "- 目录：/srv/a → /srv/b\n回复「确认」就执行；回复别的就不执行。"]
        assert await pool.fetchval("SELECT work_dir FROM projects WHERE id=$1", project["id"]) == "/srv/a"
        assert (await _confirm(chat, asked))["status"] == "ok"
        assert await pool.fetchval("SELECT work_dir FROM projects WHERE id=$1", project["id"]) == "/srv/b"
        assert await _trail(pool) == [("looper_projects", "high", "needs_confirmation"),
                                      ("looper_projects", "high", "ok")]

    run_db(test, monkeypatch, timeout=30)


@pytest.mark.parametrize("case", ["none", "own_work_dir", "agent_schedule", "disabled_command", "same_directory",
                                  "name_only"])
def test_a_project_directory_change_that_moves_no_command_schedule_runs_at_once(monkeypatch, tmp_path, case):
    async def test(pool):
        project = await _project(pool, work_dir="/srv/a")
        if case != "none":
            fields = {"kind": "command", "command": "git pull", "requirement": ""}
            if case == "agent_schedule":
                fields = {}
            elif case == "own_work_dir":
                fields["work_dir"] = "/srv/own"
            await _schedule(project, "pull", enabled=case != "disabled_command", **fields)
        fields = {"name": "renamed"} if case == "name_only" else {
            "work_dir": "/srv/a" if case == "same_directory" else "/srv/b"}
        chat = _Chat(tmp_path)
        (answer,) = await chat.say("改项目", ("looper_projects", {"action": "update", "project": "calc", **fields}))
        assert answer["status"] == "ok", answer
        assert "confirmation_id" not in answer and chat.turns[-1].blocks == []
        saved = await pool.fetchrow("SELECT name, work_dir FROM projects WHERE id=$1", project["id"])
        assert saved["work_dir"] == fields.get("work_dir", "/srv/a")
        assert saved["name"] == fields.get("name", "calc")
        assert await _trail(pool) == [("looper_projects", "write", "ok")]

    run_db(test, monkeypatch, timeout=30)


def test_clearing_a_project_directory_says_its_command_schedules_will_fail(monkeypatch, tmp_path):
    async def test(pool):
        project = await _project(pool, work_dir="/srv/a")
        await _schedule(project, "pull", kind="command", command="git pull", requirement="", enabled=True)
        chat = _Chat(tmp_path)
        (asked,) = await chat.say("清空目录", ("looper_projects", {
            "action": "update", "project": "calc", "work_dir": ""}))
        assert asked["status"] == "needs_confirmation", asked
        block = chat.turns[-1].blocks[0]
        assert "- 默认目录：/srv/a → 没有" in block
        assert "修改后它们没有目录可用，之后的运行都会失败：" in block
        assert await pool.fetchval("SELECT work_dir FROM projects WHERE id=$1", project["id"]) == "/srv/a"
        assert (await _confirm(chat, asked))["status"] == "ok"
        assert await pool.fetchval("SELECT work_dir FROM projects WHERE id=$1", project["id"]) == ""

    run_db(test, monkeypatch, timeout=30)


@pytest.mark.parametrize("change", ["command_edited", "env_edited", "env_file_edited", "schedule_enabled",
                                    "schedule_disabled", "directory_changed", "queued_started"])
def test_a_confirmed_directory_change_is_refused_when_what_it_showed_changed(monkeypatch, tmp_path, change):
    async def test(pool):
        from looper.agents import projects, schedules

        project = await _project(pool, work_dir="/srv/a")
        sid = (await _schedule(project, "pull", kind="command", command="git pull", requirement="",
                               enabled=change != "queued_started"))["schedule_id"]
        run_id = await _run_of(pool, sid, "queued") if change == "queued_started" else None
        chat = _Chat(tmp_path)
        arguments = {"action": "update", "project": "calc", "work_dir": "/srv/b"}
        (asked,) = await chat.say("改目录", ("looper_projects", arguments))
        assert asked["status"] == "needs_confirmation", asked
        if change == "command_edited":
            await schedules.update_schedule(sid, {"command": "git fetch"})
        elif change == "env_edited":
            await schedules.update_schedule(sid, {"env": {"MODE": "dry-run"}})
        elif change == "env_file_edited":
            env_file = tmp_path / "pull.env"
            env_file.write_text("MODE=dry-run\n")
            await schedules.update_schedule(sid, {"env_file": str(env_file)})
        elif change == "schedule_enabled":
            other = await _schedule(project, "other", kind="command", command="exit 0", requirement="")
            await schedules.update_schedule(other["schedule_id"], {"enabled": True})
        elif change == "schedule_disabled":
            await schedules.update_schedule(sid, {"enabled": False})
        elif change == "queued_started":
            await pool.execute("UPDATE schedule_runs SET status='running' WHERE id=$1", run_id)
        else:
            await pool.execute("UPDATE projects SET work_dir='/srv/c' WHERE id=$1", project["id"])
        refused = await _confirm(chat, asked)
        assert _error(refused) == "conflict", refused
        assert refused["error"]["message"] == projects.DIRECTORY_CHANGED
        unchanged = "/srv/c" if change == "directory_changed" else "/srv/a"
        assert await pool.fetchval("SELECT work_dir FROM projects WHERE id=$1", project["id"]) == unchanged
        (again,) = await chat.say("再改目录", ("looper_projects", arguments))
        assert again["status"] == ("ok" if change in ("schedule_disabled", "queued_started") else
                                   "needs_confirmation"), again

    run_db(test, monkeypatch, timeout=30)


def test_a_directory_confirmation_still_targets_the_project_after_it_is_renamed(monkeypatch, tmp_path):
    async def test(pool):
        from looper.db import projects

        project = await _project(pool, work_dir="/srv/a")
        await _schedule(project, "pull", kind="command", command="git pull", requirement="", enabled=True)
        chat = _Chat(tmp_path)
        (asked,) = await chat.say("改目录和背景", ("looper_projects", {
            "action": "update", "project": "calc", "work_dir": "/srv/b", "background": "Updated"}))
        assert asked["status"] == "needs_confirmation", asked
        assert "- 同时修改：背景" in chat.turns[-1].blocks[0]
        await projects.update_project(project["id"], name="renamed", pool=pool)
        replacement = await _project(pool, work_dir="/srv/other")
        assert (await _confirm(chat, asked))["status"] == "ok"
        saved = await projects.get_project(project["id"], pool=pool)
        assert (saved["name"], saved["work_dir"], saved["background"]) == ("renamed", "/srv/b", "Updated")
        assert (await projects.get_project(replacement["id"], pool=pool))["work_dir"] == "/srv/other"

    run_db(test, monkeypatch, timeout=30)


def test_an_invalid_project_directory_is_refused_before_confirmation(monkeypatch, tmp_path):
    async def test(pool):
        project = await _project(pool, work_dir="/srv/a")
        await _schedule(project, "pull", kind="command", command="git pull", requirement="", enabled=True)
        chat = _Chat(tmp_path)
        (answer,) = await chat.say("改目录", ("looper_projects", {
            "action": "update", "project": "calc", "work_dir": "relative/path"}))
        assert _error(answer) == "invalid", answer
        assert "absolute" in answer["error"]["message"]
        assert chat.turns[-1].blocks == []
        assert await pool.fetchval("SELECT work_dir FROM projects WHERE id=$1", project["id"]) == "/srv/a"

    run_db(test, monkeypatch, timeout=30)


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


@pytest.mark.parametrize("queued", [False, True])
def test_deleting_a_command_schedule_with_a_running_run_is_refused_before_asking(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, queued: bool,
) -> None:
    async def test(pool: Any) -> None:
        sid = (await _schedule(await _project(pool), "pull", kind="command",
                               command="exit 0", requirement=""))["schedule_id"]
        await _run_of(pool, sid, "running")
        if queued:
            await _run_of(pool, sid, "queued")
        chat = _Chat(tmp_path)

        (refused,) = await chat.say("删掉 pull", (TOOL, {"action": "delete", "schedule": "pull"}))
        assert _error(refused) == "conflict", refused
        assert refused["error"]["message"] == (
            "schedule 'pull' in project calc: a command schedule is not deleted while its command runs: "
            "it can be deleted once the command finishes; disabling it first stops new fires" + (
                "; its queued run still starts when the running one ends, even when the schedule is disabled, so "
                "deleting it waits for that run too" if queued else ""))
        assert not chat.turns[-1].blocks
        assert (await _call("POST", "/api/v1/schedule_admin/get", {"schedule_id": sid}))[0] == 200

    run_db(test, monkeypatch, timeout=30)


@pytest.mark.parametrize("queued", [False, True])
@pytest.mark.parametrize("target", ["same", "other"])
def test_disabling_right_after_a_refused_delete_asks_first(monkeypatch, tmp_path, queued, target):
    async def test(pool):
        project = await _project(pool, work_dir=str(tmp_path))
        sid = (await _schedule(project, "pull", enabled=True, kind="command", command="exit 0",
                               requirement="", overlap="queue"))["schedule_id"]
        await _run_of(pool, sid, "running")
        if queued:
            await _run_of(pool, sid, "queued")
        other = (await _schedule(project, "push", enabled=True, kind="command", command="exit 0",
                                 requirement=""))["schedule_id"] if target == "other" else None
        if other:
            await _run_of(pool, other, "running")
        chat = _Chat(tmp_path)
        refused, asked = await chat.say("删掉 pull", (TOOL, {"action": "delete", "schedule": "pull"}),
                                       (TOOL, {"action": "disable", "schedule": "push" if other else "pull"}))
        assert _error(refused) == "conflict"
        if other:
            assert asked["status"] == "ok", asked
            assert not await pool.fetchval("SELECT enabled FROM schedules WHERE id = $1", other)
            return
        assert asked["status"] == "needs_confirmation", asked
        assert chat.turns[-1].blocks == [
            "需要你确认：\n要停用定时任务「pull」（项目 calc）吗？\n"
            "- 删除没有执行：它的命令正在运行，命令结束后才能删除。\n"
            "- 停用后它不再按规则触发；正在运行的命令照常结束。" + (
                "\n- 它有一次排队中的运行：停用不会取消它，正在运行的命令结束后它就开始；它也结束后才能删除。"
                if queued else "") + "\n回复「确认」就执行；回复别的就不执行。"]
        assert await pool.fetchval("SELECT enabled FROM schedules WHERE id = $1", sid)
        done = await _confirm(chat, asked)
        assert done["status"] == "ok", done
        assert not await pool.fetchval("SELECT enabled FROM schedules WHERE id = $1", sid)
    run_db(test, monkeypatch, timeout=30)


@pytest.mark.parametrize("queued", [False, True])
@pytest.mark.parametrize("refused_delete", [False, True])
def test_disabling_a_running_command_in_a_new_turn_runs_at_once(monkeypatch, tmp_path, queued, refused_delete):
    async def test(pool):
        sid = (await _schedule(await _project(pool, work_dir=str(tmp_path)), "pull", enabled=True,
                               kind="command", command="exit 0", requirement="", overlap="queue"))["schedule_id"]
        await _run_of(pool, sid, "running")
        if queued:
            await _run_of(pool, sid, "queued")
        runs = await pool.fetch("SELECT * FROM schedule_runs WHERE schedule_id = $1 ORDER BY id", sid)
        chat = _Chat(tmp_path)
        if refused_delete:
            (refused,) = await chat.say("删掉 pull", (TOOL, {"action": "delete", "schedule": "pull"}))
            assert _error(refused) == "conflict", refused
            assert await pool.fetchval("SELECT enabled FROM schedules WHERE id = $1", sid)
        (done,) = await chat.say("停用 pull", (TOOL, {"action": "disable", "schedule": "pull"}))
        assert done["status"] == "ok", done
        assert not done["result"]["schedule"]["enabled"]
        assert not chat.turns[-1].blocks
        view = await _ok("POST", "/api/v1/schedule_admin/get", {"schedule_id": sid})
        assert view["schedule"]["enabled"] is False
        assert view["schedule"]["next_fire_at"] is None
        assert await pool.fetch("SELECT * FROM schedule_runs WHERE schedule_id = $1 ORDER BY id", sid) == runs
    run_db(test, monkeypatch, timeout=30)


def test_a_confirmed_delete_refused_after_its_command_started_asks_before_disabling(monkeypatch, tmp_path):
    async def test(pool):
        sid = (await _schedule(await _project(pool, work_dir=str(tmp_path)), "pull", enabled=True,
                               kind="command", command="exit 0", requirement=""))["schedule_id"]
        chat = _Chat(tmp_path)
        (asked,) = await chat.say("删掉 pull", (TOOL, {"action": "delete", "schedule": "pull"}))
        assert asked["status"] == "needs_confirmation", asked
        await _run_of(pool, sid, "running")
        refused, disable = await chat.say("确认", ("looper_confirm", {"confirmation_id": asked["confirmation_id"]}),
                                         (TOOL, {"action": "disable", "schedule": "pull"}))
        assert _error(refused) == "conflict", refused
        assert disable["status"] == "needs_confirmation", disable
        assert await pool.fetchval("SELECT enabled FROM schedules WHERE id = $1", sid)
    run_db(test, monkeypatch, timeout=30)


@pytest.mark.parametrize("queued", [False, True])
def test_deleting_a_command_schedule_with_a_queued_run_says_it_will_not_execute(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, queued: bool,
) -> None:
    async def test(pool: Any) -> None:
        sid = (await _schedule(await _project(pool), "pull", kind="command",
                               command="exit 0", requirement=""))["schedule_id"]
        if queued:
            await _run_of(pool, sid, "queued")
        chat = _Chat(tmp_path)

        (asked,) = await chat.say("删掉 pull", (TOOL, {"action": "delete", "schedule": "pull"}))
        assert asked["status"] == "needs_confirmation", asked
        (block,) = chat.turns[-1].blocks
        assert ("排队中的运行不会再执行" in block) is queued
        assert "它没有任务；它的运行记录和通知记录会一起删除，不能恢复。" in block
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


@pytest.mark.parametrize("action", ["enable", "run"])
@pytest.mark.parametrize("own_dir", [False, True])
def test_command_confirmation_is_bound_to_the_directory_it_runs_in(monkeypatch, tmp_path, action, own_dir):
    async def test(pool):
        from looper.agents.scheduler import Scheduler
        from looper.db import projects, schedules as db_schedules

        first, second, third = (tmp_path / name for name in ("first", "second", "third"))
        for directory in (first, second, third):
            directory.mkdir()
        project = await _project(pool, work_dir=str(first))
        sid = (await _schedule(project, "pull", kind="command", command="pwd", requirement="",
                               work_dir=str(third) if own_dir else ""))["schedule_id"]
        chat = _Chat(tmp_path)
        scheduler = Scheduler(bus=chat.bus, runtime_root=tmp_path / "runtime")
        scheduler._commands.prepare()
        chat.ops._platform = replace(chat.ops._platform, scheduler=scheduler)
        try:
            arguments = {"action": action, "schedule": sid}
            (asked,) = await chat.say("启用或立即运行", (TOOL, arguments))
            await projects.update_project(project["id"], work_dir=str(second))
            answer = await _confirm(chat, asked)
            if not own_dir:
                assert _error(answer) == "conflict", answer
                assert await _starting_state(pool, sid) == (False, 0)
                (asked,) = await chat.say("重新确认", (TOOL, arguments))
                assert f"项目的默认目录（{second}）" in chat.turns[-1].blocks[0]
                answer = await _confirm(chat, asked)
            assert answer["status"] == "ok", answer
            if action == "run":
                run_id = answer["result"]["run"]["run_id"]
                process = scheduler._commands._owned[run_id]
                await asyncio.to_thread(process.wait, timeout=10)
                await scheduler.tick()
                run = await db_schedules.get_run(run_id)
                assert run["status"] == "succeeded", run
                assert (third if own_dir else second).name in run["log_tail"]
        finally:
            await scheduler.stop()
            for process in list(scheduler._commands._owned.values()):
                if process.poll() is None:
                    process.terminate()
                await asyncio.to_thread(process.wait, timeout=10)
    run_db(test, monkeypatch, timeout=30)


@pytest.mark.parametrize("entry", ["manager", "client"])
@pytest.mark.parametrize("when", ["before-confirm", "before-lock"])
def test_command_directory_edit_confirmation_binds_the_inherited_destination(monkeypatch, tmp_path, entry, when):
    async def test(pool):
        from looper.agents.api_v1 import OPERATOR
        from looper.agents.ops.clients import ClientOps
        from looper.db import projects, schedules as db_schedules

        project = await _project(pool, work_dir="/srv/project-before")
        sid = (await _schedule(project, "pull", enabled=True, kind="command", command="pwd",
                               requirement="", work_dir=str(tmp_path)))["schedule_id"]
        before = await db_schedules.get_schedule(sid)
        chat = _Chat(tmp_path)
        client = ClientOps(chat.ops._platform)
        arguments = {"action": "update", "schedule": sid, "fields": {"work_dir": ""}}

        async def ask():
            if entry == "client":
                answer = await client.call(OPERATOR, "s1", TOOL, arguments)
                return answer, answer.get("impact", "")
            (answer,) = await chat.say("继承项目目录", (TOOL, arguments))
            return answer, chat.turns[-1].blocks[0]

        async def confirm(answer):
            if entry == "client":
                return await client.confirm(OPERATOR, "s1", answer["confirmation_id"])
            return await _confirm(chat, answer)

        asked, impact = await ask()
        assert asked["status"] == "needs_confirmation", asked
        assert "/srv/project-before" in impact
        destination = "/srv/project-after"
        if when == "before-lock":
            lock = db_schedules.lock_schedule

            async def change_before_lock(conn, schedule_id):
                monkeypatch.setattr(db_schedules, "lock_schedule", lock)
                await projects.update_project(project["id"], work_dir=destination)
                return await lock(conn, schedule_id)

            monkeypatch.setattr(db_schedules, "lock_schedule", change_before_lock)
        else:
            await projects.update_project(project["id"], work_dir=destination)
        refused = await confirm(asked)
        assert _error(refused) == "conflict", refused
        assert await db_schedules.get_schedule(sid) == before
        again, impact = await ask()
        assert again["status"] == "needs_confirmation", again
        assert destination in impact
        done = await confirm(again)
        assert done["status"] == "ok", done
        assert (await db_schedules.get_schedule(sid))["work_dir"] == ""
    run_db(test, monkeypatch, timeout=30)


@pytest.mark.parametrize("state,stored,fields", [
    ("enabled", {}, {"command": "exit 7"}),
    ("queued", {}, {"command": "exit 7"}),
    ("enabled", {"env": {"MODE": "dry-run"}}, {"env": {"MODE": "apply"}}),
    ("enabled", {"work_dir": "/srv/own"}, {"work_dir": ""}),
    ("enabled", {}, {"env_file": "settings.env"}),
])
def test_command_edit_of_a_schedule_that_will_run_asks_first(monkeypatch, tmp_path, state, stored, fields):
    async def test(pool):
        from looper.db import schedules as db_schedules

        if "env_file" in fields:
            env_file = tmp_path / fields["env_file"]
            env_file.write_text("MODE=test\n")
            fields["env_file"] = str(env_file)
        project = await _project(pool, work_dir=str(tmp_path))
        sid = (await _schedule(project, "pull", enabled=state == "enabled", kind="command", command="exit 0",
                               requirement="", **stored))["schedule_id"]
        if state == "queued":
            await _run_of(pool, sid, "queued")
        before = await db_schedules.get_schedule(sid)
        chat = _Chat(tmp_path)
        (asked,) = await chat.say("修改定时任务", (TOOL, {"action": "update", "schedule": sid, "fields": fields}))
        assert asked["status"] == "needs_confirmation", asked
        assert await db_schedules.get_schedule(sid) == before
        block = chat.turns[-1].blocks[0]
        if "work_dir" not in fields:
            assert f"- 目录：项目的默认目录（{tmp_path}）" in block
        if "command" in fields:
            assert "\n```\nexit 0\n```\n → \n```\nexit 7\n```\n" in block
        elif "env" in fields:
            assert "值已修改：MODE" in block and "dry-run" not in block and "apply" not in block
        elif "work_dir" in fields:
            assert f"/srv/own → 项目的默认目录（{tmp_path}）" in block
        else:
            assert "settings.env" in block
        effect = ("它已启用：之后的运行都按修改后的设置无人值守地执行（不经过 agent，不花模型额度）。"
                  if state == "enabled" else
                  "它是停用的，但有一次排队中的运行：那次运行开始时按修改后的设置执行（不经过 agent，不花模型额度）。")
        assert effect in block
        done = await _confirm(chat, asked)
        assert done["status"] == "ok", done
        assert "已修改定时任务" in chat.turns[-1].blocks[0]
        row = await db_schedules.get_schedule(sid)
        assert all((row[key] or None) == (value or None) for key, value in fields.items())
    run_db(test, monkeypatch, timeout=30)


@pytest.mark.parametrize("own_dir", [False, True])
def test_command_edit_directory_is_shown_again_after_project_conflict(monkeypatch, tmp_path, own_dir):
    async def test(pool):
        from looper.db import projects, schedules as db_schedules

        project = await _project(pool, work_dir="/srv/project-before")
        sid = (await _schedule(project, "pull", enabled=True, kind="command", command="exit 0", requirement="",
                               work_dir=str(tmp_path) if own_dir else ""))["schedule_id"]
        chat = _Chat(tmp_path)
        arguments = {"action": "update", "schedule": sid, "fields": {"command": "exit 7"}}
        (asked,) = await chat.say("修改命令", (TOOL, arguments))
        assert asked["status"] == "needs_confirmation", asked
        directory = str(tmp_path) if own_dir else "项目的默认目录（/srv/project-before）"
        assert f"- 目录：{directory}" in chat.turns[-1].blocks[0]
        await projects.update_project(project["id"], work_dir="/srv/project-after")
        done = await _confirm(chat, asked)
        if not own_dir:
            assert _error(done) == "conflict", done
            assert (await db_schedules.get_schedule(sid))["command"] == "exit 0"
            (asked,) = await chat.say("重新确认修改", (TOOL, arguments))
            assert asked["status"] == "needs_confirmation", asked
            assert "- 目录：项目的默认目录（/srv/project-after）" in chat.turns[-1].blocks[0]
            done = await _confirm(chat, asked)
        assert done["status"] == "ok", done
        assert (await db_schedules.get_schedule(sid))["command"] == "exit 7"
    run_db(test, monkeypatch, timeout=30)


@pytest.mark.parametrize("kind,enabled,fields", [
    ("command", False, {"command": "exit 7"}),
    ("command", True, {"command": "exit 0"}),
    ("command", True, {"env": None}),
    ("agent", True, {"requirement": "Look at the logs."}),
])
def test_command_edit_that_runs_nothing_new_applies_at_once(monkeypatch, tmp_path, kind, enabled, fields):
    async def test(pool):
        from looper.db import schedules as db_schedules

        sid = (await _schedule(await _project(pool, work_dir=str(tmp_path)), "pull", enabled=enabled, kind=kind,
                               **({"command": "exit 0", "requirement": ""} if kind == "command" else {})))["schedule_id"]
        chat = _Chat(tmp_path)
        (answer,) = await chat.say("修改定时任务", (TOOL, {"action": "update", "schedule": sid, "fields": fields}))
        assert answer["status"] == "ok", answer
        row = await db_schedules.get_schedule(sid)
        assert all((row[key] or None) == (value or None) for key, value in fields.items())
    run_db(test, monkeypatch, timeout=30)


@pytest.mark.parametrize("changed", [{"env": {"MODE": "apply"}}, {"enabled": False}])
def test_confirmed_command_edit_is_refused_when_the_schedule_changed(monkeypatch, tmp_path, changed):
    async def test(pool):
        from looper.agents import schedules
        from looper.db import schedules as db_schedules

        sid = (await _schedule(await _project(pool, work_dir=str(tmp_path)), "pull", enabled=True,
                               kind="command", command="exit 0", requirement=""))["schedule_id"]
        chat = _Chat(tmp_path)
        (asked,) = await chat.say("修改命令", (TOOL, {"action": "update", "schedule": sid,
                                                    "fields": {"command": "exit 7"}}))
        assert asked["status"] == "needs_confirmation", asked
        await schedules.update_schedule(sid, changed)
        refused = await _confirm(chat, asked)
        assert _error(refused) == "conflict", refused
        assert refused["error"]["message"] == "the schedule configuration changed; run the tool again to ask for confirmation"
        assert (await db_schedules.get_schedule(sid))["command"] == "exit 0"
    run_db(test, monkeypatch, timeout=30)


@pytest.mark.parametrize("fields", [{"command": ""}, {"command": "exit 7", "cron": "not a rule"}, {"work_dir": ""}])
def test_invalid_command_edit_is_refused_before_confirmation(monkeypatch, tmp_path, fields):
    async def test(pool):
        from looper.db import schedules as db_schedules

        project = await _project(pool, work_dir="")
        sid = (await _schedule(project, "pull", enabled=True, kind="command", command="exit 0",
                               requirement="", work_dir=str(tmp_path)))["schedule_id"]
        before = await db_schedules.get_schedule(sid)
        chat = _Chat(tmp_path)
        (answer,) = await chat.say("修改命令", (TOOL, {"action": "update", "schedule": sid, "fields": fields}))
        assert _error(answer) == "invalid", answer
        assert not chat.turns[-1].blocks
        assert await db_schedules.get_schedule(sid) == before
    run_db(test, monkeypatch, timeout=30)


def test_queued_run_created_after_edit_question_executes_the_confirmed_command(monkeypatch, tmp_path):
    async def test(pool):
        from looper.agents.scheduler import Scheduler
        from looper.db import schedules as db_schedules

        sid = (await _schedule(await _project(pool, work_dir=str(tmp_path)), "pull", enabled=True,
                               kind="command", command="exit 0", requirement=""))["schedule_id"]
        chat = _Chat(tmp_path)
        scheduler = Scheduler(bus=chat.bus, runtime_root=tmp_path / "runtime")
        scheduler._commands.prepare()
        try:
            (asked,) = await chat.say("修改命令", (TOOL, {"action": "update", "schedule": sid,
                                                        "fields": {"command": "exit 7"}}))
            assert asked["status"] == "needs_confirmation", asked
            run_id = await _run_of(pool, sid, "queued")
            done = await _confirm(chat, asked)
            assert done["status"] == "ok", done
            await scheduler.tick()
            await asyncio.to_thread(scheduler._commands._owned[run_id].wait, timeout=10)
            await scheduler.tick()
            run = await db_schedules.get_run(run_id)
            assert (run["status"], run["exit_code"]) == ("failed", 7)
        finally:
            await scheduler.stop()
            for process in list(scheduler._commands._owned.values()):
                if process.poll() is None:
                    process.terminate()
                await asyncio.to_thread(process.wait, timeout=10)
    run_db(test, monkeypatch, timeout=30)
