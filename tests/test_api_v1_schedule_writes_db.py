"""Schedule writes use authenticated v1 dispatch, real core services and disposable PostgreSQL."""

import json
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import pytest

from looper.agents import api_v1, schedules
from looper.agents.agent_settings import BUDGET_MAX_USD, CONCURRENCY_MAX
from looper.agents.ops.clients import ClientOps
from looper.agents.ops.service import Platform
from looper.agents.scheduler import Scheduler
from looper.db import api_clients, projects, schedules as db_schedules
from tests.db_support import DSN, run_db
from tests.schedule_support import Bus
from tests.test_api_v1_auth import _bearer, _send, _serving
from tests.test_operator_auth import TOKEN

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")
FIELDS = {"name": "Hourly", "cron": "0 * * * *", "timezone": "UTC", "requirement": "Read reports"}


@asynccontextmanager
async def _api(tmp_path, pool, *, scheduler=True):
    _, token = await api_clients.create("Schedule writes", pool=pool)
    bus = Bus()
    async with _serving(tmp_path) as server:
        server._scheduler = Scheduler(bus=bus) if scheduler else None
        server._client_ops = ClientOps(Platform(bus=bus, task_router=server._task_router,
                                               scheduler=server._scheduler, runtime_root=tmp_path))
        async def call(action, body=None, *, client=False, session="one", method="POST"):
            path = action if action.startswith("/") else f"/api/v1/schedule_admin/{action}"
            status, _, answer = await _send(server, f"{method} {path}",
                {**_bearer(token if client else TOKEN), "X-Looper-Session": session}, json.dumps(body or {}))
            if status == 200 and path.startswith("/api/v1/"):
                assert answer["status"] == "ok", answer
                result = answer["result"]
                if path != "/api/v1/confirm":
                    api_v1.validate_arguments(api_v1.find(method, path).result, result)
                assert "shown_to_user" not in result
                return status, result
            return status, answer
        try:
            yield call
        finally:
            if server._scheduler:
                await server._scheduler.idle()


async def _seed(pool, **fields):
    project = await projects.create_project(name="Reports", work_dir="/srv/reports")
    view = (await schedules.create_schedule(project["id"], {**FIELDS, **fields}))["schedule"]
    return project["id"], view["schedule_id"]


def test_create_is_disabled_rejects_enabled_and_reads_back_saved_fields(tmp_path, monkeypatch):
    async def run(pool):
        project = await projects.create_project(name="Reports", work_dir="/srv/reports")
        fields = {**FIELDS, "overlap": "parallel", "max_parallel_runs": 10, "catch_up": "skip", "notify": "never"}
        async with _api(tmp_path, pool) as call:
            status, answer = await call("create", {"project_id": project["id"], "fields": {**fields, "enabled": True}})
            assert status == 400 and answer["error"]["code"] == "invalid"
            assert await pool.fetchval("SELECT count(*) FROM schedules") == 0
            status, answer = await call("create", {"project_id": project["id"], "fields": fields}, client=True)
            assert status == 200, answer
            view = answer["schedule"]
            assert view["enabled"] is False and view["next_fire_at"] is None
            _, readback = await call("get", {"schedule_id": view["schedule_id"]})
            assert all(readback["schedule"][key] == value for key, value in fields.items())
    run_db(run, monkeypatch, timeout=30)


@pytest.mark.parametrize("timeout", ["default", None, 60])
def test_timeout_budget_profile_and_task_limit_variants_are_saved(tmp_path, monkeypatch, timeout):
    async def run(pool):
        _, sid = await _seed(pool)
        await pool.execute("INSERT INTO agent_profiles (id,name,runtime_id,model_id) VALUES "
            "('profile-schedule','Scheduled','rt-claude-code',(SELECT id FROM llm_models "
            "WHERE provider_id='prov-claude-subscription' AND name='sonnet'))")
        async with _api(tmp_path, pool) as call:
            fields = {"run_timeout_seconds": timeout, "run_budget_usd": 2.5, "daily_budget_usd": 10,
                "gate_timeout_seconds": None, "gate_command": None, "notify_target": None,
                "profile_overrides": {"planner": "profile-schedule", "worker": None},
                "task_limits": {"worker": 2, "reviewer": None}}
            status, result = await call("update", {"schedule_id": sid, "fields": fields})
            assert status == 200, result
            view = result["schedule"]
            assert view["run_timeout_seconds"] == timeout
            assert (view["run_budget_usd"], view["daily_budget_usd"]) == (2.5, 10)
            assert view["profile_overrides"] == {"planner": "profile-schedule"}
            assert view["task_limits"]["worker"] == 2 and view["task_limits"].get("reviewer") is None
            _, cleared = await call("update", {"schedule_id": sid, "fields": {"profile_overrides": None, "task_limits": None}})
            assert cleared["schedule"]["profile_overrides"] == {} and not any(cleared["schedule"]["task_limits"].values())
    run_db(run, monkeypatch, timeout=30)



@pytest.mark.parametrize("fields", [{"run_budget_usd": BUDGET_MAX_USD + 1}, {"daily_budget_usd": 0.00001},
    {"run_timeout_seconds": schedules.RUN_TIMEOUT_MAX_SECONDS + 1},
    {"gate_timeout_seconds": schedules.GATE_TIMEOUT_MAX_SECONDS + 1},
    {"task_limits": {"worker": CONCURRENCY_MAX + 1}}, {"run_budget_usd": "2.5"}])
def test_invalid_caps_fail_without_saving_fields(tmp_path, monkeypatch, fields):
    async def run(pool):
        _, sid = await _seed(pool)
        before = await db_schedules.get_schedule(sid)
        async with _api(tmp_path, pool) as call:
            status, answer = await call("update", {"schedule_id": sid, "fields": fields}, client=True)
            assert status == 400 and answer["error"]["code"] == "invalid"
            assert await db_schedules.get_schedule(sid) == before
    run_db(run, monkeypatch, timeout=30)


def test_client_enable_and_cap_raise_wait_for_confirmation_and_disable_clears_fire(tmp_path, monkeypatch):
    async def run(pool):
        _, sid = await _seed(pool, run_budget_usd=1)
        async with _api(tmp_path, pool) as call:
            for fields in ({"enabled": True}, {"run_budget_usd": 2}, {"run_budget_usd": None}):
                before = await db_schedules.get_schedule(sid)
                status, pending = await call("update", {"schedule_id": sid, "fields": fields}, client=True)
                assert status == 202 and pending["status"] == "needs_confirmation"
                assert await db_schedules.get_schedule(sid) == before
                confirmation = {"confirmation_id": pending["confirmation_id"]}
                assert (await call("/api/v1/confirm", confirmation, client=True, session="other"))[0] == 404
                assert (await call("/api/v1/confirm", confirmation, client=True))[0] == 200
            row = await db_schedules.get_schedule(sid)
            assert row["enabled"] and row["next_fire_at"] > datetime.now(timezone.utc)
            status, result = await call("update", {"schedule_id": sid, "fields": {"enabled": False}}, client=True)
            assert status == 200 and result["schedule"]["next_fire_at"] is None
    run_db(run, monkeypatch, timeout=30)



@pytest.mark.parametrize("schedule_cap,project_cap,after,high", [(None, 1, 2, True), (None, 2, 1, False),
    (1, 2, None, True), (2, 1, None, False)])
def test_run_cap_uses_inherited_project_budget_for_risk(tmp_path, monkeypatch, schedule_cap, project_cap, after, high):
    async def run(pool):
        pid, sid = await _seed(pool, run_budget_usd=schedule_cap)
        await projects.update_project(pid, execution={"task_budget_usd": project_cap})
        async with _api(tmp_path, pool) as call:
            status, answer = await call("update", {"schedule_id": sid, "fields": {"run_budget_usd": after}}, client=True)
            assert status == (202 if high else 200), answer
            if high:
                assert (await db_schedules.get_schedule(sid))["run_budget_usd"] == schedule_cap
                assert (await call("/api/v1/confirm", {"confirmation_id": answer["confirmation_id"]}, client=True))[0] == 200
            assert (await db_schedules.get_schedule(sid))["run_budget_usd"] == after
    run_db(run, monkeypatch, timeout=30)


@pytest.mark.parametrize("action", ["delete", "reset_planner_session"])
def test_client_delete_and_reset_wait_without_mutation_until_confirmed(tmp_path, monkeypatch, action):
    async def run(pool):
        _, sid = await _seed(pool)
        await pool.execute("UPDATE schedules SET planner_session_id='stored-session' WHERE id=$1", sid)
        async with _api(tmp_path, pool) as call:
            status, pending = await call(action, {"schedule_id": sid}, client=True)
            assert status == 202 and pending["impact"]
            assert (await db_schedules.get_schedule(sid))["planner_session_id"] == "stored-session"
            status, result = await call("/api/v1/confirm", {"confirmation_id": pending["confirmation_id"]}, client=True)
            assert status == 200, result
            row = await db_schedules.get_schedule(sid)
            assert row is None if action == "delete" else row["planner_session_id"] is None
    run_db(run, monkeypatch, timeout=30)


def test_confirming_deleted_schedule_rechecks_existence(tmp_path, monkeypatch):
    async def run(pool):
        _, sid = await _seed(pool)
        async with _api(tmp_path, pool) as call:
            status, pending = await call("run", {"schedule_id": sid}, client=True)
            assert status == 202 and await pool.fetchval("SELECT count(*) FROM schedule_runs") == 0
            assert (await call("delete", {"schedule_id": sid}))[0] == 200
            status, answer = await call("/api/v1/confirm", {"confirmation_id": pending["confirmation_id"]}, client=True)
            assert status == 404 and answer["error"]["code"] == "not_found"
    run_db(run, monkeypatch, timeout=30)


def test_manual_run_uses_overlap_and_keeps_next_fire_then_delete_keeps_tasks(tmp_path, monkeypatch):
    async def run(pool):
        _, sid = await _seed(pool)
        async with _api(tmp_path, pool) as call:
            await call("update", {"schedule_id": sid, "fields": {"enabled": True}})
            fire = (await db_schedules.get_schedule(sid))["next_fire_at"]
            _, first = await call("run", {"schedule_id": sid})
            _, second = await call("run", {"schedule_id": sid})
            assert first["run"]["status"] == "running" and second["run"]["status"] == "skipped_overlap"
            assert (await db_schedules.get_schedule(sid))["next_fire_at"] == fire
            await db_schedules.insert_notification(pool, schedule_id=sid, run_id=first["run"]["run_id"], kind="run",
                channel=None, chat=None, text="Report", status="dashboard", error=None)
            assert (await call("delete", {"schedule_id": sid}))[1] == {"deleted": sid}
            assert await pool.fetchval("SELECT schedule_id FROM tasks WHERE id=$1", first["run"]["task_id"]) is None
            assert await pool.fetchval("SELECT count(*) FROM tasks") == 1
            for table in ("schedules", "schedule_runs", "schedule_notifications"):
                assert await pool.fetchval(f"SELECT count(*) FROM {table}") == 0
    run_db(run, monkeypatch, timeout=30)


def test_no_scheduler_is_conflict_and_reset_has_real_cleared_readback(tmp_path, monkeypatch):
    async def run(pool):
        _, sid = await _seed(pool)
        async with _api(tmp_path, pool, scheduler=False) as call:
            status, answer = await call("run", {"schedule_id": sid})
            assert status == 409 and answer["error"]["code"] == "conflict"
            await pool.execute("UPDATE schedules SET planner_session_id='stored-session' WHERE id=$1", sid)
            assert (await call("reset_planner_session", {"schedule_id": sid}))[1] == {"cleared": True}
            assert (await call("reset_planner_session", {"schedule_id": sid}))[1] == {"cleared": False}
            assert (await call("get", {"schedule_id": sid}))[1]["schedule"]["planner_session"]["active"] is False
    run_db(run, monkeypatch, timeout=30)


def test_actual_compact_tool_write_results_validate_without_shown_to_user(tmp_path, monkeypatch):
    async def run(pool):
        await _seed(pool)
        async with _api(tmp_path, pool) as call:
            async def compact(action, **body):
                status, result = await call(f"/api/v1/ops/schedules/{action}", body)
                assert status == 200, result
                return result
            created = await compact("create", project="Reports", fields={**FIELDS, "name": "Compact"})
            sid = created["schedule"]["schedule_id"]
            assert created["enabled"] is False
            assert (await compact("update", schedule=sid, fields={"requirement": "Review reports"}))["changed"] == ["requirement"]
            assert (await compact("enable", schedule=sid))["schedule"]["enabled"] is True
            assert (await compact("enable", schedule=sid))["note"]
            assert (await compact("disable", schedule=sid))["schedule"]["enabled"] is False
            assert (await compact("run", schedule=sid))["run"]["task_id"]
            assert (await compact("reset_planner_session", schedule=sid))["cleared"] is False
            assert (await compact("delete", schedule=sid))["schedule_id"] == sid
    run_db(run, monkeypatch, timeout=30)


@pytest.mark.parametrize("action,method,suffix", [("create", "POST", None), ("update", "PUT", ""),
    ("delete", "DELETE", ""), ("run", "POST", "/run"), ("reset_planner_session", "POST", "/reset-planner-session")])
def test_retired_write_is_404_with_successful_authenticated_v1_control(tmp_path, monkeypatch, action, method, suffix):
    async def run(pool):
        pid, sid = await _seed(pool)
        body = {"project_id": pid, "fields": {**FIELDS, "name": "Created"}} if action == "create" else {"schedule_id": sid}
        if action == "update":
            body["fields"] = {"name": "Renamed"}
        async with _api(tmp_path, pool) as call:
            path = f"/api/projects/{pid}/schedules" if suffix is None else f"/api/schedules/{sid}{suffix}"
            assert (await call(path, body.get("fields", {}), method=method))[0] == 404
            assert (await call(action, body))[0] == 200
    run_db(run, monkeypatch, timeout=30)
