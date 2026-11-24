"""Explicit operator controls for the user collector; never agent tools."""

from datetime import datetime
import re

from ...db import message_collection as store
from ...db.connection import get_pool
from ..action_levels import ActionLevel
from ..api_v1 import Operation, _array, _object, _BOOL, _INT, _STR, _NULL_STR
from ..channels.lark_cli import profile_name
from ..ops import ErrorCode, OpError

_ID = {"type": "string", "pattern": "^[1-9][0-9]{0,18}$"}
_GENERATION = {"type": "string", "pattern": "^(0|[1-9][0-9]{0,18})$"}
_SOURCE = {"type": "string", "minLength": 1, "maxLength": 120}
_MODE = {"type": "string", "enum": ["off", "bridge", "collect"]}
_FIELDS = {
    "lark_profile": _STR, "user_open_id": _NULL_STR, "collect_cron": _STR,
    "sweep_cycle_runs": {"type": "integer", "minimum": 1, "maximum": 288},
    "alerts_enabled": _BOOL, "alert_channel": _NULL_STR, "alert_chat": _NULL_STR,
}
_SETTINGS = _object(_FIELDS, closed=True)
_NULL_INT = {"type": ["integer", "null"]}
_COUNTERS = ("slices", "search_hits", "unindexed", "chats_pulled", "chats_failed", "threads_pulled",
             "threads_failed", "sweep_chats", "sweep_failed", "sweep_found", "events_new", "events_seen",
             "events_skipped", "skipped_by_deletion")
_RUN_FIELDS = {"run_id": _ID, "status": _STR, "started_at": _STR, "finished_at": _NULL_STR,
               "window_from": _NULL_STR, "window_to": _NULL_STR, "watermark_before": _NULL_STR,
               "watermark_after": _NULL_STR, "error": _NULL_STR, "error_code": _NULL_INT,
               **dict.fromkeys(_COUNTERS, _NULL_INT)}
_ALERT_META = _object({"run_id": _ID, "status": _STR, "error": _NULL_STR, "error_code": _NULL_INT,
                      "window_from": _NULL_STR, "window_to": _NULL_STR, "bridge_only": _INT, "collect_only": _INT,
                      "identity_conflicts": _INT, "partials": _INT}, [], closed=True)
_ALERT = _object({"id": _ID, "kind": _STR, "opened_at": _STR, "resolved_at": _NULL_STR,
                  "parity_session_id": {"type": ["string", "null"]}, "metadata": _ALERT_META,
                  "open_state": _STR, "recovery_state": _STR}, closed=True)
_PARITY_COUNTS = dict.fromkeys(("bridge", "collect", "intersection", "bridge_only", "collect_only",
                              "identity_conflicts"), _INT)
_PARITY = {"anyOf": [{"type": "null"}, _object({
    "version": {"const": 1}, "source": _STR, "session": _NULL_STR,
    "window": {"anyOf": [{"type": "null"}, _object({"from": _STR, "to": _STR}, closed=True)]},
    "baseline_seq": _NULL_STR, "state": {"type": "string", "enum": ["pending", "matched", "drift", "incomplete"]},
    "empty_window": _BOOL, "coverage": _object(dict.fromkeys(("total", "complete", "pending", "failed"), _INT), closed=True),
    "totals": _object({**_PARITY_COUNTS, "stored_only": _INT}, closed=True),
    "buckets": _array(_object({"chat_id": _STR, "hour": _STR, **_PARITY_COUNTS}, closed=True)),
    "samples": _object(dict.fromkeys(("bridge_only", "collect_only", "identity_conflicts"),
        {"type": "array", "items": _STR, "maxItems": 20}), closed=True), "reason": _NULL_STR,
    "next_cursor": _NULL_STR}, closed=True)]}
VIEW = _object({"source_id": _STR, "ingest_mode": _MODE, "generation": _GENERATION,
                "settings": _SETTINGS, "auth": _object({"state": _STR, "checked_at": _NULL_STR,
                "refresh_expires_at": _NULL_STR}, closed=True), "last_collect_ok_at": _NULL_STR,
                "collect_enabled_at": _NULL_STR, "latest_run": {"anyOf": [
                    _object(_RUN_FIELDS, closed=True), {"type": "null"}]},
                "debt": _object({"chats": _INT, "threads": _INT}, closed=True),
                "fallback_24h": _object({"events_new": _INT, "sweep_found": _INT,
                    "ratio": {"type": ["number", "null"]}}, closed=True),
                "parity": _PARITY, "alerts": _array(_ALERT)}, closed=True)
MODE_RESULT = _object({"source_id": _STR, "mode": _MODE, "generation": _GENERATION,
                       "parity_session_id": {"type": ["string", "null"]}}, closed=True)
RESULTS = {}
OPERATIONS = (
    # All configuration and reports remain inside Looper's own state.
    Operation("message_collection.get", "POST", "/api/v1/message_collection/get",
              "Read collector settings, authentication, parity and alerts.",
              "Read collector settings, authentication, parity and alerts. Returns states and counts only.",
              _object({"source_id": _SOURCE}, closed=True), VIEW, risk="read", operator_only=True,
              level=ActionLevel.INSIDE),
    Operation("message_collection.update", "POST", "/api/v1/message_collection/update",
              "Configure collection without changing the ingest mode.",
              "Configure collection without changing the ingest mode. Alert targets must be the operator's private chat.",
              _object({"source_id": _SOURCE, "fields": _object(_FIELDS, [], closed=True)}, closed=True),
              VIEW, risk="high", confirms=True, operator_only=True, level=ActionLevel.INSIDE),
    Operation("message_collection.mode_update", "POST", "/api/v1/message_collection/mode_update",
              "Switch the message source's ingest mode explicitly.",
              "Switch the message source's ingest mode explicitly. Collect requires confirmation that external polling stopped.",
              _object({"source_id": _SOURCE, "ingest_mode": _MODE, "expected_generation": _GENERATION,
                       "bridge_stopped": _BOOL}, ["source_id", "ingest_mode"], closed=True),
              MODE_RESULT, risk="high", confirms=True, operator_only=True, level=ActionLevel.INSIDE),
    # Starts a local read-only CLI process through the same service scheduler.
    Operation("message_collection.run_now", "POST", "/api/v1/message_collection/run_now",
              "Start one collection pass without queuing overlapping work.",
              "Start one collection pass without queuing overlapping work. Acknowledges its run before account reads finish.",
              _object({"source_id": _SOURCE}, closed=True),
              _object({"source_id": _STR, "accepted": {"const": True}, "generation": _GENERATION, "run_id": _ID},
                      closed=True), risk="write", operator_only=True, level=ActionLevel.MACHINE),
)


def _time(value):
    return value.isoformat() if isinstance(value, datetime) else value


async def _view(source_id):
    from ...db import message_stream_alerts as alerts
    from ..message_stream_parity import read_report

    source = await store.collector_source(source_id)
    if source is None:
        raise OpError(ErrorCode.NOT_FOUND, "no such message source")
    pool = get_pool()
    last = await pool.fetchrow("SELECT * FROM source_runs WHERE source_id=$1 AND mode='collect' ORDER BY id DESC LIMIT 1",
                               source_id)
    run = None if last is None else {key: _time(last[key]) for key in _RUN_FIELDS if key != "run_id"}
    if run is not None:
        run["run_id"] = str(last["id"])
    chats = await pool.fetchval("SELECT count(*) FROM source_chats WHERE source_id=$1 AND inclusion='included' "
                                "AND failures>0 AND purged_at IS NULL", source_id)
    threads = await pool.fetchval("SELECT count(*) FROM source_threads WHERE source_id=$1 AND failures>0", source_id)
    try:
        parity = await read_report(source_id)
        if parity["session"] is None:
            parity = None
    except LookupError:
        parity = None
    active, _ = await alerts.list_active(pool, source_id=source_id)
    alert_views = [alerts.view(row) for row in active]
    return {"source_id": source_id, "ingest_mode": source["ingest_mode"], "generation": str(source["generation"]),
            "settings": {key: source.get(key) for key in _FIELDS},
            "auth": {"state": source["auth_state"], "checked_at": _time(source["auth_checked_at"]),
                     "refresh_expires_at": _time(source["refresh_expires_at"])},
            "last_collect_ok_at": _time(source["last_collect_ok_at"]),
            "collect_enabled_at": _time(source["collect_enabled_at"]),
            "latest_run": run, "debt": {"chats": chats, "threads": threads},
            "fallback_24h": {"events_new": source["new_ids_last_day"], "sweep_found": source["sweep_ids_last_day"],
                             "ratio": source["unindexed_ratio"]}, "parity": parity, "alerts": alert_views}


async def _check_fields(fields, source):
    from .. import cron, operator_identity, subscriptions
    from ..message import builtin_channel_id, parse_channel_id

    if "lark_profile" in fields:
        profile_name(fields["lark_profile"])
    if fields.get("user_open_id") is not None and not re.fullmatch(r"[A-Za-z0-9_.:-]{1,120}", fields["user_open_id"]):
        raise ValueError("invalid user_open_id")
    if "collect_cron" in fields:
        cron.parse(fields["collect_cron"])
    merged = {**source, **fields}
    channel, chat = merged.get("alert_channel"), merged.get("alert_chat")
    if bool(channel) != bool(chat):
        raise ValueError("alert_channel and alert_chat must both be set or both be empty")
    if channel:
        channel_id = channel if parse_channel_id(channel) is not None else builtin_channel_id(channel)
        parsed = parse_channel_id(channel_id)
        if parsed is None or parsed.channel_type not in ("lark", "lark_bot", "telegram"):
            raise ValueError("unsupported alert channel")
        operator = await operator_identity.operator_sender_id(channel_id)
        if not operator or chat != operator:
            raise ValueError("alert target must be the operator's own direct chat")
        if parsed.channel_type != "telegram":
            subscriptions.check_private_target({"chat_kinds": ["p2p"]}, parsed.channel_type, chat, operator)


async def run(server, operation, arguments, principal, headers):
    from ..message_collect import CollectorError
    source_id = arguments["source_id"]
    source = await store.collector_source(source_id)
    if source is None:
        raise OpError(ErrorCode.NOT_FOUND, "no such message source")
    action = operation.operation_id.rsplit(".", 1)[-1]
    try:
        if action == "get":
            return await _view(source_id)
        if action == "update":
            fields = arguments["fields"]
            await _check_fields(fields, source)
            await store.save_settings(source_id, fields)
            return await _view(source_id)
        if action == "mode_update":
            from ..source_service import own_bot_app_ids
            result = await store.switch_mode(source_id, arguments["ingest_mode"],
                expected_generation=int(arguments["expected_generation"]) if "expected_generation" in arguments else None,
                bridge_stopped=arguments.get("bridge_stopped", False), own_app_ids=await own_bot_app_ids())
            return {**result, "generation": str(result["generation"]),
                    "parity_session_id": str(result["parity_session_id"]) if result["parity_session_id"] is not None else None}
        service = getattr(server, "_source_service", None)
        if source["ingest_mode"] != "collect":
            raise OpError(ErrorCode.CONFLICT, "source is not in collect mode")
        if service is None:
            raise OpError(ErrorCode.UNAVAILABLE, "collection service is unavailable")
        result = await service.run_now(source_id)
        return {**result, "generation": str(result["generation"]), "run_id": str(result["run_id"])}
    except store.CollectionConflict as exc:
        raise OpError(ErrorCode.CONFLICT, str(exc)) from None
    except CollectorError as exc:
        raise OpError(ErrorCode.CONFLICT, "collection could not start: " + exc.kind.value) from None
    except TimeoutError:
        raise OpError(ErrorCode.CONFLICT, "collection start handshake timed out") from None
    except ValueError as exc:
        raise OpError(ErrorCode.INVALID, str(exc)) from None
    except LookupError:
        raise OpError(ErrorCode.NOT_FOUND, "no such message source") from None


async def impact(server, operation, arguments, principal, headers):
    source = await store.collector_source(arguments["source_id"])
    if source is None:
        return "The message source does not exist."
    if operation.operation_id.endswith("mode_update"):
        return (f"Source {source['id']}: {source['ingest_mode']} → {arguments['ingest_mode']}; "
                f"generation {source['generation']}, profile bound: {bool(source['lark_profile'])}. "
                f"Bridge reference configured: {bool(source['bridge_dir'])}. Collect freezes a 24-hour window and "
                "requires external polling to have stopped. Messages continue through existing consumers.")
    return "Update collector settings and the explicitly confirmed operator-private alert target; ingest mode stays unchanged."
