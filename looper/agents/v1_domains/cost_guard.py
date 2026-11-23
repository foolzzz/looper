"""Operator-only cost guard settings; agent settings and tools never expose guard controls."""

import logging
from datetime import datetime, timezone

from .. import cost_guard, cost_guard_settings as settings
from ..action_levels import ActionLevel
from ..api_v1 import Operation, _array, _object, _BOOL, _STR, _NULL_STR
from ..ops import ErrorCode, OpError

logger = logging.getLogger(__name__)

_CAP = {"type": ["number", "null"], "minimum": 0}
_MODE = {"type": "string", "enum": ["off", "observe", "enforce"]}
_LIMITS = _object(dict.fromkeys(settings.CAPS, _CAP), closed=True)
_ROLES = _object(dict.fromkeys(settings.ROLES, _LIMITS), [], closed=True)
_COUNTER = _object({"scope": _STR, "scope_ref": _STR, "currency": _STR, "spent_usd": {"type": "number"},
                    "reserved_usd": {"type": "number"}, "limit_usd": _CAP}, closed=True)
_RECENT = _object({"at": _STR, "run_kind": _STR, "currency": _STR, "estimate_usd": {"type": "number"},
                   "estimate_source": _STR, "decision": _STR, "scope": _NULL_STR, "scope_ref": _NULL_STR,
                   "action": _NULL_STR, "would_hold_until": _NULL_STR, "would_hold_reason": _NULL_STR,
                   "note": _NULL_STR}, closed=True)
for _name in ("would_hold_until", "would_hold_reason", "note"):
    _RECENT["required"].remove(_name)
_HOLD = _object({"provider": _STR, "reason": _STR, "until": _STR, "window": _NULL_STR, "reset_known": _BOOL},
                ["provider", "reason", "until"], closed=True)
VIEW = _object({"mode": _MODE, **dict.fromkeys(settings.CAPS, _CAP), "roles": _ROLES,
                "counters": _array(_COUNTER), "recent": _array(_RECENT),
                "recommended_daily_usd": _CAP, "recommended_daily_equivalent_usd": _CAP,
                "problem": _NULL_STR, "holds": _array(_HOLD)}, closed=True)
VIEW["required"] = [name for name in VIEW["required"] if name != "holds"]
_PATCH_ROLES = _object({role: _object(dict.fromkeys(settings.CAPS, _CAP), [], closed=True)
                        for role in settings.ROLES}, [], closed=True)
PATCH = _object({"mode": {"type": ["string", "null"], "enum": ["off", "observe", "enforce", None]},
                 **dict.fromkeys(settings.CAPS, _CAP), "roles": _PATCH_ROLES}, [], closed=True)
RESULTS = {}
_GET = ("Read cost guard observations. Shows separate real and equivalent spend, reservations, current usage-window "
        "holds and the latest 20 admission observations. All daily caps are unset by default.")
_UPDATE = ("Configure cost guard settings. off records nothing. observe records reservations and simulated refusals "
           "without blocking runs or sending messages. enforce holds schedule firings and the synchronous background "
           "roles until a usage window resets or for at most five minutes; runs you start yourself are recorded only. "
           "Window rows apply when engine.limit_signals.enabled is on; otherwise only quota-failure rules apply. "
           "Null restores the default; daily caps have no default.")
OPERATIONS = (
    Operation("cost_guard.get", "POST", "/api/v1/cost_guard/get", "Read cost guard observations.",
              _GET, _object({}, closed=True), VIEW,
              operator_only=True, level=ActionLevel.INSIDE),  # reads only Looper's own settings and observations
    Operation("cost_guard.update", "POST", "/api/v1/cost_guard/update", "Configure cost guard settings.",
              _UPDATE, PATCH, VIEW, risk="high", confirms=True, operator_only=True,
              level=ActionLevel.INSIDE),  # changes only Looper's own cost guard settings
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
    if arguments.get("mode") == "enforce":
        return ("Update cost guard settings? enforce holds schedule firings and the synchronous background roles "
                "(processor, leads_check, advice_check, outbound_check, advisor) until a usage window resets or for "
                "at most five minutes. Runs you start yourself are recorded only. Window rows apply when "
                "engine.limit_signals.enabled is on; otherwise only quota-failure rules apply. No account is switched.")
    return ("Update cost guard settings? off records nothing. observe records reservations and simulated refusals "
            "without blocking runs or sending messages. enforce holds schedule firings and the synchronous background "
            "roles until a usage window resets or for at most five minutes. Runs you start yourself are recorded only. "
            "Window rows apply when engine.limit_signals.enabled is on; otherwise only quota-failure rules apply. "
            "No account is switched.")


async def run(server, operation, arguments, principal, headers):
    if operation.operation_id == "cost_guard.update":
        try:
            await settings.update(arguments)
            await cost_guard.refresh()
        except ValueError as exc:
            raise OpError(ErrorCode.INVALID, str(exc)) from None
    view = _view()
    if view["mode"] != "off":
        from .. import usage_windows

        try:
            view["holds"] = await usage_windows.list_holds(datetime.now(timezone.utc))
        except Exception:
            logger.exception("could not list usage-window holds")
            view["holds"] = []
    return view
