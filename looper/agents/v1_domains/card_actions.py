"""Operator-only card-button switch; no model tool or chat-settings field."""

from ...db import system_settings
from .. import card_actions
from ..action_levels import ActionLevel
from ..api_v1 import Operation, _BOOL, _object

VIEW = _object({"enabled": _BOOL}, closed=True)
RESULTS = {}
OPERATIONS = (
    Operation("card_actions.get", "POST", "/api/v1/card_actions/get",
              "Read the card button setting.", "Read the card button setting. It is disabled by default.",
              _object({}, closed=True), VIEW, risk="read", operator_only=True,
              level=ActionLevel.INSIDE),  # reads only Looper's stored switch
    Operation("card_actions.update", "POST", "/api/v1/card_actions/update",
              "Set the card button setting.",
              "Set the card button setting. Null deletes the stored setting and restores the disabled default.",
              _object({"enabled": {"type": ["boolean", "null"]}}, closed=True), VIEW,
              risk="high", confirms=True, operator_only=True,
              level=ActionLevel.INSIDE),  # stores permission; card sends and clicks run separately
)


async def impact(server, operation, arguments, principal, headers):
    if operation.operation_id != "card_actions.update":
        return None
    value = arguments["enabled"]
    return (f"Set {card_actions.ENABLED_KEY} to {str(value is True).lower()}"
            f"{' (built-in default)' if value is None else ''}? "
            "This controls card buttons for confirmations, gates, intents and ledger decisions on Lark and Telegram.")


async def run(server, operation, arguments, principal, headers):
    if operation.operation_id == "card_actions.update":
        value = arguments["enabled"]
        await system_settings.put_settings({card_actions.ENABLED_KEY: None if value is None else str(value).lower()})
    return {"enabled": await card_actions.enabled()}
