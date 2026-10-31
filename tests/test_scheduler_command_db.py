"""Scheduled commands use detached runners and settle once across server restarts.

Runs only with a disposable LOOPER_TEST_DATABASE_URL. Commands use temporary directories,
real process time and a separately controlled scheduler clock.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest

from looper.agents import proc_tree
from looper.agents.scheduler import Scheduler
from looper.db import schedules as db_schedules
from tests.db_support import DSN
from tests.schedule_support import UTC, ScheduleEnv, run_scheduled

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")


@pytest.fixture(autouse=True)
def _clean_command_processes(tmp_path: Path):
    """Clean detached processes even when an assertion fails before settlement."""
    yield
    identities: set[tuple[int, str]] = set()
    for path in tmp_path.rglob("runner.json"):
        try:
            record = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        for prefix in ("child", "runner"):
            if record.get(f"{prefix}_pid") and record.get(f"{prefix}_started"):
                identities.add((record[f"{prefix}_pid"], record[f"{prefix}_started"]))
    for pid, started in identities:
        if proc_tree._start_time(pid) == started:
            try:
                os.killpg(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
    # Reap only direct children created by this pytest process.
    deadline = time.monotonic() + 3
    pending = {pid for pid, _ in identities}
    while pending and time.monotonic() < deadline:
        for pid in list(pending):
            try:
                reaped, _ = os.waitpid(pid, os.WNOHANG)
            except ChildProcessError:
                pending.remove(pid)
            else:
                if reaped:
                    pending.remove(pid)
        if pending:
            time.sleep(0.02)


async def _command(env: ScheduleEnv, command: str = "printf hi", **fields: Any) -> str:
    async with env.pool.acquire() as conn:
        async with conn.transaction():
            sid = await db_schedules.create_schedule(
                conn, project_id=env.project_id, profiles={}, fields={
                    "name": "command", "kind": "command", "command": command, "cron": "*/5 * * * *",
                    "timezone": "UTC", "requirement": "", "run_timeout_mode": "none", **fields,
                })
            await db_schedules.update_schedule(conn, sid, {"enabled": True, "enabled_at": env.clock.now})
    return sid


async def _rows(env: ScheduleEnv, sid: str) -> list[dict[str, Any]]:
    return [dict(row) for row in await env.pool.fetch(
        "SELECT * FROM schedule_runs WHERE schedule_id=$1 ORDER BY created_at, id", sid)]


async def _fire(env: ScheduleEnv, sid: str) -> dict[str, Any]:
    await env.due(sid)
    await env.scheduler.tick()
    return (await _rows(env, sid))[-1]


async def _ended(env: ScheduleEnv, sid: str, *, count: int = 1) -> list[dict[str, Any]]:
    deadline = asyncio.get_running_loop().time() + 10
    while True:
        await env.scheduler.tick()
        rows = await _rows(env, sid)
        if len(rows) >= count and all(row["status"] not in {"running", "queued"} for row in rows):
            await env.scheduler.idle()
            return rows
        assert asyncio.get_running_loop().time() < deadline, rows
        await asyncio.sleep(0.05)


async def _runner(env: ScheduleEnv, run_id: str) -> dict[str, Any]:
    path = env.runtime_root / ".local" / "command-runs" / run_id / "runner.json"
    deadline = asyncio.get_running_loop().time() + 5
    while not path.exists():
        assert asyncio.get_running_loop().time() < deadline, "runner did not record its child"
        await asyncio.sleep(0.02)
    return json.loads(path.read_text())


def _alive(pid: int) -> bool:
    result = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True,
                            timeout=2, check=False)
    return bool(result.stdout.strip()) and not result.stdout.strip().startswith("Z")


async def _group_empty(pgid: int) -> None:
    deadline = asyncio.get_running_loop().time() + 3
    while True:
        result = subprocess.run(["ps", "-A", "-o", "pgid=,stat="], capture_output=True, text=True,
                                timeout=2, check=True)
        if not any(int(group) == pgid and not stat.startswith("Z")
                   for group, stat in (line.split() for line in result.stdout.splitlines())):
            return
        assert asyncio.get_running_loop().time() < deadline, "command process group survived"
        await asyncio.sleep(0.02)


def test_command_success_records_exit_times_log_and_no_task(monkeypatch, tmp_path):
    async def test(env: ScheduleEnv):
        sid = await _command(env)
        run = await _fire(env, sid)
        assert run["runner_pid"] and run["runner_started"]
        [run] = await _ended(env, sid)
        assert run["status"] == "succeeded" and run["exit_code"] == 0
        assert run["started_at"] and run["finished_at"]
        assert (env.runtime_root / run["log_path"]).read_bytes() == b"hi"
        assert run["log_bytes"] == 2 and run["log_tail"] == "hi" and not run["log_truncated"]
        assert run["cost_usd"] is None and run["task_id"] is None
        assert await env.pool.fetchval("SELECT count(*) FROM tasks WHERE schedule_id=$1", sid) == 0
        assert not (env.runtime_root / ".local" / "command-runs" / run["id"]).exists()
    run_scheduled(test, monkeypatch, tmp_path)


@pytest.mark.parametrize("restart", [False, True])
def test_nul_output_settles_and_allows_the_next_trigger(monkeypatch, tmp_path, restart):
    async def test(env: ScheduleEnv):
        sid = await _command(env, r"printf 'first\000second\000'")
        run = await _fire(env, sid)
        process = env.scheduler._commands._owned[run["id"]]
        assert await asyncio.to_thread(process.wait, timeout=5) == 0
        directory = env.runtime_root / ".local" / "command-runs" / run["id"]
        # Existing exit records must also settle after an upgrade or restart.
        assert json.loads((directory / "exit.json").read_text())["tail"] == "first\0second\0"
        if restart:
            await env.scheduler.stop()
            env.scheduler = Scheduler(bus=env.bus, clock=env.clock, runtime_root=env.runtime_root)
            await asyncio.to_thread(env.scheduler._commands.prepare)
        [finished] = await _ended(env, sid)
        assert finished["id"] == run["id"]
        assert finished["status"] == "succeeded" and finished["exit_code"] == 0
        assert finished["finished_at"]
        assert finished["log_tail"] == "firstsecond"
        assert finished["log_bytes"] == 13 and not finished["log_truncated"]
        assert (env.runtime_root / finished["log_path"]).read_bytes() == b"first\0second\0"
        assert not directory.exists()
        await _fire(env, sid)
        rows = await _ended(env, sid, count=2)
        assert len(rows) == 2 and all(row["status"] == "succeeded" for row in rows)
    run_scheduled(test, monkeypatch, tmp_path)


def test_nonzero_command_exit_counts_one_failure(monkeypatch, tmp_path):
    async def test(env: ScheduleEnv):
        sid = await _command(env, "exit 3")
        await _fire(env, sid)
        [run] = await _ended(env, sid)
        assert (run["status"], run["exit_code"]) == ("failed", 3)
        assert await env.pool.fetchval("SELECT consecutive_failures FROM schedules WHERE id=$1", sid) == 1
    run_scheduled(test, monkeypatch, tmp_path)


def test_command_overlap_skip_records_every_firing(monkeypatch, tmp_path):
    async def test(env: ScheduleEnv):
        sid = await _command(env, "sleep 3", overlap="skip")
        await _fire(env, sid)
        await _fire(env, sid)
        await _fire(env, sid)
        rows = await _rows(env, sid)
        assert [row["status"] for row in rows].count("running") == 1
        assert [row["status"] for row in rows].count("skipped_overlap") == 2
        assert len(rows) == 3
        await _ended(env, sid, count=3)
    run_scheduled(test, monkeypatch, tmp_path)


@pytest.mark.parametrize("missing_identity", [False, True])
def test_restart_and_agent_sweep_preserve_the_command_runner(monkeypatch, tmp_path, caplog, missing_identity):
    async def test(env: ScheduleEnv):
        sid = await _command(env, "sleep 3")
        run = await _fire(env, sid)
        identity = await _runner(env, run["id"])
        await env.scheduler.stop()
        assert _alive(identity["runner_pid"])
        await proc_tree.sweep(env.runtime_root)
        assert _alive(identity["runner_pid"])
        if missing_identity:
            await env.pool.execute("UPDATE schedule_runs SET runner_pid=NULL, runner_started=NULL WHERE id=$1",
                                   run["id"])
        env.scheduler = Scheduler(bus=env.bus, clock=env.clock, runtime_root=env.runtime_root, tick_seconds=3600)
        await env.scheduler.start()
        try:
            await env.scheduler.tick()
            [adopted] = await _rows(env, sid)
            assert adopted["status"] == "running"
            assert adopted["runner_pid"] == identity["runner_pid"]
            assert adopted["started_at"] == datetime.fromisoformat(identity["started_at"])
            [ended] = await _ended(env, sid)
            assert ended["status"] == "succeeded"
        finally:
            await env.scheduler.stop()
    caplog.set_level("INFO", logger="looper.agents.scheduler")
    run_scheduled(test, monkeypatch, tmp_path)
    assert "command run adopted" in caplog.text


def test_killed_runner_interrupts_the_run_and_ends_its_command_group(monkeypatch, tmp_path):
    async def test(env: ScheduleEnv):
        sid = await _command(env, "sleep 30")
        run = await _fire(env, sid)
        identity = await _runner(env, run["id"])
        os.kill(identity["runner_pid"], signal.SIGKILL)
        [ended] = await _ended(env, sid)
        assert ended["status"] == "interrupted"
        assert "ended without recording an exit code" in ended["error"]
        assert await env.pool.fetchval("SELECT consecutive_failures FROM schedules WHERE id=$1", sid) == 1
        await _group_empty(identity["child_pid"])
    run_scheduled(test, monkeypatch, tmp_path)


def test_a_run_without_start_files_is_interrupted_after_start_grace(monkeypatch, tmp_path):
    async def test(env: ScheduleEnv):
        sid = await _command(env)
        await db_schedules.insert_run(env.pool, schedule_id=sid, scheduled_for=env.clock.now,
                                      trigger="scheduled", status="running",
                                      created_at=env.clock.now - timedelta(minutes=2))
        await env.scheduler.tick()
        [run] = await _rows(env, sid)
        assert run["status"] == "interrupted"
        assert run["error"] == "the server stopped before the command started"
    run_scheduled(test, monkeypatch, tmp_path)


def test_catch_up_command_receives_missed_count_and_trigger(monkeypatch, tmp_path):
    async def test(env: ScheduleEnv):
        sid = await _command(env, 'printf "%s %s" "$LOOPER_SCHEDULE_MISSED" "$LOOPER_SCHEDULE_TRIGGER" > catch-up.txt',
                             cron="0 0 * * *", catch_up="once")
        env.clock.now = datetime.now(UTC).replace(hour=2, minute=0, second=0, microsecond=0)
        await env.pool.execute("UPDATE schedules SET next_fire_at=$2 WHERE id=$1", sid,
                               env.clock.now - timedelta(hours=2))
        await env.scheduler.tick()
        [run] = await _ended(env, sid)
        assert run["trigger"] == "catch_up" and run["missed"] == 1
        assert (Path(env.work_dir) / "catch-up.txt").read_text() == "1 catch_up"
    run_scheduled(test, monkeypatch, tmp_path)


@pytest.mark.parametrize("missing_identity", [False, True])
def test_backstop_kills_a_stopped_runner_and_its_command_group(monkeypatch, tmp_path, missing_identity):
    async def test(env: ScheduleEnv):
        sid = await _command(env, "sleep 30", run_timeout_mode="seconds", run_timeout_seconds=1)
        run = await _fire(env, sid)
        identity = await _runner(env, run["id"])
        os.kill(identity["runner_pid"], signal.SIGSTOP)
        try:
            if missing_identity:
                await env.pool.execute("UPDATE schedule_runs SET runner_pid=NULL, runner_started=NULL WHERE id=$1",
                                       run["id"])
            env.clock.now = datetime.fromisoformat(identity["started_at"]) + timedelta(seconds=200)
            await env.scheduler.tick()
            [ended] = await _rows(env, sid)
            assert ended["status"] == "timed_out"
            assert ended["error"] == (
                "the runner did not end the run at its timeout; Looper killed its process group")
            await _group_empty(identity["child_pid"])
            deadline = asyncio.get_running_loop().time() + 3
            while _alive(identity["runner_pid"]):
                assert asyncio.get_running_loop().time() < deadline
                await asyncio.sleep(0.02)
        finally:
            for sig in (signal.SIGCONT, signal.SIGKILL):
                try:
                    os.kill(identity["runner_pid"], sig)
                except ProcessLookupError:
                    pass
    run_scheduled(test, monkeypatch, tmp_path)


def test_queued_command_starts_after_the_running_command_finishes(monkeypatch, tmp_path):
    async def test(env: ScheduleEnv):
        sid = await _command(env, "sleep 1", overlap="queue")
        first = await _fire(env, sid)
        await _fire(env, sid)
        rows = await _rows(env, sid)
        assert {row["status"] for row in rows} == {"running", "queued"}
        queued = next(row for row in rows if row["status"] == "queued")
        rows = await _ended(env, sid, count=2)
        assert {row["id"] for row in rows} == {first["id"], queued["id"]}
        assert all(row["status"] == "succeeded" for row in rows)
        assert next(row for row in rows if row["id"] == queued["id"])["runner_pid"]
    run_scheduled(test, monkeypatch, tmp_path)


def test_server_without_runtime_root_records_a_failed_command(monkeypatch, tmp_path):
    async def test(env: ScheduleEnv):
        env.scheduler = Scheduler(bus=env.bus, clock=env.clock, runtime_root=None)
        sid = await _command(env)
        run = await _fire(env, sid)
        assert run["status"] == "failed"
        assert run["error"] == "this server cannot run commands (no runtime root)"
        assert await env.pool.fetchval("SELECT consecutive_failures FROM schedules WHERE id=$1", sid) == 1
    run_scheduled(test, monkeypatch, tmp_path)


def test_two_servers_settle_and_notify_a_command_failure_only_once(monkeypatch, tmp_path):
    from looper.agents.command_runs import Finished

    async def test(env: ScheduleEnv):
        sid = await _command(env)
        run_id = await db_schedules.insert_run(env.pool, schedule_id=sid, scheduled_for=env.clock.now,
                                               trigger="scheduled", status="running")
        [row] = await db_schedules.running_runs(pool=env.pool)
        other = Scheduler(bus=env.bus, clock=env.clock, runtime_root=env.runtime_root)
        first_notice, second_notice = AsyncMock(), AsyncMock()
        monkeypatch.setattr(env.scheduler._notifier, "run_ended", first_notice)
        monkeypatch.setattr(other._notifier, "run_ended", second_notice)
        first_release, second_release = Mock(), Mock()
        monkeypatch.setattr(env.scheduler._commands, "release", first_release)
        monkeypatch.setattr(other._commands, "release", second_release)
        observed = Finished(status="failed", error="command exited with code 3", exit_code=3, signal=None,
                            started_at=env.clock.now, ended_at=env.clock.now,
                            log_bytes=0, log_truncated=False, tail="")
        await asyncio.gather(env.scheduler._settle_command(row, observed, env.clock.now),
                             other._settle_command(row, observed, env.clock.now))
        await asyncio.gather(env.scheduler.idle(), other.idle())
        assert first_notice.await_count + second_notice.await_count == 1
        assert first_release.call_count + second_release.call_count == 1
        assert await env.pool.fetchval("SELECT consecutive_failures FROM schedules WHERE id=$1", sid) == 1
        assert (await _rows(env, sid))[0]["id"] == run_id
    run_scheduled(test, monkeypatch, tmp_path)


def test_runner_spawn_error_finishes_the_run_and_notifies_once(monkeypatch, tmp_path):
    async def test(env: ScheduleEnv):
        sid = await _command(env)
        notice = AsyncMock()
        monkeypatch.setattr(env.scheduler._notifier, "run_ended", notice)
        monkeypatch.setattr(env.scheduler._commands, "spawn", Mock(side_effect=OSError("spawn refused")))
        run = await _fire(env, sid)
        await env.scheduler.idle()
        assert run["status"] == "failed" and run["finished_at"]
        assert run["error"] == "the runner could not start: spawn refused"
        notice.assert_awaited_once_with(run["id"])
        assert await env.pool.fetchval("SELECT consecutive_failures FROM schedules WHERE id=$1", sid) == 1
    run_scheduled(test, monkeypatch, tmp_path)


def test_invalid_effective_command_schedule_is_recorded_as_failed(monkeypatch, tmp_path):
    from looper.agents import schedule_templates

    async def test(env: ScheduleEnv):
        sid = await _command(env)
        monkeypatch.setattr(schedule_templates, "effective_schedule", AsyncMock(side_effect=ValueError("no work directory")))
        spawn = Mock()
        monkeypatch.setattr(env.scheduler._commands, "spawn", spawn)
        run = await _fire(env, sid)
        assert run["status"] == "failed" and run["error"] == "no work directory"
        spawn.assert_not_called()
        assert await env.pool.fetchval("SELECT consecutive_failures FROM schedules WHERE id=$1", sid) == 1
    run_scheduled(test, monkeypatch, tmp_path)


def test_server_without_runtime_root_interrupts_a_preexisting_command_run(monkeypatch, tmp_path):
    async def test(env: ScheduleEnv):
        sid = await _command(env)
        await db_schedules.insert_run(env.pool, schedule_id=sid, scheduled_for=env.clock.now,
                                      trigger="scheduled", status="running")
        env.scheduler = Scheduler(bus=env.bus, clock=env.clock, runtime_root=None)
        await env.scheduler.tick()
        [run] = await _rows(env, sid)
        assert run["status"] == "interrupted" and run["error"] == "this server cannot run commands"
        assert await env.pool.fetchval("SELECT consecutive_failures FROM schedules WHERE id=$1", sid) == 1
    run_scheduled(test, monkeypatch, tmp_path)


def test_parallel_commands_run_up_to_the_limit_without_creating_tasks(monkeypatch, tmp_path):
    async def test(env: ScheduleEnv):
        sid = await _command(env, "sleep 1", overlap="parallel", max_parallel_runs=2)
        await _fire(env, sid)
        await _fire(env, sid)
        await _fire(env, sid)
        rows = await _rows(env, sid)
        assert [row["status"] for row in rows].count("running") == 2
        assert [row["status"] for row in rows].count("skipped_overlap") == 1
        assert all(row["task_id"] is None for row in rows)
        rows = await _ended(env, sid, count=3)
        assert [row["status"] for row in rows].count("succeeded") == 2
    run_scheduled(test, monkeypatch, tmp_path)


def test_command_missed_skips_are_kept_as_separate_run_rows(monkeypatch, tmp_path):
    async def test(env: ScheduleEnv):
        sid = await _command(env, catch_up="skip")
        for hour in (12, 13):
            env.clock.now = datetime(2030, 1, 15, hour, 3, tzinfo=UTC)
            await env.pool.execute("UPDATE schedules SET next_fire_at=$2 WHERE id=$1", sid,
                                   env.clock.now.replace(minute=0))
            await env.scheduler.tick()
        rows = await _rows(env, sid)
        assert len(rows) == 2 and all(row["status"] == "skipped_missed" for row in rows)
    run_scheduled(test, monkeypatch, tmp_path)
