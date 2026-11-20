"""A consult of the advisor's question and advice (``advisor_advice``, the advisor RFC, section 5.3): one row per
``agent_spawns`` row whose request passed its checks. Its question is the last request sent; its advice the answer,
once there is one."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

import asyncpg

from .connection import get_pool

SOURCES = ("ladder", "checkpoint", "optional")


def _row(record: asyncpg.Record | None) -> dict[str, Any] | None:
    if record is None:
        return None
    row = dict(record)
    for key in ("question", "advice", "adoption_detail"):
        if isinstance(row.get(key), str):
            row[key] = json.loads(row[key])
    return row


async def record_question(*, spawn_id: int, task_id: str, step_id: str | None, attempt: int, source: str, kind: str,
                          question: Mapping[str, Any], pool: asyncpg.Pool | None = None) -> dict[str, Any]:
    """The consult's row with ``question`` as its question: created by its first request sent, replaced by the
    next (a round that answers what the advisor asked)."""
    if source not in SOURCES:
        raise ValueError(f"unknown advice source {source!r}")
    pool = pool or get_pool()
    row = await pool.fetchrow(
        """INSERT INTO advisor_advice (spawn_id, task_id, step_id, attempt, source, kind, question)
           VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb)
           ON CONFLICT (spawn_id) DO UPDATE SET question = EXCLUDED.question RETURNING *""",
        spawn_id, task_id, step_id, attempt, source, kind, json.dumps(dict(question)))
    return _row(row)  # type: ignore[return-value]


async def record_advice(spawn_id: int, advice: Mapping[str, Any], *,
                        pool: asyncpg.Pool | None = None) -> dict[str, Any] | None:
    """The consult's answer; None when it has no row (its request was never sent)."""
    pool = pool or get_pool()
    return _row(await pool.fetchrow("UPDATE advisor_advice SET advice = $2::jsonb WHERE spawn_id = $1 RETURNING *",
                                    spawn_id, json.dumps(dict(advice))))


async def get(spawn_id: int, *, pool: asyncpg.Pool | None = None) -> dict[str, Any] | None:
    pool = pool or get_pool()
    return _row(await pool.fetchrow("SELECT * FROM advisor_advice WHERE spawn_id = $1", spawn_id))


async def adopt(spawn_id: int, adoption: Mapping[str, Any], *, pool: asyncpg.Pool | None = None) -> None:
    """What the Planner took of the consult's answer (``advice_adoption``)."""
    pool = pool or get_pool()
    await pool.execute(
        "UPDATE advisor_advice SET adoption = $2, adoption_detail = $3::jsonb, adopted_at = now() "
        "WHERE spawn_id = $1 AND advice IS NOT NULL AND adoption IS NULL",
        spawn_id, adoption["adoption"], json.dumps({"items": list(adoption.get("items", ()))}))


async def unlock(task_id: str, step_id: str, attempt: int, passed: bool, *,
                 pool: asyncpg.Pool | None = None) -> int:
    """Whether the attempt the step's answered consults were for passed (``unlocked``), set once; how many."""
    pool = pool or get_pool()
    status = await pool.execute(
        "UPDATE advisor_advice SET unlocked = $4, unlocked_at = now() WHERE task_id = $1 AND step_id = $2 "
        "AND attempt = $3 AND advice IS NOT NULL AND unlocked IS NULL", task_id, step_id, attempt, passed)
    return int(status.split()[-1])


async def for_step(step_id: str, *, pool: asyncpg.Pool | None = None) -> list[dict[str, Any]]:
    """The step's consults that sent a request, oldest first."""
    pool = pool or get_pool()
    return [_row(r) for r in await pool.fetch(
        "SELECT * FROM advisor_advice WHERE step_id = $1 ORDER BY spawn_id", step_id)]
