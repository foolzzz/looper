"""Bound schedule history and runtime files without disturbing active command runs."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
import errno
import logging
import os
from pathlib import Path
import shutil
from typing import Any

from ..db import connection as db_connection, schedules as db_schedules, llm_requests
from . import scheduler_settings
from .command_runs import CommandRuns, Finished, Lost, LOGS_DIR, RUNNER_DIR, RUNS_DIR
from .llmrec import ROUTES_DIR, RouteFile
from .llmrec import settings as llmrec_settings

logger = logging.getLogger(__name__)

INTERVAL = timedelta(hours=1)
KEEP_LATEST_RUNS = 100
PRUNE_BATCH = 5000
LOG_TOTAL_MAX_BYTES = 2 * 1024**3
ROTATE_AT_BYTES = 20 * 1024**2
ROTATE_KEEP = 5
RUNNER_FILE_MAX_AGE = timedelta(days=31)
ROTATED = ("serve.log", "probe.log")
MIB = 1024**2
LLM_PRUNE_ROUNDS = 50


@dataclass
class Report:
    runs: int = 0
    notifications: int = 0
    logs: int = 0
    log_bytes: int = 0
    run_dirs: int = 0
    runner_files: int = 0
    llm_requests: int = 0
    llm_routes: int = 0
    llm_route_files: int = 0
    rotated: list[str] = field(default_factory=list)
    failed_steps: dict[str, str] = field(default_factory=dict)


@contextmanager
def _step(report: Report, name: str) -> Iterator[None]:
    try:
        yield
    except Exception as exc:
        cause = f"{type(exc).__name__}: {exc}"
        report.failed_steps[name] = cause
        logger.warning("housekeeping step %s failed: %s", name, cause)


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


def prune_route_files(directory: Path, now: datetime) -> int:
    deleted = 0
    for path in directory.glob("*.json"):
        try:
            stat = path.stat()
            route = RouteFile.read(path)
            if route is None:
                if stat.st_mtime < (now - timedelta(days=7)).timestamp():
                    logger.warning("housekeeping skipped unreadable llmrec route file %s", path.name)
                continue
            ended = datetime.fromisoformat(route.ended_at) if route.ended_at else None
            if ended is not None and (ended < now - timedelta(days=1)
                    or stat.st_mtime < (now - timedelta(days=7)).timestamp()):
                # RouteFile.write atomically replaces the file. Recheck its identity so a fresh
                # bind/release that happened during the read is kept for the next pass.
                current = path.stat()
                if (stat.st_ino, stat.st_mtime_ns) != (current.st_ino, current.st_mtime_ns):
                    continue
                path.unlink()
                deleted += 1
        except FileNotFoundError:
            pass
    return deleted


async def _prune_llm_requests(report: Report, now: datetime) -> None:
    async with db_connection.transaction() as conn:
        retention = await llmrec_settings.settings_view(conn)
    before = now - timedelta(days=retention["retention_days"]["value"])
    cap = retention["max_total_mb"]["value"] * MIB
    for prune, counter in ((lambda conn: llm_requests.prune_older(conn, before), "llm_requests"),
                           (llm_requests.prune_empty_routes, "llm_routes")):
        while True:
            async with db_connection.transaction() as conn:
                count = await prune(conn)
            setattr(report, counter, getattr(report, counter) + count)
            if count == 0:
                break
    async with db_connection.transaction() as conn:
        size = await llm_requests.stored_bytes(conn)
    if size <= cap:
        return
    # DELETE leaves reusable heap/TOAST/index pages allocated. Reclaim that space before
    # evicting retained requests, then compact committed batches to measure actual progress.
    await _compact_llm_requests()
    async with db_connection.transaction() as conn:
        size = await llm_requests.stored_bytes(conn)
    if size <= cap * 0.9:
        return
    for _ in range(LLM_PRUNE_ROUNDS):
        async with db_connection.transaction() as conn:
            requests = await llm_requests.prune_oldest(conn)
        report.llm_requests += requests
        removed_routes = 0
        while True:
            async with db_connection.transaction() as conn:
                routes = await llm_requests.prune_empty_routes(conn)
            report.llm_routes += routes
            removed_routes += routes
            if routes == 0:
                break
        if requests or removed_routes:
            await _compact_llm_requests()
        async with db_connection.transaction() as conn:
            size = await llm_requests.stored_bytes(conn)
        if size <= cap * 0.9:
            return
    logger.warning("housekeeping llm requests still above target after %d rounds: stored_bytes=%d cap_bytes=%d; "
                   "live routes or allocated PostgreSQL pages may retain space", LLM_PRUNE_ROUNDS, size, cap)


async def _compact_llm_requests() -> None:
    # VACUUM FULL needs a non-transaction connection and an exclusive table lock. The
    # recorder queues asynchronously; bound both lock waits and execution during pressure.
    async with db_connection.get_pool().acquire() as conn:
        await conn.execute("SET lock_timeout = '1s'")
        await conn.execute("SET statement_timeout = '30s'")
        await conn.execute("VACUUM (FULL) llm_requests, llm_request_parts, llm_request_routes")


async def run_once(runtime_root: Path, now: datetime, *, current_runner: Path | None,
                   commands: CommandRuns | None) -> Report:
    report = Report()
    before = None
    with _step(report, "retention"):
        async with db_connection.transaction() as conn:
            days = await scheduler_settings.run_retention_days(conn)
        before = now - timedelta(days=days)
    if before is not None:
        # Release the transaction between batches so a large backlog does not hold locks for the whole pass.
        for prune, counter in ((db_schedules.prune_runs, "runs"), (db_schedules.prune_notifications, "notifications")):
            with _step(report, counter):
                while True:
                    async with db_connection.transaction() as conn:
                        count = await prune(conn, before)
                    setattr(report, counter, getattr(report, counter) + count)
                    if count == 0:
                        break

    with _step(report, "llm requests"):
        await _prune_llm_requests(report, now)
    with _step(report, "llm route files"):
        report.llm_route_files = await asyncio.to_thread(prune_route_files, runtime_root / ROUTES_DIR, now)

    with _step(report, "command logs"):
        # A run commits its open row before creating its log. Snapshot files first so runs starting
        # after the open-row query cannot lose a new log to the size cap.
        logs = await asyncio.to_thread(_log_files, runtime_root / LOGS_DIR)
        async with db_connection.transaction() as conn:
            before = now - timedelta(days=await scheduler_settings.run_retention_days(conn))
            paths = await db_schedules.open_log_paths(conn)
        keep = {runtime_root / path for path in paths}
        report.logs, report.log_bytes = await asyncio.to_thread(
            _prune_logs, runtime_root / LOGS_DIR, logs, before, LOG_TOTAL_MAX_BYTES, keep)
    with _step(report, "run directories"):
        if commands is not None:
            runs = await asyncio.to_thread(_run_directories, runtime_root, now)
            async with db_connection.transaction() as conn:
                states = await db_schedules.run_states(conn, [run["id"] for run in runs])
            closed = [run for run in runs if states.get(run["id"]) not in db_schedules.OPEN_STATUSES]
            report.run_dirs = await asyncio.to_thread(_prune_run_directories, commands, closed, now)
    with _step(report, "runner files"):
        report.runner_files = await asyncio.to_thread(prune_runner_files, runtime_root / RUNNER_DIR, current_runner, now)
    for name in ROTATED:
        with _step(report, f"log rotation {name}"):
            if await asyncio.to_thread(rotate, runtime_root / "logs" / name, at_bytes=ROTATE_AT_BYTES, keep=ROTATE_KEEP):
                report.rotated.append(name)
    logger.info("housekeeping runs_deleted=%d notifications_deleted=%d logs_deleted=%d log_bytes=%d "
                "run_dirs=%d runner_files=%d llm_requests=%d llm_routes=%d llm_route_files=%d rotated=%s failed_steps=%s",
                report.runs, report.notifications, report.logs, report.log_bytes, report.run_dirs, report.runner_files,
                report.llm_requests, report.llm_routes, report.llm_route_files, ",".join(report.rotated), report.failed_steps)
    return report
