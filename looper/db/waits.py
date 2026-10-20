"""A task's waits (``task_waits``), saved with the steps they belong to.

The Planner writes its columns in the plan's transaction (``plans.write``).
A row with ``expect`` only changes while its state is one the Planner read;
when someone moved it first, the next event reads it again. The loop and the
user move waiting rows with the same condition (``move``); checks only update
rows still waiting (``record_check``). The types and limits are
``agents.waits``'s.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

import asyncpg

from looper.agents.waits import OUTPUT_MAX_BYTES

from .connection import get_pool
from .tasks import ACTIVE_STATUSES

log = logging.getLogger(__name__)

_WRITE = """
INSERT INTO task_waits (step_id, task_id, topic, spec, state, outcome, opened_at, due_at, deadline_at,
                        next_check_at, resolution, extensions, failures, failed_at, closed_at)
VALUES ($1, $2, $3, $4::jsonb, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15)
ON CONFLICT (step_id) DO UPDATE
SET topic = EXCLUDED.topic, spec = EXCLUDED.spec, state = EXCLUDED.state, outcome = EXCLUDED.outcome,
    opened_at = EXCLUDED.opened_at, due_at = EXCLUDED.due_at, deadline_at = EXCLUDED.deadline_at,
    next_check_at = EXCLUDED.next_check_at, resolution = EXCLUDED.resolution, extensions = EXCLUDED.extensions,
    failures = EXCLUDED.failures, failed_at = EXCLUDED.failed_at, closed_at = EXCLUDED.closed_at, updated_at = now()
WHERE $16::text[] IS NULL OR task_waits.state = ANY($16::text[])
RETURNING step_id
"""

_WAITS = """
SELECT w.*, s.step_key, s.step_index,
       ARRAY(SELECT d.step_key FROM unnest(s.depends_on) WITH ORDINALITY AS dep(id, place)
             JOIN task_steps d ON d.id = dep.id ORDER BY dep.place) AS depends_on
FROM task_waits w JOIN task_steps s ON s.id = w.step_id
"""


def _json(value: Any) -> str | None:
    return None if value is None else json.dumps(value, ensure_ascii=False, default=str)


def _wait(row: Mapping[str, Any]) -> dict[str, Any]:
    wait = dict(row)
    for name in ("spec", "baseline"):
        if isinstance(wait.get(name), str):
            wait[name] = json.loads(wait[name])
    if "depends_on" in wait:
        wait["depends_on"] = list(wait.get("depends_on") or [])
    return wait


async def write(conn: asyncpg.Connection, task_id: str, waits: Sequence[Mapping[str, Any]]) -> None:
    """Write the Planner's rows (``agents.waits.Wait.row``) on ``conn`` in the caller's transaction, after their
    steps. ``expect``, when given, limits an update to those states; a row someone moved first is left alone."""
    for wait in waits:
        expect = wait.get("expect")
        applied = await conn.fetchval(
            _WRITE, wait["step_id"], task_id, wait["topic"], _json(wait["spec"]), wait["state"], wait.get("outcome"),
            wait.get("opened_at"), wait.get("due_at"), wait.get("deadline_at"), wait.get("next_check_at"),
            wait.get("resolution") or "", int(wait.get("extensions") or 0), int(wait.get("failures") or 0),
            wait.get("failed_at"), wait.get("closed_at"), None if expect is None else list(expect))
        if applied is None:
            log.info("Wait %s changed before the Planner's write (expected %s)", wait["step_id"], expect)


async def list_for_task(task_id: str, *, pool: asyncpg.Pool | asyncpg.Connection | None = None
                        ) -> list[dict[str, Any]]:
    """The task's waits in plan order, with their steps' ``step_key``, ``step_index`` and ``depends_on`` as keys."""
    pool = pool or get_pool()
    rows = await pool.fetch(_WAITS + " WHERE w.task_id = $1 ORDER BY s.step_index, s.created_at", task_id)
    return [_wait(row) for row in rows]


async def get(task_id: str, label: str, *, pool: asyncpg.Pool | asyncpg.Connection | None = None
              ) -> dict[str, Any] | None:
    """The task's wait whose step is labelled ``label``; None when it has none."""
    pool = pool or get_pool()
    row = await pool.fetchrow(_WAITS + " WHERE w.task_id = $1 AND s.step_key = $2", task_id, label)
    return _wait(row) if row else None


async def for_planner(task_id: str, *, pool: asyncpg.Pool | asyncpg.Connection | None = None
                      ) -> list[dict[str, Any]]:
    """The task's waits the loop or the user moved and the Planner must take, in plan order."""
    pool = pool or get_pool()
    rows = await pool.fetch(_WAITS + " WHERE w.task_id = $1 AND w.state IN ('ready', 'timed_out', 'cancel_requested') "
                            "ORDER BY s.step_index, s.created_at", task_id)
    return [_wait(row) for row in rows]


async def due(now: datetime, statuses: Sequence[str], *, limit: int = 100,
              pool: asyncpg.Pool | asyncpg.Connection | None = None) -> list[dict[str, Any]]:
    """Waiting rows of tasks in ``statuses`` whose check, deadline or check-free due time has arrived."""
    pool = pool or get_pool()
    rows = await pool.fetch(
        "SELECT w.*, t.work_dir, t.project_id, s.step_key FROM task_waits w "
        "JOIN tasks t ON t.id = w.task_id JOIN task_steps s ON s.id = w.step_id "
        "WHERE w.state = 'waiting' AND t.status = ANY($2::text[]) "
        "AND (w.next_check_at <= $1 OR w.deadline_at <= $1 OR (w.spec->'check' = 'null'::jsonb AND w.due_at <= $1)) "
        "ORDER BY w.deadline_at, w.next_check_at, w.step_id LIMIT $3", now, list(statuses), limit)
    return [_wait(row) for row in rows]


async def record_check(step_id: str, *, at: datetime, result: str, output: str, error: str,
                       next_check_at: datetime | None, baseline: Mapping[str, Any] | None = None,
                       pool: asyncpg.Pool | asyncpg.Connection | None = None) -> bool:
    """Record a check only while the wait still waits. Keep the last ``OUTPUT_MAX_BYTES`` of its
    UTF-8 output, replacing a split character at the boundary;
    ``baseline`` stays as it is unless given."""
    pool = pool or get_pool()
    tail = output.encode("utf-8")[-OUTPUT_MAX_BYTES:]
    output = tail.decode("utf-8", errors="replace")
    if len(output.encode("utf-8")) > OUTPUT_MAX_BYTES:
        # Replacement characters can grow the cut boundary. Keep them and the newest whole characters.
        split = 0
        while tail[split] & 0xC0 == 0x80:
            split += 1
        boundary = tail[:split].decode("utf-8", errors="replace")
        available = OUTPUT_MAX_BYTES - len(boundary.encode("utf-8"))
        output = boundary + tail[split:][-available:].decode("utf-8", errors="ignore")
    row = await pool.fetchval(
        "UPDATE task_waits SET checks = checks + 1, last_checked_at = $2, last_result = $3, last_output = $4, "
        "last_error = $5, next_check_at = $6, baseline = COALESCE($7::jsonb, baseline), updated_at = now() "
        "WHERE step_id = $1 AND state = 'waiting' RETURNING step_id",
        step_id, at, result, output, error, next_check_at, _json(baseline))
    return row is not None


async def move(step_id: str, *, to: str, expect: Sequence[str] = ("waiting",), resolution: str | None = None,
               outcome: str | None = None, pool: asyncpg.Pool | asyncpg.Connection | None = None
               ) -> dict[str, Any] | None:
    """Move an active task's wait only from an expected state; return None if the wait or task moved first.

    Moving to ``ready`` needs an ``outcome`` (``ready`` or ``resumed``): the service or the user says which, and
    the same update stores it. Any other state stores no outcome.
    """
    if to == "ready" and not outcome:
        raise ValueError("moving a wait to ready needs an outcome")
    pool = pool or get_pool()
    row = await pool.fetchrow(
        "UPDATE task_waits SET state = $2, outcome = $5, resolution = COALESCE($4, resolution), updated_at = now() "
        "WHERE step_id = $1 AND state = ANY($3::text[]) "
        "AND EXISTS (SELECT 1 FROM tasks WHERE id = task_waits.task_id AND status = ANY($6::text[])) RETURNING *",
        step_id, to, list(expect), resolution, outcome if to == "ready" else None, list(ACTIVE_STATUSES))
    return _wait(row) if row else None


async def set_notice(step_id: str, notice: str, *, pool: asyncpg.Pool | asyncpg.Connection | None = None) -> None:
    """Record where the wait's timeout notice went (or that it failed)."""
    pool = pool or get_pool()
    await pool.execute("UPDATE task_waits SET notice = $2, updated_at = now() WHERE step_id = $1", step_id, notice)
