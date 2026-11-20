"""Runs another run starts (``agent_spawns``, the advisor RFC, section 5.2): one row per consult of the advisor.

A row's state moves only along ``TRANSITIONS``, and only from the state its caller expects (``move``): a row another
caller moved first is left as it is, and ``move`` returns None. A finished row (``FINISHED``) has ``finished_at``
and never moves again. ``result`` holds typed outcomes only: each round, each check hit by category and position
(never what it matched), and why a row failed.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from enum import Enum
from typing import Any

import asyncpg

from .connection import get_pool


class SpawnState(str, Enum):
    DRAFTING = "drafting"  # the Planner writes the request (again, after a return)
    LINTING = "linting"  # the checks run on it
    ASKING = "asking"  # a round of the child runs
    NEEDS_INFO = "needs_info"  # the child asked back; the Planner writes what it asked for
    ANSWERED = "answered"
    BLOCKED = "blocked"  # its request failed the checks after the last return
    FAILED = "failed"
    OVER_BUDGET = "over_budget"


FINISHED = frozenset({SpawnState.ANSWERED, SpawnState.BLOCKED, SpawnState.FAILED, SpawnState.OVER_BUDGET})
# Where a row may move from each unfinished state.
TRANSITIONS: Mapping[SpawnState, frozenset[SpawnState]] = {
    SpawnState.DRAFTING: frozenset({SpawnState.LINTING, SpawnState.FAILED, SpawnState.OVER_BUDGET}),
    SpawnState.LINTING: frozenset({SpawnState.DRAFTING, SpawnState.ASKING, SpawnState.BLOCKED, SpawnState.FAILED,
                                   SpawnState.OVER_BUDGET}),
    SpawnState.ASKING: frozenset({SpawnState.ANSWERED, SpawnState.NEEDS_INFO, SpawnState.FAILED,
                                  SpawnState.OVER_BUDGET}),
    SpawnState.NEEDS_INFO: frozenset({SpawnState.LINTING, SpawnState.FAILED}),
}
KINDS = ("advisor",)


class TransitionError(ValueError):
    """A move the state machine does not have (a caller's mistake, not a race)."""


def _row(record: asyncpg.Record | None) -> dict[str, Any] | None:
    if record is None:
        return None
    row = dict(record)
    for key in ("brief", "inputs", "result"):
        if isinstance(row.get(key), str):
            row[key] = json.loads(row[key])
    for key in ("budget_usd", "spent_usd"):
        if row.get(key) is not None:
            row[key] = float(row[key])
    return row


async def create(*, kind: str, task_id: str, step_id: str | None, parent_record_id: int | None,
                 brief: Mapping[str, Any], inputs: Mapping[str, Any], budget_usd: float,
                 state: SpawnState = SpawnState.DRAFTING, pool: asyncpg.Pool | None = None) -> dict[str, Any]:
    """A new row for a run ``parent_record_id`` starts in task ``task_id``, in ``state`` (an unfinished one)."""
    if kind not in KINDS:
        raise ValueError(f"unknown spawn kind {kind!r}")
    if state in FINISHED:
        raise TransitionError(f"a spawn cannot start {state.value}")
    if not budget_usd > 0:
        raise ValueError("a spawn needs a budget above 0")
    pool = pool or get_pool()
    row = await pool.fetchrow(
        """INSERT INTO agent_spawns (kind, task_id, step_id, parent_record_id, brief, inputs, budget_usd, state,
                                     rounds)
           VALUES ($1, $2, $3, $4, $5::jsonb, $6::jsonb, $7, $8, $9) RETURNING *""",
        kind, task_id, step_id, parent_record_id, json.dumps(dict(brief)), json.dumps(dict(inputs)), budget_usd,
        state.value, 1 if state is SpawnState.ASKING else 0)
    return _row(row)  # type: ignore[return-value]


async def get(spawn_id: int, *, pool: asyncpg.Pool | None = None) -> dict[str, Any] | None:
    pool = pool or get_pool()
    return _row(await pool.fetchrow("SELECT * FROM agent_spawns WHERE id = $1", spawn_id))


async def ids_in(state: SpawnState, *, pool: asyncpg.Pool | None = None) -> list[int]:
    """The spawns in ``state``, oldest first."""
    pool = pool or get_pool()
    return [r["id"] for r in await pool.fetch("SELECT id FROM agent_spawns WHERE state = $1 ORDER BY id", state.value)]


async def for_task(task_id: str, *, pool: asyncpg.Pool | None = None) -> list[dict[str, Any]]:
    """The task's spawns, oldest first."""
    pool = pool or get_pool()
    return [_row(r) for r in await pool.fetch("SELECT * FROM agent_spawns WHERE task_id = $1 ORDER BY id", task_id)]


async def move(spawn_id: int, to: SpawnState, *, expect: SpawnState, child_record_id: int | None = None,
               spent_usd: float = 0.0, result: Mapping[str, Any] | None = None,
               append: Mapping[str, Mapping[str, Any] | Sequence[Mapping[str, Any]]] | None = None,
               inputs: Mapping[str, Any] | None = None,
               detail: str | None = None, returned: bool = False,
               pool: asyncpg.Pool | None = None) -> dict[str, Any] | None:
    """Move the row from ``expect`` to ``to``; None, changing nothing, when it is no longer in ``expect``.

    With the move: ``spent_usd`` is added to what it spent; ``result``'s keys replace the result's, and each item of
    ``append`` (one, or a list of them) is added to the list under its key, all as the row is now; ``inputs`` replaces its inputs; ``detail`` replaces its detail; a
    move to ``asking`` counts a round, and ``returned`` (``linting`` to ``drafting``) a return. Raises
    ``TransitionError`` for a move ``TRANSITIONS`` does not have."""
    if to not in TRANSITIONS.get(expect, frozenset()):
        raise TransitionError(f"a spawn cannot move from {expect.value} to {to.value}")
    if returned and (expect, to) != (SpawnState.LINTING, SpawnState.DRAFTING):
        raise TransitionError("only a move from linting back to drafting is a return")
    if spent_usd < 0:
        raise ValueError("spent_usd cannot be negative")
    pool = pool or get_pool()
    async with pool.acquire() as conn, conn.transaction():
        current = _row(await conn.fetchrow("SELECT * FROM agent_spawns WHERE id = $1 FOR UPDATE", spawn_id))
        if current is None or current["state"] != expect.value:
            return None
        merged = {**current["result"], **(result or {})}
        for key, item in (append or {}).items():
            items = item if isinstance(item, (list, tuple)) else [item]
            merged[key] = [*merged.get(key, []), *(dict(entry) for entry in items)]
        row = await conn.fetchrow(
            """UPDATE agent_spawns
                  SET state = $2, child_record_id = COALESCE($3, child_record_id), spent_usd = spent_usd + $4,
                      result = $5::jsonb, inputs = COALESCE($6::jsonb, inputs), detail = COALESCE($7, detail),
                      rounds = rounds + $8, returns = returns + $9, updated_at = now(),
                      finished_at = CASE WHEN $10 THEN now() END
                WHERE id = $1
            RETURNING *""",
            spawn_id, to.value, child_record_id, spent_usd, json.dumps(merged),
            json.dumps(dict(inputs)) if inputs is not None else None, detail,
            1 if to is SpawnState.ASKING else 0, 1 if returned else 0, to in FINISHED)
    return _row(row)


async def note_run(spawn_id: int, record_id: int, *, pool: asyncpg.Pool | None = None) -> None:
    """A run the spawn started (its ``llm_records`` id, under ``result.runs``), so what it spends can be booked even
    when the spawn is stopped while it runs."""
    pool = pool or get_pool()
    await pool.execute(
        """UPDATE agent_spawns SET result = jsonb_set(result, '{runs}', COALESCE(result->'runs', '[]') || to_jsonb($2::bigint))
            WHERE id = $1 AND NOT COALESCE(result->'runs', '[]') @> to_jsonb($2::bigint)""", spawn_id, record_id)


async def book(spawn_id: int, record_id: int, cost_usd: float, *, pool: asyncpg.Pool | None = None) -> bool:
    """Add what run ``record_id`` spent to the spawn's spend, once per run (``result.booked``); False when it was
    booked before."""
    if cost_usd < 0:
        raise ValueError("cost_usd cannot be negative")
    pool = pool or get_pool()
    status = await pool.execute(
        """UPDATE agent_spawns SET spent_usd = spent_usd + $3, updated_at = now(),
                  result = jsonb_set(result, '{booked}', COALESCE(result->'booked', '[]') || to_jsonb($2::bigint))
            WHERE id = $1 AND NOT COALESCE(result->'booked', '[]') @> to_jsonb($2::bigint)""",
        spawn_id, record_id, cost_usd)
    return status == "UPDATE 1"


async def usage(task_id: str, step_id: str | None, *, kind: str = "advisor",
                pool: asyncpg.Pool | None = None) -> tuple[int, int, float]:
    """A task's spawns of ``kind``: how many it has, how many of them are about ``step_id``, and what they spent."""
    pool = pool or get_pool()
    row = await pool.fetchrow(
        "SELECT count(*) AS task, count(*) FILTER (WHERE step_id = $2) AS step, COALESCE(sum(spent_usd), 0) AS spent "
        "FROM agent_spawns WHERE task_id = $1 AND kind = $3", task_id, step_id, kind)
    return int(row["task"]), int(row["step"]), float(row["spent"])


async def has_source(step_id: str, source: str, *, pool: asyncpg.Pool | None = None) -> bool:
    """Whether the step has a spawn that ``source`` started (a checkpoint's, whatever became of it)."""
    pool = pool or get_pool()
    return bool(await pool.fetchval("SELECT EXISTS (SELECT 1 FROM agent_spawns WHERE step_id = $1 "
                                    "AND brief->>'source' = $2)", step_id, source))


async def asked(task_id: str, step_id: str | None, *, kind: str | None = None, evidence: str,
                states: tuple[str, ...] = (), pool: asyncpg.Pool | None = None) -> bool:
    """Whether the task has a consult about ``step_id`` (None: the plan) asked with ``evidence``, of ``kind`` when
    given, in one of ``states`` when given."""
    pool = pool or get_pool()
    return bool(await pool.fetchval(
        "SELECT EXISTS (SELECT 1 FROM agent_spawns WHERE task_id = $1 AND step_id IS NOT DISTINCT FROM $2 "
        "AND brief->>'evidence' = $3 AND ($4::text IS NULL OR brief->>'asked' = $4) "
        "AND (cardinality($5::text[]) = 0 OR state = ANY($5::text[])))",
        task_id, step_id, evidence, kind, list(states)))
