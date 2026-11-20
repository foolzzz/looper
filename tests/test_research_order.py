"""The default Manager's research order (Settings → Chat, ``manager.research_order``): which sources a turn has, from
its typed tools, library and runtime, and the section they make; off, the Manager's prompt is as before."""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pytest

from looper.agents import chat_settings
from looper.agents.library import AgentLibrary, ItemKind, LoadedItem
from looper.agents.research_order import ResearchSources, section
from looper.agents.runtimes import ADAPTERS

FULL = ResearchSources(vault=True, records=True, leads=True, installed=("github", "lark-cli"), web=True)
GOLDEN_FULL = """\
## Looking things up
When an answer needs facts you do not have, look for them in this order, and stop as soon as you have enough to answer. Say where each fact came from.
1. The user's knowledge vault: their notes and what earlier work kept (looper_vault_search, then looper_vault_read).
2. Looper's own records: tasks, their requirements and results, and projects with their background (looper_tasks, looper_projects).
3. Message leads from the user's chats (looper_leads_search): unconfirmed background, as its section says.
4. The tools installed for you: github, lark-cli.
5. The web: search, then read the pages that answer it.
Ask the user only for what none of these has, or for a decision that is theirs to make.
"""
GOLDEN_GROUP = """\
## Looking things up
When an answer needs facts you do not have, look for them in this order, and stop as soon as you have enough to answer. Say where each fact came from.
1. Looper's own records: tasks, their requirements and results, and projects with their background (looper_tasks, looper_projects).
2. The web: search, then read the pages that answer it.
Ask the user only for what none of these has, or for a decision that is theirs to make.
"""


def test_the_section_lists_the_sources_in_order_then_asking_the_user() -> None:
    assert section(FULL) == GOLDEN_FULL
    assert section(ResearchSources(records=True, web=True)) == GOLDEN_GROUP


def test_many_installed_tools_are_named_up_to_twelve() -> None:
    names = tuple(f"tool-{n:02d}" for n in range(15))
    assert "tool-11 and 3 more." in section(ResearchSources(installed=names))


def _listing(*names: str) -> list[dict[str, Any]]:
    return [{"name": name, "description": "", "inputSchema": {}} for name in names]


OPERATOR = _listing("looper_overview", "looper_tasks", "looper_projects", "looper_confirm", "looper_leads_search")
GROUP = _listing("looper_overview", "looper_tasks", "looper_projects", "looper_confirm")
SCENARIO = _listing("looper_tasks", "looper_task_create", "looper_projects", "looper_schedules", "looper_confirm")
LIBRARY = AgentLibrary(items=(LoadedItem(ItemKind.MCP, "looper", "a" * 64, plugin="", scope="builtin"),
                              LoadedItem(ItemKind.MCP, "github", "b" * 64, plugin=""),
                              LoadedItem(ItemKind.SKILL, "lark-cli", "c" * 64)))


def _launch(runtime: str = "claude-code", *, web_search: bool = True) -> Any:
    return SimpleNamespace(runtime=ADAPTERS[runtime], library=LIBRARY, profile=SimpleNamespace(web_search=web_search))


@pytest.mark.parametrize("tools, launch, expected", [
    # The user's own turn: Looper's records, the leads search, what is installed, the web.
    (OPERATOR, _launch(), ResearchSources(records=True, leads=True, installed=("github", "lark-cli"), web=True)),
    # A group turn, or a scenario Manager: no leads search listed, so none named.
    (GROUP, _launch(), ResearchSources(records=True, installed=("github", "lark-cli"), web=True)),
    (SCENARIO, _launch(), ResearchSources(records=True, installed=("github", "lark-cli"), web=True)),
    # The vault's tools, when a turn lists both.
    ([*GROUP, *_listing("looper_vault_search", "looper_vault_read")], _launch(),
     ResearchSources(vault=True, records=True, installed=("github", "lark-cli"), web=True)),
    # The API runtime loads no library and has no web tools; a Grok profile may turn its web tools off.
    (OPERATOR, _launch("api"), ResearchSources(records=True, leads=True)),
    # Grok loads MCP servers but not skills, and its profile may turn its web tools off.
    (OPERATOR, _launch("grok", web_search=False), ResearchSources(records=True, leads=True, installed=("github",))),
])
def test_the_sources_come_from_the_turns_tools_library_and_runtime(tools: Any, launch: Any,
                                                                   expected: ResearchSources) -> None:
    assert ResearchSources.of(tools, launch) == expected


def test_the_leads_line_appears_only_when_the_leads_search_is_listed() -> None:
    assert "looper_leads_search" in section(ResearchSources.of(OPERATOR, _launch()))
    for tools in (GROUP, SCENARIO):
        assert "leads" not in section(ResearchSources.of(tools, _launch())).lower()


def _composed(research: ResearchSources | None, *, leads: bool = False, agent_id: str = "manager") -> Any:
    from looper.agents.agent_settings import AgentSettings
    from looper.agents.profile_registry import builtin_resolved_profile
    from looper.agents.tiers.manager import ManagerAgent

    settings = AgentSettings(profile=replace(builtin_resolved_profile(), custom_prompt=""))
    manager = SimpleNamespace(agent_id=agent_id, _system_prompt="ROLE", _prompt_recommended=False,
                              _leads_on=leads, **({"_research": research} if research is not None else {}))
    return ManagerAgent._composed_prompt(manager, settings)


def test_off_the_managers_prompt_is_exactly_as_before() -> None:
    """The golden comparison: the prompt composed without a research order is the one main composes (its contract
    alone), with and without leads; with one, only the section is added, after the contract."""
    from looper.agents.agent_settings import AgentSettings
    from looper.agents.profile_registry import builtin_resolved_profile
    from looper.agents.system_prompt import build_system_prompt
    from looper.agents.tiers.manager import TOOLS_CONTRACT, root_adjusted

    settings = AgentSettings(profile=replace(builtin_resolved_profile(), custom_prompt=""))
    main = build_system_prompt(harness=settings.harness, custom_prompt="", role_id="manager", role="ROLE",
                               recommended=False, contract_name="TOOLS_CONTRACT", contract=root_adjusted(TOOLS_CONTRACT))
    off = _composed(None)
    assert (off.text, off.layers) == (main.text, main.layers)
    on = _composed(FULL)
    assert on.layers["contract"]["name"] == "TOOLS_CONTRACT+RESEARCH_ORDER"
    assert on.text.replace("\n" + GOLDEN_FULL, "", 1) == off.text
    with_leads = _composed(FULL, leads=True)
    assert with_leads.layers["contract"]["name"] == "TOOLS_CONTRACT+LEADS_CONTRACT+RESEARCH_ORDER"


def test_the_setting_is_off_by_default_and_checked() -> None:
    assert chat_settings.DEFAULT_RESEARCH_ORDER is False and chat_settings.research_order() is False
    assert chat_settings.checked({"research_order": True}) == {"research_order": True}
    assert chat_settings.checked({"research_order": None}) == {"research_order": None}
    with pytest.raises(ValueError, match="research_order must be true, false or null"):
        chat_settings.checked({"research_order": "on"})


@pytest.mark.parametrize("agent_id, on, offered", [("manager", True, True), ("manager", False, False),
                                                    ("manager-desk", True, False)])
def test_only_the_default_manager_gets_a_research_order_and_only_while_it_is_on(
    monkeypatch: pytest.MonkeyPatch, agent_id: str, on: bool, offered: bool,
) -> None:
    from looper.agents.tiers.manager import ManagerAgent

    monkeypatch.setattr(chat_settings, "_research_order", on)
    found = ManagerAgent._research_sources(SimpleNamespace(agent_id=agent_id), OPERATOR, _launch())
    assert (found is not None) is offered
