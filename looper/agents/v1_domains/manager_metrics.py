"""Read-only Manager turn and proactive-delivery measurements."""
from datetime import datetime, timedelta, timezone

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
    return result
