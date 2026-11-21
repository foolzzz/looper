"""The ``decision_snapshots`` table: what a task's engine saw when it decided (the decision snapshot RFC).

Rows are written only while the system setting ``decision_snapshots.enabled`` is ``true``; ``agents.tiers.decisions``
builds them and replays them. They go with their task, and housekeeping deletes them after ``RETENTION_DAYS``.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import datetime
from typing import Any

import asyncpg

from . import system_settings
from .connection import get_pool

Db = asyncpg.Pool | asyncpg.Connection

ENABLED_KEY = "decision_snapshots.enabled"
KINDS = ("dispatch", "step_decision")
RETENTION_DAYS = 30
PRUNE_BATCH = 1000


def _db(db: Db | None) -> Db:
    return db if db is not None else get_pool()


def _json(value: Mapping[str, Any]) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


async def enabled(*, db: Db | None = None) -> bool:
    """Whether snapshots are written (``decision_snapshots.enabled`` is ``true``); off without a row."""
    values = await system_settings.get_settings([ENABLED_KEY], **({"pool": db} if db is not None else {}))
    return values.get(ENABLED_KEY) == "true"


async def add(task_id: str, kind: str, *, inputs: Mapping[str, Any], decision: Mapping[str, Any],
              step_id: str | None = None, record_id: int | None = None, db: Db | None = None) -> int | None:
    """Write one snapshot and return its id. A dispatch whose inputs and decision equal the task's last dispatch
    snapshot's is not written again (None)."""
    if kind not in KINDS:
        raise ValueError(f"unknown decision snapshot kind {kind!r}")
    text = _json(inputs)
    return await _db(db).fetchval(
        "INSERT INTO decision_snapshots (task_id, kind, step_id, record_id, inputs, inputs_sha256, decision) "
        "SELECT $1, $2, $3, $4, $5::jsonb, $6, $7::jsonb WHERE $2 <> 'dispatch' OR NOT EXISTS ("
        "  SELECT 1 FROM (SELECT inputs_sha256, decision FROM decision_snapshots WHERE task_id = $1 "
        "  AND kind = 'dispatch' ORDER BY id DESC LIMIT 1) last "
        "  WHERE last.inputs_sha256 = $6 AND last.decision = $7::jsonb) RETURNING id",
        task_id, kind, step_id, record_id, text, hashlib.sha256(text.encode("utf-8")).hexdigest(), _json(decision))


async def for_task(task_id: str, *, db: Db | None = None) -> list[dict[str, Any]]:
    """A task's snapshots, oldest first, with ``inputs`` and ``decision`` parsed."""
    rows = await _db(db).fetch(
        "SELECT id, task_id, kind, step_id, record_id, inputs, inputs_sha256, decision, created_at "
        "FROM decision_snapshots WHERE task_id = $1 ORDER BY id", task_id)
    return [{**dict(row), "inputs": json.loads(row["inputs"]), "decision": json.loads(row["decision"])}
            for row in rows]


async def turn(record_id: int, *, db: Db | None = None) -> dict[str, Any] | None:
    """What a snapshot keeps of a turn's run record (``decisions.turn_digest`` reduces it); None without the row."""
    row = await _db(db).fetchrow(
        "SELECT id, metadata, input_tokens, context_tokens, cache_read_tokens, length(input_text) AS input_chars "
        "FROM llm_records WHERE id = $1", record_id)
    if row is None:
        return None
    metadata = row["metadata"]
    return {**dict(row), "metadata": json.loads(metadata) if isinstance(metadata, str) else metadata}


async def prune(conn: Db, before: datetime) -> int:
    """Delete one batch of snapshots written before ``before``; how many."""
    result = await conn.execute(
        "DELETE FROM decision_snapshots WHERE id IN (SELECT id FROM decision_snapshots WHERE created_at < $1 "
        "LIMIT $2)", before, PRUNE_BATCH)
    return int(result.split()[-1])
