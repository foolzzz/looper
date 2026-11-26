"""Network recovery policy is available only to an authenticated operator."""

from dataclasses import asdict

from .. import network_pause_settings
from ..action_levels import ActionLevel
from ..api_v1 import Operation, _BOOL, _object
from ..ops import ErrorCode, OpError

_DURATION = {"type": "integer", "minimum": 1, "maximum": network_pause_settings.MAX_PAUSE_SECONDS}
VIEW = _object({"enabled": _BOOL, "max_pause_seconds": _DURATION}, closed=True)
RESULTS = {}
OPERATIONS = (
    Operation("network_pause.get", "POST", "/api/v1/network_pause/get",
              "Read the network pause policy.", "Read the network pause policy. It is disabled by default.",
              _object({}, closed=True), VIEW, operator_only=True,
              level=ActionLevel.INSIDE),  # reads only Looper's own recovery policy
    Operation("network_pause.update", "POST", "/api/v1/network_pause/update",
              "Set the network pause policy.",
              "Set the network pause policy. Null restores the disabled switch or the two-hour maximum pause.",
              _object({"enabled": {"type": ["boolean", "null"]},
                       "max_pause_seconds": {**_DURATION, "type": ["integer", "null"]}},
                      required=(), closed=True),
              VIEW, risk="high", confirms=True, operator_only=True,
              level=ActionLevel.INSIDE),  # changes only Looper's own recovery policy
)


async def impact(server, operation, arguments, principal, headers):
    try:
        network_pause_settings.checked(arguments)
    except ValueError as exc:
        raise OpError(ErrorCode.INVALID, str(exc)) from None
    return ("Change the network pause policy? When enabled, model API connection failures pause work without "
            "consuming a step retry. Connectivity probes resume it; expiry waits for the operator.")


async def run(server, operation, arguments, principal, headers):
    try:
        value = (await network_pause_settings.update(arguments) if operation.operation_id == "network_pause.update"
                 else await network_pause_settings.load())
        return asdict(value)
    except ValueError as exc:
        raise OpError(ErrorCode.INVALID, str(exc)) from None
