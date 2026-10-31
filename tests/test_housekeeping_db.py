"""Retention on real PostgreSQL, including the scheduler's hourly background pass."""

from datetime import timedelta
import json
import os
import subprocess
from typing import Any

import pytest

from looper.agents import housekeeping, proc_tree, scheduler_settings
from looper.agents.command_runs import CommandRuns
from looper.db import schedules
from tests.db_support import DSN
from tests.schedule_support import ScheduleEnv, run_scheduled

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")


async def _runs(env: ScheduleEnv, sid: str, count: int, age: int, *, prefix: str = "old") -> None:
    await env.pool.execute(
        "INSERT INTO schedule_runs (id, schedule_id, scheduled_for, trigger, status, created_at, finished_at) "
        "SELECT $1 || i, $2, $3::timestamptz, 'scheduled', 'completed', "
        "$3::timestamptz + i * interval '1 second', $3::timestamptz + i * interval '1 second' "
        "FROM generate_series(1, $4::int) i", prefix, sid, env.clock.now - timedelta(days=age), count)


def test_retention_keeps_latest_runs_and_open_states_and_nulls_references(monkeypatch, tmp_path):
    async def test(env: ScheduleEnv) -> None:
        sid = (await env.schedule())["schedule_id"]
        await _runs(env, sid, 150, 40)
        await _runs(env, sid, 10, 0, prefix="today")
        await env.pool.execute("UPDATE schedule_runs SET carried_from = 'old1' WHERE id = 'today1'")
        await env.pool.execute(
            "INSERT INTO schedule_runs (id, schedule_id, scheduled_for, trigger, status, created_at) "
            "SELECT status, $1, $2, 'scheduled', status, $2 FROM unnest($3::text[]) status",
            sid, env.clock.now - timedelta(days=41), list(schedules.OPEN_STATUSES))
        await env.pool.execute(
            "INSERT INTO schedule_notifications (id, schedule_id, run_id, kind, text, status, created_at) "
            "VALUES ('old-note', $1, 'old1', 'run', '', 'dashboard', $2), "
            "('fresh-note', $1, 'old1', 'run', '', 'dashboard', $3)",
            sid, env.clock.now - timedelta(days=40), env.clock.now)
        report = await housekeeping.run_once(env.runtime_root, env.clock.now, current_runner=None, commands=None)
        assert (report.runs, report.notifications) == (60, 1)
        assert await env.pool.fetchval("SELECT count(*) FROM schedule_runs WHERE status = 'completed'") == 100
        assert await schedules.run_states(env.pool, [*schedules.OPEN_STATUSES, "old1"]) == {
            state: state for state in schedules.OPEN_STATUSES}
        assert await env.pool.fetchval("SELECT carried_from FROM schedule_runs WHERE id = 'today1'") is None
        assert await env.pool.fetchval("SELECT run_id FROM schedule_notifications WHERE id = 'fresh-note'") is None
        assert (await housekeeping.run_once(env.runtime_root, env.clock.now,
                                            current_runner=None, commands=None)).runs == 0

    run_scheduled(test, monkeypatch, tmp_path)


def test_configured_retention_and_open_log_protection(monkeypatch, tmp_path):
    async def test(env: ScheduleEnv) -> None:
        sid = (await env.schedule())["schedule_id"]
        await _runs(env, sid, 102, 8)
        await scheduler_settings.update({"run_retention_days": 7})
        log = env.runtime_root / "logs" / "schedules" / sid / "open.log"
        log.parent.mkdir(parents=True)
        log.write_bytes(b"active")
        stamp = (env.clock.now - timedelta(days=40)).timestamp()
        os.utime(log, (stamp, stamp))
        await env.pool.execute("UPDATE schedule_runs SET status = 'running', finished_at = NULL, log_path = $1 "
                               "WHERE id = 'old1'", str(log.relative_to(env.runtime_root)))
        monkeypatch.setattr(housekeeping, "LOG_TOTAL_MAX_BYTES", 1)
        report = await housekeeping.run_once(env.runtime_root, env.clock.now, current_runner=None, commands=None)
        assert report.runs == 1 and report.logs == 0 and log.read_bytes() == b"active"
        assert await schedules.open_log_paths(env.pool) == {str(log.relative_to(env.runtime_root))}

    run_scheduled(test, monkeypatch, tmp_path)


def test_pruning_is_batched(monkeypatch, tmp_path):
    async def test(env: ScheduleEnv) -> None:
        sid = (await env.schedule())["schedule_id"]
        await _runs(env, sid, 5101, 40)
        async with env.pool.acquire() as conn, conn.transaction():
            before = env.clock.now - timedelta(days=30)
            assert await schedules.prune_runs(conn, before) == 5000
            assert await schedules.prune_runs(conn, before) == 1
            assert await schedules.prune_runs(conn, before) == 0
            await conn.execute(
                "INSERT INTO schedule_notifications (id, schedule_id, kind, text, status, created_at) "
                "SELECT 'note' || i, $1, 'run', '', 'dashboard', $2 FROM generate_series(1, 5001) i",
                sid, env.clock.now - timedelta(days=40))
            assert await schedules.prune_notifications(conn, before) == 5000
        report = await housekeeping.run_once(env.runtime_root, env.clock.now, current_runner=None, commands=None)
        assert report.notifications == 1

    run_scheduled(test, monkeypatch, tmp_path)


def test_run_directories_are_removed_only_after_the_runner_is_gone(monkeypatch, tmp_path):
    async def test(env: ScheduleEnv) -> None:
        sid = (await env.schedule())["schedule_id"]
        await _runs(env, sid, 2, 1)
        await env.pool.execute("UPDATE schedule_runs SET status = 'running', finished_at = NULL WHERE id = 'old2'")
        root = env.runtime_root / ".local" / "command-runs"
        for run_id in ("old1", "old2", "orphan", "live-orphan"):
            directory = root / run_id
            directory.mkdir(parents=True)
            (directory / "spec.json").write_text("{}")
            stamp = (env.clock.now - timedelta(days=1)).timestamp()
            os.utime(directory, (stamp, stamp))
        # The test process is a real, live identity; no extra process is needed.
        (root / "live-orphan" / "runner.json").write_text(json.dumps({
            "runner_pid": os.getpid(), "runner_started": proc_tree._start_time(os.getpid())}))
        report = await housekeeping.run_once(env.runtime_root, env.clock.now,
                                            current_runner=None, commands=CommandRuns(env.runtime_root))
        assert report.run_dirs == 2
        assert {path.name for path in root.iterdir()} == {"old2", "live-orphan"}

    run_scheduled(test, monkeypatch, tmp_path)


def test_new_logs_after_the_candidate_snapshot_are_not_pruned(monkeypatch, tmp_path):
    async def test(env: ScheduleEnv) -> None:
        sid = (await env.schedule())["schedule_id"]
        await _runs(env, sid, 2, 0)
        root = env.runtime_root / "logs" / "schedules" / sid
        root.mkdir(parents=True)
        (root / "old1.log").write_bytes(b"already above the cap")
        await env.pool.execute(
            "UPDATE schedule_runs SET status = 'running', finished_at = NULL, log_path = $1 WHERE id = 'old1'",
            str((root / "old1.log").relative_to(env.runtime_root)))
        read_open = schedules.open_log_paths

        async def start_during_cleanup(conn):
            paths = await read_open(conn)
            await env.pool.execute(
                "UPDATE schedule_runs SET status = 'running', finished_at = NULL, log_path = $1 WHERE id = 'old2'",
                str((root / "old2.log").relative_to(env.runtime_root)))
            (root / "old2.log").write_bytes(b"new active output")
            return paths

        monkeypatch.setattr(schedules, "open_log_paths", start_during_cleanup)
        monkeypatch.setattr(housekeeping, "LOG_TOTAL_MAX_BYTES", 1)
        report = await housekeeping.run_once(env.runtime_root, env.clock.now, current_runner=None, commands=None)
        assert report.logs == 0 and (root / "old2.log").read_bytes() == b"new active output"

    run_scheduled(test, monkeypatch, tmp_path)


def test_process_probe_failure_preserves_orphan_runner_directory(monkeypatch, tmp_path, caplog):
    async def test(env: ScheduleEnv) -> None:
        directory = env.runtime_root / ".local" / "command-runs" / "orphan"
        directory.mkdir(parents=True)
        (directory / "spec.json").write_text("{}")
        (directory / "runner.json").write_text(json.dumps({
            "runner_pid": os.getpid(), "runner_started": proc_tree._start_time(os.getpid())}))

        def fail_probe(*args, **kwargs):
            raise subprocess.TimeoutExpired("ps", 5)

        monkeypatch.setattr(subprocess, "run", fail_probe)
        await env.scheduler._housekeep(env.clock.now)
        assert directory.is_dir() and "housekeeping failed" in caplog.text

    run_scheduled(test, monkeypatch, tmp_path)


def test_scheduler_runs_housekeeping_hourly_and_reports_failures(monkeypatch, tmp_path, caplog):
    async def test(env: ScheduleEnv) -> None:
        log = env.runtime_root / "logs" / "serve.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(housekeeping, "ROTATE_AT_BYTES", 4)
        log.write_text("first")
        await env.scheduler.tick()
        await env.scheduler.idle()
        assert log.read_bytes() == b"" and log.with_suffix(".log.1").read_text() == "first"
        log.write_text("second")
        await env.scheduler.tick()
        await env.scheduler.idle()
        assert log.read_text() == "second"
        env.clock.now += housekeeping.INTERVAL
        await env.scheduler.tick()
        await env.scheduler.idle()
        assert log.read_bytes() == b""

        async def fail(*args: Any, **kwargs: Any) -> None:
            raise OSError("cleanup failed")

        monkeypatch.setattr(housekeeping, "run_once", fail)
        env.clock.now += housekeeping.INTERVAL
        await env.scheduler.tick()
        await env.scheduler.idle()
        assert "cleanup failed" in caplog.text
        assert not env.scheduler._background

    run_scheduled(test, monkeypatch, tmp_path)
