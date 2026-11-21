"""Operator-only configuration of Manager observations, separate from client chat controls."""

from .. import chat_settings
from ..api_v1 import Operation, _object, _BOOL

VIEW = _object({"enabled": _BOOL}, closed=True)
RESULTS = {}
OPERATIONS = (
    Operation("manager_metrics_settings.get", "POST", "/api/v1/manager_metrics_settings/get",
              "Read the Manager metrics setting.",
              "Read the Manager metrics setting. It is disabled by default.",
              _object({}, closed=True), VIEW, operator_only=True),
    Operation("manager_metrics_settings.update", "POST", "/api/v1/manager_metrics_settings/update",
              "Set the Manager metrics setting.",
              "Set the Manager metrics setting. Null restores the disabled default.",
              _object({"enabled": {"type": ["boolean", "null"]}}, closed=True), VIEW,
              risk="high", confirms=True, operator_only=True),
)


async def impact(server, operation, arguments, principal, headers):
    if operation.operation_id != "manager_metrics_settings.update":
        return None
    value = arguments["enabled"]
    enabled = chat_settings.DEFAULT_METRICS_ENABLED if value is None else value
    return (f"Set {chat_settings.METRICS_ENABLED_KEY} to {str(enabled).lower()}"
            f"{' (built-in default)' if value is None else ''}? "
            "This records Manager turn latency and proactive delivery metrics without changing delivery.")


async def run(server, operation, arguments, principal, headers):
    if operation.operation_id == "manager_metrics_settings.get":
        return chat_settings.metrics_view()
    return await chat_settings.update_metrics(arguments["enabled"])
