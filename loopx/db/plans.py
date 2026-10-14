"""A task's plan: the ``task_plans`` row and its ``task_steps`` rows.

The Planner writes each change as it happens, one row per changed step; a
change to several rows (a new plan, a replan, added steps, a settled step and
the plan's summary) is one transaction. On a restart the Planner reads the
plan back with ``load``.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

import asyncpg

from .connection import get_pool

# Every status a plan can have (task_plans.status).
STATUSES = ("planning", "executing", "verifying", "stopping", "completed", "failed")
# A plan in one of these still runs; a server that stops leaves it to be resumed.
LIVE_STATUSES = ("executing", "verifying")
# A step in one of these has an outcome (or, for "completed", a result in review).
_DONE_STEP_STATUSES = ("completed", "verified", "failed", "skipped")

_SAVE_PLAN = """
INSERT INTO task_plans (task_id, plan_id, status, correlation_id, origin_channel_id, final_summary, fail_reason)
VALUES ($1, $2, $3, $4, $5, $6, $7)
ON CONFLICT (task_id) DO UPDATE
SET plan_id = EXCLUDED.plan_id, status = EXCLUDED.status, correlation_id = EXCLUDED.correlation_id,
    origin_channel_id = EXCLUDED.origin_channel_id, final_summary = EXCLUDED.final_summary,
    fail_reason = EXCLUDED.fail_reason, updated_at = now()
"""

_SAVE_STEP = f"""
INSERT INTO task_steps (id, task_id, step_index, step_key, instruction, target_files, validation_commands,
                        acceptance_criteria, depends_on, review, status, result, worker_id, reviewer_id,
                        retries, feedback, missed_messages, created_at, updated_at, completed_at)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12::jsonb, $13, $14, $15, $16, $17, now(), now(),
        CASE WHEN $11::text IN {_DONE_STEP_STATUSES!r} THEN now() END)
ON CONFLICT (id) DO UPDATE
SET step_index = EXCLUDED.step_index, step_key = EXCLUDED.step_key, instruction = EXCLUDED.instruction,
    target_files = EXCLUDED.target_files, validation_commands = EXCLUDED.validation_commands,
    acceptance_criteria = EXCLUDED.acceptance_criteria, depends_on = EXCLUDED.depends_on,
    review = EXCLUDED.review, status = EXCLUDED.status, result = EXCLUDED.result,
    worker_id = EXCLUDED.worker_id, reviewer_id = EXCLUDED.reviewer_id, retries = EXCLUDED.retries,
    feedback = EXCLUDED.feedback, missed_messages = EXCLUDED.missed_messages, updated_at = now(),
    completed_at = COALESCE(EXCLUDED.completed_at, task_steps.completed_at)
"""


def _step_args(task_id: str, step: Mapping[str, Any]) -> tuple[Any, ...]:
    result = step.get("result")
    return (
        step["id"], task_id, int(step["index"]), str(step.get("key") or ""), step["instruction"],
        list(step.get("target_files") or []), list(step.get("validation_commands") or []),
        list(step.get("acceptance_criteria") or []), list(step.get("depends_on") or []),
        bool(step.get("review")), step.get("status") or "pending",
        None if result is None else json.dumps(result, ensure_ascii=False),
        step.get("worker_id"), step.get("reviewer_id"), int(step.get("retries") or 0),
        list(step.get("feedback") or []), list(step.get("missed_messages") or []),
    )


async def save(
    task_id: str,
    *,
    plan: Mapping[str, Any] | None = None,
    steps: Sequence[Mapping[str, Any]] = (),
    pool: asyncpg.Pool | None = None,
) -> None:
    """Write the plan's own row (when given) and ``steps``' rows, in one transaction.

    ``plan`` has ``plan_id``, ``status``, ``correlation_id``,
    ``origin_channel_id``, ``final_summary`` and ``fail_reason``; each step
    has ``id``, ``index``, ``key``, ``instruction``, ``status`` and the rest of
    a step's columns, a missing one written as empty.
    """
    if plan is None and not steps:
        return
    pool = pool or get_pool()
    async with pool.acquire() as conn, conn.transaction():
        await write(conn, task_id, plan=plan, steps=steps)


async def write(
    conn: asyncpg.Connection,
    task_id: str,
    *,
    plan: Mapping[str, Any] | None = None,
    steps: Sequence[Mapping[str, Any]] = (),
) -> None:
    """``save``'s writes on ``conn``, in the caller's transaction."""
    if plan is not None:
        await conn.execute(
            _SAVE_PLAN, task_id, plan["plan_id"], plan["status"], plan.get("correlation_id") or "",
            plan.get("origin_channel_id") or "", plan.get("final_summary") or "", plan.get("fail_reason") or "",
        )
    if steps:
        await conn.executemany(_SAVE_STEP, [_step_args(task_id, step) for step in steps])


def _step(row: Mapping[str, Any]) -> dict[str, Any]:
    step = dict(row)
    if isinstance(step.get("result"), str):
        step["result"] = json.loads(step["result"])
    for name in ("target_files", "validation_commands", "acceptance_criteria", "depends_on", "feedback",
                 "missed_messages"):
        step[name] = list(step.get(name) or [])
    return step


async def load(task_id: str, *, pool: asyncpg.Pool | None = None) -> dict[str, Any] | None:
    """The task's plan, or None when it has none.

    ``{"plan": ..., "steps": [...]}``: the plan's row with the task's
    ``requirement``, ``goal_id``, ``work_dir`` and ``project_id``, and its
    steps in order, each ``result`` decoded.
    """
    pool = pool or get_pool()
    row = await pool.fetchrow(
        """
        SELECT p.*, t.requirement, t.goal_id, t.work_dir, t.project_id
        FROM task_plans p JOIN tasks t ON t.id = p.task_id WHERE p.task_id = $1
        """,
        task_id,
    )
    if row is None:
        return None
    steps = await pool.fetch("SELECT * FROM task_steps WHERE task_id = $1 ORDER BY step_index, created_at", task_id)
    return {"plan": dict(row), "steps": [_step(step) for step in steps]}


async def resumable_task_ids(
    active_statuses: Sequence[str], *, pool: asyncpg.Pool | asyncpg.Connection | None = None,
) -> list[str]:
    """The tasks still marked as running (``active_statuses``) whose plan is live, oldest first."""
    pool = pool or get_pool()
    rows = await pool.fetch(
        """
        SELECT t.id FROM tasks t JOIN task_plans p ON p.task_id = t.id
        WHERE t.status = ANY($1::text[]) AND t.completed_at IS NULL AND p.status = ANY($2::text[])
        ORDER BY t.created_at, t.id
        """,
        list(active_statuses), list(LIVE_STATUSES),
    )
    return [row["id"] for row in rows]
