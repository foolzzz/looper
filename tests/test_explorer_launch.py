"""Every explorer launch has local reading tools and no platform authority."""
from dataclasses import replace
import json
from pathlib import Path
import tomllib

import pytest

from looper.agents.agent_settings import Access, AgentSettings, AgentType, Launch
from looper.agents.knowledge import AgentKnowledge
from looper.agents.library import AgentLibrary
from looper.agents.profile_registry import LegacyProviderSpec, RuntimeSpec
from looper.agents.runtimes import ApiProtocol, AuthKind, Capability, Launcher, RuntimeUnsupported, adapter
from tests.runtime_model_helpers import builtin_launch, resolved_profile


def launch_for(kind, *, capabilities=None, launcher=Launcher.NATIVE):
    runtime = adapter(kind)
    vendor = {'grok': 'grok', 'coco': 'coco'}.get(kind)
    if kind == 'codex' and launcher is Launcher.TTADK:
        vendor = 'codex'
    protocol = ApiProtocol.ANTHROPIC_MESSAGES if kind == 'dsh' else ApiProtocol.OPENAI_RESPONSES
    provider = LegacyProviderSpec('account', 'account', AuthKind.OAUTH if vendor else AuthKind.API_KEY,
        frozenset({protocol}), oauth_runtime_type=vendor, api_key=None if vendor else 'synthetic-key',
        base_url='' if vendor else 'https://endpoint.example.invalid/v1')
    profile = resolved_profile('profile', 'profile', RuntimeSpec('runtime', kind, kind,
        capabilities=runtime.capabilities if capabilities is None else capabilities, launcher=launcher),
        provider, 'deepseek-flash' if kind == 'dsh' else 'selected')
    return Launch(AgentSettings(profile=profile), access=Access.EXPLORE)


def flag(args, name):
    return args[args.index(name) + 1]


def test_explorer_discards_catalog_and_extra_directories(tmp_path):
    config = tmp_path / 'mcp.json'
    config.write_text(json.dumps({'mcpServers': {'external': {'command': 'never-start'}}}))
    launch = replace(builtin_launch(), access=Access.EXPLORE,
        library=AgentLibrary(plugin_dirs=('untrusted-plugin',), mcp_config=str(config)),
        knowledge=AgentKnowledge(add_dirs=('extra-directory',)), read_dirs=('another-directory',))
    assert launch.library == AgentLibrary()
    assert not launch.knowledge.add_dirs and not launch.read_dirs
    assert not launch.agent_teams and not launch.steers
    assert launch.read_only and launch.explore
    assert launch.runtime.loaded_sources(launch).mcp_servers == ()


def test_claude_explorer_only_read_tools_and_strict_empty_mcp(tmp_path):
    launch = replace(builtin_launch(), access=Access.EXPLORE)
    prompt = tmp_path / 'prompt.md'
    prompt.write_text('Explore')
    args = launch.runtime.run_command('claude', cwd=tmp_path, prompt_file=prompt, launch=launch)
    assert flag(args, '--tools') == 'Read,Glob,Grep'
    assert flag(args, '--allowedTools') == 'Read,Glob,Grep'
    assert '--disable-slash-commands' in args
    assert '--strict-mcp-config' in args
    assert '--mcp-config' not in args and '--plugin-dir' not in args
    assert '--teammate-mode' not in args
    assert '--permission-mode' in args and '--dangerously-skip-permissions' not in args
    assert 'CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS' not in launch.environment()


@pytest.mark.parametrize('kind', ['codex', 'coco'])
@pytest.mark.parametrize('launcher', [Launcher.NATIVE, Launcher.TTADK])
def test_sandbox_explorers_disable_web_and_native_agents(kind, launcher, tmp_path):
    from looper.agents.runtimes.codex import CODEX, config_toml
    from looper.agents.runtimes.coco import COCO
    launch = launch_for(kind, launcher=launcher)
    args = launch.args()
    assert 'sandbox_mode="read-only"' in args and 'approval_policy="never"' in args
    assert 'web_search="disabled"' in args and 'features.multi_agent=false' in args
    assert 'agents.enabled=false' in args and 'features.multi_agent_v2=false' in args
    config = tomllib.loads(config_toml(launch, shell_home=tmp_path / 'shell', home=COCO if kind == 'coco' else CODEX))
    assert 'mcp_servers' not in config
    assert config['web_search'] == 'disabled'
    assert config['features']['multi_agent'] is False
    assert config['features']['multi_agent_v2'] is False
    assert config['agents']['enabled'] is False


@pytest.mark.parametrize('kind,tools', [
    ('omp', 'read,glob,grep'), ('grok', 'read_file,grep,list_dir'), ('pi', 'read,grep,find,ls'),
])
def test_tool_explorers_only_allow_local_reads(kind, tools):
    launch = launch_for(kind)
    args = launch.args()
    assert flag(args, '--tools') == tools
    assert not any('mcp__' in arg for arg in args)
    assert launch.runtime.loaded_sources(launch).mcp_servers == ()
    assert not launch.runtime.web_tools(launch)
    if kind == 'grok':
        assert '--disable-web-search' in args
    if kind == 'pi':
        assert '-e' not in args and '--no-mcp' in args
        assert not any(name.startswith('LOOPER_MCP') for name in launch.environment())


def test_dsh_explorer_has_guard_no_mcp_no_web(tmp_path):
    from looper.agents.runtimes.dsh import _launch_patch, mcp_config
    launch = launch_for('dsh')
    config = {item['id']: item for item in _launch_patch(launch, tmp_path)}
    assert config['tool-web']['config']['fetch'] is False
    assert config['tool-web']['config']['search'] is False
    assert not launch.runtime.web_tools(launch)
    assert not mcp_config(launch.library, role='explorer', read_only=True, consult=launch.explore).servers
    guard = (Path(__file__).parents[1] / 'looper/agents/runtimes/dsh_bootstrap.mjs').read_text()
    assert "new Set(['read', 'glob', 'grep', ...platformTools])" in guard
    assert launch.read_only


@pytest.mark.parametrize('kind,capabilities', [
    ('cursor-agent', None), ('api', None), ('omp', frozenset({Capability.RUN})),
])
def test_check_only_runtime_refuses_explorer(kind, capabilities):
    with pytest.raises(RuntimeUnsupported, match='explor'):
        launch_for(kind, capabilities=capabilities)


def test_explorer_role_is_read_only_without_explicit_access_override():
    launch = replace(builtin_launch(), role=AgentType.EXPLORER)
    assert launch.access_level is Access.EXPLORE
    assert flag(launch.args(), '--tools') == 'Read,Glob,Grep'
