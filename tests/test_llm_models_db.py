"""Model CRUD contracts on disposable PostgreSQL, directly through the registry."""
from __future__ import annotations

import asyncio
import uuid
from contextlib import asynccontextmanager
from dataclasses import asdict
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from looper.agents import profile_registry as service
from looper.db import connection, llm_records, profile_registry as db
from tests.db_support import DSN, run_with_models as _run
from tests.test_llm_provider_api_db import _run as _run_http, _account, SECRET

needs_db = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")


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


@pytest.mark.parametrize("protocol,input_price", [("anthropic-messages", 0), ("openai-chat", 2.5)])
def test_allowed_profile_view_resolves_model_reference_and_prices(protocol, input_price):
    from looper.agents.projects import allowed_profile_view

    model = _row(name="shared-model", protocol=protocol, input_usd_per_mtok=input_price,
                 output_usd_per_mtok=3)
    row = {"id": "agent-test", "name": "Allowed profile", "runtime_id": "rt-test",
           "runtime_name": "Test runtime", "runtime_type": "omp", "runtime_capabilities": ["run"],
           "model_id": model["id"], "model": model, "provider": model["provider"],
           "endpoint": model["endpoint"], "effort": "high", "agent_teams": False, "custom_prompt": ""}

    assert allowed_profile_view(row) == {
        "profile_id": "agent-test", "name": "Allowed profile", "runtime": "omp", "model": "shared-model",
        "effort": "high", "roles": ["worker", "reviewer"],
        "usd_per_million_tokens": {"input": input_price, "output": 3}}


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
    ("omp", {"anthropic-messages", "openai-chat", "openai-responses"}), ("grok", set()),
    ("pi", {"openai-chat", "openai-responses"}), ("dsh", {"anthropic-messages"})])
def test_service_runtime_filter_obeys_adapter_pairing(runtime_type, protocols, monkeypatch):
    rows = [_row(name="deepseek-flash", protocol=protocol.value) for protocol in service.ApiProtocol]
    monkeypatch.setattr(db, "list_models", AsyncMock(return_value=rows))
    monkeypatch.setattr(db, "get_runtime", AsyncMock(return_value={
        "id": "rt-test", "name": "Test runtime", "type": runtime_type, "executable": "", "capabilities": []}))
    async def run():
        result = await service.list_models(runtime_id="rt-test")
        assert {m["protocol"] for m in result["models"]} == protocols
    asyncio.run(asyncio.wait_for(run(), timeout=2))


@pytest.mark.parametrize("runtime_type,levels", [
    ("pi", ["off", "minimal", "low", "medium", "high", "xhigh", "max"]),
    ("dsh", ["low", "high", "max"]),
])
def test_runtime_type_offers_only_its_effort_levels(runtime_type, levels):
    assert service.type_view(service.runtimes.ADAPTERS[runtime_type])["effort_levels"] == levels


@pytest.mark.parametrize("runtime_type,protocol,model,effort,error", [
    ("pi", "openai-responses", "gpt-6.1-sol", "extreme", "pi effort"),
    ("dsh", "anthropic-messages", "deepseek-flash", "medium", "DSH effort"),
    ("dsh", "anthropic-messages", "gpt-6.1-sol", "high", "DSH supports only"),
    ("pi", "anthropic-messages", "deepseek-flash", None, "exact protocol"),
    ("dsh", "openai-chat", "deepseek-flash", None, "exact protocol"),
])
def test_config_import_rejects_incompatible_runtime_model_and_effort(
    monkeypatch, runtime_type, protocol, model, effort, error,
):
    from looper.agents import config_transfer as transfer

    target = transfer._Target(runtimes={runtime_type: {
        "id": f"rt-{runtime_type}", "name": runtime_type, "type": runtime_type,
        "executable": "", "capabilities": [], "builtin": True}})
    monkeypatch.setattr(transfer._Target, "read", AsyncMock(return_value=target))
    data = {"format": transfer.FORMAT, "providers": [{
        "name": "account", "vendor_type": "custom", "auth": "api_key",
        "endpoints": {protocol: {"base_url": "https://example.invalid"}},
    }], "models": [{"provider": "account", "name": model, "protocol": protocol}],
        "profiles": [{"name": "imported", "runtime": runtime_type,
                      "model": {"provider": "account", "name": model, "protocol": protocol},
                      "effort": effort}]}

    async def run():
        plan = await transfer.import_config(None, data, apply=False)
        change = next(c for c in plan.changes if c.section == "profiles")
        assert change.action == transfer.Action.SKIPPED and error in change.reason
    asyncio.run(asyncio.wait_for(run(), timeout=2))


@needs_db
def test_existing_database_upgrades_profile_efforts_for_pi(monkeypatch):
    async def test(pool):
        # Recreate the pre-pi constraint and migration state on this disposable schema.
        await pool.execute("ALTER TABLE agent_profiles DROP CONSTRAINT agent_profiles_effort_check; "
                           "ALTER TABLE agent_profiles ADD CONSTRAINT agent_profiles_effort_check "
                           "CHECK (effort IN ('low', 'medium', 'high', 'xhigh', 'max')); "
                           "DELETE FROM schema_migrations WHERE name = 'profile_runtime_effort_levels'")
        await connection.ensure_schema(pool)
        await connection.ensure_schema(pool)
        await pool.execute("INSERT INTO agent_runtimes (id, name, type, executable, capabilities) "
                           "VALUES ('rt-pi', 'pi', 'pi', 'pi', '{}')")
        mid = (await service.create_model(_body(protocol="openai-chat")))["model"]["model_id"]
        for effort in ("off", "minimal"):
            profile = (await service.create_profile({"name": effort, "runtime_id": "rt-pi",
                                                     "model_id": mid, "effort": effort}))["profile"]
            assert profile["effort"] == effort
        with pytest.raises(service.RegistryError, match="effort must"):
            await service.create_profile({"name": "wrong-runtime", "runtime_id": "rt-claude-code",
                                          "model_id": (await service.create_model(_body()))["model"]["model_id"],
                                          "effort": "off"})
    _run(test, monkeypatch)


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


# Exercise the authenticated production HTTP parser and registry together.


@needs_db
def test_http_provider_model_profile_flow(monkeypatch, tmp_path):
    async def test(client, pool):
        account = (await client.ok("POST", "/api/providers", _account(api_key=SECRET, endpoints=[
            {"protocol": protocol, "base_url": f"https://{protocol}.example.invalid"}
            for protocol in ("anthropic-messages", "openai-chat")]), status=201))["provider"]
        pid = account["provider_id"]
        body = _body(provider_id=pid, name="claude-haiku-4-5", context_window=100000,
                     efforts=["high"], input_usd_per_mtok=0, output_usd_per_mtok=0)
        model = (await client.ok("POST", "/api/models", body, status=201))["model"]
        mid = model["model_id"]
        assert mid.startswith("model-") and model["provider_id"] == pid
        chat = (await client.ok("POST", "/api/models", {**body, "protocol": "openai-chat"}, status=201))["model"]
        assert mid != chat["model_id"]
        listed = (await client.ok("GET", f"/api/models?provider_id={pid}&runtime_id=rt-claude-code"))["models"]
        assert [m["model_id"] for m in listed] == [mid]
        assert len((await client.ok("GET", f"/api/models?provider_id={pid}"))["models"]) == 2
        assert (await client.ok("GET", f"/api/models/{mid}"))["model"] == model
        changed = (await client.ok("PUT", f"/api/models/{mid}", {
            "context_window": None, "efforts": [], "input_usd_per_mtok": None}))["model"]
        assert changed["context_window"] is None and changed["efforts"] == []
        assert changed["output_usd_per_mtok"] == 0
        assert changed["prices"]["input_usd_per_mtok"] == {"override": None, "effective": 1, "source": "builtin"}
        changed = (await client.ok("PUT", f"/api/models/{mid}", {"efforts": None}))["model"]
        assert changed["efforts"] is None and changed["output_usd_per_mtok"] == 0
        profile = (await client.ok("POST", "/api/agent-profiles", {
            "name": "HTTP profile", "runtime_id": "rt-claude-code", "model_id": mid,
            "effort": "high", "agent_teams": False, "custom_prompt": "HTTP instructions"}, status=201))["profile"]
        aid = profile["profile_id"]
        assert profile["model"] == {"model_id": mid, "name": body["name"],
                                    "protocol": body["protocol"], "provider_id": pid}
        assert profile["provider"]["provider_id"] == pid and profile["provider"]["vendor_type"] == "custom"
        assert profile["roles"] and profile["used_by"] == dict(service.NOT_USED)
        assert profile["cost"]["source"] == "provider_price"
        assert not {"mode", "system_prompt_mode", "role_system_prompt_mode", "system_prompt_modes"} & profile.keys()
        registry = await client.ok("POST", "/api/v1/registry/view", {})
        assert next(p for p in registry["profiles"] if p["profile_id"] == aid) == profile
        assert next(m for m in registry["models"] if m["model_id"] == mid)["profile_count"] == 1
        for path in (f"/api/models/{mid}", f"/api/providers/{pid}"):
            status, error = await client.call("DELETE", path)
            assert status == 409 and set(error) == {"error"}
        profile = (await client.ok("PUT", f"/api/agent-profiles/{aid}", {"effort": None}))["profile"]
        assert profile["effort"] is None and profile["custom_prompt"] == "HTTP instructions"
        await client.ok("DELETE", f"/api/agent-profiles/{aid}")
        for model_id in (mid, chat["model_id"]):
            assert await client.ok("DELETE", f"/api/models/{model_id}") == {"deleted": model_id}
        await client.ok("DELETE", f"/api/providers/{pid}")
        assert await db.get_provider(pid) is None and await db.get_model(mid) is None
    _run_http(test, monkeypatch, tmp_path)


@needs_db
def test_http_model_errors_and_concurrent_deletion(monkeypatch, tmp_path):
    async def test(client, pool):
        pid = (await client.ok("POST", "/api/providers", _account(), status=201))["provider"]["provider_id"]
        body = _body(provider_id=pid)
        mid = (await client.ok("POST", "/api/models", body, status=201))["model"]["model_id"]
        cases = [
            ("POST", "/api/models", body, 409),
            ("POST", "/api/models", {**body, "protocol": "openai-chat"}, 400),
            ("POST", "/api/models", {**body, "provider_id": "prov-missing"}, 404),
            ("PUT", f"/api/models/{mid}", {"name": "renamed"}, 400),
            ("PUT", f"/api/models/{mid}", {"context_window": True}, 400),
            ("GET", "/api/models?runtime_id=rt-missing", None, 404),
        ]
        for method in ("GET", "PUT", "DELETE"):
            cases.append((method, "/api/models/model-missing", {"efforts": None} if method == "PUT" else None, 404))
        builtin = await pool.fetchval("SELECT id FROM llm_models WHERE builtin LIMIT 1")
        cases += [("PUT", f"/api/models/{builtin}", {"efforts": []}, 400),
                  ("DELETE", f"/api/models/{builtin}", None, 400)]
        for method, path, value, expected in cases:
            status, error = await client.call(method, path, value)
            assert status == expected and set(error) == {"error"}, (method, path, error)
        results = await asyncio.gather(client.call("DELETE", f"/api/models/{mid}"),
                                       client.call("DELETE", f"/api/models/{mid}"))
        assert sorted(status for status, _ in results) == [200, 404]
        assert await db.get_model(mid) is None
    _run_http(test, monkeypatch, tmp_path)


@pytest.mark.parametrize("field", ["provider_id", "model", "system_prompt_mode", "mode", "protocol", "efforts",
                                 "role_system_prompt_mode", "system_prompt_modes"])
@pytest.mark.parametrize("method,path", [("POST", "/api/agent-profiles"),
                                         ("PUT", "/api/agent-profiles/agent-test")])
def test_http_profile_rejects_old_body_and_mode_fields(monkeypatch, tmp_path, field, method, path):
    async def test(client, pool):
        status, error = await client.call(method, path, {
            "name": "Rejected", "runtime_id": "rt-claude-code", "model_id": "model-test", field: "legacy"})
        assert status == 400 and set(error) == {"error"} and field in error["error"]
    _run_http(test, monkeypatch, tmp_path, database=False)


@pytest.mark.parametrize("method,path", [("GET", "/api/models"), ("POST", "/api/models"),
    ("GET", "/api/models/model-test"), ("PUT", "/api/models/model-test"), ("DELETE", "/api/models/model-test")])
def test_http_model_routes_require_authentication(monkeypatch, tmp_path, method, path):
    async def test(client, pool):
        status, error = await client.call(method, path, _body(), token=None)
        assert status == 401 and set(error) == {"error"}
    _run_http(test, monkeypatch, tmp_path, database=False)


@pytest.mark.parametrize("method,path,body", [
    ("POST", "/api/models", {"provider_id": None, "name": "m", "protocol": "anthropic-messages"}),
    ("POST", "/api/models", {"provider_id": "prov-test", "name": "m", "protocol": ["openai-chat"]}),
    ("PUT", "/api/models/model-test", {"name": "immutable"}),
    ("PUT", "/api/models/model-test", {}),
])
def test_http_model_validation_uses_shared_registry(monkeypatch, tmp_path, method, path, body):
    async def test(client, pool):
        status, error = await client.call(method, path, body)
        assert status == 400 and set(error) == {"error"}
    _run_http(test, monkeypatch, tmp_path, database=False)


@pytest.mark.parametrize("resource", ["models", "agent-profiles"])
@pytest.mark.parametrize("raw", [b"{", b"[]", b"null", b'"text"', b"\xff"])
def test_http_registry_rejects_malformed_or_nonobject_bodies(monkeypatch, tmp_path, resource, raw):
    async def test(client, pool):
        status, answer = await client.call("POST", f"/api/{resource}", raw=raw)
        assert status == 400 and set(answer) == {"error"}
    _run_http(test, monkeypatch, tmp_path, database=False)


@pytest.mark.parametrize("model_id", ["prov-test", "agent-test", "model-test/extra", "model_test"])
@pytest.mark.parametrize("method", ["GET", "PUT", "DELETE"])
def test_http_model_routes_accept_only_model_ids(monkeypatch, tmp_path, method, model_id):
    async def test(client, pool):
        status, answer = await client.call(method, f"/api/models/{model_id}", {"efforts": None})
        assert status == 404 and set(answer) == {"error"}
    _run_http(test, monkeypatch, tmp_path, database=False)


@pytest.mark.parametrize("changes", [
    {"context_window": 0}, {"context_window": True}, {"input_usd_per_mtok": -1},
    {"output_usd_per_mtok": float("nan")}, {"cache_read_usd_per_mtok": float("inf")},
    {"cache_write_usd_per_mtok": 10001}, {"efforts": ["bad value"]}, {"background_model": "bad name"},
])
def test_http_model_creation_rejects_invalid_settings(monkeypatch, tmp_path, changes):
    async def test(client, pool):
        status, answer = await client.call("POST", "/api/models", _body(**changes))
        assert status == 400 and set(answer) == {"error"}
    _run_http(test, monkeypatch, tmp_path, database=False)


@pytest.mark.parametrize("runtime_type,protocol,auth,owner,error", [
    ("claude-code", "openai-chat", "api_key", None, "exact protocol"),
    ("omp", "anthropic-messages", "oauth", "claude-code", "only use its own runtime"),
    ("grok", "openai-responses", "api_key", None, "auth kind"),
    ("codex", "openai-responses", "oauth", "grok", "only use its own runtime"),
])
def test_http_profile_rejects_incompatible_model_before_inserting(
    monkeypatch, tmp_path, runtime_type, protocol, auth, owner, error,
):
    model = _row(protocol=protocol)
    if owner is not None:
        vendor = {"claude-code": "claude-subscription", "grok": "grok-login"}[owner]
        model["provider"].update(auth=auth, oauth_runtime_type=owner, vendor_type=vendor)
        model["endpoint"]["base_url"] = ""
    monkeypatch.setattr(db, "get_runtime", AsyncMock(return_value={
        "id": "rt-test", "name": "Test runtime", "type": runtime_type, "executable": "", "capabilities": []}))
    monkeypatch.setattr(db, "lock_model", AsyncMock(return_value=model))
    insert = AsyncMock()
    monkeypatch.setattr(db, "insert_profile", insert)

    @asynccontextmanager
    async def transaction():
        yield object()

    monkeypatch.setattr(connection, "transaction", transaction)
    async def test(client, pool):
        status, answer = await client.call("POST", "/api/agent-profiles", {
            "name": "Incompatible", "runtime_id": "rt-test", "model_id": "model-test"})
        assert status == 400 and error in answer["error"], answer
        insert.assert_not_awaited()
    _run_http(test, monkeypatch, tmp_path, database=False)


@needs_db
def test_http_profile_preview_uses_saved_binding_role_project_and_mode_source(monkeypatch, tmp_path):
    async def test(client, pool):
        from looper.agents.system_prompt import build_system_prompt
        from looper.agents import prompt_preview

        pid = (await client.ok("POST", "/api/providers", _account(), status=201))["provider"]["provider_id"]
        mid = (await client.ok("POST", "/api/models", _body(provider_id=pid), status=201))["model"]["model_id"]
        aid = (await client.ok("POST", "/api/agent-profiles", {
            "name": "Preview profile", "runtime_id": "rt-claude-code", "model_id": mid,
            "custom_prompt": "Preview identity"}, status=201))["profile"]["profile_id"]
        project = (await client.ok("POST", "/api/projects", {"name": "Preview project"}, status=201))["project_id"]
        await client.ok("POST", "/api/v1/agent_settings/update_role", {"role": "worker", "values": {"system_prompt_mode": "replace"}})
        builds = []
        def capture(**kwargs):
            result = build_system_prompt(**kwargs)
            builds.append(result)
            return result
        monkeypatch.setattr(prompt_preview, "build_system_prompt", capture)
        preview = await client.ok("GET", f"/api/agent-profiles/{aid}/prompt-preview?role=worker&project_id={project}")
        assert preview["role"] == "worker" and preview["prompt_layers"]["project"]["id"] == project
        assert preview["system_prompt_mode"] == "replace" and preview["system_prompt_mode_source"] == "role"
        assert preview["looper_prompt_sha256"] == builds[-1].sha256
        assert any("Preview identity" in (b["text"] or "") for b in preview["blocks"])
        assert preview["prompt_layers"]["contract"]["name"] == "WORK_CONTRACT"
        manager = await client.ok("GET", f"/api/agent-profiles/{aid}/prompt-preview?role=manager&project_id={project}")
        assert manager["prompt_layers"]["project"] is None
        assert manager["system_prompt_mode_source"] == "builtin"
        for path, expected in ((f"/api/agent-profiles/{aid}/prompt-preview", 400),
                               (f"/api/agent-profiles/agent-missing/prompt-preview?role=worker", 404)):
            status, error = await client.call("GET", path)
            assert status == expected and set(error) == {"error"}
    _run_http(test, monkeypatch, tmp_path)


def test_http_model_filters_and_readback_use_registry_views(monkeypatch, tmp_path):
    row = _row(name="claude-haiku-4-5", input_usd_per_mtok=0)
    other_protocol = _row(protocol="openai-chat")
    models = AsyncMock(return_value=[row, other_protocol])
    monkeypatch.setattr(db, "list_models", models)
    monkeypatch.setattr(db, "get_model", AsyncMock(return_value=row))
    monkeypatch.setattr(db, "get_runtime", AsyncMock(return_value={
        "id": "rt-test", "name": "Test runtime", "type": "claude-code", "executable": "", "capabilities": []}))
    async def test(client, pool):
        listed = await client.ok("GET", "/api/models?provider_id=prov-test&runtime_id=rt-test")
        assert [m["protocol"] for m in listed["models"]] == ["anthropic-messages"]
        assert models.await_args.kwargs == {"provider_id": "prov-test"}
        saved = (await client.ok("GET", "/api/models/model-test"))["model"]
        assert saved == listed["models"][0]
        assert saved["prices"]["input_usd_per_mtok"] == {"override": 0, "effective": 0, "source": "override"}
        assert "api_key" not in repr(saved)
    _run_http(test, monkeypatch, tmp_path, database=False)


@pytest.mark.parametrize("role", ["worker", "reviewer", "planner", "manager"])
@pytest.mark.parametrize("configured", [False, True])
@pytest.mark.parametrize("runtime_type", ["claude-code", "codex"])
def test_http_normalized_profile_preview_uses_launch_builder(monkeypatch, tmp_path, role, configured, runtime_type):
    from looper.agents import prompt_preview
    from looper.agents.system_prompt import build_system_prompt
    from looper.db import agent_setting_layers

    runtime = service.runtimes.ADAPTERS[runtime_type]
    protocol = "openai-responses" if runtime_type == "codex" else "anthropic-messages"
    model = _row(protocol=protocol)
    row = {"id": "agent-preview", "name": "Preview", "runtime_id": f"rt-{runtime_type}",
           "runtime_name": runtime_type, "runtime_type": runtime_type,
           "runtime_capabilities": [c.value for c in runtime.capabilities],
           "model_id": model["id"], "model": model, "provider": model["provider"],
           "endpoint": model["endpoint"], "custom_prompt": "Preview identity", "agent_teams": False}
    monkeypatch.setattr(db, "get_profile", AsyncMock(return_value=row))
    monkeypatch.setattr(prompt_preview.db_roles, "get_config", AsyncMock(return_value=None))
    layers = [{"scope": "role", "role": role, "system_prompt_mode": "replace"}] if configured else []
    loader = AsyncMock(return_value=layers)
    monkeypatch.setattr(agent_setting_layers, "list_layers", loader)
    builds = []
    def capture(**kwargs):
        result = build_system_prompt(**kwargs)
        builds.append(result)
        return result
    monkeypatch.setattr(prompt_preview, "build_system_prompt", capture)
    async def test(client, pool):
        answer = await client.ok("GET", f"/api/agent-profiles/agent-preview/prompt-preview?role={role}&system_prompt_mode=append")
        assert answer["role"] == role and answer["runtime"] == runtime_type
        expected_mode = "replace" if configured or role in ("planner", "manager") else "append"
        assert answer["system_prompt_mode"] == (expected_mode if runtime.can_replace_system_prompt else "append")
        role_source = "role" if configured else "builtin"
        assert answer["system_prompt_mode_source"] == (role_source if runtime.can_replace_system_prompt else "runtime")
        assert answer["role_system_prompt_mode_source"] == role_source
        assert answer["role_system_prompt_mode"] == expected_mode
        assert answer["looper_prompt_sha256"] == builds[-1].sha256
        assert answer["prompt_layers"] == builds[-1].layers
        assert any("Preview identity" in (block["text"] or "") for block in answer["blocks"])
        assert loader.await_count == 1
    _run_http(test, monkeypatch, tmp_path, database=False)
