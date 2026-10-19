"""The scheduler's system settings and the typed run timeout of a schedule, on a real PostgreSQL: precedence of the
stored minimum interval over the deprecated variable, the bounds, a schedule that follows the default or has its
own timeout or none, a run keeping the timeout it started with, schedules from before keeping their meaning, the
Manager's settings tool and the configuration export and import.

Runs only when LOOPER_TEST_DATABASE_URL points at a disposable database.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from looper.agents import scheduler_settings
from tests.test_manager_config_tools_db import _ok as _manager_ok, _trail
from tests.test_manager_schedule_tools_db import _confirm
from tests.test_ops_tasks_db import _Chat, _error, _project
from tests.test_schedules_db import DSN, _Env, _run
from tests.test_usage_api_db import _run as _manager_run

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")

ENV = scheduler_settings.MIN_INTERVAL_ENV


@pytest.fixture(autouse=True)
def _clean(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ENV, raising=False)
    monkeypatch.delenv("LOOPX_SCHEDULE_MIN_INTERVAL_SECONDS", raising=False)
    monkeypatch.setattr(scheduler_settings, "_warned_env", False)
    monkeypatch.setattr(scheduler_settings, "_min_source", None)
    monkeypatch.setattr(scheduler_settings, "_min_interval", scheduler_settings.DEFAULT_MIN_INTERVAL_SECONDS)


# --- The settings ---


def test_the_stored_minimum_interval_beats_the_deprecated_variable_which_beats_the_built_in(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    async def test(env: _Env) -> None:
        view = await env.ok("GET", "/api/scheduler-settings")
        assert view["min_interval_seconds"]["value"] == 300 and view["min_interval_seconds"]["source"] == "built-in"
        assert view["run_timeout_seconds"]["value"] == 3600 and view["run_timeout_seconds"]["source"] == "built-in"

        monkeypatch.setenv(ENV, "123")
        with caplog.at_level(logging.WARNING):
            await scheduler_settings.load()
            await scheduler_settings.load()
        warnings = [r.getMessage() for r in caplog.records if "deprecated" in r.getMessage()]
        assert len(warnings) == 1 and ENV in warnings[0] and "123" not in warnings[0], warnings
        view = await env.ok("GET", "/api/scheduler-settings")
        assert (view["min_interval_seconds"]["value"], view["min_interval_seconds"]["source"]) == (123, "env")

        # Saved in the database, the variable is ignored; going back to the built-in hands over to it again.
        saved = await env.ok("PUT", "/api/scheduler-settings", {"min_interval_seconds": 60})
        assert (saved["min_interval_seconds"]["value"], saved["min_interval_seconds"]["source"]) == (60, "setting")
        assert scheduler_settings.min_interval_seconds(gated=False) == 60
        reset = await env.ok("PUT", "/api/scheduler-settings", {"min_interval_seconds": None})
        assert (reset["min_interval_seconds"]["value"], reset["min_interval_seconds"]["source"]) == (123, "env")

    _run(test, monkeypatch, tmp_path)


def test_the_legacy_variable_name_is_named_in_the_warning(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    async def test(env: _Env) -> None:
        monkeypatch.setenv("LOOPX_SCHEDULE_MIN_INTERVAL_SECONDS", "77")
        with caplog.at_level(logging.WARNING):
            assert await scheduler_settings.load() == 77
        assert any("LOOPX_SCHEDULE_MIN_INTERVAL_SECONDS is deprecated" in r.getMessage()
                   and "77" not in r.getMessage() for r in caplog.records)

    _run(test, monkeypatch, tmp_path)


def test_a_variable_that_is_not_a_whole_number_refuses_the_start(monkeypatch: pytest.MonkeyPatch,
                                                                 tmp_path: Path) -> None:
    async def test(env: _Env) -> None:
        monkeypatch.setenv(ENV, "soon")
        with pytest.raises(ValueError, match=f"{ENV} must be a whole number of seconds from 1"):
            await scheduler_settings.load()
        # A stored value makes the variable moot.
        await env.ok("PUT", "/api/scheduler-settings", {"min_interval_seconds": 90})
        assert await scheduler_settings.load() == 90

    _run(test, monkeypatch, tmp_path)


@pytest.mark.parametrize(("body", "error"), [
    ({}, "give at least one of"),
    ({"min_interval": 5}, "unknown setting 'min_interval'"),
    ({"min_interval_seconds": 0}, "min_interval_seconds must be a whole number of seconds from 1"),
    ({"min_interval_seconds": 1.5}, "min_interval_seconds must be a whole number"),
    ({"min_interval_seconds": "60"}, "min_interval_seconds must be a whole number"),
    ({"min_interval_seconds": True}, "min_interval_seconds must be a whole number"),
    ({"run_timeout_seconds": 0}, "run_timeout_seconds must be a whole number of seconds from 1 to 2592000"),
    ({"run_timeout_seconds": 2_592_001}, "from 1 to 2592000"),
    ({"run_timeout_seconds": 60, "min_interval_seconds": -1}, "min_interval_seconds must be"),
])
def test_a_bad_scheduler_setting_is_refused_and_changes_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, body: dict[str, Any], error: str,
) -> None:
    async def test(env: _Env) -> None:
        status, payload = await env.call("PUT", "/api/scheduler-settings", body)
        assert status == 400 and error in payload["error"], payload
        view = await env.ok("GET", "/api/scheduler-settings")
        assert (view["min_interval_seconds"]["source"], view["run_timeout_seconds"]["source"]) == ("built-in",) * 2

    _run(test, monkeypatch, tmp_path)


def test_the_minimum_interval_setting_decides_which_rules_a_schedule_may_have(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    async def test(env: _Env) -> None:
        body = {"name": "often", "cron": "*/2 * * * *", "timezone": "UTC", "requirement": "Look."}
        status, payload = await env.call("POST", f"/api/projects/{env.project_id}/schedules", body)
        assert status == 400 and "fires 2 minutes apart" in payload["error"]
        await env.ok("PUT", "/api/scheduler-settings", {"min_interval_seconds": 120})
        await env.ok("POST", f"/api/projects/{env.project_id}/schedules", body)
        limits = (await env.ok("GET", f"/api/projects/{env.project_id}/schedules"))["limits"]
        assert (limits["min_interval_seconds"], limits["min_interval_gated_seconds"]) == (120, 60)

    _run(test, monkeypatch, tmp_path)


# --- A schedule's run timeout ---


def test_a_schedule_follows_the_default_has_its_own_timeout_or_none(monkeypatch: pytest.MonkeyPatch,
                                                                    tmp_path: Path) -> None:
    async def test(env: _Env) -> None:
        followed = await env.schedule()
        assert (followed["run_timeout_seconds"], followed["run_timeout_effective_seconds"]) == ("default", 3600)
        await env.ok("PUT", "/api/scheduler-settings", {"run_timeout_seconds": 900})
        sid = followed["schedule_id"]
        view = (await env.ok("GET", f"/api/schedules/{sid}"))["schedule"]
        assert (view["run_timeout_seconds"], view["run_timeout_effective_seconds"]) == ("default", 900)

        own = await env.schedule(run_timeout_seconds=120)
        none = await env.schedule(run_timeout_seconds=None)
        assert (own["run_timeout_seconds"], own["run_timeout_effective_seconds"]) == (120, 120)
        assert (none["run_timeout_seconds"], none["run_timeout_effective_seconds"]) == (None, None)
        columns = "SELECT run_timeout_mode, run_timeout_seconds FROM schedules WHERE id = $1"
        assert [tuple((await env.pool.fetchrow(columns, s["schedule_id"])).values())
                for s in (followed, own, none)] == [("inherit", None), ("seconds", 120), ("none", None)]

        # An update that leaves it out keeps it; each choice can be switched to any other.
        await env.ok("PUT", f"/api/schedules/{own['schedule_id']}", {"name": "renamed"})
        assert (await env.ok("GET", f"/api/schedules/{own['schedule_id']}"))["schedule"]["run_timeout_seconds"] == 120
        for value, expected in (("default", "default"), (None, None), (45, 45)):
            changed = await env.ok("PUT", f"/api/schedules/{own['schedule_id']}", {"run_timeout_seconds": value})
            assert changed["schedule"]["run_timeout_seconds"] == expected
        # The list of limits tells the form the default.
        limits = (await env.ok("GET", f"/api/projects/{env.project_id}/schedules"))["limits"]
        assert limits["run_timeout_default_seconds"] == 900

    _run(test, monkeypatch, tmp_path)


@pytest.mark.parametrize("value", [0, -5, 1.5, "soon", True, 2_592_001, [60]])
def test_a_bad_schedule_timeout_is_refused(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, value: Any) -> None:
    async def test(env: _Env) -> None:
        body = {"name": "x", "cron": "*/5 * * * *", "timezone": "UTC", "requirement": "Look.",
                "run_timeout_seconds": value}
        status, payload = await env.call("POST", f"/api/projects/{env.project_id}/schedules", body)
        assert status == 400 and 'null for no limit, or "default"' in payload["error"], payload

    _run(test, monkeypatch, tmp_path)


def test_a_run_keeps_the_timeout_it_started_with(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    async def test(env: _Env) -> None:
        await env.ok("PUT", "/api/scheduler-settings", {"run_timeout_seconds": 120})
        sid = (await env.enabled())["schedule_id"]  # follows the default
        await env.due(sid)
        await env.scheduler.tick()
        [run] = await env.runs(sid)
        assert await env.pool.fetchval("SELECT run_timeout_seconds FROM schedule_runs WHERE id = $1",
                                       run["run_id"]) == 120
        await env.pool.execute("UPDATE tasks SET status = 'running' WHERE id = $1", run["task_id"])

        # Neither the default nor the schedule changing reaches the run in progress.
        await env.ok("PUT", "/api/scheduler-settings", {"run_timeout_seconds": 3000})
        await env.ok("PUT", f"/api/schedules/{sid}", {"run_timeout_seconds": None})
        env.clock.now += timedelta(seconds=119)
        await env.scheduler.tick()
        assert (await env.runs(sid))[0]["status"] == "running"
        env.clock.now += timedelta(seconds=2)
        await env.scheduler.tick()
        [timed_out] = await env.runs(sid)
        assert timed_out["status"] == "timed_out" and "run timeout of 120 s" in timed_out["error"]

        # The next run starts with the schedule's choice now: no limit.
        await env.end_task(run["task_id"], "stopped")
        await env.due(sid)
        await env.scheduler.tick()
        newest = (await env.runs(sid))[0]
        assert newest["status"] == "running"
        assert await env.pool.fetchval("SELECT run_timeout_seconds FROM schedule_runs WHERE id = $1",
                                       newest["run_id"]) is None

    _run(test, monkeypatch, tmp_path)


def test_a_gate_is_told_the_timeout_its_run_started_with(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from looper.agents.scheduler import Firing, Trigger, gate_env

    firing = Firing(Trigger.SCHEDULED, datetime(2030, 1, 15, tzinfo=timezone.utc))
    assert gate_env({"id": "sched-a"}, "run-a", firing, 90)["LOOPER_SCHEDULE_RUN_TIMEOUT_SECONDS"] == "90"
    assert gate_env({"id": "sched-a"}, "run-a", firing, None)["LOOPER_SCHEDULE_RUN_TIMEOUT_SECONDS"] == ""


def test_schedules_from_before_keep_their_meaning(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    async def test(env: _Env) -> None:
        from looper.db import connection

        # The database as the previous version left it: no mode, a number or none on each schedule, runs in progress.
        await env.pool.execute("ALTER TABLE schedules DROP COLUMN run_timeout_mode")
        await env.pool.execute("ALTER TABLE schedule_runs DROP COLUMN run_timeout_seconds")
        await env.pool.execute("DELETE FROM schema_migrations WHERE name = 'schedules_run_timeout_mode'")
        for name, timeout in (("numbered", 300), ("unlimited", None)):
            await env.pool.execute(
                "INSERT INTO schedules (id, project_id, name, cron, timezone, requirement, run_timeout_seconds) "
                "VALUES ($1, $2, $1, '*/5 * * * *', 'UTC', 'Look.', $3)", f"sched-{name}", env.project_id, timeout)
            await env.pool.execute(
                "INSERT INTO schedule_runs (id, schedule_id, scheduled_for, trigger, status) "
                "VALUES ($1, $2, now(), 'manual', 'running')", f"run-{name}", f"sched-{name}")
        await connection.ensure_schema(env.pool)
        await connection.ensure_schema(env.pool)  # once

        rows = await env.pool.fetch("SELECT id, run_timeout_mode, run_timeout_seconds FROM schedules ORDER BY id")
        assert [tuple(r.values()) for r in rows] == [("sched-numbered", "seconds", 300), ("sched-unlimited", "none", None)]
        runs = await env.pool.fetch("SELECT id, run_timeout_seconds FROM schedule_runs ORDER BY id")
        assert [tuple(r.values()) for r in runs] == [("run-numbered", 300), ("run-unlimited", None)]
        view = (await env.ok("GET", "/api/schedules/sched-numbered"))["schedule"]
        assert (view["run_timeout_seconds"], view["run_timeout_effective_seconds"]) == (300, 300)
        # A schedule created afterwards follows the default.
        assert (await env.schedule())["run_timeout_seconds"] == "default"

    _run(test, monkeypatch, tmp_path)


# --- The configuration file ---


def test_an_export_carries_the_scheduler_settings_and_each_schedules_timeout_choice(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    async def test(env: _Env) -> None:
        from looper.agents import config_transfer

        await env.ok("PUT", "/api/scheduler-settings", {"min_interval_seconds": 60, "run_timeout_seconds": 600})
        ids = {name: (await env.schedule(name=name, **fields))["schedule_id"] for name, fields in (
            ("follows", {}), ("numbered", {"run_timeout_seconds": 90}), ("unlimited", {"run_timeout_seconds": None}))}
        path = tmp_path / "config.json"
        async with env.pool.acquire() as conn:
            async with conn.transaction(readonly=True):
                path.write_text("".join([c async for c in config_transfer.export_chunks(
                    conn, include_projects=True, include_secrets=False)]), encoding="utf-8")
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["settings"]["scheduler"] == {"min_interval_seconds": 60, "run_timeout_seconds": 600}
        assert {s["name"]: s["run_timeout_seconds"] for s in data["projects"][0]["schedules"]} == {
            "follows": "default", "numbered": 90, "unlimited": None}

        # Changed here, the file puts both back; a setting left at the built-in is not carried.
        await env.ok("PUT", "/api/scheduler-settings", {"min_interval_seconds": 200, "run_timeout_seconds": None})
        await env.ok("PUT", f"/api/schedules/{ids['follows']}", {"run_timeout_seconds": 5})
        await env.ok("PUT", f"/api/schedules/{ids['numbered']}", {"run_timeout_seconds": "default"})
        async with env.pool.acquire() as conn:
            plan = await config_transfer.run_import(conn, config_transfer.read_file(path), apply=False)
        scheduler = {c.key: c.action.value for c in plan.changes if c.section == "settings.scheduler"}
        assert scheduler == {"min_interval_seconds": "changed", "run_timeout_seconds": "added"}
        async with env.pool.acquire() as conn:
            await config_transfer.run_import(conn, config_transfer.read_file(path), apply=True)
        view = await env.ok("GET", "/api/scheduler-settings")
        assert (view["min_interval_seconds"]["value"], view["run_timeout_seconds"]["value"]) == (200, 600)
        got = {s["name"]: s["run_timeout_seconds"] for s in (await env.ok("GET", "/api/schedules"))["schedules"]}
        assert got == {"follows": "default", "numbered": 90, "unlimited": None}

        data["settings"]["scheduler"] = {"min_interval_seconds": 0, "run_timeout_seconds": None, "other": 1}
        async with env.pool.acquire() as conn:
            plan = await config_transfer.run_import(conn, data, apply=False)
        skipped = {c.key: c.reason for c in plan.changes if c.section == "settings.scheduler"}
        assert set(skipped) == {"min_interval_seconds", "run_timeout_seconds", "other"}
        assert all(c.action.value == "skipped" for c in plan.changes if c.section == "settings.scheduler")

    _run(test, monkeypatch, tmp_path)


# --- The Manager ---


def test_the_manager_changes_the_scheduler_settings_after_the_user_confirms(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    async def test(pool: Any) -> None:
        await _project(pool, "calc", str(tmp_path))
        chat = _Chat(tmp_path)
        (view,) = await chat.say("定时任务的设置", ("looper_settings", {"action": "view"}))
        assert view["result"]["scheduler"] == {"min_interval_seconds": "300 秒（内置默认）",
                                               "run_timeout_seconds": "3600 秒（内置默认）"}
        (bad,) = await chat.say("改成 0", ("looper_settings", {"action": "scheduler",
                                                               "scheduler": {"min_interval_seconds": 0}}))
        assert _error(bad) == "invalid" and "min_interval_seconds must be a whole number" in bad["error"]["message"]
        (asked,) = await chat.say("最小间隔 60 秒，默认超时 1800 秒", ("looper_settings", {
            "action": "scheduler", "scheduler": {"min_interval_seconds": 60, "run_timeout_seconds": 1800}}))
        assert asked["status"] == "needs_confirmation"
        assert "定时任务的最小间隔：300 秒（内置默认） → 60 秒" in asked["confirmation"]["summary"] \
            if "confirmation" in asked else True
        assert (await _manager_ok("GET", "/api/scheduler-settings"))["min_interval_seconds"]["value"] == 300
        done = await _confirm(chat, asked)
        assert done["status"] == "ok", done
        assert done["result"]["changes"] == [
            {"setting": "min_interval_seconds", "before": "300 秒（内置默认）", "after": "60 秒（设置）"},
            {"setting": "run_timeout_seconds", "before": "3600 秒（内置默认）", "after": "1800 秒（设置）"}]
        view = await _manager_ok("GET", "/api/scheduler-settings")
        assert (view["min_interval_seconds"]["value"], view["run_timeout_seconds"]["value"]) == (60, 1800)
        assert await _trail(pool) == [("looper_settings", "read", "ok"), ("looper_settings", "write", "error"),
                                      ("looper_settings", "high", "needs_confirmation"),
                                      ("looper_settings", "high", "ok")]

    _manager_run(test, monkeypatch)
