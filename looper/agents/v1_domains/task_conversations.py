"""Operator-only controls for work-mode draft creation."""

from .. import task_conversations_settings as settings
from ..action_levels import ActionLevel
from ..api_v1 import Operation, _BOOL, _object

VIEW = _object({"enabled": _BOOL}, closed=True)
RESULTS = {}
OPERATIONS = (
    Operation("task_conversations.get", "POST", "/api/v1/task_conversations/get",
              "Read the work-mode setting.",
              "Read the work-mode setting. It is disabled by default. Operator-only.",
              _object({}, closed=True), VIEW, risk="read", operator_only=True,
              level=ActionLevel.INSIDE),  # reads only Looper's own setting
    Operation("task_conversations.update", "POST", "/api/v1/task_conversations/update",
              "Set the work-mode setting.",
              "Set the work-mode setting. On, the dashboard can create draft tasks; nothing else changes yet. "
              "Null restores the disabled default.",
              _object({"enabled": {"type": ["boolean", "null"]}}, closed=True), VIEW,
              risk="high", confirms=True, operator_only=True,
              level=ActionLevel.INSIDE),  # changes only Looper's own setting
)


async def impact(server, operation, arguments, principal, headers):
    if operation.operation_id != "task_conversations.update":
        return None
    value = False if arguments["enabled"] is None else arguments["enabled"]
    return (f"Set {settings.KEY} to {str(value).lower()}? "
            "On, the dashboard can create draft tasks; nothing else changes yet.")


async def run(server, operation, arguments, principal, headers):
    if operation.operation_id == "task_conversations.get":
        return settings.view()
    return await settings.update(arguments["enabled"])
