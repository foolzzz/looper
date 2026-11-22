"""Operator-only channel outage settings, outside the Manager's tool surface."""

from ..channels import down_alert_settings
from ..action_levels import ActionLevel
from ..api_v1 import Operation, _object
from ..ops import ErrorCode, OpError

_MINUTES = {"type": "integer", "minimum": 1}
VIEW = _object({"down_alert_minutes": _MINUTES}, closed=True)
RESULTS = {}
OPERATIONS = (
    Operation("channel_down_alert.get", "POST", "/api/v1/channel_down_alert/get",
              "Read the channel outage alert threshold.",
              "Read the channel outage alert threshold. down_alert_minutes defaults to 10 minutes.",
              _object({}, closed=True), VIEW, operator_only=True,
              level=ActionLevel.INSIDE),  # reads Looper's own alert threshold; sends nothing
    Operation("channel_down_alert.update", "POST", "/api/v1/channel_down_alert/update",
              "Set the channel outage alert threshold.",
              "Set the channel outage alert threshold. Give a positive whole number of minutes; null restores 10.",
              _object({"down_alert_minutes": {"type": ["integer", "null"], "minimum": 1}}, closed=True), VIEW,
              risk="high", confirms=True, operator_only=True,
              level=ActionLevel.INSIDE),  # stores only Looper's alert threshold; the monitor sends notices separately
)


async def impact(server, operation, arguments, principal, headers):
    if operation.operation_id != "channel_down_alert.update":
        return None
    try:
        value = down_alert_settings.checked(arguments["down_alert_minutes"])
    except ValueError as exc:
        raise OpError(ErrorCode.INVALID, str(exc)) from None
    return f"Set {down_alert_settings.KEY} to {value} minutes? Alerts apply to enabled channels after this threshold."


async def run(server, operation, arguments, principal, headers):
    if operation.operation_id == "channel_down_alert.get":
        return down_alert_settings.view()
    try:
        return await down_alert_settings.update(arguments["down_alert_minutes"])
    except ValueError as exc:
        raise OpError(ErrorCode.INVALID, str(exc)) from None
