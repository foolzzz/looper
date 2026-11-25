"""Read-only Manager turn and proactive-delivery measurements."""
from datetime import datetime, timedelta, timezone
import json

from ...db import manager_actions, proactive_deliveries
from ..channels import outbound_metrics
from ...db.connection import get_pool
from .. import chat_settings
from ..action_levels import ActionLevel
from ..api_v1 import Operation, _array, _object, _BOOL, _INT, _STR

_COUNTS = {"type": "object", "additionalProperties": _INT}
_NUMBER = {"type": ["number", "null"]}
_PERCENTILES = _object({"p50_ms": _NUMBER, "p95_ms": _NUMBER}, closed=True)
_LATENCY = _object({**_PERCENTILES["properties"], "by_chat": {
    "type": "object", "additionalProperties": _PERCENTILES}}, closed=True)
_OUTBOUND_ROW = _object({"channel_id": _STR, "name": _STR, "queued": _INT,
    "backoffs_24h": _INT, "breaker_opens_24h": _INT, "failed_24h": _COUNTS, "uncertain_24h": _INT,
    "breaker_state": {"enum": ["closed", "open", "half_open", None]}}, closed=True)
SUMMARY = _object({"enabled": _BOOL, "window_days": _INT,
    "outbound": {"oneOf": [_array(_OUTBOUND_ROW), _object({"enabled": {"const": False}}, closed=True)]},
    "actions": _object({"by_category": {"type": "object", "additionalProperties": _COUNTS}}, closed=True),
    "outbound_started_at": _STR, "outbound_since_start": _BOOL, "turns": _object({"by_delivery": _COUNTS}),
    "ack": _LATENCY, "first_reply": _LATENCY,
    "empty": _object({"count": _INT, "ratio": _NUMBER}),
    "proactive": _object({"per_day": _array(_object({"date": _STR, "by_source": _COUNTS})),
        "reply_rate": _object({"all": _NUMBER, "by_source": {"type": "object", "additionalProperties": _NUMBER}})})},
    closed=True)
SUMMARY["properties"]["work_mode"] = _object({"task_manager_launch_refusals_total": _array(_object({
    "reason": {"enum": ["review_hook_disabled", "cli_unverified"]},
    "phase": {"enum": ["launch", "resume"]}, "cli_version": _STR, "count": _INT}, closed=True))}, closed=True)
_TASK_MEASUREMENT = _object({"task_id": _STR, "record_id": _INT,
    "model": {"type": ["string", "null"]}, "mode": {"enum": ["direct", "orchestrated"]},
    "maintenance": _BOOL, "value": _NUMBER}, closed=True)
_TASK_BOUND_COUNT = _array(_object({"reason": _STR, "result": {"enum": ["compacted", "rotated"]},
                                  "count": _INT}, closed=True))
SUMMARY["properties"]["work_mode"]["properties"].update({
    "task_manager_context_tokens": _array(_TASK_MEASUREMENT),
    "task_manager_turn_cost_usd": _array(_TASK_MEASUREMENT),
    "task_manager_compactions_total": _TASK_BOUND_COUNT,
    "task_manager_session_rotations_total": _TASK_BOUND_COUNT,
})
RESULTS = {}
OPERATIONS = (
    Operation("manager_metrics.get", "POST", "/api/v1/manager_metrics/get",
        "Read Manager turn and proactive delivery metrics.",
        "Read Manager turn and proactive delivery metrics. "
        "Includes latency percentiles, delivery counts, proactive reply rates and write counts by category over "
        "1–30 days.",
        _object({"days": {"type": "integer", "minimum": 1, "maximum": 30, "default": 7}}, [], closed=True),
        SUMMARY, risk="read", operator_only=True,
        level=ActionLevel.INSIDE),  # reads only Looper's stored turn and delivery metrics
)

# Use the agent/time index before looking inside the optional JSON key. Historical records without it do not count.
_TURNS = """WITH turns AS (
    SELECT metadata->'turn_metrics' AS m FROM llm_records
    WHERE agent_id = 'manager' AND created_at >= now() - make_interval(days => $1)
      AND metadata->'turn_metrics'->>'origin' = 'user'
      AND metadata->'turn_metrics'->>'replayed' = 'false'
) """


async def run(server, operation, arguments, principal, headers):
    days = arguments.get("days", 7)
    db = get_pool()
    counts = await db.fetch(_TURNS + "SELECT m->>'delivery' AS delivery, count(*) AS n FROM turns "
                            "WHERE m->>'delivery' IS NOT NULL GROUP BY 1", days)
    by_delivery = {row["delivery"]: row["n"] for row in counts}
    result = {"enabled": chat_settings.metrics_enabled(), "window_days": days,
              "turns": {"by_delivery": by_delivery}}
    for key, field in (("ack", "ack_ms"), ("first_reply", "first_reply_ms")):
        rows = await db.fetch(_TURNS + f"""SELECT m->>'chat' AS chat,
            percentile_cont(0.5) WITHIN GROUP (ORDER BY (m->>'{field}')::double precision) AS p50_ms,
            percentile_cont(0.95) WITHIN GROUP (ORDER BY (m->>'{field}')::double precision) AS p95_ms,
            grouping(m->>'chat') AS total FROM turns WHERE m->>'{field}' IS NOT NULL
            GROUP BY GROUPING SETS ((), (m->>'chat'))""", days)
        overall = next(row for row in rows if row["total"])
        result[key] = {"p50_ms": overall["p50_ms"], "p95_ms": overall["p95_ms"],
                       "by_chat": {row["chat"]: {"p50_ms": row["p50_ms"], "p95_ms": row["p95_ms"]}
                                   for row in rows if not row["total"]}}
    total, empty = sum(by_delivery.values()), by_delivery.get("empty", 0)
    result["empty"] = {"count": empty, "ratio": empty / total if total else None}
    result["actions"] = {"by_category": await manager_actions.count_by_category(days)}
    result["proactive"] = await proactive_deliveries.summary(days)
    result["outbound"] = await outbound_metrics.summary(server, db)
    result["outbound_started_at"] = server._started_at.isoformat()
    result["outbound_since_start"] = datetime.now(timezone.utc) - server._started_at < timedelta(days=1)
    from .. import task_conversations_settings, task_manager_safety
    if task_conversations_settings.enabled():
        result["work_mode"] = {"task_manager_launch_refusals_total": [
            {"reason": reason, "phase": phase, "cli_version": version, "count": count}
            for (reason, phase, version), count in sorted(task_manager_safety.refusal_counts.items())]}
        result["work_mode"].update(await _task_manager_metrics(db, days))
    return result


async def _task_manager_metrics(db, days):
    """Read turn-owned measurements and maintenance outcomes, never session carry-over values."""
    rows = await db.fetch("""SELECT id, task_id, model, context_tokens, cost_usd, metadata
        FROM llm_records WHERE agent_id LIKE 'task-manager-%' AND task_id IS NOT NULL
          AND created_at >= now() - make_interval(days => $1) AND status <> 'running'
          AND metadata->'task_manager' IS NOT NULL ORDER BY id""", days)
    result = {"task_manager_context_tokens": [], "task_manager_turn_cost_usd": [],
              "task_manager_compactions_total": [], "task_manager_session_rotations_total": []}
    compactions, rotations = {}, {}
    for row in rows:
        metadata = json.loads(row["metadata"]) if isinstance(row["metadata"], str) else row["metadata"]
        dimensions = {"task_id": row["task_id"], "record_id": row["id"], "model": row["model"],
                      "mode": metadata["task_manager"]["mode"],
                      "maintenance": metadata["task_manager"]["maintenance"]}
        result["task_manager_context_tokens"].append({**dimensions, "value": row["context_tokens"]})
        result["task_manager_turn_cost_usd"].append({**dimensions,
            "value": float(row["cost_usd"]) if row["cost_usd"] is not None else None})
        if compacted := metadata.get("task_manager_compaction"):
            key = compacted["reason"], compacted["result"]
            compactions[key] = compactions.get(key, 0) + 1
        if (bound := metadata.get("session_bound")) and bound.get("action") == "rotated":
            key = bound["reason"], bound["action"]
            rotations[key] = rotations.get(key, 0) + 1
    for metric, counts in (("task_manager_compactions_total", compactions),
                           ("task_manager_session_rotations_total", rotations)):
        result[metric] = [{"reason": reason, "result": outcome, "count": count}
                          for (reason, outcome), count in sorted(counts.items())]
    return result
