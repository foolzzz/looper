"""The outbound review's system setting (``outbound_hook.ENABLED_KEY``): whether every tool call of the Managers is
checked before it runs, and which enabled Managers run on a runtime the review cannot hook."""

from .. import outbound_hook
from ..api_v1 import Operation, _array, _object, _BOOL, _STR
from ..ops import ErrorCode, OpError

_MANAGER = _object({"manager_id": _STR, "name": _STR, "runtime": _STR}, closed=True)
SETTINGS = _object({"enabled": _BOOL, "managers_without_hook": _array(_MANAGER)}, closed=True)
SCHEMAS = {"OutboundReviewSettings": SETTINGS}
RESULTS = {}


def _op(name, description, request, result, risk="read"):
    return Operation(name, "POST", "/api/v1/" + name.replace(".", "/"), description, description,
                     request, result, risk=risk, confirms=risk == "high", operator_only=True)


OPERATIONS = (
    _op("outbound_review.get", "Read whether the outbound review is on, and the enabled Managers whose runtime has "
                               "no tool hook (the review cannot be turned on while there are any).",
        _object({}, closed=True), SETTINGS),
    _op("outbound_review.update", "Turn the outbound review on or off. On, each tool call of a Manager that may "
                                  "leave this machine is checked by the outbound_check role first.",
        _object({"enabled": _BOOL}, ["enabled"], closed=True), SETTINGS, "high"),
)


async def impact(server, operation, arguments, principal, headers):
    if operation.operation_id != "outbound_review.update":
        return None
    if arguments["enabled"]:
        return ("Turn the outbound review on? Every shell command and outbound call of the Managers is checked by "
                "the outbound_check role before it runs (a few seconds and about 0.003 USD each); a call it cannot "
                "judge is blocked. Running Managers restart on their next turn.")
    return ("Turn the outbound review off? The Managers' shell commands and outbound calls run without a check, and "
            "running Managers restart on their next turn.")


async def run(server, operation, arguments, principal, headers):
    if operation.operation_id == "outbound_review.get":
        return await outbound_hook.settings()
    try:
        return await outbound_hook.set_enabled(arguments["enabled"])
    except outbound_hook.HookUnavailable as exc:
        raise OpError(ErrorCode.CONFLICT, str(exc)) from None
    except ValueError as exc:
        raise OpError(ErrorCode.INVALID, str(exc)) from None
