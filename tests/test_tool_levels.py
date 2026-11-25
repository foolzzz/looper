"""Where every Looper tool and ``/api/v1`` operation reaches (``ops/levels.py``, ``Operation.level``; the
outbound-review RFC, section 4): each tool of every agent declares a level, the table is the RFC's, and the levels
that depend on a call's arguments follow them. Nothing here changes what runs: the review reads them (PR4)."""

from __future__ import annotations

import asyncio
import importlib
import pkgutil
from pathlib import Path
from typing import Any

import pytest

from looper.agents.action_levels import ActionLevel, highest
from looper.agents.ops import Tool, Turn
from looper.agents.ops import levels as tool_levels

INSIDE, OPERATOR, ORIGIN, MACHINE, EXTERNAL, UNKNOWN = (ActionLevel.INSIDE, ActionLevel.OPERATOR, ActionLevel.ORIGIN,
                                                        ActionLevel.MACHINE, ActionLevel.EXTERNAL, ActionLevel.UNKNOWN)
# The RFC's table (section 4.2): a fixed level, or "args" where the call's arguments decide it.
RFC_TABLE: dict[str, ActionLevel | str] = {
    # Chat task operations stay inside Looper; task agents are reviewed where they run.
    "looper_chat_tasks": INSIDE, "looper_chat_task_create": INSIDE,
    "looper_chat_task_control": INSIDE, "looper_chat_task_instruct": INSIDE,
    "looper_overview": INSIDE, "looper_tasks": INSIDE, "looper_costs": INSIDE, "looper_config_export": INSIDE,
    "looper_task_create": INSIDE, "looper_task_control": INSIDE, "looper_task_instruct": INSIDE,
    "looper_task_update": INSIDE, "looper_concurrency": INSIDE, "looper_task_gate_reply": ORIGIN,
    "looper_projects": "args", "looper_agent_profiles": INSIDE, "looper_models": INSIDE, "looper_providers": MACHINE,
    "looper_catalog": MACHINE, "looper_settings": "args", "looper_schedules": "args", "looper_confirm": INSIDE,
    "looper_ledger_add": INSIDE, "looper_ledger_update": INSIDE, "looper_leads_search": INSIDE,
    "looper_skill_propose": INSIDE, "looper_intents_recognize": OPERATOR,
    "looper_memory_propose": INSIDE,
    "looper_task_hold": INSIDE,
    "looper_session_search": INSIDE,  # reads earlier conversations stored by Looper
    "looper_reminder_list": INSIDE, "looper_reminder_create": OPERATOR, "looper_reminder_cancel": OPERATOR,
    "looper_task_pipeline": INSIDE, "looper_gates": "args",
    "looper_task_profiles": INSIDE, "looper_task_roles": INSIDE, "looper_task_profile_set": INSIDE,
    "looper_task_role_set": INSIDE, "looper_task_concurrency": ORIGIN,
    "looper_room_roster": INSIDE, "looper_room_post": INSIDE, "looper_room_inbox": INSIDE, "looper_room_read": INSIDE,
    "looper_room_watch": INSIDE,
    "looper_agent_spawn": INSIDE, "looper_agent_wait": INSIDE, "looper_agent_cancel": INSIDE,
    "looper_vault_search": INSIDE, "looper_vault_read": INSIDE, "looper_vault_write": MACHINE,
}


def _every_tool() -> dict[str, Tool]:
    """Every tool any agent of Looper is given: each ``Tool`` the ops modules hold, alone or in a tuple."""
    import looper.agents.ops as ops

    found: dict[str, Tool] = {}
    for info in pkgutil.iter_modules(ops.__path__):
        module = importlib.import_module(f"{ops.__name__}.{info.name}")
        for value in vars(module).values():
            for tool in value if isinstance(value, (tuple, list)) else (value,):
                if isinstance(tool, Tool):
                    found.setdefault(tool.name, tool)
    return found


def test_every_tool_of_every_agent_declares_its_level_and_the_table_is_the_rfcs() -> None:
    tools = _every_tool()
    assert set(tools) == set(tool_levels.TOOL_LEVELS), "a tool without a level, or a level of no tool"
    assert set(tools) == set(RFC_TABLE)
    for name, level in tool_levels.TOOL_LEVELS.items():
        expected = RFC_TABLE[name]
        assert (level if isinstance(level, ActionLevel) else "args") == expected, name


def _turn(**fields: Any) -> Turn:
    return Turn(**{"channel": "lark", "chat": "oc_team", "message_id": "m-1", "sender": "ou_me", "group": True,
                   **fields})


def _level(name: str, arguments: dict[str, Any], turn: Turn | None = None) -> ActionLevel:
    return asyncio.run(tool_levels.level_of(_every_tool()[name], arguments, turn or _turn()))


def test_a_read_action_is_inside_and_a_tool_with_no_level_is_unknown() -> None:
    assert _level("looper_providers", {"action": "list"}) is INSIDE
    assert _level("looper_providers", {"action": "update"}) is MACHINE
    assert _level("looper_catalog", {"action": "list"}) is INSIDE
    stray = Tool(name="looper_added_later", description="", schema={}, risk=_every_tool()["looper_tasks"].risk,
                 run=_every_tool()["looper_tasks"].run)
    assert asyncio.run(tool_levels.level_of(stray, {}, _turn())) is UNKNOWN


@pytest.mark.parametrize(("name", "arguments", "level"), [
    ("looper_reminder_list", {}, INSIDE),
    ("looper_reminder_create", {"text": "Review the PR", "in_minutes": 5}, OPERATOR),
    ("looper_reminder_cancel", {"id": 1}, OPERATOR),
    ("looper_projects", {"action": "move", "project": "calc", "to": "/srv/calc"}, MACHINE),
    ("looper_projects", {"action": "update", "project": "calc", "name": "calc-2"}, INSIDE),
    ("looper_settings", {"action": "gateway"}, MACHINE),
    ("looper_settings", {"action": "knowledge_vault"}, MACHINE),
    ("looper_settings", {"action": "scheduler"}, INSIDE),
    ("looper_gates", {"action": "reply", "gate": 1}, ORIGIN),
    ("looper_gates", {"action": "end", "gate": 1}, ORIGIN),
    ("looper_gates", {"action": "messages", "gate": 1}, INSIDE),
    ("looper_schedules", {"action": "list"}, INSIDE),
    ("looper_schedules", {"action": "create", "fields": {"kind": "agent", "cron": "0 9 * * *"}}, INSIDE),
    ("looper_schedules", {"action": "create", "fields": {"kind": "command", "command": "make backup"}}, MACHINE),
    ("looper_schedules", {"action": "create", "fields": {"gate_command": "test -f ready"}}, MACHINE),
    ("looper_schedules", {"action": "create", "fields": {"notify_target": None}}, OPERATOR),
    ("looper_schedules", {"action": "create", "fields": {"notify_target": {"channel": "lark", "chat": "oc_team"}}},
     ORIGIN),
    ("looper_schedules", {"action": "create", "fields": {"notify_target": {"channel": "lark", "chat": "oc_other"}}},
     EXTERNAL),
    ("looper_schedules", {"action": "create", "fields": {"notify_target": {"channel": "lark", "chat": ""}}}, UNKNOWN),
    ("looper_schedules", {"action": "create", "fields": {
        "kind": "command", "command": "make", "notify_target": {"channel": "lark", "chat": "oc_other"}}}, EXTERNAL),
])
def test_a_level_that_depends_on_the_call_follows_its_arguments(monkeypatch: pytest.MonkeyPatch, name: str,
                                                                arguments: dict[str, Any], level: ActionLevel) -> None:
    from looper.agents import operator_identity

    async def nobody(channel_id: str) -> None:
        return None

    monkeypatch.setattr(operator_identity, "operator_sender_id", nobody)
    assert _level(name, arguments) is level


def test_a_push_to_the_operators_own_chat_is_operator_and_to_the_asking_direct_chat_origin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from looper.agents import operator_identity

    async def operator(channel_id: str) -> str:
        assert channel_id == "channel-lark"
        return "ou_operator"

    monkeypatch.setattr(operator_identity, "operator_sender_id", operator)
    update = {"action": "create", "fields": {"notify_target": {"channel": "lark", "chat": "ou_operator"}}}
    assert _level("looper_schedules", update) is OPERATOR
    direct = _turn(chat="oc_dm", group=False, sender="ou_friend")
    to_sender = {"action": "create", "fields": {"notify_target": {"channel": "lark", "chat": "ou_friend"}}}
    assert _level("looper_schedules", to_sender, direct) is ORIGIN
    to_telegram = {"action": "create", "fields": {"notify_target": {"channel": "telegram", "chat": "ou_friend"}}}
    assert _level("looper_schedules", to_telegram, direct) is EXTERNAL


def test_a_call_with_several_effects_is_at_its_highest_level() -> None:
    assert highest() is INSIDE and highest(MACHINE, ORIGIN) is MACHINE and highest(MACHINE, EXTERNAL) is EXTERNAL
    assert highest(EXTERNAL, UNKNOWN) is UNKNOWN


def _operations() -> dict[str, Any]:
    from looper.agents.v1_domains import _DOMAINS

    return {op.operation_id: op for module in _DOMAINS for op in module.OPERATIONS}


# The operations whose level was decided one by one (every other one is inside).
DECIDED_OPERATIONS: dict[str, ActionLevel] = {
    "ledger.page": INSIDE,  # reads only Looper's ledger, including source status
    "ledger.add": INSIDE,  # records an operator item inside Looper; executes no source action
    "ledger.action": INSIDE,  # updates only Looper's ledger through its event transaction
    # Confirmation, including save-and-confirm, and retry can commit to the operator's vault.
    **dict.fromkeys(("memory.confirm", "memory.confirm_many", "memory.edit", "memory.retry"), MACHINE),
    "cost_guard.get": INSIDE,  # reads Looper's own observation settings and counters
    "cost_guard.update": INSIDE,  # changes only Looper's observation configuration
    "card_actions.get": INSIDE,  # reads Looper's stored card-button switch
    "card_actions.update": INSIDE,  # stores permission; sends no cards and runs no click
    # Stores permission only; the runner executes checks separately after Worker success.
    "acceptance_checks.get": INSIDE, "acceptance_checks.update": INSIDE,
    "task_evidence.get": INSIDE,  # reads only stored check definitions and measured evidence

    "manager_persona.get": INSIDE,  # reads Looper's stored persona; sends nothing
    "manager_persona.update": INSIDE,  # changes Looper's stored persona; sends nothing
    "manager_sessions.list": INSIDE,  # reads the default Manager's stored sessions; sends nothing
    "manager_sessions.reset": INSIDE,  # clears one Looper session; sends nothing
    "providers.limits": INSIDE,  # reads stored quota windows and usage
    "limit_signals_settings.get": INSIDE,  # reads Looper's collection switch
    "limit_signals_settings.update": INSIDE,  # changes only Looper's collection switch
    "session_routing.get": INSIDE,  # reads Looper's own session routing controls
    "session_routing.update": INSIDE,  # stores session routing settings; sends no message
    "scenario_session_routing.get": INSIDE,  # reads only a stored Manager definition
    "scenario_session_routing.update": INSIDE,  # stores only a routing policy; sends no message
    "outbound_gateway.get": INSIDE,  # reads Looper's own gateway settings
    "outbound_gateway.update": INSIDE,  # stores gateway settings; sends no message
    "memory.list": INSIDE,  # reads stored candidates
    "memory.get": INSIDE,  # reads one stored candidate
    "memory.settings_get": INSIDE,  # reads and validates Looper's memory settings
    "memory.settings_put": INSIDE,  # stores settings; writes no vault files and sends nothing
    "memory.check": INSIDE,  # checks prerequisites without changing files or sending messages
    "memory.propose_from_lead": INSIDE,  # stores a candidate inside Looper; writes no vault file
    "outbound_review.get": INSIDE,  # reads Looper's own review setting
    "outbound_review.update": INSIDE,  # changes Looper's own review setting
    "outbound_review.list": INSIDE,  # reads stored audit rows without sending content
    "untrusted_fence.get": INSIDE,  # reads Looper's own content-fence setting
    "untrusted_fence.update": INSIDE,  # changes only how Looper wraps incoming content
    "project_skills.context_budget": INSIDE,  # reads the displayed skill-description budget; sends nothing
    "project_skills.deprecate": INSIDE,  # marks a catalog skill deprecated inside Looper; sends nothing and does not touch installs or files
    "project_skills.restore": INSIDE,  # marks a catalog skill active inside Looper; sends nothing and does not touch installs or files
    "project_skills.rollback": INSIDE,  # writes a new stored skill version inside Looper; sends nothing and does not touch the machine outside the catalog
    "project_skills.usage": INSIDE,  # reads run records and attempts to count a skill's loads and calls; sends nothing
    "project_skills.versions": INSIDE,  # reads stored skill versions; sends nothing
    "project_skills.proposals.list": INSIDE,  # reads stored skill proposals; sends nothing
    "project_skills.proposals.get": INSIDE,  # reads one stored skill proposal and its events; sends nothing
    "project_skills.proposals.confirm": INSIDE,  # writes a catalog skill version inside Looper from a stored proposal; sends nothing
    "project_skills.proposals.reject": INSIDE,  # marks a stored proposal rejected; sends nothing
    "project_skills.proposals.restore": INSIDE,  # marks an expired stored proposal pending; sends nothing
    "project_skills.proposals.set_mode": INSIDE,  # stores a project's proposal switch; sends nothing
    "memory_closeout.get": INSIDE,  # reads a project's memory closeout switch; sends nothing
    "memory_closeout.update": INSIDE,  # stores a project flag; candidates still need the operator's confirmation
    "mcp_tool_levels.get": INSIDE,  # reads stored MCP trust declarations
    "mcp_tool_levels.put": INSIDE,  # stores MCP trust declarations; runs no MCP call

    "manager_chat_bindings.put": EXTERNAL,  # a bound chat's people talk to a Manager, which answers them there
    "intent_bindings.put": INSIDE,  # maps a chat to a project; sends nothing anywhere
    # toggles bound_chats_only; the admission it widens is chat_routing's operator-only refusal to guard
    "channel_chats.set_policy": INSIDE,
    "message_collection.get": INSIDE,  # reads source-local collection state
    "message_collection.update": INSIDE,  # stores configuration without reading an account
    "message_collection.mode_update": INSIDE,  # changes Looper's ingestion state
    "message_collection.run_now": MACHINE,  # starts a local read-only collector CLI
}


def test_every_operation_is_inside_but_the_ones_that_bind_or_push_to_a_chat() -> None:
    operations = _operations()
    computed = {name for name, op in operations.items() if op.level_of is not None}
    fixed = {name: op.level for name, op in operations.items() if op.level is not INSIDE}
    assert computed == {"schedule_admin.create", "schedule_admin.update", "schedule_admin.run",
                        "schedule_admin.confirm_target"}
    assert fixed == {name: level for name, level in DECIDED_OPERATIONS.items() if level is not INSIDE}
    for name, level in DECIDED_OPERATIONS.items():
        assert operations[name].level is level and operations[name].level_of is None, name


@pytest.mark.parametrize(("fields", "level"), [
    ({"kind": "agent", "cron": "0 9 * * *"}, INSIDE),
    ({"kind": "command", "command": "make"}, MACHINE),
    ({"notify_target": None}, OPERATOR),
    ({"notify_target": {"channel": "lark", "chat": "oc_team"}}, EXTERNAL),  # an API client's call has no chat
])
def test_a_schedule_operations_level_follows_its_fields(monkeypatch: pytest.MonkeyPatch, fields: dict[str, Any],
                                                        level: ActionLevel) -> None:
    from looper.agents import operator_identity

    async def nobody(channel_id: str) -> None:
        return None

    monkeypatch.setattr(operator_identity, "operator_sender_id", nobody)
    op = _operations()["schedule_admin.create"]  # a stored one's: tests/test_tool_levels_db.py
    assert asyncio.run(op.level_of({"project_id": "p-1", "fields": fields})) is level


def test_the_openapi_document_is_what_it_was(monkeypatch: pytest.MonkeyPatch) -> None:
    """The levels are Looper's own: the document an API client reads is the same with every level reset."""
    import dataclasses
    import json

    from looper.agents import api_v1
    from looper.agents.v1_domains import _DOMAINS

    declared = json.dumps(api_v1._document(api_v1.operations()), sort_keys=True)
    for module in _DOMAINS:
        monkeypatch.setattr(module, "OPERATIONS", tuple(
            dataclasses.replace(op, level=INSIDE, level_of=None) for op in module.OPERATIONS))
    reset = json.dumps(api_v1._document(api_v1.operations()), sort_keys=True)
    assert reset == declared and '"level_of"' not in declared


def test_in_a_group_turn_a_push_to_the_speakers_direct_chat_is_external(monkeypatch: pytest.MonkeyPatch) -> None:
    """Only a direct-chat turn's sender is that turn's chat: in a group, the speaker's own chat is someone else's."""
    from looper.agents import operator_identity

    async def nobody(channel_id: str) -> None:
        return None

    monkeypatch.setattr(operator_identity, "operator_sender_id", nobody)
    to_speaker = {"action": "create", "fields": {"notify_target": {"channel": "lark", "chat": "ou_me"}}}
    assert _level("looper_schedules", to_speaker, _turn(group=True)) is EXTERNAL
    assert _level("looper_schedules", to_speaker, _turn(group=False, chat="oc_dm")) is ORIGIN


# Every /api/v1 operation and its level: "args" where its arguments (or what it acts on) decide it.
MANIFEST = Path(__file__).with_name("operation_levels.json")


@pytest.mark.parametrize("operation_id", [
    "attention.settings_update", "reminders_settings.get", "reminders_settings.update",
    "stable_context_settings.get", "stable_context_settings.update", "decision_snapshots_settings.get",
    "decision_snapshots_settings.update", "leads_search_settings.get", "leads_search_settings.update",
])
def test_operator_switch_settings_only_change_looper_state(operation_id):
    from looper.agents import api_v1

    operation = api_v1.find("POST", "/api/v1/" + operation_id.replace(".", "/"))
    assert operation is not None and operation.level is INSIDE and operation.level_of is None


def _declared(operations: Any) -> dict[str, str]:
    return {op.operation_id: "args" if op.level_of is not None else op.level.value for op in operations}


def test_every_operation_declares_the_level_its_manifest_lists() -> None:
    import json

    from looper.agents import api_v1

    assert _declared(api_v1.operations()) == json.loads(MANIFEST.read_text())


def test_a_new_operation_is_not_in_the_manifest_until_it_is_listed() -> None:
    import dataclasses
    import json

    from looper.agents import api_v1

    some = next(iter(api_v1.operations()))
    added = dataclasses.replace(some, operation_id="outbound_review.added_later")
    assert _declared([*api_v1.operations(), added]) != json.loads(MANIFEST.read_text())
