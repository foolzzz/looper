"""Full and compact schedule reads through authenticated dispatch and disposable PostgreSQL."""

import json
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import pytest

from looper.agents import api_v1, schedules
from looper.agents.bus import MessageBus
from looper.agents.ops.clients import ClientOps
from looper.agents.ops.service import Platform
from looper.db import api_clients, projects, schedules as db_schedules
from tests.db_support import DSN, run_db
from tests.test_api_v1_auth import _bearer, _send, _serving
from tests.test_operator_auth import TOKEN

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")


@asynccontextmanager
async def _api(tmp_path, pool):
    _, token = await api_clients.create("Schedule reads", pool=pool)
    async with _serving(tmp_path) as server:
        server._client_ops = ClientOps(Platform(bus=MessageBus(runtime_root=tmp_path),
                                               task_router=server._task_router, runtime_root=tmp_path))
        async def call(action, body=None, *, client=True, method="POST"):
            path = action if action.startswith("/") else f"/api/v1/schedule_admin/{action}"
            status, _, answer = await _send(server, f"{method} {path}", _bearer(token if client else TOKEN),
                                            json.dumps(body or {}))
            if status in (200, 201) and path.startswith("/api/v1/"):
                assert answer["status"] == "ok"
                result = answer["result"]
                schema = api_v1.find(method, path).result
                api_v1.validate_arguments(schema, result)
                return status, result
            return status, answer
        yield call


async def _seed(pool):
    project = await projects.create_project(name="Example", work_dir="/srv/example")
    view = (await schedules.create_schedule(project["id"], {"name": "Hourly", "cron": "0 * * * *",
        "timezone": "UTC", "requirement": "Read the project reports. " * 100}))["schedule"]
    sid = view["schedule_id"]
    now = datetime.now(timezone.utc)
    await pool.execute("INSERT INTO tasks (id, project_id, title, requirement, work_dir, status) "
                       "VALUES ('task-example', $1, 'Latest report', 'Read reports', '/srv/example', 'completed')", project["id"])
    run_id = await db_schedules.insert_run(pool, schedule_id=sid, scheduled_for=now, fired_at=now,
        finished_at=now, trigger="manual", status="completed", task_id="task-example")
    await db_schedules.update_run(pool, run_id, cost_usd=0.75, summary="Reports checked")
    await pool.execute("UPDATE schedules SET planner_session_id = 'session-example', planner_session_runtime = 'codex', "
        "planner_session_profile = 'example-profile', planner_session_task_id = 'task-example', "
        "planner_session_since = $2 WHERE id = $1", sid, now)
    return project["id"], sid, run_id


@pytest.mark.parametrize("client", [True, False])
def test_full_list_get_and_project_limits_keep_requirement_spend_and_session(tmp_path, monkeypatch, client):
    async def run(pool):
        pid, sid, _ = await _seed(pool)
        async with _api(tmp_path, pool) as call:
            for action, body in (("list", {}), ("get", {"schedule_id": sid}), ("project_list", {"project_id": pid})):
                status, result = await call(action, body, client=client)
                assert status == 200
                view = result["schedule"] if action == "get" else result["schedules"][0]
                assert view["requirement"] == "Read the project reports. " * 99 + "Read the project reports."
                assert view["spent_today_usd"] == 0.75
                assert view["manager_id"] == "manager"
                assert isinstance(view["manager_name"], str) or view["manager_name"] is None
                assert view["last_run"]["manager_id"] == "manager"
                assert view["last_run"]["summary"] == "Reports checked"
                assert view["planner_session"]["active"] is True
                assert view["planner_session"]["last_task_title"] == "Latest report"
                assert "session-example" not in json.dumps(view)
                if action == "project_list":
                    limits = result["limits"]
                    assert limits["run_timeout_max_seconds"] > limits["gate_timeout_default_seconds"]
                    assert limits["roles"] == ["planner", "worker", "reviewer"]
    run_db(run, monkeypatch, timeout=30)


def test_compact_get_stays_clipped_and_compact_list_keeps_its_shape(tmp_path, monkeypatch):
    async def run(pool):
        _, sid, rid = await _seed(pool)
        await pool.execute("UPDATE schedule_runs SET firings = 3 WHERE id = $1", rid)
        async with _api(tmp_path, pool) as call:
            _, full = await call("get", {"schedule_id": sid})
            _, compact = await call("/api/v1/ops/schedules/get", {"schedule": sid})
            assert len(full["schedule"]["requirement"]) > 1000
            assert len(compact["schedule"]["requirement"]) == 1000
            assert "last_run" not in compact["schedule"]
            assert not {"manager_id", "manager_name"} & compact["schedule"].keys()
            assert compact["latest_runs"][0]["firings"] == 3
            _, listed = await call("/api/v1/ops/schedules/list")
            assert listed["schedules"][0]["last_run"]["run_id"] == rid
            assert "requirement" not in listed["schedules"][0]
    run_db(run, monkeypatch, timeout=30)


def test_run_cursor_paging_has_no_duplicates_or_loss_and_rejects_foreign_cursors(tmp_path, monkeypatch):
    async def run(pool):
        pid, sid, first = await _seed(pool)
        now = datetime.now(timezone.utc)
        ids = [first]
        for i in range(5):
            ids.append(await db_schedules.insert_run(pool, schedule_id=sid, scheduled_for=now,
                trigger="scheduled", status="skipped_budget", created_at=now + timedelta(seconds=i)))
        other = (await schedules.create_schedule(pid, {"name": "Other", "cron": "0 * * * *",
            "requirement": "Read", "timezone": "UTC"}))["schedule"]["schedule_id"]
        async with _api(tmp_path, pool) as call:
            seen, before = [], None
            while True:
                status, page = await call("runs", {"schedule_id": sid, "limit": 2, "before": before})
                assert status == 200 and page["runs"]
                seen.extend(row["run_id"] for row in page["runs"])
                before = page["next_before"]
                if before is None:
                    break
            assert seen == list(reversed(ids))
            for wanted, cursor in ((sid, "run-missing"), (other, first)):
                status, answer = await call("runs", {"schedule_id": wanted, "before": cursor})
                assert status == 400 and answer["error"]["code"] == "invalid"
    run_db(run, monkeypatch, timeout=30)


def test_hidden_routine_skips_keep_aggregate_firing_counts(tmp_path, monkeypatch):
    async def run(pool):
        _, sid, first = await _seed(pool)
        for status, count in (("skipped_gate", 7), ("skipped_overlap", 4)):
            rid = await db_schedules.insert_run(pool, schedule_id=sid, scheduled_for=datetime.now(timezone.utc),
                                               trigger="scheduled", status=status)
            await pool.execute("UPDATE schedule_runs SET firings = $2 WHERE id = $1", rid, count)
        async with _api(tmp_path, pool) as call:
            _, hidden = await call("runs", {"schedule_id": sid, "skips": "hide"})
            assert [row["run_id"] for row in hidden["runs"]] == [first]
            _, shown = await call("runs", {"schedule_id": sid})
            assert len(shown["runs"]) == 3
            assert {row["manager_id"] for row in shown["runs"]} == {"manager"}
            assert shown["routine_skips"] == hidden["routine_skips"] == {"skipped_gate": 7, "skipped_overlap": 4}
    run_db(run, monkeypatch, timeout=30)


def test_notification_order_and_limit_preserve_null_fields(tmp_path, monkeypatch):
    async def run(pool):
        _, sid, rid = await _seed(pool)
        ids = []
        for kind in ("run", "failures_alert", "failures_recovered"):
            ids.append(await db_schedules.insert_notification(pool, schedule_id=sid, run_id=rid, kind=kind,
                channel=None, chat=None, text="Report available", status="dashboard", error=None))
        async with _api(tmp_path, pool) as call:
            _, result = await call("notifications", {"schedule_id": sid, "limit": 2})
            assert [row["notification_id"] for row in result["notifications"]] == list(reversed(ids))[:2]
            assert result["notifications"][0]["channel"] is None
    run_db(run, monkeypatch, timeout=30)


@pytest.mark.parametrize("action,body", [("get", {}), ("get", {"schedule_id": 4}),
    ("runs", {"schedule_id": "sched-missing", "limit": 0}),
    ("runs", {"schedule_id": "sched-missing", "limit": 201}),
    ("runs", {"schedule_id": "sched-missing", "before": 1}),
    ("runs", {"schedule_id": "sched-missing", "skips": "all"}),
    ("notifications", {"schedule_id": "sched-missing", "limit": "2"})])
def test_bad_identifiers_and_paging_parameters_are_typed_invalid(tmp_path, monkeypatch, action, body):
    async def run(pool):
        async with _api(tmp_path, pool) as call:
            status, answer = await call(action, body)
            assert status == 400 and answer["error"]["code"] == "invalid"
    run_db(run, monkeypatch, timeout=30)


@pytest.mark.parametrize("action", ["project_list", "get", "runs", "notifications"])
def test_unknown_schedule_or_project_is_typed_not_found(tmp_path, monkeypatch, action):
    async def run(pool):
        async with _api(tmp_path, pool) as call:
            body = {"project_id": "proj-missing"} if action == "project_list" else {"schedule_id": "sched-missing"}
            status, answer = await call(action, body)
            assert status == 404 and answer["error"]["code"] == "not_found"
    run_db(run, monkeypatch, timeout=30)


def test_old_reads_are_404_while_corresponding_v1_reads_and_writes_work(tmp_path, monkeypatch):
    async def run(pool):
        pid, sid, rid = await _seed(pool)
        await db_schedules.insert_notification(pool, schedule_id=sid, run_id=rid, kind="run", channel=None,
            chat=None, text="Report available", status="dashboard", error=None)
        async with _api(tmp_path, pool) as call:
            for method, path, action, body in (
                ("GET", "/api/schedules", "list", {}),
                ("GET", f"/api/projects/{pid}/schedules", "project_list", {"project_id": pid}),
                ("GET", f"/api/schedules/{sid}", "get", {"schedule_id": sid}),
                ("GET", f"/api/schedules/{sid}/runs", "runs", {"schedule_id": sid}),
                ("GET", f"/api/schedules/{sid}/notifications", "notifications", {"schedule_id": sid}),
                ("POST", "/api/schedules/preview", "preview", {"cron": "0 * * * *"})):
                assert (await call(action, body, client=False))[0] == 200
                assert (await call(path, body, method=method, client=False))[0] == 404
            assert (await call("update", {"schedule_id": sid, "fields": {"name": "Renamed"}}, client=False))[0] == 200
            assert (await call("get", {"schedule_id": sid}))[1]["schedule"]["name"] == "Renamed"
            assert (await call("reset_planner_session", {"schedule_id": sid}, client=False))[1] == {"cleared": True}
            status, created = await call("create", {"project_id": pid, "fields": {"name": "Disabled", "cron": "0 * * * *",
                "requirement": "Read", "timezone": "UTC"}}, client=False)
            assert status == 200
            assert (await call("delete", {"schedule_id": created['schedule']['schedule_id']}, client=False))[0] == 200
    run_db(run, monkeypatch, timeout=30)
