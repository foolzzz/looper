"""Command schedule storage, one-time migration and fresh/upgrade parity."""

import asyncio
import json
from datetime import datetime, timezone

import asyncpg
import pytest

from looper.db import connection, schedules
from tests.bridge_schema_support import structure, witness
from tests.db_support import DSN, fresh_connection

pytestmark = pytest.mark.skipif(not DSN, reason="requires disposable LOOPER_TEST_DATABASE_URL")

_SCHEDULE_COLUMNS = """    kind TEXT NOT NULL DEFAULT 'agent' CHECK (kind IN ('agent', 'command')),
    command TEXT,
    env JSONB NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(env) = 'object'),
    env_file TEXT,
    success_deadline TEXT CHECK (success_deadline ~ '^([01][0-9]|2[0-3]):[0-5][0-9]$'),
    alert_failures_after INTEGER CHECK (alert_failures_after BETWEEN 1 AND 1000),
    alert_stale_after_seconds INTEGER CHECK (alert_stale_after_seconds BETWEEN 300 AND 2592000),
    alert_overlap_after INTEGER CHECK (alert_overlap_after BETWEEN 0 AND 10000),
    consecutive_overlaps INTEGER NOT NULL DEFAULT 0 CHECK (consecutive_overlaps >= 0),
    deadline_alert_at TIMESTAMPTZ,
    overlap_alert_at TIMESTAMPTZ,
"""
_RUN_COLUMNS = """    started_at TIMESTAMPTZ,
    exit_code INTEGER,
    exit_signal INTEGER,
    log_path TEXT,
    log_bytes BIGINT,
    log_truncated BOOLEAN NOT NULL DEFAULT false,
    log_tail TEXT,
    runner_pid INTEGER,
    runner_started TEXT
"""
_KIND_CHECK = """    CONSTRAINT schedules_kind_fields_check CHECK (
        (kind = 'command') = (command IS NOT NULL)
        AND (kind = 'command' OR (env = '{}'::jsonb AND env_file IS NULL))
        AND (kind = 'agent' OR (gate_command IS NULL AND template_key IS NULL)))
"""


async def existing_baseline(conn):
    prefix, schedules_start, sql = connection.SCHEMA_PATH.read_text().partition("CREATE TABLE IF NOT EXISTS schedules (")
    sql, next_table, suffix = sql.partition("CREATE TABLE IF NOT EXISTS llm_records (")
    pieces = [*_SCHEDULE_COLUMNS.splitlines(keepends=True), *_RUN_COLUMNS.splitlines(keepends=True),
              _KIND_CHECK, "                      'succeeded', 'interrupted')),\n",
              "                                              'deadline_alert', 'deadline_recovered', 'overlap_alert', 'overlap_recovered')),\n",
              "CREATE INDEX IF NOT EXISTS idx_schedule_runs_finished ON schedule_runs (finished_at) WHERE finished_at IS NOT NULL;\n"]
    for piece in pieces:
        assert sql.count(piece) == 1, piece
        sql = sql.replace(piece, "")
    for line, original in [
        ("    CONSTRAINT schedules_notify_target_check CHECK ((notify_channel IS NULL) = (notify_chat IS NULL)),\n",
         "    CONSTRAINT schedules_notify_target_check CHECK ((notify_channel IS NULL) = (notify_chat IS NULL))\n"),
        ("    carried_on BOOLEAN NOT NULL DEFAULT false,\n", "    carried_on BOOLEAN NOT NULL DEFAULT false\n"),
        ("                      'gate_failed', 'skipped_overlap', 'skipped_missed', 'skipped_gate', 'skipped_budget',\n",
         "                      'gate_failed', 'skipped_overlap', 'skipped_missed', 'skipped_gate', 'skipped_budget')),\n"),
        ("                                              'stale_recovered',\n", "                                              'stale_recovered')),\n"),
    ]:
        assert sql.count(line) == 1, line
        sql = sql.replace(line, original)
    await conn.execute(prefix + schedules_start + sql + next_table + suffix)
    await conn.execute("INSERT INTO schema_migrations (name) VALUES ('post_bridge_baseline'), ('schedule_user_gates')")
    await conn.execute("INSERT INTO registry_schema_version (id, version) VALUES (TRUE, 4)")
    await conn.execute(connection.SEEDS_PATH.read_text())
    await conn.execute("INSERT INTO projects (id,name) VALUES ('p','Example')")
    await conn.execute("INSERT INTO schedules (id,project_id,name,cron,timezone,requirement) "
                       "VALUES ('s','p','Reports','0 * * * *','UTC','Report')")
    await conn.execute("INSERT INTO schedule_runs (id,schedule_id,scheduled_for,trigger,status) "
                       "VALUES ('r','s',now(),'scheduled','completed')")
    await conn.execute("INSERT INTO schedule_notifications (id,schedule_id,run_id,kind,text,status) "
                       "VALUES ('n','s','r','run','Report ready','dashboard')")


def test_existing_baseline_upgrades_once_preserves_data_and_matches_fresh():
    async def test():
        async with fresh_connection() as old, fresh_connection() as fresh:
            await existing_baseline(old)
            before, shape = await witness(old), await structure(old)
            assert await connection.schema_change_pending(old)
            assert await witness(old) == before
            assert await structure(old) == shape
            await connection.ensure_schema(old)
            expected = {line.strip().split()[0]: None for line in _SCHEDULE_COLUMNS.splitlines()}
            expected.update(kind="agent", env="{}", consecutive_overlaps=0)
            assert dict(await old.fetchrow("SELECT * FROM schedules")) == {**before["schedules"][0], **expected}
            run_defaults = {line.strip().split()[0]: None for line in _RUN_COLUMNS.splitlines()}
            run_defaults["log_truncated"] = False
            assert dict(await old.fetchrow("SELECT * FROM schedule_runs")) == {**before["schedule_runs"][0], **run_defaults}
            assert [dict(row) for row in await old.fetch("SELECT * FROM schedule_notifications")] == before["schedule_notifications"]
            await connection.ensure_schema(fresh)
            assert await structure(old) == await structure(fresh)
            assert [row["name"] for row in await fresh.fetch("SELECT name FROM schema_migrations")] == ["post_bridge_baseline"]
            after, shape = await witness(old), await structure(old)
            assert not await connection.schema_change_pending(old)
            await connection.ensure_schema(old)
            assert await witness(old) == after
            assert await structure(old) == shape
            assert await old.fetchval("SELECT count(*) FROM schema_migrations WHERE name='command_schedules'") == 1
    asyncio.run(asyncio.wait_for(test(), 30))


@pytest.mark.parametrize("upgraded", [False, True], ids=["fresh", "upgraded"])
@pytest.mark.parametrize("table,assignment,valid", [
    ("schedules", "kind='command'", False),
    ("schedules", "command='true'", False),
    ("schedules", "kind='command',command='true',gate_command='true'", False),
    ("schedules", "kind='command',command='true',template_key='example'", False),
    ("schedules", "env='{\"KEY\":\"value\"}'", False),
    ("schedules", "env_file='example.env'", False),
    ("schedules", "kind='command',command='true',env='[]'", False),
    ("schedules", "kind='command',command='true',env='{\"KEY\":\"value\"}',env_file='example.env'", True),
    ("schedules", "success_deadline='8:00'", False),
    ("schedules", "success_deadline='24:00'", False),
    ("schedules", "success_deadline='08:00'", True),
    ("schedules", "alert_failures_after=0", False),
    ("schedules", "alert_failures_after=1001", False),
    ("schedules", "alert_stale_after_seconds=299", False),
    ("schedules", "alert_stale_after_seconds=2592001", False),
    ("schedules", "alert_overlap_after=-1", False),
    ("schedules", "alert_overlap_after=10001", False),
    ("schedules", "consecutive_overlaps=-1", False),
    ("schedules", "alert_failures_after=1,alert_stale_after_seconds=300,alert_overlap_after=0", True),
    ("schedules", "alert_failures_after=1000,alert_stale_after_seconds=2592000,alert_overlap_after=10000", True),
    ("schedule_runs", "status='succeeded'", True),
    ("schedule_runs", "status='interrupted'", True),
    ("schedule_runs", "status='bogus'", False),
    ("schedule_notifications", "kind='deadline_alert'", True),
    ("schedule_notifications", "kind='deadline_recovered'", True),
    ("schedule_notifications", "kind='overlap_alert'", True),
    ("schedule_notifications", "kind='overlap_recovered'", True),
    ("schedule_notifications", "kind='bogus'", False),
])
def test_constraints_on_fresh_and_upgraded_databases(upgraded, table, assignment, valid):
    async def test():
        async with fresh_connection() as conn:
            if upgraded:
                await existing_baseline(conn)
            await connection.ensure_schema(conn)
            if not upgraded:
                await conn.execute("INSERT INTO projects (id,name) VALUES ('p','Example')")
                await conn.execute("INSERT INTO schedules (id,project_id,name,cron,timezone,requirement) "
                                   "VALUES ('s','p','Reports','0 * * * *','UTC','Report')")
                await schedules.insert_run(conn, schedule_id="s", scheduled_for=datetime.now(timezone.utc),
                                           trigger="manual", status="completed")
                await schedules.insert_notification(conn, schedule_id="s", run_id=None, kind="run", channel=None,
                                                    chat=None, text="Report ready", status="dashboard", error=None)
            if valid:
                assert await conn.execute(f"UPDATE {table} SET {assignment}") == "UPDATE 1"
            else:
                with pytest.raises(asyncpg.CheckViolationError):
                    await conn.execute(f"UPDATE {table} SET {assignment}")
    asyncio.run(asyncio.wait_for(test(), 30))


def test_failed_migration_rolls_back_all_ddl_and_marker(monkeypatch):
    async def test():
        async with fresh_connection() as conn:
            await existing_baseline(conn)
            before, shape = await witness(conn), await structure(conn)
            with monkeypatch.context() as patch:
                patch.setattr(connection, "_COMMAND_SCHEDULES_DDL", (*connection._COMMAND_SCHEDULES_DDL, "SELECT 1/0"))
                with pytest.raises(asyncpg.DivisionByZeroError):
                    await connection.ensure_schema(conn)
            assert await witness(conn) == before
            assert await structure(conn) == shape
            assert await connection.schema_change_pending(conn)
            await connection.ensure_schema(conn)
            assert not await connection.schema_change_pending(conn)
    asyncio.run(asyncio.wait_for(test(), 30))


def test_recorded_migration_does_not_repair_a_removed_column():
    async def test():
        async with fresh_connection() as conn:
            await existing_baseline(conn)
            await connection.ensure_schema(conn)
            await conn.execute("ALTER TABLE schedules DROP COLUMN kind CASCADE")
            shape = await structure(conn)
            assert not await connection.schema_change_pending(conn)
            await connection.ensure_schema(conn)
            assert await structure(conn) == shape
    asyncio.run(asyncio.wait_for(test(), 30))


@pytest.mark.parametrize("status,failures,success", [("succeeded", 0, True), ("interrupted", 4, False)])
def test_command_outcomes_update_schedule_counters(status, failures, success):
    async def test():
        async with fresh_connection() as conn:
            await existing_baseline(conn)
            await connection.ensure_schema(conn)
            previous = datetime(2026, 10, 29, tzinfo=timezone.utc)
            await conn.execute("UPDATE schedules SET kind='command',command='true',consecutive_failures=3, "
                               "last_ok_at=$1,last_success_at=$1", previous)
            at = datetime(2026, 10, 30, tzinfo=timezone.utc)
            await schedules.count_outcome(conn, "s", status, at)
            row = await conn.fetchrow("SELECT consecutive_failures,last_ok_at,last_success_at FROM schedules")
            assert dict(row) == {"consecutive_failures": failures, "last_ok_at": at if success else previous,
                                 "last_success_at": at if success else previous}
    asyncio.run(asyncio.wait_for(test(), 30))


def test_command_fields_json_run_metadata_and_alerts_round_trip():
    async def test():
        async with fresh_connection() as conn:
            await connection.ensure_schema(conn)
            await conn.execute("INSERT INTO projects (id,name) VALUES ('p','Example')")
            fields = dict(name="Command", cron="0 * * * *", timezone="UTC", requirement="", kind="command",
                          command="true", env={"LANG": "en_US.UTF-8"}, env_file="example.env", success_deadline="08:00",
                          alert_failures_after=2, alert_stale_after_seconds=600, alert_overlap_after=10,
                          template_inputs={"example": "value"})
            sid = await schedules.create_schedule(conn, project_id="p", fields=fields, profiles={})
            row = await schedules.get_schedule(sid, pool=conn)
            assert {key: row[key] for key in fields} == fields
            await schedules.update_schedule(conn, sid, {"env": {"LANG": "C"}, "command": "exit 0",
                                                        "template_inputs": {"example": "updated"}})
            row = await schedules.get_schedule(sid, pool=conn)
            assert row["env"] == {"LANG": "C"} and row["template_inputs"] == {"example": "updated"}
            assert json.loads(await conn.fetchval("SELECT env FROM schedules WHERE id=$1", sid)) == row["env"]
            for kind in ("agent", "command"):
                with pytest.raises(ValueError, match="a schedule's kind cannot change"):
                    await schedules.update_schedule(conn, sid, {"kind": kind})
            at = datetime(2026, 10, 30, tzinfo=timezone.utc)
            log_path = f"logs/schedules/{sid}/srun-example.log"
            rid = await schedules.insert_run(conn, schedule_id=sid, scheduled_for=at, trigger="manual", status="running",
                                             run_id="srun-example", started_at=at, log_path=log_path)
            assert rid == "srun-example"
            run = await schedules.get_run(rid, pool=conn)
            assert (run["started_at"], run["log_path"], run["log_truncated"]) == (at, log_path, False)
            metadata = dict(started_at=at, exit_code=0, exit_signal=None, log_path=log_path, log_bytes=2**32,
                            log_truncated=True, log_tail="ready", runner_pid=123, runner_started="Fri Oct 30 00:00:00 2026")
            await schedules.update_run(conn, rid, **metadata)
            run = await schedules.get_run(rid, pool=conn)
            assert {key: run[key] for key in metadata} == metadata
            for column in ("deadline_alert_at", "overlap_alert_at"):
                assert await schedules.set_alert(conn, sid, column, at)
                assert not await schedules.set_alert(conn, sid, column, at)
                assert await conn.fetchval(f"SELECT {column} FROM schedules WHERE id=$1", sid) == at
                assert await schedules.set_alert(conn, sid, column, None)
                assert not await schedules.set_alert(conn, sid, column, None)
    asyncio.run(asyncio.wait_for(test(), 30))
