"""Start standalone command supervisors and observe them across server restarts."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
from typing import Any

from .. import child_env
from . import command_runner, proc_tree

RUNS_DIR = Path(".local") / "command-runs"
RUNNER_DIR = Path(".local") / "command-runner"
LOGS_DIR = Path("logs") / "schedules"
LOG_MAX_BYTES = 50 * 1024 * 1024
LOG_TAIL_RING_BYTES = 1024 * 1024
KILL_GRACE_SECONDS = 30
LEFTOVER_GRACE_SECONDS = 5
START_GRACE = timedelta(seconds=60)
BACKSTOP_SLACK = timedelta(seconds=90)


class End(StrEnum):
    EXITED = "exited"
    TIMED_OUT = "timed_out"
    RUNNER_TERMINATED = "runner_terminated"
    START_FAILED = "start_failed"
    RUNNER_ERROR = "runner_error"


@dataclass(frozen=True)
class CommandSpec:
    run_id: str
    schedule_id: str
    command: str
    work_dir: str
    env: Mapping[str, str]
    env_file: str | None
    timeout_seconds: int | None
    schedule_env: Mapping[str, str]


@dataclass(frozen=True)
class Finished:
    status: str
    error: str | None
    exit_code: int | None
    signal: int | None
    started_at: datetime | None
    ended_at: datetime
    log_bytes: int
    log_truncated: bool
    tail: str


@dataclass(frozen=True)
class Running:
    started_at: datetime | None
    child: tuple[int, str] | None
    runner_pid: int | None = None
    runner_started: str | None = None


@dataclass(frozen=True)
class Starting:
    pass


@dataclass(frozen=True)
class Lost:
    reason: str
    child: tuple[int, str] | None = None


Observation = Finished | Running | Starting | Lost


def run_status(end: End, exit_code: int | None, signal: int | None) -> tuple[str, str | None]:
    """Map the supervisor's end reason; file-specific details are added by ``inspect``."""
    match end:
        case End.EXITED:
            if signal is not None:
                return "failed", f"the command was ended by signal {signal}"
            if exit_code == 0:
                return "succeeded", None
            return "failed", f"the command exited with status {exit_code}"
        case End.TIMED_OUT:
            return "timed_out", "stopped after the run timeout (its process group was ended)"
        case End.RUNNER_TERMINATED:
            return "interrupted", f"the runner received signal {signal} (shutdown, logout or a manual kill)"
        case End.START_FAILED:
            return "failed", None
        case End.RUNNER_ERROR:
            return "interrupted", "the runner failed"
    raise ValueError(f"unknown command end reason: {end}")


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    result = value if isinstance(value, datetime) else datetime.fromisoformat(value)
    if result.tzinfo is None:
        raise ValueError("command timestamps must include a timezone")
    return result


class CommandRuns:
    def __init__(self, runtime_root: Path, *, kill_grace_seconds: int = KILL_GRACE_SECONDS,
                 leftover_grace_seconds: int = LEFTOVER_GRACE_SECONDS) -> None:
        self.root = runtime_root.resolve()
        self.kill_grace_seconds = kill_grace_seconds
        self.leftover_grace_seconds = leftover_grace_seconds
        self._runner: Path | None = None
        self._owned: dict[str, subprocess.Popen[bytes]] = {}

    def prepare(self) -> Path:
        if self._runner is not None:
            return self._runner
        source = Path(command_runner.__file__).read_bytes()
        directory = self.root / RUNNER_DIR
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        directory.chmod(0o700)
        path = directory / f"runner-{hashlib.sha256(source).hexdigest()[:12]}.py"
        try:
            identical = path.read_bytes() == source
        except FileNotFoundError:
            identical = False
        if not identical:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "wb") as target:
                target.write(source)
        path.chmod(0o600)
        self._runner = path
        return path

    @staticmethod
    def log_path(schedule_id: str, run_id: str) -> str:
        return str(LOGS_DIR / schedule_id / f"{run_id}.log")

    def spawn(self, spec: CommandSpec) -> tuple[int, str | None]:
        if self._runner is None:
            raise RuntimeError("prepare must be called before starting a command runner")
        directory = self.root / RUNS_DIR
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        directory = directory / spec.run_id
        directory.mkdir(mode=0o700)  # A run id may only be started once.
        data = {
            "format": 1, "run_id": spec.run_id, "schedule_id": spec.schedule_id,
            "command": spec.command, "work_dir": spec.work_dir,
            "env": dict(spec.env), "env_file": spec.env_file,
            "timeout_seconds": spec.timeout_seconds, "schedule_env": dict(spec.schedule_env),
            "log_path": str(self.root / self.log_path(spec.schedule_id, spec.run_id)),
            "log_max_bytes": LOG_MAX_BYTES, "log_tail_ring_bytes": LOG_TAIL_RING_BYTES,
            "kill_grace_seconds": self.kill_grace_seconds,
            "leftover_grace_seconds": self.leftover_grace_seconds,
        }
        fd = os.open(directory / "spec.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as target:
            json.dump(data, target)
        log_fd = os.open(directory / "runner.log", os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            process = subprocess.Popen(
                [sys.executable, "-I", str(self._runner), str(directory)], start_new_session=True,
                stdin=subprocess.DEVNULL, stdout=log_fd, stderr=subprocess.STDOUT,
                cwd=directory, env=child_env.inherited(), close_fds=True,
            )
        finally:
            os.close(log_fd)
        self._owned[spec.run_id] = process
        return process.pid, proc_tree._start_time(process.pid)

    def reap(self) -> None:
        for run_id, process in list(self._owned.items()):
            if process.poll() is not None:
                del self._owned[run_id]

    def _finished(self, directory: Path, now: datetime) -> Finished | None:
        data = _read_json(directory / "exit.json")
        if data is None:
            return None
        try:
            end = End(data["end"])
            status, error = run_status(end, data["exit_code"], data["signal"])
            ended_at = _datetime(data["ended_at"])
            if ended_at is None:
                return None
            if end == End.TIMED_OUT:
                spec = _read_json(directory / "spec.json") or {}
                timeout = spec.get("timeout_seconds")
                if timeout is not None:
                    error = f"stopped after the run timeout of {timeout} s (its process group was ended)"
            elif end == End.START_FAILED:
                error = data.get("error")
            elif end == End.RUNNER_ERROR and data.get("error"):
                error = f"the runner failed: {data['error']}"
            return Finished(status, error, data["exit_code"], data["signal"],
                            _datetime(data["started_at"]), min(ended_at, now),
                            int(data["log_bytes"]), bool(data["log_truncated"]), data["tail"])
        except (KeyError, ValueError, TypeError):
            return None

    def inspect(self, runs: Sequence[Mapping[str, Any]], now: datetime, *, strict: bool = False) -> dict[str, Observation]:
        """Observe runs; destructive cleanup uses strict mode to preserve files if ps cannot run."""
        results: dict[str, Observation] = {}
        pending = []
        for run in runs:
            directory = self.root / RUNS_DIR / run["id"]
            finished = self._finished(directory, now)
            if finished is not None:
                results[run["id"]] = finished
                continue
            runner = _read_json(directory / "runner.json")
            identity = runner or {}
            pid = run.get("runner_pid") or identity.get("runner_pid")
            started = run.get("runner_started") or identity.get("runner_started")
            pending.append((run, directory, runner, pid, started))

        pids = {pid for _, _, _, pid, _ in pending if isinstance(pid, int) and pid > 0}
        live: dict[int, str] = {}
        if pids:
            try:
                listed = subprocess.run(
                    ["ps", "-o", "pid=,stat=,lstart=", "-p", ",".join(map(str, sorted(pids)))],
                    capture_output=True, text=True, timeout=5, check=False,
                    env={**child_env.inherited(), "LC_ALL": "C", "TZ": "UTC"},
                )
                if strict and listed.returncode not in (0, 1):
                    raise OSError(f"runner process query failed with status {listed.returncode}")
                for line in listed.stdout.splitlines():
                    fields = line.split(maxsplit=2)
                    if len(fields) == 3 and not fields[1].startswith("Z"):
                        try:
                            live[int(fields[0])] = " ".join(fields[2].split())
                        except ValueError:
                            continue
            except (OSError, subprocess.SubprocessError):
                if strict:
                    raise
        for run, directory, runner, pid, started in pending:
            identity = runner or {}
            child_pid, child_started = identity.get("child_pid"), identity.get("child_started")
            child = ((child_pid, child_started)
                     if isinstance(child_pid, int) and child_pid > 0 and isinstance(child_started, str) else None)
            if started is not None and live.get(pid) == started:
                try:
                    started_at = _datetime(run.get("started_at") or identity.get("started_at"))
                except (ValueError, TypeError):
                    started_at = None
                results[run["id"]] = Running(started_at, child, pid, started)
            elif (finished := self._finished(directory, now)) is not None:
                results[run["id"]] = finished
            elif runner is None and now - run["created_at"] < START_GRACE:
                results[run["id"]] = Starting()
            elif not directory.is_dir() or not (directory / "spec.json").is_file():
                results[run["id"]] = Lost("the server stopped before the command started", child)
            else:
                results[run["id"]] = Lost(f"its runner (pid {pid}) ended without recording an exit code", child)
        return results

    def end_group(self, child: tuple[int, str] | None) -> None:
        if child is not None and proc_tree._start_time(child[0]) == child[1]:
            try:
                os.killpg(child[0], signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass

    def kill_runner(self, pid: int, started: str | None) -> None:
        if started is not None and proc_tree._start_time(pid) == started:
            try:
                os.kill(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass

    def release(self, run_id: str) -> None:
        shutil.rmtree(self.root / RUNS_DIR / run_id, ignore_errors=True)
