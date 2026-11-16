"""Audience message settings survive API, Manager and config transfer writes."""

import asyncio
import json

import asyncpg
import pytest

from looper.agents import config_transfer
from looper.db import connection, projects, schedules
from tests.db_support import DSN, run_db
from tests.test_api_v1_schedule_writes_db import FIELDS, _api
from tests.test_ops_tasks_db import _Chat, _project
from tests.test_config_transfer_db import _installation

pytestmark = pytest.mark.skipif(not DSN, reason="requires disposable LOOPER_TEST_DATABASE_URL")


def test_notify_format_api_roundtrip_and_command_guard(tmp_path, monkeypatch):
    async def test(pool):
        project = await projects.create_project(name="Reports", work_dir="/srv/reports")
        async with _api(tmp_path, pool) as call:
            status, result = await call("create", {"project_id": project["id"], "fields": FIELDS})
            assert status == 200, result
            sid = result["schedule"]["schedule_id"]
            assert result["schedule"]["notify_format"] == "report"
            for value in ("deliverable", "report", "deliverable"):
                status, result = await call("update", {"schedule_id": sid, "fields": {"notify_format": value}})
                assert status == 200, result
                assert result["schedule"]["notify_format"] == value
                _, result = await call("get", {"schedule_id": sid})
                assert result["schedule"]["notify_format"] == value
            command = {"name": "Command", "kind": "command", "command": "true", "cron": "* * * * *",
                       "notify_format": "deliverable"}
            status, result = await call("create", {"project_id": project["id"], "fields": command})
            assert status == 400 and "report" in str(result)
            assert (await schedules.get_schedule(sid))["notify_format"] == "deliverable"
            status, result = await call("create", {"project_id": project["id"],
                "fields": {**command, "notify_format": "report"}})
            assert status == 200, result
            command_id = result["schedule"]["schedule_id"]
            status, result = await call("update", {"schedule_id": command_id,
                "fields": {"notify_format": "deliverable"}})
            assert status == 400 and "report" in str(result)
            assert (await schedules.get_schedule(command_id))["notify_format"] == "report"
    run_db(test, monkeypatch, timeout=30)


def test_notify_format_manager_and_export(tmp_path, monkeypatch):
    async def test(pool):
        await _project(pool, work_dir=str(tmp_path))
        chat = _Chat(tmp_path)
        (result,) = await chat.say("创建每日内容", ("looper_schedules", {"action": "create", "project": "calc",
            "fields": {**FIELDS, "notify_format": "deliverable"}}))
        assert result["status"] == "ok", result
        sid = result["result"]["schedule"]["schedule_id"]
        row = await schedules.get_schedule(sid)
        assert config_transfer.schedule_entry(row, {})["notify_format"] == "deliverable"
        (result,) = await chat.say("改为运行报告", ("looper_schedules", {"action": "update", "schedule": sid,
            "fields": {"notify_format": "report"}}))
        assert result["status"] == "ok", result
        assert (await schedules.get_schedule(sid))["notify_format"] == "report"
    run_db(test, monkeypatch, timeout=30)


def test_existing_schema_adds_report_default_and_command_constraint():
    async def test(pool):
        project = await projects.create_project(name="Reports", pool=pool)
        sid = await schedules.create_schedule(pool, project_id=project["id"], fields={**FIELDS, "notify": "always"}, profiles={})
        await pool.execute("ALTER TABLE schedules DROP CONSTRAINT schedules_command_notify_format_check")
        await pool.execute("ALTER TABLE schedules DROP COLUMN notify_format")
        async with pool.acquire() as conn:
            assert await connection.schema_change_pending(conn)
        await connection.ensure_schema(pool)
        row = await schedules.get_schedule(sid, pool=pool)
        assert row["notify_format"] == "report" and row["notify"] == "always"
        async with pool.acquire() as conn:
            assert not await connection.schema_change_pending(conn)
        with pytest.raises(asyncpg.CheckViolationError):
            await pool.execute("UPDATE schedules SET kind='command', command='true', notify_format='deliverable' WHERE id=$1", sid)
        await connection.ensure_schema(pool)
        assert await pool.fetchval("SELECT count(*) FROM schema_migrations WHERE name='schedule_notify_format'") == 1
    run_db(test, timeout=30)


def test_notify_format_config_roundtrip(tmp_path):
    async def test():
        async with _installation() as (source, source_url), _installation() as (target, target_url):
            project = await projects.create_project(name="Reports", pool=source)
            await schedules.create_schedule(source, project_id=project["id"], fields={**FIELDS,
                "notify_format": "deliverable"}, profiles={})
            path = tmp_path / "config.json"
            await config_transfer.export_to_file(source_url, path, include_projects=True, include_secrets=False)
            plan = await config_transfer.import_file(target_url, path, apply=True)
            assert not [change for change in plan.view()["changes"] if change.get("action") == "skipped"], plan.view()
            assert await target.fetchval("SELECT notify_format FROM schedules") == "deliverable"
            await config_transfer.export_to_file(target_url, path, include_projects=True, include_secrets=False)
            saved = json.loads(path.read_text())
            assert saved["projects"][0]["schedules"][0]["notify_format"] == "deliverable"
            del saved["projects"][0]["schedules"][0]["notify_format"]
            path.write_text(json.dumps(saved))
            await config_transfer.import_file(target_url, path, apply=True)
            assert await target.fetchval("SELECT notify_format FROM schedules") == "report"
    asyncio.run(asyncio.wait_for(test(), timeout=45))
