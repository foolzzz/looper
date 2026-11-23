"""A task's persisted runs and their descendants, costs and tool waits.

This read never uses spawn reservations as costs: an unpriced record stays
unknown even when the spawn reserved or booked an amount for admission.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from decimal import Decimal
from typing import Any

from ..db.connection import get_pool
from ..db.llm_records import EQUIVALENT, run_kind_case


# Both names are emitted by supported normalized transcripts. Native runtime
# sub-agents' events have parent_tool_use_id and do not describe this run's wait.
_WAIT_TOOLS = ("looper_agent_wait", "mcp__looper__looper_agent_wait")


def _object(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        value = json.loads(value)
    return value if isinstance(value, dict) else {}


def _time(value: Any) -> str | None:
    return value.isoformat() if value is not None else None


def _own_cost(row: Mapping[str, Any]) -> dict[str, Decimal | None]:
    basis = "subscription_usd" if row["equivalent"] else "api_usd"
    return {key: (Decimal(str(row["cost_usd"])) if row["cost_usd"] is not None else None)
            if key == basis else Decimal(0) for key in ("subscription_usd", "api_usd")}


def _cost(costs: list[dict[str, Decimal | None]]) -> dict[str, float | None]:
    # Round only after adding, like task_costs' numeric aggregation, never a
    # binary float running total. Unknown propagates within its billing basis.
    return {key: None if any(cost[key] is None for cost in costs) else
            float(sum((cost[key] for cost in costs), Decimal(0)).quantize(Decimal("0.000001")))
            for key in ("subscription_usd", "api_usd")}


def _waits(events: list[Mapping[str, Any]]) -> dict[int, list[dict[str, Any]]]:
    calls: dict[tuple[int, str], dict[str, Any]] = {}
    result: dict[int, list[dict[str, Any]]] = {}
    for event in events:
        key = event["record_id"], event["call_id"]
        if event["block_type"] == "tool_use" and key not in calls:
            interval = {"started_at": _time(event["created_at"]), "ended_at": None}
            calls[key] = interval
            result.setdefault(event["record_id"], []).append(interval)
        elif event["block_type"] == "tool_result" and key in calls and calls[key]["ended_at"] is None:
            calls[key]["ended_at"] = _time(event["created_at"])
    return result


async def read(task_id: str, *, pool=None) -> dict[str, Any]:
    """Read every run of one task, traversing only its parent_record_id edges.

    UNION bounds malformed cycles, including historical ones, to one pair per
    ancestor/descendant. A parent or child from another task is never followed.
    """
    pool = pool or get_pool()
    rows = await pool.fetch(f"""
        WITH RECURSIVE task_runs AS MATERIALIZED (
            SELECT id, parent_record_id, status, created_at, finished_at,
                   cost_usd, failure_class, failure_cause, left(output_summary,300) AS output_summary,
                   COALESCE(run_kind, {run_kind_case('agent_id')}) AS kind,
                   {EQUIVALENT} AS equivalent,
                   COALESCE(model,metadata->'model_choice'->>'model',
                       COALESCE(metadata->'profile',metadata->'agent')->>'model') AS model,
                   jsonb_build_object('seat',metadata->'seat','step_key',metadata->'step_key',
                       'step_id',metadata->'step_id') AS metadata
            FROM llm_records WHERE task_id=$1
                AND COALESCE(run_kind, {run_kind_case('agent_id')}) IN
                    ('planner','worker','reviewer','advisor','advice_check','outbound_check','consult','explorer')
        ), descendants(root_id, member_id) AS (
            SELECT id, id FROM task_runs
            UNION
            SELECT d.root_id, child.id FROM descendants d
            JOIN task_runs child ON child.parent_record_id=d.member_id
        )
        SELECT r.*, spawn.row AS spawn,
               EXISTS(SELECT 1 FROM agent_spawns s WHERE s.task_id=$1 AND s.parent_record_id=r.id) AS spawn_parent,
               ARRAY(SELECT member_id FROM descendants WHERE root_id=r.id) AS descendants
        FROM task_runs r
        LEFT JOIN LATERAL (
            SELECT jsonb_build_object(
                'brief',jsonb_build_object('goal',s.brief->'goal'),
                'result',jsonb_build_object('summary',CASE WHEN jsonb_typeof(s.result->'summary')='string'
                    THEN left(s.result->>'summary',300) END,'verified',s.result->'verified'),
                'spawner',s.spawner,'state',s.state,'delivery',s.delivery,'cancel_reason',s.cancel_reason,
                'parent_spend_known',s.parent_spend_known,
                'failure_class',s.failure_class,'failure_cause',s.failure_cause) AS row
            FROM agent_spawns s
            WHERE s.task_id=$1 AND (s.child_record_id=r.id OR s.result->'runs' @> to_jsonb(r.id))
            ORDER BY (s.child_record_id=r.id) DESC NULLS LAST, s.id DESC LIMIT 1
        ) spawn ON true
        ORDER BY r.created_at, r.id
        """, task_id)
    if not rows:
        return {"task_id": task_id, "nodes": []}
    # Project only tool identity and timing; wait reads never load result
    # contents, prompts or ordinary tool arguments into the run-tree response.
    parent_ids = [row["id"] for row in rows if row["spawn_parent"]]
    events = await pool.fetch("""
        WITH blocks AS MATERIALIZED (
            SELECT e.record_id,e.seq,e.created_at,b->>'type' AS block_type,
                   b->>'name' AS name,
                   CASE WHEN b->>'type'='tool_use' THEN b->>'id' ELSE b->>'tool_use_id' END AS call_id
            FROM llm_record_events e
            CROSS JOIN LATERAL jsonb_array_elements(CASE
                WHEN jsonb_typeof(e.event->'message'->'content')='array'
                THEN e.event->'message'->'content' ELSE '[]'::jsonb END) b
            WHERE e.record_id=ANY($1::bigint[]) AND COALESCE(e.event->>'parent_tool_use_id','')=''
                  AND b->>'type' IN ('tool_use','tool_result')
        ), waits AS (
            SELECT DISTINCT record_id,call_id FROM blocks
            WHERE block_type='tool_use' AND name=ANY($2::text[]) AND call_id IS NOT NULL
        )
        SELECT b.* FROM blocks b JOIN waits w USING(record_id,call_id)
        WHERE b.block_type='tool_result' OR b.name=ANY($2::text[])
        ORDER BY b.record_id,b.seq
        """, parent_ids, list(_WAIT_TOOLS)) if parent_ids else []
    waits = _waits(events)
    records = {row["id"]: row for row in rows}
    own = {row["id"]: _own_cost(row) for row in rows}
    nodes = []
    for row in rows:
        spawn = _object(row["spawn"])
        brief, result = _object(spawn.get("brief")), _object(spawn.get("result"))
        parent = records.get(row["parent_record_id"])
        parent_metadata = _object(parent["metadata"]) if parent else {}
        role = parent["kind"] if parent else None
        seat = spawn.get("spawner") or parent_metadata.get("seat")
        if not seat and parent:
            step = parent_metadata.get("step_key") or parent_metadata.get("step_id")
            seat = f"{role}@{step}" if step and role in ("worker", "reviewer") else role
        if role is None and seat:
            role = seat.split("@", 1)[0]
        summary = result.get("summary")
        if not isinstance(summary, str):
            summary = row["output_summary"]
        nodes.append({
            "id": row["id"], "kind": row["kind"] or "unknown", "parent_record_id": parent["id"] if parent else None,
            "spawner": {"role": role, "seat": seat}, "reason": brief.get("goal") or row["kind"] or "unknown",
            "model": row["model"],
            "status": row["status"], "started_at": _time(row["created_at"]), "ended_at": _time(row["finished_at"]),
            "own_cost": _cost([own[row["id"]]]),
            "subtree_cost": _cost([own[record_id] for record_id in row["descendants"]]),
            "result_summary": summary[:300] if isinstance(summary, str) else None,
            "result_verified": result.get("verified") if isinstance(result.get("verified"), bool) else None,
            "failure_class": spawn.get("failure_class") or row["failure_class"],
            "failure_cause": spawn.get("failure_cause") or row["failure_cause"],
            **{key: spawn.get(key) for key in ("state", "delivery", "cancel_reason", "parent_spend_known")},
            "wait_intervals": waits.get(row["id"], []),
        })
    return {"task_id": task_id, "nodes": nodes}
