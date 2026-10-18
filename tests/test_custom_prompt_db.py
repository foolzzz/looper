"""Custom profile text survives registry writes, imports and effort conversion."""

from __future__ import annotations

import json
from typing import Any

import pytest

from looper.agents.agent_settings import HARNESS_MAX_CHARS
from tests.test_catalog_db import DSN
from tests.test_profile_registry_db import _call, _ok, _run
from tests.test_system_prompt_mode_db import _profile

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")


def test_custom_prompt_registry_keeps_omitted_updates_and_strips_text(monkeypatch: pytest.MonkeyPatch) -> None:
    async def run(pool: Any) -> None:
        assert (await _profile("default"))["custom_prompt"] == ""
        profile = await _profile("custom", custom_prompt=" \nCUSTOM\n ")
        assert profile["custom_prompt"] == "CUSTOM"
        profile_id = profile["profile_id"]
        updated = await _ok("PUT", f"/api/agent-profiles/{profile_id}", {"model": "opus"})
        assert updated["profile"]["custom_prompt"] == "CUSTOM"
        registry = await _ok("GET", "/api/registry")
        assert next(p for p in registry["profiles"] if p["profile_id"] == profile_id)["custom_prompt"] == "CUSTOM"
        assert await pool.fetchval("SELECT custom_prompt FROM agent_profiles WHERE id = $1", profile_id) == "CUSTOM"
        cleared = await _ok("PUT", f"/api/agent-profiles/{profile_id}", {"custom_prompt": " \n "})
        assert cleared["profile"]["custom_prompt"] == ""

    _run(run, monkeypatch)


@pytest.mark.parametrize("value", [None, False, 1, [], {}, "x" * (HARNESS_MAX_CHARS + 1)])
def test_custom_prompt_bad_values_return_400(monkeypatch: pytest.MonkeyPatch, value: Any) -> None:
    async def run(_pool: Any) -> None:
        profile = await _profile("valid")
        for method, path, fields in [
            ("POST", "/api/agent-profiles", {"name": "bad", "runtime_id": "rt-claude-code",
                                           "provider_id": "prov-claude-subscription", "model": "sonnet"}),
            ("PUT", f"/api/agent-profiles/{profile['profile_id']}", {}),
        ]:
            status, payload = await _call(method, path, {**fields, "custom_prompt": value})
            assert status == 400 and "custom_prompt" in payload["error"]
            if isinstance(value, str):
                assert str(HARNESS_MAX_CHARS) in payload["error"]

    _run(run, monkeypatch)


def test_custom_prompt_config_round_trip_legacy_and_derived(monkeypatch: pytest.MonkeyPatch) -> None:
    from looper.agents.config_transfer import Action, export_chunks, import_config

    async def run(pool: Any) -> None:
        profile = await _profile("custom", custom_prompt="CUSTOM")
        await _profile("plain-high", effort="high")
        async with pool.acquire() as conn:
            document = json.loads("".join([chunk async for chunk in export_chunks(
                conn, include_projects=False, include_secrets=False)]))
            exported = next(row for row in document["profiles"] if row["name"] == "custom")
            assert exported["custom_prompt"] == "CUSTOM"
            await conn.execute("DELETE FROM agent_profiles WHERE id = $1", profile["profile_id"])
            plan = await import_config(conn, {"profiles": [exported]}, apply=True)
            assert next(c for c in plan.changes if c.key == "custom").action is Action.ADDED
            assert await conn.fetchval("SELECT custom_prompt FROM agent_profiles WHERE name = 'custom'") == "CUSTOM"
            await import_config(conn, {"settings": {"layers": [
                {"scope": "system", "profile": "custom", "effort": "high"}]}}, apply=True)
            row = await conn.fetchrow("SELECT a.name, a.effort, a.custom_prompt FROM agent_profiles a "
                                      "JOIN agent_setting_layers l ON l.profile_id = a.id WHERE l.scope = 'system'")
            assert dict(row) == {"name": "custom · high", "effort": "high", "custom_prompt": "CUSTOM"}
            legacy = {k: v for k, v in exported.items() if k != "custom_prompt"}
            await import_config(conn, {"profiles": [legacy]}, apply=True)
            assert await conn.fetchval("SELECT custom_prompt FROM agent_profiles WHERE name = 'custom'") == ""

    _run(run, monkeypatch)


def test_custom_prompt_schema_upgrade_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    from looper.db import connection

    async def run(pool: Any) -> None:
        await pool.execute("ALTER TABLE agent_profiles DROP COLUMN custom_prompt")
        await connection.ensure_schema(pool)
        assert await pool.fetchval("SELECT custom_prompt FROM agent_profiles LIMIT 1") == ""
        await pool.execute("UPDATE agent_profiles SET custom_prompt = 'CUSTOM'")
        await connection.ensure_schema(pool)
        assert await pool.fetchval("SELECT custom_prompt FROM agent_profiles LIMIT 1") == "CUSTOM"

    _run(run, monkeypatch)


def test_manager_profile_tool_saves_lists_and_updates_custom_prompt(monkeypatch: pytest.MonkeyPatch) -> None:
    from looper.agents.ops import Call, Turn
    from looper.agents.ops.config import TOOLS

    async def run(_pool: Any) -> None:
        tool = next(tool for tool in TOOLS if tool.name == "looper_agent_profiles")
        turn = Turn("web", "chat", "message")
        created = await tool.run(Call({"action": "create", "name": "tool-profile", "runtime": "claude-code",
                                      "provider": "claude-subscription", "model": "sonnet",
                                      "custom_prompt": " CUSTOM "}, turn, None))
        assert created["profile"]["custom_prompt"] == "CUSTOM"
        listed = await tool.run(Call({"action": "list"}, turn, None))
        assert next(p for p in listed["profiles"] if p["name"] == "tool-profile")["custom_prompt"] == "CUSTOM"
        updated = await tool.run(Call({"action": "update", "profile": "tool-profile",
                                      "custom_prompt": "CHANGED"}, turn, None))
        assert updated["profile"]["custom_prompt"] == "CHANGED"

    _run(run, monkeypatch)
