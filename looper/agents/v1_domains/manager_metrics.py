"""Read-only Manager turn and proactive-delivery measurements."""
from datetime import datetime, timedelta, timezone

from ...db import manager_actions, proactive_deliveries
from ..channels import outbound_metrics
from ...db.connection import get_pool
from .. import chat_settings
from ..action_levels import ActionLevel
from ..api_v1 import Operation, _array, _object, _BOOL, _INT, _NULL_STR, _STR
from .. import chat_scope_metrics

_COUNTS = {"type": "object", "additionalProperties": _INT}
_NUMBER = {"type": ["number", "null"]}
_PERCENTILES = _object({"p50_ms": _NUMBER, "p95_ms": _NUMBER}, closed=True)
_LATENCY = _object({**_PERCENTILES["properties"], "by_chat": {
    "type": "object", "additionalProperties": _PERCENTILES}}, closed=True)
_OUTBOUND_ROW = _object({"channel_id": _STR, "name": _STR, "queued": _INT,
    "backoffs_24h": _INT, "breaker_opens_24h": _INT, "failed_24h": _COUNTS, "uncertain_24h": _INT,
    "breaker_state": {"enum": ["closed", "open", "half_open", None]}}, closed=True)
_SCOPE_TURN = _object({"manager_id": _STR, "scope": _STR, "audience": _STR, "count": _INT}, closed=True)
_SCOPE_REFUSAL = _object({"manager_id": _STR, "capability": _STR, "principal_kind": _NULL_STR, "count": _INT},
                         closed=True)
_START_REFUSAL = _object({"manager_id": _STR, "runtime": _STR, "mode": _STR, "count": _INT}, closed=True)
_INVARIANT = _object({"manager_id": _STR, "where": {"enum": ["resolve", "launch", "session", "prepare"]},
                      "count": _INT}, closed=True)
_RETRY = _object({"manager_id": _STR, "count": _INT}, closed=True)
_OPENED = _object({"manager_id": _STR, "reason": {"enum": ["first", "scope_changed", "context_full"]},
                   "count": _INT}, closed=True)
_SINCE = _object({"started_at": _STR, "start_refusals": _array(_START_REFUSAL), "invariants": _array(_INVARIANT),
                  "resolution_retries": _array(_RETRY), "sessions_opened": _array(_OPENED)}, closed=True)
_RESIDENTS = _object({"total": _INT, "cap": _INT,
                      "by_manager": {"type": "object", "additionalProperties": _INT},
                      "max_wait_s_24h": {"type": "number"}}, closed=True)
_SCOPES = _object({"turns": _array(_SCOPE_TURN), "refusals": _array(_SCOPE_REFUSAL), "legacy_bindings": _INT,
                   "residents": _RESIDENTS, "since_start": _SINCE}, closed=True)
SUMMARY = _object({"enabled": _BOOL, "window_days": _INT,
    "outbound": {"oneOf": [_array(_OUTBOUND_ROW), _object({"enabled": {"const": False}}, closed=True)]},
    "actions": _object({"by_category": {"type": "object", "additionalProperties": _COUNTS}}, closed=True),
    "outbound_started_at": _STR, "outbound_since_start": _BOOL, "turns": _object({"by_delivery": _COUNTS}),
    "ack": _LATENCY, "first_reply": _LATENCY,
    "empty": _object({"count": _INT, "ratio": _NUMBER}),
    "proactive": _object({"per_day": _array(_object({"date": _STR, "by_source": _COUNTS})),
        "reply_rate": _object({"all": _NUMBER, "by_source": {"type": "object", "additionalProperties": _NUMBER}})}),
    "scopes": _SCOPES},
    closed=True)
RESULTS = {}
OPERATIONS = (
    Operation("manager_metrics.get", "POST", "/api/v1/manager_metrics/get",
        "Read Manager turn and proactive delivery metrics.",
        "Read Manager turn and proactive delivery metrics. "
        "Includes latency percentiles, delivery counts, proactive reply rates and write counts by category over "
        "1–30 days. Scoped turns, refusals, legacy bindings, residents and since-start counters are in scopes.",
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
        overall = next((row for row in rows if row["total"]), None)
        if overall is None:
            result[key] = {"p50_ms": None, "p95_ms": None, "by_chat": {}}
            continue
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
    result["scopes"] = await _scopes(days)
    return result


async def _scopes(days: int) -> dict:
    """Scoped measurements. Unscoped records have no ``scope`` key and stay out of ``turns``."""
    db = get_pool()
    turns = await db.fetch("""SELECT agent_id AS manager_id, metadata->'scope'->>'id' AS scope,
        metadata->'scope'->>'audience' AS audience, count(*) AS count FROM llm_records
        WHERE created_at >= now() - make_interval(days => $1) AND metadata ? 'scope'
          AND metadata->'turn_metrics'->>'origin' = 'user'
          AND metadata->'turn_metrics'->>'replayed' = 'false'
        GROUP BY 1, 2, 3 ORDER BY 1, 2, 3""", days)
    refusals = await db.fetch("""SELECT manager_id, result->>'capability' AS capability,
        result->>'principal_kind' AS principal_kind, count(*) AS count FROM manager_actions
        WHERE error_code = 'forbidden' AND result->>'reason' = 'out_of_scope'
          AND created_at >= now() - make_interval(days => $1)
        GROUP BY 1, 2, 3 ORDER BY 1, 2, 3""", days)
    legacy = await db.fetchval("SELECT count(*) FROM manager_chat_bindings WHERE default_scope = 'legacy'")
    return {
        "turns": [_counted(row, ("manager_id", "scope", "audience")) for row in turns],
        "refusals": [_counted(row, ("manager_id", "capability", "principal_kind")) for row in refusals],
        "legacy_bindings": int(legacy or 0),
        "residents": chat_scope_metrics.residents(),
        "since_start": chat_scope_metrics.view(),
    }


def _counted(row, fields: tuple[str, ...]) -> dict:
    return {field: row[field] for field in fields} | {"count": int(row["count"])}
