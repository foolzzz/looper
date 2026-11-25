"""Operator-only switch for the Manager's stable state block."""

from ...db import system_settings
from ..action_levels import ActionLevel
from ..api_v1 import Operation, _BOOL, _object
from ..tiers import state_view

VIEW = _object({"enabled": _BOOL}, closed=True)
RESULTS = {}
OPERATIONS = (
    Operation("stable_context_settings.get", "POST", "/api/v1/stable_context_settings/get",
              "Read whether stable Manager context is on.",
              "Read whether stable Manager context is on. It is off by default.",
              _object({}, closed=True), VIEW, risk="read", operator_only=True,
              level=ActionLevel.INSIDE),  # reads only Looper's feature setting
    Operation("stable_context_settings.update", "POST", "/api/v1/stable_context_settings/update",
              "Turn stable Manager context on or off.",
              "Turn stable Manager context on or off. Null restores the default, off. The next Manager turn reads "
              "the switch: on sends a versioned state block and change notes; off sends the original sections.",
              _object({"enabled": {"type": ["boolean", "null"]}}, closed=True), VIEW,
              risk="high", confirms=True, operator_only=True,
              level=ActionLevel.INSIDE),  # changes how Looper presents its own stored state
)


async def impact(server, operation, arguments, principal, headers):
    if operation.operation_id != "stable_context_settings.update":
        return None
    value = arguments["enabled"]
    return (f"Set manager.stable_context to {str(value is True).lower()}"
            f"{' (built-in default)' if value is None else ''}? "
            "The next Manager turn changes its state context and contract, which can restart its resident process. "
            "Off restores the original sections.")


async def run(server, operation, arguments, principal, headers):
    if operation.operation_id == "stable_context_settings.update":
        value = arguments["enabled"]
        await system_settings.put_settings({state_view.STABLE_CONTEXT_KEY: None if value is None else str(value).lower()})
    return {"enabled": await state_view.enabled()}
