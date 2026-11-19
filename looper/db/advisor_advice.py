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


async def consults_view(task_id: str, *, pool: asyncpg.Pool | None = None) -> list[dict[str, Any]]:
    """The task's consults of the advisor, oldest first, as its page shows them: where each came from and for which
    attempt, how it ended (its state and why it failed), its rounds, returns and spend, the advisor's conclusions,
    what the Planner took of them and whether the attempt passed. Never the request's text."""
    pool = pool or get_pool()
    rows = await pool.fetch(
        """SELECT s.id, s.step_id, s.state, s.rounds, s.returns, s.spent_usd, s.brief, s.result->>'failure' AS failure,
                  s.created_at, s.finished_at, a.kind, a.attempt, a.advice, a.adoption, a.unlocked
             FROM agent_spawns s LEFT JOIN advisor_advice a ON a.spawn_id = s.id
            WHERE s.task_id = $1 AND s.kind = 'advisor' ORDER BY s.id""", task_id)
    consults = []
    for row in rows:
        brief = json.loads(row["brief"]) if isinstance(row["brief"], str) else (row["brief"] or {})
        advice = json.loads(row["advice"]) if isinstance(row["advice"], str) else (row["advice"] or {})
        consults.append({
            "id": row["id"], "step_id": row["step_id"], "source": brief.get("source"), "kind": row["kind"],
            "attempt": row["attempt"] if row["attempt"] is not None else brief.get("attempt"),
            "state": row["state"], "failure": row["failure"], "rounds": row["rounds"], "returns": row["returns"],
            "spent_usd": float(row["spent_usd"]), "status": advice.get("status"),
            "answers": [{"question_id": a["question_id"], "conclusion": a["conclusion"],
                         "confidence": a.get("confidence")} for a in advice.get("answers", [])],
            "adoption": row["adoption"], "unlocked": row["unlocked"],
            "created_at": row["created_at"].isoformat(),
            "finished_at": row["finished_at"].isoformat() if row["finished_at"] else None,
        })
    return consults
