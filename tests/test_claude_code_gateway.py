"""Claude Code can use Chat models only with an attached translation route."""
from dataclasses import replace
import json

import pytest

from looper import child_env
from looper.agents.agent_settings import AgentSettings, Launch
from looper.agents.llmrec import LlmrecRoute, LlmrecSkip, RouteKind, SkipReason
from looper.agents.profile_registry import CatalogModel, LegacyProviderSpec, RuntimeSpec
from looper.agents.runtimes import ApiProtocol, AuthKind, RuntimeUnsupported
from looper.agents.runtimes.claude_code import (
    API_KEY, AUTH_TOKEN, BASE_URL, MAIN_MODEL_VARS, BACKGROUND_MODEL_VARS, TRANSLATED_ENV,
)
from tests.runtime_model_helpers import resolved_profile


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
                        'https://chat.example.invalid/v1', kind=RouteKind.TRANSLATE, record=False)
    routed = replace(translated_launch, llmrec=route)
    env = routed.environment()
    assert env[BASE_URL] == route.url
    assert env[AUTH_TOKEN] == 'synthetic-key'
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


@pytest.mark.parametrize('route,diagnostic', [
    (None, 'not installed in this process'),
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


@pytest.mark.parametrize('official', [True, False])
def test_direct_environment_is_byte_stable(official, monkeypatch):
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
