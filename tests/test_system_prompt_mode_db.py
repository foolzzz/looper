"""System prompt modes through the registry, migration and configuration transfer."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from tests.test_catalog_db import DSN
from tests.test_profile_registry_db import _call, _ok, _run, _runtime

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")


@pytest.fixture
def append_only(monkeypatch: pytest.MonkeyPatch) -> str:
    from looper.agents import runtimes
    from looper.agents.runtimes.claude_code import ClaudeCodeRuntime

    class AppendOnly(ClaudeCodeRuntime):
        type = "append-only"
        label = "Append-only"
        can_replace_system_prompt = False

    monkeypatch.setitem(runtimes.ADAPTERS, AppendOnly.type, AppendOnly())
    return AppendOnly.type


async def _profile(name: str, **fields: Any) -> dict[str, Any]:
    return (await _ok("POST", "/api/agent-profiles", {
        "name": name, "runtime_id": "rt-claude-code", "provider_id": "prov-claude-subscription",
        "model": "sonnet", **fields}))["profile"]


def test_system_prompt_mode_api_round_trip_and_runtime_capabilities(monkeypatch: pytest.MonkeyPatch) -> None:
    async def run(pool: Any) -> None:
        default = await _profile("default")
        replacing = await _profile("replacing", system_prompt_mode="replace")
        assert default["system_prompt_mode"] == "append"
        assert replacing["system_prompt_mode"] == "replace"
        registry = await _ok("GET", "/api/registry")
        assert {p["name"]: p["system_prompt_mode"] for p in registry["profiles"]} == {
            "claude-code/opus": "append", "default": "append", "replacing": "replace"}
        for runtime_type in registry["runtime_types"]:
            assert runtime_type["can_replace_system_prompt"] is True
            assert runtime_type["replace_system_prompt_refusal"] is None
        updated = await _ok("PUT", f"/api/agent-profiles/{replacing['profile_id']}", {"model": "opus"})
        assert updated["profile"]["system_prompt_mode"] == "replace"
        updated = await _ok("PUT", f"/api/agent-profiles/{default['profile_id']}", {"system_prompt_mode": "replace"})
        assert updated["profile"]["system_prompt_mode"] == "replace"
        assert await pool.fetchval("SELECT system_prompt_mode FROM agent_profiles WHERE id = $1",
                                   default["profile_id"]) == "replace"

    _run(run, monkeypatch)


def test_system_prompt_mode_refuses_unsupported_and_unknown_runtimes(
    monkeypatch: pytest.MonkeyPatch, append_only: str,
) -> None:
    from looper.agents import runtimes

    async def run(pool: Any) -> None:
        other = await _runtime(append_only)
        provider = (await _ok("POST", "/api/providers", {
            "name": "key", "protocols": ["anthropic-messages"], "auth": "api_key"}))["provider"]["provider_id"]
        profile = await _profile("replacing", provider_id=provider, system_prompt_mode="replace")
        refusal = "the Append-only runtime cannot replace its default system prompt"
        registry = await _ok("GET", "/api/registry")
        type_view = next(t for t in registry["runtime_types"] if t["type"] == append_only)
        assert type_view["can_replace_system_prompt"] is False
        assert type_view["replace_system_prompt_refusal"] == refusal
        for method, path, body in [
            ("POST", "/api/agent-profiles", {"name": "refused", "runtime_id": other, "provider_id": provider,
                                           "model": "sonnet", "system_prompt_mode": "replace"}),
            ("PUT", f"/api/agent-profiles/{profile['profile_id']}", {"runtime_id": other}),
        ]:
            status, payload = await _call(method, path, body)
            assert status == 400 and refusal in payload["error"]
        plain = await _profile("plain", runtime_id=other, provider_id=provider)
        status, payload = await _call("PUT", f"/api/agent-profiles/{plain['profile_id']}",
                                      {"system_prompt_mode": "replace"})
        assert status == 400 and refusal in payload["error"]
        assert await pool.fetchval("SELECT runtime_id FROM agent_profiles WHERE id = $1",
                                   profile["profile_id"]) == "rt-claude-code"
        monkeypatch.delitem(runtimes.ADAPTERS, append_only)
        status, payload = await _call("PUT", f"/api/agent-profiles/{plain['profile_id']}",
                                      {"system_prompt_mode": "replace"})
        assert status == 400 and append_only in payload["error"]

    _run(run, monkeypatch)


@pytest.mark.parametrize("value", ["invalid", "APPEND", "", None, True, 1, [], {}])
def test_system_prompt_mode_api_rejects_invalid_values(monkeypatch: pytest.MonkeyPatch, value: Any) -> None:
    async def run(_pool: Any) -> None:
        profile = await _profile("valid")
        for method, path, fields in [
            ("POST", "/api/agent-profiles", {"name": "bad", "runtime_id": "rt-claude-code",
                                           "provider_id": "prov-claude-subscription", "model": "sonnet"}),
            ("PUT", f"/api/agent-profiles/{profile['profile_id']}", {}),
        ]:
            status, payload = await _call(method, path, {**fields, "system_prompt_mode": value})
            assert status == 400 and payload["error"] == 'system_prompt_mode must be "append" or "replace"'

    _run(run, monkeypatch)


def test_system_prompt_mode_migration_default_check_and_idempotence(monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncpg

    from looper.db.connection import ensure_schema

    async def run(pool: Any) -> None:
        await pool.execute("ALTER TABLE agent_profiles DROP COLUMN system_prompt_mode")
        await pool.execute("INSERT INTO agent_profiles (id, name, runtime_id, provider_id, model) VALUES "
                           "('old-profile', 'old', 'rt-claude-code', 'prov-claude-subscription', 'sonnet')")
        await ensure_schema(pool)
        assert await pool.fetchval("SELECT system_prompt_mode FROM agent_profiles WHERE id = 'old-profile'") == "append"
        await pool.execute("UPDATE agent_profiles SET system_prompt_mode = 'replace' WHERE id = 'old-profile'")
        await ensure_schema(pool)
        assert await pool.fetchval("SELECT system_prompt_mode FROM agent_profiles WHERE id = 'old-profile'") == "replace"
        with pytest.raises(asyncpg.CheckViolationError):
            await pool.execute("UPDATE agent_profiles SET system_prompt_mode = 'invalid' WHERE id = 'old-profile'")
        await pool.execute("INSERT INTO agent_profiles (id, name, runtime_id, provider_id, model) VALUES "
                           "('new-profile', 'new', 'rt-claude-code', 'prov-claude-subscription', 'sonnet')")
        assert await pool.fetchval("SELECT system_prompt_mode FROM agent_profiles WHERE id = 'new-profile'") == "append"

    _run(run, monkeypatch)


def test_system_prompt_mode_config_round_trip_legacy_default_and_refusal(
    monkeypatch: pytest.MonkeyPatch, append_only: str,
) -> None:
    from looper.agents.config_transfer import Action, export_chunks, import_config

    async def run(pool: Any) -> None:
        other = await _runtime(append_only)
        provider = (await _ok("POST", "/api/providers", {
            "name": "key", "protocols": ["anthropic-messages"], "auth": "api_key"}))["provider"]["provider_id"]
        profile = await _profile("replacing", provider_id=provider, system_prompt_mode="replace")
        async with pool.acquire() as conn:
            document = json.loads("".join([chunk async for chunk in export_chunks(
                conn, include_projects=False, include_secrets=False)]))
            exported = next(row for row in document["profiles"] if row["name"] == "replacing")
            assert exported["system_prompt_mode"] == "replace"
            await conn.execute("DELETE FROM agent_profiles WHERE id = $1", profile["profile_id"])
            plan = await import_config(conn, {"profiles": [exported]}, apply=True)
            assert next(c for c in plan.changes if c.key == "replacing").action is Action.ADDED
            assert await conn.fetchval("SELECT system_prompt_mode FROM agent_profiles WHERE name = 'replacing'") == "replace"
            legacy = {k: v for k, v in exported.items() if k != "system_prompt_mode"}
            await import_config(conn, {"profiles": [legacy]}, apply=True)
            assert await conn.fetchval("SELECT system_prompt_mode FROM agent_profiles WHERE name = 'replacing'") == "append"
            refused = {**exported, "name": "refused", "runtime": append_only, "agent_teams": False}
            plan = await import_config(conn, {"profiles": [refused]}, apply=True)
            change = next(c for c in plan.changes if c.key == "refused")
            assert change.action is Action.SKIPPED and "cannot replace" in change.reason
            assert await conn.fetchval("SELECT count(*) FROM agent_profiles WHERE runtime_id = $1", other) == 0
            plan = await import_config(conn, {"profiles": [{**exported, "system_prompt_mode": "wrong"}]}, apply=True)
            assert next(c for c in plan.changes if c.key == "replacing").action is Action.SKIPPED

    _run(run, monkeypatch)


def test_system_prompt_mode_config_derived_profile_keeps_replace(monkeypatch: pytest.MonkeyPatch) -> None:
    from looper.agents.config_transfer import import_config

    async def run(pool: Any) -> None:
        await _profile("base", system_prompt_mode="replace")
        await _profile("append-high", effort="high")
        async with pool.acquire() as conn:
            await import_config(conn, {"settings": {"layers": [
                {"scope": "system", "profile": "base", "effort": "high"}]}}, apply=True)
            row = await conn.fetchrow("SELECT a.name, a.effort, a.system_prompt_mode FROM agent_profiles a "
                                      "JOIN agent_setting_layers l ON l.profile_id = a.id WHERE l.scope = 'system'")
            assert dict(row) == {"name": "base · high", "effort": "high", "system_prompt_mode": "replace"}

    _run(run, monkeypatch)


def test_manager_profile_tool_saves_and_lists_system_prompt_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    from looper.agents.ops import Call, Turn
    from looper.agents.ops.config import TOOLS

    async def run(_pool: Any) -> None:
        tool = next(tool for tool in TOOLS if tool.name == "looper_agent_profiles")
        turn = Turn("web", "chat", "message")
        created = await tool.run(Call({"action": "create", "name": "tool-profile", "runtime": "claude-code",
                                      "provider": "claude-subscription", "model": "sonnet",
                                      "system_prompt_mode": "replace"}, turn, None))
        assert created["profile"]["system_prompt_mode"] == "replace"
        listed = await tool.run(Call({"action": "list"}, turn, None))
        assert next(profile for profile in listed["profiles"] if profile["name"] == "tool-profile")[
            "system_prompt_mode"] == "replace"
        updated = await tool.run(Call({"action": "update", "profile": "tool-profile",
                                      "system_prompt_mode": "append"}, turn, None))
        assert updated["profile"]["system_prompt_mode"] == "append"

    _run(run, monkeypatch)


@pytest.mark.parametrize("role", ["manager", "worker"])
@pytest.mark.parametrize("mode", ["append", "replace"])
def test_real_launch_records_system_prompt_mode_in_history(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, role: str, mode: str,
) -> None:
    import agent_launch_parity as parity
    from fake_claude import fake_claude
    from tests.test_profile_registry_db import _launch

    async def run(_pool: Any) -> None:
        profile = await _profile("selected", system_prompt_mode=mode)
        await _ok("PUT", "/api/agent-settings/system", {"profile_id": profile["profile_id"]})
        if role == "worker":
            project = (await _ok("POST", "/api/projects", {"name": "calc"}))["project_id"]
            task = (await _ok("POST", "/api/tasks/create", {
                "requirement": "do it", "project_id": project, "work_dir": str(tmp_path / "work")}))["task_id"]
            seen = await _launch("worker", task, tmp_path / "work", tmp_path)
        else:
            out = tmp_path / "manager-capture.json"
            await parity._manager(fake_claude(tmp_path / "claude-manager", parity._CLAUDE.format(out=str(out))), tmp_path)
            seen = json.loads(out.read_text())
        flag = "--system-prompt-file" if mode == "replace" else "--append-system-prompt-file"
        assert flag in seen["argv"]
        records = (await _ok("GET", "/api/llm-records"))["records"]
        record = next(row for row in records if row["agent_id"] == ("manager" if role == "manager" else "worker-x"))
        assert record["status"] == "completed"
        assert record["metadata"]["system_prompt_mode"] == mode
        detail = await _ok("GET", f"/api/llm-records/{record['id']}")
        assert detail["record"]["metadata"]["system_prompt_mode"] == mode

    _run(run, monkeypatch)
