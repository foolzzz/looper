"""Operator settings and command identity are stored independently of agent settings."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from looper.agents import acp_reviewer_settings as settings, api_v1, profile_registry as registry
from looper.agents.ops.clients import ClientOps
from looper.agents.runtimes import AuthKind, VendorType
from tests.db_support import run_db
from tests.test_api_v1_auth import CLIENT_TOKEN, _bearer, _send, _serving, clients  # noqa: F401


def test_settings_default_off_update_and_preserved_registration(monkeypatch):
    async def check(pool):
        assert await settings.read() == settings.AcpReviewerSettings()
        before = await registry.registry_view()
        assert all(row["type"] != "acp" for row in before["runtime_types"] + before["runtimes"])
        enabled = await settings.update({"enabled": True, "command": ["grok", "agent", "stdio"],
                                         "model": "review-model", "account": "Review account"})
        assert enabled["enabled"] is True
        assert (await settings.read()).command == ("grok", "agent", "stdio")
        assert await pool.fetchval("SELECT count(*) FROM agent_runtimes WHERE type = 'acp'") == 1
        assert any(row["type"] == "acp" for row in (await registry.registry_view())["runtimes"])
        account = await registry.create_provider({"name": "Review account", "vendor_type": "acp-command"})
        model = await registry.create_model({"provider_id": account["provider"]["provider_id"],
                                              "name": "review-model", "protocol": "openai-responses"})
        profile = (await registry.create_profile({"name": "ACP review", "runtime_id": "rt-acp",
                                                  "model_id": model["model"]["model_id"]}))["profile"]
        assert profile["roles"] == ["reviewer"]
        await registry.create_model({"provider_id": account["provider"]["provider_id"],
                                     "name": "different-model", "protocol": "openai-responses"})
        assert [entry["name"] for entry in (await registry.list_models(runtime_id="rt-acp"))["models"]] == ["review-model"]
        await settings.update({"enabled": False})
        assert await pool.fetchval("SELECT count(*) FROM agent_runtimes WHERE type = 'acp'") == 1
        disabled = await registry.registry_view()
        assert all(row["type"] != "acp" for row in disabled["runtimes"])
        saved_profile = next(row for row in disabled["profiles"] if row["profile_id"] == profile["profile_id"])
        assert saved_profile["roles"] == []
        assert any("acp_reviewer.enabled is off" in warning for warning in saved_profile["warnings"])
        with pytest.raises(registry.RegistryError, match="enabled is off"):
            await registry.check_selection(profile["profile_id"], ("reviewer",))
        with pytest.raises(registry.RegistryError, match="enabled is off"):
            await registry.list_models(runtime_id="rt-acp")
        assert (await settings.read()).model == "review-model"
        assert await pool.fetchval(
            "SELECT count(*) FROM agent_setting_layers WHERE profile_id IN "
            "(SELECT a.id FROM agent_profiles a JOIN agent_runtimes r ON r.id=a.runtime_id WHERE r.type='acp')") == 0
    run_db(check, monkeypatch)


@pytest.mark.parametrize("body,reason", [
    ({"enabled": True}, "command"),
    ({"command": ["grok"]}, "command"),
    ({"command": ["grok", 1]}, "command"),
    ({"command": ["grok", "--always-approve"]}, "always-approve"),
    ({"enabled": "true"}, "enabled"),
    ({"account": "a\nsecret"}, "account"),
    ({"inspect_roots": "[]"}, "inspect_roots"),
    ({"attestation": True}, "unknown"),
])
def test_invalid_settings_write_nothing(monkeypatch, body, reason):
    async def check(pool):
        with pytest.raises(ValueError, match=reason):
            await settings.update(body)
        assert await pool.fetchval("SELECT count(*) FROM system_settings WHERE key LIKE 'acp_reviewer.%'") == 0
    run_db(check, monkeypatch)


def test_policy_refusal_is_visible_but_does_not_block_saving(monkeypatch):
    async def check(pool):
        view = await settings.update({"enabled": True, "command": ["unknown-cli", "acp"],
                                      "model": "review-model", "account": "Review account"})
        assert view["enabled"] is True
        assert view["policy_check"]["reason"] == "unknown_cli_config"
        assert view["latest_launch_check"] is None
        await pool.execute("INSERT INTO llm_records (agent_id, run_kind, metadata) VALUES "
                           "('reviewer-test', 'reviewer', $1::jsonb)", json.dumps({"acp_write_policy": {
                               "reason": "config_dir_overlaps", "detail": "Config directory overlaps the worktree"}}))
        latest = (await settings.view())["latest_launch_check"]
        assert latest == {"reason": "config_dir_overlaps", "detail": "Config directory overlaps the worktree"}
    run_db(check, monkeypatch)


def test_acp_provider_checks_and_pairing(monkeypatch):
    from looper.agents import provider_presets
    from looper.agents.profile_registry import LegacyProviderSpec
    from looper.agents.runtimes import ApiProtocol

    async def check(pool):
        defaults = provider_presets.vendor_defaults(VendorType.ACP_COMMAND, provider_id="acp-account")
        assert defaults.vendor_type.auth_kind is AuthKind.OAUTH
        provider = await registry.create_provider({"name": "Review account", "vendor_type": "acp-command"})
        row = await pool.fetchrow("SELECT * FROM llm_providers WHERE id = $1", provider["provider"]["provider_id"])
        assert row["oauth_runtime_type"] == "acp"
        config = settings.AcpReviewerSettings(True, ("grok", "agent", "stdio"), "review-model", "Review account")
        identity = LegacyProviderSpec("p", "Review account", AuthKind.OAUTH,
                                      frozenset({ApiProtocol.OPENAI_RESPONSES}), "acp")
        settings.check_pairing(config, identity, "review-model", None)
        with pytest.raises(ValueError, match="model"):
            settings.check_pairing(config, identity, "different-model", None)
        with pytest.raises(ValueError, match="account"):
            settings.check_pairing(config, registry.replace(identity, name="Other account"), "review-model", None)
        with pytest.raises(ValueError, match="effort"):
            settings.check_pairing(config, identity, "review-model", "high", profile_effort="low")
    run_db(check, monkeypatch)


@pytest.mark.parametrize("action", ["get", "update"])
@pytest.mark.usefixtures("clients")
def test_api_client_token_cannot_access_settings(tmp_path, monkeypatch, action):
    operation = api_v1.find("POST", f"/api/v1/acp_reviewer/{action}")
    assert operation.operator_only and operation.tool is None
    run = AsyncMock(side_effect=AssertionError("client reached operator settings"))
    monkeypatch.setattr(api_v1.domain_for(operation), "run", run)

    async def check():
        async with _serving(tmp_path) as server:
            server._client_ops = ClientOps(SimpleNamespace())
            status, _, answer = await _send(server, f"POST {operation.path}", _bearer(CLIENT_TOKEN),
                                            json.dumps({"enabled": False} if action == "update" else {}))
            assert status == 403 and answer["error"]["code"] == "forbidden"
            run.assert_not_awaited()
    asyncio.run(check())


def test_migration_widens_only_the_command_identity_checks():
    import asyncpg
    from looper.db.migrations import acp_command
    from tests.db_support import DSN, fresh_connection

    if not DSN:
        pytest.skip("LOOPER_TEST_DATABASE_URL is not set")

    async def check():
        async with fresh_connection() as conn:
            await conn.execute("CREATE TABLE schema_migrations (name TEXT PRIMARY KEY)")
            await conn.execute("""CREATE TABLE llm_providers (
                id TEXT PRIMARY KEY, vendor_type TEXT, auth TEXT, oauth_runtime_type TEXT,
                CONSTRAINT llm_providers_vendor_type_check CHECK (vendor_type IN ('custom','coco-login')),
                CONSTRAINT llm_providers_check1 CHECK (
                    (vendor_type='custom' AND auth='api_key' AND oauth_runtime_type IS NULL) OR
                    (vendor_type='coco-login' AND auth='oauth' AND oauth_runtime_type='coco')))
            """)
            await conn.execute("INSERT INTO llm_providers VALUES ('existing','coco-login','oauth','coco')")
            assert await acp_command.pending(conn)
            await acp_command.apply(conn, sql="", state={})
            assert not await acp_command.pending(conn)
            await acp_command.apply(conn, sql="", state={})
            await conn.execute("INSERT INTO llm_providers VALUES ('command','acp-command','oauth','acp')")
            for auth, runtime in (("oauth", "grok"), ("api_key", None), ("oauth", None)):
                with pytest.raises(asyncpg.CheckViolationError):
                    await conn.execute("INSERT INTO llm_providers VALUES ('invalid','acp-command',$1,$2)", auth, runtime)
            assert await conn.fetchval("SELECT count(*) FROM llm_providers") == 2
            assert await conn.fetchval("SELECT count(*) FROM schema_migrations WHERE name='acp_command'") == 1
    asyncio.run(check())


def test_import_skips_an_existing_chain_into_acp_and_allows_clearing_it(monkeypatch):
    from looper.agents import config_transfer
    from looper.db.config_transfer import read_profiles

    async def check(pool):
        await settings.update({"enabled": True, "command": ["grok", "agent", "stdio"],
                               "model": "review-model", "account": "Review account"})
        account = await registry.create_provider({"name": "Review account", "vendor_type": "acp-command"})
        model = await registry.create_model({"provider_id": account["provider"]["provider_id"],
                                              "name": "review-model", "protocol": "openai-responses"})
        acp = (await registry.create_profile({"name": "ACP review", "runtime_id": "rt-acp",
                                              "model_id": model["model"]["model_id"]}))["profile"]
        # Represent a previously stored invalid chain. The public setter correctly refuses creating it today.
        await pool.execute("INSERT INTO agent_profiles (id,name,runtime_id,model_id,fallback_profile_id) "
                           "SELECT 'agent-existing-hop', 'Existing hop', runtime_id, model_id, $1 "
                           "FROM agent_profiles WHERE id='agent-claude-code-opus'", acp["profile_id"])
        async with pool.acquire() as conn:
            builtin = next(row for row in await read_profiles(conn) if row["id"] == "agent-claude-code-opus")
            reference = {"runtime": builtin["runtime"], "model": {"provider": builtin["provider"],
                         "name": builtin["model"], "protocol": builtin["protocol"]}}
            document = {"format": "looper-config/v4", "profiles": [
                {"name": "Imported source", **reference, "fallback": "Existing hop"}]}
            async with conn.transaction():
                plan = await config_transfer.import_config(conn, document, apply=True)
            skipped = next(change for change in plan.changes if change.section == "profiles")
            assert skipped.action is config_transfer.Action.SKIPPED and "ACP" in skipped.reason
            assert await conn.fetchval("SELECT count(*) FROM agent_profiles WHERE name='Imported source'") == 0
            document["profiles"] = [{"name": "Existing hop", **reference, "fallback": None}]
            async with conn.transaction():
                await config_transfer.import_config(conn, document, apply=True)
            assert await conn.fetchval("SELECT fallback_profile_id FROM agent_profiles WHERE id='agent-existing-hop'") is None
    run_db(check, monkeypatch)


@pytest.mark.provenance
def test_off_registry_selection_and_export_match_main(monkeypatch, tmp_path):
    """Compare current main and this checkout on one disposable schema."""
    import io
    import os
    from pathlib import Path
    import subprocess
    import sys
    import tarfile

    root = Path(__file__).resolve().parents[1]
    revision = subprocess.run(["git", "rev-parse", "origin/main"], cwd=root, text=True,
                              capture_output=True, check=True).stdout.strip()
    archive = subprocess.run(["git", "archive", revision, "looper", "runners", "pyproject.toml"], cwd=root,
                             capture_output=True, check=True).stdout
    baseline = tmp_path / "main"
    baseline.mkdir()
    with tarfile.open(fileobj=io.BytesIO(archive)) as bundle:
        bundle.extractall(baseline, filter="data")

    program = r'''
import asyncio, hashlib, json, os, sys
from pathlib import Path
import asyncpg
import looper
from looper.agents import config_transfer, profile_registry, projects, runtimes
from looper.db import connection
assert Path(looper.__file__).resolve().is_relative_to(Path.cwd().resolve())

def normalized(value):
    if isinstance(value, dict):
        return {key: normalized(item) for key, item in value.items()
                if key not in {'created_at', 'updated_at', 'probed_at', 'exported_at'}}
    if isinstance(value, (list, tuple)):
        return [normalized(item) for item in value]
    return value

async def main():
    pool = await asyncpg.create_pool(os.environ['LOOPER_TEST_DATABASE_URL'], min_size=1, max_size=3,
                                    server_settings={'search_path': sys.argv[1]})
    connection._pool = pool
    profile_registry._login_modules = lambda: {}
    try:
        catalog = await profile_registry.registry_view()
        chosen, restricted = await projects.choosable_profiles(None)
        async with pool.acquire() as conn, conn.transaction(isolation='repeatable_read', readonly=True):
            exported = json.loads(''.join([part async for part in config_transfer.export_chunks(
                conn, include_projects=True, include_secrets=False)]))
        values = {'registry': catalog, 'choosable_profiles': [chosen, restricted],
                  'servable_roles': {name: profile_registry.servable_roles(adapter.capabilities, name)
                                     for name, adapter in runtimes.ADAPTERS.items() if name != 'acp'},
                  'config_export': exported}
        assert catalog['runtimes'] and catalog['profiles'] and chosen
        assert exported['runtimes'] and exported['profiles']
        print(json.dumps({key: hashlib.sha256(json.dumps(normalized(value), sort_keys=True, default=str).encode()).hexdigest()
                          for key, value in values.items()}, sort_keys=True))
    finally:
        connection._pool = None
        await pool.close()
asyncio.run(main())
'''

    async def check(pool):
        assert await settings.read() == settings.AcpReviewerSettings()
        # Include an ordinary runtime/Profile so an empty query/export cannot pass the comparison.
        await pool.execute("INSERT INTO agent_runtimes (id,name,type,capabilities,builtin) "
                           "VALUES ('rt-off-baseline','Baseline runtime','claude-code',ARRAY['run'],false)")
        await pool.execute("INSERT INTO agent_profiles (id,name,runtime_id,model_id) "
                           "SELECT 'agent-off-baseline','Baseline review','rt-off-baseline',model_id "
                           "FROM agent_profiles WHERE id='agent-claude-code-opus'")
        schema = await pool.fetchval("SHOW search_path")
        results = []
        environment = {**os.environ, "PYTHONPATH": ""}
        for checkout in (baseline, root):
            result = await asyncio.to_thread(subprocess.run, [sys.executable, "-c", program, schema],
                                            cwd=checkout, env=environment, text=True, capture_output=True,
                                            check=False, timeout=45)
            assert result.returncode == 0, result.stderr
            results.append(json.loads(result.stdout))
        assert results[0] == results[1], f"off-path differs from main {revision}: {results}"
    run_db(check, monkeypatch)
