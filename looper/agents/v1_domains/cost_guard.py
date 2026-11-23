"""Operator-only cost observation; agent settings and tools never expose guard controls."""

from .. import cost_guard, cost_guard_settings as settings
from ..action_levels import ActionLevel
from ..api_v1 import Operation, _array, _object, _STR, _NULL_STR
from ..ops import ErrorCode, OpError

_CAP = {"type": ["number", "null"], "minimum": 0}
_MODE = {"type": "string", "enum": ["off", "observe"]}
_LIMITS = _object(dict.fromkeys(settings.CAPS, _CAP), closed=True)
_ROLES = _object(dict.fromkeys(settings.ROLES, _LIMITS), [], closed=True)
_COUNTER = _object({"scope": _STR, "scope_ref": _STR, "currency": _STR, "spent_usd": {"type": "number"},
                    "reserved_usd": {"type": "number"}, "limit_usd": _CAP}, closed=True)
_RECENT = _object({"at": _STR, "run_kind": _STR, "currency": _STR, "estimate_usd": {"type": "number"},
                   "estimate_source": _STR, "decision": _STR, "scope": _NULL_STR, "scope_ref": _NULL_STR,
                   "action": _NULL_STR}, closed=True)
VIEW = _object({"mode": _MODE, **dict.fromkeys(settings.CAPS, _CAP), "roles": _ROLES,
                "counters": _array(_COUNTER), "recent": _array(_RECENT),
                "recommended_daily_usd": _CAP, "recommended_daily_equivalent_usd": _CAP,
                "problem": _NULL_STR}, closed=True)
_PATCH_ROLES = _object({role: _object(dict.fromkeys(settings.CAPS, _CAP), [], closed=True)
                        for role in settings.ROLES}, [], closed=True)
PATCH = _object({"mode": {"type": ["string", "null"], "enum": ["off", "observe", None]},
                 **dict.fromkeys(settings.CAPS, _CAP), "roles": _PATCH_ROLES}, [], closed=True)
RESULTS = {}
OPERATIONS = (
    Operation("cost_guard.get", "POST", "/api/v1/cost_guard/get", "Read cost guard observations.",
              "Read cost guard observations. Shows separate real and equivalent spend, reservations and the latest "
              "20 admission observations. All daily caps are unset by default.", _object({}, closed=True), VIEW,
              operator_only=True, level=ActionLevel.INSIDE),  # reads only Looper's own settings and observations
    Operation("cost_guard.update", "POST", "/api/v1/cost_guard/update", "Configure cost guard observation.",
              "Configure cost guard observation. Only off and observe are available; neither blocks, asks or sends "
              "messages. Null restores the default; daily caps have no default. Observe for a week before choosing caps.",
              PATCH, VIEW, risk="high", confirms=True, operator_only=True,
              level=ActionLevel.INSIDE),  # changes only Looper's own observation settings
)


def _view():
    config, snapshot = settings.view(), cost_guard.current().snapshot()
    return {**config, **snapshot, "problem": config["problem"] or snapshot.get("problem")}


async def impact(server, operation, arguments, principal, headers):
    if operation.operation_id != "cost_guard.update":
        return None
    try:
        settings.checked(arguments)
    except ValueError as exc:
        raise OpError(ErrorCode.INVALID, str(exc)) from None
    return ("Update cost guard settings? Only off and observe are available (no enforcement). "
            "Observe records reservations and simulated refusals without blocking runs or sending messages.")


async def run(server, operation, arguments, principal, headers):
    if operation.operation_id == "cost_guard.update":
        try:
            await settings.update(arguments)
            await cost_guard.refresh()
        except ValueError as exc:
            raise OpError(ErrorCode.INVALID, str(exc)) from None
    return _view()
