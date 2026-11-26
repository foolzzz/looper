"""The default Manager's research order (Settings → Chat, ``manager.research_order``): which sources a turn has, from
its typed tools, library and runtime, and the section they make; off, the Manager's prompt is as before."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
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


@pytest.fixture
def library(tmp_path: Path) -> AgentLibrary:
    """A Manager's library as ``load_installed`` writes it: Looper's own server, a stdio and an SSE server in the
    ``--mcp-config`` file, and a plugin with one skill."""
    config = tmp_path / "mcp.json"
    config.write_text(json.dumps({"mcpServers": {
        "looper": {"command": "python", "args": ["-m", "looper.cli", "mcp-server"]},
        "github": {"command": "github-mcp", "args": []},
        "events": {"type": "sse", "url": "https://events.example.invalid/sse"}}}))
    plugin = tmp_path / "plugins" / "looper-skills"
    (plugin / "skills" / "lark-cli").mkdir(parents=True)
    (plugin / "skills" / "lark-cli" / "SKILL.md").write_text("---\nname: lark-cli\ndescription: Lark.\n---\nUse it.\n")
    items = (LoadedItem(ItemKind.MCP, "looper", "a" * 64, plugin="", scope="builtin"),
             LoadedItem(ItemKind.MCP, "github", "b" * 64, plugin=""),
             LoadedItem(ItemKind.MCP, "events", "c" * 64, plugin=""),
             LoadedItem(ItemKind.SKILL, "lark-cli", "d" * 64))
    return AgentLibrary(items=items, plugin_dirs=(str(plugin),), mcp_config=str(config))


def _launch(runtime: str, library: AgentLibrary, *, web_search: bool = True, env: tuple = ()) -> Any:
    return SimpleNamespace(runtime=ADAPTERS[runtime], library=library, env=env, explore=False,
                           profile=SimpleNamespace(web_search=web_search))


def _claude_launch(library: AgentLibrary, *, base_url: str | None = None, env: tuple = ()) -> Any:
    """A real Claude Code launch: the built-in login, or an API key for a compatible endpoint at ``base_url``; ``env``
    is the settings' environment."""
    from looper.agents.agent_settings import AgentSettings, Launch
    from looper.agents.profile_registry import LegacyProviderSpec, RuntimeSpec, builtin_resolved_profile
    from looper.agents.runtimes import ApiProtocol, AuthKind
    from tests.runtime_model_helpers import resolved_profile

    if base_url is None:
        return Launch(AgentSettings(profile=builtin_resolved_profile(), env=env), library)
    provider = LegacyProviderSpec("compatible", "Compatible", AuthKind.API_KEY, frozenset({ApiProtocol.ANTHROPIC_MESSAGES}),
                                  base_url=base_url, api_key="synthetic-key")
    profile = resolved_profile("compatible-agent", "Compatible agent",
                               RuntimeSpec("rt-claude", "Claude Code", "claude-code"), provider, "model-x")
    return Launch(AgentSettings(profile=profile, env=env), library)


def test_the_installed_tools_are_what_the_runtime_really_loads(library: AgentLibrary) -> None:
    # Claude Code loads the whole --mcp-config file and every plugin directory.
    assert ResearchSources.of(OPERATOR, _claude_launch(library)).installed == ("events", "github", "lark-cli")
    # Codex (and Coco, which runs on its configuration) has no SSE transport, and gets the skills as documents.
    for runtime in ("codex", "coco"):
        assert ResearchSources.of(OPERATOR, _launch(runtime, library)).installed == ("github", "lark-cli"), runtime
    # Grok reaches stdio and SSE servers, and scans the plugins' skill directories.
    assert ResearchSources.of(OPERATOR, _launch("grok", library)).installed == ("events", "github", "lark-cli")
    # Pi loads no catalog server or skill; the API runtime nothing; a runtime not checked claims nothing.
    for runtime in ("pi", "api", "omp", "dsh", "cursor-agent"):
        assert ResearchSources.of(OPERATOR, _launch(runtime, library)).installed == (), runtime


@pytest.mark.parametrize("switch", ["CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY"])
@pytest.mark.parametrize("base_url", [None, "https://api.anthropic.com"])
def test_claude_code_on_another_backend_has_no_web_search(monkeypatch: pytest.MonkeyPatch, library: AgentLibrary,
                                                          switch: str, base_url: str | None) -> None:
    """Bedrock, Vertex or Foundry, chosen in the settings' environment or inherited from the server's, take
    WebSearch away whatever the profile's endpoint; a switch set off does not."""
    from looper import child_env

    on = ResearchSources.of(OPERATOR, _claude_launch(library, base_url=base_url, env=((switch, "1"),)))
    assert not on.web
    assert ResearchSources.of(OPERATOR, _claude_launch(library, base_url=base_url, env=((switch, "0"),))).web
    inherited = child_env.inherited
    monkeypatch.setattr(child_env, "inherited", lambda: {**inherited(), switch: "true"})
    assert not ResearchSources.of(OPERATOR, _claude_launch(library, base_url=base_url)).web


def test_the_web_is_named_only_where_the_launch_has_web_tools(library: AgentLibrary) -> None:
    assert ResearchSources.of(OPERATOR, _claude_launch(library)).web  # Anthropic's own API: WebSearch
    assert not ResearchSources.of(OPERATOR, _claude_launch(library, base_url="https://gateway.example.invalid")).web
    assert ResearchSources.of(OPERATOR, _launch("grok", library)).web
    assert not ResearchSources.of(OPERATOR, _launch("grok", library, web_search=False)).web
    assert not ResearchSources.of(OPERATOR, _launch("dsh", library)).web  # DSH_ALLOW_WEB_SEARCH is off by default
    assert ResearchSources.of(OPERATOR, _launch("dsh", library, env=(("DSH_ALLOW_WEB_SEARCH", "true"),))).web
    for runtime in ("pi", "api", "codex", "omp", "cursor-agent", "coco"):  # not shown to have web tools
        assert not ResearchSources.of(OPERATOR, _launch(runtime, library)).web, runtime


@pytest.mark.parametrize("tools, expected", [
    (OPERATOR, ResearchSources(records=True, leads=True)),  # the user's own turn
    (GROUP, ResearchSources(records=True)),  # a group turn: no leads search listed, none named
    (SCENARIO, ResearchSources(records=True)),
    ([*GROUP, *_listing("looper_vault_search", "looper_vault_read")], ResearchSources(vault=True, records=True)),
])
def test_the_listed_sources_come_from_the_turns_own_tools(tools: Any, expected: ResearchSources) -> None:
    assert ResearchSources.of(tools, _launch("pi", AgentLibrary())) == expected


def test_the_leads_line_appears_only_when_the_leads_search_is_listed() -> None:
    assert "looper_leads_search" in section(ResearchSources.of(OPERATOR, _launch("pi", AgentLibrary())))
    for tools in (GROUP, SCENARIO):
        assert "leads" not in section(ResearchSources.of(tools, _launch("pi", AgentLibrary()))).lower()


def _composed(research: ResearchSources | None, *, leads: bool = False, agent_id: str = "manager") -> Any:
    from looper.agents.agent_settings import AgentSettings
    from looper.agents.profile_registry import builtin_resolved_profile
    from looper.agents.tiers.manager import ManagerAgent

    settings = AgentSettings(profile=replace(builtin_resolved_profile(), custom_prompt=""))
    manager = SimpleNamespace(agent_id=agent_id, _system_prompt="ROLE", _prompt_recommended=False,
                              _session_now=lambda: SimpleNamespace(key="main", scope=None),
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
    found = ManagerAgent._research_sources(SimpleNamespace(agent_id=agent_id), OPERATOR, _launch("pi", AgentLibrary()))
    assert (found is not None) is offered


def test_the_stable_context_contract_stays_last_after_the_research_order() -> None:
    from looper.agents.tiers import state_view

    composed = _composed(FULL)
    with_stable = _composed_with(FULL, _stable_context=True)
    assert with_stable.text.endswith(state_view.STABLE_CONTEXT_CONTRACT.rstrip("\n") + "\n") or \
        with_stable.text.rstrip().endswith(state_view.STABLE_CONTEXT_CONTRACT.rstrip())
    assert with_stable.text.index(GOLDEN_FULL) < with_stable.text.index(state_view.STABLE_CONTEXT_CONTRACT.strip())
    assert with_stable.layers["contract"]["name"] == "TOOLS_CONTRACT+RESEARCH_ORDER+STABLE_CONTEXT_CONTRACT"
    assert GOLDEN_FULL in composed.text


def _composed_with(research: ResearchSources, **fields: Any) -> Any:
    from looper.agents.agent_settings import AgentSettings
    from looper.agents.profile_registry import builtin_resolved_profile
    from looper.agents.tiers.manager import ManagerAgent

    settings = AgentSettings(profile=replace(builtin_resolved_profile(), custom_prompt=""))
    manager = SimpleNamespace(agent_id="manager", _system_prompt="ROLE", _prompt_recommended=False, _leads_on=False,
                              _session_now=lambda: SimpleNamespace(key="main", scope=None),
                              _research=research, **fields)
    return ManagerAgent._composed_prompt(manager, settings)
