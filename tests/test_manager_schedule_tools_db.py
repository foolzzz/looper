"""The Manager's schedules tool on a real PostgreSQL: it lists and reads what the Schedules page reads, enables and
disables as the page's switch does, enables, runs and deletes only on the user's word in the next message, names a
schedule by id or by name, and leaves the trail.

Each call goes through ``ManagerOps`` in a turn of a Lark chat (``tests.test_ops_tasks_db._Chat``); what changed is
read back through the dashboard's HTTP API. Runs only when LOOPER_TEST_DATABASE_URL points at a disposable database.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from tests.test_manager_config_tools_db import _ok, _trail
from tests.test_manager_ops import _Clock
from tests.test_ops_tasks_db import _Chat, _error, _project, _task
from tests.test_task_agents_db import _call
from tests.test_usage_api_db import DSN, _run

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")

TOOL = "looper_schedules"


async def _confirm(chat: _Chat, asked: dict[str, Any]) -> dict[str, Any]:
    """The user confirms in the very next message, whose note names what will run."""
    assert asked["status"] == "needs_confirmation", asked
    (answer,) = await chat.say("确认", ("looper_confirm", {"confirmation_id": asked["confirmation_id"]}))
    assert asked["confirmation_id"] in chat.notes[-1]
    return answer


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
    schedule = (await _ok("GET", f"/api/schedules/{sid}"))["schedule"]
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
        page = (await _ok("GET", "/api/schedules"))["schedules"]
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

    _run(test, monkeypatch)


def test_a_schedule_is_enabled_and_disabled_as_its_switch_does_in_a_turn_answering_the_user(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    async def test(pool: Any) -> None:
        sid = (await _schedule(await _project(pool), "nightly", "0 2 * * *"))["schedule_id"]
        chat = _Chat(tmp_path)

        # A turn Claude runs on its own only reads.
        refused = await chat.ops.call(TOOL, {"action": "enable", "schedule": "nightly"})
        assert _error(refused) == "needs_user_turn"
        assert (await _ok("GET", f"/api/schedules/{sid}"))["schedule"]["enabled"] is False

        (asked,) = await chat.say("启用 nightly", (TOOL, {"action": "enable", "schedule": "nightly"}))
        assert await _starting_state(pool, sid) == (False, 0)
        on = await _confirm(chat, asked)
        assert on["status"] == "ok", on
        schedule = (await _ok("GET", f"/api/schedules/{sid}"))["schedule"]
        assert schedule["enabled"] is True and "note" not in on["result"]
        assert datetime.fromisoformat(on["result"]["schedule"]["next_fire_at"]) == \
            datetime.fromisoformat(schedule["next_fire_at"])
        (again,) = await chat.say("再启用一次", (TOOL, {"action": "enable", "schedule": sid}))
        assert again["status"] == "ok" and "confirmation_id" not in again
        assert again["result"]["note"] == "it was enabled already"
        assert chat.turns[-1].blocks == []
        (off,) = await chat.say("停用 nightly", (TOOL, {"action": "disable", "schedule": "NIGHTLY"}))
        schedule = (await _ok("GET", f"/api/schedules/{sid}"))["schedule"]
        assert (schedule["enabled"], schedule["next_fire_at"], off["result"]["schedule"]["enabled"]) == (
            False, None, False)
        assert await _trail(pool) == [(TOOL, "write", "error"), (TOOL, "high", "needs_confirmation"),
                                      (TOOL, "high", "ok"), *[(TOOL, "write", "ok")] * 2]
        assert await pool.fetchval("SELECT count(*) FROM manager_actions WHERE status = 'ok' AND result IS NULL") == 0

    _run(test, monkeypatch)


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
        status, dashboard = await _call("PUT", f"/api/schedules/{sid}", {"enabled": True})
        assert status == 400 and dashboard["error"] == answer["error"]["message"]
        assert await _starting_state(pool, sid) == (False, 0)
        assert await _trail(pool) == [(TOOL, "high", "needs_confirmation"), (TOOL, "high", "error")]
        assert tuple(await pool.fetchrow(
            "SELECT confirmation_id, error_code FROM manager_actions ORDER BY id DESC LIMIT 1")) == (
                asked["confirmation_id"], "invalid")

    _run(test, monkeypatch)


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

            done = await _confirm(chat, asked)
            assert done["status"] == "ok", done
            assert await _starting_state(pool, sid) == (action == "enable", int(action == "run"))
            if action == "run":
                runs = (await _ok("GET", f"/api/schedules/{sid}/runs"))["runs"]
                assert [(r["trigger"], r["status"]) for r in runs] == [("manual", "running")]
                assert runs[0]["run_id"] == done["result"]["run"]["run_id"]
            assert await _trail(pool) == [(TOOL, "high", "needs_confirmation"), (TOOL, "high", "ok")]
            assert [tuple(r) for r in await pool.fetch(
                "SELECT confirmation_id, message_id FROM manager_actions ORDER BY id")] == [
                    (asked["confirmation_id"], turn.message_id) for turn in chat.turns]

            (reused,) = await chat.say("再确认", ("looper_confirm", {"confirmation_id": asked["confirmation_id"]}))
            assert _error(reused) == "not_found"
            assert await _starting_state(pool, sid) == (action == "enable", int(action == "run"))

    _run(test, monkeypatch)


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

    _run(test, monkeypatch)


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

    _run(test, monkeypatch)


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
        assert (await _call("GET", f"/api/schedules/{sid}"))[0] == 200
        (lapsed,) = await chat.say("确认", ("looper_confirm", {"confirmation_id": asked["confirmation_id"]}))
        assert _error(lapsed) == "not_found"
        assert (await _call("GET", f"/api/schedules/{sid}"))[0] == 200

        (again,) = await chat.say("还是删掉 nightly", (TOOL, {"action": "delete", "schedule": "nightly"}))
        (done,) = await chat.say("确认", ("looper_confirm", {"confirmation_id": again["confirmation_id"]}))
        assert done == {"status": "ok", "result": {
            "deleted": "nightly", "schedule_id": sid, "project": "calc",
            "note": "the tasks it created stay; its run history was deleted with it"}}
        assert (await _call("GET", f"/api/schedules/{sid}"))[0] == 404
        task = await _ok("GET", "/api/tasks/task-run")
        assert (task["status"], task["schedule_id"]) == ("running", None)
        assert await pool.fetchval("SELECT count(*) FROM schedule_runs") == 0
        assert await _trail(pool) == [(TOOL, "high", "needs_confirmation"), ("looper_confirm", "high", "error"),
                                      (TOOL, "high", "needs_confirmation"), (TOOL, "high", "ok")]

    _run(test, monkeypatch)


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

    _run(test, monkeypatch)


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

    _run(test, monkeypatch)


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

    _run(test, monkeypatch)
