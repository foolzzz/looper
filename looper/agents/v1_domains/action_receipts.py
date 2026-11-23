"""Operator-only action receipts switch, separate from agent-accessible chat settings."""

from .. import receipts_settings
from ..action_levels import ActionLevel
from ..api_v1 import Operation, _BOOL, _object

VIEW = _object({"enabled": _BOOL}, closed=True)
RESULTS: dict[str, object] = {}
_READ = ("Read whether action receipts are on.",
         "Read whether action receipts are on. They are off by default. Operator-only.")
_UPDATE = ("Turn action receipts on or off.",
           "Turn action receipts on or off. Null restores the default, off. On, Looper adds a receipt line to the "
           "reply for each write the Manager or an API client makes, and records its category. Operator-only.")
OPERATIONS = (
    Operation("action_receipts.get", "POST", "/api/v1/action_receipts/get", _READ[0], _READ[1],
              _object({}, closed=True), VIEW, risk="read", operator_only=True, level=ActionLevel.INSIDE),
    Operation("action_receipts.update", "POST", "/api/v1/action_receipts/update", _UPDATE[0], _UPDATE[1],
              _object({"enabled": {"type": ["boolean", "null"]}}, closed=True), VIEW,
              risk="high", confirms=True, operator_only=True, level=ActionLevel.INSIDE),
)


async def impact(server, operation, arguments, principal, headers):
    if operation.operation_id != "action_receipts.update":
        return None
    value = arguments["enabled"]
    enabled = False if value is None else bool(value)
    default = " (built-in default)" if value is None else ""
    return (f"Set {receipts_settings.ENABLED_KEY} to {str(enabled).lower()}{default}? "
            "On, each write the Manager or an API client makes gets a receipt line after the reply, a failed write "
            "gets a line saying it did not run, the call's result carries shown_to_user, and its category and "
            "receipt are stored with it; after a confirmation card's button the next turn is told what happened. "
            "Off restores the current behavior.")


async def run(server, operation, arguments, principal, headers):
    if operation.operation_id == "action_receipts.get":
        return receipts_settings.view()
    return await receipts_settings.update(arguments["enabled"])
