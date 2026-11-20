"""Persistent schedule session bounds, telemetry and task-budget alert contracts."""

from __future__ import annotations

import pytest

from looper.agents import scheduler_settings
from looper.agents.schedule_notify import failures_alert_text
from looper.db import schedules, tasks
from tests.db_support import DSN
from tests.schedule_support import run_scheduled

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")


@pytest.mark.parametrize("name,default,own", [
    ("session_context_max_tokens", 100_000, 40_000),
])
def test_session_bound_settings_validate_and_reset(monkeypatch, tmp_path, name, default, own):
    async def test(env):
        view = await env.ok("POST", "/api/v1/scheduler_settings/get", {})
        assert view[name]["value"] == default
        saved = await env.ok("POST", "/api/v1/scheduler_settings/update", {name: own})
        assert saved[name]["value"] == own and saved[name]["source"] == "setting"
        for bad in (0, -1, 39_999, True, 1.5, "5000", scheduler_settings.SESSION_SETTING_LIMITS[name] + 1):
            status, _ = await env.call("POST", "/api/v1/scheduler_settings/update", {name: bad})
            assert status == 400
        assert (await env.ok("POST", "/api/v1/scheduler_settings/get", {}))[name]["value"] == own
        assert (await env.ok("POST", "/api/v1/scheduler_settings/update", {name: None}))[name] == view[name]
    run_scheduled(test, monkeypatch, tmp_path)


def test_session_metrics_follow_holder_and_reset_and_runs_keep_audit(monkeypatch, tmp_path):
    async def test(env):
        sid = (await env.schedule(run_budget_usd=2))["schedule_id"]
        await tasks.create_task(task_id="first", requirement="Read a report", project_id=env.project_id,
                                work_dir=env.work_dir)
        await env.pool.execute("UPDATE tasks SET schedule_id = $1, status = 'running' WHERE id = 'first'", sid)
        await schedules.claim_planner_session("first")
        await schedules.save_planner_session("first", "session", {}, runtime_type="claude-code",
                                             provider_id=None, profile_id=None)
        run_id = await schedules.insert_run(env.pool, schedule_id=sid, scheduled_for=env.clock(), trigger="manual",
                                            status="running", task_id="first")
        await schedules.record_planner_session_run("first", context_tokens=120_000, planner_cost_usd=1.1)
        await schedules.record_planner_session_bound("first", action="compacted", reason="context",
                                                     before_tokens=120_000, after_tokens=15_000)
        row = await schedules.get_schedule(sid)
        assert (row["planner_session_context_tokens"], row["planner_session_run_cost_usd"]) == (120_000, 1.1)
        run = await schedules.get_run(run_id)
        assert (run["planner_context_tokens"], run["planner_cost_usd"]) == (120_000, 1.1)
        assert (run["planner_session_action"], run["planner_session_reason"],
                run["planner_session_before_tokens"], run["planner_session_after_tokens"]) == (
                    "compacted", "context", 120_000, 15_000)
        await schedules.save_planner_session("first", "session", {}, runtime_type="claude-code",
                                             provider_id=None, profile_id=None)
        assert (await schedules.get_schedule(sid))["planner_session_context_tokens"] == 120_000
        await schedules.reset_planner_session(sid)
        await schedules.record_planner_session_run("first", context_tokens=999_000, planner_cost_usd=8)
        row = await schedules.get_schedule(sid)
        assert row["planner_session_context_tokens"] is None and row["planner_session_run_cost_usd"] is None
        assert (await schedules.get_run(run_id))["planner_session_action"] == "compacted"
    run_scheduled(test, monkeypatch, tmp_path)


def test_claim_carries_last_run_measurements_without_duplicate_history_or_budget(monkeypatch, tmp_path):
    async def test(env):
        sid = (await env.schedule(run_budget_usd=2))["schedule_id"]
        for index in range(5):
            await schedules.insert_run(env.pool, schedule_id=sid, scheduled_for=env.clock(), trigger="manual",
                                        status="completed", finished_at=env.clock(), run_id=f"run-{index}")
            await schedules.update_run(env.pool, f"run-{index}", summary=f"report {index}")
        await env.pool.execute("UPDATE schedules SET planner_session_context_tokens=120000, "
                               "planner_session_run_cost_usd=1.1 WHERE id=$1", sid)
        await tasks.create_task(task_id="next", requirement="Read", project_id=env.project_id, work_dir=env.work_dir)
        await env.pool.execute("UPDATE tasks SET schedule_id=$1 WHERE id='next'", sid)
        claim = await schedules.claim_planner_session("next")
        assert (claim["context_tokens"], claim["run_cost_usd"]) == (120_000, 1.1)
        assert "recent_runs" not in claim and "run_budget_usd" not in claim
        assert claim["previous"]["summary"] == "report 4"
    run_scheduled(test, monkeypatch, tmp_path)


def test_budget_failure_alert_uses_typed_reason_and_effective_context_threshold(monkeypatch, tmp_path):
    async def test(env):
        sid = (await env.schedule(notify="alerts"))["schedule_id"]
        await scheduler_settings.update({"session_context_max_tokens": 40_000})
        for index in range(3):
            task_id = f"budget-{index}"
            await tasks.create_task(task_id=task_id, requirement="Read", project_id=env.project_id, work_dir=env.work_dir)
            await env.pool.execute("UPDATE tasks SET schedule_id=$1, status='failed', fail_kind='task_budget', "
                                   "error='Done: nothing' WHERE id=$2", sid, task_id)
            run_id = await schedules.insert_run(env.pool, schedule_id=sid, scheduled_for=env.clock(), trigger="manual",
                                                status="running", task_id=task_id)
            await env.pool.execute("UPDATE schedule_runs SET planner_context_tokens=60_000 WHERE id=$1", run_id)
            await env.scheduler.task_ended(task_id, "failed")
        await env.scheduler.idle()
        notices = await schedules.list_notifications(sid, limit=20)
        alert = next(row for row in notices if row["kind"] == "failures_alert")
        assert "任务预算" in alert["text"]
        assert "60000" in alert["text"] and "40000" in alert["text"]
        assert (await schedules.get_run(run_id))["failure_kind"] == "task_budget"
        plain = failures_alert_text({"id":sid, "project_id":env.project_id, "name":"report", "consecutive_failures":3}, "Example",
                                    {"status":"failed", "error":"task_budget appeared in a source document"})
        assert "任务预算" not in plain
    run_scheduled(test, monkeypatch, tmp_path)


@pytest.mark.parametrize("status", ["completed", "cancelled"])
def test_successful_or_cancelled_run_does_not_publish_a_stale_failure_kind(monkeypatch, tmp_path, status):
    async def test(env):
        sid = (await env.schedule())["schedule_id"]
        await tasks.create_task(task_id="recovered", requirement="Read", project_id=env.project_id, work_dir=env.work_dir)
        await env.pool.execute("UPDATE tasks SET schedule_id=$1, status=$2, fail_kind='task_budget' WHERE id='recovered'",
                               sid, status)
        run_id = await schedules.insert_run(env.pool, schedule_id=sid, scheduled_for=env.clock(), trigger="manual",
                                            status="running", task_id="recovered")
        await env.scheduler.task_ended("recovered", status)
        await env.scheduler.idle()
        row = await schedules.get_run(run_id)
        assert row["status"] == status and row["failure_kind"] is None
    run_scheduled(test, monkeypatch, tmp_path)


def test_measurements_use_all_task_planner_turns_and_rotation_records_first_context(monkeypatch, tmp_path):
    async def test(env):
        sid = (await env.schedule())["schedule_id"]
        await tasks.create_task(task_id="measured", requirement="Read", project_id=env.project_id, work_dir=env.work_dir)
        await env.pool.execute("UPDATE tasks SET schedule_id=$1 WHERE id='measured'", sid)
        await schedules.claim_planner_session("measured")
        run_id = await schedules.insert_run(env.pool, schedule_id=sid, scheduled_for=env.clock(), trigger="manual",
                                            status="running", task_id="measured")
        await schedules.record_planner_session_bound("measured", action="rotated", reason="cost",
                                                     before_tokens=120_000, after_tokens=None)
        for agent, cost in (("planner-measured", .2), ("worker-measured", 9), ("planner-measured", .3)):
            await env.pool.execute("INSERT INTO llm_records (agent_id, task_id, cost_usd) VALUES ($1,'measured',$2)",
                                   agent, cost)
        await schedules.record_planner_session_run("measured", context_tokens=12_000, planner_cost_usd=.3)
        await schedules.record_planner_session_run("measured", context_tokens=13_000, planner_cost_usd=.3)
        row = await schedules.get_run(run_id)
        assert row["planner_cost_usd"] == .5
        assert row["planner_context_tokens"] == 13_000 and row["planner_session_after_tokens"] == 12_000
        await env.pool.execute("INSERT INTO llm_records (agent_id,task_id) VALUES ('planner-measured','measured')")
        await schedules.record_planner_session_run("measured", context_tokens=None, planner_cost_usd=.3)
        row = await schedules.get_run(run_id)
        assert row["planner_cost_usd"] is None and row["planner_context_tokens"] == 13_000
    run_scheduled(test, monkeypatch, tmp_path)


def test_existing_databases_add_session_bounds_once_and_keep_saved_state(monkeypatch, tmp_path):
    from looper.db import connection

    async def test(env):
        sid = (await env.schedule())["schedule_id"]
        await env.pool.execute("UPDATE schedules SET planner_session_id='retained' WHERE id=$1", sid)
        await env.pool.execute("ALTER TABLE schedules DROP COLUMN planner_session_context_tokens, "
                               "DROP COLUMN planner_session_run_cost_usd")
        await env.pool.execute("ALTER TABLE schedule_runs DROP COLUMN planner_context_tokens, "
                               "DROP COLUMN planner_cost_usd, DROP COLUMN planner_session_action, "
                               "DROP COLUMN planner_session_reason, DROP COLUMN planner_session_before_tokens, "
                               "DROP COLUMN planner_session_after_tokens, DROP COLUMN failure_kind")
        async with env.pool.acquire() as conn:
            assert await connection.schema_change_pending(conn)
        await connection.ensure_schema(env.pool)
        row = await schedules.get_schedule(sid)
        assert row["planner_session_id"] == "retained" and row["planner_session_context_tokens"] is None
        await connection.ensure_schema(env.pool)
        assert await env.pool.fetchval("SELECT count(*) FROM schema_migrations WHERE name='schedule_session_bounds'") == 1
    run_scheduled(test, monkeypatch, tmp_path)
