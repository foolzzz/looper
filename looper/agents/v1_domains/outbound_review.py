"""The outbound review's system setting (``outbound_hook.ENABLED_KEY``): whether every tool call of the Managers is
checked before it runs, and which enabled Managers run on a runtime the review cannot hook; and the operator's
declarations of the MCP tools that act only inside (``mcp_tool_levels``), which run without a review."""

import base64
import binascii
from datetime import datetime, timezone
import json
import re

from .. import outbound_hook
from ..action_levels import ActionLevel
from ..api_v1 import Operation, _array, _object, _summary, _BOOL, _INT, _NULL_STR, _STR, validate_arguments
from ..ops import ErrorCode, OpError
from ..outbound import Failure, Source

_MANAGER = _object({"manager_id": _STR, "name": _STR, "runtime": _STR}, closed=True)
SETTINGS = _object({"enabled": _BOOL, "managers_without_hook": _array(_MANAGER)}, closed=True)
DECLARED = _object({"item_id": _STR, "inside_tools": _array(_STR)}, closed=True)
REVIEW_ROW = _object({
    "id": _STR, "created_at": {**_STR, "format": "date-time"}, "agent_id": _STR, "task_id": _NULL_STR,
    "parent_record_id": _NULL_STR, "record_id": _NULL_STR,
    "source": {"type": "string", "enum": [source.value for source in Source]},
    "tool": _STR, "action": _NULL_STR, "level": {"type": "string", "enum": [level.value for level in ActionLevel]},
    "target_kind": _STR, "target": _STR, "summary": _STR, "verdict": {"type": "string", "enum": ["allow", "block"]},
    "failure": {**_NULL_STR, "enum": [None, *(failure.value for failure in Failure)]},
    "reason": _STR, "signals": _array(_STR), "profile_id": _NULL_STR, "model": _NULL_STR,
    "cost_usd": {"type": ["number", "null"]}, "latency_ms": {"type": ["integer", "null"]}}, closed=True)
REVIEWS = _object({"items": _array(REVIEW_ROW), "next_cursor": _NULL_STR}, closed=True)
SCHEMAS = {"OutboundReviewSettings": SETTINGS, "McpToolLevels": DECLARED, "OutboundReviewRow": REVIEW_ROW,
           "OutboundReviewPage": REVIEWS}
RESULTS = {}


def _op(name, description, request, result, risk="read"):
    return Operation(name, "POST", "/api/v1/" + name.replace(".", "/"), _summary(description), description,
                     request, result, risk=risk, confirms=risk == "high", operator_only=True,
                     level=ActionLevel.INSIDE)  # reads or changes only Looper's review settings and MCP trust declarations


OPERATIONS = (
    _op("outbound_review.get", "Read whether the outbound review is on, and the enabled Managers it cannot hook. "
                               "The review cannot be turned on while an enabled Manager's runtime has no tool hook.",
        _object({}, closed=True), SETTINGS),
    _op("outbound_review.update", "Turn the outbound review on or off. On, each tool call of a Manager that may "
                                  "leave this machine is checked by the outbound_check role first.",
        _object({"enabled": _BOOL}, ["enabled"], closed=True), SETTINGS, "high"),
    _op("outbound_review.list", "Read a bounded page of outbound review audit facts. Filter by verdict or task; "
                               "the cursor continues the same filters, newest first.",
        _object({"verdict": {"type": "string", "enum": ["allow", "block"]},
                 "task_id": {**_STR, "minLength": 1, "maxLength": 200},
                 "cursor": {**_STR, "minLength": 1, "maxLength": 2048},
                 "limit": {**_INT, "minimum": 1, "maximum": 100}}, [], closed=True), REVIEWS),
    _op("mcp_tool_levels.get", "Read the tools of an MCP server declared inside. Their calls run without the "
                               "outbound review; every other tool of the server is reviewed.",
        _object({"item_id": _STR}, ["item_id"], closed=True), DECLARED),
    _op("mcp_tool_levels.put", "Declare exactly these tools of an MCP server inside, acting only on this machine. "
                               "Their calls run without the outbound review.",
        _object({"item_id": _STR, "inside_tools": {**_array(_STR), "maxItems": 200}}, ["item_id", "inside_tools"],
                closed=True), DECLARED, "high"),
)


def _checked(operation, arguments, principal):
    if principal.kind != "operator":
        raise OpError(ErrorCode.FORBIDDEN, "only the operator can manage outbound review settings and audit")
    return validate_arguments(operation.request, arguments)


def _encode_cursor(row, *, verdict=None, task_id=None):
    timestamp = datetime.fromisoformat(row["created_at"]).astimezone(timezone.utc).isoformat()
    payload = {"v": 1, "created_at": timestamp, "id": row["id"], "verdict": verdict, "task_id": task_id}
    return base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode().rstrip("=")


def _decode_cursor(cursor, *, verdict=None, task_id=None):
    def unique_fields(pairs):
        result = dict(pairs)
        if len(result) != len(pairs):
            raise ValueError
        return result

    try:
        if not isinstance(cursor, str) or not 1 <= len(cursor) <= 2048 \
                or re.fullmatch(r"[A-Za-z0-9_-]+={0,2}", cursor) is None:
            raise ValueError
        raw = base64.b64decode(cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True)
        payload = json.loads(raw.decode("utf-8"), object_pairs_hook=unique_fields)
        if not isinstance(payload, dict) or set(payload) != {"v", "created_at", "id", "verdict", "task_id"} \
                or type(payload["v"]) is not int or payload["v"] != 1 \
                or payload["verdict"] != verdict or payload["task_id"] != task_id:
            raise ValueError
        identifier = payload["id"]
        if not isinstance(identifier, str) or re.fullmatch(r"[1-9][0-9]{0,18}", identifier) is None \
                or int(identifier) > 9223372036854775807:
            raise ValueError
        timestamp = datetime.fromisoformat(payload["created_at"])
        if timestamp.tzinfo is None or timestamp.utcoffset() != timezone.utc.utcoffset(timestamp):
            raise ValueError
        return timestamp, int(identifier)
    except (ValueError, TypeError, KeyError, binascii.Error, UnicodeDecodeError, RecursionError):
        raise OpError(ErrorCode.INVALID, "invalid outbound review cursor or mismatched filters") from None


async def _list(arguments):
    from ...db import outbound_reviews

    verdict, task_id = arguments.get("verdict"), arguments.get("task_id")
    before = _decode_cursor(arguments["cursor"], verdict=verdict, task_id=task_id) if "cursor" in arguments else None
    limit = arguments.get("limit", 50)
    try:
        rows = await outbound_reviews.list_reviews(verdict=verdict, task_id=task_id, before=before, limit=limit)
    except Exception:
        raise OpError(ErrorCode.UNAVAILABLE, "outbound review audit is unavailable") from None
    items = rows[:limit]
    cursor = _encode_cursor(items[-1], verdict=verdict, task_id=task_id) if len(rows) > limit else None
    return {"items": items, "next_cursor": cursor}


async def impact(server, operation, arguments, principal, headers):
    arguments = _checked(operation, arguments, principal)
    if operation.operation_id == "mcp_tool_levels.put":
        tools = ", ".join(arguments["inside_tools"]) or "none"
        return (f"Declare these tools of MCP server {arguments['item_id']!r} inside: {tools}? Their calls run without "
                "the outbound review; every other tool of the server is reviewed.")
    if operation.operation_id != "outbound_review.update":
        return None
    if arguments["enabled"]:
        return ("Turn the outbound review on? Every shell command and outbound call of the Managers is checked by "
                "the outbound_check role before it runs (a few seconds and about 0.003 USD each); a call it cannot "
                "judge is blocked. Running Managers restart on their next turn.")
    return ("Turn the outbound review off? The Managers' shell commands and outbound calls run without a check, and "
            "running Managers restart on their next turn.")


async def run(server, operation, arguments, principal, headers):
    from ...db import mcp_tool_levels

    arguments = _checked(operation, arguments, principal)
    if operation.operation_id == "outbound_review.list":
        return await _list(arguments)
    if operation.operation_id == "outbound_review.get":
        return await outbound_hook.settings()
    try:
        if operation.operation_id == "mcp_tool_levels.get":
            return await mcp_tool_levels.get(arguments["item_id"])
        if operation.operation_id == "mcp_tool_levels.put":
            return await mcp_tool_levels.put(arguments["item_id"], arguments["inside_tools"])
        settings = await outbound_hook.set_enabled(arguments["enabled"])
        if arguments["enabled"] and getattr(server, "_managers", None) is not None:
            # A Manager process started without the hook stops now: its next turn starts one with it.
            await server._managers.require_tool_hooks()
        return settings
    except outbound_hook.HookUnavailable as exc:
        raise OpError(ErrorCode.CONFLICT, str(exc)) from None
    except mcp_tool_levels.NoSuchServer as exc:
        raise OpError(ErrorCode.NOT_FOUND, str(exc)) from None
    except ValueError as exc:
        raise OpError(ErrorCode.INVALID, str(exc)) from None
