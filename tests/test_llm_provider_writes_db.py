"""Account writes and reference safety on the target schema, without HTTP."""
from __future__ import annotations

import asyncio
from dataclasses import asdict
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from looper.agents import profile_registry as service
from looper.db import connection, profile_registry as db
from tests.db_support import DSN, run_db

needs_db = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")
SECRET = "sk-synthetic-provider-write-only"
URL = "https://api.example.invalid/messages"


def _run(test, monkeypatch):
    async def setup(pool):
        monkeypatch.setattr(service, "_login_modules", lambda: {})

    run_db(test, monkeypatch, setup=setup, timeout=45)


def _body(**changes):
    return {"name": "Test account", "vendor_type": "custom",
            "endpoints": [{"protocol": "anthropic-messages", "base_url": URL}], **changes}


def test_valid_custom_account_and_vendor_immutability():
    fields, key = service._provider_fields(_body(api_key=SECRET), None)
    assert fields['vendor_type'] == 'custom' and fields['auth'] == 'api_key' and key == SECRET
    assert fields['endpoints'] == [{"protocol": "anthropic-messages", "base_url": URL,
                                   "use_runtime_default": False}]
    current = {**fields, 'id': 'prov-test'}
    with pytest.raises(service.RegistryError):
        service._provider_fields({'vendor_type': 'deepseek'}, current)
    updated, key = service._provider_fields({'name': 'Renamed'}, current)
    assert updated['endpoints'] == fields['endpoints'] and key is ...


@pytest.mark.parametrize('changes,expected', [({}, ...), ({'api_key': '***'}, ...),
                                            ({'api_key': None}, None), ({'api_key': ''}, None),
                                            ({'api_key': SECRET}, SECRET)])
def test_key_update_semantics(changes, expected):
    fields, _ = service._provider_fields(_body(), None)
    _, key = service._provider_fields(changes, {**fields, 'id': 'prov-test', 'api_key_set': True})
    assert key == expected


def test_defaults_only_apply_on_creation_and_migration_marker_cannot_be_forged():
    fields, _ = service._provider_fields({'name': 'Vendor', 'vendor_type': 'deepseek'}, None)
    assert fields['endpoints'] and fields['env'] and 'models' not in fields
    current = {**fields, 'id': 'prov-test', 'env': [], 'endpoints': []}
    changed, _ = service._provider_fields({'name': 'Renamed'}, current)
    assert changed['env'] == [] and changed['endpoints'] == []
    for vendor in ('cpa', 'sub2api', 'opencode'):
        with pytest.raises(service.RegistryError):
            service._provider_fields({'name': 'Needs URL', 'vendor_type': vendor}, None)
    current = {**fields, 'id': 'prov-test', 'endpoints': [
        {'protocol': 'anthropic-messages', 'base_url': '', 'use_runtime_default': True}]}
    same, _ = service._provider_fields({'name': 'Keep migrated'}, current)
    assert same['endpoints'][0]['use_runtime_default'] is True
    changed, _ = service._provider_fields({'endpoints': [
        {'protocol': 'anthropic-messages', 'base_url': URL, 'use_runtime_default': True}]}, current)
    assert changed['endpoints'][0]['use_runtime_default'] is False


@pytest.mark.parametrize('changes', [{'max_concurrency': 0}, {'max_concurrency': True},
                                    {'max_concurrency': 1001}, {'env': [{'key': 'API_KEY', 'value': SECRET}]},
                                    {'env': [{'key': 'ANTHROPIC_BASE_URL', 'value': URL}]}])
def test_concurrency_and_environment_limits(changes):
    with pytest.raises(service.RegistryError):
        service._provider_fields(_body(**changes), None)


def test_revalidation_checks_every_profiles_exact_protocol(monkeypatch):
    fields, _ = service._provider_fields(_body(), None)
    model = asdict(service.ModelSpec('model-test', 'prov-test', 'test-model', 'anthropic-messages'))
    endpoint = {'provider_id': 'prov-test', **fields['endpoints'][0]}
    row = {'id': 'agent-test', 'name': 'First profile', 'model_id': 'model-test', 'model': model,
           'endpoint': endpoint, 'runtime_id': 'rt-claude-code', 'runtime_name': 'claude-code',
           'runtime_type': 'claude-code', 'runtime_capabilities': [], 'agent_teams': False}
    profiles = AsyncMock(return_value=[row])
    monkeypatch.setattr(db, 'list_profiles', profiles)
    async def run():
        await service._check_provider_profiles(None, {**fields, 'id': 'prov-test'})
        profiles.return_value = [row, {**row, 'id': 'agent-second', 'name': 'Second profile',
                                      'runtime_type': 'codex'}]
        with pytest.raises(service.RegistryError, match='Second profile'):
            await service._check_provider_profiles(None, {**fields, 'id': 'prov-test'})
    asyncio.run(asyncio.wait_for(run(), timeout=5))


async def _seed(pool, *, profile=False):
    # Seed the target schema directly, independently of the write path under test.
    await pool.execute("INSERT INTO llm_providers (id, name, vendor_type, auth) "
                       "VALUES ('prov-test', 'Saved account', 'custom', 'api_key'); "
                       "INSERT INTO llm_provider_endpoints (provider_id, protocol, base_url) "
                       "VALUES ('prov-test', 'anthropic-messages', 'https://api.example.invalid/messages'); "
                       "INSERT INTO llm_models (id, provider_id, name, protocol) "
                       "VALUES ('model-test', 'prov-test', 'test-model', 'anthropic-messages')")
    if profile:
        await pool.execute("INSERT INTO agent_profiles (id, name, runtime_id, model_id) "
                           "VALUES ('agent-test', 'Test profile', 'rt-claude-code', 'model-test')")


@pytest.mark.parametrize("changes", [
    {"endpoints": None}, {"endpoints": []},
    {"endpoints": [{"protocol": "anthropic-messages", "base_url": ""}]},
    {"endpoints": [{"protocol": "anthropic-messages", "base_url": "", "use_runtime_default": True}]},
    {"endpoints": [{"protocol": "anthropic-messages", "base_url": URL}] * 2},
    {"vendor_type": "codex-login", "endpoints": [{"protocol": "openai-responses", "base_url": URL}]},
    {"vendor_type": "codex-login", "endpoints": [{"protocol": "openai-chat", "base_url": ""}]},
])
def test_invalid_account_fields(changes):
    with pytest.raises(service.RegistryError):
        service._provider_fields(_body(**changes), None)


@pytest.mark.parametrize("url", ["https://user:pass@example.invalid", "https://example.invalid/?api_key=secret",
                                 "https://example.invalid/#secret", "https://example.invalid/?access-token=secret"])
def test_secret_urls_are_rejected(url):
    with pytest.raises(service.RegistryError):
        service._provider_fields(_body(endpoints=[{"protocol": "openai-chat", "base_url": url}]), None)


@needs_db
def test_creation_defaults_are_copied_once_without_models(monkeypatch):
    async def test(pool):
        answer = await service.create_provider({"name": "Vendor account", "vendor_type": "deepseek"})
        account = answer["provider"]
        pid = account["provider_id"]
        assert account["endpoints"] and account["env"] and account["model_count"] == 0
        assert await db.list_models(provider_id=pid) == []
        changed = [{"protocol": "openai-chat", "base_url": "https://saved.example.invalid/v1"}]
        await service.update_provider(pid, {"endpoints": changed, "env": [], "max_concurrency": 7})
        updated = (await service.update_provider(pid, {"name": "Renamed"}))["provider"]
        assert updated["env"] == [] and updated["max_concurrency"] == 7
        assert updated["endpoints"] == [{"provider_id": pid, **changed[0], "use_runtime_default": False}]
        with pytest.raises(service.RegistryError):
            await service.update_provider(pid, {"vendor_type": "custom"})
        assert (await db.get_provider(pid))["vendor_type"] == "deepseek"
        assert await service.delete_provider(pid) == {"deleted": pid}
        assert await db.get_provider(pid) is None
    _run(test, monkeypatch)


@needs_db
def test_keys_are_isolated_omitted_masked_replaced_and_removed(monkeypatch):
    async def test(pool):
        account = (await service.create_provider(_body(api_key=SECRET)))["provider"]
        pid = account["provider_id"]
        assert account["api_key"] == "***" and SECRET not in repr(account)
        for changes in ({"name": "Renamed"}, {"api_key": "***"}):
            answer = await service.update_provider(pid, changes)
            assert SECRET not in repr(answer) and await db.get_api_key(pid) == SECRET
        await service.update_provider(pid, {"api_key": SECRET + "-new"})
        assert await db.get_api_key(pid) == SECRET + "-new"
        assert not {"api_key", "models", "base_url", "protocols"} & (await db.get_provider(pid)).keys()
        await service.update_provider(pid, {"api_key": None})
        assert await db.get_api_key(pid) is None
        assert (await db.get_provider(pid))["api_key_set"] is False
    _run(test, monkeypatch)


@needs_db
def test_model_references_block_endpoint_and_provider_deletion(monkeypatch):
    async def test(pool):
        await _seed(pool)
        for operation in (lambda: service.update_provider('prov-test', {"endpoints": []}),
                          lambda: service.delete_provider('prov-test')):
            with pytest.raises(service.RegistryConflict):
                await operation()
            assert await db.get_model('model-test') is not None
            assert (await db.get_provider('prov-test'))['endpoints'][0]['base_url'] == URL
        await pool.execute("DELETE FROM llm_models WHERE id = 'model-test'")
        await service.update_provider('prov-test', {"endpoints": []})
        assert (await db.get_provider('prov-test'))['endpoints'] == []
        await service.delete_provider('prov-test')
    _run(test, monkeypatch)


@needs_db
def test_profiles_revalidated_and_failed_update_rolls_back_key_and_url(monkeypatch):
    async def test(pool):
        await _seed(pool, profile=True)
        await service.update_provider('prov-test', {"api_key": SECRET})
        # A deliberately invalid saved pairing proves revalidation covers real profiles.
        await pool.execute("INSERT INTO agent_runtimes (id, name, type, executable, capabilities) "
                           "VALUES ('rt-test', 'Test runtime', 'codex', 'codex', '{}'); "
                           "UPDATE agent_profiles SET runtime_id = 'rt-test' WHERE id = 'agent-test'")
        with pytest.raises(service.RegistryError, match="Test profile"):
            await service.update_provider('prov-test', {"api_key": None, "env": [{"key": "REGION", "value": "test"}],
                "endpoints": [{"protocol": "anthropic-messages", "base_url": "https://changed.example.invalid"}]})
        assert await db.get_api_key('prov-test') == SECRET
        assert (await db.get_provider('prov-test'))['endpoints'][0]['base_url'] == URL
        assert (await db.get_provider('prov-test'))['env'] == []
    _run(test, monkeypatch)


@needs_db
def test_url_change_clears_migration_marker(monkeypatch):
    async def test(pool):
        await _seed(pool)
        await pool.execute("UPDATE llm_provider_endpoints SET base_url = '', use_runtime_default = true "
                           "WHERE provider_id = 'prov-test'")
        await service.update_provider('prov-test', {"name": "Keep migrated URL"})
        assert (await db.get_provider('prov-test'))['endpoints'][0]['use_runtime_default'] is True
        answer = await service.update_provider('prov-test', {"endpoints": [
            {"protocol": "anthropic-messages", "base_url": URL, "use_runtime_default": True}]})
        assert answer['provider']['endpoints'][0]['use_runtime_default'] is False
    _run(test, monkeypatch)


@needs_db
def test_builtin_protection_and_login_clear_without_real_homes(monkeypatch):
    async def test(pool):
        for operation in (lambda: service.update_provider('prov-codex-login', {"name": "Changed"}),
                          lambda: service.delete_provider('prov-codex-login')):
            with pytest.raises(service.RegistryError):
                await operation()
        module = SimpleNamespace(clear_problem=AsyncMock(), login_view=AsyncMock(return_value={"state": "ready"}))
        monkeypatch.setattr(service, '_login_modules', lambda: {'codex': module})
        answer = await service.clear_login_problem('prov-codex-login')
        module.clear_problem.assert_awaited_once()
        assert answer['provider']['login'] == {"state": "ready"}
        await _seed(pool)
        with pytest.raises(service.RegistryError):
            await service.clear_login_problem('prov-test')
    _run(test, monkeypatch)


@needs_db
def test_concurrent_provider_updates_do_not_lose_keys_or_fields(monkeypatch):
    async def test(pool):
        await _seed(pool)
        await asyncio.gather(service.update_provider('prov-test', {"name": "Concurrent name"}),
                             service.update_provider('prov-test', {"max_concurrency": 9, "api_key": SECRET}))
        account = await db.get_provider('prov-test')
        assert account['name'] == 'Concurrent name' and account['max_concurrency'] == 9
        assert await db.get_api_key('prov-test') == SECRET
        results = await asyncio.gather(service.create_provider(_body()), service.create_provider(_body()),
                                       return_exceptions=True)
        assert sum(isinstance(result, service.RegistryConflict) for result in results) == 1
        assert sum(isinstance(result, dict) for result in results) == 1
    _run(test, monkeypatch)


@needs_db
def test_profile_creation_waits_for_provider_change_and_rechecks_model(monkeypatch):
    async def test(pool):
        await _seed(pool)
        locking = asyncio.Event()
        original = db.lock_provider
        async def lock(conn, provider_id):
            locking.set()
            return await original(conn, provider_id)
        monkeypatch.setattr(db, 'lock_provider', lock)
        async with pool.acquire() as conn, conn.transaction():
            await conn.execute("SELECT 1 FROM llm_providers WHERE id = 'prov-test' FOR UPDATE")
            pending = asyncio.create_task(service.create_profile({"name": "Concurrent profile",
                "runtime_id": "rt-claude-code", "model_id": "model-test"}))
            await asyncio.wait_for(locking.wait(), timeout=2)
            assert not pending.done()
            await conn.execute("DELETE FROM llm_models WHERE id = 'model-test'; "
                               "DELETE FROM llm_provider_endpoints WHERE provider_id = 'prov-test'")
        with pytest.raises((service.RegistryError, service.RegistryNotFound)):
            await pending
        assert await pool.fetchval("SELECT count(*) FROM agent_profiles WHERE name = 'Concurrent profile'") == 0
    _run(test, monkeypatch)


@needs_db
def test_model_creation_waits_for_provider_deletion(monkeypatch):
    async def test(pool):
        await _seed(pool)
        await pool.execute("DELETE FROM llm_models WHERE id = 'model-test'")
        locking = asyncio.Event()
        original = db.lock_provider
        async def lock(conn, provider_id):
            locking.set()
            return await original(conn, provider_id)
        monkeypatch.setattr(db, 'lock_provider', lock)
        async def create():
            async with connection.transaction() as conn:
                return await db.insert_model(conn, {'provider_id': 'prov-test', 'name': 'new-model',
                                                    'protocol': 'anthropic-messages'})
        async with pool.acquire() as conn, conn.transaction():
            await conn.execute("SELECT 1 FROM llm_providers WHERE id = 'prov-test' FOR UPDATE")
            pending = asyncio.create_task(create())
            await asyncio.wait_for(locking.wait(), timeout=2)
            assert not pending.done()
            await conn.execute("DELETE FROM llm_providers WHERE id = 'prov-test'")
        import asyncpg
        with pytest.raises(asyncpg.ForeignKeyViolationError):
            await pending
        assert await pool.fetchval("SELECT count(*) FROM llm_models WHERE provider_id = 'prov-test'") == 0
    _run(test, monkeypatch)


@needs_db
def test_profile_creation_references_exact_model_and_provider_updates_preserve_it(monkeypatch):
    async def test(pool):
        await _seed(pool)
        answer = await service.create_profile({'name': 'Created profile', 'runtime_id': 'rt-claude-code',
                                               'model_id': 'model-test'})
        profile = answer['profile']
        assert profile['model_id'] == 'model-test' and profile['agent_teams'] is True
        before = await db.get_model('model-test')
        await service.update_provider('prov-test', {'env': [{'key': 'REGION', 'value': 'test'}],
            'endpoints': [{'protocol': 'anthropic-messages', 'base_url': 'https://changed.example.invalid'}]})
        after = await db.get_model('model-test')
        assert after['id'] == before['id'] and after['name'] == before['name']
        assert (await db.get_profile(profile['profile_id']))['model_id'] == 'model-test'
        updated = await service.update_profile(profile['profile_id'], {'custom_prompt': 'Saved prompt'})
        assert updated['profile']['custom_prompt'] == 'Saved prompt'
        assert updated['profile']['model_id'] == 'model-test'
    _run(test, monkeypatch)
