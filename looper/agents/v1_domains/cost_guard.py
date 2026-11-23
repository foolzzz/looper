"""Operator-only cost guard settings; agent settings and tools never expose guard controls."""

import logging
from datetime import datetime, timezone

from .. import cost_guard, cost_guard_settings as settings, cost_holds
from ...db import cost_holds as holds_store
from ..run_usage import CostStatus
from ..action_levels import ActionLevel
from ..api_v1 import Operation, _array, _object, _STR, _NULL_STR, _INT, _BOOL
from ..ops import ErrorCode, OpError

logger = logging.getLogger(__name__)

_CAP = {"type": ["number", "null"], "minimum": 0}
_MODE = {"type": "string", "enum": ["off", "observe", "enforce"]}
_LIMITS = _object(dict.fromkeys(settings.CAPS, _CAP), closed=True)
_ROLES = _object(dict.fromkeys(settings.ROLES, _LIMITS), [], closed=True)
_COUNTER = _object({"scope": _STR, "scope_ref": _STR, "currency": _STR, "spent_usd": {"type": "number"},
                    "reserved_usd": {"type": "number"}, "limit_usd": _CAP,
                    "cost_status": {"type": "string", "enum": [status.value for status in CostStatus]},
                    "unknown_models": _array(_STR)}, closed=True)
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
HOLD = _object({"id": _INT, "scope": _STR, "scope_ref": _STR, "currency": _STR,
                "limit_usd": {"type": "number"}, "spent_usd": {"type": "number"}, "reason": _STR,
                "state": _STR, "created_at": _STR, "decided_at": _NULL_STR, "decided_via": _NULL_STR}, closed=True)
_DECIDE = _object({"hold_id": {"type": "integer", "minimum": 1},
                   "decision": {"type": "string", "enum": ["continue", "stop"]},
                   "new_limit_usd": {"type": "number", "minimum": 0},
                   "via": {"type": "string", "enum": ["api"]}},
                  ["hold_id", "decision"], closed=True)
RESULTS = {}
_GET = ("Read cost guard observations. Shows separate real and equivalent spend, reservations, current usage-window "
        "holds and the latest 20 admission observations. All daily caps are unset by default.")
_ENFORCE = ("Enforce holds task dispatches at task and global daily caps and refuses other senders' Manager turns. "
            "Operator Manager turns pass the spend cap with a daily notice. Spend refusals take precedence over "
            "usage-window holds. Rejected windows and quota failures hold all runs, including runs you start "
            "yourself; 95% utilization holds only schedule firings and background roles. Holds last until reset, "
            "or at most five minutes when reset time is unknown. Window rows apply when "
            "engine.limit_signals.enabled is on; otherwise only quota-failure rules apply. No account is switched. "
            "Background-role and schedule budget enforcement remains inactive.")
_UPDATE = ("Configure cost guard admission. Off and observe preserve execution. " + _ENFORCE +
           " Existing spend holds remain actionable in every mode. With a scope cap, subscription runs soft stop "
           "at their reservation; no scope caps means no reservation soft stop. Fail counts reservations and "
           "stops running Workers. Planner closing turns use actual admission. Null restores the default.")
OPERATIONS = (
    Operation("cost_guard.get", "POST", "/api/v1/cost_guard/get", "Read cost guard observations.",
              _GET, _object({}, closed=True), VIEW,
              operator_only=True, level=ActionLevel.INSIDE),  # reads only Looper's own settings and observations
    Operation("cost_guard.update", "POST", "/api/v1/cost_guard/update", "Configure cost guard admission.",
              _UPDATE,
              PATCH, VIEW, risk="high", confirms=True, operator_only=True,
              level=ActionLevel.INSIDE),  # changes only Looper's own admission settings
    Operation("cost_holds.list", "POST", "/api/v1/cost_holds/list", "List pending spend holds.",
              "List pending spend holds. Only the operator can read these decisions.", _object({}, closed=True),
              _object({"holds": _array(HOLD)}, closed=True), risk="read", operator_only=True,
              level=ActionLevel.INSIDE),  # reads only Looper's waiting states
    Operation("cost_holds.decide", "POST", "/api/v1/cost_holds/decide", "Continue or stop a spend hold.",
              "Continue or stop a spend hold. Raising a limit requires confirmation; existing running work is not "
              "interrupted by admission holds.", _DECIDE, HOLD, risk="high", confirms=True, operator_only=True,
              level=ActionLevel.INSIDE),  # changes only Looper's caps and task lifecycle
)


def _view():
    config, snapshot = settings.view(), cost_guard.current().snapshot()
    return {**config, **snapshot, "problem": config["problem"] or snapshot.get("problem")}


async def impact(server, operation, arguments, principal, headers):
    if operation.operation_id == "cost_holds.decide":
        row = await holds_store.get(arguments["hold_id"])
        if row is None or row['state'] != 'pending':
            raise OpError(ErrorCode.INVALID, "The spend hold does not exist or has already been decided.")
        if arguments.get('new_limit_usd') is not None:
            return (f"Raise {row['scope']} {row['scope_ref']}'s {row['currency']} limit to "
                    f"${arguments['new_limit_usd']:g} and continue C-{row['id']}?")
        return f"{arguments['decision'].capitalize()} spend hold C-{row['id']} and its waiting tasks?"
    if operation.operation_id != "cost_guard.update":
        return None
    try:
        settings.checked(arguments)
    except ValueError as exc:
        raise OpError(ErrorCode.INVALID, str(exc)) from None
    return ("Update cost guard settings? " + _ENFORCE +
            " Off and observe do not block new runs or send new hold questions; existing holds remain available "
            "for decisions and expire after 24 hours in every mode. Subscription-priced runs soft stop at their "
            "reservation only when a cap is set on their scope chain; no caps means no reservation soft stop. "
            "Reservations use a configured run cap, or the 30-day role P95 with a floor ($0.50 for task/Manager "
            "roles, $0.05 for small background roles); no samples use that floor. The task fail action counts "
            "spend and reservations and can fail earlier, stopping running Workers. Planner closing turns "
            "(stopping, completed, failed) also check admission and are refused only when admission refuses.")


def _hold(row):
    return {key: row[key].isoformat() if hasattr(row[key], 'isoformat') else row[key] for key in HOLD['properties']}


async def run(server, operation, arguments, principal, headers):
    if operation.operation_id == 'cost_holds.list':
        return {'holds': [_hold(row) for row in await holds_store.list_pending()]}
    if operation.operation_id == 'cost_holds.decide':
        from ..ops.service import Platform

        try:
            row = await cost_holds.decide(arguments['hold_id'], arguments['decision'], arguments.get('new_limit_usd'),
                via='api', platform=Platform(bus=server._supervisor.bus,
                                                                 task_router=server._task_router))
        except ValueError as exc:
            raise OpError(ErrorCode.INVALID, str(exc)) from None
        return _hold(row)
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
