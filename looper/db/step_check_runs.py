"""Execution evidence: append each run, then settle it once from running to a terminal state."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from enum import Enum
from typing import Any

import asyncpg

from .connection import get_pool


class CheckState(str, Enum):
    RUNNING = "running"
    PASSED = "passed"
    FAILED = "failed"
    ERROR = "error"
    STALE = "stale"


class ErrorKind(str, Enum):
    COULD_NOT_START = "could_not_start"
    TIMEOUT = "timeout"
    CHANGED_CODE = "changed_code"
    OUTPUT_LIMIT = "output_limit"
    ARTIFACT_INVALID = "artifact_invalid"
    EVIDENCE_UNAVAILABLE = "evidence_unavailable"
    INTERRUPTED = "interrupted"
    BUDGET = "budget"
    SANDBOX_UNAVAILABLE = "sandbox_unavailable"


def _row(record: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if record is None:
        return None
    row = dict(record)
    if isinstance(row.get("artifacts"), str):
        row["artifacts"] = json.loads(row["artifacts"])
    row["protected_changed"] = list(row.get("protected_changed") or [])
    return row


async def create_run(task_id: str, step_id: str, check: Mapping[str, Any], attempt: int, *,
                     head_sha: str | None = None, content_sha: str | None = None, guarded: bool = False,
                     pool: asyncpg.Pool | None = None) -> dict[str, Any]:
    """Allocate the next run for this check/attempt, serialized on its owning step."""
    if type(attempt) is not int or attempt < 0:
        raise ValueError("attempt must be a nonnegative integer")
    pool = pool or get_pool()
    async with pool.acquire() as conn, conn.transaction():
        if not await conn.fetchval("SELECT id FROM task_steps WHERE id=$1 AND task_id=$2 FOR UPDATE",
                                   step_id, task_id):
            raise ValueError("step does not belong to task")
        next_run = await conn.fetchval(
            "SELECT COALESCE(max(run), 0) + 1 FROM step_check_runs "
            "WHERE step_id=$1 AND check_id=$2 AND attempt=$3", step_id, check["id"], attempt)
        row = await conn.fetchrow(
            """INSERT INTO step_check_runs
                (task_id, step_id, check_id, attempt, run, criterion, type, required, state,
                 head_sha, content_sha, guarded)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,'running',$9,$10,$11) RETURNING *""",
            task_id, step_id, check["id"], attempt, next_run, check["criterion"], check["type"],
            check.get("required", True), head_sha, content_sha, guarded)
    return _row(row)  # type: ignore[return-value]


async def finish_run(run_id: int, state: CheckState, *, error_kind: ErrorKind | None = None,
                     exit_code: int | None = None, duration_ms: int | None = None, output_tail: str = "",
                     output_bytes: int = 0, output_sha256: str | None = None,
                     artifacts: Sequence[Mapping[str, Any]] = (), protected_changed: Sequence[str] = (),
                     pool: asyncpg.Pool | None = None) -> dict[str, Any] | None:
    """Settle a running row. Terminal history is immutable; a row already settled returns None."""
    state = CheckState(state)
    if state is CheckState.RUNNING:
        raise ValueError("a run can only finish in a terminal state")
    if (state is CheckState.ERROR) != (error_kind is not None):
        raise ValueError("error_kind is required exactly when state is error")
    if error_kind is not None:
        error_kind = ErrorKind(error_kind)
    pool = pool or get_pool()
    row = await pool.fetchrow(
        """UPDATE step_check_runs SET state=$2, error_kind=$3, exit_code=$4, duration_ms=$5,
            output_tail=$6, output_bytes=$7, output_sha256=$8, artifacts=$9::jsonb,
            protected_changed=$10, finished_at=now()
            WHERE id=$1 AND state='running' RETURNING *""",
        run_id, state.value, error_kind.value if error_kind is not None else None, exit_code, duration_ms,
        output_tail, output_bytes, output_sha256, json.dumps([dict(item) for item in artifacts]),
        list(protected_changed))
    return _row(row)


async def list_runs(task_id: str, step_id: str | None = None, *, pool: asyncpg.Pool | None = None
                    ) -> list[dict[str, Any]]:
    """All evidence, oldest first, optionally narrowed to a step's database id."""
    pool = pool or get_pool()
    rows = await pool.fetch(
        "SELECT * FROM step_check_runs WHERE task_id=$1 AND ($2::text IS NULL OR step_id=$2) ORDER BY id",
        task_id, step_id)
    return [_row(row) for row in rows]  # type: ignore[misc]


async def recover_interrupted(*, pool: asyncpg.Pool | None = None) -> list[dict[str, Any]]:
    """At startup, settle only unfinished rows. Their next execution appends a new run."""
    pool = pool or get_pool()
    rows = await pool.fetch(
        """UPDATE step_check_runs SET state='error', error_kind='interrupted', finished_at=now(),
            duration_ms=NULL
            WHERE state='running' RETURNING *""")
    return [_row(row) for row in rows]  # type: ignore[misc]
