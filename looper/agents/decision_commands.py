"""``looper decisions replay``: replay a task's decision snapshots (the decision snapshot RFC) and compare each with
what was recorded. It reads PostgreSQL directly, so it works with the service stopped, and writes nothing.

Exit codes: 0 every snapshot replays to its recorded decision, 1 the task has none, 3 some differ (or cannot be
replayed), 6 the database is unusable.
"""

from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import Mapping, Sequence
from typing import Any

import asyncpg

from ..db import decision_snapshots
from .tiers.decisions import Replayed, replay

OK, EMPTY, DIFFERS, NO_DATABASE = 0, 1, 3, 6


def _labels(row: Mapping[str, Any]) -> dict[str, str]:
    return {step["id"]: step["key"] for step in row["inputs"].get("steps") or []}


def _summary(row: Mapping[str, Any], decision: Mapping[str, Any] | None) -> str:
    if not decision:
        return "-"
    if row["kind"] == "dispatch":
        labels = _labels(row)
        ready = ", ".join(f"{labels.get(item['step'], item['step'])} ({item['kind']})" for item in decision["ready"])
        start = ", ".join(labels.get(step, step) for step in decision["start"])
        return f"ready {ready or 'none'}; start {start or 'none'}"
    if "decision" not in decision:
        return f"allowed {', '.join(decision['allowed'])}"
    return f"{decision['decision']} (allowed {', '.join(decision['allowed'])})"


def _lines(rows: Sequence[Mapping[str, Any]], results: Sequence[Replayed]) -> list[str]:
    lines = []
    for row, result in zip(rows, results):
        step = f" {row['inputs'].get('key')} attempt {row['inputs'].get('attempt')}" if row["kind"] == "step_decision" \
            else ""
        head = f"#{row['id']} {row['created_at'].isoformat(timespec='seconds')} {row['kind']}{step}"
        if result.same:
            lines.append(f"{head}: same; {_summary(row, result.recorded)}")
            continue
        lines.append(f"{head}: DIFFERS{f' ({result.note})' if result.note else ''}")
        lines.append(f"  recorded: {_summary(row, result.recorded)}")
        lines.append(f"  replayed: {_summary(row, result.replayed)}")
    differ = sum(not result.same for result in results)
    lines.append(f"{len(results)} snapshot(s): {len(results) - differ} same, {differ} differ")
    return lines


async def _run(database_url: str, task_id: str, as_json: bool) -> tuple[int, str]:
    from ..db.connection import close_pool, init_pool

    try:
        pool = await init_pool(database_url, min_size=1, max_size=2)
    except (OSError, asyncpg.PostgresError, TimeoutError, ValueError) as error:
        return NO_DATABASE, f"cannot reach the database: {error}"
    try:
        if not await pool.fetchval("SELECT to_regclass('decision_snapshots') IS NOT NULL"):
            return NO_DATABASE, "the database has no decision snapshots table yet: start `looper serve` once to upgrade it"
        rows = await decision_snapshots.for_task(task_id, db=pool)
    except (OSError, asyncpg.PostgresError, TimeoutError) as error:
        return NO_DATABASE, f"the database failed: {error}"
    finally:
        await close_pool()
    if not rows:
        return EMPTY, (f"task {task_id} has no decision snapshots (they are written only while the system setting "
                       f"{decision_snapshots.ENABLED_KEY} is true)")
    results = [replay(row) for row in rows]
    code = OK if all(result.same for result in results) else DIFFERS
    if as_json:
        return code, json.dumps([{"id": row["id"], "kind": row["kind"], "step_id": row["step_id"],
                                  "created_at": row["created_at"].isoformat(), "same": result.same,
                                  "note": result.note, "recorded": result.recorded, "replayed": result.replayed}
                                 for row, result in zip(rows, results)], ensure_ascii=False, indent=2)
    return code, "\n".join(_lines(rows, results))


def run(args: Any, load_ops: Any) -> int:
    ops = load_ops(args)
    if ops is None:
        return NO_DATABASE
    code, text = asyncio.run(_run(ops.database_url, args.task, args.json))
    print(text, file=sys.stderr if code in (EMPTY, NO_DATABASE) else sys.stdout)
    return code
