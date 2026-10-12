"""LLM invocation records — audit trail for agent sessions."""

from __future__ import annotations

import json
from typing import Any

import asyncpg

from .connection import get_pool


def _row_to_dict(row: asyncpg.Record) -> dict[str, Any]:
    d = dict(row)
    if "created_at" in d and d["created_at"] is not None:
        d["created_at"] = d["created_at"].isoformat()
    if "metadata" in d and d["metadata"] is not None:
        d["metadata"] = json.loads(d["metadata"])
    return d


async def insert_record(
    *,
    agent_id: str,
    task_id: str | None = None,
    session_id: str | None = None,
    input_summary: str | None = None,
    output_summary: str | None = None,
    input_text: str | None = None,
    output_text: str | None = None,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    duration_ms: int | None = None,
    model: str | None = None,
    error: str | None = None,
    metadata: dict[str, Any] | None = None,
    pool: asyncpg.Pool | None = None,
) -> int:
    """Insert an LLM invocation record. Returns the record id."""
    pool = pool or get_pool()
    meta_json = json.dumps(metadata) if metadata else None
    row = await pool.fetchrow(
        """
        INSERT INTO llm_records
            (agent_id, task_id, session_id, input_summary, output_summary,
             input_text, output_text,
             input_tokens, output_tokens, duration_ms, model, error, metadata)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13)
        RETURNING id
        """,
        agent_id,
        task_id,
        session_id,
        input_summary,
        output_summary,
        input_text,
        output_text,
        input_tokens,
        output_tokens,
        duration_ms,
        model,
        error,
        meta_json,
    )
    return row["id"]


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
