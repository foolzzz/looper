"""The delivery layer's switch (``delivery.enabled``): operator-only, separate from the chat settings clients see."""

from .. import chat_settings
from ..action_levels import ActionLevel
from ..api_v1 import Operation, _BOOL, _object

VIEW = _object({"enabled": _BOOL}, closed=True)
RESULTS = {}
OPERATIONS = (
    Operation("proactive_delivery.get", "POST", "/api/v1/proactive_delivery/get",
              "Read whether proactive messages pass the delivery layer.",
              "Read whether proactive messages pass the delivery layer. It is off by default.",
              _object({}, closed=True), VIEW, operator_only=True,
              level=ActionLevel.INSIDE),  # Reads Looper's own delivery setting.
    Operation("proactive_delivery.update", "POST", "/api/v1/proactive_delivery/update",
              "Turn the delivery layer for proactive messages on or off.",
              "Turn the delivery layer for proactive messages on or off. Null restores the default, off.",
              _object({"enabled": {"type": ["boolean", "null"]}}, closed=True), VIEW,
              risk="high", confirms=True, operator_only=True,
              level=ActionLevel.INSIDE),  # Stores only Looper's own delivery setting; sends nothing.
)


async def impact(server, operation, arguments, principal, headers):
    if operation.operation_id != "proactive_delivery.update":
        return None
    value = arguments["enabled"]
    enabled = chat_settings.DEFAULT_DELIVERY_ENABLED if value is None else value
    return (f"Set {chat_settings.DELIVERY_ENABLED_KEY} to {str(enabled).lower()}{' (built-in default)' if value is None else ''}? "
            + ("From the next one, each proactive message wired to the delivery layer is decided on before it goes "
               "out (sent, into the digest, or dropped, and why), and the decision is recorded." if enabled else
               "From the next one, proactive messages go out without a delivery decision."))


async def run(server, operation, arguments, principal, headers):
    if operation.operation_id == "proactive_delivery.get":
        return chat_settings.delivery_view()
    try:
        return await chat_settings.update_delivery(arguments["enabled"])
    except ValueError as exc:
        from ..ops import ErrorCode, OpError

        raise OpError(ErrorCode.INVALID, str(exc)) from None
