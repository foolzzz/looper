"""Current v4 import against an isolated target registry."""
from __future__ import annotations

import json
from copy import deepcopy
from unittest.mock import AsyncMock

import pytest

from looper.agents import config_transfer as transfer
from tests.db_support import DSN
from tests.test_config_transfer_db import _installation

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


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
@pytest.mark.parametrize("runtime_default", [False, True])
async def test_import_roundtrip_retains_ids_and_secret(tmp_path, runtime_default):
    data = v3()
    if runtime_default:
        data["providers"][0]["endpoints"]["anthropic-messages"] = {"base_url": "", "use_runtime_default": True}
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
            assert exported["providers"][0]["endpoints"] == data["providers"][0]["endpoints"]
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


async def test_existing_builtin_model_can_be_referenced_without_importing_its_definition(monkeypatch):
    data = {"format": transfer.FORMAT, "profiles": [{"name": "builtin-reference", "runtime": "claude-code",
            "model": {"provider": "claude-subscription", "name": "opus", "protocol": "anthropic-messages"}}]}
    plan = await preview(monkeypatch, data)
    assert plan.changes[0].action == transfer.Action.ADDED
    assert not plan.need_secret


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


@pytest.mark.parametrize("registered", [False, True])
async def test_custom_runtime_references_require_the_exact_registered_runtime(monkeypatch, registered):
    data = v3()
    data["runtimes"] = [{"name": "custom-claude", "type": "claude-code", "executable": None}]
    data["profiles"][0]["runtime"] = "custom-claude"
    target = transfer._Target()
    if registered:
        target.runtimes["custom-claude"] = {
            "id": "rt-custom", "name": "custom-claude", "type": "claude-code", "executable": "custom-claude",
            "capabilities": ["resident_session", "resume", "run"], "builtin": False}
    plan = await preview(monkeypatch, data, target)
    profile = next(c for c in plan.changes if c.section == "profiles")
    assert profile.action == (transfer.Action.ADDED if registered else transfer.Action.SKIPPED)
    assert next(c for c in plan.changes if c.section == "runtimes").action == (
        transfer.Action.UNCHANGED if registered else transfer.Action.SKIPPED)
    assert not any("built-in claude-code" in note for note in plan.notes)


@pytest.mark.parametrize("section,entry,field", [
    ("providers", {"name": "old", "protocols": ["anthropic-messages"]}, "protocols"),
    ("providers", {"name": "old", "base_url": "https://example.invalid"}, "base_url"),
    ("providers", {"name": "old", "models": []}, "models"),
    ("settings.layers", {"scope": "system", "effort": "high"}, "effort"),
    ("settings.layers", {"scope": "system", "model": "sonnet"}, "model"),
    ("settings.layers", {"scope": "system", "agent": "claude-code/opus"}, "agent"),
    ("settings.layers", {"scope": "role", "role": "planner", "max_concurrency": 2}, "max_concurrency"),
    ("settings.layers", {"scope": "system", "env": [
        {"key": "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS", "value": "1"}]}, "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS"),
])
async def test_current_document_refuses_old_provider_and_layer_fields(monkeypatch, section, entry, field):
    data = {"format": transfer.FORMAT}
    if section == "settings.layers":
        data["settings"] = {"layers": [entry]}
    else:
        data[section] = [entry]
    plan = await preview(monkeypatch, data)
    assert len(plan.changes) == 1
    assert plan.changes[0].action == transfer.Action.SKIPPED
    assert field in plan.changes[0].reason
    assert plan.writes == 0 and not plan.notes


@pytest.mark.parametrize("field", ["app_id_env", "app_secret_env"])
async def test_current_lark_channel_refuses_old_app_keys(monkeypatch, field):
    data = {"format": transfer.FORMAT, "channels": [{"channel_id": "channel-lark", "channel_type": "lark",
            "display_name": "Lark", "enabled": False, "config": {field: "RETIRED_APP"}}]}
    with pytest.raises(transfer.ConfigFileError, match=field):
        await preview(monkeypatch, data)
