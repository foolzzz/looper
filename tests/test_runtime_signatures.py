"""Every runtime adapter accepts the arguments its callers pass (the abstract signatures).

A runtime added before a new parameter reached the abstract method once kept the old signature, and every Grok
Worker run then failed with a TypeError that no unit test of the runtime itself could see.
"""

from __future__ import annotations

import asyncio
from dataclasses import asdict, replace
import inspect
import json
import sys
from unittest.mock import AsyncMock

import pytest

from looper.agents.agent_settings import AgentSettings, Launch, Layer, load_agent_settings
from looper.agents.profile_registry import (
    Endpoint, ModelSpec, ProviderSpec, ResolvedModel, ResolvedAgentProfile, RuntimeSpec,
)
from looper.agents.runtimes import ApiProtocol, AuthKind, RuntimeAdapter, VendorType, adapter, can_resume

ADAPTER_TYPES = ("claude-code", "codex", "omp", "grok", "dsh", "pi")
METHODS = [name for name, member in vars(RuntimeAdapter).items()
           if getattr(member, "__isabstractmethod__", False) and name.endswith("_command")]


@pytest.mark.parametrize("runtime_type", ADAPTER_TYPES)
@pytest.mark.parametrize("method", METHODS)
def test_an_adapter_accepts_every_parameter_of_the_abstract_command(runtime_type: str, method: str) -> None:
    abstract = inspect.signature(getattr(RuntimeAdapter, method))
    concrete = inspect.signature(getattr(type(adapter(runtime_type)), method))
    missing = [name for name in abstract.parameters if name not in concrete.parameters]
    assert not missing, f"{runtime_type}.{method} lacks {missing}"


def bound_profile(runtime_type='omp', protocol=ApiProtocol.OPENAI_CHAT):
    runtime = adapter(runtime_type)
    provider = ProviderSpec('prov-test', 'Test account', VendorType.CUSTOM, AuthKind.API_KEY,
                            api_key='synthetic-secret')
    model = ModelSpec('model-test', provider.id, 'Exact.Model[1m]', protocol, context_window=128000,
                      input_usd_per_mtok=1, output_usd_per_mtok=2)
    resolved = ResolvedModel(model, provider, Endpoint(provider.id, protocol, 'https://example.invalid/v1'))
    return ResolvedAgentProfile('profile-test', 'Test profile', RuntimeSpec('rt-test', 'Test runtime', runtime_type,
                                capabilities=runtime.capabilities), model.id, resolved_model=resolved)


@pytest.mark.parametrize('part,changes', [
    ('model', {'name': 'Exact.Model'}), ('model', {'protocol': ApiProtocol.OPENAI_RESPONSES}),
    ('model', {'context_window': 256000}), ('model', {'background_model': 'Background.Model'}),
    ('model', {'efforts': ()}), ('endpoint', {'base_url': 'https://new.example.invalid/v1'}),
    ('provider', {'api_key': 'rotated-synthetic-secret'}), ('provider', {'env': (('SETTING', 'changed'),)}),
    ('profile', {'effort': 'high'}), ('profile', {'custom_prompt': 'A different identity'}),
])
def test_effective_model_binding_changes_the_resident_signature(part, changes, tmp_path):
    profile = bound_profile()
    first = Launch(AgentSettings(profile=profile))
    resolved = profile.resolved_model
    if part == 'profile':
        profile = replace(profile, **changes)
    else:
        changed = replace(getattr(resolved, part), **changes)
        updates = {part: changed}
        if part == 'model' and 'protocol' in changes:
            updates['endpoint'] = replace(resolved.endpoint, protocol=changed.protocol)
        profile = replace(profile, resolved_model=replace(resolved, **updates))
    second = Launch(AgentSettings(profile=profile))
    assert first.binding_sha256 != second.binding_sha256
    session = first.runtime.resident_session('binding-test')
    asyncio.run(session.start(['env', f'HOME={tmp_path}', sys.executable], cwd=str(tmp_path),
                              signature=first.binding_sha256))
    assert session.started and not session.stale(first.binding_sha256)
    assert session.stale(second.binding_sha256)
    asyncio.run(session.stop())
    assert can_resume(first.profile.runtime.type, first.profile.provider.id, second.runtime, second.profile.provider.id)


def test_price_changes_refresh_the_next_turn_pricer_and_leave_prior_run_snapshot_intact():
    from looper.agents.run_usage import Usage
    profile = bound_profile()
    first = Launch(AgentSettings(profile=profile))
    changed = replace(profile.resolved_model.model, input_usd_per_mtok=7, catalog_order=9)
    second = Launch(AgentSettings(profile=replace(profile, resolved_model=replace(profile.resolved_model, model=changed))))
    assert first.binding_sha256 != second.binding_sha256
    usage = Usage(input_tokens=1_000_000, output_tokens=0)
    assert first.price(first.model, usage) == 1
    assert second.price(second.model, usage) == 7
    reordered = replace(profile.resolved_model.model, catalog_order=9)
    third = Launch(AgentSettings(profile=replace(profile, resolved_model=replace(profile.resolved_model, model=reordered))))
    assert first.binding_sha256 == third.binding_sha256


def test_binding_secrets_and_internal_fingerprint_are_not_recorded_or_represented(caplog):
    launch = Launch(AgentSettings(profile=bound_profile()))
    signature = launch.binding_sha256
    public = repr(launch) + json.dumps(launch.record()) + caplog.text
    assert 'synthetic-secret' not in public
    assert signature not in public
    assert 'binding_sha256' not in launch.record()


def _profile_row(profile):
    resolved = profile.resolved_model
    row = {'id': profile.id, 'name': profile.name, 'model_id': profile.model_id,
           'runtime_id': profile.runtime.id, 'runtime_name': profile.runtime.name,
           'runtime_type': profile.runtime.type, 'runtime_capabilities': [c.value for c in profile.runtime.capabilities],
           'provider': {**asdict(resolved.provider), 'api_key': None, 'env': []},
           'model': asdict(resolved.model), 'endpoint': asdict(resolved.endpoint)}
    row['provider'].pop('api_key')
    return row


@pytest.fixture
def pricing_catalog(monkeypatch):
    from looper.db import profile_registry as db

    # The selected model is already in the profile; these unit fixtures have no auxiliary models.
    monkeypatch.setattr(db, 'list_models', AsyncMock(return_value=[]))


def test_loader_builds_exact_model_binding_before_reading_its_key(monkeypatch, pricing_catalog):
    from looper.db import agent_setting_layers, profile_registry as db
    profile = bound_profile()
    resolved = profile.resolved_model
    row = _profile_row(profile)
    monkeypatch.setattr(agent_setting_layers, 'list_layers', AsyncMock(return_value=[
        {'scope': 'role', 'role': 'worker', 'profile': row}]))
    key = AsyncMock(return_value='launch-only-secret')
    monkeypatch.setattr(db, 'get_api_key', key)
    settings = asyncio.run(load_agent_settings('worker'))
    assert settings.profile_error is None
    assert settings.resolved_model.model == resolved.model
    assert settings.resolved_model.provider.api_key == 'launch-only-secret'
    assert settings.profile.provider.base_url == resolved.endpoint.base_url
    assert settings.profile.provider.protocols == {resolved.model.protocol}
    assert 'launch-only-secret' not in repr(settings)
    assert key.await_args.args == ('prov-test',)


def test_runtime_and_provider_changes_still_start_new_sessions():
    launch = Launch(AgentSettings(profile=bound_profile()))
    assert not can_resume('codex', launch.profile.provider.id, launch.runtime, launch.profile.provider.id)
    assert not can_resume('omp', 'another-account', launch.runtime, launch.profile.provider.id)


@pytest.mark.parametrize('damage', ['runtime', 'endpoint', 'model_id'])
def test_invalid_binding_keeps_the_selected_profile_and_fails_before_secret_read(damage, monkeypatch):
    from looper.db import agent_setting_layers, profile_registry as db
    row = _profile_row(bound_profile())
    if damage == 'runtime':
        row['runtime_type'] = 'claude-code'
    elif damage == 'endpoint':
        row['endpoint']['protocol'] = 'anthropic-messages'
    else:
        row['model_id'] = 'model-other'
    monkeypatch.setattr(agent_setting_layers, 'list_layers', AsyncMock(return_value=[
        {'scope': 'role', 'role': 'worker', 'profile': row}]))
    key = AsyncMock(side_effect=AssertionError('invalid binding read a secret'))
    monkeypatch.setattr(db, 'get_api_key', key)
    settings = asyncio.run(load_agent_settings('worker'))
    assert settings.profile.id == row['id'] and settings.profile_error
    assert key.await_count == 0
    with pytest.raises(ValueError):
        Launch(settings).args()


@pytest.mark.parametrize('protocol', list(ApiProtocol))
def test_loader_never_substitutes_another_protocol_endpoint(protocol, monkeypatch, pricing_catalog):
    from looper.db import agent_setting_layers, profile_registry as db
    row = _profile_row(bound_profile(protocol=protocol))
    monkeypatch.setattr(agent_setting_layers, 'list_layers', AsyncMock(return_value=[
        {'scope': 'role', 'role': 'worker', 'profile': row}]))
    monkeypatch.setattr(db, 'get_api_key', AsyncMock(return_value='synthetic-secret'))
    settings = asyncio.run(load_agent_settings('worker'))
    assert settings.profile_error is None
    assert settings.resolved_model.model.protocol is protocol
    from looper.agents.runtimes.omp import models_yml
    config = models_yml(Launch(settings))['providers']['looper']
    assert config['api'] == ('openai-completions' if protocol is ApiProtocol.OPENAI_CHAT else protocol.value)
    assert config['baseUrl'] == row['endpoint']['base_url']


def test_loader_preserves_profile_precedence_through_every_layer_to_builtin(monkeypatch, pricing_catalog):
    from looper.agents.profile_registry import builtin_resolved_profile
    from looper.db import agent_setting_layers, profile_registry as db
    places = [(Layer.SYSTEM, None), (Layer.ROLE, 'worker'), (Layer.PROJECT, None),
              (Layer.ROLE_PROJECT, 'worker'), (Layer.TASK, 'worker')]
    rows = [{'scope': layer.value, 'role': role,
             'profile': _profile_row(replace(bound_profile(), id=f'profile-{layer.value}'))}
            for layer, role in places]
    monkeypatch.setattr(agent_setting_layers, 'list_layers', AsyncMock(return_value=rows))
    builtin = builtin_resolved_profile()
    monkeypatch.setattr(db, 'get_profile', AsyncMock(return_value=_profile_row(builtin)))
    monkeypatch.setattr(db, 'get_api_key', AsyncMock(return_value='launch-only-secret'))
    monkeypatch.setattr(adapter('claude-code'), 'login_problem', AsyncMock(return_value=None))

    async def run():
        for source, _ in reversed(places):
            settings = await load_agent_settings('worker', 'project', 'task')
            assert settings.profile_source is source
            assert settings.profile.id == f'profile-{source.value}'
            assert settings.profile_error is None
            assert settings.resolved_model.model.id == 'model-test'
            rows.pop()
        settings = await load_agent_settings('worker', 'project', 'task')
        assert settings.profile.id == builtin.id and settings.profile_source is Layer.BUILTIN
        assert settings.resolved_model == builtin.resolved_model
        assert settings.profile_error is None
    asyncio.run(run())


@pytest.mark.parametrize('failure', [None, RuntimeError('synthetic-secret')])
def test_missing_or_unreadable_key_fails_launch_without_leaking_secrets(failure, monkeypatch, caplog):
    from looper.db import agent_setting_layers, profile_registry as db
    row = _profile_row(bound_profile())
    monkeypatch.setattr(agent_setting_layers, 'list_layers', AsyncMock(return_value=[
        {'scope': 'role', 'role': 'worker', 'profile': row}]))
    monkeypatch.setattr(db, 'get_api_key', AsyncMock(return_value=None, side_effect=failure))
    settings = asyncio.run(load_agent_settings('worker'))
    assert settings.profile_error
    with pytest.raises(ValueError):
        Launch(settings).environment()
    assert 'synthetic-secret' not in caplog.text + repr(settings)


@pytest.mark.parametrize("session_id", [None, ""])
def test_an_empty_session_with_a_selected_profile_starts_clean(session_id):
    from looper.agents.session_switch import profile_switch
    launch = Launch(AgentSettings(profile=bound_profile()))
    assert profile_switch(session_id, None, launch.profile.id, launch, session_provider=None) is None
    assert not can_resume(None, None, adapter("claude-code"), "prov-claude-subscription")
