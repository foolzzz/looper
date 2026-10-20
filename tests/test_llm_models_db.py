"""Model CRUD contracts on disposable PostgreSQL, directly through the registry."""
from __future__ import annotations

import asyncio
import uuid
from dataclasses import asdict
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from looper.agents import profile_registry as service
from looper.db import connection, llm_records, profile_registry as db
from tests.test_catalog_db import DSN, _fresh_schema

needs_db = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")


def _run(test, monkeypatch):
    async def run():
        async with _fresh_schema() as pool:
            await connection.ensure_schema(pool)
            monkeypatch.setattr(connection, "_pool", pool)
            monkeypatch.setattr(service, "_login_modules", lambda: {})
            await pool.execute("""
                INSERT INTO llm_providers (id, name, vendor_type, auth) VALUES
                  ('prov-test', 'Test account', 'custom', 'api_key'),
                  ('prov-other', 'Other account', 'custom', 'api_key');
                INSERT INTO llm_provider_endpoints (provider_id, protocol, base_url) VALUES
                  ('prov-test', 'anthropic-messages', 'https://messages.example.invalid'),
                  ('prov-test', 'openai-chat', 'https://chat.example.invalid'),
                  ('prov-other', 'anthropic-messages', 'https://other.example.invalid');
            """)
            await test(pool)
    asyncio.run(asyncio.wait_for(run(), timeout=30))


def _body(**changes):
    return {"provider_id": "prov-test", "name": "test-model", "protocol": "anthropic-messages", **changes}


def _row(**changes):
    model = service.ModelSpec.from_entry({"id": "model-test", **_body(**changes)})
    return {**asdict(model), "provider_name": "Test account", "profile_count": 0,
            "provider": {"id": "prov-test", "name": "Test account", "vendor_type": "custom",
                         "auth": "api_key", "oauth_runtime_type": None},
            "endpoint": {"provider_id": "prov-test", "protocol": model.protocol.value,
                         "base_url": "https://api.example.invalid", "use_runtime_default": False},
            "created_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
            "updated_at": datetime(2026, 1, 1, tzinfo=timezone.utc)}


@pytest.mark.parametrize("price,source,effective", [(None, "builtin", 1), (0, "override", 0), (2, "override", 2)])
def test_model_view_price_override_and_builtin_fallback(price, source, effective):
    view = service.model_view(_row(name="claude-haiku-4-5", input_usd_per_mtok=price))
    assert view["prices"]["input_usd_per_mtok"] == {"override": price, "effective": effective, "source": source}
    assert view["profile_count"] == 0 and view["builtin"] is False
    assert view["warnings"] == []


def test_unknown_prices_and_protocol_suffix_warnings():
    view = service.model_view(_row(name="unknown[1m]", protocol="openai-chat"))
    assert all(p == {"override": None, "effective": None, "source": "unknown"} for p in view["prices"].values())
    assert len(view["warnings"]) == 2
    assert "api_key" not in repr(view)


@pytest.mark.parametrize("changes", [{"provider_id": None}, {"protocol": ["openai-chat"]},
    {"context_window": 0}, {"context_window": True}, {"input_usd_per_mtok": -1},
    {"output_usd_per_mtok": float("nan")}, {"cache_read_usd_per_mtok": float("inf")},
    {"cache_write_usd_per_mtok": 10001}, {"efforts": ["bad value"]}, {"background_model": "bad name"}])
def test_create_rejects_invalid_input_before_accessing_database(changes):
    async def run():
        with pytest.raises(service.RegistryError):
            await service.create_model(_body(**changes))
    asyncio.run(asyncio.wait_for(run(), timeout=2))


@pytest.mark.parametrize("changes", [{"provider_id": "prov-other"}, {"name": "renamed"},
                                     {"protocol": "openai-chat"}, {"catalog_order": 1}, {"builtin": True}, {}])
def test_update_schema_rejects_identity_before_accessing_database(changes):
    async def run():
        with pytest.raises(service.RegistryError):
            await service.update_model("model-test", changes)
    asyncio.run(asyncio.wait_for(run(), timeout=2))


@pytest.mark.parametrize("runtime_type,protocols", [
    ("claude-code", {"anthropic-messages"}), ("codex", {"openai-responses"}),
    ("omp", {"anthropic-messages", "openai-chat", "openai-responses"}), ("grok", set())])
def test_service_runtime_filter_obeys_adapter_pairing(runtime_type, protocols, monkeypatch):
    rows = [_row(protocol=protocol.value) for protocol in service.ApiProtocol]
    monkeypatch.setattr(db, "list_models", AsyncMock(return_value=rows))
    monkeypatch.setattr(db, "get_runtime", AsyncMock(return_value={
        "id": "rt-test", "name": "Test runtime", "type": runtime_type, "executable": "", "capabilities": []}))
    async def run():
        result = await service.list_models(runtime_id="rt-test")
        assert {m["protocol"] for m in result["models"]} == protocols
    asyncio.run(asyncio.wait_for(run(), timeout=2))


@needs_db
def test_natural_keys_duplicates_and_readback(monkeypatch):
    async def test(pool):
        first = (await service.create_model(_body()))["model"]
        second = (await service.create_model(_body(protocol="openai-chat")))["model"]
        third = (await service.create_model(_body(provider_id="prov-other")))["model"]
        assert len({first["model_id"], second["model_id"], third["model_id"]}) == 3
        assert uuid.UUID(first["model_id"].removeprefix("model-")).version == 4
        with pytest.raises(service.RegistryConflict):
            await service.create_model(_body())
        models = (await service.list_models(provider_id="prov-test"))["models"]
        assert {m["model_id"] for m in models} == {first["model_id"], second["model_id"]}
        assert (await service.get_model(first["model_id"]))["model"] == first
        assert first["profile_count"] == 0 and first["builtin"] is False
        assert first["warnings"]
        assert await db.get_model(first["model_id"]) is not None
    _run(test, monkeypatch)


@needs_db
@pytest.mark.parametrize("changes", [{"provider_id": "missing"}, {"provider_id": None},
                                     {"protocol": "openai-responses"}, {"protocol": ["openai-chat"]}])
def test_missing_provider_or_exact_endpoint(changes, monkeypatch):
    async def test(pool):
        with pytest.raises((service.RegistryError, service.RegistryNotFound)):
            await service.create_model(_body(**changes))
        assert await db.list_models(provider_id="prov-test") == []
        assert await db.get_provider("prov-test") is not None
    _run(test, monkeypatch)


@needs_db
@pytest.mark.parametrize("changes", [{"provider_id": "prov-other"}, {"name": "renamed"},
                                     {"protocol": "openai-chat"}, {"catalog_order": 1}, {"builtin": True}, {}])
def test_update_rejects_identity_and_nonsettings(changes, monkeypatch):
    async def test(pool):
        original = (await service.create_model(_body()))["model"]
        with pytest.raises(service.RegistryError):
            await service.update_model(original["model_id"], changes)
        assert (await service.get_model(original["model_id"]))["model"] == original
    _run(test, monkeypatch)


@needs_db
def test_settings_null_zero_omission_and_price_sources(monkeypatch):
    async def test(pool):
        model = (await service.create_model(_body(name="claude-haiku-4-5", context_window=100000,
            efforts=["low", "high"], background_model="helper", **dict.fromkeys(service.PRICE_FIELDS, 0))))["model"]
        mid = model["model_id"]
        for field in service.PRICE_FIELDS:
            assert model["prices"][field] == {"override": 0, "effective": 0, "source": "override"}
        updated = (await service.update_model(mid, {"context_window": None, "efforts": [],
            "background_model": None, "input_usd_per_mtok": None}))["model"]
        assert updated["context_window"] is None and updated["efforts"] == []
        assert updated["background_model"] is None
        assert updated["prices"]["input_usd_per_mtok"] == {
            "override": None, "effective": 1, "source": "builtin"}
        assert updated["output_usd_per_mtok"] == 0
        retained = (await service.update_model(mid, {"context_window": 42}))["model"]
        assert retained["efforts"] == [] and retained["output_usd_per_mtok"] == 0
        cleared = (await service.update_model(mid, {"efforts": None}))["model"]
        assert cleared["efforts"] is None
        unknown = (await service.create_model(_body()))["model"]
        assert all(price == {"override": None, "effective": None, "source": "unknown"}
                   for price in unknown["prices"].values())
    _run(test, monkeypatch)


@needs_db
@pytest.mark.parametrize("changes", [{"context_window": 0}, {"context_window": True},
    {"input_usd_per_mtok": -1}, {"output_usd_per_mtok": float("nan")},
    {"cache_read_usd_per_mtok": float("inf")}, {"cache_write_usd_per_mtok": 10001},
    {"efforts": ["bad value"]}, {"background_model": "bad name"}])
def test_invalid_settings_rollback(changes, monkeypatch):
    async def test(pool):
        mid = (await service.create_model(_body()))["model"]["model_id"]
        original = await db.get_model(mid)
        with pytest.raises(service.RegistryError):
            await service.update_model(mid, changes)
        assert await db.get_model(mid) == original
    _run(test, monkeypatch)


@needs_db
def test_profile_reference_protection_revalidation_and_history(monkeypatch):
    async def test(pool):
        mid = (await service.create_model(_body()))["model"]["model_id"]
        profile = (await service.create_profile({"name": "Referenced profile", "runtime_id": "rt-claude-code",
                                                "model_id": mid, "effort": "max"}))["profile"]
        profile_id = profile["profile_id"]
        assert (await service.get_model(mid))["model"]["profile_count"] == 1
        with pytest.raises(service.RegistryConflict):
            await service.delete_model(mid)
        # Changing effort metadata alone must preserve existing runtime behavior.
        await service.update_model(mid, {"efforts": []})
        retained = await db.get_profile(profile_id)
        assert retained["model_id"] == mid and retained["effort"] == "max"
        assert service.AgentProfile.from_row(retained).record()["model_id"] == mid
        await pool.execute("INSERT INTO agent_runtimes (id, name, type, executable, capabilities) "
                           "VALUES ('rt-test', 'Test runtime', 'codex', 'codex', '{}'); "
                           "UPDATE agent_profiles SET runtime_id = 'rt-test', agent_teams = false "
                           "WHERE name = 'Referenced profile'")
        with pytest.raises(service.RegistryError, match="Referenced profile"):
            await service.update_model(mid, {"context_window": 123})
        assert (await db.get_model(mid))["context_window"] is None
        await pool.execute("INSERT INTO llm_model_migration_map VALUES "
                           "('old-account', 'test-model', 'anthropic-messages', 'prov-test', $1)", mid)
        record_id = await llm_records.start_record(agent_id="worker-test", metadata={"model_id": mid}, pool=pool)
        await llm_records.finish_record(record_id, status="completed", model="test-model", cost_usd=0.25, pool=pool)
        history = await llm_records.get_record(record_id, pool=pool)
        await pool.execute("DELETE FROM agent_profiles WHERE model_id = $1", mid)
        assert await service.delete_model(mid) == {"deleted": mid}
        assert await db.get_model(mid) is None
        assert await pool.fetchval("SELECT model_id FROM llm_model_migration_map WHERE model_id = $1", mid) == mid
        assert await llm_records.get_record(record_id, pool=pool) == history
        for action in (service.get_model, service.delete_model):
            with pytest.raises(service.RegistryNotFound):
                await action(mid)
        with pytest.raises(service.RegistryNotFound):
            await service.update_model(mid, {"efforts": None})
        builtin = await pool.fetchval("SELECT id FROM llm_models WHERE builtin LIMIT 1")
        for action in (lambda: service.update_model(builtin, {"efforts": None}),
                       lambda: service.delete_model(builtin)):
            with pytest.raises(service.RegistryError):
                await action()
    _run(test, monkeypatch)


@needs_db
def test_runtime_filter_uses_exact_protocol_and_login_owner(monkeypatch):
    async def test(pool):
        messages = (await service.create_model(_body()))["model"]["model_id"]
        chat = (await service.create_model(_body(protocol="openai-chat")))["model"]["model_id"]
        await pool.execute("INSERT INTO agent_runtimes (id, name, type, executable, capabilities) "
                           "VALUES ('rt-test', 'Test Codex', 'codex', 'codex', '{}')")
        models = (await service.list_models(runtime_id="rt-claude-code"))["models"]
        assert messages in {m["model_id"] for m in models}
        assert chat not in {m["model_id"] for m in models}
        assert not any(m["provider_id"] == "prov-codex-login" for m in models)
        assert (await service.list_models(provider_id="prov-test", runtime_id="rt-test"))["models"] == []
        codex = (await service.list_models(runtime_id="rt-test"))["models"]
        assert any(m["provider_id"] == "prov-codex-login" for m in codex)
        with pytest.raises(service.RegistryNotFound):
            await service.list_models(runtime_id="missing")
    _run(test, monkeypatch)


@needs_db
def test_concurrent_creates_updates_and_deletes(monkeypatch):
    async def test(pool):
        results = await asyncio.gather(service.create_model(_body()), service.create_model(_body()),
                                       return_exceptions=True)
        assert sum(isinstance(r, service.RegistryConflict) for r in results) == 1
        mid = next(r["model"]["model_id"] for r in results if isinstance(r, dict))
        await asyncio.gather(service.update_model(mid, {"context_window": 42}),
                             service.update_model(mid, {"input_usd_per_mtok": 0}))
        row = await db.get_model(mid)
        assert row["context_window"] == 42 and row["input_usd_per_mtok"] == 0
        results = await asyncio.gather(service.delete_model(mid), service.delete_model(mid), return_exceptions=True)
        assert sum(isinstance(r, dict) for r in results) == 1
        assert sum(isinstance(r, service.RegistryNotFound) for r in results) == 1
    _run(test, monkeypatch)


@needs_db
def test_creation_waits_for_provider_deletion_and_rechecks(monkeypatch):
    async def test(pool):
        locking = asyncio.Event()
        original = db.lock_provider
        async def lock(conn, provider_id):
            locking.set()
            return await original(conn, provider_id)
        monkeypatch.setattr(db, "lock_provider", lock)
        async with pool.acquire() as conn, conn.transaction():
            await original(conn, "prov-test")
            pending = asyncio.create_task(service.create_model(_body()))
            await asyncio.wait_for(locking.wait(), timeout=2)
            assert not pending.done()
            await conn.execute("DELETE FROM llm_providers WHERE id = 'prov-test'")
        with pytest.raises((service.RegistryError, service.RegistryNotFound)):
            await pending
        assert await db.list_models(provider_id="prov-test") == []
        assert await db.get_provider("prov-other") is not None
    _run(test, monkeypatch)


@needs_db
def test_profile_creation_waits_for_model_delete(monkeypatch):
    async def test(pool):
        mid = (await service.create_model(_body()))["model"]["model_id"]
        locking = asyncio.Event()
        original = db.lock_provider
        async def lock(conn, provider_id):
            locking.set()
            return await original(conn, provider_id)
        monkeypatch.setattr(db, "lock_provider", lock)
        async with pool.acquire() as conn, conn.transaction():
            await db.lock_model(conn, mid)
            locking.clear()
            pending = asyncio.create_task(service.create_profile({"name": "Concurrent profile",
                "runtime_id": "rt-claude-code", "model_id": mid}))
            await asyncio.wait_for(locking.wait(), timeout=2)
            assert not pending.done()
            await db.delete_model(mid, pool=conn)
        with pytest.raises((service.RegistryError, service.RegistryNotFound)):
            await pending
        assert await pool.fetchval("SELECT count(*) FROM agent_profiles WHERE model_id = $1", mid) == 0
    _run(test, monkeypatch)


@needs_db
def test_model_delete_waits_for_profile_creation_and_preserves_reference(monkeypatch):
    async def test(pool):
        mid = (await service.create_model(_body()))["model"]["model_id"]
        locking = asyncio.Event()
        original = db.lock_provider
        async def lock(conn, provider_id):
            locking.set()
            return await original(conn, provider_id)
        monkeypatch.setattr(db, "lock_provider", lock)
        async with pool.acquire() as conn, conn.transaction():
            profile_id = await db.insert_profile({"name": "Concurrent profile", "runtime_id": "rt-claude-code",
                "model_id": mid, "effort": None, "agent_teams": False}, pool=conn)
            locking.clear()
            pending = asyncio.create_task(service.delete_model(mid))
            await asyncio.wait_for(locking.wait(), timeout=2)
            assert not pending.done()
        with pytest.raises(service.RegistryConflict):
            await pending
        assert (await db.get_profile(profile_id))["model_id"] == mid
        assert (await db.get_model(mid))["profile_count"] == 1
    _run(test, monkeypatch)
