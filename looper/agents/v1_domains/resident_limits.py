"""Operator-only machine capacity and idle reclaim controls for scoped Managers."""

from ...db import system_settings
from .. import session_routing
from ..action_levels import ActionLevel
from ..api_v1 import Operation, _object

_TOTAL = {"type": "integer", "minimum": 1, "maximum": 32}
_IDLE = {"type": "integer", "anyOf": [{"const": 0}, {"minimum": 60, "maximum": 86400}]}
VIEW = _object({"max_resident_total": _TOTAL, "idle_reclaim_seconds": _IDLE}, closed=True)
_UPDATE = {**_object({"max_resident_total": {**_TOTAL, "type": ["integer", "null"]},
                      "idle_reclaim_seconds": {"anyOf": [_IDLE, {"type": "null"}]}}, [], closed=True),
           "anyOf": [{"required": ["max_resident_total"]}, {"required": ["idle_reclaim_seconds"]}]}
RESULTS = {}
OPERATIONS = (
    Operation("resident_limits.get", "POST", "/api/v1/resident_limits/get",
              "Read scoped Manager resident limits.",
              "Read scoped Manager resident limits. Defaults: six machine-wide residents and 1800 idle seconds.",
              _object({}, closed=True), VIEW, risk="read", operator_only=True,
              level=ActionLevel.INSIDE),  # Reads only Looper's capacity settings.
    Operation("resident_limits.update", "POST", "/api/v1/resident_limits/update",
              "Set scoped Manager resident limits.",
              "Set scoped Manager resident limits. Null restores a default; zero idle seconds disables reclaim.",
              _UPDATE, VIEW, risk="high", confirms=True, operator_only=True,
              level=ActionLevel.INSIDE),  # Stores only Looper's capacity settings; sends no message.
)


def _values(arguments):
    total = arguments.get("max_resident_total", session_routing.max_resident_total())
    idle = arguments.get("idle_reclaim_seconds", session_routing.idle_reclaim_seconds())
    return {
        "max_resident_total": session_routing.checked_max_resident_total(
            session_routing.DEFAULT_MAX_RESIDENT_TOTAL if total is None else total),
        "idle_reclaim_seconds": session_routing.checked_idle_reclaim_seconds(
            session_routing.DEFAULT_IDLE_RECLAIM_SECONDS if idle is None else idle),
    }


async def impact(server, operation, arguments, principal, headers):
    if operation.operation_id != "resident_limits.update":
        return None
    values = _values(arguments)
    return (f"Set max_resident_total to {values['max_resident_total']} and idle_reclaim_seconds to "
            f"{values['idle_reclaim_seconds']}? These apply to scoped Managers' sessions; "
            "the cap counts every Manager's residents and start reservations.")


async def run(server, operation, arguments, principal, headers):
    values = _values(arguments if operation.operation_id == "resident_limits.update" else {})
    if operation.operation_id == "resident_limits.update":
        await system_settings.put_settings({
            session_routing.MAX_RESIDENT_TOTAL_KEY: None if arguments.get("max_resident_total", 0) is None
                else str(values["max_resident_total"]),
            session_routing.IDLE_RECLAIM_KEY: None if arguments.get("idle_reclaim_seconds", 0) is None
                else str(values["idle_reclaim_seconds"]),
        })
        session_routing.set_max_resident_total(values["max_resident_total"])
        session_routing.set_idle_reclaim_seconds(values["idle_reclaim_seconds"])
    return values
