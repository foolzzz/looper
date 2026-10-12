"""LLM invocation records — audit trail for agent sessions."""

from __future__ import annotations

import json
from typing import Any

import asyncpg

from .connection import get_pool


def _row_to_dict(row: asyncpg.Record) -> dict[str, Any]:
    d = dict(row)
    for key in ("created_at", "finished_at"):
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
            finished_at = now()
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


async def list_records(
    *,
    agent_id: str | None = None,
    task_id: str | None = None,
    limit: int = 100,
    offset: int = 0,
    pool: asyncpg.Pool | None = None,
) -> list[dict[str, Any]]:
    pool = pool or get_pool()

    conditions = []
    params: list[Any] = []
    idx = 1

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


async def list_sessions(
    agent_id: str,
    *,
    limit: int = 50,
    pool: asyncpg.Pool | None = None,
) -> list[dict[str, Any]]:
    """List distinct task sessions for an agent, with summary stats."""
    pool = pool or get_pool()
    rows = await pool.fetch(
        """
        SELECT
            task_id,
            COUNT(*) AS turn_count,
            SUM(input_tokens) AS total_input_tokens,
            SUM(output_tokens) AS total_output_tokens,
            SUM(duration_ms) AS total_duration_ms,
            MIN(created_at) AS first_at,
            MAX(created_at) AS last_at
        FROM llm_records
        WHERE agent_id = $1 AND task_id IS NOT NULL
        GROUP BY task_id
        ORDER BY MAX(created_at) DESC
        LIMIT $2
        """,
        agent_id, limit,
    )
    result = []
    for r in rows:
        d = dict(r)
        for k in ("first_at", "last_at"):
            if d.get(k) is not None:
                d[k] = d[k].isoformat()
        result.append(d)
    return result


async def list_session_messages(
    agent_id: str,
    task_id: str,
    *,
    limit: int = 200,
    pool: asyncpg.Pool | None = None,
) -> list[dict[str, Any]]:
    """Get full conversation messages for a specific agent+task session."""
    pool = pool or get_pool()
    rows = await pool.fetch(
        """
        SELECT id, input_summary, output_summary, input_text, output_text,
               input_tokens, output_tokens, duration_ms, model, error, created_at
        FROM llm_records
        WHERE agent_id = $1 AND task_id = $2
        ORDER BY created_at ASC
        LIMIT $3
        """,
        agent_id, task_id, limit,
    )
    return [_row_to_dict(r) for r in rows]
