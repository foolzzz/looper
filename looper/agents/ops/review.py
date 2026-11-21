"""The outbound review of Looper's own tools (the outbound-review RFC, section 6.1, layer 1): while
``outbound_review.enabled`` is on, a write call whose level (``ops.levels``, ``Operation.level``) is ``external``
is reviewed after its turn checks and before ``escalate`` and the user's confirmation, which it never replaces
(RFC D7, D15). In a turn that is not the operator's, code blocks it (D17); a blocked call does not run, and the
model gets the ``blocked`` error. Off, nothing here runs."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from ..action_levels import ActionLevel
from ..outbound import Action

BLOCKED_ERROR = "blocked"


def action_of(tool_name: str, arguments: Mapping[str, Any], level: ActionLevel) -> Action:
    """A tool call as the review reads it: what it acts on, by its typed arguments, and its arguments whole."""
    fields = arguments.get("fields") if isinstance(arguments.get("fields"), Mapping) else {}
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
