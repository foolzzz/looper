"""Schedules and their runs (``schedules``, ``schedule_profile_overrides``, ``schedule_task_limits``,
``schedule_runs``).

A schedule belongs to a project; its name is unique there ignoring case (a
taken one raises ``asyncpg.UniqueViolationError``). The scheduler
(``looper/agents/scheduler.py``) locks a schedule's row for each firing, so two
servers on one database never fire it twice. A run's final status is written
once; every time comes from the caller, so tests can set the clock.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

import asyncpg

from .connection import get_pool
from .llm_records import TASK_RUN

# The schedule fields a request sets (looper/agents/schedules.py validates them).
FIELDS = ("name", "cron", "timezone", "requirement", "work_dir", "overlap", "max_parallel_runs", "catch_up",
          "run_timeout_seconds", "run_budget_usd", "gate_command", "gate_timeout_seconds", "daily_budget_usd", "notify",
          "notify_channel", "notify_chat")

QUEUED = "queued"
GATING = "gating"
RUNNING = "running"
OPEN_STATUSES = (QUEUED, GATING, RUNNING)
# The final statuses that count as a failure.
FAILURES = ("failed", "timed_out", "gate_failed")
# The final statuses of a run that went well: its task completed, or its gate found nothing to do. Either ends a
# failure streak and is the schedule's latest ok run (``last_ok_at``); only a completed one is a success.
OK = ("completed", "skipped_gate")
# Runs that ran nothing: they neither carry an unfinished input nor hide one.
NOTHING_RAN = ("skipped_overlap", "skipped_missed", "skipped_budget")
# Firings that created no task; one that repeats the schedule's latest run is counted on it (``count_skip``).
SKIPPED = ("skipped_gate", *NOTHING_RAN)
# A run whose input was not done: the next run is given its gate output.
UNFINISHED = ("failed", "timed_out", "gate_failed", "stopped", "cancelled")

_RUN = """
    SELECT r.*, t.status AS task_status, t.title AS task_title FROM schedule_runs r LEFT JOIN tasks t ON t.id = r.task_id
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


async def _task_limits(db: Db, schedule_ids: list[str]) -> dict[str, dict[str, int]]:
    rows = await db.fetch(
        "SELECT schedule_id, role, max_concurrency FROM schedule_task_limits WHERE schedule_id = ANY($1::text[]) "
        "ORDER BY role", schedule_ids,
    )
    limits: dict[str, dict[str, int]] = {schedule_id: {} for schedule_id in schedule_ids}
    for row in rows:
        limits[row["schedule_id"]][row["role"]] = row["max_concurrency"]
    return limits


async def _with_overrides(db: Db, rows: list[asyncpg.Record]) -> list[dict[str, Any]]:
    ids = [row["id"] for row in rows]
    overrides, limits = await _overrides(db, ids), await _task_limits(db, ids)
    return [{**dict(row), "profile_overrides": overrides[row["id"]], "task_limits": limits[row["id"]]}
            for row in rows]


# A schedule's row with its project's name.
_WITH_PROJECT = "SELECT s.*, p.name AS project_name FROM schedules s JOIN projects p ON p.id = s.project_id"


async def get_schedule(schedule_id: str, *, pool: Db | None = None) -> dict[str, Any] | None:
    """One schedule with its ``project_name``, ``profile_overrides`` (role -> profile id) and ``task_limits`` (role ->
    how many of its tasks' Workers or Reviewers run at once; the roles it sets)."""
    db = pool or get_pool()
    row = await db.fetchrow(f"{_WITH_PROJECT} WHERE s.id = $1", schedule_id)
    return (await _with_overrides(db, [row]))[0] if row else None


async def list_schedules(project_id: str | None = None, *, pool: Db | None = None) -> list[dict[str, Any]]:
    """A project's schedules (every project's without ``project_id``, by project name) by name ignoring case, each
    with its ``project_name``, ``profile_overrides`` and ``task_limits``."""
    db = pool or get_pool()
    rows = await db.fetch(
        f"{_WITH_PROJECT} WHERE $1::text IS NULL OR s.project_id = $1 "
        "ORDER BY lower(p.name), p.id, lower(s.name), s.id",
        project_id,
    )
    return await _with_overrides(db, rows)


async def _set_overrides(conn: asyncpg.Connection, schedule_id: str, profiles: Mapping[str, str]) -> None:
    await conn.execute("DELETE FROM schedule_profile_overrides WHERE schedule_id = $1", schedule_id)
    await conn.executemany(
        "INSERT INTO schedule_profile_overrides (schedule_id, role, profile_id) VALUES ($1, $2, $3)",
        [(schedule_id, role, profile_id) for role, profile_id in profiles.items()],
    )


async def _set_task_limits(conn: asyncpg.Connection, schedule_id: str, limits: Mapping[str, int]) -> None:
    await conn.execute("DELETE FROM schedule_task_limits WHERE schedule_id = $1", schedule_id)
    await conn.executemany(
        "INSERT INTO schedule_task_limits (schedule_id, role, max_concurrency) VALUES ($1, $2, $3)",
        [(schedule_id, role, limit) for role, limit in limits.items()],
    )


async def create_schedule(conn: asyncpg.Connection, *, project_id: str, fields: Mapping[str, Any],
                          profiles: Mapping[str, str], task_limits: Mapping[str, int] | None = None,
                          created_via: str = "dashboard") -> str:
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
    await _set_task_limits(conn, schedule_id, task_limits or {})
    return schedule_id


async def update_schedule(conn: asyncpg.Connection, schedule_id: str, fields: Mapping[str, Any], *,
                          profiles: Mapping[str, str] | None = None,
                          task_limits: Mapping[str, int] | None = None) -> None:
    """Set ``fields`` (any of ``FIELDS``, ``enabled``, ``enabled_at``, ``rule_changed_at`` and ``next_fire_at``),
    and the profile overrides and the task limits when given (each replaces what the schedule had)."""
    columns = [name for name in (*FIELDS, "enabled", "enabled_at", "rule_changed_at", "next_fire_at") if name in fields]
    sets = [f"{name} = ${i}" for i, name in enumerate(columns, start=2)]
    await conn.execute(
        f"UPDATE schedules SET {', '.join([*sets, 'updated_at = now()'])} WHERE id = $1",
        schedule_id, *[fields[name] for name in columns],
    )
    if profiles is not None:
        await _set_overrides(conn, schedule_id, profiles)
    if task_limits is not None:
        await _set_task_limits(conn, schedule_id, task_limits)


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
    are passed over, so a second scheduler never handles the same firing. The lock is the one an update takes
    (``FOR NO KEY UPDATE``): a row held only by a foreign key check, such as a message about its last run being
    recorded, is not passed over."""
    row = await conn.fetchrow(
        """
        SELECT * FROM schedules
        WHERE enabled AND next_fire_at <= $1 AND NOT (id = ANY($2::text[]))
        ORDER BY next_fire_at, id LIMIT 1 FOR NO KEY UPDATE SKIP LOCKED
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
                     created_at: datetime | None = None, carried_from: str | None = None) -> str:
    run_id = new_run_id()
    await conn.execute(
        """
        INSERT INTO schedule_runs (id, schedule_id, scheduled_for, fired_at, trigger, missed, status, task_id,
                                   error, finished_at, created_at, carried_from)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, COALESCE($11, now()), $12)
        """,
        run_id, schedule_id, scheduled_for, fired_at, trigger, missed, status, task_id, error, finished_at,
        created_at, carried_from,
    )
    return run_id


_RUN_COLUMNS = ("status", "fired_at", "task_id", "summary", "cost_usd", "error", "finished_at", "gate_output",
                "carried_from")


async def count_skip(conn: Db, schedule_id: str, *, status: str, at: datetime, since: datetime,
                     besides: str | None = None) -> str | None:
    """Count a skipped firing on the schedule's latest run (other than ``besides``, the run being decided) when
    that run is a skip for the same reason recorded since ``since`` (the day's start): its id, its ``firings`` one
    more and its ``finished_at`` ``at``. None when it is not: the firing is then recorded as a run of its own."""
    if status not in SKIPPED:
        raise ValueError(f"not a skip: {status}")
    return await conn.fetchval(
        """
        UPDATE schedule_runs SET firings = firings + 1, finished_at = $3
        WHERE id = (SELECT id FROM schedule_runs WHERE schedule_id = $1 AND id IS DISTINCT FROM $5
                    ORDER BY created_at DESC, id DESC LIMIT 1)
          AND status = $2 AND created_at >= $4
        RETURNING id
        """,
        schedule_id, status, at, since, besides,
    )


async def delete_run(conn: Db, run_id: str) -> None:
    await conn.execute("DELETE FROM schedule_runs WHERE id = $1", run_id)


async def update_run(conn: Db, run_id: str, **fields: Any) -> None:
    """Set a run's ``fields`` (any of ``_RUN_COLUMNS``)."""
    unknown = sorted(set(fields) - set(_RUN_COLUMNS))
    if unknown:
        raise ValueError(f"not a run column: {unknown[0]}")
    sets = [f"{name} = ${i}" for i, name in enumerate(fields, start=2)]
    await conn.execute(f"UPDATE schedule_runs SET {', '.join(sets)} WHERE id = $1", run_id, *fields.values())


async def get_run(run_id: str, *, pool: Db | None = None) -> dict[str, Any] | None:
    db = pool or get_pool()
    row = await db.fetchrow(f"{_RUN} WHERE r.id = $1", run_id)
    return dict(row) if row else None


async def list_runs(schedule_id: str, *, limit: int, before: str | None = None, without: Sequence[str] = (),
                    pool: Db | None = None) -> list[dict[str, Any]]:
    """The schedule's runs, newest first, each with its task's status and title: those older than the run
    ``before`` when given, and none whose status is in ``without``."""
    db = pool or get_pool()
    rows = await db.fetch(
        f"""
        {_RUN}
        WHERE r.schedule_id = $1 AND NOT (r.status = ANY($3::text[]))
          AND ($4::text IS NULL OR (r.created_at, r.id) < (SELECT b.created_at, b.id FROM schedule_runs b
                                                          WHERE b.id = $4 AND b.schedule_id = $1))
        ORDER BY r.created_at DESC, r.id DESC LIMIT $2
        """,
        schedule_id, limit, list(without), before,
    )
    return [dict(row) for row in rows]


async def run_counts(schedule_id: str, statuses: Sequence[str], *, pool: Db | None = None) -> dict[str, int]:
    """How many of the schedule's firings ended with each of ``statuses`` (0 for none): a run that counts repeated
    skips counts each of its ``firings``."""
    db = pool or get_pool()
    rows = await db.fetch(
        "SELECT status, sum(firings) AS n FROM schedule_runs WHERE schedule_id = $1 AND status = ANY($2::text[]) "
        "GROUP BY status",
        schedule_id, list(statuses),
    )
    counts = {row["status"]: int(row["n"]) for row in rows}
    return {status: counts.get(status, 0) for status in statuses}


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
    """Write a running run's final status, once, and the schedule's counters with it (``count_outcome``). False
    when the run is not running (it was finished already), or its schedule was deleted. The schedule's row is
    locked first, as every other writer of a schedule and its runs locks them: a delete, which locks the schedule
    and then its runs, waits for this instead of deadlocking with it."""
    if await conn.fetchval("SELECT s.id FROM schedules s JOIN schedule_runs r ON r.schedule_id = s.id WHERE r.id = $1 "
                           "FOR NO KEY UPDATE OF s", run_id) is None:
        return False
    schedule_id = await conn.fetchval(
        """
        UPDATE schedule_runs SET status = $2, summary = $3, cost_usd = $4, error = $5, finished_at = $6
        WHERE id = $1 AND status = 'running' RETURNING schedule_id
        """,
        run_id, status, summary, cost_usd, error, at,
    )
    if schedule_id is None:
        return False
    await count_outcome(conn, schedule_id, status, at)
    return True


async def count_outcome(conn: Db, schedule_id: str, status: str, at: datetime) -> None:
    """A run's final ``status`` on the schedule's counters: an ok run (``OK``) clears ``consecutive_failures`` and
    sets ``last_ok_at``, and a completed one ``last_success_at`` too; a failure adds one; anything else (a stopped
    or cancelled run, a skip that ran nothing) changes neither."""
    if status in OK:
        await conn.execute(
            "UPDATE schedules SET consecutive_failures = 0, last_ok_at = $2, "
            "last_success_at = CASE WHEN $3 THEN $2 ELSE last_success_at END WHERE id = $1",
            schedule_id, at, status == "completed",
        )
    elif status in FAILURES:
        await conn.execute("UPDATE schedules SET consecutive_failures = consecutive_failures + 1 WHERE id = $1",
                           schedule_id)


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


async def unhanded_runs(conn: Db, schedule_id: str, *, besides: str | None = None) -> list[dict[str, Any]]:
    """The schedule's runs that ended without finishing their input (``UNFINISHED``, with a gate output) and have not
    handed it on to a later run yet (``carried_on``), oldest to end first; not ``besides``, the run being started.
    A ``parallel`` schedule's runs end in any order, and several may fail before the next one starts."""
    rows = await conn.fetch(
        """
        SELECT * FROM schedule_runs
        WHERE schedule_id = $1 AND status = ANY($2::text[]) AND NOT carried_on AND finished_at IS NOT NULL
          AND btrim(COALESCE(gate_output, '')) <> '' AND id IS DISTINCT FROM $3
        ORDER BY finished_at, id
        """,
        schedule_id, list(UNFINISHED), besides,
    )
    return [dict(row) for row in rows]


async def hand_on(conn: Db, run_ids: Sequence[str]) -> None:
    """Mark these runs' unfinished input as handed on: no later run is given it again."""
    if run_ids:
        await conn.execute("UPDATE schedule_runs SET carried_on = true WHERE id = ANY($1::text[])", list(run_ids))


async def spent_since(conn: Db, schedule_id: str, since: datetime) -> float:
    """What the schedule's runs fired since ``since`` cost: each its recorded cost, a running one its task's so
    far, as ``task_costs`` counts it."""
    return (await spent_since_each(conn, [schedule_id], since))[schedule_id]


async def spent_since_each(conn: Db, schedule_ids: list[str], since: datetime) -> dict[str, float]:
    """``spent_since`` for each of ``schedule_ids``, in one query."""
    rows = await conn.fetch(
        f"""
        SELECT r.schedule_id, sum(COALESCE(r.cost_usd,
                   (SELECT sum(l.cost_usd) FROM llm_records l WHERE l.task_id = r.task_id
                    AND {TASK_RUN.replace("agent_id", "l.agent_id")}))) AS spent
        FROM schedule_runs r WHERE r.schedule_id = ANY($1::text[]) AND r.fired_at >= $2 GROUP BY r.schedule_id
        """,
        schedule_ids, since,
    )
    spent = {row["schedule_id"]: float(row["spent"] or 0) for row in rows}
    return {schedule_id: spent.get(schedule_id, 0.0) for schedule_id in schedule_ids}


async def fail_gating_runs(conn: Db, at: datetime) -> int:
    """Runs left checking their gate by a server that stopped: gate_failed (a failure each)."""
    rows = await conn.fetch(
        "UPDATE schedule_runs SET status = 'gate_failed', finished_at = $1, "
        "error = 'the server stopped while the gate ran' WHERE status = 'gating' RETURNING schedule_id", at,
    )
    for row in rows:
        await count_outcome(conn, row["schedule_id"], "gate_failed", at)
    return len(rows)


async def set_alert(conn: Db, schedule_id: str, column: str, at: datetime | None) -> bool:
    """Start (``at``) or end (None) an alert, once: False when it already was in that state."""
    if column not in ("failures_alert_at", "stale_alert_at"):
        raise ValueError(f"not an alert column: {column}")
    condition = f"{column} IS NULL" if at is not None else f"{column} IS NOT NULL"
    return await conn.execute(f"UPDATE schedules SET {column} = $2 WHERE id = $1 AND {condition}",
                              schedule_id, at) == "UPDATE 1"


async def start_stale_alert(conn: Db, schedule_id: str, at: datetime, *, last_ok_at: datetime | None) -> bool:
    """Start the stale alert, once, while the schedule's latest ok run is still ``last_ok_at``, the one its deadline
    was found from: False when it already started, or when a run went well since (a long run that succeeded after
    the pass read the schedule), which leaves it not stale."""
    return await conn.execute(
        "UPDATE schedules SET stale_alert_at = $2 WHERE id = $1 AND stale_alert_at IS NULL "
        "AND last_ok_at IS NOT DISTINCT FROM $3", schedule_id, at, last_ok_at) == "UPDATE 1"


async def insert_notification(conn: Db, *, schedule_id: str, run_id: str | None, kind: str, channel: str | None,
                              chat: str | None, text: str, status: str, error: str | None) -> str:
    """Record a message; ``created_at`` is the database's clock, so the outbox keeps the order they were sent in."""
    notification_id = f"note-{uuid.uuid4().hex[:12]}"
    await conn.execute(
        "INSERT INTO schedule_notifications (id, schedule_id, run_id, kind, channel, chat, text, status, error, "
        "created_at) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, clock_timestamp())",
        notification_id, schedule_id, run_id, kind, channel, chat, text, status, error,
    )
    return notification_id


async def list_notifications(schedule_id: str, *, limit: int, pool: Db | None = None) -> list[dict[str, Any]]:
    db = pool or get_pool()
    rows = await db.fetch("SELECT * FROM schedule_notifications WHERE schedule_id = $1 "
                          "ORDER BY created_at DESC, id DESC LIMIT $2", schedule_id, limit)
    return [dict(row) for row in rows]


async def enabled_schedules(*, pool: Db | None = None) -> list[dict[str, Any]]:
    db = pool or get_pool()
    return await _with_overrides(db, await db.fetch("SELECT * FROM schedules WHERE enabled"))


async def first_run_at(conn: Db, schedule_id: str) -> datetime | None:
    return await conn.fetchval("SELECT min(scheduled_for) FROM schedule_runs WHERE schedule_id = $1", schedule_id)


async def overview(since: datetime, *, pool: Db | None = None) -> dict[str, Any]:
    """Every project's schedules at a glance: the counts, the next fires, the failing ones and the runs skipped
    since ``since``, by reason."""
    db = pool or get_pool()
    counts = await db.fetchrow("SELECT count(*) AS total, count(*) FILTER (WHERE enabled) AS enabled FROM schedules")
    upcoming = await db.fetch(
        "SELECT s.id, s.name, s.project_id, p.name AS project_name, s.next_fire_at, s.timezone "
        "FROM schedules s JOIN projects p ON p.id = s.project_id WHERE s.enabled AND s.next_fire_at IS NOT NULL "
        "ORDER BY s.next_fire_at, s.id LIMIT 5",
    )
    failing = await db.fetch(
        """
        SELECT s.id, s.name, s.project_id, p.name AS project_name, s.timezone, s.consecutive_failures,
               s.failures_alert_at, s.stale_alert_at, s.last_success_at,
               (SELECT r.status FROM schedule_runs r WHERE r.schedule_id = s.id AND NOT (r.status = ANY($1::text[]))
                ORDER BY r.created_at DESC, r.id DESC LIMIT 1) AS last_status
        FROM schedules s JOIN projects p ON p.id = s.project_id
        WHERE s.consecutive_failures > 0 OR s.failures_alert_at IS NOT NULL OR s.stale_alert_at IS NOT NULL
        ORDER BY (s.failures_alert_at IS NULL AND s.stale_alert_at IS NULL), s.consecutive_failures DESC, lower(s.name)
        """,
        list(OPEN_STATUSES),
    )
    skipped = await db.fetch(
        "SELECT status, sum(firings) AS n FROM schedule_runs WHERE created_at >= $1 AND status = ANY($2::text[]) "
        "GROUP BY status", since, list(SKIPPED),
    )
    return {"total": counts["total"], "enabled": counts["enabled"],
            "upcoming": [dict(row) for row in upcoming], "failing": [dict(row) for row in failing],
            "skipped": {row["status"]: row["n"] for row in skipped}}
