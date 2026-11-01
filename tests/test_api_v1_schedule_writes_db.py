"""Schedule writes use authenticated v1 dispatch, real core services and disposable PostgreSQL."""

import asyncio
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


@pytest.mark.parametrize("initial", [None, "off", "on"])
def test_user_gates_create_update_get_and_lists(tmp_path, monkeypatch, initial):
    async def run(pool):
        project = await projects.create_project(name="Reports", work_dir="/srv/reports")
        fields = {**FIELDS, **({"user_gates": initial} if initial is not None else {})}
        async with _api(tmp_path, pool) as call:
            status, answer = await call("create", {"project_id": project["id"], "fields": fields})
            assert status == 200, answer
            sid = answer["schedule"]["schedule_id"]
            assert answer["schedule"]["user_gates"] == (initial or "off")
            for value in ("on", "off"):
                status, result = await call("update", {"schedule_id": sid, "fields": {"user_gates": value}})
                assert status == 200 and result["schedule"]["user_gates"] == value
                for action, body in (("get", {"schedule_id": sid}), ("list", {}),
                                     ("project_list", {"project_id": project["id"]})):
                    _, result = await call(action, body)
                    view = result["schedule"] if action == "get" else result["schedules"][0]
                    assert view["user_gates"] == value
            for invalid in (None, True, "yes"):
                status, _ = await call("update", {"schedule_id": sid, "fields": {"user_gates": invalid}})
                assert status == 400
                assert (await db_schedules.get_schedule(sid))["user_gates"] == "off"
    run_db(run, monkeypatch, timeout=30)


@asynccontextmanager
async def _api(tmp_path, pool, *, scheduler=True):
    _, token = await api_clients.create("Schedule writes", pool=pool)
    bus = Bus()
    async with _serving(tmp_path) as server:
        server._scheduler = Scheduler(bus=bus) if scheduler is True else scheduler or None
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


def test_create_owner_survives_v1_readback(tmp_path, monkeypatch):
    async def run(pool):
        project = await projects.create_project(name="Reports", work_dir="/srv/reports")
        await pool.execute("INSERT INTO managers (id, name, system_prompt) "
                           "VALUES ('manager-reports', 'Reports owner', 'Review reports.')")
        async with _api(tmp_path, pool) as call:
            status, result = await call("create", {"project_id": project["id"],
                "fields": {**FIELDS, "manager_id": "manager-reports"}})
            assert status == 200, result
            sid = result["schedule"]["schedule_id"]
            for action, body in (("get", {"schedule_id": sid}), ("list", {}),
                                 ("project_list", {"project_id": project["id"]})):
                _, result = await call(action, body)
                view = result["schedule"] if action == "get" else result["schedules"][0]
                assert (view["manager_id"], view["manager_name"]) == ("manager-reports", "Reports owner")
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


@pytest.mark.parametrize("kind,action,command,command_patch,work_dir,fence", [
    ("agent", "enable", None, None, "", ""), ("agent", "run", None, None, "", ""),
    ("command", "enable", "exit 0", None, "", "```"),
    ("command", "enable", "exit 0", "printf '`updated`'\nexit 0", "", "```"),
    ("command", "enable", "printf '`ok`'\nexit 0", None, "", "```"),
    ("command", "run", "exit 0", None, "", "```"),
    ("command", "run", "printf '`ok`'\nexit 0", None, "/srv/reports", "```"),
    ("command", "enable", "exit 0\n# ````", None, "", "`````"),
    ("command", "run", "exit 0\n# ````", None, "", "`````"),
])
def test_client_start_impact_describes_schedule_kind(tmp_path, monkeypatch, action, kind, command,
                                                   command_patch, work_dir, fence):
    async def run(pool):
        _, sid = await _seed(pool, kind=kind, work_dir=work_dir,
                             **({"command": command, "requirement": ""} if kind == "command" else {}))
        subject = "schedule 'Hourly' in project 'Reports'"
        async with _api(tmp_path, pool) as call:
            fields = {"enabled": True, **({"command": command_patch} if command_patch else {})}
            status, pending = await call("update" if action == "enable" else "run",
                {"schedule_id": sid, **({"fields": fields} if action == "enable" else {})}, client=True)
            assert status == 202, pending
            if kind == "command":
                fenced = f"\n{fence}\n{command_patch or command}\n{fence}\n"
                expected = (f"Update {subject}: enabled: False → True. Enabling runs its shell command "
                            f"{fenced} in {work_dir or 'the project directory (/srv/reports)'} unattended at each fire, without an agent or model spend."
                            if action == "enable" else
                            f"Run {subject}'s shell command once now, without an agent or model spend: "
                            f"{fenced} in {work_dir or 'the project directory (/srv/reports)'}. Its next fire stays.")
            else:
                expected = (f"Update {subject}: enabled: False → True. Enabling starts unattended scheduled tasks; "
                            "raised or removed caps allow more spending." if action == "enable" else
                            f"Run {subject} once now through its overlap check, spending within its caps. Its next fire stays.")
            assert pending["impact"] == expected
            assert not (await db_schedules.get_schedule(sid))["enabled"]
            assert await pool.fetchval("SELECT count(*) FROM schedule_runs") == 0
    run_db(run, monkeypatch, timeout=30)


@pytest.mark.parametrize("action", ["enable", "run"])
@pytest.mark.parametrize("when", ["before-confirm", "before-lock"])
@pytest.mark.parametrize("changed", [{"command": "exit 7"}, {"env": {"MODE": "apply"}}])
def test_client_command_confirmation_is_bound_to_locked_configuration(tmp_path, monkeypatch, action, when, changed):
    async def run(pool):
        _, sid = await _seed(pool, kind="command", command="exit 0", requirement="", work_dir=str(tmp_path))
        scheduler = Scheduler(bus=Bus(), runtime_root=tmp_path)
        scheduler._commands.prepare()
        try:
            async with _api(tmp_path, pool, scheduler=scheduler) as call:
                body = {"schedule_id": sid, **({"fields": {"enabled": True}} if action == "enable" else {})}
                operation = "update" if action == "enable" else "run"
                status, pending = await call(operation, body, client=True)
                assert status == 202, pending
                assert "\n```\nexit 0\n```\n" in pending["impact"]
                if when == "before-lock":
                    lock = db_schedules.lock_schedule

                    async def change_before_lock(conn, schedule_id):
                        monkeypatch.setattr(db_schedules, "lock_schedule", lock)
                        assert (await call("update", {"schedule_id": sid, "fields": changed}))[0] == 200
                        return await lock(conn, schedule_id)

                    monkeypatch.setattr(db_schedules, "lock_schedule", change_before_lock)
                else:
                    assert (await call("update", {"schedule_id": sid, "fields": changed}))[0] == 200

                status, refused = await call("/api/v1/confirm", {"confirmation_id": pending["confirmation_id"]}, client=True)
                assert status == 409 and refused["error"]["code"] == "conflict", refused
                assert "confirm" in refused["error"]["message"].lower()
                assert not (await db_schedules.get_schedule(sid))["enabled"]
                assert await pool.fetchval("SELECT count(*) FROM schedule_runs") == 0

                status, fresh = await call(operation, body, client=True)
                assert status == 202, fresh
                assert f"\n```\n{changed.get('command', 'exit 0')}\n```\n" in fresh["impact"]
                status, done = await call("/api/v1/confirm", {"confirmation_id": fresh["confirmation_id"]}, client=True)
                assert status == 200, done
                if action == "enable":
                    assert (await db_schedules.get_schedule(sid))["enabled"]
                else:
                    run_id = done["run"]["run_id"]
                    await asyncio.to_thread(scheduler._commands._owned[run_id].wait, timeout=10)
                    await scheduler.tick()
                    saved = await db_schedules.get_run(run_id)
                    assert saved["exit_code"] == (7 if "command" in changed else 0)
        finally:
            await scheduler.stop()
            for process in list(scheduler._commands._owned.values()):
                if process.poll() is None:
                    process.terminate()
                await asyncio.to_thread(process.wait, timeout=10)

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


@pytest.mark.parametrize("action", ["enable", "run"])
def test_client_command_confirmation_is_bound_to_the_project_directory(tmp_path, monkeypatch, action):
    async def run(pool):
        pid, sid = await _seed(pool, kind="command", command="pwd", requirement="")
        async with _api(tmp_path, pool) as call:
            body = {"schedule_id": sid, **({"fields": {"enabled": True}} if action == "enable" else {})}
            status, pending = await call("update" if action == "enable" else "run", body, client=True)
            assert status == 202, pending
            await projects.update_project(pid, work_dir=str(tmp_path))
            status, answer = await call("/api/v1/confirm", {"confirmation_id": pending["confirmation_id"]}, client=True)
            assert status == 409 and answer["error"]["code"] == "conflict", answer
            assert not (await db_schedules.get_schedule(sid))["enabled"]
            assert await pool.fetchval("SELECT count(*) FROM schedule_runs") == 0
    run_db(run, monkeypatch, timeout=30)


@pytest.mark.parametrize("when", ["before-confirm", "before-lock"])
def test_client_directory_edit_confirmation_binds_the_inherited_destination(tmp_path, monkeypatch, when):
    async def run(pool):
        pid, sid = await _seed(pool, kind="command", command="pwd", requirement="", work_dir=str(tmp_path))
        await schedules.update_schedule(sid, {"enabled": True})
        before = await db_schedules.get_schedule(sid)
        body = {"schedule_id": sid, "fields": {"work_dir": ""}}
        async with _api(tmp_path, pool) as call:
            status, pending = await call("update", body, client=True)
            assert status == 202, pending
            assert "the project directory (/srv/reports)" in pending["impact"]
            destination = "/srv/reports-after"
            if when == "before-lock":
                lock = db_schedules.lock_schedule

                async def change_before_lock(conn, schedule_id):
                    monkeypatch.setattr(db_schedules, "lock_schedule", lock)
                    await projects.update_project(pid, work_dir=destination)
                    return await lock(conn, schedule_id)

                monkeypatch.setattr(db_schedules, "lock_schedule", change_before_lock)
            else:
                await projects.update_project(pid, work_dir=destination)
            status, refused = await call("/api/v1/confirm", {"confirmation_id": pending["confirmation_id"]}, client=True)
            assert status == 409 and refused["error"]["code"] == "conflict", refused
            assert await db_schedules.get_schedule(sid) == before
            status, fresh = await call("update", body, client=True)
            assert status == 202, fresh
            assert f"the project directory ({destination})" in fresh["impact"]
            status, done = await call("/api/v1/confirm", {"confirmation_id": fresh["confirmation_id"]}, client=True)
            assert status == 200, done
            assert (await db_schedules.get_schedule(sid))["work_dir"] == ""
    run_db(run, monkeypatch, timeout=30)


@pytest.mark.parametrize("state", ["enabled", "queued", "disabled"])
def test_client_command_edit_of_an_enabled_schedule_waits_for_confirmation(tmp_path, monkeypatch, state):
    async def run(pool):
        _, sid = await _seed(pool, kind="command", command="exit 0", requirement="", env={"MODE": "dry-run"})
        if state == "enabled":
            await schedules.update_schedule(sid, {"enabled": True})
        elif state == "queued":
            await db_schedules.insert_run(pool, schedule_id=sid, scheduled_for=datetime.now(timezone.utc),
                                          trigger="manual", status="queued")
        before = await db_schedules.get_schedule(sid)
        fields = {"command": "exit 7", "env": {"MODE": "apply"}}
        async with _api(tmp_path, pool) as call:
            status, pending = await call("update", {"schedule_id": sid, "fields": fields}, client=True)
            if state != "disabled":
                assert status == 202, pending
                effect = ("It is enabled: its later runs execute the changed command unattended, without an agent or model spend."
                          if state == "enabled" else
                          "It is disabled but has a queued run, which executes the changed command when it starts, without an agent or model spend.")
                assert pending["impact"] == (
                    "Update schedule 'Hourly' in project 'Reports': command: \n```\nexit 0\n```\n → "
                    "\n```\nexit 7\n```\n; env keys: MODE → MODE (values changed: MODE); "
                    "directory: the project directory (/srv/reports). " + effect)
                assert await db_schedules.get_schedule(sid) == before
                status, _ = await call("/api/v1/confirm", {"confirmation_id": pending["confirmation_id"]}, client=True)
            assert status == 200
            row = await db_schedules.get_schedule(sid)
            assert all(row[key] == value for key, value in fields.items())
    run_db(run, monkeypatch, timeout=30)


@pytest.mark.parametrize("state,extra", [
    ("enabled", {"run_budget_usd": None, "daily_budget_usd": None}),
    ("enabled", {"enabled": False}),
    ("queued", {"run_budget_usd": None, "daily_budget_usd": None}),
])
def test_client_command_edit_names_and_applies_combined_changes(tmp_path, monkeypatch, state, extra):
    async def run(pool):
        _, sid = await _seed(pool, kind="command", command="exit 0", requirement="")
        if state == "enabled":
            await schedules.update_schedule(sid, {"enabled": True})
        else:
            await db_schedules.insert_run(pool, schedule_id=sid, scheduled_for=datetime.now(timezone.utc),
                                          trigger="manual", status="queued")
        if "run_budget_usd" in extra:
            # Nonempty caps are rejected for command schedules; removing stored caps is allowed.
            await pool.execute("UPDATE schedules SET run_budget_usd=1, daily_budget_usd=2 WHERE id=$1", sid)
        before = await db_schedules.get_schedule(sid)
        fields = {"command": "exit 7", **extra}
        async with _api(tmp_path, pool) as call:
            status, pending = await call("update", {"schedule_id": sid, "fields": fields}, client=True)
            assert status == 202, pending
            assert "\n```\nexit 0\n```\n → \n```\nexit 7\n```\n" in pending["impact"]
            for key, value in extra.items():
                assert f"{key}: {before[key]!r} → {value!r}" in pending["impact"]
            assert await db_schedules.get_schedule(sid) == before
            status, done = await call("/api/v1/confirm", {"confirmation_id": pending["confirmation_id"]}, client=True)
            assert status == 200, done
            row = await db_schedules.get_schedule(sid)
            assert all(row[key] == value for key, value in fields.items())
    run_db(run, monkeypatch, timeout=30)


@pytest.mark.parametrize("fields", [{"command": ""}, {"command": "exit 7", "cron": "not a rule"}, {"work_dir": ""}])
def test_client_invalid_command_edit_is_refused_before_confirmation(tmp_path, monkeypatch, fields):
    async def run(pool):
        pid, sid = await _seed(pool, kind="command", command="exit 0", requirement="", work_dir=str(tmp_path))
        await schedules.update_schedule(sid, {"enabled": True})
        await projects.update_project(pid, work_dir="")
        before = await db_schedules.get_schedule(sid)
        async with _api(tmp_path, pool) as call:
            status, answer = await call("update", {"schedule_id": sid, "fields": fields}, client=True)
            assert status == 400 and answer["error"]["code"] == "invalid", answer
            assert await db_schedules.get_schedule(sid) == before
    run_db(run, monkeypatch, timeout=30)


@pytest.mark.parametrize("key", ["env_file", "work_dir"])
def test_client_command_edit_shows_environment_file_and_effective_directory(tmp_path, monkeypatch, key):
    async def run(pool):
        _, sid = await _seed(pool, kind="command", command="exit 0", requirement="", work_dir=str(tmp_path))
        await schedules.update_schedule(sid, {"enabled": True})
        env_file = tmp_path / "settings.env"
        env_file.write_text("MODE=private-mode\n")
        fields = {key: str(env_file) if key == "env_file" else ""}
        line = (f"env_file: none → {env_file}" if key == "env_file" else
                f"directory: {tmp_path} → the project directory (/srv/reports)")
        async with _api(tmp_path, pool) as call:
            status, pending = await call("update", {"schedule_id": sid, "fields": fields}, client=True)
            assert status == 202, pending
            assert line in pending["impact"] and "private-mode" not in pending["impact"]
            status, _ = await call("/api/v1/confirm", {"confirmation_id": pending["confirmation_id"]}, client=True)
            assert status == 200
            assert ((await db_schedules.get_schedule(sid))[key] or "") == fields[key]
    run_db(run, monkeypatch, timeout=30)
