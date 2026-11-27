"""What an API client's Manager session may use: an explicit list of task-control tools, never main's."""

from __future__ import annotations

from looper.agents import session_routing, session_visibility
from looper.agents.ops import Audience
from looper.agents.ops.service import _MANAGER_TOOL_NAMES, _OPERATOR_TOOLS, _audience

EXPECTED = frozenset({
    "looper_tasks", "looper_task_create", "looper_task_control", "looper_task_instruct", "looper_task_update",
})


def test_the_client_tool_list_is_exactly_the_listed_task_control_tools() -> None:
    assert session_visibility.CLIENT_TOOLS == EXPECTED
    assert session_visibility.CLIENT_ALLOWLIST.tools == EXPECTED


def test_every_listed_tool_is_a_manager_tool_open_to_any_turn() -> None:
    for name in EXPECTED:
        assert name in _MANAGER_TOOL_NAMES, name
        assert name not in _OPERATOR_TOOLS, name
        assert _audience(_MANAGER_TOOL_NAMES[name]) is Audience.ANY, name


def test_no_configuration_cost_or_confirmation_tool_is_listed() -> None:
    for name in ("looper_settings", "looper_projects", "looper_schedules", "looper_costs", "looper_overview",
                 "looper_confirm", "looper_task_gate_reply", "looper_providers", "looper_models", "looper_catalog", "looper_config_export",
                 "looper_agent_profiles", "looper_concurrency", "looper_session_search", "looper_vault_read"):
        assert name not in session_visibility.CLIENT_TOOLS, name


def test_a_client_session_has_the_client_allowlist_and_main_has_none() -> None:
    key = session_routing.client_key("client-1").text
    assert session_visibility.for_session(key, manager_id="manager") is session_visibility.CLIENT_ALLOWLIST
    assert session_visibility.for_session("main", manager_id="manager") is None
    # A scenario Manager's client messages stay on its own main: no client session there.
    assert session_visibility.for_session(key, manager_id="scenario") is None


def test_the_client_inputs_carry_no_operator_data() -> None:
    inputs = session_visibility.CLIENT_ALLOWLIST.inputs
    for item in (session_visibility.Input.PROFILE_PROMPT, session_visibility.Input.HARNESS,
                 session_visibility.Input.ROLE_PROMPT, session_visibility.Input.TOOLS_CONTRACT,
                 session_visibility.Input.OPERATOR_CONTRACT, session_visibility.Input.GOALS,
                 session_visibility.Input.OPEN_GATES, session_visibility.Input.READY_TASKS,
                 session_visibility.Input.GROUP_ACTIVITY):
        assert item not in inputs, item
