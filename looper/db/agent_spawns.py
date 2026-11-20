"""Runs another run starts (``agent_spawns``, the advisor RFC, section 5.2): one row per consult of the advisor.

A row's state moves only along ``TRANSITIONS``, and only from the state its caller expects (``move``): a row another
caller moved first is left as it is, and ``move`` returns None. A finished row (``FINISHED``) has ``finished_at``
and never moves again. ``result`` holds typed outcomes only: each round, each check hit by category and position
(never what it matched), and why a row failed.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
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


async def for_task(task_id: str, *, pool: asyncpg.Pool | None = None) -> list[dict[str, Any]]:
    """The task's spawns, oldest first."""
    pool = pool or get_pool()
    return [_row(r) for r in await pool.fetch("SELECT * FROM agent_spawns WHERE task_id = $1 ORDER BY id", task_id)]


async def move(spawn_id: int, to: SpawnState, *, expect: SpawnState, child_record_id: int | None = None,
               spent_usd: float = 0.0, result: Mapping[str, Any] | None = None,
               append: Mapping[str, Mapping[str, Any]] | None = None, inputs: Mapping[str, Any] | None = None,
               detail: str | None = None, returned: bool = False,
               pool: asyncpg.Pool | None = None) -> dict[str, Any] | None:
    """Move the row from ``expect`` to ``to``; None, changing nothing, when it is no longer in ``expect``.

    With the move: ``spent_usd`` is added to what it spent; ``result``'s keys replace the result's, and each item of
    ``append`` is added to the list under its key; ``inputs`` replaces its inputs; ``detail`` replaces its detail; a
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
            merged[key] = [*merged.get(key, []), dict(item)]
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
