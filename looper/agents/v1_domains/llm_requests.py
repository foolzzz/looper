"""Operator-only request recording settings and captured exchanges."""

import logging
from datetime import datetime, timezone

from looper.db import llm_requests, system_settings
from looper.db.connection import get_pool
from ..api_v1 import Operation, _array, _object, _STR, _NULL_STR, _INT
from ..llmrec.control import controller
from ..llmrec import settings as retention
from ..ops import ErrorCode, OpError
from ..ops.service import _plain

logger = logging.getLogger(__name__)

_NULL_INT = {"type": ["integer", "null"]}
_NULL_OBJECT = {"type": ["object", "null"]}
_BOOL = {"type": "boolean"}
PROXY = _object({"state": {"type": "string", "enum": ["running", "stopped", "unavailable", "outdated"]},
    "port": _NULL_INT, "pid": _NULL_INT,
    **dict.fromkeys(("requests", "recorded", "gaps", "dropped", "write_errors"), _INT),
    "gap_reasons": {"type": "object", "additionalProperties": _INT}}, closed=True)


def _setting(value):
    return _object({"value": value, "source": {"type": "string", "enum": ["setting", "built-in"]},
                    "built_in": value, "minimum": value, "maximum": value}, closed=True)


SETTINGS = _object({"enabled": _BOOL, "include_oauth": _BOOL, "proxy": PROXY,
    "retention_days": _setting(_INT), "max_total_mb": _setting({"type": "number"}),
    "stored_bytes": _INT, "request_count": _INT}, closed=True)
_FIELDS = {
    "id": _INT, "route_id": _STR, "proxy_instance": _STR, "proxy_seq": _INT,
    "record_id": _NULL_INT, "after_record_id": _NULL_INT, "agent_id": _STR, "task_id": _NULL_STR,
    "started_at": _STR, "method": _STR, "path": _STR,
    "kind": {"type": "string", "enum": ["model", "count_tokens", "other"]}, "model": _NULL_STR,
    "stream": {"type": ["boolean", "null"]}, "status": _NULL_INT,
    "outcome": {"type": "string", "enum": ["completed", "upstream_error", "client_closed", "proxy_error"]},
    "error": _NULL_STR, "ttfb_ms": _NULL_INT, "duration_ms": _NULL_INT,
    "request_headers": {"type": "object"}, "response_headers": _NULL_OBJECT,
    "request_bytes": _INT, "request_sha256": _STR, "response_bytes": _NULL_INT,
    **dict.fromkeys(("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens"), _NULL_INT),
    "usage": _NULL_OBJECT, "record_gap": _NULL_STR, "body_sanitized": _BOOL,
}
ROW = _object(_FIELDS, closed=True)
ROUTE = _object({"id": _STR, "agent_id": _STR, "task_id": _NULL_STR, "step_id": _NULL_STR,
    "role": _NULL_STR, "runtime_type": _STR, "provider_id": _NULL_STR, "protocol": _NULL_STR,
    "model": _NULL_STR, "upstream_host": _STR, "upstream_path": _STR}, closed=True)
FULL = _object({**_FIELDS, "request_body": {}, "response_body": {}, "response_raw": _NULL_STR,
    "new_from_index": _NULL_INT, "route": ROUTE}, closed=True)
SCHEMAS = {"LlmRequest": FULL, "LlmRequestRow": ROW, "LlmrecSettings": SETTINGS}
RESULTS = {}


def _op(name, fields, result, required=None, risk="read", hand=False, confirms=False):
    description = {"llmrec_settings.get": "Read model request recording settings and proxy health.",
        "llmrec_settings.update": "Set recording for future launches. Bodies can contain prompts, code and tool output.",
        "llm_requests.list": "List captured request metadata without bodies.",
        "llm_requests.get": "Read a captured exchange with the complete reconstructed request.",
        "llm_requests.export": "Export complete captured exchanges in id-ordered pages.",
        "llm_requests.purge": "Permanently delete recorded model requests in one scope."}[name]
    return Operation(name, "POST", "/api/v1/" + name.replace(".", "/"), description, description,
                     _object(fields, required, closed=True), result, risk=risk, operator_only=True, hand=hand, confirms=confirms)


OPERATIONS = (
    _op("llmrec_settings.get", {}, SETTINGS),
    _op("llmrec_settings.update", {"enabled": _BOOL, "include_oauth": _BOOL,
        "retention_days": {"type": ["integer", "null"]},
        "max_total_mb": {"type": ["number", "null"]}}, SETTINGS, [], risk="write"),
    _op("llm_requests.purge", {"task_id": {"type": "string", "minLength": 1},
        "agent_id": {"type": "string", "minLength": 1}, "all": {"type": "boolean", "const": True}},
        _object({"requests": _INT, "routes": _INT}, closed=True), [], risk="high", confirms=True),
    _op("llm_requests.list", {"record_id": {"type": "integer", "minimum": 1},
        "agent_id": _STR, "task_id": _STR, "route_id": _STR, "between_turns": _BOOL,
        "before_id": {"type": "integer", "minimum": 1},
        "limit": {"type": "integer", "minimum": 1, "maximum": 500}},
        _object({"requests": _array(ROW)}, closed=True), []),
    _op("llm_requests.export", {"task_id": {"type": "string", "minLength": 1},
        "agent_id": {"type": "string", "minLength": 1}, "record_id": {"type": "integer", "minimum": 1},
        "since": _STR, "until": _STR, "after_id": {"type": "integer", "minimum": 1},
        "limit": {"type": "integer", "minimum": 1, "maximum": 50}},
        _object({"requests": _array(FULL), "next_after_id": _NULL_INT}, closed=True), [], hand=True),
    _op("llm_requests.get", {"request_id": {"type": "integer", "minimum": 1}},
        _object({"request": FULL}, closed=True)),
)


async def settings():
    if (control := controller()) is not None:
        result = await control.status()
    else:
        values = await system_settings.get_settings(["llmrec.enabled", "llmrec.include_oauth"])
        enabled = values.get("llmrec.enabled") == "true"
        result = {"enabled": enabled, "include_oauth": values.get("llmrec.include_oauth") == "true",
            "proxy": {"state": "unavailable" if enabled else "stopped", "port": None, "pid": None,
                      "requests": 0, "recorded": 0, "gaps": 0, "gap_reasons": {}, "dropped": 0, "write_errors": 0}}
    async with get_pool().acquire() as conn, conn.transaction():
        result.update(await retention.settings_view(conn))
        result["stored_bytes"] = await llm_requests.stored_bytes(conn)
        result["request_count"] = await llm_requests.count_requests(conn)
    return result


def _purge_scope(arguments):
    if len(arguments) != 1 or not (arguments.get("task_id") or arguments.get("agent_id") or arguments.get("all") is True):
        raise OpError(ErrorCode.INVALID, "provide exactly one of task_id, agent_id or all: true")
    return {"everything" if key == "all" else key: value for key, value in arguments.items()}


async def impact(server, operation, arguments, principal, headers):
    if operation.operation_id != "llm_requests.purge":
        return None
    scope = _purge_scope(arguments)
    async with get_pool().acquire() as conn, conn.transaction():
        count = await llm_requests.count_requests(conn, **{key: value for key, value in scope.items()
                                                          if key != "everything"})
    label = (f"task {arguments['task_id']}" if "task_id" in arguments else
             f"agent {arguments['agent_id']}" if "agent_id" in arguments else "all agents and tasks")
    return f"Permanently delete {count} recorded model requests for {label}? This cannot be undone."


async def run(server, operation, arguments, principal, headers):
    try:
        match operation.operation_id:
            case "llmrec_settings.get":
                result = await settings()
            case "llmrec_settings.update":
                if not arguments:
                    raise OpError(ErrorCode.INVALID, "provide enabled, include_oauth, retention_days or max_total_mb")
                try:
                    retention.checked({key: value for key, value in arguments.items() if key in retention.FIELDS})
                except ValueError as error:
                    raise OpError(ErrorCode.INVALID, str(error)) from None
                await system_settings.put_settings({"llmrec." + key: None if value is None else str(value).lower()
                                                    for key, value in arguments.items()})
                if (control := controller()) is not None:
                    await control.set_enabled(arguments.get("enabled", control.enabled),
                                              include_oauth=arguments.get("include_oauth"))
                result = await settings()
            case "llm_requests.purge":
                scope = _purge_scope(arguments)
                async with get_pool().acquire() as conn, conn.transaction():
                    result = await llm_requests.purge(conn, **scope)
            case "llm_requests.list":
                if arguments.get("between_turns") and not arguments.get("record_id"):
                    raise OpError(ErrorCode.INVALID, "between_turns requires record_id")
                result = {"requests": await llm_requests.list_requests(**arguments)}
            case "llm_requests.export":
                timestamps = {}
                for key in ("since", "until"):
                    if key in arguments:
                        try:
                            value = datetime.fromisoformat(arguments[key])
                        except ValueError:
                            raise OpError(ErrorCode.INVALID, f"{key} must be an ISO 8601 timestamp") from None
                        timestamps[key] = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
                if len(timestamps) == 2 and timestamps["until"] <= timestamps["since"]:
                    raise OpError(ErrorCode.INVALID, "until must be after since")
                try:
                    result = await llm_requests.export_page(**arguments)
                except ValueError as error:
                    raise OpError(ErrorCode.INVALID, str(error)) from None
            case _:
                row = await llm_requests.get_request(arguments["request_id"])
                if row is None:
                    raise OpError(ErrorCode.NOT_FOUND, "model request not found")
                result = {"request": row}
    except llm_requests.MissingRequestPart as error:
        raise OpError(ErrorCode.INTERNAL, str(error)) from None
    except OpError:
        raise
    except Exception as error:
        logger.warning("request recording operation failed operation=%s error_type=%s",
                       operation.operation_id, type(error).__name__)
        raise OpError(ErrorCode.UNAVAILABLE, "request recording unavailable") from None
    return _plain(result)
