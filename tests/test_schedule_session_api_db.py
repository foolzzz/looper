"""Schedule Planner session visibility, reset and configuration boundaries on PostgreSQL."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from tests.db_support import DSN, run_db
from tests.test_catalog_db import _call
from tests.test_manager_config_tools_db import _trail
from tests.test_manager_schedule_tools_db import _confirm, _schedule
from tests.test_ops_tasks_db import _Chat, _project, _task

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")

SESSION_COLUMNS = ("planner_session_id", "planner_session_totals", "planner_session_runtime",
                   "planner_session_provider", "planner_session_profile", "planner_session_task_id",
                   "planner_session_since")
SINCE = datetime(2026, 10, 19, 1, tzinfo=timezone.utc)


async def _stored(pool: Any, schedule_id: str, task_id: str) -> None:
    await pool.execute(
        "UPDATE schedules SET planner_session_id = 'private-session', planner_session_totals = '{\"cost_usd\": 1}', "
        "planner_session_runtime = 'claude-code', planner_session_provider = 'prov-test', "
        "planner_session_profile = 'prof-test', planner_session_task_id = $2, planner_session_since = $3 WHERE id = $1",
        schedule_id, task_id, SINCE)


def test_schedule_api_shows_session_metadata_without_its_id_and_reset_clears_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def test(pool: Any) -> None:
        project = await _project(pool)
        sid = (await _schedule(project, "nightly"))["schedule_id"]
        await _task(pool, "task-session", "running", project)
        await pool.execute("UPDATE tasks SET title = 'Nightly run' WHERE id = 'task-session'")
        await _stored(pool, sid, "task-session")
        expected = {"active": True, "runtime": "claude-code", "profile": "prof-test",
                    "last_task_id": "task-session", "last_task_title": "Nightly run",
                    "updated_at": SINCE.isoformat()}
        for action, arguments in (("get", {"schedule_id": sid}), ("list", {}),
                                  ("project_list", {"project_id": project["id"]})):
            status, body = await _call("POST", f"/api/v1/schedule_admin/{action}", arguments)
            assert status == 200, body
            view = body["schedule"] if "schedule" in body else body["schedules"][0]
            assert view["planner_session"] == expected
            assert "private-session" not in json.dumps(body)
            assert not any(key.startswith("planner_session_") for key in view)

        await pool.execute("UPDATE tasks SET title = NULL, goal_id = 'Nightly goal' WHERE id = 'task-session'")
        status, body = await _call("POST", "/api/v1/schedule_admin/get", {"schedule_id": sid})
        assert status == 200 and body["schedule"]["planner_session"]["last_task_title"] == "Nightly goal"

        assert await _call("POST", "/api/v1/schedule_admin/reset_planner_session", {"schedule_id": sid}) == (200, {"cleared": True})
        row = await pool.fetchrow("SELECT * FROM schedules WHERE id = $1", sid)
        assert all(row[key] is None for key in SESSION_COLUMNS if key != "planner_session_totals")
        assert json.loads(row["planner_session_totals"]) == {}
        assert await _call("POST", "/api/v1/schedule_admin/reset_planner_session", {"schedule_id": sid}) == (200, {"cleared": False})
        status, body = await _call("POST", "/api/v1/schedule_admin/get", {"schedule_id": sid})
        assert status == 200
        assert body["schedule"]["planner_session"] == {key: False if key == "active" else None for key in expected}
        assert (await _call("POST", "/api/v1/schedule_admin/reset_planner_session", {"schedule_id": 'sched-missing'}))[0] == 404

    run_db(test, monkeypatch)


def test_task_api_exposes_whether_its_planner_continued_the_schedule_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def test(pool: Any) -> None:
        project = await _project(pool)
        for task_id, continues in (("task-fresh", False), ("task-continued", True)):
            await _task(pool, task_id, "running", project)
            if continues:
                await pool.execute("UPDATE tasks SET continues_schedule_session = true WHERE id = $1", task_id)
            status, body = await _call("GET", f"/api/tasks/{task_id}")
            assert status == 200 and body["continues_schedule_session"] is continues
        status, body = await _call("GET", "/api/tasks")
        assert status == 200
        assert {task["task_id"]: task["continues_schedule_session"] for task in body["tasks"]} == {
            "task-fresh": False, "task-continued": True}

    run_db(test, monkeypatch)


@pytest.mark.parametrize("stored", [False, True])
def test_manager_reset_asks_for_next_message_confirmation_before_clearing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, stored: bool,
) -> None:
    async def test(pool: Any) -> None:
        project = await _project(pool)
        sid = (await _schedule(project, "nightly"))["schedule_id"]
        await _task(pool, "task-session", "running", project)
        if stored:
            await _stored(pool, sid, "task-session")
        chat = _Chat(tmp_path)
        (asked,) = await chat.say("重置 nightly 的规划者会话", ("looper_schedules", {
            "action": "reset_planner_session", "schedule": sid}))
        assert asked["status"] == "needs_confirmation", asked
        assert bool(await pool.fetchval("SELECT planner_session_id FROM schedules WHERE id = $1", sid)) is stored
        block = "\n".join(chat.turns[-1].blocks)
        assert "下一次运行从新会话开始" in block
        assert "正在进行的运行" in block and "不会写回" in block
        if not stored:
            assert "当前没有已存会话" in block
        answer = await _confirm(chat, asked)
        assert answer["status"] == "ok", answer
        assert answer["result"]["cleared"] is stored
        assert await pool.fetchval("SELECT planner_session_id FROM schedules WHERE id = $1", sid) is None
        assert await _trail(pool) == [("looper_schedules", "high", "needs_confirmation"),
                                      ("looper_schedules", "high", "ok")]

    run_db(test, monkeypatch)


def test_configuration_export_excludes_session_state_and_import_skips_unknown_session_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from looper.agents.config_transfer import export_chunks, import_config

    async def test(pool: Any) -> None:
        project = await _project(pool)
        sid = (await _schedule(project, "nightly"))["schedule_id"]
        await _task(pool, "task-session", "completed", project)
        await _stored(pool, sid, "task-session")
        async with pool.acquire() as conn:
            exported = json.loads("".join([chunk async for chunk in export_chunks(
                conn, include_projects=True, include_secrets=False)]))
            entry = exported["projects"][0]["schedules"][0]
            assert not set(SESSION_COLUMNS) & entry.keys()
            assert "planner_session" not in entry
            assert "private-session" not in json.dumps(exported)
            for column in SESSION_COLUMNS:
                name = "invalid-" + column
                async with conn.transaction():
                    plan = await import_config(conn, {"format": "looper-config/v2", "projects": [
                        {"name": "calc", "schedules": [{**entry, "name": name, column: "untrusted"}]}]}, apply=True)
                changes = [change for change in plan.changes if change.section == "project.schedules"]
                assert len(changes) == 1
                assert changes[0].action.value == "skipped"
                assert changes[0].reason == f"unknown field {column!r}"
                assert await conn.fetchval("SELECT count(*) FROM schedules WHERE name = $1", name) == 0
        assert await pool.fetchval("SELECT planner_session_id FROM schedules WHERE id = $1", sid) == "private-session"

    run_db(test, monkeypatch)
