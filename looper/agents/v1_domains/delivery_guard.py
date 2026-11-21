"""Operator-only delivery guard settings, separate from agent-accessible chat settings."""

from .. import chat_settings
from ..api_v1 import Operation, _BOOL, _object

VIEW = _object({"enabled": _BOOL}, closed=True)
RESULTS = {}
OPERATIONS = (
    Operation("delivery_guard.get", "POST", "/api/v1/delivery_guard/get",
              "Read the delivery guard setting.", "Read the delivery guard setting; disabled by default.",
              _object({}, closed=True), VIEW, operator_only=True),
    Operation("delivery_guard.update", "POST", "/api/v1/delivery_guard/update",
              "Set the delivery guard setting.",
              "Set the delivery guard setting; null restores the disabled default. Manager wiring is separate.",
              _object({"enabled": {"type": ["boolean", "null"]}}, closed=True), VIEW,
              risk="write", operator_only=True),
)


async def run(server, operation, arguments, principal, headers):
    if operation.operation_id == "delivery_guard.get":
        return chat_settings.delivery_guard_view()
    return await chat_settings.update_delivery_guard(arguments["enabled"])
