"""The launch loader resolves one model and its exact endpoint on disposable PostgreSQL."""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from looper.agents.agent_settings import AgentType, Launch, Layer, load_agent_settings
from looper.agents import profile_registry as registry
from looper.agents.runtimes import ADAPTERS
from looper.db import agent_setting_layers, profile_registry as db
from tests.db_support import DSN, run_db

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")


def _run(test, monkeypatch):
    async def setup(pool):
        for runtime in ADAPTERS.values():
            monkeypatch.setattr(runtime, "login_problem", AsyncMock(return_value=None))
        await pool.execute("""
            INSERT INTO projects (id, name) VALUES ('binding-project', 'Binding project');
            INSERT INTO tasks (id, project_id, requirement) VALUES
              ('binding-task', 'binding-project', 'Exercise model selection');
            INSERT INTO llm_providers (id, name, vendor_type, auth) VALUES
              ('prov-binding', 'Binding account', 'custom', 'api_key');
            INSERT INTO llm_provider_secrets (provider_id, api_key) VALUES
              ('prov-binding', 'synthetic-binding-secret');
            INSERT INTO llm_provider_endpoints (provider_id, protocol, base_url) VALUES
              ('prov-binding', 'anthropic-messages', 'https://messages.example.invalid'),
              ('prov-binding', 'openai-chat', 'https://chat.example.invalid'),
              ('prov-binding', 'openai-responses', 'https://responses.example.invalid');
        """)
        for index, protocol in enumerate(registry.ApiProtocol):
            await pool.execute("""INSERT INTO llm_models (id, provider_id, name, protocol, context_window)
                VALUES ($1, 'prov-binding', 'Exact.Model[1m]', $2, 128000)""", f'model-binding-{index}', protocol.value)
            await pool.execute("""INSERT INTO agent_profiles (id, name, runtime_id, model_id)
                VALUES ($1, $1, 'rt-claude-code', $2)""", f'profile-binding-{index}', f'model-binding-{index}')

    run_db(test, monkeypatch, setup=setup, timeout=30)


def test_profile_precedence_and_builtin_remain_unchanged(monkeypatch):
    async def test(pool):
        places = [("system", None, None), ("role", "worker", None),
                  ("project", None, "binding-project"), ("role_project", "worker", "binding-project")]
        for index, (scope, role, project) in enumerate(places):
            await pool.execute("""INSERT INTO agent_profiles (id, name, runtime_id, model_id)
                VALUES ($1, $1, 'rt-claude-code', 'model-binding-0')""", f'profile-layer-{index}')
            await agent_setting_layers.put_layer(scope, role, project, {"profile_id": f'profile-layer-{index}'})
        await agent_setting_layers.set_task_layer('binding-task', profiles={"worker": 'profile-binding-0'})
        for expected, source in [('profile-binding-0', Layer.TASK), ('profile-layer-3', Layer.ROLE_PROJECT),
                                 ('profile-layer-2', Layer.PROJECT), ('profile-layer-1', Layer.ROLE),
                                 ('profile-layer-0', Layer.SYSTEM), (registry.BUILTIN_PROFILE.id, Layer.BUILTIN)]:
            settings = await load_agent_settings('worker', 'binding-project', 'binding-task')
            assert settings.profile.id == expected and settings.profile_source is source
            assert settings.profile_error is None
            assert settings.resolved_model.model.name == settings.model
            if source is Layer.TASK:
                await agent_setting_layers.set_task_layer('binding-task', profiles={"worker": None})
            elif source is not Layer.BUILTIN:
                scope, role, project = places.pop()
                await agent_setting_layers.put_layer(scope, role, project, {"profile_id": None})
    _run(test, monkeypatch)


@pytest.mark.parametrize('index,protocol', list(enumerate(registry.ApiProtocol)))
def test_each_model_binds_only_its_endpoint_and_fails_before_request(index, protocol, monkeypatch):
    async def test(pool):
        # OMP supports all three APIs; Claude Code supports Messages only.
        await pool.execute("""INSERT INTO agent_runtimes (id, name, type, capabilities)
            VALUES ('rt-binding-omp', 'binding-omp', 'omp', $1)""", [c.value for c in ADAPTERS['omp'].capabilities])
        await pool.execute("UPDATE agent_profiles SET runtime_id = 'rt-binding-omp' WHERE id = $1",
                           f'profile-binding-{index}')
        await agent_setting_layers.put_layer('role', 'worker', None, {'profile_id': f'profile-binding-{index}'})
        settings = await load_agent_settings('worker')
        assert settings.profile_error is None
        launch = Launch(settings, role=AgentType.WORKER)
        assert settings.resolved_model.model.protocol is protocol
        assert launch.profile.provider.protocols == frozenset({protocol})
        from looper.agents.runtimes.omp import models_yml
        config = models_yml(launch)['providers']['looper']
        assert config['baseUrl'] == settings.resolved_model.endpoint.base_url
        assert config['api'] == ('openai-completions' if protocol is registry.ApiProtocol.OPENAI_CHAT else protocol.value)
        assert 'synthetic-binding-secret' not in repr(settings)
        assert 'synthetic-binding-secret' not in str(launch.record())
        if protocol is not registry.ApiProtocol.ANTHROPIC_MESSAGES:
            await pool.execute("UPDATE agent_profiles SET runtime_id = 'rt-claude-code' WHERE id = $1",
                               f'profile-binding-{index}')
            key = AsyncMock(side_effect=AssertionError('invalid pairing read a secret'))
            monkeypatch.setattr(db, 'get_api_key', key)
            invalid = await load_agent_settings('worker')
            assert invalid.profile.id == settings.profile.id and invalid.profile_error
            assert key.await_count == 0
            with pytest.raises(ValueError):
                Launch(invalid).args()
    _run(test, monkeypatch)


def test_reload_changes_binding_but_keeps_prior_run_price_snapshot(monkeypatch):
    async def test(pool):
        await agent_setting_layers.put_layer('role', 'worker', None, {'profile_id': 'profile-binding-0'})
        first = Launch(await load_agent_settings('worker'))
        await pool.execute("UPDATE llm_models SET context_window = 256000, input_usd_per_mtok = 7 "
                           "WHERE id = 'model-binding-0'")
        await pool.execute("UPDATE llm_provider_endpoints SET base_url = 'https://new.example.invalid' "
                           "WHERE provider_id = 'prov-binding' AND protocol = 'anthropic-messages'")
        second = Launch(await load_agent_settings('worker'))
        assert first.binding_sha256 != second.binding_sha256
        assert first.settings.resolved_model.model.context_window == 128000
        assert first.settings.resolved_model.model.input_usd_per_mtok is None
        assert second.settings.resolved_model.model.input_usd_per_mtok == 7
        await pool.execute("UPDATE llm_models SET input_usd_per_mtok = 9 WHERE id = 'model-binding-0'")
        third = Launch(await load_agent_settings('worker'))
        assert second.binding_sha256 != third.binding_sha256
        assert third.settings.resolved_model.model.input_usd_per_mtok == 9
    _run(test, monkeypatch)
