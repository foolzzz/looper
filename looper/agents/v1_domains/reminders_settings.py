"""Operator-only one-time reminder switch, independent of chat settings."""

from ...db import system_settings
from .. import reminders
from ..action_levels import ActionLevel
from ..api_v1 import Operation, _BOOL, _object

VIEW = _object({"enabled": _BOOL}, closed=True)
RESULTS = {}
OPERATIONS = (
    Operation("reminders_settings.get", "POST", "/api/v1/reminders_settings/get",
              "Read whether one-time reminders are on.",
              "Read whether one-time reminders are on. They are off by default.",
              _object({}, closed=True), VIEW, risk="read", operator_only=True,
              level=ActionLevel.INSIDE),  # reads only Looper's feature setting
    Operation("reminders_settings.update", "POST", "/api/v1/reminders_settings/update",
              "Turn one-time reminders on or off.",
              "Turn one-time reminders on or off. Null restores the default, off. Off hides reminder tools and "
              "stops sending reminders; existing reminders remain. On resumes processing, including catch-up rules.",
              _object({"enabled": {"type": ["boolean", "null"]}}, closed=True), VIEW,
              risk="high", confirms=True, operator_only=True,
              level=ActionLevel.INSIDE),  # stores permission; reminder sends run separately
)


async def impact(server, operation, arguments, principal, headers):
    if operation.operation_id != "reminders_settings.update":
        return None
    value = arguments["enabled"]
    return (f"Set reminders.enabled to {str(value is True).lower()}"
            f"{' (built-in default)' if value is None else ''}? "
            "On exposes reminder tools to the operator's default Manager and resumes due reminders, including "
            "catch-up. Off stops subsequent sends without deleting reminders.")


async def run(server, operation, arguments, principal, headers):
    if operation.operation_id == "reminders_settings.update":
        value = arguments["enabled"]
        await system_settings.put_settings({reminders.ENABLED_KEY: None if value is None else str(value).lower()})
    return {"enabled": await reminders.enabled()}
