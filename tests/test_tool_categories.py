"""The phase 3 RFC's category registry covers exactly the Manager's and API clients' write actions."""

from collections.abc import Mapping

import pytest

from looper.agents.ops import Risk
from looper.agents.ops import categories
from looper.agents.ops.clients import CLIENT_TOOLS
from looper.agents.ops.ledger import TOOLS as LEDGER_TOOLS
from looper.agents.ops.reminders import TOOLS as REMINDER_TOOLS
from looper.agents.ops.service import CONFIRM_TOOL, MANAGER_TOOLS


def test_every_write_action_has_exactly_one_kind_and_reads_have_none():
    tools = {tool.name: tool for tool in (*MANAGER_TOOLS, *CLIENT_TOOLS, *LEDGER_TOOLS, *REMINDER_TOOLS)}
    expected = set()
    for name, tool in tools.items():
        # Confirmation executes the original tool, whose category describes the effect.
        if tool.risk is Risk.READ or name == CONFIRM_TOOL:
            continue
        action = tool.schema.get("properties", {}).get("action")
        if action is None:
            expected.add((name, None))
        else:
            assert action["enum"] and set(tool.read_actions) <= set(action["enum"]), name
            expected.update((name, value) for value in action["enum"] if value not in tool.read_actions)

    assert set(categories.TOOL_ACTIONS) == {name for name, _ in expected}
    declared = {}
    for name, entry in categories.TOOL_ACTIONS.items():
        kinds = entry.items() if isinstance(entry, Mapping) else ((None, entry),)
        for action, kind in kinds:
            assert isinstance(kind, categories.ActionKind), (name, action)
            assert isinstance(kind.category, categories.ActionCategory)
            assert all(value.strip() for value in (kind.noun, kind.verb, kind.subject)), (name, action)
            declared[name, action] = kind
    assert declared.keys() == expected, "a write without a category, or a category of no write action"


# Independent of completeness: the semantic choices in RFC section 6.3, including its exceptions.
@pytest.mark.parametrize("tool,groups", [
    ("looper_task_create", {"create": [None]}),
    ("looper_task_control", {"control": ["start", "stop", "restart", "start_over", "cancel"]}),
    ("looper_task_instruct", {"relay": [None]}),
    ("looper_task_gate_reply", {"relay": [None]}),
    ("looper_task_update", {"update": [None]}),
    ("looper_concurrency", {"update": [None]}),
    ("looper_projects", {"create": ["create"], "update": ["update", "move"], "remove": ["delete"]}),
    ("looper_agent_profiles", {"create": ["create"], "update": ["update"], "remove": ["delete"]}),
    ("looper_providers", {"create": ["register"], "update": ["update"]}),
    ("looper_models", {"create": ["create"], "update": ["update"], "remove": ["delete"]}),
    ("looper_catalog", {"create": ["install"], "update": ["update"], "remove": ["uninstall"]}),
    ("looper_settings", {"update": ["set", "allowed_profiles", "scheduler", "waits", "knowledge_vault",
                                    "llmrec", "gateway"]}),
    ("looper_schedules", {"create": ["create"], "update": ["update"],
                          "control": ["enable", "disable", "run", "reset_planner_session"], "remove": ["delete"]}),
    ("looper_ledger_add", {"create": [None]}),
    ("looper_ledger_update", {"update": ["amend", "note"], "control": ["confirm", "close", "cancel", "reopen"]}),
    ("looper_reminder_create", {"create": [None]}),
    ("looper_reminder_cancel", {"remove": [None]}),
    ("looper_gates", {"relay": ["reply"], "control": ["end"]}),
])
def test_the_categories_are_the_rfcs(tool, groups):
    entry = categories.TOOL_ACTIONS[tool]
    actual = {action: kind.category.value for action, kind in entry.items()} if isinstance(entry, Mapping) else {
        None: entry.category.value}
    assert actual == {action: category for category, actions in groups.items() for action in actions}
