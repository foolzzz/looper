"""TTADK runtimes register independently and enforce provider pairing on writes."""

from pathlib import Path

import pytest

from fake_claude import fake_claude
from looper.agents import profile_registry as registry, runtimes
from looper.db import profile_registry as db
from tests.db_support import DSN, run_db
from tests.test_profile_registry_db import _version_codex

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")


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
