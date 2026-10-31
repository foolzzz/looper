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
from looper.agents.drain import DrainState
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


@pytest.mark.parametrize("failure", ["timeout", "os_error", "empty", "nonzero"])
def test_ps_failure_preserves_live_commands_until_the_next_tick(monkeypatch, tmp_path, caplog, failure):
    async def test(env: ScheduleEnv):
        sid = await _command(env, "sleep 3; printf done", overlap="parallel", max_parallel_runs=2)
        run_ids = [await env.scheduler.run_now(sid) for _ in range(2)]
        identities = [await _runner(env, run_id) for run_id in run_ids]
        processes = [env.scheduler._commands._owned[run_id] for run_id in run_ids]

        def failed_ps(args, **kwargs):
            if failure == "timeout":
                raise subprocess.TimeoutExpired(args, 5)
            if failure == "os_error":
                raise OSError("ps unavailable")
            return subprocess.CompletedProcess(args, 2 if failure == "nonzero" else 0, "", "ps failed")

        for tick in range(2):
            with monkeypatch.context() as patch:
                patch.setattr(subprocess, "run", failed_ps)
                await env.scheduler.tick()
            assert all(run["status"] == "running" for run in await _rows(env, sid))
            assert all(_alive(identity[key]) for identity in identities for key in ("runner_pid", "child_pid"))
            warnings = [record for record in caplog.records if record.levelname == "WARNING"]
            assert len(warnings) == tick + 1
        await env.scheduler.tick()
        assert all(run["status"] == "running" for run in await _rows(env, sid))
        for process in processes:
            assert await asyncio.to_thread(process.wait, timeout=5) == 0
        await env.scheduler.tick()
        assert all(run["status"] == "succeeded" for run in await _rows(env, sid))
    run_scheduled(test, monkeypatch, tmp_path)


def test_runner_killed_before_its_first_record_is_interrupted_in_one_tick(monkeypatch, tmp_path):
    async def test(env: ScheduleEnv):
        # Block before the runner can write metadata or start a command.
        blocked = tmp_path / "blocked_runner.py"
        blocked.write_text("import signal\nsignal.pause()\n")
        monkeypatch.setattr(env.scheduler._commands, "_runner", blocked)
        sid = await _command(env)
        run_id = await env.scheduler.run_now(sid)
        process = env.scheduler._commands._owned[run_id]
        directory = env.runtime_root / ".local" / "command-runs" / run_id
        try:
            assert not (directory / "runner.json").exists()
            process.kill()
            assert await asyncio.to_thread(process.wait, timeout=5) == -signal.SIGKILL
            await env.scheduler.tick()
            [run] = await _rows(env, sid)
            assert run["status"] == "interrupted"
            assert "ended without recording an exit code" in run["error"]
        finally:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)
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


def test_queued_catch_up_keeps_the_first_missed_time_after_restart(monkeypatch, tmp_path):
    async def test(env: ScheduleEnv):
        sid = await _command(env, 'if [ "$LOOPER_SCHEDULE_TRIGGER" = manual ]; then sleep 3; fi; '
                             'printf "%s\\n%s\\n" "$LOOPER_SCHEDULE_MISSED" '
                             '"$LOOPER_SCHEDULE_MISSED_SINCE"', overlap="queue", catch_up="once")
        first_id = await env.scheduler.run_now(sid)
        process = env.scheduler._commands._owned[first_id]
        await _runner(env, first_id)
        await env.due(sid, fires_ago=2)
        missed_since = await env.pool.fetchval("SELECT next_fire_at FROM schedules WHERE id=$1", sid)
        await env.scheduler.tick()
        queued = next(run for run in await _rows(env, sid) if run["status"] == "queued")
        assert queued["trigger"] == "catch_up" and queued["missed"] == 3
        assert process.poll() is None
        await env.scheduler.stop()
        assert await asyncio.to_thread(process.wait, timeout=5) == 0
        env.scheduler = Scheduler(bus=env.bus, clock=env.clock, runtime_root=env.runtime_root)
        await asyncio.to_thread(env.scheduler._commands.prepare)
        rows = await _ended(env, sid, count=2)
        catch_up = next(run for run in rows if run["id"] == queued["id"])
        assert catch_up["status"] == "succeeded"
        assert (env.runtime_root / catch_up["log_path"]).read_text().splitlines() == ["3", missed_since.isoformat()]
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


def _with_drain(env: ScheduleEnv) -> DrainState:
    drain = DrainState(clock=env.clock)
    env.scheduler = Scheduler(bus=env.bus, clock=env.clock, runtime_root=env.runtime_root, drain=drain)
    env.scheduler._commands.prepare()
    drain.add_source(env.scheduler.drain_waiting)
    return drain


@pytest.mark.parametrize("catch_up,expected", [("once", "running"), ("skip", "skipped_missed")])
def test_drain_fires_commands_and_preserves_agent_catch_up(monkeypatch, tmp_path, catch_up, expected):
    async def test(env: ScheduleEnv):
        drain = _with_drain(env)
        agent = (await env.enabled(catch_up=catch_up))["schedule_id"]
        command = await _command(env, "sleep 1")
        await env.due(agent, fires_ago=2)
        due = await env.pool.fetchval("SELECT next_fire_at FROM schedules WHERE id=$1", agent)
        # Put the blocked agent first: an unfiltered due lookup must not starve the command.
        await env.pool.execute("UPDATE schedules SET next_fire_at=$2 WHERE id=$1", command,
                               due + timedelta(minutes=5))
        drain.begin("runs")
        await env.scheduler.tick()
        [run] = await _rows(env, command)
        identity = await _runner(env, run["id"])
        assert run["status"] == "running" and _alive(identity["runner_pid"])
        assert await _rows(env, agent) == []
        assert await env.pool.fetchval("SELECT next_fire_at FROM schedules WHERE id=$1", agent) == due
        assert not env.bus.sent
        assert drain.snapshot()["phase"] == "drained"
        assert not any(drain.snapshot()["waiting"].values())
        await _ended(env, command)

        # A cancelled deployment resumes normal catch-up from the unchanged cursor.
        env.clock.now += timedelta(minutes=3)
        assert drain.cancel()["phase"] == "serving"
        await env.scheduler.tick()
        [run] = await _rows(env, agent)
        assert run["status"] == expected and run["missed"] == 3
        assert len(env.bus.of("task_start")) == (1 if expected == "running" else 0)
        await _ended(env, command)
    run_scheduled(test, monkeypatch, tmp_path)


@pytest.mark.parametrize("kind", ["agent", "command"])
def test_run_now_during_drain_returns_command_run_or_agent_conflict(monkeypatch, tmp_path, kind):
    async def test(env: ScheduleEnv):
        drain = _with_drain(env)
        sid = (await _command(env) if kind == "command"
               else (await env.enabled())["schedule_id"])
        next_fire = await env.pool.fetchval("SELECT next_fire_at FROM schedules WHERE id=$1", sid)
        drain.begin("runs")
        status, payload = await env.call("POST", "/api/v1/schedule_admin/run", {"schedule_id": sid})
        if kind == "command":
            assert status == 200, payload
            run_id = payload["run"]["run_id"]
            assert run_id and payload["run"]["trigger"] == "manual"
            [run] = await _ended(env, sid)
            assert run["id"] == run_id and run["status"] == "succeeded"
        else:
            assert status == 409, payload
            assert payload["error"]["message"] == (
                "the service is draining for an upgrade: run it after the restart")
            assert await env.scheduler.run_now(sid) is None
            assert await _rows(env, sid) == []
        assert await env.pool.fetchval("SELECT next_fire_at FROM schedules WHERE id=$1", sid) == next_fire
        assert (await env.call("POST", "/api/v1/schedule_admin/run",
                               {"schedule_id": "sched-missing"}))[0] == 404
    run_scheduled(test, monkeypatch, tmp_path)


@pytest.mark.parametrize("kind", ["agent", "command"])
def test_manager_confirmed_run_during_drain_obeys_schedule_kind(monkeypatch, tmp_path, kind):
    from looper.agents.ops import Turn
    from looper.agents.ops.service import ManagerOps, Platform

    async def test(env: ScheduleEnv):
        drain = _with_drain(env)
        sid = (await _command(env) if kind == "command"
               else (await env.enabled())["schedule_id"])
        ops = ManagerOps(Platform(bus=env.bus, task_router=None, scheduler=env.scheduler),
                         url="http://127.0.0.1:9", token="test-token")
        asking = Turn(channel="web", chat="drain-test", message_id="ask")
        ops.begin_turn(asking, binding="ask")
        asked = await ops.call("looper_schedules", {"action": "run", "schedule": sid}, binding="ask")
        assert asked["status"] == "needs_confirmation", asked
        ops.end_turn(asking)
        drain.begin("runs")
        confirming = Turn(channel="web", chat="drain-test", message_id="confirm")
        ops.begin_turn(confirming, binding="confirm")
        answer = await ops.call("looper_confirm", {"confirmation_id": asked["confirmation_id"]},
                                binding="confirm")
        ops.end_turn(confirming)
        if kind == "command":
            assert answer["status"] == "ok", answer
            [run] = await _ended(env, sid)
            assert answer["result"]["run"]["run_id"] == run["id"]
            assert run["status"] == "succeeded"
        else:
            assert answer == {"status": "error", "error": {"code": "conflict", "message":
                "the service is draining for an upgrade: run it after the restart"}}
            assert await _rows(env, sid) == []
    run_scheduled(test, monkeypatch, tmp_path)


def test_drain_starts_only_queued_commands_after_previous_run_finishes(monkeypatch, tmp_path):
    async def test(env: ScheduleEnv):
        drain = _with_drain(env)
        command = await _command(env, "sleep 1", overlap="queue")
        first = await env.scheduler.run_now(command)
        queued = await env.scheduler.run_now(command)
        agent = (await env.enabled(overlap="queue"))["schedule_id"]
        agent_first = await env.scheduler.run_now(agent)
        agent_queued = await env.scheduler.run_now(agent)
        [task] = env.bus.of("task_start")
        drain.begin("runs")
        await env.end_task(task, "completed")  # Also exercises the direct _start_queued entry point.
        assert (await db_schedules.get_run(agent_first))["status"] == "completed"
        assert (await db_schedules.get_run(agent_queued))["status"] == "queued"
        assert (await db_schedules.get_run(first))["status"] == "running"
        assert (await db_schedules.get_run(queued))["status"] == "queued"
        rows = await _ended(env, command, count=2)
        assert all(row["status"] == "succeeded" for row in rows)
        assert next(row for row in rows if row["id"] == queued)["runner_pid"]
        assert (await db_schedules.get_run(agent_queued))["status"] == "queued"
        assert len(env.bus.of("task_start")) == 1
        assert drain.snapshot()["phase"] == "drained"
    run_scheduled(test, monkeypatch, tmp_path)


@pytest.mark.parametrize("entry", ["due", "manual", "queued"])
def test_command_launch_does_not_hold_drain_waiting(monkeypatch, tmp_path, entry):
    async def test(env: ScheduleEnv):
        drain = _with_drain(env)
        sid = await _command(env)
        if entry == "due":
            await env.due(sid)
        elif entry == "queued":
            await db_schedules.insert_run(env.pool, schedule_id=sid, scheduled_for=env.clock.now,
                                          trigger="manual", status="queued")
        drain.begin("runs")
        start = env.scheduler._start_runner

        async def starting(*args):
            assert drain.snapshot()["phase"] == "drained"
            assert env.scheduler.drain_waiting() == {"gates": 0, "starting": 0}
            await start(*args)

        monkeypatch.setattr(env.scheduler, "_start_runner", starting)
        if entry == "manual":
            assert await env.scheduler.run_now(sid)
        else:
            await env.scheduler.tick()
        [run] = await _ended(env, sid)
        assert run["status"] == "succeeded"
    run_scheduled(test, monkeypatch, tmp_path)


def test_command_due_lookup_can_finish_when_drain_begins_mid_tick(monkeypatch, tmp_path):
    async def test(env: ScheduleEnv):
        drain = _with_drain(env)
        sid = await _command(env)
        await env.due(sid)
        lock = db_schedules.lock_next_due

        async def lookup(*args, **kwargs):
            row = await lock(*args, **kwargs)
            drain.begin("runs")
            return row

        monkeypatch.setattr(db_schedules, "lock_next_due", lookup)
        await env.scheduler.tick()
        [run] = await _ended(env, sid)
        assert run["status"] == "succeeded"
        assert drain.snapshot()["phase"] == "drained"
    run_scheduled(test, monkeypatch, tmp_path)
