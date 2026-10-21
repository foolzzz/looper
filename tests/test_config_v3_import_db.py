"""Shared legacy conversion and v3 import against an isolated target registry."""
from __future__ import annotations

import json
from copy import deepcopy
from unittest.mock import AsyncMock

import pytest

from looper.agents import config_transfer as transfer
from looper.agents import registry_migration as migration
from tests.test_catalog_db import DSN
from tests.test_config_transfer_db import _installation

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


def legacy(version="v2", **changes):
    data = {"format": f"looper-config/{version}", "providers": [{
        "name": "account", "protocols": ["anthropic-messages"], "auth": "api_key",
        "base_url": "https://example.invalid", "models": [{"name": "exact", "input_usd_per_mtok": 2}],
        "env": [], "max_concurrency": None, "api_key": transfer.PLACEHOLDER,
    }], "profiles": [{"name": "imported", "runtime": "claude-code", "provider": "account",
                      "model": "exact", "agent_teams": True, "system_prompt_mode": "old"}],
        "settings": {"layers": [{"scope": "role", "role": "worker", "profile": "imported"}]}, **changes}
    if version == "v1":
        data["agents"] = data.pop("profiles")
        for row in data["settings"]["layers"]:
            row["agent"] = row.pop("profile")
    return data


def v3():
    return {"format": transfer.FORMAT, "providers": [{
        "name": "account", "vendor_type": "custom", "auth": "api_key", "oauth_runtime_type": None,
        "endpoints": {"anthropic-messages": {"base_url": "https://example.invalid"}},
        "env": [], "max_concurrency": None, "api_key": transfer.PLACEHOLDER,
    }], "models": [{"provider": "account", "name": "exact", "protocol": "anthropic-messages",
                    "catalog_order": 3, "efforts": [], "input_usd_per_mtok": 2}],
        "profiles": [{"name": "imported", "runtime": "claude-code", "agent_teams": True,
                      "model": {"provider": "account", "name": "exact", "protocol": "anthropic-messages"}}],
        "settings": {"layers": [{"scope": "role", "role": "worker", "profile": "imported"}]}}


@pytest.mark.parametrize("version", ["v1", "v2"])
async def test_legacy_file_uses_the_database_converter(tmp_path, monkeypatch, version):
    path = tmp_path / "config.json"
    path.write_text(json.dumps(legacy(version)))
    data = transfer.read_file(path)
    real_convert = migration.convert
    calls = []

    def capture(*args, **kwargs):
        result = real_convert(*args, **kwargs)
        calls.append((deepcopy(args), deepcopy(kwargs), result))
        return result

    monkeypatch.setattr(migration, "convert", capture)
    monkeypatch.setattr(transfer._Target, "read", AsyncMock(return_value=transfer._Target()))
    plan = await transfer.import_config(None, data, apply=False)
    assert calls
    args, kwargs, converted = calls[-1]
    assert real_convert(*args, **kwargs) == converted
    assert len(converted.models) == len(converted.profiles) == 1
    assert any(c.section == "models" and c.action == transfer.Action.ADDED for c in plan.changes)
    assert any(c.section == "profiles" and c.action == transfer.Action.ADDED for c in plan.changes)
    assert plan.need_secret[0].names == ("api_key",)
    assert any("system_prompt_mode" in n for n in plan.notes)


async def preview(monkeypatch, data, target=None):
    monkeypatch.setattr(transfer._Target, "read", AsyncMock(return_value=target or transfer._Target()))
    return await transfer.import_config(None, data, apply=False)


async def test_v3_missing_model_skips_profile_and_dependents(monkeypatch):
    data = v3()
    data["models"] = []
    plan = await preview(monkeypatch, data)
    for section in ("profiles", "settings.layers"):
        change = next(c for c in plan.changes if c.section == section)
        assert change.action == transfer.Action.SKIPPED and change.reason
    assert not any(c.section == "profiles" and c.action == transfer.Action.ADDED for c in plan.changes)


async def test_v3_profile_mode_is_rejected(monkeypatch):
    data = v3()
    data["profiles"][0]["system_prompt_mode"] = "append"
    plan = await preview(monkeypatch, data)
    assert any(c.section == "profiles" and c.action == transfer.Action.SKIPPED
               and "system_prompt_mode" in c.reason for c in plan.changes)
    assert not plan.notes


@pytest.mark.parametrize("section", ["providers", "models", "profiles"])
async def test_duplicate_natural_keys_are_skipped(monkeypatch, section):
    data = v3()
    data[section].append(deepcopy(data[section][0]))
    plan = await preview(monkeypatch, data)
    assert any(c.section == section and c.action == transfer.Action.SKIPPED
               and "duplicate" in c.reason for c in plan.changes)
    assert not any(c.section == section and c.action == transfer.Action.ADDED for c in plan.changes)


@pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")
@pytest.mark.parametrize("version", ["v1", "v2", "v3"])
async def test_import_roundtrip_retains_ids_and_secret(tmp_path, version):
    data = v3() if version == "v3" else legacy(version)
    path = tmp_path / "config.json"
    path.write_text(json.dumps(data))
    data = transfer.read_file(path)
    async with _installation() as (pool, _):
        async with pool.acquire() as conn:
            before = await transfer.run_import(conn, data, apply=False)
            applied = await transfer.run_import(conn, data, apply=True)
            assert before.changes == applied.changes
            assert before.need_secret and applied.need_secret
            first = [dict(r) for r in await conn.fetch("SELECT id, model_id FROM agent_profiles ORDER BY id")]
            await conn.execute("INSERT INTO llm_provider_secrets SELECT id, 'synthetic-saved-key' "
                               "FROM llm_providers WHERE name = 'account'")
            again = await transfer.run_import(conn, data, apply=True)
            assert again.writes == 0 and not again.need_secret
            assert first == [dict(r) for r in await conn.fetch("SELECT id, model_id FROM agent_profiles ORDER BY id")]
            exported = json.loads("".join([chunk async for chunk in transfer.export_chunks(
                conn, include_projects=False, include_secrets=False)]))
            assert exported["providers"][0]["api_key"] == transfer.PLACEHOLDER
            assert (await transfer.run_import(conn, exported, apply=True)).writes == 0
            assert await conn.fetchval("SELECT api_key FROM llm_provider_secrets s JOIN llm_providers p "
                                       "ON p.id = s.provider_id WHERE p.name = 'account'") == "synthetic-saved-key"


def target_registry():
    from looper.agents.profile_registry import MODEL_FIELDS

    data = v3()
    provider = {**data["providers"][0], "id": "prov-existing", "builtin": False, "api_key": "synthetic-stored-key",
                "endpoints": [{"protocol": "anthropic-messages", "base_url": "https://example.invalid",
                               "use_runtime_default": False}]}
    model = {**{k: None for k in MODEL_FIELDS}, **data["models"][0], "id": "model-existing",
             "provider_id": provider["id"], "builtin": False}
    profile = {"id": "agent-existing", "name": "unrelated", "runtime": "claude-code", "provider": "account",
               "model": "exact", "protocol": "anthropic-messages", "effort": None, "agent_teams": True,
               "custom_prompt": "keep this", "builtin": False}
    return transfer._Target(providers={"account": provider},
                            models={("account", "exact", "anthropic-messages"): model},
                            profiles={"unrelated": profile})


async def test_omitted_key_and_unrelated_profiles_are_preserved(monkeypatch):
    target = target_registry()
    original = deepcopy(target)
    plan = await preview(monkeypatch, v3(), target)
    assert not plan.need_secret
    assert target == original
    assert next(c for c in plan.changes if c.section == "providers").action == transfer.Action.UNCHANGED
    assert next(c for c in plan.changes if c.section == "models").action == transfer.Action.UNCHANGED
    assert not any(c.key == "unrelated" for c in plan.changes)
    assert "synthetic-stored-key" not in json.dumps(plan.view(targets=True))


async def test_endpoint_conflict_revalidates_profiles_absent_from_file(monkeypatch):
    target = target_registry()
    data = v3()
    data["providers"][0]["endpoints"]["anthropic-messages"]["base_url"] = "https://changed.invalid"
    plan = await preview(monkeypatch, data, target)
    provider = next(c for c in plan.changes if c.section == "providers")
    assert provider.action == transfer.Action.SKIPPED and "unrelated" in provider.reason
    assert all(c.action == transfer.Action.SKIPPED for c in plan.changes)


@pytest.mark.parametrize("section", ["providers", "models", "profiles"])
async def test_builtin_definitions_cannot_be_overwritten(monkeypatch, section):
    target = target_registry()
    if section == "providers":
        target.providers["account"]["builtin"] = True
    elif section == "models":
        target.models[("account", "exact", "anthropic-messages")]["builtin"] = True
    else:
        target.profiles["imported"] = {**target.profiles["unrelated"], "name": "imported", "builtin": True}
    plan = await preview(monkeypatch, v3(), target)
    assert any(c.section == section and c.action == transfer.Action.SKIPPED and "built-in" in c.reason
               for c in plan.changes)


async def test_bad_legacy_model_is_skipped_without_default_fallback(monkeypatch):
    data = legacy()
    data["profiles"][0]["model"] = "outside-catalog"
    plan = await preview(monkeypatch, data)
    assert any(c.section == "profiles" and c.action == transfer.Action.SKIPPED
               and c.code == "model_outside_catalog" for c in plan.changes)
    assert any(c.section == "settings.layers" and c.action == transfer.Action.SKIPPED for c in plan.changes)


async def test_legacy_conversion_never_receives_unrelated_target_accounts(monkeypatch):
    target = target_registry()
    target.providers["unrelated-account"] = {**target.providers["account"], "id": "prov-unrelated",
                                            "name": "unrelated-account"}
    convert = migration.convert
    seen = []

    def capture(runtimes, providers, profiles, **kwargs):
        seen.extend(p["id"] for p in providers)
        return convert(runtimes, providers, profiles, **kwargs)

    monkeypatch.setattr(migration, "convert", capture)
    await preview(monkeypatch, legacy(), target)
    assert "prov-existing" in seen and "prov-unrelated" not in seen


async def test_apply_recomputes_and_keeps_existing_ids(monkeypatch):
    from looper.db import config_transfer as db
    from looper.db import connection

    target = target_registry()
    data = v3()
    data["profiles"][0]["name"] = "unrelated"
    data["profiles"][0]["custom_prompt"] = "changed"
    monkeypatch.setattr(transfer._Target, "read", AsyncMock(return_value=target))
    monkeypatch.setattr(connection, "schema_change_pending", AsyncMock(return_value=False))
    monkeypatch.setattr(connection, "ensure_schema", AsyncMock())
    write = AsyncMock(return_value="agent-existing")
    monkeypatch.setattr(db, "write_profile", write)
    first = await transfer.import_config(None, data, apply=False)
    assert not write.called
    # A target change between preview and apply must be observed and must not
    # erase the now-saved key just because preview reported it missing.
    target.providers["account"]["api_key"] = "new-synthetic-key"
    applied = await transfer.import_config(AsyncMock(), data, apply=True)
    assert first.changes == applied.changes
    assert write.await_args.args[1] == "agent-existing"
    assert write.await_args.args[2]["model_id"] == "model-existing"
    assert "provider_id" not in write.await_args.args[2]


@pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")
async def test_apply_failure_rolls_back_the_whole_import(monkeypatch):
    from looper.db import config_transfer as db

    async with _installation() as (pool, _):
        async with pool.acquire() as conn:
            async def fail(*args, **kwargs):
                raise RuntimeError("injected profile write failure")

            monkeypatch.setattr(db, "write_profile", fail)
            with pytest.raises(RuntimeError, match="injected"):
                await transfer.run_import(conn, v3(), apply=True)
            assert await conn.fetchval("SELECT count(*) FROM llm_providers WHERE name = 'account'") == 0
            assert await conn.fetchval("SELECT count(*) FROM llm_models WHERE name = 'exact'") == 0
            assert await conn.fetchval("SELECT count(*) FROM agent_profiles WHERE name = 'claude-code/opus'") == 1


async def test_legacy_runtime_alias_uses_existing_runtime_once(monkeypatch):
    data = legacy()
    data["runtimes"] = [{"name": "old-claude", "type": "claude-code", "executable": None}]
    data["profiles"][0]["runtime"] = "old-claude"
    plan = await preview(monkeypatch, data)
    assert any(c.section == "profiles" and c.action == transfer.Action.ADDED for c in plan.changes)


async def test_existing_builtin_model_can_be_referenced_without_importing_its_definition(monkeypatch):
    data = {"format": transfer.FORMAT, "profiles": [{"name": "builtin-reference", "runtime": "claude-code",
            "model": {"provider": "claude-subscription", "name": "opus", "protocol": "anthropic-messages"}}]}
    plan = await preview(monkeypatch, data)
    assert plan.changes[0].action == transfer.Action.ADDED
    assert not plan.need_secret


async def test_legacy_builtin_provider_is_not_recreated(monkeypatch):
    data = legacy(providers=[])
    data["profiles"][0].update(provider="claude-subscription", model="opus")
    plan = await preview(monkeypatch, data)
    assert any(c.section == "profiles" and c.action == transfer.Action.ADDED for c in plan.changes)
    assert not any(c.section == "providers" for c in plan.changes)


async def test_legacy_default_endpoint_marker_survives_import(monkeypatch):
    data = legacy()
    data["providers"][0]["base_url"] = ""
    importer = transfer._Importer(None, transfer._Target(), False)
    converted = importer.legacy(data)
    endpoint = converted["providers"][0]["endpoints"]["anthropic-messages"]
    assert endpoint == {"base_url": "", "use_runtime_default": True}
    await importer.provider(converted["providers"][0])
    assert importer.endpoints[("account", "anthropic-messages")].use_runtime_default


@pytest.mark.parametrize("version", ["v1", "v2"])
@pytest.mark.parametrize("key,provider_count", [(transfer.PLACEHOLDER, 2), ("synthetic-identical-key", 1)])
async def test_legacy_mapping_matches_normalized_database_rows(version, key, provider_count):
    from looper.agents.profile_registry import BUILTIN_RUNTIME

    data = legacy(version)
    data["providers"][0].update(name="a", base_url="https://api.deepseek.com/anthropic", api_key=key)
    data["providers"].append({**deepcopy(data["providers"][0]), "name": "b"})
    profiles = data["agents" if version == "v1" else "profiles"]
    profiles[0]["provider"] = "b"
    normalized = [{**p, "id": f"file:provider:{p['name']}"} for p in data["providers"]]
    expected = migration.convert([BUILTIN_RUNTIME], normalized, [{
        "id": "file:profile:imported", "name": "imported", "runtime_id": BUILTIN_RUNTIME.id,
        "provider_id": "file:provider:b", "model": "exact", "agent_teams": True}])
    assert len(expected.providers) == provider_count
    importer = transfer._Importer(None, transfer._Target(), False)
    converted = importer.legacy(transfer._from_v1(data) if version == "v1" else data)
    assert {p["name"] for p in converted["providers"]} == {p.name for p in expected.providers.values()}
    model = expected.models[expected.profiles["file:profile:imported"].model_id]
    assert converted["profiles"][0]["model"] == {
        "provider": expected.providers[model.provider_id].name, "name": model.name, "protocol": model.protocol.value}


async def test_skipped_existing_profile_does_not_supply_a_layer_fallback(monkeypatch):
    target = target_registry()
    target.profiles["imported"] = {**target.profiles.pop("unrelated"), "name": "imported"}
    data = v3()
    data["profiles"][0]["model"]["name"] = "missing"
    plan = await preview(monkeypatch, data, target)
    assert next(c for c in plan.changes if c.section == "profiles").action == transfer.Action.SKIPPED
    assert next(c for c in plan.changes if c.section == "settings.layers").action == transfer.Action.SKIPPED


async def test_endpoints_absent_from_file_are_retained(monkeypatch):
    target = target_registry()
    target.providers["account"]["endpoints"].append({"protocol": "openai-chat", "base_url": "https://other.invalid",
                                                    "use_runtime_default": False})
    plan = await preview(monkeypatch, v3(), target)
    assert next(c for c in plan.changes if c.section == "providers").action == transfer.Action.UNCHANGED


async def test_model_protocol_conflict_has_no_cross_protocol_fallback(monkeypatch):
    data = v3()
    data["models"][0]["protocol"] = "openai-chat"
    data["profiles"][0]["model"]["protocol"] = "openai-chat"
    plan = await preview(monkeypatch, data, target_registry())
    change = next(c for c in plan.changes if c.section == "models")
    assert change.action == transfer.Action.SKIPPED and "endpoint" in change.reason
    assert next(c for c in plan.changes if c.section == "profiles").action == transfer.Action.SKIPPED
