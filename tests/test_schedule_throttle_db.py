"""A schedule's throttle fields on a real PostgreSQL: outside its active hours a firing does not happen (no run, the
next fire moves on), an idle_only schedule's firing while its Manager answers a message is skipped_busy, a manual
run is never throttled, the API stores and shows them, an older database gets them on upgrade, and an export
carries them only when set.

Runs only when LOOPER_TEST_DATABASE_URL points at a disposable database.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from tests.db_support import DSN, run_db
from tests.schedule_support import ScheduleEnv, run_scheduled

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")

LA = ZoneInfo("America/Los_Angeles")  # the schedules' zone; the clock starts at noon there


def test_outside_its_active_hours_a_schedule_does_not_fire(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    async def test(env: ScheduleEnv) -> None:
        sid = (await env.enabled(active_hours="13:00-17:00"))["schedule_id"]
        await env.due(sid)
        due_at = await env.pool.fetchval("SELECT next_fire_at FROM schedules WHERE id = $1", sid)
        await env.scheduler.tick()
        assert await env.runs(sid) == [] and env.bus.of("task_start") == []
        row = await env.pool.fetchrow("SELECT last_fired_at, next_fire_at FROM schedules WHERE id = $1", sid)
        assert row["last_fired_at"] is None and row["next_fire_at"] == due_at + timedelta(minutes=5)

        # Hours that cross midnight and hold the clock's 12:05: it fires.
        await env.ok("POST", "/api/v1/schedule_admin/update", {"schedule_id": sid,
                                                               "fields": {"active_hours": "22:00-12:30"}})
        await env.due(sid)
        await env.scheduler.tick()
        [run] = await env.runs(sid)
        assert (run["trigger"], run["status"]) == ("scheduled", "running")

    run_scheduled(test, monkeypatch, tmp_path)


def test_a_catch_up_that_would_run_outside_the_active_hours_does_not(monkeypatch: pytest.MonkeyPatch,
                                                                    tmp_path: Path) -> None:
    async def test(env: ScheduleEnv) -> None:
        sid = (await env.enabled(cron="0 * * * *", active_hours="11:00-12:30"))["schedule_id"]
        # The 12:00 fire was missed; the server is back at 12:59, after the active hours.
        noon = datetime(2026, 1, 15, 12, 0, tzinfo=LA)
        await env.pool.execute("UPDATE schedules SET next_fire_at = $2 WHERE id = $1", sid, noon)
        env.clock.now = noon + timedelta(minutes=59)
        await env.scheduler.tick()
        assert await env.runs(sid) == []
        assert await env.pool.fetchval("SELECT next_fire_at FROM schedules WHERE id = $1", sid) == noon + timedelta(
            hours=1)

    run_scheduled(test, monkeypatch, tmp_path)


@pytest.mark.parametrize(("idle", "busy", "expected"), [
    (True, True, "skipped_busy"), (True, False, "running"), (False, True, "running")])
def test_an_idle_only_schedule_skips_a_firing_while_its_manager_answers(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path, idle: bool, busy: bool, expected: str) -> None:
    async def test(env: ScheduleEnv) -> None:
        asked: list[str] = []
        env.scheduler.manager_busy = lambda manager_id: asked.append(manager_id) or busy
        sid = (await env.enabled(idle_only=idle))["schedule_id"]
        await env.due(sid)
        await env.scheduler.tick()
        [run] = await env.runs(sid)
        assert run["status"] == expected
        assert asked == (["manager"] if idle else [])
        if expected == "skipped_busy":
            assert run["task_id"] is None and env.bus.of("task_start") == []
            assert "its Manager was answering a message (idle_only)" in run["error"]
            # A second busy firing the same day is counted on the same run.
            await env.due(sid)
            await env.scheduler.tick()
            [again] = await env.runs(sid)
            assert (again["run_id"], again["firings"]) == (run["run_id"], 2)

    run_scheduled(test, monkeypatch, tmp_path)


def test_run_now_is_never_throttled(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    async def test(env: ScheduleEnv) -> None:
        env.scheduler.manager_busy = lambda manager_id: True
        sid = (await env.enabled(active_hours="01:00-02:00", idle_only=True))["schedule_id"]
        run = (await env.ok("POST", "/api/v1/schedule_admin/run", {"schedule_id": sid}))["run"]
        assert (run["trigger"], run["status"]) == ("manual", "running")

    run_scheduled(test, monkeypatch, tmp_path)


def test_the_api_stores_and_shows_the_throttle_fields(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    async def test(env: ScheduleEnv) -> None:
        plain = await env.schedule()
        assert (plain["active_hours"], plain["idle_only"]) == (None, False)
        assert plain["throttle"] == {"active_hours": None, "idle_only": False, "allows_silence": True,
                                     "pre_check": "none"}
        made = await env.schedule(active_hours="09:00-18:00", idle_only=True, gate_command="echo new",
                                  cron="*/5 * * * *")
        assert made["throttle"] == {"active_hours": "09:00-18:00", "idle_only": True, "allows_silence": True,
                                    "pre_check": "gate_command"}
        for bad in ("09:00-09:00", "9:00-18:00", "09:00"):
            status, answer = await env.call("POST", "/api/v1/schedule_admin/update", {
                "schedule_id": made["schedule_id"], "fields": {"active_hours": bad}})
            assert status == 400, (bad, answer)
        status, _ = await env.call("POST", "/api/v1/schedule_admin/update", {
            "schedule_id": made["schedule_id"], "fields": {"idle_only": "yes"}})
        assert status == 400
        cleared = (await env.ok("POST", "/api/v1/schedule_admin/update", {
            "schedule_id": made["schedule_id"], "fields": {"active_hours": None, "idle_only": None}}))["schedule"]
        assert (cleared["active_hours"], cleared["idle_only"]) == (None, False)
        # The table refuses what the API would.
        with pytest.raises(Exception, match="schedules_active_hours_check"):
            await env.pool.execute("UPDATE schedules SET active_hours = '10:00-10:00' WHERE id = $1",
                                   made["schedule_id"])

    run_scheduled(test, monkeypatch, tmp_path)


def test_a_database_without_the_throttle_fields_gets_them_on_upgrade(monkeypatch: pytest.MonkeyPatch) -> None:
    async def test(pool: Any) -> None:
        from looper.db import connection

        fresh = {row["conname"]: row["def"] for row in await pool.fetch(
            "SELECT conname, pg_get_constraintdef(oid) AS def FROM pg_constraint "
            "WHERE conname IN ('schedules_active_hours_check', 'schedule_runs_status_check')")}
        old_status = fresh["schedule_runs_status_check"].replace(", 'skipped_busy'::text", "")
        await pool.execute(
            "ALTER TABLE schedules DROP COLUMN active_hours, DROP COLUMN idle_only; "
            f"ALTER TABLE schedule_runs DROP CONSTRAINT schedule_runs_status_check, "
            f"ADD CONSTRAINT schedule_runs_status_check {old_status}; "
            "DELETE FROM schema_migrations WHERE name IN ('schedule_throttle', 'schedule_throttle_online')")
        async with pool.acquire() as conn:
            assert await connection.schema_change_pending(conn)
        await connection.ensure_schema(pool)
        async with pool.acquire() as conn:
            assert not await connection.schema_change_pending(conn)
        after = {row["conname"]: (row["def"], row["convalidated"]) for row in await pool.fetch(
            "SELECT conname, pg_get_constraintdef(oid) AS def, convalidated FROM pg_constraint "
            "WHERE conname IN ('schedules_active_hours_check', 'schedule_runs_status_check')")}
        # The same checks as a new database, validated once the upgrade committed.
        assert after == {name: (definition, True) for name, definition in fresh.items()}
        assert await pool.fetchval("SELECT count(*) FROM schema_migrations WHERE name IN "
                                   "('schedule_throttle', 'schedule_throttle_online')") == 2
        await connection.ensure_schema(pool)  # a second upgrade changes nothing

    run_db(test, monkeypatch, timeout=30)


def test_an_export_carries_the_throttle_fields_only_when_set(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from tests.test_config_transfer_db import _installation, _plan, _transfer_file

    async def test() -> None:
        async with _installation() as (source, source_url), _installation() as (target, target_url):
            path = tmp_path / "config.json"
            plain = {"name": "plain", "cron": "0 9 * * *", "timezone": "UTC", "requirement": "Run it."}
            throttled = {**plain, "name": "throttled", "active_hours": "08:00-20:00", "idle_only": True}
            path.write_text(json.dumps({"format": "looper-config/v4", "projects": [
                {"name": "calc", "schedules": [plain, throttled]}]}))
            code, out, err = await _transfer_file(source_url, "import", str(path), apply=True, json_output=True)
            assert code == 0, err
            assert _plan(out)["counts"]["project.schedules"]["added"] == 2
            assert (await _transfer_file(source_url, "export", str(path), include_projects=True))[0] == 0
            entries = {e["name"]: e for e in json.loads(path.read_text())["projects"][0]["schedules"]}
            assert "active_hours" not in entries["plain"] and "idle_only" not in entries["plain"]
            assert (entries["throttled"]["active_hours"], entries["throttled"]["idle_only"]) == ("08:00-20:00", True)
            code, out, err = await _transfer_file(target_url, "import", str(path), apply=True, json_output=True)
            assert code == 0, err
            rows = {r["name"]: r for r in await target.fetch("SELECT name, active_hours, idle_only FROM schedules")}
            assert (rows["plain"]["active_hours"], rows["plain"]["idle_only"]) == (None, False)
            assert (rows["throttled"]["active_hours"], rows["throttled"]["idle_only"]) == ("08:00-20:00", True)

    asyncio.run(asyncio.wait_for(test(), timeout=60))
