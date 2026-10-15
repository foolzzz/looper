"""Schedules and their runs (``schedules``, ``schedule_profile_overrides``, ``schedule_runs``).

A schedule belongs to a project; its name is unique there ignoring case (a
taken one raises ``asyncpg.UniqueViolationError``). The scheduler
(``looper/agents/scheduler.py``) locks a schedule's row for each firing, so two
servers on one database never fire it twice. A run's final status is written
once; every time comes from the caller, so tests can set the clock.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from datetime import datetime
from typing import Any

import asyncpg

from .connection import get_pool

# The schedule fields a request sets (looper/agents/schedules.py validates them).
FIELDS = ("name", "cron", "timezone", "requirement", "work_dir", "overlap", "catch_up", "run_timeout_seconds",
          "run_budget_usd")

QUEUED = "queued"
RUNNING = "running"
OPEN_STATUSES = (QUEUED, RUNNING)

_RUN = """
    SELECT r.*, t.status AS task_status FROM schedule_runs r LEFT JOIN tasks t ON t.id = r.task_id
"""

Db = asyncpg.Pool | asyncpg.Connection


def new_run_id() -> str:
    return f"srun-{uuid.uuid4().hex[:12]}"


async def _overrides(db: Db, schedule_ids: list[str]) -> dict[str, dict[str, str]]:
    rows = await db.fetch(
        "SELECT schedule_id, role, profile_id FROM schedule_profile_overrides WHERE schedule_id = ANY($1::text[]) "
        "ORDER BY role", schedule_ids,
    )
    overrides: dict[str, dict[str, str]] = {schedule_id: {} for schedule_id in schedule_ids}
    for row in rows:
        overrides[row["schedule_id"]][row["role"]] = row["profile_id"]
    return overrides


async def _with_overrides(db: Db, rows: list[asyncpg.Record]) -> list[dict[str, Any]]:
    overrides = await _overrides(db, [row["id"] for row in rows])
    return [{**dict(row), "profile_overrides": overrides[row["id"]]} for row in rows]


async def get_schedule(schedule_id: str, *, pool: Db | None = None) -> dict[str, Any] | None:
    """One schedule with its ``profile_overrides`` (role -> profile id)."""
    db = pool or get_pool()
    row = await db.fetchrow("SELECT * FROM schedules WHERE id = $1", schedule_id)
    return (await _with_overrides(db, [row]))[0] if row else None


async def list_schedules(project_id: str, *, pool: Db | None = None) -> list[dict[str, Any]]:
    """A project's schedules by name ignoring case, each with its ``profile_overrides``."""
    db = pool or get_pool()
    rows = await db.fetch("SELECT * FROM schedules WHERE project_id = $1 ORDER BY lower(name), id", project_id)
    return await _with_overrides(db, rows)


async def _set_overrides(conn: asyncpg.Connection, schedule_id: str, profiles: Mapping[str, str]) -> None:
    await conn.execute("DELETE FROM schedule_profile_overrides WHERE schedule_id = $1", schedule_id)
    await conn.executemany(
        "INSERT INTO schedule_profile_overrides (schedule_id, role, profile_id) VALUES ($1, $2, $3)",
        [(schedule_id, role, profile_id) for role, profile_id in profiles.items()],
    )


async def create_schedule(conn: asyncpg.Connection, *, project_id: str, fields: Mapping[str, Any],
                          profiles: Mapping[str, str], created_via: str = "dashboard") -> str:
    """A new schedule, disabled; its id."""
    schedule_id = f"sched-{uuid.uuid4().hex[:12]}"
    columns = [name for name in FIELDS if name in fields]
    values = [fields[name] for name in columns]
    placeholders = ", ".join(f"${i}" for i in range(4, len(columns) + 4))
    await conn.execute(
        f"INSERT INTO schedules (id, project_id, created_via, {', '.join(columns)}) VALUES ($1, $2, $3, "
        f"{placeholders})",
        schedule_id, project_id, created_via, *values,
    )
    await _set_overrides(conn, schedule_id, profiles)
    return schedule_id


async def update_schedule(conn: asyncpg.Connection, schedule_id: str, fields: Mapping[str, Any], *,
                          profiles: Mapping[str, str] | None = None) -> None:
    """Set ``fields`` (any of ``FIELDS``, ``enabled`` and ``next_fire_at``), and the profile overrides when given."""
    columns = [name for name in (*FIELDS, "enabled", "next_fire_at") if name in fields]
    sets = [f"{name} = ${i}" for i, name in enumerate(columns, start=2)]
    await conn.execute(
        f"UPDATE schedules SET {', '.join([*sets, 'updated_at = now()'])} WHERE id = $1",
        schedule_id, *[fields[name] for name in columns],
    )
    if profiles is not None:
        await _set_overrides(conn, schedule_id, profiles)


async def delete_schedule(schedule_id: str, *, pool: Db | None = None) -> bool:
    """Delete a schedule and its runs; its tasks stay, without it."""
    db = pool or get_pool()
    return await db.execute("DELETE FROM schedules WHERE id = $1", schedule_id) == "DELETE 1"


async def lock_schedule(conn: asyncpg.Connection, schedule_id: str) -> dict[str, Any] | None:
    """The schedule, its row locked until the transaction ends."""
    row = await conn.fetchrow("SELECT * FROM schedules WHERE id = $1 FOR UPDATE", schedule_id)
    return (await _with_overrides(conn, [row]))[0] if row else None


async def lock_next_due(conn: asyncpg.Connection, now: datetime, *, skip: list[str]) -> dict[str, Any] | None:
    """An enabled schedule due at ``now`` (not one in ``skip``), its row locked; another transaction's locked rows
    are passed over, so a second scheduler never handles the same firing."""
    row = await conn.fetchrow(
        """
        SELECT * FROM schedules
        WHERE enabled AND next_fire_at <= $1 AND NOT (id = ANY($2::text[]))
        ORDER BY next_fire_at, id LIMIT 1 FOR UPDATE SKIP LOCKED
        """,
        now, skip,
    )
    return (await _with_overrides(conn, [row]))[0] if row else None


async def set_fired(conn: asyncpg.Connection, schedule_id: str, *, last_fired_at: datetime,
                    next_fire_at: datetime | None) -> None:
    await conn.execute(
        "UPDATE schedules SET last_fired_at = $2, next_fire_at = $3, updated_at = now() WHERE id = $1",
        schedule_id, last_fired_at, next_fire_at,
    )


async def open_runs(conn: Db, schedule_id: str) -> list[dict[str, Any]]:
    """The schedule's queued and running runs, oldest first, each with its task's status."""
    rows = await conn.fetch(
        f"{_RUN} WHERE r.schedule_id = $1 AND r.status = ANY($2::text[]) ORDER BY r.created_at, r.id",
        schedule_id, list(OPEN_STATUSES),
    )
    return [dict(row) for row in rows]


async def insert_run(conn: Db, *, schedule_id: str, scheduled_for: datetime, trigger: str, status: str,
                     missed: int = 0, fired_at: datetime | None = None, task_id: str | None = None,
                     error: str | None = None, finished_at: datetime | None = None,
                     created_at: datetime | None = None) -> str:
    run_id = new_run_id()
    await conn.execute(
        """
        INSERT INTO schedule_runs (id, schedule_id, scheduled_for, fired_at, trigger, missed, status, task_id,
                                   error, finished_at, created_at)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, COALESCE($11, now()))
        """,
        run_id, schedule_id, scheduled_for, fired_at, trigger, missed, status, task_id, error, finished_at,
        created_at,
    )
    return run_id


async def start_queued_run(conn: Db, run_id: str, *, task_id: str | None, fired_at: datetime,
                           error: str | None = None) -> None:
    """A queued run fires now: running with its task, or failed with ``error`` when it has none."""
    await conn.execute(
        "UPDATE schedule_runs SET status = $2, task_id = $3, fired_at = $4, error = $5, "
        "finished_at = CASE WHEN $3::text IS NULL THEN $4::timestamptz END WHERE id = $1",
        run_id, RUNNING if task_id else "failed", task_id, fired_at, error,
    )


async def get_run(run_id: str, *, pool: Db | None = None) -> dict[str, Any] | None:
    db = pool or get_pool()
    row = await db.fetchrow(f"{_RUN} WHERE r.id = $1", run_id)
    return dict(row) if row else None


async def list_runs(schedule_id: str, *, limit: int, pool: Db | None = None) -> list[dict[str, Any]]:
    """The schedule's runs, newest first, each with its task's status."""
    db = pool or get_pool()
    rows = await db.fetch(f"{_RUN} WHERE r.schedule_id = $1 ORDER BY r.created_at DESC, r.id DESC LIMIT $2",
                          schedule_id, limit)
    return [dict(row) for row in rows]


async def latest_runs(schedule_ids: list[str], *, pool: Db | None = None) -> dict[str, dict[str, Any]]:
    """Each schedule's newest run, by schedule id (a schedule without runs is left out)."""
    db = pool or get_pool()
    rows = await db.fetch(
        """
        SELECT DISTINCT ON (r.schedule_id) r.*, t.status AS task_status
        FROM schedule_runs r LEFT JOIN tasks t ON t.id = r.task_id
        WHERE r.schedule_id = ANY($1::text[]) ORDER BY r.schedule_id, r.created_at DESC, r.id DESC
        """,
        schedule_ids,
    )
    return {row["schedule_id"]: dict(row) for row in rows}


async def finish_run(conn: Db, run_id: str, *, status: str, at: datetime, summary: str | None = None,
                     cost_usd: float | None = None, error: str | None = None) -> bool:
    """Write a running run's final status, once, and the schedule's counters with it: a completed run clears
    ``consecutive_failures`` and sets ``last_success_at``, a failed or timed-out one adds a failure. False when
    the run is not running (it was finished already)."""
    schedule_id = await conn.fetchval(
        """
        UPDATE schedule_runs SET status = $2, summary = $3, cost_usd = $4, error = $5, finished_at = $6
        WHERE id = $1 AND status = 'running' RETURNING schedule_id
        """,
        run_id, status, summary, cost_usd, error, at,
    )
    if schedule_id is None:
        return False
    if status == "completed":
        await conn.execute("UPDATE schedules SET consecutive_failures = 0, last_success_at = $2 WHERE id = $1",
                           schedule_id, at)
    elif status in ("failed", "timed_out"):
        await conn.execute("UPDATE schedules SET consecutive_failures = consecutive_failures + 1 WHERE id = $1",
                           schedule_id)
    return True


async def running_run_of_task(task_id: str, *, pool: Db | None = None) -> dict[str, Any] | None:
    """The running run whose task is ``task_id``."""
    db = pool or get_pool()
    row = await db.fetchrow(f"{_RUN} WHERE r.task_id = $1 AND r.status = 'running'", task_id)
    return dict(row) if row else None


async def running_runs(*, pool: Db | None = None) -> list[dict[str, Any]]:
    """Every running run with its task's status (NULL when the task was deleted), ``run_timeout_seconds`` and
    ``project_id``."""
    db = pool or get_pool()
    rows = await db.fetch(
        """
        SELECT r.*, t.status AS task_status, t.result_summary, t.error AS task_error, s.run_timeout_seconds,
               s.project_id
        FROM schedule_runs r JOIN schedules s ON s.id = r.schedule_id LEFT JOIN tasks t ON t.id = r.task_id
        WHERE r.status = 'running'
        """,
    )
    return [dict(row) for row in rows]


async def schedules_with_queued_runs(*, pool: Db | None = None) -> list[str]:
    db = pool or get_pool()
    rows = await db.fetch("SELECT DISTINCT schedule_id FROM schedule_runs WHERE status = 'queued'")
    return [row["schedule_id"] for row in rows]


async def recent_runs(conn: Db, schedule_id: str, *, limit: int) -> list[dict[str, Any]]:
    """The schedule's newest runs that have ended or were skipped, newest first, for the next run's context."""
    rows = await conn.fetch(
        f"{_RUN} WHERE r.schedule_id = $1 AND NOT (r.status = ANY($2::text[])) "
        "ORDER BY r.created_at DESC, r.id DESC LIMIT $3",
        schedule_id, list(OPEN_STATUSES), limit,
    )
    return [dict(row) for row in rows]
