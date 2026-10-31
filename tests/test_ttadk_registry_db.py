"""TTADK runtimes register independently and enforce provider pairing on writes."""

from pathlib import Path

import pytest

from fake_claude import fake_claude
from looper.agents import agent_settings, profile_registry as registry, runtimes
from looper.db import agent_setting_layers, profile_registry as db
from tests.db_support import DSN, run_db
from tests.test_profile_registry_db import _version_codex

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")


@pytest.mark.parametrize("key", ["CI", "DEBUG", "FORCE_COLOR", "TTADK_DEBUG"])
def test_existing_native_provider_environment_saves_and_loads_for_every_role(monkeypatch, key):
    monkeypatch.setattr(registry, "_login_modules", lambda: {})

    async def test(pool):
        provider_id = (await registry.create_provider({
            "name": "Native account", "vendor_type": "custom", "api_key": "test-native-key",
            "endpoints": [{"protocol": "anthropic-messages", "base_url": "https://example.invalid"}],
        }))["provider"]["provider_id"]
        model_id = (await registry.create_model({"provider_id": provider_id, "name": "native-model",
            "protocol": "anthropic-messages"}))["model"]["model_id"]
        profile_id = (await registry.create_profile({"name": "Native profile",
            "runtime_id": "rt-claude-code", "model_id": model_id}))["profile"]["profile_id"]
        for role in agent_settings.AgentType:
            await agent_setting_layers.put_layer("role", role.value, None, {"profile_id": profile_id})
        saved = await registry.update_provider(provider_id, {"env": [{"key": key, "value": "1"}]})
        assert saved["provider"]["env"] == [{"key": key, "value": "1"}]
        for role in agent_settings.AgentType:
            settings = await agent_settings.load_agent_settings(role)
            assert settings.error is None and settings.profile_error is None
            assert settings.profile.id == profile_id
            assert settings.profile.runtime.launcher is runtimes.Launcher.NATIVE
            assert agent_settings.Launch(settings, role=role).environment()[key] == "1"

    run_db(test, monkeypatch, timeout=30)


def test_ttadk_profile_provider_refuses_gateway_key_environment(monkeypatch):
    monkeypatch.setattr(registry, "_login_modules", lambda: {})

    async def test(pool):
        await db.insert_builtin_runtime("rt-ttadk-codex", {
            "name": "ttadk-codex", "type": "codex", "launcher": "ttadk", "executable": "",
            "version": "ttadk stand-in", "probe_error": None,
            "capabilities": sorted(c.value for c in runtimes.adapter("codex").capabilities)})
        provider = (await registry.create_provider({
            "name": "TTADK account", "vendor_type": "codex-login",
        }))["provider"]["provider_id"]
        model = (await registry.create_model({"provider_id": provider, "name": "ttadk-test-model",
            "protocol": "openai-responses"}))["model"]["model_id"]
        await registry.create_profile({"name": "TTADK profile", "runtime_id": "rt-ttadk-codex",
                                       "model_id": model})
        with pytest.raises(registry.RegistryError, match="TTADK_OPENAI_API_KEY"):
            await registry.update_provider(provider, {
                "env": [{"key": "TTADK_OPENAI_API_KEY", "value": "synthetic-key"}]})
        assert (await db.get_provider(provider))["env"] == []

    run_db(test, monkeypatch, timeout=30)


def test_ttadk_on_path_registers_a_separate_builtin_runtime(monkeypatch, tmp_path: Path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_claude(bin_dir / "ttadk", "print('1.0.27')\n")
    _version_codex(bin_dir / "codex")
    monkeypatch.setattr(registry, "_login_modules", lambda: {})
    monkeypatch.setenv("HOME", str(tmp_path))

    async def test(pool):
        monkeypatch.setenv("PATH", str(tmp_path / "missing"))
        await registry.register_found_runtimes()
        assert await db.get_runtime("rt-ttadk-codex") is None
        monkeypatch.setenv("PATH", str(bin_dir))
        assert await registry.register_found_runtimes() == ["codex", "ttadk-codex"]
        row = await db.get_runtime("rt-ttadk-codex")
        assert (row["name"], row["type"], row["launcher"], row["executable"]) == (
            "ttadk-codex", "codex", "ttadk", "")
        assert row["version"] == "ttadk 1.0.27 / codex-cli 0.162.0"
        assert await registry.register_found_runtimes() == []
        # Re-detect also creates the wrapper row when it has never been registered.
        await pool.execute("DELETE FROM agent_runtimes WHERE id = 'rt-ttadk-codex'")
        answer = await registry.probe_runtime("rt-ttadk-codex")
        assert answer["runtime"]["launcher"] == "ttadk"
        assert answer["runtime"]["auth_kinds"] == ["oauth"]

    run_db(test, monkeypatch, timeout=30)


@pytest.mark.parametrize("api_key", [False, True])
def test_ttadk_profile_write_uses_only_codex_login_provider(monkeypatch, api_key):
    monkeypatch.setattr(registry, "_login_modules", lambda: {})

    async def test(pool):
        await db.insert_builtin_runtime("rt-ttadk-codex", {
            "name": "ttadk-codex", "type": "codex", "launcher": "ttadk", "executable": "",
            "version": "ttadk stand-in", "probe_error": None,
            "capabilities": sorted(c.value for c in runtimes.adapter("codex").capabilities)})
        provider = "prov-codex-login"
        if api_key:
            provider = (await registry.create_provider({"name": "Key account", "vendor_type": "custom",
                "endpoints": [{"protocol": "openai-responses", "base_url": "https://example.invalid/v1"}]}))[
                    "provider"]["provider_id"]
        model = (await registry.create_model({"provider_id": provider, "name": "ttadk-test-model",
                                               "protocol": "openai-responses"}))["model"]["model_id"]
        request = {"name": "TTADK profile", "runtime_id": "rt-ttadk-codex", "model_id": model}
        if api_key:
            with pytest.raises(registry.RegistryError) as failure:
                await registry.create_profile(request)
            assert str(failure.value) == (
                "runtime ttadk-codex launches Codex through TTADK, which replaces the provider with TTADK's gateway: "
                "provider Key account's key would not be used. Use the codex-login provider, or the codex runtime")
            assert await pool.fetchval("SELECT count(*) FROM agent_profiles WHERE name = 'TTADK profile'") == 0
        else:
            answer = await registry.create_profile(request)
            row = await db.get_profile(answer["profile"]["profile_id"])
            profile = registry.AgentProfile.from_row(row)
            assert profile.runtime.launcher is runtimes.Launcher.TTADK
            assert profile.runtime.version == "ttadk stand-in"
            assert row["provider"]["id"] == "prov-codex-login"

    run_db(test, monkeypatch, timeout=30)
