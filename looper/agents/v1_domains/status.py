"""The dashboard status projection, without adding a model-facing Tool."""

from ..api_v1 import Operation, _array, _object, _STR, _NULL_STR, _INT, _BOOL

ORDER = 1

_AGENT = _object({"healthy": _BOOL, "state": _STR, "up": _BOOL, "restart_count": _INT,
                  "last_crash_at": _NULL_STR, "next_restart_at": _NULL_STR, "agent_type": _STR})
_BINDING = _object({
    "enforced": _BOOL, "mismatches": _INT,
    "by": _array(_object({"reason": _STR, "runtime": _STR, "tool": _STR, "count": _INT, "manager": _STR},
                         ["reason", "runtime", "tool", "count"])),
    "matches": _array(_object({"by": _STR, "when": _STR, "count": _INT}))}, ["enforced", "mismatches", "by"])
_STATUS = _object({
    "status": {"const": "running"}, "agents": {"type": "object", "additionalProperties": _AGENT},
    "tasks": _object({"total": _INT, "running": _INT, "completed": _INT}),
    "channels": _object({"web": _object({"channel": {"const": "web"}, "healthy": _BOOL,
                                         "active_sse_streams": _INT})}),
    "checks": _array(_object({"id": _STR, "label": _STR, "detail": _STR, "fix": _STR,
                               "status": {"type": "string", "enum": ["ok", "warn", "fail", "skip"]}})),
    "can_sign_out": _BOOL, "turn_binding": _BINDING,
    "managers": _array(_object({"id": _STR, "name": _STR, "running": _BOOL, "busy": _BOOL,
                                 "healthy": _BOOL, "sync": {"type": "string", "enum": ["pending", "ok"]},
                                 "sync_error": _STR, "turn_binding": _BINDING},
                                ["id", "name", "running", "busy", "healthy", "sync", "turn_binding"]))},
    ["status", "agents", "tasks", "channels", "checks", "can_sign_out"])

RESULTS = {}
OPERATIONS = (Operation("system.status", "POST", "/api/v1/system/status", "Read dashboard status.",
                        "Read dashboard status.", _object({}, closed=True), _STATUS, risk="read"),)


async def run(server, operation, arguments, principal, headers):
    result = await server._status_result(headers)
    if principal.kind != "operator":
        result["can_sign_out"] = False
    return result
