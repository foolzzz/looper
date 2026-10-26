"""The API runtime's registration, private launch description and process boundary."""

import asyncio
import importlib.metadata
import json
import stat
from dataclasses import replace
from pathlib import Path

import pytest

from looper.agents import profile_registry as registry, runtimes
from looper.agents.agent_settings import AgentSettings, AgentType, Launch
from looper.agents.library import AgentLibrary, ItemKind, LoadedItem
from looper.agents.profile_registry import CatalogModel, LegacyProviderSpec, RuntimeSpec
from looper.agents.runtimes import ApiProtocol, AuthKind, RuntimeUnsupported, SystemPromptMode
from looper.agents.runtimes.api import ApiRuntime, Descriptor, KEY_ENV
from looper.agents.runtimes.api.adapter import RUNTIME_NOTE
from looper.agents.runtimes.api.session import ApiSession
from tests.runtime_model_helpers import resolved_profile


PROVIDER = LegacyProviderSpec("provider-test", "Test account", AuthKind.API_KEY,
                              frozenset({ApiProtocol.OPENAI_RESPONSES}),
                              base_url="http://127.0.0.1:12345/v1", api_key="test-key",
                              catalog=(CatalogModel("test-model", context_window=128000),),
                              env=(("PROVIDER_SETTING", "provider-private-value"),))


@pytest.fixture
def runtime(monkeypatch):
    adapter = ApiRuntime()
    monkeypatch.setitem(runtimes.ADAPTERS, "api", adapter)
    return adapter


def launch_for(runtime, *, mode=SystemPromptMode.APPEND):
    profile = resolved_profile("agent-test", "Test profile",
                               RuntimeSpec("rt-api", "api", "api", capabilities=runtime.capabilities),
                               PROVIDER, "test-model", "high", custom_prompt="Custom instructions")
    return Launch(AgentSettings(profile=profile, system_prompt_mode=mode,
                                env=(("SETTINGS_SETTING", "settings-private-value"),)), role=AgentType.MANAGER)


def test_api_runtime_is_registered():
    assert isinstance(runtimes.ADAPTERS["api"], ApiRuntime)


def test_api_type_view_exposes_manager_only_contract(runtime):
    view = registry.type_view(runtime)
    assert view["roles"] == ["manager"]
    assert view["in_process"] is True
    assert view["protocols"] == ["openai-responses"]
    assert view["auth_kinds"] == ["api_key"]
    assert view["can_interrupt"] is False
    assert view["can_replace_system_prompt"] is True
    registry.check_roles(launch_for(runtime).profile, ["manager"])


@pytest.mark.parametrize("role", ["planner", "worker", "reviewer"])
def test_api_profile_refuses_other_roles(runtime, role):
    with pytest.raises(registry.RegistryError, match="runs only the manager"):
        registry.check_roles(launch_for(runtime).profile, [role])


@pytest.mark.parametrize("runtime_type", ["api", "claude-code", "codex"])
def test_servable_roles_preserves_capabilities_and_applies_runtime_restriction(runtime, runtime_type):
    adapter = runtimes.ADAPTERS[runtime_type]
    assert registry.servable_roles(adapter.capabilities, runtime_type) == (
        ["manager"] if runtime_type == "api" else ["manager", "planner", "worker", "reviewer"])
    # A registered older version's recorded capabilities remain authoritative.
    assert registry.servable_roles([], runtime_type) == []
    assert registry.servable_roles([runtimes.Capability.RUN], runtime_type) == (
        [] if runtime_type == "api" else ["worker", "reviewer"])


@pytest.mark.parametrize("changes,message", [
    ({"auth": AuthKind.OAUTH, "protocols": frozenset(), "catalog": (), "api_key": None},
     "the api runtime takes an API key provider; 'Test account' is a login"),
    ({"protocols": frozenset({ApiProtocol.OPENAI_RESPONSES, ApiProtocol.OPENAI_CHAT}), "catalog": (),
      "api_key": None},
     "the api runtime speaks openai-responses; provider 'Test account' must supply that exact protocol"),
    ({"catalog": (), "api_key": None}, "the api runtime requires one resolved model matching the launch model"),
    ({"catalog": (CatalogModel("other"),)}, "the api runtime requires one resolved model matching the launch model"),
    ({"api_key": None}, "provider 'Test account' has no API key: set one in Settings"),
])
def test_provider_binding_refusals_follow_validation_order(runtime, changes, message):
    with pytest.raises(RuntimeUnsupported) as exc:
        runtime.bind_provider(replace(PROVIDER, **changes), "test-model", "high")
    assert str(exc.value) == message


def test_provider_binding_passes_only_the_key(runtime, monkeypatch):
    binding = runtime.bind_provider(PROVIDER, "test-model", "high")
    assert binding.args == () and binding.env == ((KEY_ENV, "test-key"),)
    monkeypatch.setenv("API_RUNTIME_SENTINEL", "inherited-private-value")
    assert runtime.environment(launch_for(runtime)) == {KEY_ENV: "test-key"}
    assert runtime.launch_args(launch_for(runtime)) == []


def test_api_requires_an_explicit_endpoint(runtime):
    runtime.check_pairing(PROVIDER, "test-model")
    with pytest.raises(RuntimeUnsupported, match="the api runtime needs the endpoint's base_url"):
        runtime.check_pairing(replace(PROVIDER, base_url=""), "test-model")


@pytest.mark.parametrize("effort,window,session", [(None, None, None), ("high", 128000, "session-test")])
def test_descriptor_round_trips(effort, window, session):
    descriptor = Descriptor(ApiProtocol.OPENAI_RESPONSES, "test-model", PROVIDER.base_url,
                            effort, window, "prompt.md", session)
    assert Descriptor.parse(descriptor.to_argv()) == descriptor


@pytest.mark.parametrize("argv", [
    ["codex", "exec", "--model", "test-model"],
    ["looper-api", "--model", "test-model", "--protocol", "openai-responses", "--base-url", "url",
     "--effort", "", "--context-window", "", "--system-prompt-file", "prompt.md"],
    ["looper-api", "--protocol", "unknown", "--model", "test-model", "--base-url", "url",
     "--effort", "", "--context-window", "", "--system-prompt-file", "prompt.md"],
    ["looper-api", "--protocol", "openai-responses", "--model", "test-model", "--base-url", "url",
     "--effort", "", "--context-window", "invalid", "--system-prompt-file", "prompt.md"],
])
def test_descriptor_rejects_noncanonical_commands(argv):
    with pytest.raises(ValueError, match="^not an api runtime command$"):
        Descriptor.parse(argv)


@pytest.mark.parametrize("mode", [SystemPromptMode.APPEND, SystemPromptMode.REPLACE])
def test_resident_description_materializes_preview_text_privately(runtime, tmp_path, mode):
    launch = launch_for(runtime, mode=mode)
    prompt = "Custom instructions\n\nLooper instructions\n"
    source = tmp_path / ".local" / "prompts" / "source.md"
    source.parent.mkdir(parents=True)
    source.write_text(prompt, encoding="utf-8")
    command = runtime.resident_command("", cwd=tmp_path, prompt_file=source, launch=launch)
    descriptor = Descriptor.parse(command)
    assert descriptor.model == "test-model" and descriptor.context_window == 128000
    assert descriptor.effort == "high" and descriptor.base_url == PROVIDER.base_url
    assert "test-key" not in json.dumps(command)
    target = Path(descriptor.system_prompt_file)
    assert target.parent == tmp_path / ".local" / "api" / "prompts"
    assert target.read_text(encoding="utf-8") == runtime.prompt_blocks(launch, prompt)[0].text == (
        "Custom instructions\n\n" + RUNTIME_NOTE + "\n\n---\n\nLooper instructions\n")
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert all(stat.S_IMODE(path.stat().st_mode) == 0o700 for path in (target.parent, target.parent.parent))
    before = target.stat().st_mtime_ns
    assert runtime.resident_command("", cwd=tmp_path, prompt_file=source, launch=launch) == command
    assert target.stat().st_mtime_ns == before
    assert Descriptor.parse(command + runtime.resume_args("session-test")).session == "session-test"


def test_bad_binding_fails_before_prompt_file_access(runtime, tmp_path):
    launch = launch_for(runtime)
    account = replace(launch.profile.resolved_model.provider, api_key=None)
    launch = replace(launch, settings=replace(launch.settings, profile=replace(
        launch.profile, resolved_model=replace(launch.profile.resolved_model, provider=account))))
    for operation in (lambda: runtime.launch_args(launch),
                      lambda: runtime.resident_command("", cwd=tmp_path, prompt_file=tmp_path / "missing",
                                                       launch=launch)):
        with pytest.raises(RuntimeUnsupported, match="has no API key"):
            operation()


@pytest.mark.parametrize("installed", [True, False])
def test_probe_reads_package_metadata_without_importing_sdk(runtime, monkeypatch, installed):
    def version(name):
        assert name == "openai"
        if not installed:
            raise importlib.metadata.PackageNotFoundError(name)
        return "3.37.0"
    monkeypatch.setattr(importlib.metadata, "version", version)
    probe = runtime.probe("unused")
    if installed:
        assert probe == runtimes.Probe("openai 3.37.0", runtime.capabilities)
    else:
        assert probe == runtimes.Probe(None, frozenset(),
                                     "the openai Python package is not installed with this Looper; reinstall Looper")


def test_launch_record_lists_omitted_names_without_values(runtime):
    launch = replace(launch_for(runtime), library=AgentLibrary(items=(
        LoadedItem(ItemKind.MCP, "looper", "builtin"),
        LoadedItem(ItemKind.MCP, "catalog-server", "hash"),
        LoadedItem(ItemKind.SKILL, "test-skill", "hash"),
        LoadedItem(ItemKind.PLUGIN, "test-plugin", "hash"))))
    record = runtime.launch_record(launch)
    assert record == {
        "api": {"protocol": "openai-responses", "base_url_host": "127.0.0.1"},
        "omitted": [{"kind": kind, "name": name,
                     "reason": "the api runtime loads no plugins, skills or catalog MCP servers"}
                    for kind, name in (("mcp", "catalog-server"), ("skill", "test-skill"), ("plugin", "test-plugin"))],
        "provider_env_ignored": ["PROVIDER_SETTING"], "settings_env_ignored": ["SETTINGS_SETTING"],
    }
    for value in ("test-key", "provider-private-value", "settings-private-value"):
        assert value not in json.dumps(record)


def test_api_runtime_cannot_start_or_drive_a_process(runtime, tmp_path):
    launch = launch_for(runtime)
    for operation in (lambda: runtime.run_command("looper-api", cwd=tmp_path, prompt_file=tmp_path / "prompt",
                                                  launch=launch), lambda: runtime.parse(None),
                      lambda: asyncio.run(runtime.write_turn(None, "hello"))):
        with pytest.raises(RuntimeUnsupported, match="inside looper serve; it has no process"):
            operation()
    assert isinstance(runtime.resident_session("Manager"), ApiSession)
    assert runtime.instruction_files(tmp_path) == ()
