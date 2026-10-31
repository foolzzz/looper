"""Launcher identity survives registry views and portable configuration."""

import asyncio
from dataclasses import replace

import pytest

from looper.agents import config_transfer, profile_registry as registry, runtimes
from looper.agents.runtimes import Launcher


def _row(launcher="native", **changes):
    return {"id": f"rt-{launcher}-codex", "name": "codex-here", "type": "codex", "executable": "",
            "capabilities": ["run"], "launcher": launcher, "version": "stand-in", "builtin": True,
            "probed_at": None, "probe_error": None, "created_at": "now", "updated_at": "now", **changes}


@pytest.mark.parametrize("launcher", list(Launcher))
def test_runtime_launcher_and_version_survive_rows_and_records(launcher):
    row = _row(launcher.value)
    spec = registry.runtime_spec(row)
    assert spec.launcher is launcher
    assert spec.version == "stand-in"
    profile = registry.AgentProfile.from_row({"id": "profile", "name": "profile", "model_id": "model",
        **{f"runtime_{key}": value for key, value in row.items()}})
    assert profile.runtime == spec
    assert profile.record()["runtime"] == {
        "id": row["id"], "name": row["name"], "type": row["type"],
        "launcher": launcher.value,
    }
    view = registry.runtime_view(row)
    assert view["launcher"] == launcher.value
    assert view["default_executable"] == ("ttadk" if launcher is Launcher.TTADK else "codex")
    assert view["auth_kinds"] == (["oauth"] if launcher is Launcher.TTADK else ["api_key", "oauth"])
    assert registry._probe_executable("codex", "", "claude", launcher) == view["default_executable"]
    assert registry._probe_executable("codex", "/bin/custom", "claude", launcher) == "/bin/custom"


def test_old_runtime_rows_default_to_native():
    row = _row()
    del row["launcher"]
    assert registry.runtime_spec(row).launcher is Launcher.NATIVE
    assert config_transfer.runtime_entry(row) == {"name": "codex-here", "type": "codex", "executable": ""}


@pytest.mark.parametrize("destination,source", [("native", "ttadk"), ("ttadk", "native")])
def test_import_rejects_same_named_runtime_with_different_launcher(destination, source):
    row = _row(destination)
    importer = config_transfer._Importer(None, config_transfer._Target(runtimes={row["name"]: row}), False)
    entry = config_transfer.runtime_entry(_row(source))
    assert entry.get("launcher", "native") == source
    asyncio.run(importer.runtime(entry))
    change = importer.plan.changes[-1]
    assert change.action is config_transfer.Action.SKIPPED
    assert change.reason == f"a {destination} runtime has this name here"


@pytest.mark.parametrize("launcher", list(Launcher))
def test_import_resolves_runtime_alias_by_type_and_launcher(launcher):
    rows = {kind.value: _row(kind.value, name=kind.value) for kind in Launcher}
    importer = config_transfer._Importer(None, config_transfer._Target(runtimes=rows), False)
    asyncio.run(importer.runtime({"name": "imported-alias", "type": "codex", "launcher": launcher.value}))
    assert importer.runtimes["imported-alias"][1].launcher is launcher


def test_import_does_not_replace_missing_ttadk_runtime_with_native():
    row = _row()
    importer = config_transfer._Importer(None, config_transfer._Target(runtimes={row["name"]: row}), False)
    asyncio.run(importer.runtime({"name": "ttadk-codex", "type": "codex", "launcher": "ttadk"}))
    assert importer.plan.changes[-1].action is config_transfer.Action.SKIPPED
    assert "ttadk-codex" not in importer.runtimes


def test_ttadk_pairing_refuses_api_key_at_both_validation_boundaries():
    from tests.runtime_model_helpers import resolved_profile
    provider = registry.LegacyProviderSpec("key", "Key account", runtimes.AuthKind.API_KEY,
                                          frozenset({runtimes.ApiProtocol.OPENAI_RESPONSES}))
    runtime = replace(registry.runtime_spec(_row("ttadk")), name="ttadk-codex")
    expected = ("runtime ttadk-codex launches Codex through TTADK, which replaces the provider with TTADK's gateway: "
                "provider Key account's key would not be used. Use the codex-login provider, or the codex runtime")
    with pytest.raises(registry.RegistryError) as failure:
        registry.check_pairing(runtime, provider, "gpt-6.1-sol")
    assert str(failure.value) == expected
    profile = resolved_profile("test", "test", runtime, provider, "gpt-6.1-sol")
    with pytest.raises(registry.RegistryError) as failure:
        profile.resolved_model.check_runtime(runtime)
    assert str(failure.value) == expected
