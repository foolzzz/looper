"""The one-time import of the state files kept before PostgreSQL held all agent state.

Each task's Planner kept its plan in
``<runtime_root>/.local/planner/planner-<task_id>/<plan_id>.json`` and the
Manager its Claude session in ``.local/manager/session.json``. At server start
``import_file_state`` copies them into ``task_plans``, ``task_steps`` and
``agent_sessions``, once per database (recorded in ``schema_migrations``).
Rows the database already has win. Plan files from before the resident
Planner (no ``session_id``; they have keys like ``project_root`` and steps
with a ``complexity``) are skipped. The files are left in place; nothing reads
them any more, so they can be removed.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import asyncpg

from ..db import plans as db_plans
from ..db.connection import get_pool
from ..db.tasks import planner_agent_id
from .run_usage import Usage

logger = logging.getLogger(__name__)

MIGRATION = "file_state_import"


@dataclass
class ImportReport:
    """What one import did; ``done_before`` when the database had it already."""

    done_before: bool = False
    plans: int = 0
    steps: int = 0
    sessions: int = 0
    skipped_old_format: int = 0
    skipped_no_task: int = 0
    skipped_in_database: int = 0
    skipped_superseded: int = 0  # an older plan file of a task with a newer one
    skipped_unreadable: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _read(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        return None


def _is_current(data: Any) -> bool:
    """Whether a plan file is the resident Planner's: it names its session and labels its steps."""
    return (isinstance(data, dict) and "session_id" in data and isinstance(data.get("plan_id"), str)
            and isinstance(data.get("steps"), list) and all(isinstance(s, dict) and "step_id" in s
                                                            for s in data["steps"]))


def _strings(value: Any) -> list[str]:
    return [str(item) for item in value] if isinstance(value, list) else []


def _step(saved: dict[str, Any], result: Any) -> dict[str, Any]:
    """A plan file's step as ``plans.write`` takes it; the file never held the result, so the row's stays."""
    return {
        "id": str(saved["step_id"]),
        "index": int(saved.get("index") or 0),
        "key": str(saved.get("key") or ""),
        "instruction": str(saved.get("instruction") or ""),
        "target_files": _strings(saved.get("target_files")),
        "acceptance_criteria": _strings(saved.get("acceptance_criteria")),
        "depends_on": _strings(saved.get("depends_on")),
        "review": bool(saved.get("review")),
        "status": str(saved.get("status") or "pending"),
        "result": result,
        "worker_id": saved.get("worker_id"),
        "reviewer_id": saved.get("reviewer_id"),
        "retries": int(saved.get("retries") or 0),
        "feedback": _strings(saved.get("feedback")),
    }


async def _save_session(conn: asyncpg.Connection, agent_id: str, data: dict[str, Any]) -> bool:
    session_id = data.get("session_id")
    if session_id is not None and not isinstance(session_id, str):
        return False
    done = await conn.execute(
        "INSERT INTO agent_sessions (agent_id, session_id, session_totals) VALUES ($1, $2, $3::jsonb) "
        "ON CONFLICT (agent_id) DO NOTHING",
        agent_id, session_id, json.dumps(Usage.from_dict(data.get("session_totals")).to_dict()),
    )
    return done == "INSERT 0 1"


async def _import_plan(conn: asyncpg.Connection, task_id: str, path: Path, data: dict[str, Any],
                       report: ImportReport) -> None:
    if await conn.fetchval("SELECT 1 FROM task_plans WHERE task_id = $1", task_id):
        report.skipped_in_database += 1
        return
    if data.get("status") not in db_plans.STATUSES:
        logger.warning("state import: skipping %s: unknown plan status %r", path, data.get("status"))
        report.skipped_unreadable += 1
        return
    results = {row["id"]: row["result"] for row in await conn.fetch(
        "SELECT id, result FROM task_steps WHERE task_id = $1", task_id)}
    steps = [_step(s, json.loads(results[s["step_id"]]) if isinstance(results.get(s["step_id"]), str) else None)
             for s in data["steps"]]
    await db_plans.write(conn, task_id, plan={
        "plan_id": data["plan_id"],
        "status": data["status"],
        "correlation_id": str(data.get("correlation_id") or ""),
        "origin_channel_id": str(data.get("origin_channel_id") or ""),
        "final_summary": str(data.get("final_summary") or ""),
        "fail_reason": str(data.get("fail_reason") or ""),
    }, steps=steps)
    report.plans += 1
    report.steps += len(steps)
    if await _save_session(conn, planner_agent_id(task_id), data):
        report.sessions += 1


async def import_file_state(runtime_root: Path, *, pool: asyncpg.Pool | None = None) -> ImportReport:
    """Copy the plan and session files under ``runtime_root`` into the database, unless it has them already."""
    pool = pool or get_pool()
    report = ImportReport()
    async with pool.acquire() as conn, conn.transaction():
        # Another server starting on the same database waits here, then finds the import done.
        await conn.execute("LOCK TABLE schema_migrations IN SHARE ROW EXCLUSIVE MODE")
        if await conn.fetchval("SELECT 1 FROM schema_migrations WHERE name = $1", MIGRATION):
            return ImportReport(done_before=True)

        # A task's newest plan file in the current format is its plan.
        newest: dict[str, tuple[float, Path, dict[str, Any]]] = {}
        for path in sorted((runtime_root / ".local" / "planner").glob("planner-*/*.json")):
            task_id = path.parent.name.removeprefix("planner-")
            data = _read(path)
            if data is None:
                logger.warning("state import: skipping %s: not readable JSON", path)
                report.skipped_unreadable += 1
            elif not _is_current(data):
                logger.info("state import: skipping %s: a plan file from before the resident Planner", path)
                report.skipped_old_format += 1
            elif not await conn.fetchval("SELECT 1 FROM tasks WHERE id = $1", task_id):
                logger.info("state import: skipping %s: there is no task %s", path, task_id)
                report.skipped_no_task += 1
            else:
                mtime = path.stat().st_mtime
                if task_id in newest:
                    report.skipped_superseded += 1
                if task_id not in newest or mtime > newest[task_id][0]:
                    newest[task_id] = (mtime, path, data)
        for task_id, (_, path, data) in sorted(newest.items()):
            await _import_plan(conn, task_id, path, data, report)

        manager = runtime_root / ".local" / "manager" / "session.json"
        if manager.is_file():
            data = _read(manager)
            if isinstance(data, dict) and await _save_session(conn, "manager", data):
                report.sessions += 1
            elif not isinstance(data, dict):
                logger.warning("state import: skipping %s: not readable JSON", manager)
                report.skipped_unreadable += 1
        await conn.execute("INSERT INTO schema_migrations (name) VALUES ($1)", MIGRATION)
    logger.info("state import: %s", ", ".join(f"{k} {v}" for k, v in report.to_dict().items() if k != "done_before"))
    return report
