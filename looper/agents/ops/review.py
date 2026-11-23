"""The outbound review of Looper's own tools (the outbound-review RFC, section 6.1, layer 1): while
``outbound_review.enabled`` is on, a write call whose level (``ops.levels``, ``Operation.level``) is ``external``
is reviewed after its turn checks and before ``escalate`` and the user's confirmation, which it never replaces
(RFC D7, D15). In a turn that is not the operator's, code blocks it (D17); a blocked call does not run, and the
model gets the ``blocked`` error. Off, nothing here runs."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from ..action_levels import ActionLevel, schedule_reach
from ..outbound import Action

BLOCKED_ERROR = "blocked"


async def action_of(tool_name: str, arguments: Mapping[str, Any], level: ActionLevel) -> Action:
    """A tool call as the review reads it, including the stored reach its level was computed from."""
    fields = arguments.get("fields") if isinstance(arguments.get("fields"), Mapping) else {}
    row = None
    if tool_name == "looper_schedules" and arguments.get("action") in ("update", "enable", "run"):
        from .schedules import _schedule

        row = await _schedule(arguments)
        fields = fields if arguments["action"] == "update" else {}
    elif tool_name in ("schedule_admin.update", "schedule_admin.run", "schedule_admin.confirm_target") \
            and "schedule_id" in arguments:
        from ...db import schedules

        row = await schedules.get_schedule(arguments["schedule_id"])
    if row is not None:
        fields = {**schedule_reach(row), **fields}
        arguments = {**arguments, "fields": fields}
    target = fields.get("notify_target") if "notify_target" in fields else None
    if isinstance(target, Mapping):
        kind, label = "notify_target", f"{target.get('channel')}:{target.get('chat')}"
    else:
        named = next(((key, arguments[key]) for key in ("chat_id", "schedule", "schedule_id", "project", "task_id")
                      if isinstance(arguments.get(key), str) and arguments.get(key)), None)
        kind, label = named if named is not None else ("tool", tool_name)
    action = arguments.get("action") if isinstance(arguments.get("action"), str) else None
    summary = f"Call {tool_name}" + (f" ({action})" if action else "") + f" on {kind} {label}"
    return Action(tool_name, level, kind, str(label)[:500], summary, action=action,
                  content=json.dumps(arguments, ensure_ascii=False, sort_keys=True, default=str))
