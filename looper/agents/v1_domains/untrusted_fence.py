"""Operator-only controls for the fence on external content injected by Looper."""

from ...db import system_settings
from .. import untrusted
from ..action_levels import ActionLevel
from ..api_v1 import Operation, _BOOL, _object, validate_arguments
from ..ops import ErrorCode, OpError

VIEW = _object({"enabled": _BOOL}, closed=True)
SCHEMAS = {"UntrustedFenceSettings": VIEW}
RESULTS = {}
OPERATIONS = (
    Operation("untrusted_fence.get", "POST", "/api/v1/untrusted_fence/get",
              "Read the untrusted content fence setting.",
              "Read the untrusted content fence setting. It is disabled by default.",
              _object({}, closed=True), VIEW, risk="read", operator_only=True,
              level=ActionLevel.INSIDE),  # reads only Looper's own setting
    Operation("untrusted_fence.update", "POST", "/api/v1/untrusted_fence/update",
              "Turn the untrusted content fence on or off.",
              "Turn the untrusted content fence on or off. This changes only external content injected by Looper.",
              _object({"enabled": _BOOL}, closed=True), VIEW, risk="high", confirms=True, operator_only=True,
              level=ActionLevel.INSIDE),  # changes only Looper's own setting
)


def _checked(operation, arguments, principal):
    if principal.kind != "operator":
        raise OpError(ErrorCode.FORBIDDEN, "only the operator can manage the untrusted content fence")
    return validate_arguments(operation.request, arguments)


async def impact(server, operation, arguments, principal, headers):
    arguments = _checked(operation, arguments, principal)
    if operation.operation_id != "untrusted_fence.update":
        return None
    direction = ("Turn the fence on? Looper-injected external content will carry its source and an untrusted fence."
                 if arguments["enabled"] else
                 "Turn the fence off? Looper-injected external content will use its existing text without the fence.")
    return direction + " This does not change the outbound review setting."


async def run(server, operation, arguments, principal, headers):
    arguments = _checked(operation, arguments, principal)
    try:
        if operation.operation_id == "untrusted_fence.get":
            values = await system_settings.get_settings([untrusted.ENABLED_KEY])
            return {"enabled": values.get(untrusted.ENABLED_KEY) == "true"}
        enabled = arguments["enabled"]
        await system_settings.put_settings({untrusted.ENABLED_KEY: str(enabled).lower()})
        return {"enabled": enabled}
    except Exception:
        raise OpError(ErrorCode.UNAVAILABLE, "the untrusted content fence setting is unavailable") from None
