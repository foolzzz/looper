"""LLM invocation records — audit trail for agent sessions."""

from __future__ import annotations

import json
from typing import Any

import asyncpg

from .connection import get_pool


def _row_to_dict(row: asyncpg.Record) -> dict[str, Any]:
    d = dict(row)
    for key in ("created_at", "finished_at", "last_output_at"):
        if d.get(key) is not None:
            d[key] = d[key].isoformat()
    if "metadata" in d and d["metadata"] is not None:
        d["metadata"] = json.loads(d["metadata"])
    return d


async def start_record(
    *,
    agent_id: str,
    task_id: str | None = None,
    session_id: str | None = None,
    input_text: str | None = None,
    metadata: dict[str, Any] | None = None,
    pool: asyncpg.Pool | None = None,
) -> int:
    """Open a ``running`` record for one Claude invocation; returns its id."""
    pool = pool or get_pool()
    row = await pool.fetchrow(
        """
        INSERT INTO llm_records
            (agent_id, task_id, session_id, input_summary, input_text, metadata, status)
        VALUES ($1, $2, $3, $4, $5, $6::jsonb, 'running')
        RETURNING id
        """,
        agent_id, task_id, session_id,
        input_text[:500] if input_text else None, input_text,
        json.dumps(metadata) if metadata else None,
    )
    return row["id"]


async def append_events(
    record_id: int,
    events: list[tuple[int, dict[str, Any]]],
    *,
    pool: asyncpg.Pool | None = None,
) -> None:
    """Append ``(seq, event)`` stream events to a record's transcript."""
    if not events:
        return
    pool = pool or get_pool()
    await pool.executemany(
        "INSERT INTO llm_record_events (record_id, seq, event) VALUES ($1, $2, $3::jsonb)",
        [(record_id, seq, json.dumps(event, ensure_ascii=False)) for seq, event in events],
    )


async def update_event(
    record_id: int,
    seq: int,
    event: dict[str, Any],
    *,
    pool: asyncpg.Pool | None = None,
) -> None:
    """Replace a stored event in place (a progress counter that keeps growing)."""
    pool = pool or get_pool()
    await pool.execute(
        "UPDATE llm_record_events SET event = $3::jsonb WHERE record_id = $1 AND seq = $2",
        record_id, seq, json.dumps(event, ensure_ascii=False),
    )


async def touch_record(record_id: int, *, pool: asyncpg.Pool | None = None) -> None:
    """Mark that a running record just produced output (its heartbeat)."""
    pool = pool or get_pool()
    await pool.execute("UPDATE llm_records SET last_output_at = now() WHERE id = $1", record_id)


async def finish_record(
    record_id: int,
    *,
    status: str,
    output_text: str | None = None,
    session_id: str | None = None,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    cost_usd: float | None = None,
    duration_ms: int | None = None,
    model: str | None = None,
    error: str | None = None,
    pool: asyncpg.Pool | None = None,
) -> None:
    """Close a record with the run's outcome, usage and cost."""
    pool = pool or get_pool()
    await pool.execute(
        """
        UPDATE llm_records SET
            status = $2,
            output_text = $3,
            output_summary = $4,
            session_id = COALESCE($5, session_id),
            input_tokens = $6,
            output_tokens = $7,
            cost_usd = $8,
            duration_ms = $9,
            model = COALESCE($10, model),
            error = $11,
            finished_at = now(),
            last_output_at = COALESCE(last_output_at, now())
        WHERE id = $1
        """,
        record_id, status, output_text, output_text[:500] if output_text else None,
        session_id, input_tokens, output_tokens, cost_usd, duration_ms, model, error,
    )


async def list_events(
    record_id: int,
    *,
    after_seq: int = 0,
    limit: int = 1000,
    pool: asyncpg.Pool | None = None,
) -> list[dict[str, Any]]:
    """A record's stream events with ``seq`` greater than ``after_seq``, in order."""
    pool = pool or get_pool()
    rows = await pool.fetch(
        """
        SELECT seq, event, created_at FROM llm_record_events
        WHERE record_id = $1 AND seq > $2
        ORDER BY seq
        LIMIT $3
        """,
        record_id, after_seq, limit,
    )
    return [
        {"seq": r["seq"], "event": json.loads(r["event"]), "created_at": r["created_at"].isoformat()}
        for r in rows
    ]


# Which records belong to an agent role (the ids in agent_configs). Workers
# and reviewers record under their per-run instance id; the Planner runs no LLM.
_ROLE_FILTERS = {
    "manager": "agent_id = 'manager'",
    "worker": "(agent_id LIKE 'worker-%' OR agent_id LIKE 'reviewer-%')",
}


async def list_task_runs(
    task_ids: list[str],
    *,
    pool: asyncpg.Pool | None = None,
) -> list[dict[str, Any]]:
    """Worker and Reviewer runs of several tasks, oldest first, with the step each ran."""
    if not task_ids:
        return []
    pool = pool or get_pool()
    rows = await pool.fetch(
        "SELECT agent_id, task_id, status, created_at, metadata->>'step_id' AS step_id "
        f"FROM llm_records WHERE task_id = ANY($1::text[]) AND {_ROLE_FILTERS['worker']} "
        "ORDER BY created_at, id",
        task_ids,
    )
    return [dict(r) for r in rows]


async def list_records(
    *,
    agent_id: str | None = None,
    task_id: str | None = None,
    role: str | None = None,
    limit: int = 100,
    offset: int = 0,
    pool: asyncpg.Pool | None = None,
) -> list[dict[str, Any]]:
    """Records newest first, filtered by agent instance, task and/or role."""
    pool = pool or get_pool()

    conditions = []
    params: list[Any] = []
    idx = 1

    if role is not None:
        if role not in _ROLE_FILTERS:
            return []
        conditions.append(_ROLE_FILTERS[role])

    if agent_id:
        conditions.append(f"agent_id = ${idx}")
        params.append(agent_id)
        idx += 1
    if task_id:
        conditions.append(f"task_id = ${idx}")
        params.append(task_id)
        idx += 1

    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    params.extend([limit, offset])

    rows = await pool.fetch(
        f"SELECT * FROM llm_records {where} ORDER BY created_at DESC LIMIT ${idx} OFFSET ${idx + 1}",
        *params,
    )
    return [_row_to_dict(r) for r in rows]


async def get_record(
    record_id: int,
    *,
    pool: asyncpg.Pool | None = None,
) -> dict[str, Any] | None:
    pool = pool or get_pool()
    row = await pool.fetchrow("SELECT * FROM llm_records WHERE id = $1", record_id)
    if row is None:
        return None
    return _row_to_dict(row)
