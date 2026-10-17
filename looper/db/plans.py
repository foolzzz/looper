"""A task's plan: the ``task_plans`` row and its ``task_steps`` rows.

The Planner writes each change as it happens, one row per changed step; a
change to several rows (a new plan, a replan, added steps, a settled step and
the plan's summary, with the step's ``task_journal`` entry when it was
accepted) is one transaction, and so is a gate step's change with its gate and
the messages it adds to the gate's discussion (``user_gates.write``). On a
restart the Planner reads the plan, its journal and its gates back with
``load``.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

import asyncpg

from . import journal as db_journal, user_gates as db_user_gates
from .connection import get_pool
from .tasks import step_row

# Every status a plan can have (task_plans.status).
STATUSES = ("planning", "executing", "verifying", "stopping", "completed", "failed")
# A plan in one of these still runs; a server that stops leaves it to be resumed.
LIVE_STATUSES = ("executing", "verifying")
# A step in one of these has an outcome (or, for "completed", a result in review).
_DONE_STEP_STATUSES = ("completed", "verified", "failed", "skipped")

_SAVE_PLAN = """
INSERT INTO task_plans (task_id, plan_id, status, correlation_id, origin_channel_id, final_summary, fail_reason,
                        start_branch, start_commit)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
ON CONFLICT (task_id) DO UPDATE
SET plan_id = EXCLUDED.plan_id, status = EXCLUDED.status, correlation_id = EXCLUDED.correlation_id,
    origin_channel_id = EXCLUDED.origin_channel_id, final_summary = EXCLUDED.final_summary,
    fail_reason = EXCLUDED.fail_reason, start_branch = EXCLUDED.start_branch, start_commit = EXCLUDED.start_commit,
    updated_at = now()
"""

_SAVE_STEP = f"""
INSERT INTO task_steps (id, task_id, step_index, step_key, instruction, target_files, validation_commands,
                        acceptance_criteria, depends_on, review, status, result, worker_id, reviewer_id,
                        retries, feedback, missed_messages, role_key, review_role_key, role_version,
                        review_role_version, review_stage, review_role_required, kind, created_at, updated_at,
                        completed_at, started_at)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12::jsonb, $13, $14, $15, $16, $17, $18, $19, $20, $21, $22,
        $23, $24, now(), now(), CASE WHEN $11::text IN {_DONE_STEP_STATUSES!r} THEN now() END, $25)
ON CONFLICT (id) DO UPDATE
SET step_index = EXCLUDED.step_index, step_key = EXCLUDED.step_key, instruction = EXCLUDED.instruction,
    target_files = EXCLUDED.target_files, validation_commands = EXCLUDED.validation_commands,
    acceptance_criteria = EXCLUDED.acceptance_criteria, depends_on = EXCLUDED.depends_on,
    review = EXCLUDED.review, status = EXCLUDED.status, result = EXCLUDED.result,
    worker_id = EXCLUDED.worker_id, reviewer_id = EXCLUDED.reviewer_id, retries = EXCLUDED.retries,
    feedback = EXCLUDED.feedback, missed_messages = EXCLUDED.missed_messages, role_key = EXCLUDED.role_key,
    review_role_key = EXCLUDED.review_role_key, role_version = EXCLUDED.role_version,
    review_role_version = EXCLUDED.review_role_version, review_stage = EXCLUDED.review_stage,
    review_role_required = EXCLUDED.review_role_required, kind = EXCLUDED.kind, updated_at = now(),
    completed_at = COALESCE(EXCLUDED.completed_at, task_steps.completed_at),
    started_at = COALESCE(task_steps.started_at, EXCLUDED.started_at)
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
        step.get("role_key"), step.get("review_role_key"), step.get("role_version"), step.get("review_role_version"),
        step.get("review_stage"), bool(step.get("review_role_required", True)), step.get("kind") or "work",
        step.get("started_at"),
    )


async def save(
    task_id: str,
    *,
    plan: Mapping[str, Any] | None = None,
    steps: Sequence[Mapping[str, Any]] = (),
    journal: Sequence[Mapping[str, Any]] = (),
    gates: Sequence[Mapping[str, Any]] = (),
    gate_messages: Sequence[Mapping[str, Any]] = (),
    pool: asyncpg.Pool | None = None,
) -> None:
    """Write the plan's own row (when given), ``steps``' rows, ``journal``'s entries and the gates of gate steps
    with the messages they add to their discussions (``user_gates.write``), in one transaction.

    ``plan`` has ``plan_id``, ``status``, ``correlation_id``,
    ``origin_channel_id``, ``final_summary`` and ``fail_reason``, and may have
    ``start_branch`` and ``start_commit``; each step
    has ``id``, ``index``, ``key``, ``instruction``, ``status`` and the rest of
    a step's columns, a missing one written as empty; each journal entry has
    ``step_id``, ``kind``, ``summary`` and ``learnings`` (``journal.insert``).
    """
    if plan is None and not steps and not journal and not gates and not gate_messages:
        return
    pool = pool or get_pool()
    async with pool.acquire() as conn, conn.transaction():
        await write(conn, task_id, plan=plan, steps=steps, journal=journal, gates=gates,
                    gate_messages=gate_messages)


async def write(
    conn: asyncpg.Connection,
    task_id: str,
    *,
    plan: Mapping[str, Any] | None = None,
    steps: Sequence[Mapping[str, Any]] = (),
    journal: Sequence[Mapping[str, Any]] = (),
    gates: Sequence[Mapping[str, Any]] = (),
    gate_messages: Sequence[Mapping[str, Any]] = (),
) -> None:
    """``save``'s writes on ``conn``, in the caller's transaction."""
    if plan is not None:
        await conn.execute(
            _SAVE_PLAN, task_id, plan["plan_id"], plan["status"], plan.get("correlation_id") or "",
            plan.get("origin_channel_id") or "", plan.get("final_summary") or "", plan.get("fail_reason") or "",
            plan.get("start_branch") or "", plan.get("start_commit") or "",
        )
    if steps:
        await conn.executemany(_SAVE_STEP, [_step_args(task_id, step) for step in steps])
    # After the steps: an entry, a gate and its messages belong to a step saved with them.
    await db_journal.insert(conn, task_id, journal)
    await db_user_gates.write(conn, task_id, gates, gate_messages)


async def load(task_id: str, *, pool: asyncpg.Pool | None = None) -> dict[str, Any] | None:
    """The task's plan, or None when it has none.

    ``{"plan": ..., "steps": [...], "journal": [...], "gates": {...}}``: the
    plan's row with the task's ``requirement``, ``goal_id``, ``work_dir`` and
    ``project_id``, its steps in order, each ``result`` decoded, its journal
    oldest first (``journal.list_entries``) and its gates by step id
    (``user_gates.list_for_task``).
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
    return {"plan": dict(row), "steps": [step_row(step) for step in steps],
            "journal": await db_journal.list_entries(task_id, pool=pool),
            "gates": {gate["step_id"]: gate for gate in await db_user_gates.list_for_task(task_id, pool=pool)}}


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
