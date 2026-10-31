"""Bound schedule history and runtime files without disturbing active command runs."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta
import errno
import logging
import os
from pathlib import Path
import shutil
from typing import Any

from ..db import connection as db_connection, schedules as db_schedules
from . import scheduler_settings
from .command_runs import CommandRuns, Finished, Lost, LOGS_DIR, RUNNER_DIR, RUNS_DIR

logger = logging.getLogger(__name__)

INTERVAL = timedelta(hours=1)
KEEP_LATEST_RUNS = 100
PRUNE_BATCH = 5000
LOG_TOTAL_MAX_BYTES = 2 * 1024**3
ROTATE_AT_BYTES = 20 * 1024**2
ROTATE_KEEP = 5
RUNNER_FILE_MAX_AGE = timedelta(days=31)
ROTATED = ("serve.log", "probe.log")


@dataclass
class Report:
    runs: int = 0
    notifications: int = 0
    logs: int = 0
    log_bytes: int = 0
    run_dirs: int = 0
    runner_files: int = 0
    rotated: list[str] = field(default_factory=list)


def _log_files(logs_root: Path) -> list[tuple[float, Path, int]]:
    files = []
    for path in logs_root.glob("*/*.log"):
        try:
            stat = path.stat()
            files.append((stat.st_mtime, path, stat.st_size))
        except FileNotFoundError:
            pass
    return files


def prune_logs(logs_root: Path, before: datetime, total_max: int, keep: set[Path]) -> tuple[int, int]:
    """Expire logs, then evict the oldest until under the cap; active logs count but stay."""
    return _prune_logs(logs_root, _log_files(logs_root), before, total_max, keep)


def _prune_logs(logs_root: Path, files: list[tuple[float, Path, int]], before: datetime,
                total_max: int, keep: set[Path]) -> tuple[int, int]:
    keep = {path.resolve() for path in keep}
    remaining = []
    deleted = deleted_bytes = 0
    for mtime, path, size in files:
        try:
            protected = path.resolve() in keep
            if mtime < before.timestamp() and not protected:
                path.unlink()
                deleted += 1
                deleted_bytes += size
            else:
                remaining.append((mtime, path, size, protected))
        except FileNotFoundError:
            continue
    total = sum(size for _, _, size, _ in remaining)
    for _, path, size, protected in sorted(remaining):
        if total <= total_max:
            break
        if protected:
            continue
        try:
            path.unlink()
        except FileNotFoundError:
            total -= size
            continue
        deleted += 1
        deleted_bytes += size
        total -= size
    for directory in logs_root.glob("*"):
        if directory.is_dir():
            try:
                directory.rmdir()
            except FileNotFoundError:
                pass
            except OSError as exc:
                if exc.errno not in (errno.ENOTEMPTY, errno.EEXIST):
                    raise
    return deleted, deleted_bytes


def prune_runner_files(runner_dir: Path, current: Path | None, now: datetime) -> int:
    current = current.resolve() if current is not None else None
    before = (now - RUNNER_FILE_MAX_AGE).timestamp()
    deleted = 0
    for path in runner_dir.glob("runner-*.py"):
        try:
            if path.resolve() != current and path.stat().st_mtime < before:
                path.unlink()
                deleted += 1
        except FileNotFoundError:
            pass
    return deleted


def rotate(path: Path, *, at_bytes: int, keep: int) -> bool:
    """Copy and truncate: service managers keep appending through the original inode."""
    try:
        if path.stat().st_size < at_bytes:
            return False
        for number in range(keep - 1, 0, -1):
            try:
                os.replace(f"{path}.{number}", f"{path}.{number + 1}")
            except FileNotFoundError:
                pass
        shutil.copyfile(path, f"{path}.1")
        os.truncate(path, 0)
    except FileNotFoundError:
        return False
    return True


def _run_directories(runtime_root: Path, now: datetime) -> list[dict[str, Any]]:
    runs = []
    for path in (runtime_root / RUNS_DIR).glob("*"):
        try:
            if path.is_dir():
                runs.append({"id": path.name, "created_at": datetime.fromtimestamp(path.stat().st_mtime, now.tzinfo)})
        except FileNotFoundError:
            pass
    return runs


def _prune_run_directories(commands: CommandRuns, runs: list[dict[str, Any]], now: datetime) -> int:
    deleted = 0
    for run_id, state in commands.inspect(runs, now, strict=True).items():
        if isinstance(state, (Finished, Lost)):
            try:
                shutil.rmtree(commands.root / RUNS_DIR / run_id)
                deleted += 1
            except FileNotFoundError:
                pass
    return deleted


async def run_once(runtime_root: Path, now: datetime, *, current_runner: Path | None,
                   commands: CommandRuns | None) -> Report:
    report = Report()
    async with db_connection.transaction() as conn:
        before = now - timedelta(days=await scheduler_settings.run_retention_days(conn))
    # Release the transaction between batches so a large backlog does not hold locks for the whole pass.
    for prune, counter in ((db_schedules.prune_runs, "runs"), (db_schedules.prune_notifications, "notifications")):
        while True:
            async with db_connection.transaction() as conn:
                count = await prune(conn, before)
            setattr(report, counter, getattr(report, counter) + count)
            if count == 0:
                break

    runs = await asyncio.to_thread(_run_directories, runtime_root, now) if commands is not None else []
    # A run commits its open row before creating its log. Snapshot files first so runs starting
    # after the open-row query cannot lose a new log to the size cap.
    logs = await asyncio.to_thread(_log_files, runtime_root / LOGS_DIR)
    async with db_connection.transaction() as conn:
        states = await db_schedules.run_states(conn, [run["id"] for run in runs])
        paths = await db_schedules.open_log_paths(conn)
    keep = {runtime_root / path for path in paths}
    report.logs, report.log_bytes = await asyncio.to_thread(
        _prune_logs, runtime_root / LOGS_DIR, logs, before, LOG_TOTAL_MAX_BYTES, keep)
    if commands is not None:
        closed = [run for run in runs if states.get(run["id"]) not in db_schedules.OPEN_STATUSES]
        report.run_dirs = await asyncio.to_thread(_prune_run_directories, commands, closed, now)
    report.runner_files = await asyncio.to_thread(prune_runner_files, runtime_root / RUNNER_DIR, current_runner, now)
    for name in ROTATED:
        if await asyncio.to_thread(rotate, runtime_root / "logs" / name, at_bytes=ROTATE_AT_BYTES, keep=ROTATE_KEEP):
            report.rotated.append(name)
    logger.info("housekeeping runs_deleted=%d notifications_deleted=%d logs_deleted=%d log_bytes=%d "
                "run_dirs=%d runner_files=%d rotated=%s", report.runs, report.notifications, report.logs,
                report.log_bytes, report.run_dirs, report.runner_files, ",".join(report.rotated))
    return report
