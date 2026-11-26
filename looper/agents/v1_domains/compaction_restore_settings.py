"""Operator-only switch for compaction keep instructions and state snapshots."""

from ...db import system_settings
from .. import compaction_restore
from ..action_levels import ActionLevel
from ..api_v1 import Operation, _BOOL, _object

VIEW = _object({"enabled": _BOOL}, closed=True)
RESULTS = {}
OPERATIONS = (
    Operation("compaction_restore_settings.get", "POST", "/api/v1/compaction_restore_settings/get",
              "Read whether compaction restore is on.",
              "Read whether compaction restore is on. Claude Code Planner and Manager compactions keep a fixed "
              "list and are followed by a state snapshot. It is off by default.",
              _object({}, closed=True), VIEW, risk="read", operator_only=True,
              level=ActionLevel.INSIDE),  # reads only Looper's feature setting
    Operation("compaction_restore_settings.update", "POST", "/api/v1/compaction_restore_settings/update",
              "Turn compaction restore on or off.",
              "Turn compaction restore on or off. Null restores the default, off. The next Planner or Manager turn reads it: "
              "on, Claude Code maintenance compactions carry a fixed keep-list and the first turn after a compaction starts "
              "with a snapshot of Looper's current state; off sends bare /compact and no snapshot.",
              _object({"enabled": {"type": ["boolean", "null"]}}, closed=True), VIEW,
              risk="high", confirms=True, operator_only=True,
              level=ActionLevel.INSIDE),  # changes how Looper presents its own stored state
)


async def impact(server, operation, arguments, principal, headers):
    if operation.operation_id != "compaction_restore_settings.update":
        return None
    value = arguments["enabled"]
    behavior = ("compact with a fixed keep-list and get a state snapshot on the first turn after a compaction"
                if value is True else "compact with a bare /compact and get no snapshot")
    return (f"Set compaction_restore.enabled to {str(value is True).lower()}"
            f"{' (built-in default)' if value is None else ''}? Claude Code Planner and Manager sessions then "
            f"{behavior}. Prompts, tools and launches are otherwise unchanged, and no resident process restarts.")


async def run(server, operation, arguments, principal, headers):
    if operation.operation_id == "compaction_restore_settings.update":
        value = arguments["enabled"]
        await system_settings.put_settings({compaction_restore.KEY: None if value is None else str(value).lower()})
    return {"enabled": await compaction_restore.enabled()}
