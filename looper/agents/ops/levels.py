"""Where each Looper tool's calls reach (the outbound-review RFC, section 4.2): an ``ActionLevel`` per tool, or a
function of the call's arguments and turn where they decide it. A read action (``Tool.read_actions``) is always
``inside``.

The levels are declared here, by each tool's exact name, and not on ``Tool``: the upgrade probe reads ``Tool``'s
declaration and the room's and vault's tool constructors by their exact shape (``looper/_component_probe.py``), so
neither can take a field. Every tool of every agent has an entry (``tests/test_tool_levels.py``): a new tool does not
pass the tests until it declares one, and a tool with none is ``unknown`` here.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from ..action_levels import ActionLevel, schedule_level, schedule_reach
from . import Tool, Turn

LevelOf = Callable[[Mapping[str, Any], Turn], Awaitable[ActionLevel]]

INSIDE, OPERATOR, ORIGIN, MACHINE = ActionLevel.INSIDE, ActionLevel.OPERATOR, ActionLevel.ORIGIN, ActionLevel.MACHINE


async def _projects(arguments: Mapping[str, Any], turn: Turn) -> ActionLevel:
    # move renames the project's directory on this machine and leaves a link at the old place
    return MACHINE if arguments.get("action") == "move" else INSIDE


async def _settings(arguments: Mapping[str, Any], turn: Turn) -> ActionLevel:
    # the local gateway listens on this machine; the knowledge vault is a directory of the operator's
    return MACHINE if arguments.get("action") in ("gateway", "knowledge_vault") else INSIDE


async def _schedules(arguments: Mapping[str, Any], turn: Turn) -> ActionLevel:
    """Where a schedule reaches (``action_levels.schedule_level``): a new one by its fields; enabling or running one
    by what it has stored, changing one by that with the change; one Looper cannot find is ``unknown``."""
    from .schedules import _schedule

    action = arguments.get("action")
    fields = arguments.get("fields") if isinstance(arguments.get("fields"), Mapping) else {}
    if action in ("update", "enable", "run"):
        try:
            row = await _schedule(arguments)
        except Exception:
            return ActionLevel.UNKNOWN
        fields = {**schedule_reach(row), **(fields if action == "update" else {})}
    elif action != "create":
        return INSIDE  # disable, delete, reset_planner_session
    return await schedule_level(fields, channel=turn.channel, chat=turn.chat, sender=turn.sender, group=turn.group)


async def _gates(arguments: Mapping[str, Any], turn: Turn) -> ActionLevel:
    # a reply or an end goes into the gate's discussion, which its courier posts to the task's chat
    return ORIGIN if arguments.get("action") in ("reply", "end") else INSIDE


TOOL_LEVELS: Mapping[str, ActionLevel | LevelOf] = {
    # The Manager's (and an API client's)
    "looper_overview": INSIDE, "looper_tasks": INSIDE, "looper_costs": INSIDE,
    "looper_config_export": INSIDE,  # written under the runtime root
    "looper_task_create": INSIDE,  # its task's agents' own calls are reviewed where they run
    "looper_task_control": INSIDE, "looper_task_instruct": INSIDE, "looper_task_update": INSIDE,
    "looper_concurrency": INSIDE,
    "looper_task_gate_reply": ORIGIN,  # the Planner's answer goes back to the chat the task came from
    "looper_projects": _projects,
    "looper_agent_profiles": INSIDE, "looper_models": INSIDE,
    "looper_providers": MACHINE,  # where the account's runs send the model's traffic
    "looper_catalog": MACHINE,  # an MCP server is a command on this machine
    "looper_settings": _settings,
    "looper_schedules": _schedules,
    # Runs the call the user confirmed, which was reviewed when it asked (RFC D7): not again.
    "looper_confirm": INSIDE,
    "looper_ledger_add": INSIDE, "looper_ledger_update": INSIDE,
    "looper_leads_search": INSIDE,  # reads message leads stored by Looper, including full-text search
    "looper_session_search": INSIDE,  # reads earlier conversations stored by Looper
    "looper_reminder_list": INSIDE,
    # Reminder writes affect delivery only to the operator's own direct chat.
    "looper_reminder_create": OPERATOR, "looper_reminder_cancel": OPERATOR,
    # An API client's own
    "looper_task_pipeline": INSIDE,
    "looper_gates": _gates,
    # A task's Planner's
    "looper_task_profiles": INSIDE, "looper_task_roles": INSIDE, "looper_task_profile_set": INSIDE,
    "looper_task_role_set": INSIDE,
    "looper_task_concurrency": ORIGIN,  # a fixed notice goes to the chat the task came from
    # A step's run's room, inside its task
    "looper_room_roster": INSIDE, "looper_room_post": INSIDE, "looper_room_inbox": INSIDE,
    "looper_room_read": INSIDE, "looper_room_watch": INSIDE,
    # Bounded read-only children and their results stay within this task.
    "looper_agent_spawn": INSIDE, "looper_agent_wait": INSIDE, "looper_agent_cancel": INSIDE,
    # The knowledge vault: reading, or writing and committing the operator's notes (never pushed)
    "looper_vault_search": INSIDE, "looper_vault_read": INSIDE, "looper_vault_write": MACHINE,
}


async def level_of(tool: Tool, arguments: Mapping[str, Any], turn: Turn) -> ActionLevel:
    """Where this call of ``tool`` reaches: ``inside`` for a read action, else its declared level; ``unknown`` for
    a tool that declares none."""
    if arguments.get("action") in tool.read_actions:
        return INSIDE
    declared = TOOL_LEVELS.get(tool.name)
    if declared is None:
        return ActionLevel.UNKNOWN
    return declared if isinstance(declared, ActionLevel) else await declared(arguments, turn)
