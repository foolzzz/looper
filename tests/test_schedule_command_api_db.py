"""Command schedules and binary run logs through v1 and disposable PostgreSQL."""

import base64
import os
from datetime import datetime, timedelta, timezone

import pytest

from looper.db import projects, schedules as db_schedules
from tests.db_support import DSN, run_db
from tests.test_api_v1_schedule_reads_db import _api

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")


async def _create(call, project_id, **fields):
    status, result = await call("create", {"project_id": project_id, "fields": {
        "name": "Command", "kind": "command", "command": "printf hello", "cron": "* * * * *",
        "timezone": "UTC", **fields}}, client=False)
    assert status == 200, result
    return result["schedule"]


async def _run(pool, sid, *, status="succeeded", log_path=None, finished_at=None, seconds=5,
               started=True):
    end = finished_at or datetime.now(timezone.utc)
    begin = end - timedelta(seconds=seconds)
    return await db_schedules.insert_run(pool, schedule_id=sid, scheduled_for=begin,
        fired_at=begin - timedelta(seconds=100) if started else begin,
        started_at=begin if started else None, finished_at=None if status in db_schedules.OPEN_STATUSES else end,
        trigger="scheduled", status=status, log_path=log_path)


def test_agent_profile_update_completes_with_one_database_connection(tmp_path, monkeypatch):
    async def run(pool):
        project = await projects.create_project(name="Example", work_dir=str(tmp_path))
        async with _api(tmp_path, pool) as call:
            status, result = await call("create", {"project_id": project["id"], "fields": {
                "name": "Agent", "cron": "0 * * * *", "requirement": "Read"}}, client=False)
            assert status == 200, result
            sid = result["schedule"]["schedule_id"]
            status, result = await call("update", {"schedule_id": sid,
                "fields": {"profile_overrides": {"planner": "agent-claude-code-opus"}}}, client=False)
            assert status == 200, result
            assert result["schedule"]["profile_overrides"] == {"planner": "agent-claude-code-opus"}
    run_db(run, monkeypatch, timeout=15, max_size=1)


def test_command_create_defaults_and_update_readback_across_full_views(tmp_path, monkeypatch):
    async def run(pool):
        project = await projects.create_project(name="Example", work_dir=str(tmp_path))
        async with _api(tmp_path, pool) as call:
            view = await _create(call, project["id"])
            sid = view["schedule_id"]
            assert view["kind"] == "command" and view["requirement"] == ""
            assert view["run_timeout_seconds"] is None and view["env"] == {}
            assert view["alert_policy"] == {
                "failures_after": {"value": 3, "default": True},
                "stale_after_seconds": {"value": None, "default": True},
                "overlap_after": {"value": 10, "default": True}}
            assert view["alerts"]["deadline_since"] is None
            assert view["alerts"]["overlap_since"] is None
            env_file = tmp_path / "command.env"
            env_file.write_text("TOKEN=example\n")
            fields = {"command": "printf changed", "env": {"REPORT": "daily"}, "env_file": str(env_file),
                "success_deadline": "09:30", "alert_failures_after": 4,
                "alert_stale_after_seconds": 600, "alert_overlap_after": 2, "run_timeout_seconds": 120,
                "requirement": "", "template_key": None, "template_inputs": {}, "gate_command": None,
                "gate_timeout_seconds": None, "run_budget_usd": None, "daily_budget_usd": None,
                "profile_overrides": {}, "task_limits": {}, "user_gates": "off"}
            status, result = await call("update", {"schedule_id": sid, "fields": fields}, client=False)
            assert status == 200, result
            env_file.unlink()
            status, result = await call("update", {"schedule_id": sid, "fields": {"name": "Renamed"}}, client=False)
            assert status == 200, result
            for action, body in (("get", {"schedule_id": sid}), ("list", {}),
                                 ("project_list", {"project_id": project["id"]})):
                status, result = await call(action, body)
                assert status == 200
                saved = result["schedule"] if action == "get" else result["schedules"][0]
                for key in ("command", "env", "env_file", "success_deadline", "run_timeout_seconds"):
                    assert saved[key] == fields[key]
                assert saved["name"] == "Renamed"
                assert saved["alert_policy"] == {
                    "failures_after": {"value": 4, "default": False},
                    "stale_after_seconds": {"value": 600, "default": False},
                    "overlap_after": {"value": 2, "default": False}}
            status, result = await call("update", {"schedule_id": sid, "fields": {"env": None}}, client=False)
            assert status == 200 and result["schedule"]["env"] == {}, result
            for fields in ({"kind": "command"}, {"kind": "agent"}, {"requirement": "Run a task"},
                           {"gate_command": "true"}, {"run_timeout_seconds": "default"},
                           {"template_key": None, "template_inputs": {"REPORT": "daily"}},
                           {"profile_overrides": {"planner": "profile-not-present"}}):
                status, result = await call("update", {"schedule_id": sid, "fields": fields}, client=False)
                assert status == 400 and result["error"]["code"] == "invalid", result
                if "profile_overrides" in fields:
                    assert "profile_overrides does not apply" in result["error"]["message"]
                if "template_inputs" in fields:
                    assert "template_inputs does not apply" in result["error"]["message"]
            assert (await db_schedules.get_schedule(sid))["command"] == "printf changed"
    run_db(run, monkeypatch, timeout=30)


def test_templates_only_owner_rejects_command_schedule(tmp_path, monkeypatch):
    async def run(pool):
        project = await projects.create_project(name="Example", work_dir=str(tmp_path))
        await pool.execute("UPDATE managers SET task_policy = 'templates_only' WHERE id = 'manager'")
        async with _api(tmp_path, pool) as call:
            status, result = await call("create", {"project_id": project["id"], "fields": {
                "name": "Command", "kind": "command", "command": "true", "cron": "0 * * * *"}}, client=False)
            assert status == 400 and "template" in result["error"]["message"], result
            assert await pool.fetchval("SELECT count(*) FROM schedules") == 0
    run_db(run, monkeypatch, timeout=30)


def test_runs_status_filter_stats_and_execution_details(tmp_path, monkeypatch):
    async def run(pool):
        project = await projects.create_project(name="Example", work_dir=str(tmp_path))
        now = datetime.now(timezone.utc)
        async with _api(tmp_path, pool) as call:
            sid = (await _create(call, project["id"]))["schedule_id"]
            _, empty = await call("runs", {"schedule_id": sid})
            zero = {"runs": 0, "succeeded": 0, "failed": 0, "success_rate": None,
                    "avg_seconds": None, "max_seconds": None}
            assert empty["stats"] == {"day": zero, "week": zero}
            ids = {}
            for i, status in enumerate((*db_schedules.SUCCESS, *db_schedules.FAILURES, "stopped", "cancelled"), 1):
                ids[status] = await _run(pool, sid, status=status, finished_at=now - timedelta(hours=2),
                                         seconds=i * 10, started=status != "cancelled")
            for status, age, duration in (("completed", 3, 90), ("failed", 4, 100), ("succeeded", 8, 110)):
                await _run(pool, sid, status=status, finished_at=now - timedelta(days=age), seconds=duration)
            for status in (*db_schedules.SKIPPED, *db_schedules.OPEN_STATUSES):
                await _run(pool, sid, status=status, finished_at=now - timedelta(hours=1), seconds=500)
            await db_schedules.update_run(pool, ids["failed"], exit_code=7, exit_signal=9,
                log_path="logs/schedules/example/run.log", log_bytes=123, log_truncated=True, log_tail="last line")
            status, page = await call("runs", {"schedule_id": sid, "status": "failed"})
            assert status == 200 and len(page["runs"]) == 2
            assert {row["status"] for row in page["runs"]} == {"failed"}
            assert page["stats"]["day"] == {"runs": 8, "succeeded": 2, "failed": 4,
                "success_rate": 0.25, "avg_seconds": 45, "max_seconds": 80}
            assert page["stats"]["week"] == {"runs": 10, "succeeded": 3, "failed": 5,
                "success_rate": 0.3, "avg_seconds": 55, "max_seconds": 100}
            details = next(row for row in page["runs"] if row["run_id"] == ids["failed"])
            assert details["started_at"] and details["duration_seconds"] == 30
            assert (details["exit_code"], details["exit_signal"], details["log_tail"]) == (7, 9, "last line")
            assert details["log"] == {"file": "logs/schedules/example/run.log", "bytes": 123, "truncated": True}
            _, all_runs = await call("runs", {"schedule_id": sid})
            assert all_runs["stats"] == page["stats"]
            running = next(row for row in all_runs["runs"] if row["status"] == "running")
            assert running["duration_seconds"] is None and running["log"] is None
            cancelled = next(row for row in all_runs["runs"] if row["status"] == "cancelled")
            assert cancelled["started_at"] is None and cancelled["duration_seconds"] == 80
            status, answer = await call("runs", {"schedule_id": sid, "status": "unknown"})
            assert status == 400 and answer["error"]["code"] == "invalid"
    run_db(run, monkeypatch, timeout=30)


def test_run_log_binary_paging_round_trips_and_empty_final_page(tmp_path, monkeypatch):
    async def run(pool):
        project = await projects.create_project(name="Example", work_dir=str(tmp_path))
        async with _api(tmp_path, pool) as call:
            sid = (await _create(call, project["id"]))["schedule_id"]
            path = tmp_path / "logs" / "schedules" / "example" / "run.log"
            path.parent.mkdir(parents=True)
            data = os.urandom(3 * 1024 * 1024)
            path.write_bytes(data)
            rid = await _run(pool, sid, log_path=str(path.relative_to(tmp_path)))
            await db_schedules.update_run(pool, rid, log_bytes=len(data), log_truncated=True)
            chunks, offset = [], 0
            while True:
                status, result = await call("run_log", {"run_id": rid, "offset": offset, "limit_bytes": 1024 * 1024})
                assert status == 200, result
                assert result["run_id"] == rid and result["schedule_id"] == sid
                assert result["size"] == len(data) and result["offset"] == offset
                assert result["complete"] is True and result["truncated"] is True
                chunk = base64.b64decode(result["data_base64"], validate=True)
                assert len(chunk) == 1024 * 1024
                chunks.append(chunk)
                if result["next_offset"] is None:
                    break
                assert result["next_offset"] == offset + len(chunk)
                offset = result["next_offset"]
            assert len(chunks) == 3 and b"".join(chunks) == data
            status, result = await call("run_log", {"run_id": rid, "offset": len(data)})
            assert status == 200 and result["data_base64"] == "" and result["next_offset"] is None
            status, _ = await call("run_log", {"run_id": rid, "offset": len(data) + 1})
            assert status == 400
    run_db(run, monkeypatch, timeout=30)


@pytest.mark.parametrize("ending", [b"", b"\n"])
def test_run_log_tail_returns_last_lines_and_their_byte_offset(tmp_path, monkeypatch, ending):
    async def run(pool):
        project = await projects.create_project(name="Example", work_dir=str(tmp_path))
        async with _api(tmp_path, pool) as call:
            sid = (await _create(call, project["id"]))["schedule_id"]
            path = tmp_path / "logs" / "schedules" / "tail.log"
            path.parent.mkdir(parents=True)
            prefix = b"first\nsecond\n"
            tail = b"third\nfourth\nfifth" + ending
            path.write_bytes(prefix + tail)
            rid = await _run(pool, sid, status="running", log_path=str(path.relative_to(tmp_path)))
            _, schedule = await call("get", {"schedule_id": sid})
            assert schedule["schedule"]["last_run"]["log"]["bytes"] is None
            status, result = await call("run_log", {"run_id": rid, "tail_lines": 3})
            assert status == 200 and base64.b64decode(result["data_base64"]) == tail
            assert result["offset"] == len(prefix) and result["next_offset"] is None
            assert result["complete"] is False
            _, bounded = await call("run_log", {"run_id": rid, "tail_lines": 3, "limit_bytes": 4})
            assert base64.b64decode(bounded["data_base64"]) == (prefix + tail)[-4:]
            assert bounded["offset"] == len(prefix + tail) - 4
    run_db(run, monkeypatch, timeout=30)


def test_run_log_rejects_escaping_paths_and_reports_missing_files(tmp_path, monkeypatch):
    async def run(pool):
        project = await projects.create_project(name="Example", work_dir=str(tmp_path))
        async with _api(tmp_path, pool) as call:
            sid = (await _create(call, project["id"]))["schedule_id"]
            rid = await _run(pool, sid, log_path="../../ops/server.toml")
            assert (await call("run_log", {"run_id": rid}))[0] == 400
            outside = tmp_path / "outside.log"
            outside.write_bytes(b"private")
            link = tmp_path / "logs" / "schedules" / "link.log"
            link.parent.mkdir(parents=True)
            link.symlink_to(outside)
            await db_schedules.update_run(pool, rid, log_path=str(link.relative_to(tmp_path)))
            assert (await call("run_log", {"run_id": rid}))[0] == 400
            await db_schedules.update_run(pool, rid, log_path="logs/schedules/missing.log")
            status, result = await call("run_log", {"run_id": rid})
            assert status == 404 and "retention policy" in result["error"]["message"]
            await db_schedules.update_run(pool, rid, status="running", finished_at=None)
            status, result = await call("run_log", {"run_id": rid})
            assert status == 404 and "not been written yet" in result["error"]["message"]
            status, result = await call("create", {"project_id": project["id"], "fields": {
                "name": "Agent", "cron": "0 * * * *", "requirement": "Read the project"}}, client=False)
            assert status == 200, result
            agent = result["schedule"]
            assert agent["kind"] == "agent" and agent["run_timeout_seconds"] == "default"
            assert agent["alert_policy"]["overlap_after"] == {"value": 0, "default": True}
            status, result = await call("update", {"schedule_id": agent["schedule_id"],
                "fields": {"command": "true"}}, client=False)
            assert status == 400 and "command applies to command schedules only" in result["error"]["message"]
            agent_run = await _run(pool, agent["schedule_id"], status="completed")
            status, result = await call("run_log", {"run_id": agent_run})
            assert status == 404 and "this run has no log" in result["error"]["message"]
            assert (await call("run_log", {"run_id": "srun-missing"}))[0] == 404
    run_db(run, monkeypatch, timeout=30)


def test_run_log_rejects_invalid_paging_parameters(tmp_path, monkeypatch):
    async def run(pool):
        project = await projects.create_project(name="Example", work_dir=str(tmp_path))
        async with _api(tmp_path, pool) as call:
            sid = (await _create(call, project["id"]))["schedule_id"]
            path = tmp_path / "logs" / "schedules" / "run.log"
            path.parent.mkdir(parents=True)
            path.write_bytes(b"line\n")
            rid = await _run(pool, sid, log_path=str(path.relative_to(tmp_path)))
            for fields in ({"offset": 0, "tail_lines": 1}, {"offset": -1}, {"offset": True},
                           {"offset": "0"}, {"limit_bytes": 0}, {"limit_bytes": 4 * 1024 * 1024 + 1},
                           {"limit_bytes": True}, {"tail_lines": 0}, {"tail_lines": 5001}, {"tail_lines": True}):
                status, result = await call("run_log", {"run_id": rid, **fields})
                assert status == 400 and result["error"]["code"] == "invalid", (fields, result)
    run_db(run, monkeypatch, timeout=30)
