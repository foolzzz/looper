"""Operator-only controls for Manager interactive and background lanes."""

from .. import lanes_settings
from ..action_levels import ActionLevel
from ..api_v1 import Operation, _BOOL, _object

VIEW = _object({"enabled": _BOOL}, closed=True)
RESULTS = {}
OPERATIONS = (
    Operation("manager_lanes.get", "POST", "/api/v1/manager_lanes/get",
              "Read the Manager lanes setting.",
              "Read the Manager lanes setting. It is disabled by default. Operator-only.",
              _object({}, closed=True), VIEW, risk="read", operator_only=True,
              level=ActionLevel.INSIDE),  # reads only Looper's own setting
    Operation("manager_lanes.update", "POST", "/api/v1/manager_lanes/update",
              "Set the Manager lanes setting.",
              "Set the Manager lanes setting. On, user messages take priority over queued background work and "
              "interrupt running background turns on a supporting runtime. Null restores the disabled default.",
              _object({"enabled": {"type": ["boolean", "null"]}}, closed=True), VIEW,
              risk="high", confirms=True, operator_only=True,
              level=ActionLevel.INSIDE),  # changes only Looper's own scheduling setting
)


async def impact(server, operation, arguments, principal, headers):
    if operation.operation_id != "manager_lanes.update":
        return None
    value = arguments["enabled"]
    enabled = False if value is None else value
    return (f"Set {lanes_settings.KEY} to {str(enabled).lower()}"
            f"{' (built-in default)' if value is None else ''}? "
            "打开后，你的消息到达时，正在进行的收尾回合、空闲检查会被中断"
            "（收尾改发代码通知），你的消息先处理。")


async def run(server, operation, arguments, principal, headers):
    if operation.operation_id == "manager_lanes.get":
        return lanes_settings.view()
    return await lanes_settings.update(arguments["enabled"])
