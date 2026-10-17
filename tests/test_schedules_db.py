"""Schedules against a real PostgreSQL: the API's checks, firing (once, even with two schedulers), overlap,
missed fires, run timeouts, a task's end written back to its run, and the project and profile guards.

Runs only when LOOPER_TEST_DATABASE_URL points at a disposable database. Each
test works in a new schema of its own, dropped afterwards. The scheduler's
clock is set by the test; its bus records what it would send the TaskRouter.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import urllib.parse
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo

import pytest

DSN = os.environ.get("LOOPER_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")

UTC = timezone.utc
# Noon in Los Angeles, where the scheduler's day (``_run``, ``_setup``) starts at midnight: far from a day's end.
NOON = datetime(2030, 1, 15, 20, 0, tzinfo=UTC)


class _Bus:
    def __init__(self) -> None:
        self.sent: list[Any] = []

    async def publish(self, envelope: Any) -> None:
        self.sent.append(envelope)

    def of(self, kind: str) -> list[str]:
        return [e.payload["task_id"] for e in self.sent if e.message_type.value == kind]


class _Clock:
    def __init__(self) -> None:
        self.now = datetime.now(UTC)

    def __call__(self) -> datetime:
        return self.now


class _Writer:
    def __init__(self) -> None:
        self.data = b""

    def write(self, data: bytes) -> None:
        self.data += data

    async def drain(self) -> None:
        pass


class _Env(SimpleNamespace):
    pool: Any
    bus: _Bus
    clock: _Clock
    scheduler: Any
    project_id: str
    work_dir: str

    async def call(self, method: str, path: str, body: Any = None, query: dict[str, str] | None = None,
                   ) -> tuple[int, Any]:
        from looper.agents.http_server import AgentHTTPServer
        from looper.agents.tiers.task import TaskRouter

        server = AgentHTTPServer(web_adapter=SimpleNamespace(), supervisor=SimpleNamespace(),
                                 task_router=TaskRouter(runtime_root=Path(tempfile.mkdtemp())),
                                 scheduler=self.scheduler)
        writer = _Writer()
        raw = b"" if body is None else json.dumps(body).encode()
        await server._route_api(method, path, query or {}, raw, writer)
        head, _, payload = writer.data.partition(b"\r\n\r\n")
        return int(head.split()[1]), json.loads(payload)

    async def ok(self, method: str, path: str, body: Any = None) -> Any:
        status, payload = await self.call(method, path, body)
        assert status in (200, 201), (status, payload)
        return payload

    async def schedule(self, **fields: Any) -> dict[str, Any]:
        body = {"name": f"s-{uuid.uuid4().hex[:6]}", "cron": "*/5 * * * *", "timezone": "America/Los_Angeles",
                "requirement": "Look at the new alerts.", **fields}
        return (await self.ok("POST", f"/api/projects/{self.project_id}/schedules", body))["schedule"]

    async def enabled(self, **fields: Any) -> dict[str, Any]:
        schedule = await self.schedule(**fields)
        return (await self.ok("PUT", f"/api/schedules/{schedule['schedule_id']}", {"enabled": True}))["schedule"]

    async def due(self, schedule_id: str, *, fires_ago: int = 0, step: timedelta = timedelta(minutes=5)) -> None:
        """Make the schedule's next fire ``fires_ago`` fire times in the past, and the clock 10 s after the latest."""
        latest = (self.clock.now + step).replace(second=0, microsecond=0)
        latest -= timedelta(minutes=latest.minute % 5)
        await self.pool.execute("UPDATE schedules SET next_fire_at = $2 WHERE id = $1", schedule_id,
                                latest - fires_ago * step)
        self.clock.now = latest + timedelta(seconds=10)

    async def runs(self, schedule_id: str) -> list[dict[str, Any]]:
        return (await self.ok("GET", f"/api/schedules/{schedule_id}/runs"))["runs"]

    async def end_task(self, task_id: str, status: str, *, summary: str = "", cost: float = 0.0) -> None:
        await self.pool.execute(
            "UPDATE tasks SET status = $2, result_summary = CASE WHEN $2 = 'completed' THEN $3 END, "
            "error = CASE WHEN $2 = 'failed' THEN $3 END WHERE id = $1", task_id, status, summary)
        if cost:
            await self.pool.execute(
                "INSERT INTO llm_records (agent_id, task_id, input_text, output_text, status, cost_usd) "
                "VALUES ($1, $2, '', '', 'completed', $3)", f"planner-{task_id}", task_id, cost)
        await self.scheduler.task_ended(task_id, status)


def _run(test: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import asyncpg

    from looper.agents import schedules
    from looper.agents.scheduler import Scheduler
    from looper.db import connection, projects

    async def _main() -> None:
        schema = f"looper_test_{uuid.uuid4().hex[:12]}"
        admin = await asyncpg.connect(DSN)
        await admin.execute(f'CREATE SCHEMA "{schema}"')
        pool = await asyncpg.create_pool(DSN, min_size=1, max_size=4, server_settings={"search_path": schema})
        try:
            await connection.ensure_schema(pool)
            monkeypatch.setattr(connection, "_pool", pool)
            monkeypatch.setattr(schedules, "_min_interval", schedules.DEFAULT_MIN_INTERVAL_SECONDS)
            project = await projects.create_project(name="alerts", work_dir=str(tmp_path), pool=pool)
            bus, clock = _Bus(), _Clock()
            env = _Env(pool=pool, bus=bus, clock=clock,
                       scheduler=Scheduler(bus=bus, clock=clock, day_zone="America/Los_Angeles"),
                       project_id=project["id"], work_dir=str(tmp_path))
            await test(env)
        finally:
            await pool.close()
            await admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
            await admin.close()

    asyncio.run(_main())


# --- The API ---


@pytest.mark.parametrize(("body", "error"), [
    ({"name": ""}, "a schedule name is required"),
    ({"name": "x" * 81}, "at most 80 characters"),
    ({"cron": "*/3 * * * *"}, "fires 3 minutes apart"),
    ({"cron": "0 9 * *"}, "5 fields"),
    ({"cron": "0 0 30 2 *"}, "never fires"),
    ({"timezone": "Mars/Base"}, "unknown time zone"),
    ({"requirement": "   "}, "requirement is required"),
    ({"requirement": "x" * 27_001}, "at most 27000 characters"),
    ({"overlap": "both"}, "overlap must be one of skip, queue, parallel"),
    ({"catch_up": "all"}, "catch_up must be one of once, skip"),
    ({"run_timeout_seconds": 0}, "run_timeout_seconds must be"),
    ({"run_timeout_seconds": 1.5}, "run_timeout_seconds must be"),
    ({"run_budget_usd": -1}, "run_budget_usd must be"),
    ({"work_dir": "relative/dir"}, "absolute path"),
    ({"profile_overrides": {"manager": "agent-claude-code-opus"}}, "not 'manager'"),
    ({"profile_overrides": {"planner": "agent-nope"}}, "planner:"),
    ({"task_limits": {"planner": 2}}, "task_limits: one Planner runs a task"),
    ({"task_limits": {"manager": 2}}, "task_limits: unknown role 'manager'"),
    ({"task_limits": {"worker": 0}}, "task_limits.worker: max_concurrency must be a whole number from 1 to 64"),
    ({"task_limits": {"reviewer": 65}}, "task_limits.reviewer:"),
    ({"task_limits": [8]}, "task_limits must be an object"),
    ({"enabled": True}, "created disabled"),
    ({"gate": "true"}, "unknown field 'gate'"),
])
def test_a_bad_schedule_is_refused_with_the_reason(monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
                                                   body: dict[str, Any], error: str) -> None:
    async def test(env: _Env) -> None:
        full = {"name": "triage", "cron": "*/5 * * * *", "requirement": "Look at the alerts.", **body}
        status, payload = await env.call("POST", f"/api/projects/{env.project_id}/schedules", full)
        assert status == 400, payload
        assert error in payload["error"]
        assert await env.pool.fetchval("SELECT count(*) FROM schedules") == 0

    _run(test, monkeypatch, tmp_path)


def test_a_new_schedule_is_disabled_named_once_per_project_and_previewed(monkeypatch: pytest.MonkeyPatch,
                                                                       tmp_path: Path) -> None:
    async def test(env: _Env) -> None:
        schedule = await env.schedule(name="Triage", cron="0  9 * * 1-5", profile_overrides={
            "planner": "agent-claude-code-opus", "worker": "claude-code/opus"})  # by id or by name
        assert (schedule["enabled"], schedule["next_fire_at"], schedule["last_run"]) == (False, None, None)
        assert schedule["cron"] == "0 9 * * 1-5"
        assert schedule["description"] == "Weekdays at 09:00 (America/Los_Angeles)"
        assert schedule["profile_overrides"] == {"planner": "agent-claude-code-opus",
                                                 "worker": "agent-claude-code-opus"}
        assert schedule["task_limits"] == {"worker": None, "reviewer": None}

        status, payload = await env.call("POST", f"/api/projects/{env.project_id}/schedules", {
            "name": "triage", "cron": "*/5 * * * *", "requirement": "r"})
        assert status == 409 and "already has a schedule named" in payload["error"]

        listed = await env.ok("GET", f"/api/projects/{env.project_id}/schedules")
        assert [s["name"] for s in listed["schedules"]] == ["Triage"]
        assert listed["limits"]["min_interval_seconds"] == 300

        preview = await env.ok("POST", "/api/schedules/preview", {"cron": "30 2 * * *",
                                                                    "timezone": "America/Los_Angeles", "count": 3})
        assert preview["valid"] and len(preview["next"]) == 3
        bad = await env.ok("POST", "/api/schedules/preview", {"cron": "* * * * *"})
        assert (bad["valid"], bad["next"]) == (False, []) and "1 minute apart" in bad["error"]
        status, _ = await env.call("POST", "/api/schedules/preview", {"cron": 5})
        assert status == 400

        assert (await env.call("GET", "/api/schedules/sched-000000000000"))[0] == 404
        assert (await env.call("PUT", f"/api/schedules/{schedule['schedule_id']}", {}))[0] == 400
        status, payload = await env.call("PUT", f"/api/schedules/{schedule['schedule_id']}", {"enabled": "yes"})
        assert status == 400 and "true or false" in payload["error"]

    _run(test, monkeypatch, tmp_path)


def test_enabling_sets_the_next_fire_from_now_and_never_back_fills(monkeypatch: pytest.MonkeyPatch,
                                                                   tmp_path: Path) -> None:
    async def test(env: _Env) -> None:
        schedule = await env.enabled()
        sid = schedule["schedule_id"]
        first = datetime.fromisoformat(schedule["next_fire_at"])
        assert datetime.now(UTC) < first <= datetime.now(UTC) + timedelta(minutes=5)

        # Disabled for a day, then enabled again: nothing that fell due meanwhile runs.
        disabled = (await env.ok("PUT", f"/api/schedules/{sid}", {"enabled": False}))["schedule"]
        assert disabled["next_fire_at"] is None
        await env.pool.execute("UPDATE schedules SET last_fired_at = now() - interval '1 day' WHERE id = $1", sid)
        again = (await env.ok("PUT", f"/api/schedules/{sid}", {"enabled": True}))["schedule"]
        assert datetime.fromisoformat(again["next_fire_at"]) > datetime.now(UTC)
        await env.scheduler.tick()
        assert await env.runs(sid) == []

        # A new rule on an enabled schedule recomputes it, also from now.
        hourly = (await env.ok("PUT", f"/api/schedules/{sid}", {"cron": "0 * * * *"}))["schedule"]
        assert datetime.fromisoformat(hourly["next_fire_at"]).minute == 0

    _run(test, monkeypatch, tmp_path)


def test_every_projects_schedules_are_listed_together_the_disabled_ones_too(monkeypatch: pytest.MonkeyPatch,
                                                                           tmp_path: Path) -> None:
    from looper.db import projects

    async def test(env: _Env) -> None:
        assert await env.ok("GET", "/api/schedules") == {"schedules": []}
        billing = await projects.create_project(name="Billing", work_dir=env.work_dir, pool=env.pool)
        await env.ok("POST", f"/api/projects/{billing['id']}/schedules", {
            "name": "Invoices", "cron": "0 6 * * *", "timezone": "Europe/Berlin", "requirement": "Check them."})
        await env.schedule(name="nightly")
        hourly = await env.enabled(name="Hourly check", cron="0 * * * *")
        run = (await env.ok("POST", f"/api/schedules/{hourly['schedule_id']}/run"))["run"]
        await env.end_task(run["task_id"], "completed", summary="nothing new", cost=0.25)

        listed = (await env.ok("GET", "/api/schedules"))["schedules"]
        # By project name, then by name, ignoring case; the disabled ones too.
        assert [(s["project_name"], s["name"], s["enabled"]) for s in listed] == [
            ("alerts", "Hourly check", True), ("alerts", "nightly", False), ("Billing", "Invoices", False)]
        assert listed[2]["project_id"] == billing["id"]
        assert listed[2]["description"] == "Daily at 06:00 (Europe/Berlin)" and listed[2]["next_fire_at"] is None
        assert (listed[0]["last_run"]["task_id"], listed[0]["last_run"]["status"]) == (run["task_id"], "completed")
        assert listed[0]["spent_today_usd"] == pytest.approx(0.25) and listed[1]["spent_today_usd"] == 0
        assert listed[0]["alerts"] == {"failures_since": None, "stale_since": None}
        # The same rows a project's list and a schedule's own answer give.
        assert (await env.ok("GET", f"/api/projects/{env.project_id}/schedules"))["schedules"] == listed[:2]
        assert (await env.ok("GET", f"/api/schedules/{hourly['schedule_id']}"))["schedule"] == listed[0]

    _run(test, monkeypatch, tmp_path)


def test_a_schedules_runs_page_by_cursor_and_can_leave_out_the_routine_skips(monkeypatch: pytest.MonkeyPatch,
                                                                           tmp_path: Path) -> None:
    async def test(env: _Env) -> None:
        sid = (await env.enabled())["schedule_id"]
        run = (await env.ok("POST", f"/api/schedules/{sid}/run"))["run"]  # the oldest, with its task
        await env.pool.execute("UPDATE tasks SET title = 'Look at the new alerts' WHERE id = $1", run["task_id"])
        start = datetime.now(UTC)
        statuses = ["skipped_gate", "completed", "skipped_overlap", "skipped_gate", "failed", "skipped_budget"]
        for i, status in enumerate(statuses):  # oldest first, after the manual run
            await env.pool.execute(
                "INSERT INTO schedule_runs (id, schedule_id, scheduled_for, fired_at, trigger, status, created_at) "
                "VALUES ($1, $2, $3, $3, 'scheduled', $4, $3)",
                f"srun-test{i:04d}", sid, start + timedelta(minutes=i + 1), status)

        async def page(**query: str) -> dict[str, Any]:
            status, payload = await env.call("GET", f"/api/schedules/{sid}/runs", query=query)
            assert status == 200, payload
            return payload

        everything = await page()
        assert [r["status"] for r in everything["runs"]] == [*reversed(statuses), "running"]
        assert everything["next_before"] is None
        assert everything["routine_skips"] == {"skipped_gate": 2, "skipped_overlap": 1}
        oldest = everything["runs"][-1]
        task_status = await env.pool.fetchval("SELECT status FROM tasks WHERE id = $1", run["task_id"])
        assert (oldest["task_id"], oldest["task_title"], oldest["task_status"]) == (
            run["task_id"], "Look at the new alerts", task_status)

        # Without the routine skips, two at a time, each page going on from the last one's oldest run.
        first = await page(skips="hide", limit="2")
        assert [r["status"] for r in first["runs"]] == ["skipped_budget", "failed"]
        second = await page(skips="hide", limit="2", before=first["next_before"])
        assert [r["status"] for r in second["runs"]] == ["completed", "running"] and second["next_before"] is None
        assert second["routine_skips"] == everything["routine_skips"]  # counted whether shown or not
        assert [r["status"] for r in (await page(limit="3", before="srun-test0003"))["runs"]] == [
            "skipped_overlap", "completed", "skipped_gate"]

        for query, says in [({"skips": "some"}, "skips must be one of show, hide"),
                            ({"before": "srun-nope"}, "before must be a run of schedule"),
                            ({"limit": "0"}, "limit must be a whole number")]:
            status, payload = await env.call("GET", f"/api/schedules/{sid}/runs", query=query)
            assert status == 400 and says in payload["error"], payload
        other = await env.schedule()
        status, payload = await env.call("GET", f"/api/schedules/{other['schedule_id']}/runs",
                                         query={"before": "srun-test0001"})
        assert status == 400 and "before must be a run of schedule" in payload["error"]

    _run(test, monkeypatch, tmp_path)


def test_a_schedule_with_a_gate_may_fire_every_minute(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    async def test(env: _Env) -> None:
        base = {"name": "every minute", "cron": "* * * * *", "timezone": "UTC", "requirement": "Look."}
        status, payload = await env.call("POST", f"/api/projects/{env.project_id}/schedules", base)
        assert status == 400
        assert "fires 1 minute apart" in payload["error"] and "at least 5 minutes apart" in payload["error"]
        assert "(a schedule with a gate command may fire every 1 minute)" in payload["error"]

        sid = (await env.ok("POST", f"/api/projects/{env.project_id}/schedules",
                            {**base, "gate_command": "cat items.txt"}))["schedule"]["schedule_id"]
        # Its gate cannot go while it fires every minute; with a 5-minute rule in the same change it can.
        status, payload = await env.call("PUT", f"/api/schedules/{sid}", {"gate_command": None})
        assert status == 400
        assert payload["error"].startswith("without its gate command, the rule '* * * * *' fires 1 minute apart")
        await env.ok("PUT", f"/api/schedules/{sid}", {"gate_command": "", "cron": "*/5 * * * *"})
        back = await env.ok("PUT", f"/api/schedules/{sid}", {"gate_command": "true", "cron": "* * * * *"})
        assert (back["schedule"]["cron"], back["schedule"]["gate_command"]) == ("* * * * *", "true")

        for gated, valid, least in ((True, True, 60), (False, False, 300)):
            preview = await env.ok("POST", "/api/schedules/preview", {"cron": "* * * * *", "gated": gated})
            assert (preview["valid"], preview["min_interval_seconds"]) == (valid, least)
        status, payload = await env.call("POST", "/api/schedules/preview", {"cron": "* * * * *", "gated": "yes"})
        assert status == 400 and "gated must be true or false" in payload["error"]
        limits = (await env.ok("GET", f"/api/projects/{env.project_id}/schedules"))["limits"]
        assert (limits["min_interval_seconds"], limits["min_interval_gated_seconds"]) == (300, 60)

    _run(test, monkeypatch, tmp_path)


# --- Firing ---


def test_a_schedules_task_limits_are_replaced_whole_and_given_to_each_task_it_fires(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    async def test(env: _Env) -> None:
        schedule = await env.schedule(task_limits={"worker": 8, "reviewer": 3})
        sid = schedule["schedule_id"]
        assert schedule["task_limits"] == {"worker": 8, "reviewer": 3}
        # A given object replaces the schedule's limits (a role left out has none), null clears both, and a change
        # of another field keeps them.
        changed = await env.ok("PUT", f"/api/schedules/{sid}", {"task_limits": {"worker": 2, "reviewer": None}})
        assert changed["schedule"]["task_limits"] == {"worker": 2, "reviewer": None}
        cleared = await env.ok("PUT", f"/api/schedules/{sid}", {"task_limits": None})
        assert cleared["schedule"]["task_limits"] == {"worker": None, "reviewer": None}
        await env.ok("PUT", f"/api/schedules/{sid}", {"task_limits": {"reviewer": 3, "worker": 8}})
        kept = await env.ok("PUT", f"/api/schedules/{sid}", {"name": "renamed", "enabled": True})
        assert kept["schedule"]["task_limits"] == {"worker": 8, "reviewer": 3}

        await env.due(sid)
        await env.scheduler.tick()
        [run] = await env.runs(sid)
        assert {r["role"]: r["max_concurrency"] for r in await env.pool.fetch(
            "SELECT role, max_concurrency FROM task_concurrency_limits WHERE task_id = $1", run["task_id"])} == {
            "worker": 8, "reviewer": 3}
        # They are the task's own, as if set on its Agents tab.
        status, task = await env.call("GET", f"/api/tasks/{run['task_id']}")
        assert status == 200 and task["agents"]["roles"]["worker"]["concurrency"] == {
            "value": 8, "source": "task", "task": 8, "default": {"value": 16, "source": "builtin"}}

    _run(test, monkeypatch, tmp_path)


def test_a_due_schedule_fires_a_started_task_with_its_settings_and_run_context(monkeypatch: pytest.MonkeyPatch,
                                                                               tmp_path: Path) -> None:
    from looper.db import projects as db_projects

    async def test(env: _Env) -> None:
        schedule = await env.enabled(name="Alert triage", run_budget_usd=0.25,
                                     profile_overrides={"worker": "agent-claude-code-opus"})
        sid = schedule["schedule_id"]
        await env.due(sid)
        due_at = await env.pool.fetchval("SELECT next_fire_at FROM schedules WHERE id = $1", sid)

        await env.scheduler.tick()

        [run] = await env.runs(sid)
        assert (run["trigger"], run["status"], run["missed"]) == ("scheduled", "running", 0)
        assert datetime.fromisoformat(run["scheduled_for"]) == due_at
        assert env.bus.of("task_start") == [run["task_id"]]
        task = await env.pool.fetchrow("SELECT * FROM tasks WHERE id = $1", run["task_id"])
        assert (task["schedule_id"], task["project_id"], task["work_dir"]) == (sid, env.project_id, env.work_dir)
        assert task["requirement"].startswith("Look at the new alerts.\n\n## 定时运行上下文\n")
        assert "「Alert triage」" in task["requirement"] and "无人值守" in task["requirement"]
        assert await env.pool.fetchval("SELECT profile_id FROM task_profile_overrides WHERE task_id = $1 AND "
                                       "role = 'worker'", run["task_id"]) == "agent-claude-code-opus"
        # The run's spend cap is the task's, over the project's.
        assert (await db_projects.get_task_project(run["task_id"]))["task_budget_usd"] == 0.25
        row = await env.pool.fetchrow("SELECT last_fired_at, next_fire_at FROM schedules WHERE id = $1", sid)
        assert row["last_fired_at"] == due_at and row["next_fire_at"] == due_at + timedelta(minutes=5)

        # Named by the schedule and the fire time in its zone, not by the requirement's first line.
        when = due_at.astimezone(ZoneInfo("America/Los_Angeles")).strftime("%m-%d %H:%M")
        assert task["title"] == f"Alert triage · {when}"
        listed = await env.ok("GET", "/api/tasks", None)
        assert listed["tasks"][0]["schedule_id"] == sid and listed["tasks"][0]["schedule_name"] == "Alert triage"
        assert listed["tasks"][0]["name"] == f"Alert triage · {when}"
        status, filtered = await env.call("GET", "/api/tasks", query={"schedule_id": "sched-000000000000"})
        assert status == 200 and filtered["tasks"] == []

        # The next tick has nothing due.
        await env.scheduler.tick()
        assert len(await env.runs(sid)) == 1

    _run(test, monkeypatch, tmp_path)


def test_two_schedulers_ticking_together_fire_each_firing_once(monkeypatch: pytest.MonkeyPatch,
                                                              tmp_path: Path) -> None:
    from looper.agents.scheduler import Scheduler

    async def test(env: _Env) -> None:
        schedules = [(await env.enabled())["schedule_id"] for _ in range(3)]
        other = Scheduler(bus=env.bus, clock=env.clock)
        for round_ in range(1, 6):
            for sid in schedules:
                await env.due(sid)
            await asyncio.gather(env.scheduler.tick(), other.tick())
            # Each firing counted once: the first created a task, the later ones are one repeated skip.
            counts = [sum(run["firings"] for run in await env.runs(sid)) for sid in schedules]
            assert counts == [round_] * 3, counts
            # As the TaskRouter would: the started tasks run.
            await env.pool.execute("UPDATE tasks SET status = 'running' WHERE status = 'init'")
        # Each schedule's first run created a task; the later ones found it still in progress.
        assert len(env.bus.of("task_start")) == 3

    _run(test, monkeypatch, tmp_path)


def test_a_message_being_recorded_about_a_schedule_does_not_hold_up_its_firing(monkeypatch: pytest.MonkeyPatch,
                                                                              tmp_path: Path) -> None:
    from looper.db import schedules as db_schedules

    async def test(env: _Env) -> None:
        sid = (await env.enabled())["schedule_id"]
        await env.due(sid)
        # The Notifier records a message about an earlier run while the pass looks for due schedules: until it
        # commits, the insert's foreign key check holds the schedule's row (FOR KEY SHARE).
        async with env.pool.acquire() as conn, conn.transaction():
            await db_schedules.insert_notification(conn, schedule_id=sid, run_id=None, kind="run", channel=None,
                                                   chat=None, text="an earlier run failed", status="dashboard",
                                                   error=None)
            await env.scheduler.tick()
        assert [run["status"] for run in await env.runs(sid)] == ["running"]

    _run(test, monkeypatch, tmp_path)


def test_a_schedule_that_cannot_fire_holds_up_no_other(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    async def test(env: _Env) -> None:
        broken, working = (await env.enabled())["schedule_id"], (await env.enabled())["schedule_id"]
        await env.due(broken)
        await env.pool.execute("UPDATE schedules SET next_fire_at = (SELECT next_fire_at FROM schedules "
                               "WHERE id = $1) WHERE id = $2", broken, working)
        fire = env.scheduler._fire_schedule

        async def failing(conn: Any, schedule: dict[str, Any], now: datetime) -> str | None:
            if schedule["id"] == broken:
                raise RuntimeError("boom")
            return await fire(conn, schedule, now)

        monkeypatch.setattr(env.scheduler, "_fire_schedule", failing)
        await env.scheduler.tick()
        assert (await env.runs(broken), len(await env.runs(working))) == ([], 1)
        # Rolled back, the broken one is still due and fires once it can.
        monkeypatch.setattr(env.scheduler, "_fire_schedule", fire)
        await env.scheduler.tick()
        assert len(await env.runs(broken)) == 1

        # When the database itself fails, the pass ends (the loop logs it and tries again later).
        from looper.db import schedules as db_schedules

        class Unreachable(Exception):
            pass

        async def unreachable(*_: Any, **__: Any) -> None:
            raise Unreachable("connection refused")

        monkeypatch.setattr(db_schedules, "lock_next_due", unreachable)
        with pytest.raises(Unreachable):
            await asyncio.wait_for(env.scheduler.tick(), timeout=5)

    _run(test, monkeypatch, tmp_path)


@pytest.mark.parametrize("overlap", ["skip", "queue"])
def test_a_firing_while_the_previous_task_runs_is_skipped_or_queued_once(monkeypatch: pytest.MonkeyPatch,
                                                                         tmp_path: Path, overlap: str) -> None:
    async def test(env: _Env) -> None:
        env.clock.now = NOON
        sid = (await env.enabled(overlap=overlap))["schedule_id"]
        await env.due(sid)
        await env.scheduler.tick()
        [first] = await env.runs(sid)
        await env.pool.execute("UPDATE tasks SET status = 'running' WHERE id = $1", first["task_id"])

        for _ in range(2):
            await env.due(sid)
            await env.scheduler.tick()
        if overlap == "skip":
            # The second skip repeats the first: one run that counts both firings.
            skipped, _ = await env.runs(sid)
            assert (skipped["status"], skipped["firings"]) == ("skipped_overlap", 2)
            assert f"the previous run's task ({first['task_id']}) is still in progress" in skipped["error"]
            assert skipped["finished_at"] > skipped["scheduled_for"]
        else:
            newest, middle, _ = await env.runs(sid)
            # At most one waits: the second firing behind it is skipped.
            assert [middle["status"], newest["status"]] == ["queued", "skipped_overlap"]
            assert middle["task_id"] is None and middle["fired_at"] is None
            assert "a run is already waiting" in newest["error"]

        await env.end_task(first["task_id"], "completed", summary="done")
        runs = {run["run_id"]: run for run in await env.runs(sid)}
        assert runs[first["run_id"]]["status"] == "completed"
        if overlap == "queue":
            started = runs[middle["run_id"]]
            assert started["status"] == "running" and started["task_id"] and started["fired_at"]
            assert env.bus.of("task_start") == [first["task_id"], started["task_id"]]
        else:
            assert env.bus.of("task_start") == [first["task_id"]]

    _run(test, monkeypatch, tmp_path)


def test_missed_fires_run_once_as_a_catch_up_or_are_skipped(monkeypatch: pytest.MonkeyPatch,
                                                           tmp_path: Path) -> None:
    async def test(env: _Env) -> None:
        once = (await env.enabled(catch_up="once"))["schedule_id"]
        skip = (await env.enabled(catch_up="skip"))["schedule_id"]
        # The server was down for four fire times, the latest 10 s ago.
        await env.due(once, fires_ago=3)
        await env.pool.execute("UPDATE schedules SET next_fire_at = (SELECT next_fire_at FROM schedules "
                               "WHERE id = $1) WHERE id = $2", once, skip)

        await env.scheduler.tick()

        [caught_up] = await env.runs(once)
        assert (caught_up["trigger"], caught_up["missed"], caught_up["status"]) == ("catch_up", 4, "running")
        requirement = await env.pool.fetchval("SELECT requirement FROM tasks WHERE id = $1", caught_up["task_id"])
        assert "漏掉了 4 次触发" in requirement
        # skip: the latest was within 2 minutes, so it runs on time and the earlier ones are skipped.
        [on_time] = await env.runs(skip)
        assert (on_time["trigger"], on_time["missed"], on_time["status"]) == ("scheduled", 3, "running")

        # Down again, the latest fire now 3 minutes ago: skip records it skipped and waits for the next.
        await env.end_task(on_time["task_id"], "completed")
        await env.due(skip, fires_ago=2)
        env.clock.now += timedelta(minutes=3)
        await env.scheduler.tick()
        skipped = (await env.runs(skip))[0]
        assert (skipped["status"], skipped["missed"], skipped["task_id"]) == ("skipped_missed", 3, None)
        assert datetime.fromisoformat(
            (await env.ok("GET", f"/api/schedules/{skip}"))["schedule"]["next_fire_at"]) > env.clock.now

    _run(test, monkeypatch, tmp_path)


def test_run_now_fires_a_manual_run_and_keeps_the_next_fire(monkeypatch: pytest.MonkeyPatch,
                                                            tmp_path: Path) -> None:
    async def test(env: _Env) -> None:
        sid = (await env.enabled())["schedule_id"]
        before = await env.pool.fetchval("SELECT next_fire_at FROM schedules WHERE id = $1", sid)
        run = (await env.ok("POST", f"/api/schedules/{sid}/run"))["run"]
        assert (run["trigger"], run["status"]) == ("manual", "running")
        assert env.bus.of("task_start") == [run["task_id"]]
        assert await env.pool.fetchval("SELECT next_fire_at FROM schedules WHERE id = $1", sid) == before
        # A second one goes through the overlap check.
        again = (await env.ok("POST", f"/api/schedules/{sid}/run"))["run"]
        assert again["status"] == "skipped_overlap"
        assert (await env.call("POST", "/api/schedules/sched-000000000000/run"))[0] == 404

        # A disabled schedule can run now too; without a work directory the run fails, saying why.
        manual = await env.schedule()
        await env.pool.execute("UPDATE projects SET work_dir = '' WHERE id = $1", env.project_id)
        failed = (await env.ok("POST", f"/api/schedules/{manual['schedule_id']}/run"))["run"]
        assert failed["status"] == "failed" and "work directory is required" in failed["error"]

    _run(test, monkeypatch, tmp_path)


def test_a_task_end_writes_the_run_outcome_and_the_failure_count(monkeypatch: pytest.MonkeyPatch,
                                                                 tmp_path: Path) -> None:
    async def test(env: _Env) -> None:
        sid = (await env.enabled())["schedule_id"]

        async def fire_and_end(status: str, summary: str, cost: float) -> dict[str, Any]:
            await env.due(sid)
            await env.scheduler.tick()
            run = (await env.runs(sid))[0]
            await env.end_task(run["task_id"], status, summary=summary, cost=cost)
            return (await env.runs(sid))[0]

        failed = await fire_and_end("failed", "the API was down", 0.02)
        assert (failed["status"], failed["summary"], failed["error"]) == ("failed", "the API was down",
                                                                            "the API was down")
        assert failed["cost_usd"] == pytest.approx(0.02) and failed["finished_at"]
        await fire_and_end("failed", "still down", 0.0)
        schedule = (await env.ok("GET", f"/api/schedules/{sid}"))["schedule"]
        assert (schedule["consecutive_failures"], schedule["last_success_at"]) == (2, None)

        # A user stopping a run is neither a failure nor a success.
        stopped = await fire_and_end("stopped", "", 0.0)
        assert stopped["status"] == "stopped"
        assert (await env.ok("GET", f"/api/schedules/{sid}"))["schedule"]["consecutive_failures"] == 2

        done = await fire_and_end("completed", "2 new alerts, both known", 0.05)
        assert (done["status"], done["summary"], done["error"]) == ("completed", "2 new alerts, both known", None)
        schedule = (await env.ok("GET", f"/api/schedules/{sid}"))["schedule"]
        assert schedule["consecutive_failures"] == 0 and schedule["last_success_at"]
        assert schedule["last_run"]["run_id"] == done["run_id"]

        # A run's outcome is written once: a later end of the same task changes nothing.
        await env.end_task(done["task_id"], "failed", summary="again")
        assert (await env.runs(sid))[0]["status"] == "completed"
        # The next run's context lists the latest runs, newest first.
        await env.due(sid)
        await env.scheduler.tick()
        latest = (await env.runs(sid))[0]
        requirement = await env.pool.fetchval("SELECT requirement FROM tasks WHERE id = $1", latest["task_id"])
        assert "最近 3 次运行" in requirement and requirement.index("2 new alerts") < requirement.index("已停止")

    _run(test, monkeypatch, tmp_path)


def test_a_run_past_its_timeout_is_stopped_and_counts_as_a_failure(monkeypatch: pytest.MonkeyPatch,
                                                                   tmp_path: Path) -> None:
    async def test(env: _Env) -> None:
        sid = (await env.enabled(run_timeout_seconds=60))["schedule_id"]
        await env.due(sid)
        await env.scheduler.tick()
        [run] = await env.runs(sid)
        await env.pool.execute("UPDATE tasks SET status = 'running' WHERE id = $1", run["task_id"])

        env.clock.now += timedelta(seconds=59)
        await env.scheduler.tick()
        assert (await env.runs(sid))[0]["status"] == "running" and env.bus.of("task_stop") == []

        env.clock.now += timedelta(seconds=2)
        await env.scheduler.tick()
        [timed_out] = await env.runs(sid)
        assert timed_out["status"] == "timed_out" and "run timeout of 60 s" in timed_out["error"]
        assert env.bus.of("task_stop") == [run["task_id"]]
        assert (await env.ok("GET", f"/api/schedules/{sid}"))["schedule"]["consecutive_failures"] == 1
        # The stop then ends the task: the run keeps timed_out.
        await env.end_task(run["task_id"], "stopped")
        assert (await env.runs(sid))[0]["status"] == "timed_out"

    _run(test, monkeypatch, tmp_path)


def test_runs_whose_task_ended_while_no_one_listened_are_settled(monkeypatch: pytest.MonkeyPatch,
                                                                 tmp_path: Path) -> None:
    async def test(env: _Env) -> None:
        sid = (await env.enabled())["schedule_id"]
        await env.due(sid)
        await env.scheduler.tick()
        [run] = await env.runs(sid)
        # A server start stopped the task (its plan was not live): the next pass records it.
        await env.pool.execute("UPDATE tasks SET status = 'stopped' WHERE id = $1", run["task_id"])
        await env.scheduler.tick()
        assert (await env.runs(sid))[0]["status"] == "stopped"

        # A task created but never started (the server stopped in between) is started again a minute later.
        await env.due(sid)
        await env.scheduler.tick()
        pending = (await env.runs(sid))[0]
        env.bus.sent.clear()
        env.clock.now += timedelta(seconds=61)
        await env.scheduler.tick()
        assert env.bus.of("task_start") == [pending["task_id"]]

    _run(test, monkeypatch, tmp_path)


# --- Guards ---


def test_a_project_with_schedules_is_not_deleted_and_a_deleted_schedule_keeps_its_tasks(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from looper.db import projects as db_projects

    async def test(env: _Env) -> None:
        empty = await db_projects.create_project(name="empty", pool=env.pool)
        await env.pool.execute("UPDATE projects SET work_dir = $2 WHERE id = $1", empty["id"], env.work_dir)
        status, schedule = await env.call("POST", f"/api/projects/{empty['id']}/schedules", {
            "name": "n", "cron": "0 9 * * *", "requirement": "r"})
        assert status == 201
        sid = schedule["schedule"]["schedule_id"]
        counts = {p["project_id"]: p["schedule_count"] for p in (await env.ok("GET", "/api/projects"))["projects"]}
        assert counts == {env.project_id: 0, empty["id"]: 1}

        status, payload = await env.call("DELETE", f"/api/projects/{empty['id']}")
        assert status == 409 and "still has 1 schedule; delete them first" in payload["error"]

        run = (await env.ok("POST", f"/api/schedules/{sid}/run"))["run"]
        await env.ok("DELETE", f"/api/schedules/{sid}")
        assert await env.pool.fetchval("SELECT schedule_id FROM tasks WHERE id = $1", run["task_id"]) is None
        assert await env.pool.fetchval("SELECT count(*) FROM schedule_runs") == 0
        assert (await env.call("GET", f"/api/schedules/{sid}"))[0] == 404

    _run(test, monkeypatch, tmp_path)


def test_a_profile_a_schedule_chose_is_not_deleted(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    async def test(env: _Env) -> None:
        created = await env.ok("POST", "/api/agent-profiles", {
            "name": "cheap", "runtime_id": "rt-claude-code", "provider_id": "prov-claude-subscription",
            "model": "haiku"})
        profile_id = created["profile"]["profile_id"]
        sid = (await env.schedule(profile_overrides={"planner": profile_id}))["schedule_id"]

        status, payload = await env.call("DELETE", f"/api/agent-profiles/{profile_id}")
        assert status == 409 and "1 schedule" in payload["error"]

        await env.ok("PUT", f"/api/schedules/{sid}", {"profile_overrides": {}})
        await env.ok("DELETE", f"/api/agent-profiles/{profile_id}")

    _run(test, monkeypatch, tmp_path)


def test_an_export_and_import_with_schedules_present_still_work(monkeypatch: pytest.MonkeyPatch,
                                                                tmp_path: Path) -> None:
    from tests.test_config_transfer_db import _looper

    async def test(env: _Env) -> None:
        await env.enabled(name="nightly")
        parts = urllib.parse.urlsplit(DSN)
        query = urllib.parse.urlencode([*urllib.parse.parse_qsl(parts.query),
                                        ("search_path", await env.pool.fetchval("SELECT current_schema()"))])
        url = urllib.parse.urlunsplit(parts._replace(query=query))
        exported = tmp_path / "config.json"
        code, _, err = await _looper(monkeypatch, url, "config", "export", str(exported), "--include-projects")
        assert code == 0, err
        assert "schedule" not in exported.read_text()  # not exported yet (S3)
        code, _, err = await _looper(monkeypatch, url, "config", "import", str(exported), "--apply")
        assert code == 0, err
        assert await env.pool.fetchval("SELECT count(*) FROM schedules") == 1

    _run(test, monkeypatch, tmp_path)
