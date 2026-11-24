"""The dashboard status projection, without adding a model-facing Tool."""

import os
from datetime import datetime, timezone

from ... import __version__
from ..api_v1 import Operation, _array, _object, _STR, _NULL_STR, _INT, _BOOL

from .. import completion_service
from ..channels.lark_inbox import CatchupDropReason
from .drain import DRAIN

ORDER = 1

_AGENT = _object({"healthy": _BOOL, "state": _STR, "up": _BOOL, "restart_count": _INT,
                  "last_crash_at": _NULL_STR, "next_restart_at": _NULL_STR, "agent_type": _STR})
_BINDING = _object({
    "enforced": _BOOL, "mismatches": _INT,
    "by": _array(_object({"reason": _STR, "runtime": _STR, "tool": _STR, "count": _INT, "manager": _STR},
                         ["reason", "runtime", "tool", "count"])),
    "matches": _array(_object({"by": _STR, "when": _STR, "count": _INT}))}, ["enforced", "mismatches", "by"])
_CATCHUP_WINDOW = _object({
    "chat_id": _STR, "start": {"type": "number"}, "end": {"type": "number"},
    "state": {"type": "string", "enum": ["pending", "dropped"]}, "attempts": _INT,
    "reason": {"enum": [None, *(reason.value for reason in CatchupDropReason)]},
    "recorded_at": {"type": "number"}, "thread_id": _STR,
}, ["chat_id", "start", "end", "state", "attempts", "reason"])
_CHANNELS = _object({"web": _object({"channel": {"const": "web"}, "healthy": _BOOL,
                                   "active_sse_streams": _INT})})
_CHANNELS["additionalProperties"] = _object({
    "channel_id": _STR, "channel_type": _STR, "channel": _STR, "healthy": _BOOL,
    "active_conversations": _INT, "lark_cli_available": _BOOL, "transport": _STR,
    "catchup": _object({"pending": _INT, "dropped": _INT, "windows": _array(_CATCHUP_WINDOW)}),
}, ["channel_id", "channel_type", "channel", "healthy", "catchup"])
_STATUS = _object({
    "startup_steps": {"type": "object", "additionalProperties": {"type": "number", "minimum": 0}},
    "drain": DRAIN,
    "keepers": _object({"mode": {"const": "on"}, "attached": _INT, "candidates_at_start": _INT,
                        "adopted_at_start": _INT, "pending": _INT,
                        "ended_at_start": {"type": "object", "additionalProperties": _INT},
                        "failed_after_start": {"type": "object", "additionalProperties": _INT}}),
    "status": {"const": "running"}, "agents": {"type": "object", "additionalProperties": _AGENT},
    "tasks": _object({"total": _INT, "running": _INT, "completed": _INT}),
    "channels": _CHANNELS,
    "checks": _array(_object({"id": _STR, "label": _STR, "detail": _STR, "fix": _STR,
                               "status": {"type": "string", "enum": ["ok", "warn", "fail", "skip", "pending"]}})),
    "can_sign_out": _BOOL, "turn_binding": _BINDING,
    "managers": _array(_object({"id": _STR, "name": _STR, "running": _BOOL, "busy": _BOOL,
                                 "healthy": _BOOL, "sync": {"type": "string", "enum": ["pending", "ok"]},
                                 "sync_error": _STR, "turn_binding": _BINDING},
                                ["id", "name", "running", "busy", "healthy", "sync", "turn_binding"]))},
    ["status", "agents", "tasks", "channels", "checks", "can_sign_out"])

_COUNTS = {"type": "object", "additionalProperties": _INT}
# The Manager's completion turns (manager completion ledger RFC, section 6), as the completion service last read
# them (from memory: health never waits for the database); null before its first pass.
_COMPLETION = {"type": ["object", "null"], "properties": {
    "pending": _INT, "oldest_pending_seconds": {"type": ["number", "null"]}, "window_hours": _INT,
    "by_state": _COUNTS, "by_reason": _COUNTS, "turn_cost_usd": {"type": "number"}, "failing": _BOOL,
    "cost_status": {"type": "string", "enum": ["known", "unknown"]}, "unpriced_runs": _INT,
    "checked_at": _STR},
    "required": ["pending", "oldest_pending_seconds", "window_hours", "by_state", "by_reason", "turn_cost_usd",
                 "failing", "checked_at", "cost_status", "unpriced_runs"], "additionalProperties": False}
_HEALTH = _object({
    "status": {"const": "ok"}, "version": _STR, "pid": _INT, "started_at": _STR, "now": _STR,
    "completion": _COMPLETION,
    "scheduler": _object({
        "running": _BOOL, "tick_seconds": {"type": ["number", "null"]},
        "last_tick_at": _NULL_STR, "last_ok_tick_at": _NULL_STR,
        "last_ok_tick_age_seconds": {"type": ["number", "null"]},
        "command_runs": _object({"running": _INT}),
    }),
})

RESULTS = {}
OPERATIONS = (
    Operation("system.status", "POST", "/api/v1/system/status", "Read dashboard status.",
              "Read dashboard status.", _object({}, closed=True), _STATUS, risk="read"),
    Operation("system.health", "POST", "/api/v1/system/health",
              "Read the server's liveness: its scheduler's latest tick, and the Manager's completion turns.",
              "Read the server's liveness: its scheduler's latest tick, and the Manager's completion turns. The "
              "completion counts are the waiting events, the last 24 hours by state and reason, and whether the "
              "turns keep falling back, as the completion service last read them.",
              _object({}, closed=True), _HEALTH, risk="read"),
)


async def run(server, operation, arguments, principal, headers):
    if operation.operation_id == "system.health":
        return {
            "completion": completion_service.health(),
            "status": "ok", "version": __version__, "pid": os.getpid(),
            "started_at": server._started_at.isoformat(), "now": datetime.now(timezone.utc).isoformat(),
            "scheduler": server._scheduler.health() if server._scheduler else {
                "running": False, "tick_seconds": None, "last_tick_at": None,
                "last_ok_tick_at": None, "last_ok_tick_age_seconds": None,
                "command_runs": {"running": 0},
            },
        }
    result = await server._status_result(headers)
    if principal.kind != "operator":
        result["can_sign_out"] = False
    return result
