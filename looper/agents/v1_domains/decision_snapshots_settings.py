"""Operator-only decision snapshot recording switch."""

from ...db import decision_snapshots, system_settings
from ..action_levels import ActionLevel
from ..api_v1 import Operation, _BOOL, _object

VIEW = _object({"enabled": _BOOL}, closed=True)
RESULTS = {}
OPERATIONS = (
    Operation("decision_snapshots_settings.get", "POST", "/api/v1/decision_snapshots_settings/get",
              "Read whether decision snapshots are on.",
              "Read whether decision snapshots are on. They are off by default.",
              _object({}, closed=True), VIEW, risk="read", operator_only=True,
              level=ActionLevel.INSIDE),  # reads only Looper's feature setting
    Operation("decision_snapshots_settings.update", "POST", "/api/v1/decision_snapshots_settings/update",
              "Turn decision snapshots on or off.",
              "Turn decision snapshots on or off. Null restores the default, off. On records future dispatch and "
              "step decisions for offline replay and enables retention cleanup. Off keeps existing snapshots.",
              _object({"enabled": {"type": ["boolean", "null"]}}, closed=True), VIEW,
              risk="high", confirms=True, operator_only=True,
              level=ActionLevel.INSIDE),  # controls recording and retention in Looper's database
)


async def impact(server, operation, arguments, principal, headers):
    if operation.operation_id != "decision_snapshots_settings.update":
        return None
    value = arguments["enabled"]
    return (f"Set decision_snapshots.enabled to {str(value is True).lower()}"
            f"{' (built-in default)' if value is None else ''}? "
            "On records future decisions and enables retention cleanup. Off stops new snapshots and age-based "
            "cleanup; existing records remain. No model call is made.")


async def run(server, operation, arguments, principal, headers):
    if operation.operation_id == "decision_snapshots_settings.update":
        value = arguments["enabled"]
        await system_settings.put_settings({decision_snapshots.ENABLED_KEY: None if value is None else str(value).lower()})
    return {"enabled": await decision_snapshots.enabled()}
