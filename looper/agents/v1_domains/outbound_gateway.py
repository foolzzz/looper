"""Operator-only gateway controls, separate from agent-accessible chat settings."""

from .. import chat_settings
from ..action_levels import ActionLevel
from ..api_v1 import Operation, _BOOL, _object
from ..ops import ErrorCode, OpError

LIMITS = {
    key: ({"type": "integer", "minimum": 1, "maximum": 32, "default": default} if key == "max_in_flight" else
          {"type": "number", "exclusiveMinimum": 0, "default": default,
           "description": "Conservative default, to verify."})
    for key, default in chat_settings.OUTBOUND_DEFAULTS.items()
}
VIEW = _object({"outbound_enabled": _BOOL, **LIMITS}, closed=True)
UPDATE = {"outbound_enabled": {"type": ["boolean", "null"]},
          **{key: {**schema, "type": [schema["type"], "null"]} for key, schema in LIMITS.items()}}
RESULTS = {}
OPERATIONS = (
    Operation("outbound_gateway.get", "POST", "/api/v1/outbound_gateway/get",
              "Read the outbound gateway controls.",
              "Read the outbound gateway controls. The gateway is disabled by default.",
              _object({}, closed=True), VIEW, operator_only=True,
              level=ActionLevel.INSIDE),  # reads only Looper's own gateway settings
    Operation("outbound_gateway.update", "POST", "/api/v1/outbound_gateway/update",
              "Configure the outbound gateway and durable channel outbox.",
              "Configure the outbound gateway and durable channel outbox. Null restores a default; "
              "rate and edit defaults are conservative and remain to verify.",
              _object(UPDATE, [], closed=True), VIEW, risk="high", confirms=True, operator_only=True,
              level=ActionLevel.INSIDE),  # changes only Looper's own gateway settings; sends no message
)


async def impact(server, operation, arguments, principal, headers):
    if operation.operation_id != "outbound_gateway.update":
        return None
    try:
        values = chat_settings.checked_outbound(arguments)
    except ValueError as exc:
        raise OpError(ErrorCode.INVALID, str(exc)) from None
    changes = [f"Set channels.outbound.{key} to {chat_settings.OUTBOUND_DEFAULTS[key] if value is None else value}?"
               for key, value in values.items() if key in chat_settings.OUTBOUND_DEFAULTS]
    if "outbound_enabled" in values:
        value = values["outbound_enabled"]
        enabled = chat_settings.DEFAULT_OUTBOUND_ENABLED if value is None else value
        changes.append(f"Set channels.outbound.enabled to {str(enabled).lower()}? "
                       "Lark and Telegram messages use a durable outbox and shared rate limits when enabled; "
                       "only Lark retries unknown sends with platform deduplication. "
                       "Disabling restores the original transport for new writes; persisted work continues draining.")
    return " ".join(changes)


async def run(server, operation, arguments, principal, headers):
    if operation.operation_id == "outbound_gateway.get":
        return chat_settings.outbound_view()
    try:
        return await chat_settings.update_outbound(arguments)
    except ValueError as exc:
        raise OpError(ErrorCode.INVALID, str(exc)) from None
