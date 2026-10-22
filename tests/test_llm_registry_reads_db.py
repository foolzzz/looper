"""Normalized registry reads: direct services and disposable PostgreSQL, never HTTP."""
from __future__ import annotations

import asyncio
import json
from dataclasses import asdict
import pytest

from looper.agents import profile_registry as service
from looper.agents.runtimes import AuthKind, VendorType
from looper.db import agent_setting_layers as layers, connection, profile_registry as db
from tests.test_catalog_db import DSN, _fresh_schema

SECRET = "sk-synthetic-registry-read-test-only"
needs_db = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")


def _run(test, monkeypatch):
    async def run():
        async with _fresh_schema() as pool:
            await connection.ensure_schema(pool)
            monkeypatch.setattr(connection, "_pool", pool)
            # Login state is external to the registry DB; never inspect personal CLI homes.
            monkeypatch.setattr(service, "_login_modules", lambda: {})
            await test(pool)
    asyncio.run(asyncio.wait_for(run(), timeout=45))


async def _seed(pool):
    await pool.execute("""
        INSERT INTO llm_providers (id, name, vendor_type, auth, env, max_concurrency)
        VALUES ('prov-test', 'A test account', 'custom', 'api_key',
                '[{"key":"REGION","value":"test"}]', 3),
               ('prov-empty', 'Z empty account', 'custom', 'api_key', '[]', NULL);
        INSERT INTO llm_provider_secrets VALUES ('prov-test', 'sk-synthetic-registry-read-test-only');
        INSERT INTO llm_provider_endpoints (provider_id, protocol, base_url) VALUES
          ('prov-test', 'openai-chat', 'https://chat.example.invalid/v1'),
          ('prov-test', 'anthropic-messages', 'https://messages.example.invalid');
        INSERT INTO llm_models (id, provider_id, name, protocol, catalog_order, context_window,
                                input_usd_per_mtok, efforts) VALUES
          ('model-z', 'prov-test', 'unknown-model', 'anthropic-messages', 0, 123456, 0, '{low,high}'),
          ('model-b', 'prov-test', 'same-name', 'openai-chat', 1, NULL, NULL, NULL),
          ('model-a', 'prov-test', 'same-name', 'anthropic-messages', 1, NULL, NULL, '{}');
        INSERT INTO agent_profiles (id, name, runtime_id, model_id, effort, agent_teams, custom_prompt) VALUES
          ('agent-test', 'A test profile', 'rt-claude-code', 'model-z', 'high', true, 'Test prompt'),
          ('agent-second', 'Z second profile', 'rt-claude-code', 'model-z', NULL, false, '');
        INSERT INTO projects (id, name) VALUES ('proj-test', 'Test project');
        INSERT INTO tasks (id, requirement, project_id) VALUES ('task-test', 'Read registry', 'proj-test');
        INSERT INTO agent_setting_layers (scope, role, project_id, profile_id) VALUES
          ('system', NULL, NULL, 'agent-test'), ('role', 'worker', NULL, 'agent-test'),
          ('project', NULL, 'proj-test', 'agent-test'),
          ('role_project', 'reviewer', 'proj-test', 'agent-test');
        INSERT INTO task_profile_overrides VALUES
          ('task-test', 'worker', 'agent-test'), ('task-test', 'reviewer', 'agent-test');
        INSERT INTO task_planner_profiles (task_id, role, profile_id) VALUES
          ('task-test', 'reviewer', 'agent-test');
        INSERT INTO task_steps (id, task_id, step_index, instruction) VALUES
          ('step-test', 'task-test', 0, 'Read settings');
        INSERT INTO task_planner_profiles (task_id, role, step_id, profile_id) VALUES
          ('task-test', 'worker', 'step-test', 'agent-test');
        INSERT INTO task_roles (task_id, role_key, version, kind, profile_id, role_prompt) VALUES
          ('task-test', 'test-role', 1, 'work', 'agent-test', 'Read registry');
        INSERT INTO project_allowed_profiles (project_id, profile_id) VALUES ('proj-test', 'agent-test');
        INSERT INTO schedules (id, project_id, name, cron, timezone, requirement) VALUES
          ('schedule-test', 'proj-test', 'Test schedule', '0 * * * *', 'UTC', 'Read registry');
        INSERT INTO schedule_profile_overrides VALUES
          ('schedule-test', 'worker', 'agent-test'), ('schedule-test', 'reviewer', 'agent-test');
    """)


@needs_db
def test_normalized_nested_reads_counts_order_and_missing(monkeypatch):
    async def test(pool):
        await _seed(pool)
        providers = await db.list_providers()
        assert [p['id'] for p in providers] == [
            'prov-claude-subscription', 'prov-codex-login', 'prov-grok-login', 'prov-test', 'prov-empty']
        provider = await db.get_provider('prov-test')
        assert provider == next(p for p in providers if p['id'] == 'prov-test')
        assert (provider['model_count'], provider['profile_count'], provider['api_key_set']) == (3, 2, True)
        assert [e['protocol'] for e in provider['endpoints']] == ['anthropic-messages', 'openai-chat']
        empty = await db.get_provider('prov-empty')
        assert (empty['endpoints'], empty['model_count'], empty['profile_count'], empty['api_key_set']) == ([], 0, 0, False)
        models = await db.list_models(provider_id='prov-test')
        assert [m['id'] for m in models] == ['model-z', 'model-a', 'model-b']
        assert [m['profile_count'] for m in models] == [2, 0, 0]
        assert await db.get_model('model-z') == models[0]
        profiles = await db.list_profiles(provider_id='prov-test')
        assert [p['id'] for p in profiles] == ['agent-test', 'agent-second']
        profile = await db.get_profile('agent-test')
        assert profile == profiles[0]
        assert profile['model_id'] == profile['model']['id'] == 'model-z'
        assert profile['provider_id'] == profile['model']['provider_id'] == profile['provider']['id'] == 'prov-test'
        assert profile['endpoint']['base_url'] == 'https://messages.example.invalid'
        assert profile['model']['input_usd_per_mtok'] == 0
        assert profile['model']['efforts'] == ['low', 'high']
        assert profile['provider']['env'] == [{'key': 'REGION', 'value': 'test'}]
        assert await db.list_profiles(provider_id='missing') == []
        assert await db.list_models(provider_id='missing') == []
        assert await db.get_provider('missing') is None
        assert await db.get_profile('missing') is None
        assert await db.get_model('missing') is None
        assert await db.login_profile_count('claude-code') == 1
        assert await db.login_profile_count('codex') == 0
        assert SECRET not in repr((providers, models, profiles))
        assert await db.get_api_key('prov-test') == SECRET  # positive control for the isolated secret path
    _run(test, monkeypatch)


@needs_db
def test_every_layer_and_usage_reads_the_same_model_reference(monkeypatch):
    async def test(pool):
        await _seed(pool)
        profile = await db.get_profile('agent-test')
        rows = await layers.list_layers('proj-test', task_id='task-test', step_id='step-test',
                                        task_role=('test-role', 1))
        assert {r['scope'] for r in rows if r.get('profile_id') == 'agent-test'} == {
            'system', 'role', 'project', 'role_project', 'task', 'planner_task', 'planner_step', 'task_role'}
        assert all(r['profile'] == profile for r in rows if r.get('profile_id') == 'agent-test')
        assert (await layers.profile_overrides('task-test'))['worker'] == profile
        assert (await layers.planner_profiles('task-test'))[0]['profile'] == profile
        usage = (await db.profile_usage())['agent-test']
        assert [r['scope'] for r in usage['layers']] == ['system', 'role', 'project', 'role_project']
        assert usage['task_count'] == 1 and usage['schedule_count'] == 1
        assert usage['allowed_in'] == ['Test project']
        view = service.profile_view(profile, usage)
        assert view['profile_id'] == 'agent-test' and view['used_by'] == usage
        assert view['roles'] == ['manager', 'planner', 'worker', 'reviewer']
        assert view['cost']['source'] == 'provider_price'
        assert view['cost']['unpriced'] == ['unknown-model'] and view['warnings']
        assert SECRET not in repr((rows, view))
        await pool.execute("DELETE FROM project_allowed_profiles WHERE project_id = 'proj-test'")
        await pool.execute("INSERT INTO project_allowed_profiles (project_id, profile_id) "
                           "VALUES ('proj-test', 'agent-claude-code-opus')")
        denied = await layers.list_layers('proj-test', task_id='task-test', step_id='step-test',
                                         task_role=('test-role', 1))
        assert not {'planner_task', 'planner_step', 'task_role'} & {r['scope'] for r in denied}
        assert await layers.planner_profiles('task-test') == []
    _run(test, monkeypatch)


@needs_db
def test_registry_service_lists_models_vendor_types_login_and_masks_keys(monkeypatch):
    async def test(pool):
        await _seed(pool)
        async def login_view():
            return {'state': 'signed_out', 'detail': 'synthetic login state'}
        from types import SimpleNamespace
        monkeypatch.setattr(service, '_login_modules', lambda: {'codex': SimpleNamespace(login_view=login_view)})
        view = await service.registry_view()
        assert {v['vendor_type'] for v in view['vendor_types']} == {v.value for v in VendorType}
        assert 'provider_presets' not in view
        account = next(p for p in view['providers'] if p['provider_id'] == 'prov-test')
        assert account['api_key'] == service.SECRET_MASK
        assert (account['model_count'], account['profile_count']) == (3, 2)
        assert not {'models', 'base_url', 'protocols'} & account.keys()
        login = next(p for p in view['providers'] if p['provider_id'] == 'prov-codex-login')
        assert login['builtin'] and login['login']['state'] == 'signed_out'
        assert next(m for m in view['models'] if m['model_id'] == 'model-z')['profile_count'] == 2
        assert view['prices'] and view['limits']
        assert SECRET not in json.dumps(view, default=str)
    _run(test, monkeypatch)


@needs_db
def test_builtin_restart_preserves_saved_account_and_model(monkeypatch):
    async def test(pool):
        await pool.execute("UPDATE llm_providers SET name = 'Saved subscription', max_concurrency = 7 "
                           "WHERE id = 'prov-claude-subscription'")
        await pool.execute("UPDATE llm_models SET context_window = 123, input_usd_per_mtok = 0 "
                           "WHERE provider_id = 'prov-claude-subscription' AND name = 'opus'")
        before = await db.get_profile('agent-claude-code-opus')
        await connection.ensure_schema(pool)
        await connection.ensure_schema(pool)
        assert await db.get_profile('agent-claude-code-opus') == before
        assert before['model_id'] == before['model']['id']
        assert before['provider']['name'] == 'Saved subscription'
        assert before['provider']['max_concurrency'] == 7
        assert before['model']['context_window'] == 123
    _run(test, monkeypatch)


def test_provider_spec_uses_account_fields_and_never_loads_a_key():
    spec = service.provider_spec({'id': 'prov-test', 'name': 'Test', 'vendor_type': 'custom',
                                 'auth': 'api_key', 'oauth_runtime_type': None, 'env': [],
                                 'max_concurrency': 3, 'builtin': False, 'api_key': SECRET})
    assert isinstance(spec, service.ProviderSpec)
    assert spec.vendor_type is VendorType.CUSTOM and spec.auth is AuthKind.API_KEY
    assert spec.max_concurrency == 3 and spec.api_key is None
    assert SECRET not in repr(asdict(spec))


def _profile_row():
    model = {"id": "model-test", "provider_id": "prov-test", "name": "unknown-model[1m]",
             "protocol": "anthropic-messages", **dict.fromkeys(service.MODEL_FIELDS[1:]),
             "input_usd_per_mtok": 0, "catalog_order": 0, "builtin": False}
    return {"id": "agent-test", "name": "Test profile", "model_id": "model-test", "model": model,
            "provider": {"id": "prov-test", "name": "Saved account", "vendor_type": "custom",
                         "auth": "api_key", "oauth_runtime_type": None, "env": [], "builtin": False},
            "endpoint": {"provider_id": "prov-test", "protocol": "anthropic-messages",
                         "base_url": "https://messages.example.invalid", "use_runtime_default": False},
            "runtime_id": "rt-claude-code", "runtime_name": "claude-code", "runtime_type": "claude-code",
            "runtime_capabilities": [c.value for c in service.BUILTIN_RUNTIME.capabilities],
            "agent_teams": True, "effort": "high", "custom_prompt": "Saved prompt", "builtin": False}


@pytest.mark.parametrize("runtime_type", ["claude-code", "omp"])
def test_profile_view_preserves_usage_cost_and_warnings_from_normalized_settings(runtime_type):
    row = _profile_row()
    row.update(runtime_type=runtime_type, agent_teams=runtime_type == "claude-code")
    # Stale directory fields must have no effect on resolution, pricing or display.
    row.update(provider_models=[{"name": "wrong-model"}], provider_base_url="https://api.anthropic.com")
    used = {"layers": [], "task_count": 2, "schedule_count": 1, "allowed_in": ["Project"]}
    view = service.profile_view(row, used)
    assert view['profile_id'] == 'agent-test' and view['model_id'] == 'model-test'
    assert view['model']['name'] == 'unknown-model[1m]'
    assert view['provider']['provider_id'] == 'prov-test' and view['provider_name'] == 'Saved account'
    assert view['roles'] == ['manager', 'planner', 'worker', 'reviewer'] and view['used_by'] == used
    assert view['cost'] == {'source': 'provider_price', 'equivalent': False, 'unpriced': ['unknown-model[1m]']}
    assert view['warnings'] and view['custom_prompt'] == 'Saved prompt'
    assert 'wrong-model' not in repr(view)


def test_profile_view_prices_in_launch_catalog_order_not_selected_model_first():
    row = _profile_row()
    row["model"].update(name="foo[1m]", background_model="foo-20250102", catalog_order=1,
                        **dict.fromkeys(service.PRICE_FIELDS, 1.0))
    earlier = {**row["model"], "id": "model-other", "name": "foo-20250101", "catalog_order": 0,
               "background_model": None, **dict.fromkeys(service.PRICE_FIELDS)}
    # The earlier, unpriced entry owns the shared normalized name, as it does at launch.
    assert service.profile_view(row, models=[earlier])["cost"]["unpriced"] == ["foo-20250102"]


def test_vendor_suggestions_are_stable_and_do_not_allocate_public_ids():
    first = service.vendor_types_view()
    assert first == service.vendor_types_view()
    assert [v['vendor_type'] for v in first] == [v.value for v in VendorType]
    login = next(v for v in first if v['vendor_type'] == 'codex-login')
    assert login['auth'] == 'oauth' and login['oauth_runtime_type'] == 'codex'
    assert login['endpoints'] == [{'protocol': 'openai-responses', 'base_url': '', 'use_runtime_default': False}]
    assert login['suggested_models']
    assert all('provider_id' not in endpoint for vendor in first for endpoint in vendor['endpoints'])
