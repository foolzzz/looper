"""Claude Code can use Chat models only with an attached translation route."""
from dataclasses import replace
import asyncio
import json
from pathlib import Path
import stat
from unittest.mock import AsyncMock

import pytest

from looper import child_env
from looper.agents.agent_settings import AgentSettings, Launch
from looper.agents.llmrec import LlmrecRoute, LlmrecSkip, RouteKind, SkipReason
from looper.agents.profile_registry import CatalogModel, LegacyProviderSpec, RuntimeSpec
from looper.agents.runtimes import ApiProtocol, AuthKind, RuntimeUnsupported
from looper.agents.runtimes.claude_code import (
    API_KEY, AUTH_TOKEN, BASE_URL, MAIN_MODEL_VARS, BACKGROUND_MODEL_VARS, TRANSLATED_ENV,
)
from tests.runtime_model_helpers import builtin_launch, resolved_profile


@pytest.fixture
def translated_launch():
    provider = LegacyProviderSpec('test-provider', 'Test provider', AuthKind.API_KEY,
        frozenset({ApiProtocol.OPENAI_CHAT}), base_url='https://chat.example.invalid/v1', api_key='synthetic-key',
        catalog=(CatalogModel('chat-main', background_model='chat-small'),),
        env=(('ENABLE_TOOL_SEARCH', 'true'),))
    profile = resolved_profile('test-agent', 'Test agent', RuntimeSpec('test-runtime', 'Claude Code', 'claude-code'),
                               provider, 'chat-main')
    return Launch(AgentSettings(profile=profile, env=((API_KEY, 'settings-key'),
        ('CLAUDE_CODE_ATTRIBUTION_HEADER', '1'), ('CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS', '0'))))


def test_translated_environment_enforces_gateway_and_preserves_model_binding(translated_launch, monkeypatch):
    monkeypatch.setattr(child_env, 'inherited', lambda: {API_KEY: 'inherited-key', BASE_URL: 'https://wrong.invalid',
                                                        'NO_PROXY': 'example.invalid'})
    route = LlmrecRoute('abcdefghijklmnopqrstuv', 'http://127.0.0.1:12345/r/abcdefghijklmnopqrstuv',
                        'https://chat.example.invalid/v1', kind=RouteKind.TRANSLATE, record=False,
                        local_token='local-route-token')
    routed = replace(translated_launch, llmrec=route)
    env = routed.environment()
    assert env[BASE_URL] == route.url
    assert env[AUTH_TOKEN] == route.local_token
    assert 'synthetic-key' not in json.dumps(env)
    assert API_KEY not in env
    assert all(env[name] == value for name, value in TRANSLATED_ENV)
    assert all(env[name] == 'chat-main' for name in MAIN_MODEL_VARS)
    assert all(env[name] == 'chat-small' for name in BACKGROUND_MODEL_VARS)
    assert '127.0.0.1' in env['NO_PROXY'].split(',')
    binding = routed.runtime.bind_provider(routed.profile.provider, routed.model, routed.effort)
    assert BASE_URL not in dict(binding.env)
    assert binding.unset == (API_KEY, BASE_URL)
    assert routed.binding_sha256 == translated_launch.binding_sha256
    assert routed.runtime.llmrec_upstream(routed) == route.upstream
    record = routed.record()
    assert record['llm_model']['path'] == 'translated'
    assert record['llmrec'] == {'state': 'translating', 'route': route.id, 'recording': False,
        'upstream_host': 'chat.example.invalid', 'dialect': 'generic'}
    assert 'synthetic-key' not in json.dumps(record)
    assert route.local_token not in json.dumps(record)


@pytest.mark.parametrize('source', ['inherited', 'shared', 'provider'])
def test_translated_environment_disables_other_backends(translated_launch, monkeypatch, source):
    selectors = dict.fromkeys((
        'CLAUDE_CODE_USE_BEDROCK', 'CLAUDE_CODE_USE_VERTEX', 'CLAUDE_CODE_USE_FOUNDRY',
        'CLAUDE_CODE_USE_ANTHROPIC_AWS', 'CLAUDE_CODE_USE_ANTHROPIC_GOOGLE_CLOUD',
        'CLAUDE_CODE_USE_MANTLE', 'CLAUDE_CODE_USE_GATEWAY',
    ), '1')
    monkeypatch.setattr(child_env, 'inherited', lambda: selectors if source == 'inherited' else {})
    settings = translated_launch.settings
    if source == 'shared':
        settings = replace(settings, env=(*settings.env, *selectors.items()))
    elif source == 'provider':
        resolved = settings.profile.resolved_model
        provider = replace(resolved.provider, env=(*resolved.provider.env, *selectors.items()))
        profile = replace(settings.profile, resolved_model=replace(resolved, provider=provider))
        settings = replace(settings, profile=profile)
    route = LlmrecRoute('abcdefghijklmnopqrstuv', 'http://127.0.0.1:12345/r/abcdefghijklmnopqrstuv',
                        'https://chat.example.invalid/v1', kind=RouteKind.TRANSLATE, record=False,
                        local_token='local-route-token')
    env = replace(translated_launch, settings=settings, llmrec=route).environment()
    assert env[BASE_URL] == route.url
    assert {name: env.get(name) for name in selectors} == dict.fromkeys(selectors, '0')


@pytest.mark.parametrize('resident', [False, True])
@pytest.mark.parametrize('translated', [False, True])
def test_backend_selection_settings_are_forced_only_for_translation(translated_launch, tmp_path, resident, translated):
    route = LlmrecRoute('abcdefghijklmnopqrstuv', 'http://127.0.0.1:12345/r/abcdefghijklmnopqrstuv',
                        'https://chat.example.invalid/v1', kind=RouteKind.TRANSLATE, record=False,
                        local_token='local-route-token')
    launch = replace(translated_launch, llmrec=route) if translated else builtin_launch()
    command = launch.runtime.resident_command if resident else launch.runtime.run_command
    args = command('claude', cwd=tmp_path, prompt_file=tmp_path / 'prompt.md', launch=launch)
    assert args.count('--settings') == 1
    settings_arg = args[args.index('--settings') + 1]
    if translated:
        settings_file = Path(settings_arg)
        assert settings_file.is_absolute()
        assert stat.S_IMODE(settings_file.stat().st_mode) == 0o600
        assert 'synthetic-key' not in settings_file.read_text()
        settings = json.loads(settings_file.read_text())
        # These explicit values win even when the CLI loads project env after process env.
        assert settings['env'] == {
            'ANTHROPIC_MODEL': 'chat-main',
            **dict.fromkeys(MAIN_MODEL_VARS, 'chat-main'),
            **dict.fromkeys(BACKGROUND_MODEL_VARS, 'chat-small'),
            BASE_URL: route.url,
            AUTH_TOKEN: route.local_token,
            API_KEY: '',
            'CLAUDE_CODE_USE_BEDROCK': '0',
            'CLAUDE_CODE_USE_VERTEX': '0',
            'CLAUDE_CODE_USE_FOUNDRY': '0',
            'CLAUDE_CODE_USE_ANTHROPIC_AWS': '0',
            'CLAUDE_CODE_USE_ANTHROPIC_GOOGLE_CLOUD': '0',
            'CLAUDE_CODE_USE_MANTLE': '0',
            'CLAUDE_CODE_USE_GATEWAY': '0',
            'CLAUDE_CODE_ATTRIBUTION_HEADER': '0',
            'ENABLE_TOOL_SEARCH': 'false',
            'CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS': '1',
        }
        assert settings['autoMemoryEnabled'] is False
        assert settings['crossSessionInbound'] == 'refuse'
        assert settings['claudeMdExcludes']
        assert 'synthetic-key' not in json.dumps([args, launch.record()])
        # Residents compare commands between turns; reuse the path, including after file damage.
        settings_file.write_text('{}')
        settings_file.chmod(0o644)
        assert command('claude', cwd=tmp_path, prompt_file=tmp_path / 'prompt.md', launch=launch) == args
        assert json.loads(settings_file.read_text()) == settings
        assert stat.S_IMODE(settings_file.stat().st_mode) == 0o600
    else:
        settings = json.loads(settings_arg)
        assert 'env' not in settings


@pytest.mark.parametrize('resident', [False, True])
def test_translated_private_settings_pin_models_over_project_settings(translated_launch, tmp_path, resident):
    project = tmp_path / '.claude'
    project.mkdir()
    (project / 'settings.json').write_text(json.dumps({'env': {'ANTHROPIC_DEFAULT_HAIKU_MODEL': 'unrouted'}}))
    route = LlmrecRoute('abcdefghijklmnopqrstuv', 'http://127.0.0.1:12345/r/abcdefghijklmnopqrstuv',
                        'https://chat.example.invalid/v1', kind=RouteKind.TRANSLATE, record=False,
                        local_token='local-route-token')
    launch = replace(translated_launch, llmrec=route)
    command = launch.runtime.resident_command if resident else launch.runtime.run_command
    args = command('claude', cwd=tmp_path, prompt_file=tmp_path / 'prompt.md', launch=launch)
    settings = json.loads(Path(args[args.index('--settings') + 1]).read_text())
    process_env = launch.environment()
    project_env = json.loads((project / 'settings.json').read_text())['env']
    effective_env = {**process_env, **project_env, **settings['env']}
    assert effective_env['ANTHROPIC_DEFAULT_HAIKU_MODEL'] == 'chat-small'
    for name in ('ANTHROPIC_MODEL', *MAIN_MODEL_VARS, *BACKGROUND_MODEL_VARS):
        assert settings['env'][name] == process_env[name]


@pytest.mark.parametrize('route,diagnostic', [
    (None, 'not installed in this process'),
    (LlmrecRoute('a' * 22, 'http://127.0.0.1:12345/r/test', 'https://chat.example.invalid',
                 kind=RouteKind.TRANSLATE), 'not installed in this process'),
    (LlmrecSkip(SkipReason.GATEWAY_UNAVAILABLE, 'startup failed'), 'could not start: startup failed'),
    (LlmrecSkip(SkipReason.PROXY_OUTDATED), 'older version kept alive'),
    (LlmrecSkip(SkipReason.CREDENTIALS_IN_URL), 'URL contains credentials'),
    (LlmrecRoute('abcdefghijklmnopqrstuv', 'http://127.0.0.1:12345/r/test', 'https://chat.example.invalid'),
     'not installed in this process'),
])
def test_translated_launch_refuses_missing_translation_route(translated_launch, route, diagnostic):
    launch = replace(translated_launch, llmrec=route)
    with pytest.raises(RuntimeUnsupported, match=diagnostic):
        launch.environment()
    assert launch.record()['llmrec']['state'] == 'refused'
    assert diagnostic in launch.record()['llmrec']['detail']


@pytest.mark.parametrize('resident', [False, True])
def test_gateway_refusal_is_deferred_until_process_environment(translated_launch, tmp_path, resident):
    launch = replace(translated_launch, llmrec=LlmrecSkip(SkipReason.GATEWAY_UNAVAILABLE, 'startup failed'))
    command = launch.runtime.resident_command if resident else launch.runtime.run_command
    # Tiers construct commands before creating the Runs record; validation belongs at process startup.
    args = command('claude', cwd=tmp_path, prompt_file=tmp_path / 'prompt.md', launch=launch)
    settings = json.loads(Path(args[args.index('--settings') + 1]).read_text())
    assert settings['env'] == {**dict(TRANSLATED_ENV), AUTH_TOKEN: '', API_KEY: '',
        'ANTHROPIC_MODEL': 'chat-main', **dict.fromkeys(MAIN_MODEL_VARS, 'chat-main'),
        **dict.fromkeys(BACKGROUND_MODEL_VARS, 'chat-small')}
    assert launch.record()['llmrec']['state'] == 'refused'
    with pytest.raises(RuntimeUnsupported, match='could not start: startup failed'):
        launch.environment()


def test_translated_launch_refuses_empty_upstream_before_starting_route(translated_launch, tmp_path, monkeypatch):
    from looper.agents.llmrec import ROUTES_DIR, control

    resolved = translated_launch.profile.resolved_model
    endpoint = replace(resolved.endpoint, base_url='', use_runtime_default=True)
    profile = replace(translated_launch.profile, resolved_model=replace(resolved, endpoint=endpoint))
    launch = replace(translated_launch, settings=replace(translated_launch.settings, profile=profile))
    owner = control.ProxyControl(tmp_path, '', enabled=False)
    monkeypatch.setattr(control, '_control', owner)
    start = AsyncMock(side_effect=AssertionError('invalid upstream must not start a proxy'))
    monkeypatch.setattr(owner, '_start', start)
    diagnostic = "Cannot prepare Claude Code: the model's OpenAI endpoint has no base URL"
    with pytest.raises(RuntimeUnsupported, match=diagnostic):
        asyncio.run(control.attach(launch, agent_id='worker-test', task_id=None))
    with pytest.raises(RuntimeUnsupported, match=diagnostic):
        launch.runtime.bind_provider(launch.settings.resolved_model.binding_provider(), launch.model, launch.effort)
    start.assert_not_awaited()
    assert not list((tmp_path / ROUTES_DIR).glob('*.json'))


@pytest.mark.parametrize('official', [True, False])
def test_direct_environment_is_byte_stable(official, monkeypatch, tmp_path):
    monkeypatch.setattr(child_env, 'inherited', lambda: {'PATH': '/synthetic/bin', API_KEY: 'old',
        AUTH_TOKEN: 'old-token', BASE_URL: 'https://old.invalid', 'ENABLE_TOOL_SEARCH': 'false'})
    provider = LegacyProviderSpec('test-provider', 'Test provider', AuthKind.API_KEY,
        frozenset({ApiProtocol.ANTHROPIC_MESSAGES}), api_key='synthetic-key',
        base_url='' if official else 'https://messages.example.invalid', catalog=(CatalogModel('haiku'),))
    profile = resolved_profile('test-agent', 'Test agent', RuntimeSpec('rt', 'Claude Code', 'claude-code'),
                               provider, 'haiku', agent_teams=False)
    launch = Launch(AgentSettings(profile=profile, env=(('CUSTOM_SETTING', 'kept'),)))
    expected = {'PATH': '/synthetic/bin', 'ENABLE_TOOL_SEARCH': 'false', 'CUSTOM_SETTING': 'kept'}
    if official:
        expected[API_KEY] = 'synthetic-key'
    else:
        expected.update({BASE_URL: provider.base_url, AUTH_TOKEN: 'synthetic-key',
                         **dict.fromkeys((*MAIN_MODEL_VARS, *BACKGROUND_MODEL_VARS), 'haiku')})
    assert json.dumps(launch.environment(), sort_keys=True) == json.dumps(expected, sort_keys=True)
    for command in (launch.runtime.run_command, launch.runtime.resident_command):
        args = command('claude', cwd=tmp_path, prompt_file=tmp_path / 'prompt.md', launch=launch)
        assert 'synthetic-key' not in json.dumps([args, launch.record()])
    assert all(b'synthetic-key' not in path.read_bytes() for path in tmp_path.rglob('*') if path.is_file())


def test_default_legacy_launch_keeps_its_direct_environment(monkeypatch):
    monkeypatch.setattr(child_env, 'inherited', lambda: {'PATH': '/synthetic/bin', 'CUSTOM_ENV': 'preserved'})
    default = Launch()
    assert 'llmrec' not in default.record()
    profile = replace(default.profile, provider=replace(default.profile.provider,
        catalog=(CatalogModel(default.model),)), agent_teams=True)
    launch = Launch(AgentSettings(profile=profile))
    assert launch.environment() == {'PATH': '/synthetic/bin', 'CUSTOM_ENV': 'preserved',
                                    'CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS': '1'}
    assert 'llmrec' not in launch.record()
