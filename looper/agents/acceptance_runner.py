"""Execute step checks after Worker success, recording evidence without changing decisions."""
from __future__ import annotations

import asyncio
from collections.abc import Sequence
import hashlib
import logging
import os
from pathlib import Path
import re
import shutil
import subprocess
import time
from typing import Any
import uuid
import weakref

from looper import child_env
from looper.db import activity, projects, step_check_runs as runs, tasks
from .acceptance_checks import ArtifactCheck, Check
from . import acceptance_artifacts as artifacts, proc_tree
from .acceptance_sandbox import local_prefix
from .project_execution import AcceptanceChecksMode, ProjectExecution
from .verification_writes import VerificationWrites

logger = logging.getLogger(__name__)
MAX_CONCURRENT_CHECKS = 2
STEP_BUDGET_SECONDS = 900
OUTPUT_TAIL_CHARS = 4000
MAX_OUTPUT_BYTES = 16 * 1024 * 1024
_loops: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()
_SECRET_NAME = re.compile(r"(?:^|_)(?:KEY|TOKEN|SECRET|PASSWORD|DATABASE_URL|DSN)(?:$|_)")


class EvidenceUnavailable(Exception):
    """The runner cannot durably record a measurement; never launch without its row."""


class _OutputLimit(Exception):
    pass


def _coordination():
    loop = asyncio.get_running_loop()
    if loop not in _loops:
        _loops[loop] = (asyncio.Semaphore(MAX_CONCURRENT_CHECKS), weakref.WeakValueDictionary())
    return _loops[loop]


def _environment() -> dict[str, str]:
    inherited = child_env.inherited()
    # Runtime bindings can contain provider credentials. Checks get only OS basics,
    # never those bindings, proxies, SSH agents or git command overrides.
    names = {"PATH", "HOME", "USER", "LOGNAME", "TMPDIR", "TMP", "TEMP", "LANG", "LC_ALL", "LC_CTYPE", "TZ"}
    return {key: value for key, value in inherited.items() if key in names}


class _Output:
    def __init__(self):
        self.total = 0
        self.digest = hashlib.sha256()
        self.data = b""
        self.secrets = sorted({v for k, v in os.environ.items()
                               if (k == "PGPASSWORD" or _SECRET_NAME.search(k)) and len(v) >= 4},
                              key=len, reverse=True)
        self.keep = OUTPUT_TAIL_CHARS * 4 + max((len(v.encode()) for v in self.secrets), default=0)

    def consume(self, data: bytes) -> None:
        self.total += len(data)
        self.digest.update(data)
        self.data = (self.data + data)[-self.keep:]
        if self.total > MAX_OUTPUT_BYTES:
            raise _OutputLimit

    def fields(self) -> dict[str, Any]:
        tail = self.data.decode("utf-8", "replace")
        for secret in self.secrets:
            tail = tail.replace(secret, "[redacted]")
        return {"output_tail": tail.replace("\0", "\ufffd")[-OUTPUT_TAIL_CHARS:], "output_bytes": self.total,
                "output_sha256": self.digest.hexdigest()}


def _error(kind: runs.ErrorKind) -> dict[str, Any]:
    return {"state": runs.CheckState.ERROR, "error_kind": kind}


async def _execute(check: Check, work_dir: Path, env: dict[str, str], prefix: tuple[str, ...],
                   timeout: float) -> dict[str, Any]:
    output = _Output()
    try:
        process = await proc_tree.spawn(*prefix, "/bin/sh", "-c", check.command, cwd=work_dir, env=env,
            stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    except OSError:
        kind = runs.ErrorKind.SANDBOX_UNAVAILABLE if prefix else runs.ErrorKind.COULD_NOT_START
        return {**_error(kind), **output.fields()}
    except RuntimeError:
        return {**_error(runs.ErrorKind.EVIDENCE_UNAVAILABLE), **output.fields()}
    tree = None
    result: dict[str, Any] = {}
    try:
        tree = proc_tree.track(process, label="acceptance check")
        tree.start_ticker()
        await tree.begin_end()
        async def capture():
            assert process.stdout is not None
            while data := await process.stdout.read(65536):
                output.consume(data)
            await process.wait()
        try:
            await asyncio.wait_for(capture(), max(0, timeout))
            result = {"state": runs.CheckState.PASSED if process.returncode == 0 else runs.CheckState.FAILED}
        except asyncio.TimeoutError:
            result = _error(runs.ErrorKind.TIMEOUT)
        except _OutputLimit:
            result = _error(runs.ErrorKind.OUTPUT_LIMIT)
    finally:
        if tree is not None:
            await tree.end()
        else:
            await proc_tree.end(process, label="acceptance check")
    return {**result, "exit_code": process.returncode, **output.fields()}


def _git(directory: Path, *args: str) -> bytes:
    return subprocess.run(["git", "-C", str(directory), *args], capture_output=True, check=True,
                          timeout=10, env=child_env.inherited()).stdout


def _protected(work_dir: Path, paths: tuple[str, ...], base_commit: str | None) -> list[str]:
    if not paths:
        return []
    if not base_commit:
        raise EvidenceUnavailable("step start commit unavailable")
    # Literal pathspecs prevent names beginning ':' from becoming git pathspec magic.
    specs = [f":(literal){path}" for path in paths]
    changed = _git(work_dir, "diff", "--relative", "--name-only", "-z", base_commit, "--", *specs)
    changed += _git(work_dir, "ls-files", "--others", "--exclude-standard", "-z", "--", *specs)
    return sorted({os.fsdecode(name) for name in changed.split(b"\0") if name})


def _prepare(work_dir: Path, backup: Path, runtime_paths: Sequence[str]):
    # Import the existing implementation at the boundary; no second clone/rollback protocol.
    from .tiers.worker import (_verification_baseline, _verification_checkout, _verification_snapshot,
                               _verification_write_paths)
    snapshot = _verification_snapshot(str(work_dir))
    if snapshot.get("error"):
        raise EvidenceUnavailable("source snapshot unavailable")
    if not snapshot["git"]:
        return work_dir, snapshot, None
    directory = Path(_verification_checkout(str(work_dir), backup, runtime_paths))
    before = _verification_baseline(str(directory), backup / "baseline")
    if before.get("error") or not before["git"]:
        raise EvidenceUnavailable("private snapshot unavailable")
    root, paths = _verification_write_paths(str(directory))
    monitor = VerificationWrites(root, paths)
    monitor.start()
    return directory, before, monitor


async def _thread(function, *args):
    """Finish filesystem preparation before cancellation can remove its private tree."""
    future = asyncio.create_task(asyncio.to_thread(function, *args))
    try:
        return await asyncio.shield(future)
    except asyncio.CancelledError:
        result = await future
        # A monitor created in a cancelled preparation has no caller to close it.
        if function is _prepare and result[2] is not None:
            result[2].close()
        raise


async def _one(*, task_id: str, step_id: str, check: Check, attempt: int, work_dir: Path,
               runtime_root: Path, remaining: float, mode: AcceptanceChecksMode,
               base_commit: str | None, runtime_paths: Sequence[str]) -> dict[str, Any]:
    started = time.monotonic()
    backup = runtime_root / ".local" / "acceptance" / uuid.uuid4().hex
    monitor = None
    before: dict[str, Any] = {}
    directory = work_dir
    outcome = _error(runs.ErrorKind.BUDGET)
    row = None
    finishing = False
    try:
        if remaining > 0:
            try:
                backup.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                directory, before, monitor = await _thread(_prepare, work_dir, backup, runtime_paths)
                outcome = {}
            except (OSError, ValueError, subprocess.SubprocessError, EvidenceUnavailable):
                outcome = _error(runs.ErrorKind.EVIDENCE_UNAVAILABLE)
        try:
            row = await runs.create_run(task_id, step_id, check.row(), attempt,
                head_sha=before.get("head"), content_sha=before.get("content_sha256"), guarded=monitor is not None)
        except Exception as exc:
            raise EvidenceUnavailable("cannot start evidence record") from exc
        if not outcome:
            env = _environment()
            prefix: tuple[str, ...] | None = ()
            if check.network and mode is not AcceptanceChecksMode.NETWORK:
                outcome = _error(runs.ErrorKind.COULD_NOT_START)
            elif not check.network:
                prefix = await local_prefix(directory, env)
            if prefix is None:
                outcome = _error(runs.ErrorKind.SANDBOX_UNAVAILABLE)
            elif not outcome:
                left = remaining - (time.monotonic() - started)
                outcome = (_error(runs.ErrorKind.BUDGET) if left <= 0 else
                           await _execute(check, directory, env, prefix, min(check.timeout_seconds, left)))
                if left < check.timeout_seconds and outcome.get("error_kind") is runs.ErrorKind.TIMEOUT:
                    outcome["error_kind"] = runs.ErrorKind.BUDGET
            written = []
            changed = False
            if monitor is not None:
                try:
                    written = monitor.finish()
                    head = (await _thread(_git, directory, "rev-parse", "--verify", "HEAD")).decode().strip()
                    index = await _thread(_git, directory, "diff", "--cached", "--binary", "--no-ext-diff")
                    changed = (written or head != before["head"] or
                               hashlib.sha256(index).hexdigest() != before["index_sha256"])
                except (OSError, subprocess.SubprocessError):
                    outcome = {**outcome, **_error(runs.ErrorKind.EVIDENCE_UNAVAILABLE)}
                else:
                    if changed:
                        outcome = {**outcome, **_error(runs.ErrorKind.CHANGED_CODE)}
            if outcome["state"] is not runs.CheckState.ERROR or changed:
                try:
                    protected = set(await _thread(_protected, directory, check.protect, base_commit))
                    protected.update(name for name in written
                                     if any(Path(name).is_relative_to(path) for path in check.protect))
                    outcome["protected_changed"] = sorted(protected)
                    if protected and outcome["state"] is not runs.CheckState.ERROR:
                        outcome["state"] = runs.CheckState.FAILED
                except (OSError, subprocess.SubprocessError, EvidenceUnavailable):
                    outcome = {**outcome, **_error(runs.ErrorKind.EVIDENCE_UNAVAILABLE)}
            if changed:
                from .tiers.worker import _restore_verification
                await _thread(_restore_verification, str(directory), backup / "baseline")
            if outcome["state"] is runs.CheckState.PASSED and isinstance(check, ArtifactCheck):
                try:
                    outcome["artifacts"] = await _thread(artifacts.collect, directory, check.artifact, runtime_root, row)
                except FileNotFoundError:
                    outcome["state"] = runs.CheckState.FAILED
                except artifacts.ArtifactInvalid:
                    outcome = {**outcome, **_error(runs.ErrorKind.ARTIFACT_INVALID)}
                except OSError:
                    outcome = {**outcome, **_error(runs.ErrorKind.EVIDENCE_UNAVAILABLE)}
        outcome["duration_ms"] = int((time.monotonic() - started) * 1000)
        finishing = True  # An uncertain terminal write must not be retried or reported as complete.
        try:
            finished = await runs.finish_run(row["id"], **outcome)
        except Exception as exc:
            raise EvidenceUnavailable("cannot finish evidence record") from exc
        if finished is None:
            raise EvidenceUnavailable("evidence record no longer running")
        logger.info("acceptance check %s %s exit=%s", check.id, finished["state"], finished["exit_code"])
        await activity.insert_activity(task_id=task_id, kind="acceptance_check", summary=(
            f"Check {check.id} {finished['state']}" + (f": {finished['error_kind']}" if finished["error_kind"] else
                                                     f" (exit {finished['exit_code']})")))
        return finished
    except asyncio.CancelledError:
        if row is not None:
            await runs.finish_run(row["id"], runs.CheckState.ERROR, error_kind=runs.ErrorKind.INTERRUPTED,
                                  duration_ms=int((time.monotonic() - started) * 1000))
        raise
    except Exception:
        if row is not None and not finishing:
            try:
                finished = await runs.finish_run(row["id"], runs.CheckState.ERROR,
                    error_kind=runs.ErrorKind.EVIDENCE_UNAVAILABLE,
                    duration_ms=int((time.monotonic() - started) * 1000))
            except Exception as exc:
                raise EvidenceUnavailable("cannot finish evidence record") from exc
            if finished is None:
                raise EvidenceUnavailable("evidence record no longer running")
        raise
    finally:
        if monitor is not None:
            monitor.close()
        await asyncio.to_thread(shutil.rmtree, backup, True)


async def run_step_checks(*, task_id: str, step_id: str, checks: Sequence[Check], mode: AcceptanceChecksMode,
                          work_dir: Path, attempt: int, runtime_root: Path, base_commit: str | None = None,
                          runtime_paths: Sequence[str] = ()) -> list[dict[str, Any]]:
    """Collect once per Worker attempt; after restart, resume only interrupted/missing checks.

    Neither this function nor its caller offers or removes Planner decisions.
    ``off`` returns before any DB read, filesystem work or child process.
    """
    if mode is AcceptanceChecksMode.OFF or not checks:
        return []
    semaphore, locks = _coordination()
    key = (task_id, step_id)
    lock = locks.setdefault(key, asyncio.Lock())
    async with lock:
        history = [r for r in await runs.list_runs(task_id, step_id) if r["attempt"] == attempt]
        spent = sum((r["duration_ms"] or 0) / 1000 for r in history)
        latest = {r["check_id"]: r for r in history}
        for check in checks:
            previous = latest.get(check.id)
            if previous is not None and previous["state"] == runs.CheckState.RUNNING.value:
                raise EvidenceUnavailable("previous evidence record is still running")
            if previous is not None and previous["error_kind"] != runs.ErrorKind.INTERRUPTED.value:
                continue
            async with semaphore:
                current = ProjectExecution.from_row(await projects.get_task_project(task_id)).acceptance_checks
                if current is AcceptanceChecksMode.OFF:
                    break  # Disabling the project prevents every subsequent launch, including queued ones.
                row = await _one(task_id=task_id, step_id=step_id, check=check, attempt=attempt,
                    work_dir=work_dir, runtime_root=runtime_root, remaining=max(0, STEP_BUDGET_SECONDS - spent),
                    mode=current, base_commit=base_commit, runtime_paths=runtime_paths)
                history.append(row)
                spent += (row["duration_ms"] or 0) / 1000
        return history


async def recover(runtime_root: Path) -> None:
    """Called after proc_tree's orphan sweep, before Planners resume their persisted decisions."""
    await runs.recover_interrupted()
    temporary = runtime_root / ".local" / "acceptance"
    if temporary.exists():
        await asyncio.to_thread(shutil.rmtree, temporary)
    evidence = runtime_root / "evidence"
    if evidence.exists():
        for directory in evidence.iterdir():
            if await tasks.get_task(directory.name) is None:
                await asyncio.to_thread(artifacts.remove_task, runtime_root, directory.name)
