"""Command supervisors survive the event loop and are observed by files and pid identity."""

import asyncio
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import signal
import subprocess
import time

import pytest

from looper.agents import command_runner, proc_tree
from looper.agents.command_runs import (
    CommandRuns, CommandSpec, End, Finished, Lost, RUNNER_DIR, RUNS_DIR, Running, Starting, run_status,
)


NOW = datetime.now(timezone.utc)


@pytest.mark.parametrize(("end", "code", "sig", "status", "error"), [
    (End.EXITED, 0, None, "succeeded", None),
    (End.EXITED, 3, None, "failed", "the command exited with status 3"),
    (End.EXITED, None, 15, "failed", "the command was ended by signal 15"),
    (End.TIMED_OUT, None, 15, "timed_out", "stopped after the run timeout (its process group was ended)"),
    (End.RUNNER_TERMINATED, None, 15, "interrupted",
     "the runner received signal 15 (shutdown, logout or a manual kill)"),
    (End.START_FAILED, None, None, "failed", None),
    (End.RUNNER_ERROR, None, None, "interrupted", "the runner failed"),
])
def test_run_status(end, code, sig, status, error):
    assert run_status(end, code, sig) == (status, error)


def row(run_id="srun-test", **fields):
    return {"id": run_id, "created_at": NOW - timedelta(minutes=2),
            "runner_pid": None, "runner_started": None, "started_at": None, **fields}


def directory(root, run_id="srun-test", **runner):
    path = root / RUNS_DIR / run_id
    path.mkdir(parents=True)
    (path / "spec.json").write_text(json.dumps({"timeout_seconds": 5}))
    if runner:
        (path / "runner.json").write_text(json.dumps(runner))
    return path


def exit_record(**fields):
    return {"format": 1, "end": "exited", "exit_code": 0, "signal": None,
            "started_at": (NOW - timedelta(seconds=1)).isoformat(), "ended_at": NOW.isoformat(),
            "log_bytes": 2, "log_truncated": False, "tail": "hi", "error": None, **fields}


def test_inspect_finished_uses_exit_timestamp_and_clamps_future(tmp_path):
    path = directory(tmp_path)
    runs = CommandRuns(tmp_path)
    for end in (NOW - timedelta(seconds=1), NOW + timedelta(seconds=10)):
        (path / "exit.json").write_text(json.dumps(exit_record(ended_at=end.isoformat())))
        observed = runs.inspect([row()], NOW)["srun-test"]
        assert isinstance(observed, Finished)
        assert observed.status == "succeeded"
        assert observed.ended_at == min(end, NOW)
        assert observed.started_at == NOW - timedelta(seconds=1)
        assert (observed.exit_code, observed.signal, observed.log_bytes, observed.log_truncated,
                observed.tail) == (0, None, 2, False, "hi")


@pytest.mark.parametrize(("end", "detail", "expected"), [
    ("timed_out", None, "stopped after the run timeout of 5 s (its process group was ended)"),
    ("start_failed", "cannot start command", "cannot start command"),
    ("runner_error", "OSError: no space", "the runner failed: OSError: no space"),
])
def test_inspect_finished_error_details(tmp_path, end, detail, expected):
    path = directory(tmp_path)
    (path / "exit.json").write_text(json.dumps(exit_record(end=end, error=detail)))
    assert CommandRuns(tmp_path).inspect([row()], NOW)["srun-test"].error == expected


@pytest.mark.parametrize("from_database", [False, True])
def test_inspect_live_runner_identity(tmp_path, from_database):
    process = subprocess.Popen(["sleep", "5"])
    try:
        started = proc_tree._start_time(process.pid)
        assert started is not None
        fields = {"runner_pid": process.pid, "runner_started": started}
        directory(tmp_path, **fields, child_pid=process.pid, child_started=started, started_at=NOW.isoformat())
        run = row(**(fields if from_database else {}))
        observed = CommandRuns(tmp_path).inspect([run], NOW)[run["id"]]
        assert observed == Running(NOW, (process.pid, started), process.pid, started)
        run.update(runner_pid=process.pid, runner_started="wrong identity")
        assert isinstance(CommandRuns(tmp_path).inspect([run], NOW)[run["id"]], Lost)
    finally:
        process.kill()
        process.wait(timeout=2)


def test_inspect_zombie_is_not_running(tmp_path):
    process = subprocess.Popen(["sleep", "0.1"])
    try:
        started = proc_tree._start_time(process.pid)
        assert started is not None
        directory(tmp_path, runner_pid=process.pid, runner_started=started)
        # Do not poll or wait: confirm the exited child remains a zombie for this observation.
        deadline = time.monotonic() + 2
        while True:
            state = subprocess.run(["ps", "-o", "stat=", "-p", str(process.pid)],
                                   capture_output=True, text=True, timeout=1, check=True).stdout.strip()
            if state.startswith("Z") or time.monotonic() >= deadline:
                break
            time.sleep(0.02)
        assert state.startswith("Z")
        observed = CommandRuns(tmp_path).inspect([row()], NOW)["srun-test"]
        assert isinstance(observed, Lost)
    finally:
        process.wait(timeout=2)


@pytest.mark.parametrize(("age", "expected"), [(10, Starting), (120, Lost)])
def test_inspect_start_grace(tmp_path, age, expected):
    directory(tmp_path)
    observed = CommandRuns(tmp_path).inspect([row(created_at=NOW - timedelta(seconds=age))], NOW)["srun-test"]
    assert isinstance(observed, expected)


def test_inspect_missing_directory_explains_server_interruption(tmp_path):
    observed = CommandRuns(tmp_path).inspect([row()], NOW)["srun-test"]
    assert observed == Lost("the server stopped before the command started", None)


@pytest.mark.parametrize("contents", ["{", "[]", '{"end":"unknown"}'])
def test_inspect_unreadable_exit_is_not_a_finished_run(tmp_path, contents):
    path = directory(tmp_path)
    (path / "exit.json").write_text(contents)
    assert isinstance(CommandRuns(tmp_path).inspect([row()], NOW)["srun-test"], Lost)


def test_prepare_is_content_addressed_and_does_not_rewrite(tmp_path):
    runs = CommandRuns(tmp_path)
    path = runs.prepare()
    before = path.stat().st_mtime_ns
    assert runs.prepare() == path
    assert path.stat().st_mtime_ns == before
    assert path.name == f"runner-{hashlib.sha256(Path(command_runner.__file__).read_bytes()).hexdigest()[:12]}.py"
    assert list((tmp_path / RUNNER_DIR).iterdir()) == [path]
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700


def spec(root):
    return CommandSpec("srun-test", "sched-test", "sleep 5", str(root), {}, None, None, {})


def test_spawn_requires_prepare(tmp_path):
    with pytest.raises(RuntimeError, match="prepare"):
        CommandRuns(tmp_path).spawn(spec(tmp_path))


def test_spawn_survives_event_loop_close_and_rejects_duplicate(tmp_path):
    runs = CommandRuns(tmp_path, kill_grace_seconds=1, leftover_grace_seconds=1)
    runs.prepare()

    async def start():
        return await asyncio.to_thread(runs.spawn, spec(tmp_path))

    pid, started = asyncio.run(start())
    process = runs._owned["srun-test"]
    path = tmp_path / RUNS_DIR / "srun-test"
    try:
        assert started == proc_tree._start_time(pid)
        assert process.poll() is None
        with pytest.raises(FileExistsError):
            runs.spawn(spec(tmp_path))
        recorded = json.loads((path / "spec.json").read_text())
        assert recorded["log_path"] == str(tmp_path / "logs/schedules/sched-test/srun-test.log")
        assert recorded["kill_grace_seconds"] == recorded["leftover_grace_seconds"] == 1
        for name in ("spec.json", "runner.log"):
            assert (path / name).stat().st_mode & 0o777 == 0o600
        assert path.stat().st_mode & 0o777 == 0o700
        deadline = time.monotonic() + 3
        while not (path / "runner.json").exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        data = json.loads((path / "runner.json").read_text())
        assert process.poll() is None
        observed = runs.inspect([row()], NOW)["srun-test"]
        assert isinstance(observed, Running)
    finally:
        if (path / "runner.json").exists():
            data = json.loads((path / "runner.json").read_text())
            runs.end_group((data["child_pid"], data["child_started"]))
        runs.kill_runner(pid, started)
        process.wait(timeout=3)
        runs.reap()
    assert not runs._owned


def test_end_group_and_kill_runner_require_matching_identity(tmp_path):
    process = subprocess.Popen(["sleep", "5"], start_new_session=True)
    try:
        runs = CommandRuns(tmp_path)
        runs.end_group((process.pid, "wrong identity"))
        runs.kill_runner(process.pid, None)
        assert process.poll() is None
        runs.end_group((process.pid, proc_tree._start_time(process.pid)))
        assert process.wait(timeout=2) == -signal.SIGKILL
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=2)


def test_release_preserves_command_logs(tmp_path):
    directory(tmp_path)
    log = tmp_path / CommandRuns.log_path("sched-test", "srun-test")
    log.parent.mkdir(parents=True)
    log.write_bytes(b"hi")
    runs = CommandRuns(tmp_path)
    runs.release("srun-test")
    runs.release("srun-test")
    assert not (tmp_path / RUNS_DIR / "srun-test").exists()
    assert log.read_bytes() == b"hi"


def test_inspect_batches_processes_and_rechecks_exit_race(tmp_path, monkeypatch):
    runs = CommandRuns(tmp_path)
    records = [row("one", runner_pid=101, runner_started="start one"),
               row("two", runner_pid=102, runner_started="start two")]
    one = directory(tmp_path, "one")
    directory(tmp_path, "two")
    calls = []

    def snapshot(args, **kwargs):
        calls.append((args, kwargs))
        (one / "exit.json").write_text(json.dumps(exit_record()))
        return subprocess.CompletedProcess(args, 0, "102 S start two\n", "")

    monkeypatch.setattr(subprocess, "run", snapshot)
    observed = runs.inspect(records, NOW)
    assert isinstance(observed["one"], Finished)
    assert observed["two"] == Running(None, None, 102, "start two")
    assert len(calls) == 1
    assert calls[0][0] == ["ps", "-o", "pid=,stat=,lstart=", "-p", "101,102"]
    assert calls[0][1]["env"]["LC_ALL"] == "C"
    assert calls[0][1]["env"]["TZ"] == "UTC"
