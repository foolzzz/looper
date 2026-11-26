"""Operator-only controls for work-mode draft creation."""

from .. import task_conversations_settings as settings
from ..action_levels import ActionLevel
from ..api_v1 import Operation, _BOOL, _object

_MAX = {"type": "integer", "minimum": 1, "maximum": 20}
VIEW = _object({"enabled": _BOOL, "max_resident": _MAX}, closed=True)
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
              "Max resident sets the task Manager process cap (1–20). Null restores defaults: disabled and 6.",
              _object({"enabled": {"type": ["boolean", "null"]},
                       "max_resident": {**_MAX, "type": ["integer", "null"]}}, [], closed=True), VIEW,
              risk="high", confirms=True, operator_only=True,
              level=ActionLevel.INSIDE),  # changes only Looper's own setting
)


async def impact(server, operation, arguments, principal, headers):
    if operation.operation_id != "task_conversations.update":
        return None
    changes = []
    if "enabled" in arguments:
        value = False if arguments["enabled"] is None else arguments["enabled"]
        changes.append(f"Set {settings.KEY} to {str(value).lower()}? "
                       "On, the dashboard can create draft tasks; nothing else changes yet.")
    if "max_resident" in arguments:
        value = settings.DEFAULT_MAX_RESIDENT if arguments["max_resident"] is None else arguments["max_resident"]
        changes.append(f"Set {settings.MAX_RESIDENT_KEY} to {value}?")
    return " ".join(changes)


async def run(server, operation, arguments, principal, headers):
    if operation.operation_id == "task_conversations.get":
        return settings.view()
    return await settings.update(**{"value" if key == "enabled" else key: value
                                    for key, value in arguments.items()})
