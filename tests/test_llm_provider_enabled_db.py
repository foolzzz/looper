"""Provider availability, preserved selections and launch checks on disposable PostgreSQL."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from looper.agents import profile_registry as registry
from tests.db_support import DSN, fresh_connection, run_db, run_with_models

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")


@pytest.fixture(autouse=True)
def isolated_logins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(registry, "_login_modules", lambda: {})


async def _profile(*, provider_id: str = "prov-test", name: str = "Example profile") -> dict[str, Any]:
    model = (await registry.create_model({"provider_id": provider_id, "name": "claude-sonnet-4-5",
        "protocol": "anthropic-messages"}))["model"]
    return (await registry.create_profile({"name": name, "runtime_id": "rt-claude-code",
        "model_id": model["model_id"]}))["profile"]


def test_enabled_upgrade_once_and_fresh_parity() -> None:
    from looper.db import connection
    from tests.bridge_schema_support import structure, witness

    async def test() -> None:
        async with fresh_connection() as old, fresh_connection() as fresh:
            await connection.ensure_schema(old)
            await old.execute("ALTER TABLE llm_providers DROP COLUMN enabled")
            before, shape = await witness(old), await structure(old)
            assert await connection.schema_change_pending(old)
            assert await witness(old) == before and await structure(old) == shape
            await connection.ensure_schema(old)
            assert await old.fetchval("SELECT bool_and(enabled) FROM llm_providers") is True
            await connection.ensure_schema(fresh)
            assert await structure(old) == await structure(fresh)
            after, shape = await witness(old), await structure(old)
            assert not await connection.schema_change_pending(old)
            await connection.ensure_schema(old)
            assert await witness(old) == after and await structure(old) == shape
            assert await old.fetchval("SELECT count(*) FROM schema_migrations WHERE name='provider_enabled'") == 1
            assert not await fresh.fetchval("SELECT EXISTS (SELECT 1 FROM schema_migrations "
                                            "WHERE name='provider_enabled')")

    asyncio.run(asyncio.wait_for(test(), 30))


def test_disable_warns_about_selected_profiles_and_preserves_account(monkeypatch: pytest.MonkeyPatch) -> None:
    from looper.agents import agent_settings

    async def test(pool: Any) -> None:
        profile = await _profile()
        await agent_settings.update_system({"profile_id": profile["profile_id"]})
        await registry.update_provider("prov-test", {"api_key": "synthetic-provider-key"})
        before = dict(await pool.fetchrow("SELECT * FROM llm_providers WHERE id='prov-test'"))
        answer = await registry.set_provider_enabled("prov-test", False)
        assert answer["provider"]["enabled"] is False
        assert answer["warnings"] == ["Agent Profile 'Example profile' is selected by the defaults of every role: "
                                      "its runs will not start"]
        after = dict(await pool.fetchrow("SELECT * FROM llm_providers WHERE id='prov-test'"))
        assert after == {**before, "enabled": False, "updated_at": after["updated_at"]}
        assert await pool.fetchval("SELECT api_key FROM llm_provider_secrets WHERE provider_id='prov-test'") == "synthetic-provider-key"
        assert await pool.fetchval("SELECT count(*) FROM llm_provider_endpoints WHERE provider_id='prov-test'") == 2
        assert await pool.fetchval("SELECT count(*) FROM llm_models WHERE provider_id='prov-test'") == 1
        view = await registry.registry_view()
        assert next(p for p in view["profiles"] if p["profile_id"] == profile["profile_id"])["provider"]["enabled"] is False
        assert (await registry.get_model(profile["model_id"]))["model"]["provider_enabled"] is False
        assert await pool.fetchval("SELECT profile_id FROM agent_setting_layers WHERE scope='system'") == profile["profile_id"]
        assert await registry.set_provider_enabled("prov-test", False) == answer
        assert dict(await pool.fetchrow("SELECT * FROM llm_providers WHERE id='prov-test'")) == after
        enabled = await registry.set_provider_enabled("prov-test", True)
        assert enabled["provider"]["enabled"] is True and enabled["warnings"] == []

    run_with_models(test, monkeypatch)


def test_builtin_accounts_toggle_and_default_profile_warns(monkeypatch: pytest.MonkeyPatch) -> None:
    async def test(pool: Any) -> None:
        for enabled in (False, True):
            answer = await registry.set_provider_enabled("prov-codex-login", enabled)
            assert answer["provider"]["builtin"] is True
            assert answer["provider"]["enabled"] is enabled
        answer = await registry.set_provider_enabled("prov-claude-subscription", False)
        assert "Agent Profile 'claude-code/opus' is what a role runs when nothing selects another profile: " \
               "those runs will not start" in answer["warnings"]
        with pytest.raises(registry.RegistryNotFound, match="no provider"):
            await registry.set_provider_enabled("prov-missing", False)
        for value in (None, 0, 1, "false", []):
            with pytest.raises(registry.RegistryError, match="enabled must be true or false"):
                await registry.set_provider_enabled("prov-codex-login", value)

    run_db(test, monkeypatch, timeout=30)


def test_disable_warnings_are_name_ordered_and_truncated(monkeypatch: pytest.MonkeyPatch) -> None:
    from looper.db import projects

    async def test(pool: Any) -> None:
        profile = await _profile(name="Unused profile")
        project = await projects.create_project(name="Example project", pool=pool)
        for index in reversed(range(12)):
            profile_id, name = f"agent-selected-{index}", f"Selected {index:02d}"
            await pool.execute("INSERT INTO agent_profiles (id, name, runtime_id, model_id) "
                               "VALUES ($1, $2, 'rt-claude-code', $3)", profile_id, name, profile["model_id"])
            await pool.execute("INSERT INTO project_allowed_profiles (project_id, profile_id) VALUES ($1, $2)",
                               project["id"], profile_id)
        answer = await registry.set_provider_enabled("prov-test", False)
        assert answer["warnings"] == [
            *(f"Agent Profile 'Selected {index:02d}' is selected by the profiles the Planners of project "
              "'Example project' may choose: its runs will not start" for index in range(10)),
            "and 2 more Agent Profiles",
        ]

    run_with_models(test, monkeypatch)


def test_disabled_profile_launch_is_refused_until_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    from looper.agents import agent_settings

    async def test(pool: Any) -> None:
        profile = await _profile()
        await agent_settings.update_system({"profile_id": profile["profile_id"]})
        await registry.update_provider("prov-test", {"api_key": "synthetic-provider-key"})
        assert (await agent_settings.load_agent_settings("worker")).profile_error is None
        await registry.set_provider_enabled("prov-test", False)
        refusal = "provider 'Test account' is disabled; enable it in Settings → LLM Providers"
        for role in ("worker", "reviewer", "manager", "planner"):
            settings = await agent_settings.load_agent_settings(role)
            assert settings.profile.id == profile["profile_id"]
            assert refusal in settings.profile_error
            assert refusal in await agent_settings.profile_launch_problem(settings.profile, agent_settings.AgentType(role))
        await registry.set_provider_enabled("prov-test", True)
        assert (await agent_settings.load_agent_settings("worker")).profile_error is None

    run_with_models(test, monkeypatch)


def test_builtin_default_profile_launch_reads_provider_availability(monkeypatch: pytest.MonkeyPatch) -> None:
    from looper.agents import agent_settings
    from unittest.mock import AsyncMock

    monkeypatch.setattr(agent_settings, "_login_problem", AsyncMock(return_value=None))

    async def test(pool: Any) -> None:
        assert (await agent_settings.load_agent_settings("worker")).profile_error is None
        await registry.set_provider_enabled("prov-claude-subscription", False)
        assert "provider 'claude-subscription' is disabled" in (await agent_settings.load_agent_settings("worker")).profile_error
        await registry.set_provider_enabled("prov-claude-subscription", True)
        assert (await agent_settings.load_agent_settings("worker")).profile_error is None

    run_db(test, monkeypatch, timeout=30)


def test_disabled_provider_refuses_new_profile_bindings_but_keeps_existing_selections(monkeypatch: pytest.MonkeyPatch) -> None:
    from looper.agents import agent_settings

    async def test(pool: Any) -> None:
        existing = await _profile()
        other = await _profile(provider_id="prov-other", name="Other profile")
        await agent_settings.update_system({"profile_id": existing["profile_id"]})
        await registry.set_provider_enabled("prov-test", False)
        with pytest.raises(registry.RegistryError, match="disabled.*choose a model of another provider"):
            await registry.create_profile({"name": "New profile", "runtime_id": "rt-claude-code",
                                           "model_id": existing["model_id"]})
        with pytest.raises(registry.RegistryError, match="disabled.*choose a model of another provider"):
            await registry.update_profile(other["profile_id"], {"model_id": existing["model_id"]})
        updated = (await registry.update_profile(existing["profile_id"], {"name": "Renamed profile",
            "model_id": existing["model_id"], "effort": "high", "custom_prompt": "Check carefully."}))["profile"]
        assert (updated["name"], updated["effort"], updated["custom_prompt"]) == ("Renamed profile", "high", "Check carefully.")
        await agent_settings.update_system({"profile_id": existing["profile_id"], "harness_prompt": "Use tests."})
        assert await pool.fetchval("SELECT profile_id FROM agent_setting_layers WHERE scope='system'") == existing["profile_id"]
        assert await pool.fetchval("SELECT model_id FROM agent_profiles WHERE id=$1", other["profile_id"]) == other["model_id"]

    run_with_models(test, monkeypatch)


def test_manager_tool_updates_builtin_availability_and_lists_state(monkeypatch: pytest.MonkeyPatch) -> None:
    from looper.agents import agent_settings
    from looper.agents.ops import Call, ErrorCode, OpError, Turn, check_arguments
    from looper.agents.ops.config import TOOLS

    async def test(pool: Any) -> None:
        tool = next(tool for tool in TOOLS if tool.name == "looper_providers")

        async def call(arguments: dict[str, Any]) -> dict[str, Any]:
            return await tool.run(Call(check_arguments(tool.schema, arguments), Turn(channel=None, chat=None), None))

        answer = await call({"action": "update", "provider": "prov-codex-login", "enabled": False})
        assert answer["provider"]["enabled"] is False
        assert isinstance(answer["warnings"], list)
        listed = await call({"action": "list"})
        assert next(p for p in listed["providers"] if p["provider_id"] == "prov-codex-login")["enabled"] is False
        assert all(isinstance(p["enabled"], bool) for p in listed["providers"])
        account = (await registry.create_provider({"name": "Example account", "vendor_type": "custom",
            "endpoints": [{"protocol": "openai-chat", "base_url": "https://models.example.invalid"}]}))["provider"]
        model = (await registry.create_model({"provider_id": account["provider_id"], "name": "example[1m]",
                                              "protocol": "openai-chat"}))["model"]
        await pool.execute("INSERT INTO agent_runtimes (id, name, type, capabilities) "
                           "VALUES ('rt-example', 'Example runtime', 'omp', ARRAY['run'])")
        profile = (await registry.create_profile({"name": "Selected profile", "runtime_id": "rt-example",
                                                  "model_id": model["model_id"]}))["profile"]
        await agent_settings.update_role("worker", {"profile_id": profile["profile_id"]})
        ordinary_warnings = (await registry.update_provider(account["provider_id"], {"name": "Example account"}))["warnings"]
        assert ordinary_warnings
        mixed = await call({"action": "update", "provider": account["provider_id"],
                            "name": "Renamed account", "enabled": False})
        assert mixed["provider"]["name"] == "Renamed account" and mixed["provider"]["enabled"] is False
        assert mixed["warnings"] == [*ordinary_warnings,
            "Agent Profile 'Selected profile' is selected by the worker role: its runs will not start"]
        with pytest.raises(OpError, match="a new account is enabled; use update to disable it") as caught:
            await call({"action": "register", "name": "New account", "vendor_type": "custom", "enabled": False})
        assert caught.value.code is ErrorCode.INVALID

    run_db(test, monkeypatch, timeout=30)


def test_config_transfer_preserves_omitted_enabled_and_imports_explicit_state(monkeypatch: pytest.MonkeyPatch) -> None:
    from looper.agents import config_transfer as transfer

    async def test(pool: Any) -> None:
        profile = await _profile()

        async def exported() -> dict[str, Any]:
            async with pool.acquire() as conn:
                return json.loads("".join([chunk async for chunk in transfer.export_chunks(
                    conn, include_projects=False, include_secrets=False)]))

        async def imported(document: dict[str, Any]) -> Any:
            async with pool.acquire() as conn:
                return await transfer.run_import(conn, document, apply=True)

        original = await exported()
        account = next(p for p in original["providers"] if p["name"] == "Test account")
        assert "enabled" not in account
        await registry.set_provider_enabled("prov-test", False)
        disabled = await exported()
        assert next(p for p in disabled["providers"] if p["name"] == "Test account")["enabled"] is False
        older_file = {"format": transfer.FORMAT, "providers": [account], "profiles": [
            *original["profiles"], {**original["profiles"][0], "name": "Imported profile"}]}
        plan = await imported(older_file)
        assert not any(c.action == transfer.Action.SKIPPED for c in plan.changes)
        assert await pool.fetchval("SELECT enabled FROM llm_providers WHERE id='prov-test'") is False
        assert await pool.fetchval("SELECT id FROM agent_profiles WHERE id=$1", profile["profile_id"])
        assert await pool.fetchval("SELECT count(*) FROM agent_profiles WHERE name='Imported profile'") == 1
        plan = await imported({"format": transfer.FORMAT, "providers": [{**account, "enabled": True}]})
        assert any(c.section == "providers" and c.action == transfer.Action.CHANGED for c in plan.changes)
        assert await pool.fetchval("SELECT enabled FROM llm_providers WHERE id='prov-test'") is True
        assert (await exported())["providers"] == original["providers"]
        for value in (None, 0, "false"):
            plan = await imported({"format": transfer.FORMAT, "providers": [{**account, "enabled": value}]})
            assert any(c.section == "providers" and c.action == transfer.Action.SKIPPED
                       and c.reason == "enabled must be true or false" for c in plan.changes)
            assert await pool.fetchval("SELECT enabled FROM llm_providers WHERE id='prov-test'") is True

    run_with_models(test, monkeypatch)
