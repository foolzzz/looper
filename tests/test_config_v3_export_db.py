"""Portable v4 exports from normalized registry rows and disposable PostgreSQL."""

from __future__ import annotations

import asyncio
import json
import stat
from functools import partial
from unittest.mock import AsyncMock

import pytest

from looper.agents import config_transfer as transfer
from looper.agents.profile_registry import MODEL_FIELDS
from looper.db import config_transfer as db
from tests.db_support import DSN, run_db

needs_db = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")
SECRET = "sk-synthetic-config-v3-test-only"


_run = partial(run_db, timeout=45)


async def _seed(pool):
    await pool.execute("""
        INSERT INTO llm_providers (id, name, vendor_type, auth, env, max_concurrency) VALUES
          ('prov-export', 'Account', 'custom', 'api_key', '[{"key":"REGION","value":"test"}]', 3),
          ('prov-other', 'Other account', 'custom', 'api_key', '[]', NULL);
        INSERT INTO llm_provider_secrets VALUES ('prov-export', 'sk-synthetic-config-v3-test-only');
        INSERT INTO llm_provider_endpoints (provider_id, protocol, base_url, use_runtime_default) VALUES
          ('prov-export', 'anthropic-messages', '', true),
          ('prov-export', 'openai-chat', 'https://chat.example.invalid/v1', false),
          ('prov-other', 'openai-chat', 'https://other.example.invalid', false);
        INSERT INTO llm_models (id, provider_id, name, protocol, context_window, input_usd_per_mtok,
          output_usd_per_mtok, cache_read_usd_per_mtok, cache_write_usd_per_mtok, efforts,
          background_model, catalog_order) VALUES
          ('model-export-chat', 'prov-export', 'same-name', 'openai-chat', 1000000, 0, 2.5, 0.25, 3,
           '{low,high}', 'background[1m]', 9),
          ('model-export-messages', 'prov-export', 'same-name', 'anthropic-messages', NULL, NULL, NULL,
           NULL, NULL, '{}', NULL, 4),
          ('model-export-other', 'prov-other', 'same-name', 'openai-chat', NULL, NULL, NULL,
           NULL, NULL, NULL, NULL, 0);
        INSERT INTO agent_profiles (id, name, runtime_id, model_id, effort, agent_teams, custom_prompt) VALUES
          ('agent-export-chat', 'Chat profile', 'rt-claude-code', 'model-export-chat', 'high', true, 'Be precise.'),
          ('agent-export-messages', 'Messages profile', 'rt-claude-code', 'model-export-messages', NULL, false, '');
    """)


async def _export(pool, *, projects=False, secrets=False):
    summary = transfer.ExportSummary()
    async with pool.acquire() as conn, conn.transaction(isolation="repeatable_read", readonly=True):
        text = "".join([chunk async for chunk in transfer.export_chunks(
            conn, include_projects=projects, include_secrets=secrets, summary=summary)])
    return json.loads(text), summary


def _no_local_references(value, section=None):
    if isinstance(value, dict):
        forbidden = {"provider_id", "model_id", "runtime_id", "project_id", "work_dir",
                     "created_at", "updated_at", "builtin"}
        if section != "managers":
            forbidden.add("id")
        assert not forbidden & value.keys()
        for name, nested in value.items():
            _no_local_references(nested, name)
    elif isinstance(value, list):
        for nested in value:
            _no_local_references(nested, section)


def test_format_and_sections_are_v4():
    assert transfer.FORMAT == "looper-config/v4"
    assert "models" in transfer.SECTIONS


@pytest.mark.parametrize("include_secrets", [False, True])
def test_streamed_file_uses_only_portable_registry_fields(tmp_path, monkeypatch, include_secrets):
    """Exercise the file writer even when no disposable PostgreSQL is available."""
    private = {"id": "local-id", "provider_id": "local-provider", "model_id": "local-model",
               "builtin": False, "created_at": "local-time"}
    provider = {**private, "name": "Account", "vendor_type": "custom", "auth": "api_key",
                "oauth_runtime_type": None, "env": [], "max_concurrency": None, "api_key": SECRET,
                "endpoints": [{"provider_id": "local-provider", "protocol": "openai-chat",
                               "base_url": "https://example.invalid/v1", "use_runtime_default": False},
                              {"protocol": "anthropic-messages", "base_url": "", "use_runtime_default": True}]}
    model = {**dict.fromkeys(MODEL_FIELDS), **private, "provider": "Account", "name": "same-name",
             "protocol": "openai-chat", "input_usd_per_mtok": 0, "efforts": [], "catalog_order": 7}
    profile = {**private, "name": "Profile", "runtime": "claude-code", "provider": "Account",
               "model": "same-name", "protocol": "openai-chat", "effort": None, "agent_teams": False,
               "custom_prompt": "", "system_prompt_mode": "replace"}
    reads = {
        "read_managers": [], "read_manager_installs": [], "read_manager_templates": [], "read_projects": [],
        "read_layers": [], "read_items": [], "read_installs": [], "read_channels": [], "read_roles": [],
        "read_runtimes": [{**private, "name": "custom-runtime", "type": "claude-code",
                           "executable": "/machine/private/claude"}],
        "read_providers": [provider, {**provider, "name": "builtin", "builtin": True}],
        "read_models": [model, {**model, "protocol": "anthropic-messages", "efforts": None},
                        {**model, "name": "builtin", "builtin": True}],
        "read_profiles": [profile, {**profile, "name": "builtin", "builtin": True}],
    }
    for name, rows in reads.items():
        monkeypatch.setattr(db, name, AsyncMock(return_value=rows))
    conn = AsyncMock()
    conn.fetchval.return_value = False  # no stored scheduler settings
    path = tmp_path / "portable.json"
    summary = asyncio.run(transfer.write_export(conn, path, include_projects=False, include_secrets=include_secrets))
    data = json.loads(path.read_text())
    assert data["format"] == "looper-config/v4"
    assert len(data["providers"]) == len(data["profiles"]) == 1
    assert len(data["models"]) == 2 and summary.counts["models"] == 2
    assert data["providers"][0]["api_key"] == (SECRET if include_secrets else transfer.PLACEHOLDER)
    assert data["providers"][0]["endpoints"] == {
        "openai-chat": {"base_url": "https://example.invalid/v1"},
        "anthropic-messages": {"base_url": "", "use_runtime_default": True}}
    assert "models" not in data["providers"][0] and "protocols" not in data["providers"][0]
    assert data["profiles"][0]["model"] == {
        "provider": "Account", "name": "same-name", "protocol": "openai-chat"}
    assert "provider" not in data["profiles"][0] and "system_prompt_mode" not in data["profiles"][0]
    assert data["models"][0]["input_usd_per_mtok"] == 0
    assert data["models"][0]["output_usd_per_mtok"] is None
    assert data["models"][0]["efforts"] == [] and data["models"][1]["efforts"] is None
    assert all(m["catalog_order"] == 7 for m in data["models"])
    assert data["runtimes"][0]["executable"] is None
    assert summary.secrets == 1 and summary.bytes == path.stat().st_size
    assert (SECRET in path.read_text()) is include_secrets
    assert "local-" not in path.read_text()
    _no_local_references(data)


@needs_db
def test_models_have_natural_keys_and_preserve_price_metadata():
    async def test(pool):
        await _seed(pool)
        data, summary = await _export(pool)
        assert data["format"] == "looper-config/v4"
        assert set(data) == {"format", "looper_version", "exported_at", "secrets", "runtimes",
                             "providers", "models", "profiles", "settings", "channels", "installs", "catalog", "managers"}
        assert len(data["providers"]) == 2
        provider = data["providers"][0]
        assert provider == {
            "name": "Account", "vendor_type": "custom", "auth": "api_key", "oauth_runtime_type": None,
            "env": [{"key": "REGION", "value": "test"}], "max_concurrency": 3,
            "api_key": transfer.PLACEHOLDER,
            "endpoints": {"anthropic-messages": {"base_url": "", "use_runtime_default": True},
                          "openai-chat": {"base_url": "https://chat.example.invalid/v1"}},
        }
        models = {(m["provider"], m["name"], m["protocol"]): m for m in data["models"]}
        assert set(models) == {("Account", "same-name", "openai-chat"),
                               ("Account", "same-name", "anthropic-messages"),
                               ("Other account", "same-name", "openai-chat")}
        chat = models[("Account", "same-name", "openai-chat")]
        assert {field: chat.get(field) for field in MODEL_FIELDS} == {
            "name": "same-name", "context_window": 1000000, "input_usd_per_mtok": 0,
            "output_usd_per_mtok": 2.5, "cache_read_usd_per_mtok": 0.25, "cache_write_usd_per_mtok": 3,
            "efforts": ["low", "high"], "background_model": "background[1m]"}
        assert chat["catalog_order"] == 9
        messages = models[("Account", "same-name", "anthropic-messages")]
        assert messages.get("input_usd_per_mtok") is None and messages["efforts"] == []
        assert models[("Other account", "same-name", "openai-chat")].get("efforts") is None
        assert messages["catalog_order"] == 4
        assert data["profiles"] == [
            {"name": "Chat profile", "runtime": "claude-code",
             "model": {"provider": "Account", "name": "same-name", "protocol": "openai-chat"},
             "effort": "high", "agent_teams": True, "custom_prompt": "Be precise."},
            {"name": "Messages profile", "runtime": "claude-code",
             "model": {"provider": "Account", "name": "same-name", "protocol": "anthropic-messages"},
             "effort": None, "agent_teams": False, "custom_prompt": ""},
        ]
        assert summary.counts["models"] == 3 and summary.counts["providers"] == 2
        assert summary.secrets == 1
        _no_local_references(data)
    _run(test)


@needs_db
def test_builtin_model_references_do_not_export_builtin_definitions():
    async def test(pool):
        await pool.execute("""
            INSERT INTO agent_profiles (id, name, runtime_id, model_id)
            SELECT 'agent-export-login', 'Login profile', 'rt-claude-code', id FROM llm_models
            WHERE provider_id = 'prov-claude-subscription' AND name = 'opus';
            INSERT INTO llm_models (id, provider_id, name, protocol)
            VALUES ('model-export-login-custom', 'prov-claude-subscription', 'custom-name', 'anthropic-messages');
        """)
        data, summary = await _export(pool)
        assert data["providers"] == data["runtimes"] == []
        assert len(data["models"]) == len(data["profiles"]) == 1
        assert data["models"][0]["provider"] == "claude-subscription"
        assert data["models"][0]["name"] == "custom-name"
        assert data["profiles"][0]["model"] == {
            "provider": "claude-subscription", "name": "opus", "protocol": "anthropic-messages"}
        assert summary.counts["models"] == 1
        _no_local_references(data)
    _run(test)


async def _seed_content(pool):
    from looper.agents import scheduler_settings

    async with pool.acquire() as conn:
        skill_id = await db.write_item(conn, None, {
            "kind": "skill", "name": "Export skill", "origin": "ui", "source": "/machine/private/skill",
            "version": "1", "description": "Export fixture", "modified": False, "sha256": "test",
            "size_bytes": 3, "file_count": 1}, owner_project_id=None,
            files=[{"path": "SKILL.md", "content": "old", "sha256": "test", "executable": False}], server=None)
        await db.write_item(conn, None, {
            "kind": "mcp", "name": "Export MCP", "origin": "ui", "source": "", "version": "",
            "description": "Test server", "modified": False, "sha256": "test", "size_bytes": 0, "file_count": 0},
            owner_project_id=None, files=None,
            server={"transport": "stdio", "command": "example-mcp", "args": [], "env": {"TOKEN": SECRET},
                    "url": "", "headers": {}})
        await db.write_item(conn, None, {
            "kind": "mcp", "name": "Export HTTP MCP", "origin": "ui", "source": "", "version": "",
            "description": "Test HTTP server", "modified": False, "sha256": "test", "size_bytes": 0,
            "file_count": 0}, owner_project_id=None, files=None,
            server={"transport": "http", "command": "", "args": [], "env": {},
                    "url": "https://mcp.example.invalid", "headers": {"Authorization": SECRET}})
    await pool.execute("""
        INSERT INTO projects (id, name, work_dir, description) VALUES
          ('proj-export', 'Export project', '/machine/private/project', 'Portable project');
        INSERT INTO agent_setting_layers (scope, role, profile_id, system_prompt_mode) VALUES
          ('role', 'worker', 'agent-export-messages', 'replace');
        INSERT INTO project_documents (id, project_id, title, summary, source_kind, path, size_bytes, sha256)
          VALUES ('doc-export', 'proj-export', 'Guide', 'Read guide', 'path', 'docs/guide.md', 0, '');
        INSERT INTO schedules (id, project_id, name, cron, timezone, requirement, work_dir, enabled)
          VALUES ('schedule-export', 'proj-export', 'Hourly', '0 * * * *', 'UTC', 'Check configuration',
                  '/machine/private/schedule', true);
        INSERT INTO schedule_profile_overrides VALUES ('schedule-export', 'worker', 'agent-export-chat');
        INSERT INTO schedule_task_limits VALUES ('schedule-export', 'worker', 2);
        INSERT INTO role_configs (role, system_prompt, default_prompt, timeout_seconds, enabled)
          VALUES ('worker', 'Custom role prompt', 'Default prompt', 123, true);
        INSERT INTO channel_configs (channel_id, channel_type, display_name, enabled, config) VALUES
          ('channel-telegram', 'telegram', 'Export channel', true,
           '{"allowed_user_ids":["12345"],"webhook_secret":"sk-synthetic-config-v3-test-only"}');
    """)
    await pool.execute("INSERT INTO system_settings (key, value) VALUES ($1, '60')",
                       scheduler_settings.MIN_INTERVAL_KEY)
    async with pool.acquire() as conn:
        await db.write_install(conn, None, item_id=skill_id, project_id=None, roles=["worker"], enabled=True)
    return skill_id


@needs_db
@pytest.mark.parametrize("include_secrets", [False, True])
def test_file_export_keeps_sections_secrets_repoint_and_read_only_snapshot(tmp_path, monkeypatch, include_secrets):
    async def test(pool):
        await _seed(pool)
        await _seed_content(pool)
        path = tmp_path / "config.json"
        original_write = transfer.write_export
        original_connect = transfer._read_only
        observed = []

        async def connect(_dsn):
            # Reach only this test's disposable schema, still using the real read-only connection setup.
            async with pool.acquire() as source:
                schema = await source.fetchval("SHOW search_path")
            conn = await original_connect(DSN)
            await conn.execute(f'SET search_path TO "{schema}"')
            return conn

        async def write(conn, output, **kwargs):
            observed.append(await conn.fetchrow(
                "SELECT current_setting('transaction_read_only') AS ro, "
                "current_setting('transaction_isolation') AS isolation, "
                "current_setting('default_transaction_read_only') AS default_ro"))
            return await original_write(conn, output, **kwargs)

        monkeypatch.setattr(transfer, "_read_only", connect)
        monkeypatch.setattr(transfer, "write_export", write)
        summary = await transfer.export_to_file(DSN, path, include_projects=True, include_secrets=include_secrets)
        assert [dict(row) for row in observed] == [{"ro": "on", "isolation": "repeatable read", "default_ro": "on"}]
        data = json.loads(path.read_text())
        secret = SECRET if include_secrets else transfer.PLACEHOLDER
        assert data["providers"][0]["api_key"] == secret
        assert data["providers"][1]["api_key"] is None
        assert data["channels"][0]["config"]["webhook_secret"] == secret
        servers = {item["server"]["transport"]: item["server"] for item in data["catalog"] if item["kind"] == "mcp"}
        assert servers["stdio"]["env"]["TOKEN"] == servers["http"]["headers"]["Authorization"] == secret
        assert summary.secrets == 4 and summary.bytes == path.stat().st_size
        assert (SECRET in path.read_text()) is include_secrets
        assert data["secrets"] == ("included" if include_secrets else "omitted")
        if include_secrets:
            assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert data["settings"]["layers"][0]["system_prompt_mode"] == "replace"
        assert data["settings"]["roles"][0]["prompt"] == "Custom role prompt"
        assert data["settings"]["scheduler"] == {"min_interval_seconds": 60}
        assert data["installs"][0]["item"] == {"kind": "skill", "name": "Export skill"}
        project = data["projects"][0]
        assert project["name"] == "Export project"
        assert project["knowledge"][0]["path"] == "docs/guide.md"
        assert project["knowledge"][0]["repoint"] is True and summary.repoint == 1
        schedule = project["schedules"][0]
        assert schedule["profiles"] == {"worker": "Chat profile"}
        assert schedule["task_limits"] == {"worker": 2} and schedule["repoint"] is True
        assert "enabled" not in schedule and "consecutive_failures" not in schedule
        assert "/machine/private" not in path.read_text()
        assert next(item for item in data["catalog"] if item["kind"] == "skill")["source"] is None
        assert not list(tmp_path.glob(".config.json.*"))
        _no_local_references(data)
        without_projects, _ = await _export(pool)
        assert "projects" not in without_projects
    _run(test)


@needs_db
def test_catalog_is_streamed_lazily_from_one_snapshot(monkeypatch):
    async def test(pool):
        await _seed(pool)
        skill_id = await _seed_content(pool)
        original_files = db.read_item_files
        reads = []

        async def read_files(conn, item_id):
            reads.append(item_id)
            return await original_files(conn, item_id)

        monkeypatch.setattr(db, "read_item_files", read_files)
        async with pool.acquire() as conn, conn.transaction(isolation="repeatable_read", readonly=True):
            stream = transfer.export_chunks(conn, include_projects=False, include_secrets=False)
            try:
                head = await anext(stream)
                assert reads == []
                assert json.loads(head + "}")["models"]  # positive control: the first chunk is a real registry
                await pool.execute("UPDATE library_files SET content = 'new' WHERE item_id = $1", skill_id)
                await pool.execute("UPDATE llm_models SET context_window = 1234 WHERE id = 'model-export-chat'")
                data = json.loads(head + "".join([chunk async for chunk in stream]))
            finally:
                await stream.aclose()
        assert reads == [skill_id]
        skill = next(item for item in data["catalog"] if item["kind"] == "skill")
        assert skill["files"][0]["content"] == "old"
        assert next(m for m in data["models"] if m["protocol"] == "openai-chat")["context_window"] == 1000000
        newer, _ = await _export(pool)
        assert next(item for item in newer["catalog"] if item["kind"] == "skill")["files"][0]["content"] == "new"
        assert next(m for m in newer["models"] if m["provider"] == "Account" and m["protocol"] == "openai-chat")[
            "context_window"] == 1234
    _run(test)
