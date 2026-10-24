"""DSH policy, headless launch and stream lifecycle through real subprocesses."""

import asyncio
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import pytest

from looper.agents import runtimes
from looper.agents.profile_registry import (
    CatalogModel, LegacyProviderSpec as ProviderSpec, RegistryError, RuntimeSpec, check_pairing,
)
from tests.runtime_model_helpers import resolved_profile
from looper.agents.runtimes import ApiProtocol, AuthKind, RuntimeUnsupported
from tests.test_dsh_installed import installation as installed_dsh


MODELS = ("deepseek-flash", "deepseek-flash[1m]", "deepseek-v4-pro", "deepseek-v4-pro[1m]")
PROVIDER = ProviderSpec("gateway", "Messages gateway", AuthKind.API_KEY,
                        frozenset({ApiProtocol.ANTHROPIC_MESSAGES}),
                        base_url="https://gateway.example/anthropic/v1/", api_key="test-dsh-key")


@pytest.mark.parametrize("role", ["manager", "worker-pool"])
def test_dsh_only_server_health_tracks_the_listener_not_claude(tmp_path, monkeypatch, role):
    """A supervisor health tick must not cancel dsh work because Claude is absent."""
    from looper.agents.bus import MessageBus
    from looper.agents.supervisor import AgentSupervisor
    from looper.agents.tiers.manager import ManagerAgent, ManagerConfig
    from looper.agents.tiers.worker import WorkerConfig, WorkerPool

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "dsh").write_text("#!/bin/sh\nexit 0\n")
    (bin_dir / "dsh").chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir))
    assert shutil.which("dsh") and shutil.which("claude") is None

    async def run():
        bus = MessageBus(tmp_path)
        if role == "manager":
            agent = ManagerAgent(config=ManagerConfig(work_dir=tmp_path), runtime_root=tmp_path)

            async def no_database():
                pass

            monkeypatch.setattr(agent, "_load_db_config", no_database)
            monkeypatch.setattr(agent, "_load_session_state", no_database)
        else:
            agent = WorkerPool(config=WorkerConfig(), runtime_root=tmp_path)
        supervisor = AgentSupervisor(bus, tmp_path)
        supervisor.register(agent)
        assert not agent.is_healthy()
        await supervisor.start_all()
        try:
            await asyncio.sleep(0)  # the real listener reaches its bus subscription
            await supervisor.check_health()
            status = supervisor.agent_status()[role]
            assert status["state"] == "running", status
            assert status["healthy"] and status["restart_count"] == 0
            listener = agent._task if role == "manager" else agent._listener_task
            listener.cancel()
            await asyncio.gather(listener, return_exceptions=True)
            assert not agent.is_healthy()  # a dead listener still requires recovery
        finally:
            await supervisor.stop_all()
        assert not agent.is_healthy()

    asyncio.run(run())


def test_registry_exposes_exact_dsh_models_and_replace_refusal():
    from looper.agents.profile_registry import type_view
    from looper.agents.runtimes.dsh import MODEL_MAP

    view = type_view(runtimes.adapter("dsh"))
    assert view["supported_models"] == sorted(MODEL_MAP)
    assert set(view["supported_models"]) == set(MODELS)
    assert view["auth_kinds"] == ["api_key"]
    assert view["protocols"] == ["anthropic-messages"]
    assert view["can_replace_system_prompt"] is False
    assert view["replace_system_prompt_refusal"] == runtimes.replace_refusal(runtimes.adapter("dsh"))
    for name, adapter in runtimes.ADAPTERS.items():
        if name != "dsh":
            assert type_view(adapter)["supported_models"] is None


def _register_prompt_runtime(monkeypatch):
    """Use the executable adapter's prompt hooks in the existing preview checks."""
    from looper.agents.runtimes import dsh

    runtime = dsh.DshRuntime()
    monkeypatch.setitem(runtimes.ADAPTERS, "dsh", runtime)
    return runtime


def _prompt_launch(monkeypatch, custom=""):
    from looper.agents.agent_settings import AgentSettings, AgentType, Launch

    runtime = _register_prompt_runtime(monkeypatch)
    profile = resolved_profile("dsh-prompt", "DSH prompt", RuntimeSpec("dsh", "DSH", runtime.type),
                               PROVIDER, "deepseek-flash", custom_prompt=custom)
    return Launch(AgentSettings(profile=profile), role=AgentType.WORKER)


def _node(tmp_path, script, *args):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required for DSH bootstrap validation")
    return subprocess.run([node, "--input-type=module", "-", *map(str, args)], input=script,
                          cwd=tmp_path, capture_output=True, text=True, timeout=60)


@pytest.fixture
def prompt_only_mcp(monkeypatch):
    """Keep prompt unit checks independent of the mandatory bridge (covered with real processes below)."""
    from looper.agents.runtimes import dsh

    monkeypatch.setattr(dsh, "mcp_config", lambda *args, **kwargs: None)


@pytest.mark.parametrize("custom", ["", 'Profile {{unknown}} 中文 "identity"\nsecond line'])
def test_dsh_bootstrap_preserves_literal_prompt_and_delegates_after_registration(
    tmp_path, monkeypatch, custom, prompt_only_mcp,
):
    from looper.agents.runtimes import dsh

    # An exports-only package at the detected executable's installation anchor.
    package = tmp_path / "installation" / "node_modules" / "@deepseek-ai" / "dsh-headless"
    package.mkdir(parents=True)
    (package / "package.json").write_text(json.dumps({"type": "module", "exports": "./runner.mjs"}))
    (package / "runner.mjs").write_text('''
export const name = 'headless-runner';
export const Config = { runnerSchema: true };
export const inject = ['agentDefaultModel', 'agents', 'sessions'];
export function apply(ctx, config) {
  if (ctx.sections.length !== 1) throw new Error('runner started before section registration');
  if (config !== ctx.expectedConfig) throw new Error('runner config was changed');
  ctx.started++;
  return ctx.result;
}
''')
    executable = tmp_path / "installation" / "bin" / "dsh"
    executable.parent.mkdir()
    executable.touch()
    symlink = tmp_path / "detected-dsh"
    symlink.symlink_to(executable)
    launch = _prompt_launch(monkeypatch, custom)
    body = '  {{unknown}} "quoted" \'single\' \\ path\n中文🙂\n`literal ${value}`\r\nlast line\n'
    text = custom + "\n\n" + body if custom else body
    home = dsh.write_home(tmp_path / "local", dsh.bootstrap_files(str(symlink), text, launch))
    entry = dsh.bootstrap_entry(home)
    assert entry["id"] == "headless-runner"  # required entry, never a separate optional plugin
    assert set(entry) == {"id", "name"}  # preserve the official config and headlessStartup injection
    done = _node(tmp_path, '''
import assert from 'node:assert/strict';
const plugin = await import(process.argv[2]);
assert.equal(plugin.name, 'headless-runner');
assert.deepEqual(plugin.Config, {runnerSchema: true});
assert.deepEqual(plugin.inject, ['agentDefaultModel', 'agents', 'sessions', 'systemPrompt']);
const ctx = { sections: [], started: 0, expectedConfig: {task: '-', json: true, sessionId: 'saved'},
              result: Promise.resolve('delegated') };
ctx.systemPrompt = {section(section) { ctx.sections.push(section); }};
assert.equal(plugin.apply(ctx, ctx.expectedConfig), ctx.result);
assert.equal(ctx.started, 1);
assert.equal(ctx.sections[0].name, 'looper');
assert.equal(ctx.sections[0].order, 20000);
assert.equal(ctx.sections[0].interpolate, false);
assert.equal(ctx.sections[0].complete, undefined);
const broken = {...ctx, started: 0, systemPrompt: {section() {throw new Error('registration failed');}}};
assert.throws(() => plugin.apply(broken, ctx.expectedConfig), /registration failed/);
assert.equal(broken.started, 0);
console.log(JSON.stringify(ctx.sections[0]));
''', entry["name"])
    assert done.returncode == 0, done.stderr
    section = json.loads(done.stdout)
    assert section["text"] == dsh._prompt_text(text, launch)
    assert section["text"].endswith(body)
    if custom:
        assert section["text"].startswith(custom + "\n\n# Runtime notes")
        assert section["text"].count(custom) == 1
    assert not (home.root / "node_modules").exists()


def test_dsh_bootstrap_rejects_unavailable_installation(tmp_path, monkeypatch):
    from looper.agents.runtimes import dsh

    launch = _prompt_launch(monkeypatch)
    for executable in (tmp_path / "missing-dsh", tmp_path):
        with pytest.raises(RuntimeUnsupported, match="installed executable"):
            dsh.bootstrap_files(str(executable), "literal prompt", launch)


def test_dsh_bootstrap_rejects_incompatible_official_exports(tmp_path, monkeypatch, prompt_only_mcp):
    from looper.agents.runtimes import dsh

    package = tmp_path / "node_modules" / "@deepseek-ai" / "dsh-headless"
    package.mkdir(parents=True)
    (package / "package.json").write_text('{"type":"module","exports":"./index.mjs"}')
    (package / "index.mjs").write_text("export const name='headless-runner', Config={}, inject={};")
    executable = tmp_path / "dsh"
    executable.touch()
    home = dsh.write_home(tmp_path / "local", dsh.bootstrap_files(str(executable), "prompt",
                                                                  _prompt_launch(monkeypatch)))
    done = _node(tmp_path, '''
import assert from 'node:assert/strict';
await assert.rejects(import(process.argv[2]), /Unsupported DSH headless exports/);
''', dsh.bootstrap_entry(home)["name"])
    assert done.returncode == 0, done.stderr


@pytest.mark.installed
def test_dsh_bootstrap_imports_official_exports_and_renders_literal_text(tmp_path, monkeypatch, prompt_only_mcp):
    from looper.agents.runtimes import dsh

    executable = shutil.which("dsh")
    if executable is None:
        pytest.skip("Installed DSH is required to verify official exports")
    launch = _prompt_launch(monkeypatch)
    home = dsh.write_home(tmp_path / "local", dsh.bootstrap_files(executable, '{{unknown}}\n中文 "quote"', launch))
    done = _node(tmp_path, '''
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
import {realpathSync} from 'node:fs';
import {pathToFileURL} from 'node:url';
const require = createRequire(realpathSync(process.argv[3]));
const official = await import(pathToFileURL(require.resolve('@deepseek-ai/dsh-headless')));
assert.equal(official.name, 'headless-runner');
assert.equal(typeof official.Config, 'function');
assert.equal(typeof official.apply, 'function');
assert.ok(Array.isArray(official.inject));
const plugin = await import(process.argv[2]);
assert.equal(plugin.Config, official.Config);
assert.deepEqual(plugin.inject, [...official.inject, 'systemPrompt']);
let section;
// The real apply fails before reading stdin or starting a model when appExit is absent.
assert.throws(() => plugin.apply({systemPrompt: {section(value) {section = value;}}, get() {}}, {}), /appExit/);
assert.equal(section.interpolate, false);
const {renderPrompt} = await import(pathToFileURL(require.resolve('@deepseek-ai/dsh-system-prompt')));
const assembly = {sections: [section], contexts: [], tools: [], variables: {}};
assert.equal(renderPrompt(assembly), section.text);
assert.throws(() => renderPrompt({...assembly, sections: [{...section, interpolate: true}]}), /unknown/);
console.log(JSON.stringify(section));
''', dsh.bootstrap_entry(home)["name"], executable)
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout)["text"] == dsh._prompt_text('{{unknown}}\n中文 "quote"', launch)


@pytest.fixture
def dsh_policy(monkeypatch: pytest.MonkeyPatch):
    """Only tests expose this policy as an adapter, exercising the real registry gates."""
    from looper.agents.runtimes import dsh
    from looper.agents.runtimes.claude_code import ClaudeCodeRuntime

    class PolicyRuntime(ClaudeCodeRuntime):
        type = "dsh-policy-test"
        label = "DSH policy test"
        auth_kinds = frozenset({AuthKind.API_KEY})

        def check_pairing(self, provider, model, effort=None):
            dsh.validate_provider(provider, model, effort)

        def bind_provider(self, provider, model, effort):
            return dsh.bind_provider(provider, model, effort)

    adapter = PolicyRuntime()
    monkeypatch.setitem(runtimes.ADAPTERS, adapter.type, adapter)
    return adapter


# The model and the effort are checked apart: every model and every effort once, none of them crossed.
@pytest.mark.parametrize("model,effort", [
    (MODELS[0], None), (MODELS[1], ""), (MODELS[2], "low"), (MODELS[3], "high"), (MODELS[0], "max"),
])
def test_dsh_pairing_and_binding_accept_exact_models_without_reading_a_key(dsh_policy, model, effort):
    from looper.agents.runtimes.dsh import validate_provider

    provider = replace(PROVIDER, catalog=(CatalogModel(model),), api_key=None)
    check_pairing(RuntimeSpec("test", "test", dsh_policy.type), provider, model, effort)
    policy = validate_provider(provider, model, effort)
    assert policy.route == "deepseek-official"
    assert policy.model == {"deepseek-flash": "deepseek-flash", "deepseek-flash[1m]": "deepseek-flash",
                            "deepseek-v4-pro": "deepseek-v4-pro",
                            "deepseek-v4-pro[1m]": "deepseek-v4-pro"}[model]
    assert policy.effort == (effort or "high")
    assert policy.base_url == provider.base_url
    binding = dsh_policy.bind_provider(replace(provider, api_key=PROVIDER.api_key), model, effort)
    assert dict(binding.env)["DEEPSEEK_API_KEY"] == PROVIDER.api_key
    assert PROVIDER.api_key not in repr(binding)


@pytest.mark.parametrize(("changes", "model", "effort"), [
    ({"auth": AuthKind.OAUTH, "oauth_runtime_type": "dsh-policy-test"}, "deepseek-flash", None),
    ({"protocols": frozenset({ApiProtocol.OPENAI_CHAT})}, "deepseek-flash", None),
    ({"protocols": frozenset({ApiProtocol.OPENAI_RESPONSES})}, "deepseek-flash", None),
    ({}, "gpt-6.1-sol", None),
    ({}, "deepseek-chat", None),
    ({}, "deepseek-flash-20261020", None),
    ({}, "deepseek-flash[1m][1m]", None),
    ({}, "DeepSeek-flash", None),
    ({}, "deepseek-flash", "off"),
    ({}, "deepseek-flash", "medium"),
    ({}, "deepseek-flash", "xhigh"),
    ({"base_url": "ftp://gateway.example"}, "deepseek-flash", None),
    ({"base_url": "gateway.example/anthropic"}, "deepseek-flash", None),
    ({"base_url": "https://"}, "deepseek-flash", None),
    ({"base_url": "https://gateway.example/\nmessages"}, "deepseek-flash", None),
    ({"catalog": (CatalogModel("deepseek-flash[1m]"),)}, "deepseek-flash", None),
])
def test_dsh_rejections_reach_registry_and_launch(dsh_policy, changes, model, effort):
    from looper.agents.runtimes.dsh import validate_provider

    provider = replace(PROVIDER, **changes)
    with pytest.raises(RegistryError):
        check_pairing(RuntimeSpec("test", "test", dsh_policy.type), provider, model, effort)
    with pytest.raises(RuntimeUnsupported):
        validate_provider(provider, model, effort)
    with pytest.raises(RuntimeUnsupported):
        dsh_policy.bind_provider(provider, model, effort)


@pytest.mark.parametrize("key", [None, "", "   "])
def test_dsh_missing_key_is_refused_only_at_launch(dsh_policy, key, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "must-not-inherit")
    provider = replace(PROVIDER, api_key=key)
    check_pairing(RuntimeSpec("test", "test", dsh_policy.type), provider, "deepseek-flash")
    with pytest.raises(RuntimeUnsupported, match="API key"):
        dsh_policy.bind_provider(provider, "deepseek-flash", "")


@pytest.mark.parametrize("base_url", ["", "https://api.deepseek.com/anthropic",
                                     "https://gateway.example/custom/v1", "http://localhost:9999/messages/",
                                     "https://gateway.example/v1/chat/completions"])
def test_dsh_keeps_the_explicit_endpoint_without_rewriting(base_url):
    from looper.agents.runtimes.dsh import validate_provider

    policy = validate_provider(replace(PROVIDER, base_url=base_url), "deepseek-flash")
    assert policy.base_url == (base_url or "https://api.deepseek.com/anthropic")


def test_dsh_uses_protocol_and_model_instead_of_vendor_heuristics(dsh_policy):
    provider = replace(PROVIDER, id="other-vendor", name="Other vendor", base_url="https://other.example/api",
                       protocols=frozenset({ApiProtocol.ANTHROPIC_MESSAGES, ApiProtocol.OPENAI_CHAT}))
    check_pairing(RuntimeSpec("test", "test", dsh_policy.type), provider, "deepseek-v4-pro[1m]")


def test_dsh_registration_does_not_change_other_pairings():
    assert runtimes.adapter("dsh").type == "dsh"
    # A default hook must leave existing runtime/model/effort combinations alone.
    check_pairing(RuntimeSpec("claude", "claude", "claude-code"), PROVIDER, "other-model", "xhigh")


def test_deepseek_messages_preset_still_pairs_without_changing_its_catalog(dsh_policy):
    from looper.agents.provider_presets import PRESETS

    preset = next(p for p in PRESETS if p.id == "deepseek")
    api = next(a for a in preset.apis if ApiProtocol.ANTHROPIC_MESSAGES in a.protocols)
    provider = replace(PROVIDER, base_url=api.base_url, protocols=frozenset(api.protocols),
                       catalog=tuple(CatalogModel(m.name) for m in preset.models), api_key=None)
    for model in provider.models:
        check_pairing(RuntimeSpec("test", "test", dsh_policy.type), provider, model)
    with pytest.raises(RegistryError):
        check_pairing(RuntimeSpec("test", "test", dsh_policy.type), provider, "deepseek-flash")


def test_dsh_runs_have_distinct_owners_and_sessions_survive_configuration_changes(tmp_path):
    from looper.agents.runtimes.dsh import write_home

    local = tmp_path / ".local"
    files = {"looper.yml": '{"prompt":"first","tokenEnv":"RUN_TOKEN"}\n'}
    first, second = [write_home(local, files) for _ in range(2)]
    assert first.root != second.root
    label = "planner/" + "long-label-" * 30
    old = write_home(local, files, owner=label)
    assert old.root == local / "dsh" / "owners" / hashlib.sha256(label.encode()).hexdigest()
    session = Path(old.paths["DSH_HOME"]) / "sessions" / "saved.jsonl"
    session.parent.mkdir()
    session.write_text("saved session")
    new = write_home(local, {"looper.yml": '{"prompt":"next","tokenEnv":"NEXT_TOKEN"}\n'}, owner=label)
    assert new.root == old.root and new.paths == old.paths
    assert new.config != old.config
    assert session.read_text() == "saved session"
    assert write_home(local, files, owner=label).config == old.config
    assert write_home(local, files, owner=label + "different").root != old.root


def test_dsh_generated_directories_and_files_are_private(tmp_path):
    from looper.agents.runtimes.dsh import write_home

    local = tmp_path / ".local"
    home = write_home(local, {"looper.yml": "{}\n", "modules/bootstrap.mjs": "export default {};\n"})
    assert set(home.paths) == {"HOME", "DSH_HOME", "DSH_AGENTS_HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME",
                               "XDG_STATE_HOME", "XDG_CACHE_HOME", "XDG_RUNTIME_DIR", "XDG_CONFIG_DIRS",
                               "XDG_DATA_DIRS"}
    for value in home.paths.values():
        path = Path(value)
        assert path.is_dir() and path.is_relative_to(home.root)
    for path in [local / "dsh", *list((local / "dsh").rglob("*"))]:
        assert stat.S_IMODE(path.stat().st_mode) == (0o700 if path.is_dir() else 0o600), path


def test_dsh_concurrent_materialization_publishes_only_complete_bundles(tmp_path, monkeypatch):
    from looper.agents.runtimes import dsh

    files = {"looper.yml": json.dumps({"value": "x" * 100_000}), "bootstrap.mjs": "// complete module\n"}
    entered, release = threading.Event(), threading.Event()
    original = dsh._write_private

    def paused_write(path, text):
        original(path, text)
        if path.name == "looper.yml":
            entered.set()
            assert release.wait(5)

    monkeypatch.setattr(dsh, "_write_private", paused_write)
    with ThreadPoolExecutor(max_workers=8) as pool:
        writes = [pool.submit(dsh.write_home, tmp_path / ".local", files, owner="shared") for _ in range(8)]
        try:
            assert entered.wait(5)
            configs = tmp_path / ".local" / "dsh" / "owners" / hashlib.sha256(b"shared").hexdigest() / "configs"
            assert not [p for p in configs.iterdir() if not p.name.startswith(".")]
        finally:
            release.set()
        homes = [future.result(timeout=10) for future in writes]
    assert len({home.config for home in homes}) == 1
    assert {p.name for p in homes[0].config.parent.iterdir()} == {homes[0].config.name}
    assert {str(p.relative_to(homes[0].config)): p.read_text() for p in homes[0].config.rglob("*")
            if p.is_file()} == files


def test_dsh_failed_materialization_removes_partial_files_and_redacts_errors(tmp_path, monkeypatch):
    from looper.agents.runtimes import dsh

    def failed_replace(*args):
        raise OSError("synthetic filesystem failure")

    monkeypatch.setattr(dsh.os, "replace", failed_replace)
    with pytest.raises(OSError) as failure:
        dsh.write_home(tmp_path / ".local", {"looper.yml": "synthetic-private-content"}, owner="failure")
    assert "synthetic-private-content" not in repr(failure.value)
    assert not list((tmp_path / ".local").rglob("*.yml"))
    assert not list((tmp_path / ".local").rglob(".building.*"))


@pytest.mark.parametrize("name", ["/outside.yml", "../outside.yml", "modules/../../outside.yml"])
def test_dsh_configuration_files_cannot_escape_the_bundle(tmp_path, name):
    from looper.agents.runtimes.dsh import write_home

    with pytest.raises(ValueError):
        write_home(tmp_path / ".local", {name: "{}"})
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("relative", ["home/.local", "configs"])
def test_dsh_rejects_directory_links_without_touching_their_targets(tmp_path, relative):
    from looper.agents.runtimes.dsh import write_home

    local = tmp_path / ".local"
    home = write_home(local, {"looper.yml": "{}\n"}, owner="linked")
    operator = tmp_path / "operator"
    operator.mkdir()
    (operator / "marker").write_text("operator-only")
    before = operator.stat().st_mode
    linked = home.root / relative
    shutil.rmtree(linked)
    linked.symlink_to(operator, target_is_directory=True)
    with pytest.raises(RuntimeUnsupported, match="symlink"):
        write_home(local, {"looper.yml": "{\"next\":true}\n"}, owner="linked")
    assert operator.stat().st_mode == before
    assert list(operator.iterdir()) == [operator / "marker"]
    assert (operator / "marker").read_text() == "operator-only"


@pytest.mark.parametrize("role", ["manager", "planner", "worker", "reviewer"])
def test_dsh_mcp_translation_keeps_stdio_and_secrets_in_child_environment(tmp_path, monkeypatch, role):
    from looper.agents.library import AgentLibrary
    from looper.agents.ops.service import server_config
    from looper.agents.runtimes import dsh
    from looper.agents.agent_settings import AgentType

    source = tmp_path / "catalog.json"
    source.write_text(json.dumps({"mcpServers": {
        "looper": server_config("http://127.0.0.1:19999", "fresh-run-token"),
        "catalog": {"type": "http", "url": "http://127.0.0.1:19998/mcp",
                    "headers": {"Authorization": "Bearer catalog-secret"}},
    }}))
    library = AgentLibrary(mcp_config=str(source), role=role)
    launch = replace(_prompt_launch(monkeypatch), library=library, role=AgentType(role))
    translated = dsh.mcp_config(library)
    own, catalog = translated.servers
    assert own["serverName"] == "looper" and own["transport"] == "stdio"
    assert own["command"] == server_config("", "")["command"]
    assert own["args"] == ["-m", "looper.cli", "mcp-server"]
    assert catalog["transport"] == "streamable-http"
    executable = tmp_path / "dsh"
    executable.touch()
    files = dsh.bootstrap_files(str(executable), "prompt", launch)
    home = dsh.write_home(tmp_path / "local", files)
    env = dsh.environment(launch, home)
    assert env[own["envRefs"]["LOOPER_AGENT_URL"]] == "http://127.0.0.1:19999"
    assert env[own["envRefs"]["LOOPER_AGENT_TOKEN"]] == "fresh-run-token"
    if launch.read_only:
        assert catalog["headerRefs"]["Authorization"] not in env
    else:
        assert env[catalog["headerRefs"]["Authorization"]] == "Bearer catalog-secret"
    assert all("SECRET" in reference for reference in translated.env)
    for secret in ("fresh-run-token", "catalog-secret"):
        assert secret not in repr(translated)
        assert all(secret not in body for body in files.values())
    source.write_text(source.read_text().replace("fresh-run-token", "rotated-run-token"))
    updated = dsh.environment(launch, home)
    assert updated[own["envRefs"]["LOOPER_AGENT_TOKEN"]] == "rotated-run-token"
    assert "fresh-run-token" not in updated.values()


@pytest.mark.parametrize("role", ["manager", "planner"])
def test_dsh_mcp_requires_platform_bridge_even_without_catalog(tmp_path, monkeypatch, role):
    from looper.agents.library import AgentLibrary
    from looper.agents.runtimes import dsh

    with pytest.raises(RuntimeUnsupported, match="Looper stdio bridge"):
        dsh.mcp_config(AgentLibrary(role=role))


@pytest.mark.parametrize("role", [None, "worker", "reviewer"])
@pytest.mark.parametrize("catalog", [False, True])
def test_dsh_mcp_allows_optional_platform_bridge(tmp_path, role, catalog):
    from looper.agents.library import AgentLibrary
    from looper.agents.runtimes import dsh

    source = tmp_path / "catalog.json"
    source.write_text(json.dumps({"mcpServers": {"catalog": {"command": "catalog-mcp"}}}))
    translated = dsh.mcp_config(AgentLibrary(role=role, mcp_config=str(source) if catalog else ""))
    assert [server["serverName"] for server in translated.servers] == (["catalog"] if catalog else [])
    assert not translated.env


@pytest.mark.parametrize("raw", [
    '{"mcpServers":{"looper":{},"looper":{}}}',
    '{"mcpServers":{"looper":{"url":"http://127.0.0.1:19999"}}}',
    '{"mcpServers":{"looper":{"command":"python","env":{"LOOPER_AGENT_URL":"http://127.0.0.1:19999"}}}}',
    '{"mcpServers":{"catalog":{"command":"python"}}}',
])
def test_dsh_mcp_rejects_duplicate_or_missing_platform_bridge(tmp_path, monkeypatch, raw):
    from looper.agents.library import AgentLibrary
    from looper.agents.runtimes import dsh

    monkeypatch.setenv("LOOPER_AGENT_TOKEN", "ambient-token-must-not-fill-a-missing-run-token")
    source = tmp_path / "catalog.json"
    source.write_text(raw)
    with pytest.raises(RuntimeUnsupported):
        dsh.mcp_config(AgentLibrary(mcp_config=str(source), role="planner"))


DSH_JSON = Path(__file__).parent / "fixtures" / "dsh_json"


async def _dsh_output(tmp_path, payload, *, exit_code=0, verify_exit=True, cancelled=False):
    """Replay synthetic bytes through a real pipe, with a small reader limit to exercise long finals."""
    from looper.agents.runtimes.dsh import DshStream

    source = tmp_path / "synthetic.jsonl"
    source.write_bytes(payload)
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-I", "-c",
        "import os,pathlib,sys; sys.stdout.buffer.write(pathlib.Path(sys.argv[1]).read_bytes()); "
        "sys.stdout.buffer.flush(); code=int(sys.argv[2]); "
        "os.kill(os.getpid(), -code) if code < 0 else sys.exit(code)",
        str(source), str(exit_code), stdout=asyncio.subprocess.PIPE, cwd=tmp_path,
        env={"HOME": str(tmp_path)}, limit=1024,
    )
    stream = DshStream(model="deepseek-flash")
    if cancelled:
        stream.cancel()
    try:
        events = [event async for event in stream.events(process.stdout, process=process if verify_exit else None)]
        await process.wait()
        return events
    finally:
        if process.returncode is None:
            process.kill()
        await process.wait()


def _dsh_replay(tmp_path, fixture, **kwargs):
    return asyncio.run(asyncio.wait_for(_dsh_output(tmp_path, (DSH_JSON / fixture).read_bytes(), **kwargs), 5))


def _dsh_result(events):
    results = [event for event in events if event["type"] == "result"]
    assert len(results) == 1
    assert events[-1] == results[0]
    return results[0]


DSH_STEP_USAGE = {"inputTokens": 10, "outputTokens": 4, "cacheReadTokens": 7, "cacheWriteTokens": 2,
                  "reasoningTokens": 3, "totalTokens": 23}


def _dsh_usage_run(tmp_path, *steps):
    records = [{"type": "session", "sessionId": "usage-session"},
               {"type": "status", "phase": "turn_start", "turn": 1}, *steps,
               {"type": "status", "phase": "turn_end", "turn": 1, "reason": {"kind": "completed"}},
               {"type": "final", "text": "done"}]
    payload = "".join(json.dumps(record) + "\n" for record in records).encode()
    return asyncio.run(asyncio.wait_for(_dsh_output(tmp_path, payload), 5))


def _dsh_step(step=1, usage=DSH_STEP_USAGE, **extra):
    return {"type": "status", "phase": "step_end", "turn": 1, "step": step, "usage": usage, **extra}


def test_dsh_step_usage_prices_disjoint_buckets_and_keeps_auxiliary_total_unknown(tmp_path):
    from looper.agents.run_usage import Usage, run_usage
    from looper.agents.runtimes.pricing import RunSpend, pricer

    events = _dsh_usage_run(tmp_path, _dsh_step())
    increments = [event for event in events if event["type"] == "usage"]
    assert len(increments) == 1
    event = increments[0]
    tokens = Usage(input_tokens=10, output_tokens=4, cache_read_tokens=7, cache_creation_tokens=2)
    assert Usage.from_dict(event["usage"]) == tokens  # input is uncached; reasoning is already in output
    assert event["raw_usage"] == DSH_STEP_USAGE
    assert event["root_usage_complete"] is True
    assert event["usage_complete"] is False and event["cost_basis"] == "observed"
    spend = RunSpend(PROVIDER, "deepseek-flash", cap=0.000008)
    crossings = [spend.add(item) for item in events]
    # (10 * .30 + 4 * 1.20 + 7 * .006 + 2 * .30) / 1M = .000008442.
    assert spend.observed_cost_usd == pytest.approx(0.000008442)
    assert spend.usage == tokens and spend.usage.cost_usd is None
    assert crossings.count(True) == 1 and spend.exceeded and spend.budget_verifiable
    result = _dsh_result(events)
    assert result["root_usage_complete"] is True and result["usage_complete"] is False
    assert run_usage(result, price=pricer(PROVIDER)) == tokens  # no result repricing as a complete total


@pytest.mark.parametrize("turn,step", [(1, 2), (2, 1)])
def test_dsh_duplicate_steps_retry_totals_and_resumed_process_do_not_double_count(tmp_path, turn, step):
    from looper.agents.run_usage import Usage, run_usage
    from looper.agents.runtimes.pricing import RunSpend, pricer

    first = _dsh_step(attempts=[{"usage": DSH_STEP_USAGE}, {"usage": DSH_STEP_USAGE}])
    events = _dsh_usage_run(tmp_path, first, {"type": "text", "text": "progress"}, first, _dsh_step(step, turn=turn))
    increments = [event for event in events if event["type"] == "usage"]
    assert len(increments) == 2  # step total already includes retries; final and text add nothing
    spend = RunSpend(PROVIDER, "deepseek-flash", cap=None)
    for event in events:
        spend.add(event)
    expected = Usage(input_tokens=20, output_tokens=8, cache_read_tokens=14, cache_creation_tokens=4)
    assert spend.usage == expected
    assert spend.observed_cost_usd == pytest.approx(0.000016884)
    assert run_usage(_dsh_result(events), price=pricer(PROVIDER)) == expected
    # The same persisted session in a new process counts only the new step, even if its indices repeat.
    resumed = _dsh_usage_run(tmp_path, _dsh_step())
    assert run_usage(_dsh_result(resumed), price=pricer(PROVIDER)).input_tokens == 10


# One value per way a step's usage is incomplete: not a mapping, no counters, a counter missing, truncated, and a counter
# that is a bool, negative or not an integer (what ``_count`` refuses), one per bucket; the optional counters (total,
# reasoning) that are invalid make the step incomplete too.
@pytest.mark.parametrize("raw_usage,truncated,missing", [
    (None, False, "input_tokens"),
    ({}, False, "output_tokens"),
    ({key: value for key, value in DSH_STEP_USAGE.items() if key != "cacheWriteTokens"}, False,
     "cache_creation_tokens"),
    (DSH_STEP_USAGE, True, None),
    ({**DSH_STEP_USAGE, "inputTokens": True}, False, "input_tokens"),
    ({**DSH_STEP_USAGE, "outputTokens": -1}, False, "output_tokens"),
    ({**DSH_STEP_USAGE, "cacheReadTokens": "10"}, False, "cache_read_tokens"),
    ({**DSH_STEP_USAGE, "totalTokens": -1}, False, None),
    ({**DSH_STEP_USAGE, "reasoningTokens": "10"}, False, None),
])
def test_dsh_incomplete_step_usage_stays_unknown_with_known_buckets_readable(
    tmp_path, raw_usage, truncated, missing,
):
    from looper.agents.run_usage import run_usage
    from looper.agents.runtimes.pricing import RunSpend, pricer

    events = _dsh_usage_run(tmp_path, _dsh_step(usage=raw_usage, truncated=truncated))
    increments = [event for event in events if event["type"] == "usage"]
    assert len(increments) == 1
    event = increments[0]
    assert event["raw_usage"] == raw_usage
    assert event["root_usage_complete"] is False and event["usage_complete"] is False
    spend = RunSpend(PROVIDER, "deepseek-flash", cap=1)
    for item in events:
        assert spend.add(item) is False
    assert spend.usage.cost_usd is None and spend.observed_cost_usd is None
    assert spend.budget_verifiable is False
    result = _dsh_result(events)
    assert result["root_usage_complete"] is False and result["usage_complete"] is False
    own = run_usage(result, price=pricer(PROVIDER))
    assert own.cost_usd is None
    if missing:
        assert getattr(own, missing) is None
    if isinstance(raw_usage, dict) and raw_usage.get("cacheWriteTokens") == 2:
        assert own.cache_creation_tokens == 2


@pytest.mark.parametrize("steps", [(), ({"type": "status", "phase": "step_start", "turn": 1, "step": 2},),
                                   ({"type": "status", "phase": "step_end", "turn": 1, "step": 1},)])
def test_dsh_no_reported_step_cannot_become_zero_dollars(tmp_path, steps):
    from looper.agents.runtimes.pricing import RunSpend

    events = _dsh_usage_run(tmp_path, *steps)
    spend = RunSpend(PROVIDER, "deepseek-flash", cap=1)
    for event in events:
        spend.add(event)
    assert spend.usage.cost_usd is None and spend.observed_cost_usd is None
    assert spend.budget_verifiable is False
    assert _dsh_result(events)["root_usage_complete"] is False


def test_dsh_missing_later_step_keeps_known_usage_without_claiming_a_complete_budget(tmp_path):
    from looper.agents.run_usage import run_usage
    from looper.agents.runtimes.pricing import RunSpend, pricer

    events = _dsh_usage_run(tmp_path, _dsh_step(),
                            {"type": "status", "phase": "step_start", "turn": 1, "step": 2})
    spend = RunSpend(PROVIDER, "deepseek-flash", cap=1)
    for event in events:
        spend.add(event)
    assert spend.usage.cost_usd is None and spend.budget_verifiable is False
    assert spend.observed_cost_usd == pytest.approx(0.000008442)
    own = run_usage(_dsh_result(events), price=pricer(PROVIDER))
    assert own.input_tokens == 10 and own.cost_usd is None


def test_dsh_projects_headless_events_and_only_final_is_the_result(tmp_path):
    from looper.agents.claude_cli import answer_text

    events = _dsh_replay(tmp_path, "success.jsonl")
    assert events[0] == {"type": "system", "subtype": "init", "session_id": "synthetic-session",
                         "runtime": "dsh", "model": "deepseek-flash", "cwd": "/synthetic/project"}
    content = [block for event in events if event["type"] == "assistant" for block in event["message"]["content"]]
    assert {"type": "thinking", "thinking": "Plan the synthetic check."} in content
    assert {"type": "text", "text": "Checking progress; this is not the answer."} in content
    calls = [block for block in content if block["type"] == "tool_use"]
    assert calls == [{"type": "tool_use", "id": "call-a", "name": "read", "input": {"path": "sample.txt"}},
                     {"type": "tool_use", "id": "call-b", "name": "shell", "input": "invalid arguments"}]
    replies = [event["message"]["content"][0] for event in events if event["type"] == "user"]
    assert replies == [{"type": "tool_result", "tool_use_id": "call-b", "content": "synthetic tool failure", "is_error": True},
                       {"type": "tool_result", "tool_use_id": "call-a", "content": "synthetic contents", "is_error": False}]
    assert all(event["session_id"] == "synthetic-session" for event in events)
    result = _dsh_result(events)
    assert result["subtype"] == "success" and result["is_error"] is False
    assert result["result"] == "最终答复 only."
    assert answer_text(result, ["earlier progress"]) == result["result"]
    assert result["stop_reason"] == {"kind": "completed"}


@pytest.mark.parametrize(("fixture", "exit_code", "error"), [
    ("error_final.jsonl", 0, "synthetic provider failed"),
    ("error_final.jsonl", 1, "exit code 1"),
    ("only_error.jsonl", 0, "synthetic startup failure"),
    ("only_error.jsonl", 1, "synthetic startup failure"),
    ("bad_json.jsonl", 0, "JSON"),
    ("no_final.jsonl", 0, "final"),
    ("success.jsonl", 3, "exit code 3"),
    ("success.jsonl", -15, "exit code -15"),
])
def test_dsh_terminal_failures_keep_original_final_and_never_succeed(tmp_path, fixture, exit_code, error):
    result = _dsh_result(_dsh_replay(tmp_path, fixture, exit_code=exit_code))
    assert result["subtype"] == "error_during_execution" and result["is_error"] is True
    assert error in "\n".join(result["errors"])
    finals = [json.loads(line)["text"] for line in (DSH_JSON / fixture).read_text().splitlines()
              if line.startswith("{\"type\": \"final\"")]
    assert result["result"] == (finals[-1] if finals else "")


@pytest.mark.parametrize("reason", [None, "completed", {}, {"kind": "cancelled"}, {"kind": "unknown"}])
def test_dsh_requires_a_completed_reason_object(tmp_path, reason):
    payload = "\n".join(json.dumps(event) for event in [
        {"type": "status", "phase": "turn_end", "reason": reason}, {"type": "final", "text": "kept final"},
    ]).encode()
    events = asyncio.run(asyncio.wait_for(_dsh_output(tmp_path, payload), 5))
    result = _dsh_result(events)
    assert result["is_error"] is True
    assert result["result"] == "kept final"


@pytest.mark.parametrize("line", [b'[]', b'null', b'{"type":"text","text":', b'\xff',
                                  b'{"type":"future","value":NaN}',
                                  b'{"type":"final","text":null}', b'{"type":"error"}'])
def test_dsh_malformed_records_are_errors_even_if_a_valid_final_follows(tmp_path, line):
    payload = line + b'\n' + (DSH_JSON / "success.jsonl").read_bytes()
    result = _dsh_result(asyncio.run(asyncio.wait_for(_dsh_output(tmp_path, payload), 5)))
    assert result["is_error"] is True
    assert result["result"] == "最终答复 only."


@pytest.mark.parametrize("field", ["type", "payload"])
def test_dsh_unknown_events_are_bounded_diagnostics_and_do_not_invent_compaction(tmp_path, field):
    unknown = json.dumps({"type": "future", field: "synthetic" * 10000}).encode() + b'\n'
    events = asyncio.run(asyncio.wait_for(
        _dsh_output(tmp_path, unknown * 40 + (DSH_JSON / "unknown.jsonl").read_bytes()), 5))
    diagnostics = [event for event in events if event.get("subtype") == "dsh_event"]
    unknowns = [event for event in diagnostics if event.get("event_type") != "status"]
    assert 1 <= len(unknowns) <= 8
    assert all(len(json.dumps(event, ensure_ascii=False).encode()) < 4096 for event in diagnostics)
    assert not any(event.get("subtype") == "compact_boundary" for event in events)
    result = _dsh_result(events)
    assert result["is_error"] is False
    assert result["result"] == "Answer after unknown events."


def test_dsh_unknown_diagnostic_limit_does_not_drop_known_truncation_markers(tmp_path):
    payload = b'{"type":"future"}\n' * 20 + (DSH_JSON / "truncated.jsonl").read_bytes()
    events = asyncio.run(asyncio.wait_for(_dsh_output(tmp_path, payload), 5))
    assert len([event for event in events if event["type"] != "usage" and event.get("truncated") is True]) == 7
    assert _dsh_result(events)["is_error"] is False


def test_dsh_errors_are_bounded_and_do_not_replace_a_preceding_final(tmp_path):
    error = json.dumps({"type": "error", "message": "synthetic failure" * 1000}).encode() + b'\n'
    payload = (DSH_JSON / "success.jsonl").read_bytes() + error * 40
    events = asyncio.run(asyncio.wait_for(_dsh_output(tmp_path, payload), 5))
    result = _dsh_result(events)
    assert result["is_error"] is True and result["result"] == "最终答复 only."
    assert 1 <= len(result["errors"]) <= 8
    assert all(len(message.encode()) <= 2048 for message in result["errors"])


@pytest.mark.parametrize("payload", [b'', b'{"type":"final","text":"kept"}\n',
                                    b'{"type":"status","phase":"turn_end","truncated":true}\n'
                                    b'{"type":"final","text":"kept"}\n',
                                    b'{"type":"status","phase":"turn_end","reason":{"kind":"completed"}}\n'
                                    b'{"type":"final","text":"cut","truncated":true}\n'])
def test_dsh_incomplete_terminal_evidence_fails_even_with_zero_exit(tmp_path, payload):
    result = _dsh_result(asyncio.run(asyncio.wait_for(_dsh_output(tmp_path, payload), 5)))
    assert result["is_error"] is True


def test_dsh_a_new_turn_cannot_reuse_a_previous_turns_completed_reason(tmp_path):
    payload = b'{"type":"status","phase":"turn_end","turn":1,"reason":{"kind":"completed"}}\n' \
              b'{"type":"status","phase":"turn_start","turn":2}\n' \
              b'{"type":"final","text":"unterminated second turn"}\n'
    result = _dsh_result(asyncio.run(asyncio.wait_for(_dsh_output(tmp_path, payload), 5)))
    assert result["is_error"] is True
    assert result["result"] == "unterminated second turn"


def test_dsh_empty_final_stays_empty_and_duplicate_final_does_not_replace_it(tmp_path):
    payload = b'{"type":"text","text":"progress"}\n' \
              b'{"type":"status","phase":"turn_end","reason":{"kind":"completed"}}\n' \
              b'{"type":"final","text":""}\n'
    result = _dsh_result(asyncio.run(asyncio.wait_for(_dsh_output(tmp_path, payload), 5)))
    assert result["is_error"] is False and result["result"] == ""
    payload += b'{"type":"final","text":"duplicate"}\n'
    result = _dsh_result(asyncio.run(asyncio.wait_for(_dsh_output(tmp_path, payload), 5)))
    assert result["is_error"] is True and result["result"] == ""


def test_dsh_keeps_truncation_markers_and_does_not_invent_dropped_tool_input(tmp_path):
    events = _dsh_replay(tmp_path, "truncated.jsonl")
    truncated = [event for event in events if event["type"] != "usage" and event.get("truncated") is True]
    assert len(truncated) == 7
    blocks = [block for event in truncated if event["type"] in ("assistant", "user")
              for block in event["message"]["content"]]
    assert all(block.get("truncated") is True for block in blocks)
    call = next(block for block in blocks if block["type"] == "tool_use")
    assert call["id"] == "cut-call" and call.get("input") is None
    assert _dsh_result(events)["is_error"] is False


@pytest.mark.parametrize("suffix", [b'\n', b''])
def test_dsh_reads_complete_large_final_without_line_limit_or_required_newline(tmp_path, suffix):
    payload = (DSH_JSON / "large_final.jsonl").read_bytes().rstrip(b'\n') + suffix
    final = json.loads(payload.splitlines()[-1])["text"]
    assert len(final.encode()) > 32 * 1024
    result = _dsh_result(asyncio.run(asyncio.wait_for(_dsh_output(tmp_path, payload), 5)))
    assert result["is_error"] is False and result["result"] == final


@pytest.mark.parametrize(("verify_exit", "cancelled"), [(False, False), (True, True)])
def test_dsh_unverified_exit_or_cancelled_run_cannot_succeed_with_a_late_final(tmp_path, verify_exit, cancelled):
    result = _dsh_result(_dsh_replay(tmp_path, "success.jsonl", verify_exit=verify_exit, cancelled=cancelled))
    assert result["is_error"] is True
    assert ("cancel" if cancelled else "exit") in "\n".join(result["errors"]).lower()
    assert result["result"] == "最终答复 only."


@pytest.mark.parametrize(("exit_code", "cancelled"), [(0, False), (7, False), (0, True)])
def test_dsh_waits_for_process_exit_after_stdout_eof_before_publishing_result(tmp_path, exit_code, cancelled):
    async def check():
        from looper.agents.runtimes.dsh import DshStream

        source = tmp_path / "source.jsonl"
        source.write_bytes((DSH_JSON / "success.jsonl").read_bytes())
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-I", "-c",
            "import os,pathlib,sys; sys.stdout.buffer.write(pathlib.Path(sys.argv[1]).read_bytes()); "
            "sys.stdout.buffer.flush(); os.close(1); sys.stderr.write(\"ready\\n\"); sys.stderr.flush(); "
            "sys.stdin.buffer.read(1); sys.exit(int(sys.argv[2]))",
            str(source), str(exit_code), stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, env={"HOME": str(tmp_path)}, cwd=tmp_path,
        )
        collected = []
        stream = DshStream()

        async def collect():
            async for event in stream.events(process.stdout, process=process):
                collected.append(event)

        reader = asyncio.create_task(collect())
        try:
            assert await process.stderr.readline() == b'ready\n'
            # Consume progress before allowing the child to exit; final must stay buffered.
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(asyncio.shield(reader), 0.05)
            assert collected
            assert not any(event["type"] == "result" for event in collected)
            assert not reader.done() and process.returncode is None
            if cancelled:
                stream.cancel()
            process.stdin.write(b'x')
            await process.stdin.drain()
            process.stdin.close()
            await reader
            result = _dsh_result(collected)
            assert result["is_error"] is (cancelled or exit_code != 0)
        finally:
            if process.returncode is None:
                process.kill()
            await process.wait()
            reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)

    asyncio.run(asyncio.wait_for(check(), 5))


def _run_launch(tmp_path, *, budget=None, role="worker", model="deepseek-flash"):
    from looper.agents.agent_settings import AgentSettings, AgentType, Launch
    from looper.agents.library import AgentLibrary
    from looper.agents.ops.service import server_config

    runtime = runtimes.adapter("dsh")
    source = tmp_path / "mcp.json"
    source.write_text(json.dumps({"mcpServers": {
        "looper": server_config("http://127.0.0.1:19999", "test-run-token"),
    }}))
    profile = resolved_profile("dsh-test", "DSH test", RuntimeSpec("dsh-test", "DSH", "dsh", "", runtime.capabilities),
                           PROVIDER, model, "high")
    return Launch(AgentSettings(profile=profile, max_budget_usd=budget),
                  AgentLibrary(mcp_config=str(source), role=role), role=AgentType(role))


@pytest.mark.parametrize("platform_bridge", [False, True])
def test_dsh_roleless_launch_has_no_platform_tools(tmp_path, platform_bridge):
    from looper.agents.runtimes import dsh

    launch = _run_launch(tmp_path)
    launch = replace(launch, role=None, library=replace(
        launch.library, role=None, mcp_config=launch.library.mcp_config if platform_bridge else None))
    executable = _headless_stand_in(tmp_path)
    prompt = tmp_path / ".local" / "prompts" / "system.md"
    prompt.parent.mkdir(parents=True)
    prompt.write_text("Role-less run")
    command = launch.runtime.run_command(executable, cwd=tmp_path, prompt_file=prompt, launch=launch)
    assert command[-2:] == ["--json", "-"]
    assert not any(name.startswith("LOOPER_DSH_MCP_SECRET_") for name in launch.environment())
    translated = dsh.mcp_config(launch.library)
    assert not translated.servers and not translated.env
    omitted = [{"name": "looper", "reason": "platform MCP omitted without an agent role"}] if platform_bridge else []
    assert list(translated.omissions) == omitted
    assert launch.runtime.launch_record(launch)["runtime_catalog"]["skipped"] == omitted
    bootstrap = dsh.bootstrap_files(executable, "Role-less run", launch)["looper-bootstrap.mjs"]
    assert '"mcp": []' in bootstrap and "test-run-token" not in bootstrap


def _headless_stand_in(tmp_path, *, usage=DSH_STEP_USAGE, exit_code=0, hang=False, final=None):
    from fake_claude import fake_claude

    records = [{"type": "session", "sessionId": "stand-in-session"}, _dsh_step(usage=usage),
               {"type": "status", "phase": "turn_end", "reason": {"kind": "completed"}},
               {"type": "final", "text": final or json.dumps({"status": "completed", "changed_files": {},
                                                               "summary": "stand-in result"})}]
    return fake_claude(tmp_path / "dsh-stand-in", f'''
import json, os, pathlib, sys, time
prompt = sys.stdin.buffer.read()  # The real CLI waits for EOF, not just the first line.
pathlib.Path({str(tmp_path / 'seen.json')!r}).write_text(json.dumps({{
    'argv': sys.argv[1:], 'prompt': prompt.decode(), 'pid': os.getpid(),
    'home': os.environ['HOME'], 'dsh_home': os.environ['DSH_HOME'],
}}))
for record in {records!r}:
    print(json.dumps(record), flush=True)
# Exercise stderr backpressure before EOF as well as completed-but-still-running children.
sys.stderr.write('x' * 200000)
sys.stderr.flush()
if {hang!r}:
    time.sleep(60)
sys.exit({exit_code})
''')


def _run_prompt_file(tmp_path):
    directory = tmp_path / ".local" / "prompts"
    directory.mkdir(parents=True, exist_ok=True)
    prompt = directory / "test.md"
    prompt.write_text("Literal {{unknown}} 中文 system prompt")
    return prompt


def _installed_dsh_skills(tmp_path, monkeypatch, overrides=None):
    from unittest.mock import AsyncMock
    from looper.agents import library
    from looper.db import library as db_library

    def skill(name, extra=""):
        return f"---\nname: {name}\ndescription: Test skill\n{extra}---\nManaged {name}.\n"

    snapshots = {
        "installed": {"SKILL.md": skill("installed"), "references/note.md": "Managed reference"},
        "invalid": {"SKILL.md": skill("Bad_Name")},
        "hidden": {"SKILL.md": skill("hidden", "disable-model-invocation: true\n")},
        "bundle": {"skills/bundled/SKILL.md": skill("bundled"),
                   "hooks/hooks.json": "{}", "commands/test.md": "command marker",
                   "agents/test.md": "agent marker", "output-styles/test.md": "style marker",
                   ".mcp.json": '{"mcpServers":{"duplicate":{"command":"never-run"}}}'},
    }
    snapshots.update(overrides or {})
    rows, files = [], {}
    for name, snapshot in snapshots.items():
        files[name] = [{"path": path, "content": text, "sha256": hashlib.sha256(text.encode()).hexdigest(),
                        "executable": False} for path, text in snapshot.items()]
        rows.append({"id": name, "kind": "plugin" if name == "bundle" else "skill", "name": name,
                     "sha256": library.item_sha256(files[name]), "origin": "ui", "scope": "global",
                     "owner_project_id": None})
    monkeypatch.setattr(db_library, "list_installed", AsyncMock(return_value=rows))
    monkeypatch.setattr(db_library, "get_item_files", AsyncMock(side_effect=lambda name: files[name]))
    return asyncio.run(library.load_installed(tmp_path, "worker"))


@pytest.mark.installed
@pytest.mark.parametrize("frontmatter", [
    "name: invalid\ndescription: test\ndisable-model-invocation: maybe",
    "name: invalid\ndescription: test\nuserInvocable: false",
    "name: invalid\ndescription: [not, a, string]",
    "name: invalid\ndescription: test\nname: duplicate",
])
def test_dsh_incompatible_frontmatter_is_explicitly_omitted(tmp_path, monkeypatch, frontmatter):
    from looper.agents.runtimes import dsh

    if shutil.which("dsh") is None:
        pytest.skip("Installed DSH is required for skill validation")
    library = _installed_dsh_skills(tmp_path, monkeypatch, {
        "invalid": {"SKILL.md": f"---\n{frontmatter}\n---\nNever advertise this skill."}})
    catalog = dsh.materialize(library)
    assert [row["name"] for row in catalog.loaded] == ["bundled", "installed"]
    assert any(row["reason"] == "incompatible DSH skill frontmatter" for row in catalog.skipped)


@pytest.mark.installed
def test_dsh_installed_catalog_matches_official_loader_and_record(tmp_path, monkeypatch):
    from looper.agents.runtimes import dsh

    executable = shutil.which("dsh")
    if executable is None:
        pytest.skip("Installed DSH is required for skill discovery validation")
    installed = _installed_dsh_skills(tmp_path, monkeypatch)
    launch = _run_launch(tmp_path)
    launch = replace(launch, library=replace(installed, mcp_config=launch.library.mcp_config))
    for root in (tmp_path / ".dsh/skills", tmp_path / ".agents/skills",
                 tmp_path / "operator/skills", tmp_path / "uninstalled"):
        (root / "marker").mkdir(parents=True)
        (root / "marker/SKILL.md").write_text("---\nname: marker\ndescription: Global marker\n---\nSECRET_MARKER")
    monkeypatch.setenv("DSH_BUNDLED_SKILL_DIR", str(tmp_path / "uninstalled"))
    command = launch.runtime.run_command(executable, cwd=tmp_path,
                                         prompt_file=_run_prompt_file(tmp_path), launch=launch)
    patch_path = Path(command[command.index("--patch") + 1])
    patch = json.loads(patch_path.read_text())
    config = next(row["config"] for row in patch if row.get("id") == "skill-filesystem")
    record = launch.record()
    assert record["runtime_catalog"]["loaded"] == [
        {"name": "bundled", "kind": "skill"}, {"name": "installed", "kind": "skill"}]
    reasons = json.dumps(record["runtime_catalog"]["skipped"])
    for part in ("Bad_Name", "hidden", "hooks", "commands", "agents", "output-styles", ".mcp.json"):
        assert part in reasons
    done = _node(tmp_path, '''
import {createRequire} from 'node:module';
import {realpathSync} from 'node:fs';
import {pathToFileURL} from 'node:url';
const require = createRequire(realpathSync(process.argv[2]));
const {apply} = await import(pathToFileURL(require.resolve('@deepseek-ai/dsh-skill-filesystem')));
let provider;
const control = {signal: new AbortController().signal, invalidate() {}};
const ctx = {skills: {registerProvider(make) {provider = make(control);}},
             get() {}, on() {}, effect() {}, logger: {warn() {}}};
apply(ctx, {...JSON.parse(process.argv[3]), dshHome: process.argv[4], agentsHome: process.argv[4]});
try {console.log(JSON.stringify(await provider.list({cwd: process.cwd()})));}
finally {await provider.dispose();}
''', executable, json.dumps(config), tmp_path / "operator")
    assert done.returncode == 0, done.stderr
    actual = json.loads(done.stdout)
    assert sorted(row["name"] for row in actual) == [row["name"] for row in record["runtime_catalog"]["loaded"]]
    for row in actual:
        assert Path(row["locator"]["path"]).is_file()
    manifest = json.loads(patch_path.with_name("launch-record.json").read_text())
    assert manifest["runtime_catalog"] == record["runtime_catalog"]
    assert manifest["runtime_config_sha256"] == hashlib.sha256(patch_path.read_bytes()).hexdigest()
    assert manifest["runtime_prompt_sha256"] == hashlib.sha256(
        dsh._prompt_text(_run_prompt_file(tmp_path).read_text(), launch, cwd=tmp_path).encode()).hexdigest()
    assert manifest["isolation"] == dsh.HOME_LAYOUT_ID
    assert manifest["system_prompt_mode"] == "append"
    assert manifest["usage_complete"] is False and manifest["web_search"] is False
    generated = "\n".join(path.read_text() for path in (tmp_path / ".local/dsh").rglob("*") if path.is_file())
    assert "SECRET_MARKER" not in generated
    assert PROVIDER.api_key not in generated and "test-run-token" not in generated
    assert "never-run" not in generated


@pytest.mark.installed
def test_dsh_duplicate_skill_names_are_recorded_and_plugin_mcp_is_not_mounted_twice(tmp_path, monkeypatch):
    from looper.agents.runtimes import dsh

    if shutil.which("dsh") is None:
        pytest.skip("Installed DSH is required for skill validation")
    installed = _installed_dsh_skills(tmp_path, monkeypatch, {
        "duplicate": {"SKILL.md": "---\nname: bundled\ndescription: Duplicate\n---\nDuplicate body."}})
    launch = _run_launch(tmp_path)
    source = Path(launch.library.mcp_config)
    servers = json.loads(source.read_text())
    servers["mcpServers"]["duplicate"] = {"command": "catalog-mcp", "env": {"TOKEN": "synthetic-mcp-secret"}}
    source.write_text(json.dumps(servers))
    launch = replace(launch, library=replace(installed, mcp_config=str(source)))
    catalog = dsh.materialize(launch.library)
    assert [row["name"] for row in catalog.loaded] == ["bundled", "installed"]
    assert any(row["reason"] == "duplicate DSH skill name" for row in catalog.skipped)
    reversed_catalog = dsh.materialize(replace(launch.library, plugin_dirs=tuple(reversed(installed.plugin_dirs))))
    assert reversed_catalog.skill_dirs != catalog.skill_dirs
    assert (Path(reversed_catalog.skill_dirs[0]) / "bundled/SKILL.md").read_text().endswith("Duplicate body.")
    assert [server["serverName"] for server in dsh.mcp_config(launch.library).servers] == ["looper", "duplicate"]
    command = launch.runtime.run_command(shutil.which("dsh"), cwd=tmp_path,
                                         prompt_file=_run_prompt_file(tmp_path), launch=launch)
    patch = Path(command[command.index("--patch") + 1])
    bootstrap = patch.with_name("looper-bootstrap.mjs").read_text()
    assert bootstrap.count('"serverName": "duplicate"') == 1
    assert "never-run" not in bootstrap and "synthetic-mcp-secret" not in bootstrap


def test_dsh_injected_instructions_and_manifest_follow_resident_reconfiguration(tmp_path, monkeypatch):
    from looper.agents.runtimes import dsh

    launch = _run_launch(tmp_path)
    (tmp_path / "AGENTS.md").write_text("Project marker {{literal}}")
    (tmp_path / "CLAUDE.md").write_text("Lower priority marker")
    prompt = _run_prompt_file(tmp_path)
    command = launch.runtime.resident_command(_headless_stand_in(tmp_path), cwd=tmp_path,
                                               prompt_file=prompt, launch=launch)
    resident = dsh.ResidentDsh(label="isolated-manager")
    rebased, _, _ = resident._split(command)
    patch_path = Path(rebased[rebased.index("--patch") + 1])
    record = json.loads(patch_path.with_name("launch-record.json").read_text())
    text = launch.runtime.prompt_blocks(launch, prompt.read_text(), cwd=tmp_path)[1].text
    assert text.count("Project marker {{literal}}") == 1
    assert "Lower priority marker" not in text
    assert record["runtime_config_sha256"] == hashlib.sha256(patch_path.read_bytes()).hexdigest()
    assert record["runtime_prompt_sha256"] == hashlib.sha256(text.encode()).hexdigest()


@pytest.mark.installed
@pytest.mark.parametrize("value, enabled", [(None, False), ("false", False), ("true", True)])
def test_dsh_search_policy_and_complete_budgets_ignore_project_dotenv(tmp_path, monkeypatch, value, enabled):
    launch = _run_launch(tmp_path)
    launch = replace(launch, settings=replace(launch.settings, env=() if value is None else
                                            (("DSH_ALLOW_WEB_SEARCH", value),)))
    (tmp_path / ".env").write_text("DSH_ALLOW_WEB_SEARCH=true\nDSH_TOOLS_MODE=ptc\nDSH_TELEMETRY_MODE=ALL\n")
    monkeypatch.setenv("DSH_ALLOW_WEB_SEARCH", "true")
    from looper.agents.runtimes import dsh

    patch = dsh._launch_patch(launch, tmp_path)
    configs = {row["id"]: row["config"] for row in patch if "config" in row}
    assert configs["tool-web"] == {"fetch": True, "search": enabled, "searchTimeoutMs": 60000,
                                   "fetchTimeoutMs": 30000, "fetchMaxOutputChars": 200000,
                                   "searchMaxQueries": 4, "searchMaxResults": 8}
    assert configs["compaction-basic"] == {"auto": True, "thresholdRatio": .8, "headroomTokens": 65536,
        "retainRatio": .16, "maxTokens": 65536, "compactionRetries": 1, "maxOverflowRetries": 1,
        "summarizationProvider": "deepseek-official", "summarizationModel": "deepseek-flash", "modelPolicies": []}
    assert configs["skill-filesystem"] == {"includeDefaultRoots": False, "customSkillDirs": [], "watch": False}
    for name in ("web-search-deepseek", "web-deepseek-api"):
        assert next(row for row in patch if row.get("id") == name)["disabled"] is not enabled
    assert launch.record()["web_search"] is enabled
    executable = shutil.which("dsh")
    if executable is not None:
        command = launch.runtime.run_command(executable, cwd=tmp_path,
                                             prompt_file=_run_prompt_file(tmp_path), launch=launch)
        command = [*command[:command.index("--json")], "--dump-config"]
        done = subprocess.run(command, cwd=tmp_path, env=launch.environment(), capture_output=True,
                              text=True, timeout=60)
        assert done.returncode == 0, done.stderr
        parsed = _node(tmp_path, '''
import {createRequire} from 'node:module';
import {realpathSync} from 'node:fs';
const require = createRequire(realpathSync(process.argv[2]));
const {parse} = require('yaml');
console.log(JSON.stringify(parse(process.argv[3])));
''', executable, done.stdout)
        assert parsed.returncode == 0, parsed.stderr
        entries = json.loads(parsed.stdout)
        by_id = {row["id"]: row for row in entries}
        for name in ("tool-web", "tools", "skill-filesystem", "compaction-basic", "llm-deepseek"):
            assert by_id[name]["config"] == configs[name]
        for name in ("hmr", "settings", "config-editor", "plugin-manager", "tool-plugin-manager", "credentials",
                     "authorization", "deepseek-account", "llm-deepseek-account", "llm-pi-ai", "otel",
                     "session-log-deepseek", "plugin-package-inventory-deepseek", "session-title-llm",
                     "agent-instructions", "session-telemetry-otel"):
            assert by_id[name]["disabled"] is True
        for name in ("web-search-deepseek", "web-deepseek-api"):
            assert by_id[name]["disabled"] is not enabled


@pytest.mark.parametrize("value", ["TRUE", "1", "yes", "", " false "])
def test_dsh_rejects_invalid_search_policy_at_every_launch_boundary(tmp_path, value):
    launch = _run_launch(tmp_path)
    launch = replace(launch, settings=replace(launch.settings, env=(("DSH_ALLOW_WEB_SEARCH", value),)))
    for check in (lambda: launch.environment(), lambda: launch.record()):
        with pytest.raises(RuntimeUnsupported, match="DSH_ALLOW_WEB_SEARCH"):
            check()


def _resident_dsh_stand_in(tmp_path):
    """An opaque test store; this deliberately knows nothing about DSH's durable log format.

    The store keeps the stdin bytes. ``DshRuntime.write_turn`` terminates that prompt with one
    newline, so the echoed final answer drops that terminator and nothing else.
    """
    from fake_claude import fake_claude

    return fake_claude(tmp_path / "resident-dsh", '''
import json, os, pathlib, sys, uuid
args = sys.argv[1:]
text = sys.stdin.read()
answer = text[:-1] if text.endswith('\\n') else text
home = pathlib.Path(os.environ['DSH_HOME'])
store = home / 'opaque-test-store'
sessions = json.loads(store.read_text()) if store.exists() else {}
sid = args[args.index('--session-id') + 1] if '--session-id' in args else None
with (home / 'calls').open('a') as log:
    log.write(json.dumps({'args': args, 'text': text, 'pid': os.getpid()}) + '\\n')
if sid and (sid not in sessions or sessions[sid]['cwd'] != os.getcwd()):
    print(json.dumps({'type': 'error', 'message': 'unknown session or cwd'}), flush=True)
    sys.exit(7)
sid = sid or str(uuid.uuid4())
sessions.setdefault(sid, {'cwd': os.getcwd(), 'turns': []})['turns'].append(text)
store.write_text(json.dumps(sessions))
if text == 'silent-exit':
    sys.exit(7)
if text == 'malformed':
    print('not JSON', flush=True)
    sys.exit(7)
if text == 'error-final':
    print(json.dumps({'type': 'error', 'message': 'failed'}), flush=True)
    print(json.dumps({'type': 'final', 'text': 'possibly executed'}), flush=True)
    sys.exit(7)
if text != 'no-opening':
    print(json.dumps({'type': 'session', 'sessionId': sid, 'cwd': os.getcwd()}), flush=True)
if text == 'unfinished':
    print(json.dumps({'type': 'tool_call', 'callId': 'write-1', 'tool': 'write', 'input': {}}), flush=True)
    sys.exit(7)
if text == 'hang':
    import signal, subprocess, time
    child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
    (home / 'child-pid').write_text(str(child.pid))
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    time.sleep(60)
print(json.dumps({'type': 'status', 'phase': 'step_end', 'turn': len(sessions[sid]['turns']), 'step': 1,
                  'usage': {'inputTokens': 10, 'outputTokens': 4, 'cacheReadTokens': 7, 'cacheWriteTokens': 2}}))
print(json.dumps({'type': 'status', 'phase': 'turn_end', 'reason': {'kind': 'completed'}}))
print(json.dumps({'type': 'final', 'text': answer}))
''')


def test_dsh_resident_owners_resume_opening_id_and_count_only_new_tokens(tmp_path):
    from looper.agents.run_usage import Usage

    executable = _resident_dsh_stand_in(tmp_path)
    runtime = runtimes.adapter("dsh")
    launch = _run_launch(tmp_path, role="manager")
    command = runtime.resident_command(executable, cwd=tmp_path, prompt_file=_run_prompt_file(tmp_path), launch=launch)

    async def check():
        owners = [runtime.resident_session(label) for label in ("manager", "planner-a", "planner-b")]
        sessions, homes = [], []
        for owner in owners:
            await owner.start(command, cwd=str(tmp_path), signature="a", env=launch.environment())
            seen = []

            async def record(event):
                seen.append(event)

            first = await owner.run_turn("first", record)
            sid = first.result_event["session_id"]
            second = await owner.run_turn("second", record)
            assert second.result_event["session_id"] == sid
            own = Usage(input_tokens=10, output_tokens=4, cache_read_tokens=7, cache_creation_tokens=2)
            assert first.usage == second.usage == own
            assert owner.totals == own.plus(own)
            assert [e["session_id"] for e in seen if e.get("subtype") == "init"] == [sid, sid]
            home = Path(next(arg.partition("=")[2] for arg in owner.command if arg.startswith("DSH_HOME=")))
            calls = [json.loads(line) for line in (home / "calls").read_text().splitlines()]
            assert "--session-id" not in calls[0]["args"]
            assert calls[1]["args"][-3:] == ["--session-id", sid, "-"]
            assert calls[0]["pid"] != calls[1]["pid"]
            sessions.append(sid)
            homes.append(home)
            await owner.stop()
        assert len(set(sessions)) == len(set(homes)) == 3

    asyncio.run(asyncio.wait_for(check(), 10))


def test_dsh_resident_prices_usage_by_the_configured_model_not_the_normalized_one(tmp_path):
    """``deepseek-v4-pro[1m]`` is sent to the API as ``deepseek-v4-pro`` but is priced as configured."""
    from looper.agents.runtimes.pricing import RunSpend

    prices = {"deepseek-v4-pro": 1.0, "deepseek-v4-pro[1m]": 5.0}
    executable = _resident_dsh_stand_in(tmp_path)
    runtime = runtimes.adapter("dsh")
    launch = _run_launch(tmp_path, role="manager", model="deepseek-v4-pro[1m]")
    command = runtime.resident_command(executable, cwd=tmp_path, prompt_file=_run_prompt_file(tmp_path), launch=launch)
    spend = RunSpend(lambda model, usage: prices[model], launch.model, 3.0)

    async def check():
        owner = runtime.resident_session("manager")
        await owner.start(command, cwd=str(tmp_path), signature="a", env=launch.environment(),
                          price=lambda model, usage: prices[model])
        events = []

        async def record(event):
            events.append(event)
            spend.add(event)

        turn = await owner.run_turn("first", record)
        assert turn.result_event["modelUsage"].keys() == {"deepseek-v4-pro[1m]"}
        assert {e["model"] for e in events if e.get("type") == "usage"} == {"deepseek-v4-pro[1m]"}
        assert spend.observed_cost_usd == 5.0
        assert turn.result_event["observed_cost_usd"] == 5.0  # what the evals and the budget read from the result
        assert spend.exceeded  # a 3 USD cap stops it; the normalized model's 1 USD would have missed it
        await owner.stop()

    asyncio.run(asyncio.wait_for(check(), 10))


@pytest.mark.parametrize("message", ["unfinished", "complete", "silent-exit", "malformed", "no-opening", "error-final"])
def test_dsh_resident_failure_and_missing_session_are_not_success(tmp_path, message):
    executable = _resident_dsh_stand_in(tmp_path)
    runtime = runtimes.adapter("dsh")
    launch = _run_launch(tmp_path, role="manager")
    command = runtime.resident_command(executable, cwd=tmp_path, prompt_file=_run_prompt_file(tmp_path), launch=launch)
    if message == "complete":
        command += runtime.resume_args("not-a-session")

    async def check():
        owner = runtime.resident_session("manager")
        await owner.start(command, cwd=str(tmp_path), signature="a", env=launch.environment())
        events = []

        async def record(event):
            events.append(event)

        # An explicit startup rejection is safe to replace; silence or corrupt output is not proof of that.
        if message in ("silent-exit", "malformed", "no-opening", "error-final"):
            with pytest.raises(RuntimeError, match="cannot be safely replayed"):
                await owner.run_turn(message, record)
            assert _dsh_result(events)["is_error"]
        else:
            turn = await owner.run_turn(message, record)
            assert turn.result_event["is_error"]
            assert turn.session_started is (message == "unfinished")
        assert owner.stale("a")
        home = Path(next(arg.partition("=")[2] for arg in owner.command if arg.startswith("DSH_HOME=")))
        assert len((home / "calls").read_text().splitlines()) == 1  # never replay the input here
        await owner.stop()

    asyncio.run(asyncio.wait_for(check(), 5))


def test_dsh_resident_stop_ends_running_process_tree_after_stdin_eof(tmp_path, monkeypatch):
    from looper.agents import proc_tree

    monkeypatch.setattr(proc_tree, "TERM_GRACE_SEC", 0.1)
    executable = _resident_dsh_stand_in(tmp_path)
    runtime = runtimes.adapter("dsh")
    launch = _run_launch(tmp_path, role="manager")
    command = runtime.resident_command(executable, cwd=tmp_path, prompt_file=_run_prompt_file(tmp_path), launch=launch)

    async def check():
        owner = runtime.resident_session("manager")
        await owner.start(command, cwd=str(tmp_path), signature="a", env=launch.environment())
        home = Path(next(arg.partition("=")[2] for arg in owner.command if arg.startswith("DSH_HOME=")))
        events = []

        async def record(event):
            events.append(event)

        running = asyncio.create_task(owner.run_turn("hang", record))
        try:
            while not (home / "child-pid").exists():
                await asyncio.sleep(0.01)
            parent, child = owner.pid, int((home / "child-pid").read_text())
            assert parent is not None
            await owner.stop()
            turn = await running
            assert turn.result_event["is_error"]
            assert not owner.started
            for pid in (parent, child):
                assert pid not in proc_tree._snapshot()
            assert not any(e.get("subtype") == "success" for e in events)
        finally:
            await owner.stop()
            running.cancel()
            await asyncio.gather(running, return_exceptions=True)

    asyncio.run(asyncio.wait_for(check(), 8))


def test_dsh_resident_does_not_open_an_old_cwd_session(tmp_path):
    runtime = runtimes.adapter("dsh")
    executable = _resident_dsh_stand_in(tmp_path)
    launch = _run_launch(tmp_path, role="manager")
    prompt = _run_prompt_file(tmp_path)
    other = tmp_path / "other-project"
    other.mkdir()

    async def check():
        owner = runtime.resident_session("manager")
        command = runtime.resident_command(executable, cwd=tmp_path, prompt_file=prompt, launch=launch)
        await owner.start(command, cwd=str(tmp_path), signature="a", env=launch.environment())
        turn = await owner.run_turn("first", lambda event: asyncio.sleep(0))
        session = turn.result_event["session_id"]
        old_home = next(arg for arg in owner.command if arg.startswith("DSH_HOME="))
        await owner.stop()
        command = runtime.resident_command(executable, cwd=other, prompt_file=prompt, launch=launch)
        await owner.start(command + runtime.resume_args(session), cwd=str(other), signature="b", env=launch.environment())
        # CWD is part of the storage identity: even a CLI that adopts saved cwd cannot open the old log.
        assert next(arg for arg in owner.command if arg.startswith("DSH_HOME=")) != old_home
        turn = await owner.run_turn("second", lambda event: asyncio.sleep(0))
        assert not turn.session_started and turn.result_event["is_error"]
        await owner.stop()

    asyncio.run(asyncio.wait_for(check(), 5))


def test_dsh_resident_missing_executable_does_not_start_or_replay(tmp_path):
    runtime = runtimes.adapter("dsh")
    executable = _resident_dsh_stand_in(tmp_path)
    launch = _run_launch(tmp_path, role="manager")
    command = runtime.resident_command(executable, cwd=tmp_path, prompt_file=_run_prompt_file(tmp_path), launch=launch)
    Path(executable).unlink()

    async def check():
        owner = runtime.resident_session("manager")
        with pytest.raises(OSError, match="not found"):
            await owner.start(command, cwd=str(tmp_path), signature="a", env=launch.environment())
        assert not owner.started and owner.pid is None

    asyncio.run(asyncio.wait_for(check(), 5))


def test_dsh_is_registered_with_only_implemented_capabilities():
    from looper.agents.runtimes import Capability, ResidentTransport
    from looper.agents.runtimes.dsh import DshRuntime

    runtime = runtimes.adapter("dsh")
    assert isinstance(runtime, DshRuntime)
    assert runtime.default_executable == "dsh"
    assert runtime.capabilities == frozenset({Capability.RUN, Capability.MCP,
                                              Capability.RESIDENT_SESSION, Capability.RESUME})
    assert not runtime.run_input and not runtime.can_replace_system_prompt
    assert runtime.resident_transport is ResidentTransport.PER_TURN
    from looper.agents.runtimes.dsh import ResidentDsh
    assert isinstance(runtime.resident_session("owner"), ResidentDsh)
    assert runtime.resume_args("saved") == ["--session-id", "saved"]


@pytest.mark.parametrize("exit_code", [0, 7])
def test_dsh_real_launch_reads_full_stdin_eof_and_validates_exit(tmp_path, exit_code):
    runtime = runtimes.adapter("dsh")
    executable = _headless_stand_in(tmp_path, exit_code=exit_code)
    launch = _run_launch(tmp_path)
    prompt_file = _run_prompt_file(tmp_path)
    command = runtime.run_command(executable, cwd=tmp_path, prompt_file=prompt_file, launch=launch,
                                  max_turns=3, session_dir=tmp_path / "kept")
    argv = command[command.index(executable) + 1:]
    assert argv[:3] == ["--profile", "headless", "--patch"]
    assert argv[-2:] == ["--json", "-"] and "--session-id" not in argv
    assert "--max-turns" not in argv
    assert "max_turns" in json.dumps(runtime.launch_record(launch))
    patch = Path(argv[3])
    assert "test-dsh-key" not in patch.read_text() and "test-run-token" not in patch.read_text()

    async def run():
        from looper.agents import proc_tree

        process = await proc_tree.spawn(*command, cwd=tmp_path, env=launch.environment(),
                                        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                                        stderr=asyncio.subprocess.PIPE)
        channel = runtime.run_channel(process, launch=launch, cwd=tmp_path, session_dir=tmp_path / "kept")
        try:
            await channel.start("first line\n中文 second line")
            with pytest.raises(RuntimeError):
                await channel.send("too late")
            events = [event async for event in channel.events()]
            assert process.returncode == exit_code
            return events
        finally:
            await proc_tree.end(process, label="test DSH")

    events = asyncio.run(asyncio.wait_for(run(), 5))
    result = _dsh_result(events)
    assert result["is_error"] is (exit_code != 0)
    seen = json.loads((tmp_path / "seen.json").read_text())
    assert seen["prompt"] == "first line\n中文 second line\n"
    assert Path(seen["home"]) == tmp_path / "kept/home"
    assert Path(seen["dsh_home"]) == tmp_path / "kept/dsh"
    assert result["session_id"] == "stand-in-session" and result["cost_basis"] == "observed"
    assert result["observed_cost_usd"] == pytest.approx(0.000008442)
    assert result["usage_complete"] is False


def test_dsh_runs_get_unique_homes_and_kept_owner_survives_config_changes(tmp_path):
    runtime = runtimes.adapter("dsh")
    launch = _run_launch(tmp_path)
    executable = _headless_stand_in(tmp_path)
    prompt = _run_prompt_file(tmp_path)

    def home(session_dir=None):
        command = runtime.run_command(executable, cwd=tmp_path, prompt_file=prompt, launch=launch,
                                      session_dir=session_dir)
        return next(arg for arg in command if arg.startswith("DSH_HOME="))

    assert home() != home()
    first = home(tmp_path / "keep")
    prompt.write_text("updated prompt")
    assert home(tmp_path / "keep") == first
    assert home(tmp_path / "another") != first


@pytest.mark.parametrize("verify", [False, True])
@pytest.mark.parametrize("mode", ["success", "exit", "cap", "unknown", "partial-no-cap"])
def test_worker_and_reviewer_read_back_dsh_stand_in_results(tmp_path, monkeypatch, agent_db, verify, mode):
    from unittest.mock import AsyncMock
    from looper.agents.message import MessageEnvelope, MessageType, planner_address, worker_address
    from looper.agents.tiers import worker

    final = json.dumps({"passed": True, "findings": [], "summary": "review result"} if verify else
                       {"status": "completed", "changed_files": {}, "summary": "work result"})
    executable = _headless_stand_in(tmp_path, final=final, exit_code=7 if mode == "exit" else 0,
                                   usage=None if mode in ("unknown", "partial-no-cap") else DSH_STEP_USAGE,
                                   hang=mode in ("cap", "unknown"))
    launch = _run_launch(tmp_path, budget=0.000001 if mode == "cap" else 1 if mode in ("success", "unknown") else None,
                         role="reviewer" if verify else "worker")
    settings = replace(launch.settings, profile=replace(launch.profile,
                       runtime=replace(launch.profile.runtime, executable=executable)))
    monkeypatch.setattr(worker, "load_agent_settings", AsyncMock(return_value=settings))
    monkeypatch.setattr(worker, "load_installed", AsyncMock(return_value=launch.library))
    envelope = MessageEnvelope.create(source=planner_address("test"), target=worker_address("test"),
                                      message_type=MessageType.VERIFY_REQUEST if verify else MessageType.WORK_ITEM,
                                      payload={"task_id": "step", "instruction": "run", "context": {"work_dir": str(tmp_path)}})
    pool = worker.WorkerPool(config=worker.WorkerConfig(), runtime_root=tmp_path)
    outcome = asyncio.run(asyncio.wait_for(pool._execute(envelope, is_verify=verify), 8))
    success = mode in ("success", "partial-no-cap")
    if verify and not success:
        assert outcome.get("passed") is False or outcome.get("status") == "failed"
    else:
        assert (outcome.get("passed") if verify else outcome.get("status") == "completed") is success
    if success:
        assert outcome["summary"] == ("review result" if verify else "work result")
    else:
        assert mode != "unknown" or "budget" in json.dumps(outcome).lower()
    recorded = agent_db.finish_record.call_args.kwargs
    assert recorded["cost_usd"] is None  # Observed root cost never becomes the total bill.
    assert recorded["session_id"] == "stand-in-session"
    if success:
        assert recorded["output_text"] == final
    if mode in ("cap", "unknown"):
        pid = json.loads((tmp_path / "seen.json").read_text())["pid"]
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)


@pytest.mark.parametrize("unknown", [False, True], ids=["observed-cap", "unknown-root-usage"])
def test_dsh_resident_recorder_stops_at_observed_or_unverifiable_budget(tmp_path, agent_db, unknown):
    from looper.agents import proc_tree
    from looper.agents.claude_cli import ClaudeRunRecorder, SOFT_CAP

    # Cover startup and the real TERM/reap grace; this checks a budget stop, not startup latency.
    timeout = 10 + proc_tree.TERM_GRACE_SEC + proc_tree.REAP_SEC
    usage = {"inputTokens": 10, "cacheReadTokens": 7} if unknown else DSH_STEP_USAGE
    executable = _headless_stand_in(tmp_path, usage=usage, hang=True)
    launch = _run_launch(tmp_path, budget=1 if unknown else .000001, role="manager")
    runtime = launch.runtime
    command = runtime.resident_command(executable, cwd=tmp_path,
                                       prompt_file=_run_prompt_file(tmp_path), launch=launch)

    async def check():
        owner = runtime.resident_session("budgeted-owner")
        await owner.start(command, cwd=str(tmp_path), signature="budget", env=launch.environment(), price=launch.price)
        recorder = ClaudeRunRecorder(agent_id="manager", task_id=None, command=command, cwd=str(tmp_path),
                                    input_text="budgeted turn", launch=launch)
        try:
            async with recorder:
                turn = await asyncio.wait_for(owner.run_turn("budgeted turn", recorder.record,
                                                            stop=lambda: recorder.cap_stop), timeout)
                result = turn.result_event
                assert result[SOFT_CAP] and result["is_error"] and result["result"] == ""
                assert result["usage_complete"] is False and result["cost_basis"] == "observed"
                assert result["root_usage_complete"] is (not unknown)
                expected_cost = None if unknown else pytest.approx(.000008442)
                assert result["observed_cost_usd"] == expected_cost
                assert turn.usage.cost_usd is None
                assert (turn.usage.input_tokens, turn.usage.cache_read_tokens) == (10, 7)
                assert turn.usage.output_tokens == (None if unknown else 4)
                assert turn.session_started
                assert ("Cannot verify" if unknown else "over") in result["errors"][0]
                await recorder.finish(output_text=None, result_event=result, usage=turn.usage)
        finally:
            await owner.stop()

    asyncio.run(asyncio.wait_for(check(), timeout + 10))
    pid = json.loads((tmp_path / "seen.json").read_text())["pid"]
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
    recorded = agent_db.finish_record.call_args.kwargs
    assert recorded["status"] == "failed" and recorded["cost_usd"] is None
    assert (recorded["input_tokens"], recorded["cache_read_tokens"]) == (10, 7)


@pytest.mark.parametrize("runtime_type", ["claude-code", "omp"])
@pytest.mark.parametrize("mode", ["priced", "unpriced", "incomplete"])
def test_nonobserved_recorder_budget_keeps_existing_priced_and_unknown_behavior(tmp_path, runtime_type, mode):
    from looper.agents.claude_cli import ClaudeRunRecorder, SOFT_CAP
    from looper.agents.run_usage import Usage
    from looper.agents.runtimes.pricing import usage_event

    launch = _run_launch(tmp_path, budget=.000001)
    adapter = runtimes.adapter(runtime_type)
    launch = replace(launch, settings=replace(launch.settings, profile=replace(launch.profile,
        runtime=replace(launch.profile.runtime, type=runtime_type, capabilities=adapter.capabilities))))
    recorder = ClaudeRunRecorder(agent_id="manager", task_id=None, command=[], cwd=str(tmp_path),
                                input_text="budget contract", launch=launch)
    event = usage_event("unpriced-model" if mode == "unpriced" else "deepseek-flash",
                        Usage(input_tokens=10, output_tokens=4, cache_read_tokens=7, cache_creation_tokens=2),
                        **({"usage_complete": False, "root_usage_complete": False} if mode == "incomplete" else {}))
    asyncio.run(recorder.record(event))
    stop = recorder.cap_stop
    if mode == "priced":
        assert stop is not None and stop.result_event[SOFT_CAP] and stop.result_event["is_error"]
        assert stop.usage.cost_usd == pytest.approx(.000008442)
    else:
        assert stop is None


def _manager_failure_dsh(tmp_path, mode):
    """Real subprocess output with progress that must never become a successful reply."""
    from fake_claude import fake_claude

    records = [{"type": "session", "sessionId": "failed-manager-session"},
               {"type": "text", "text": "progress before failure"}, _dsh_step(),
               {"type": "status", "phase": "turn_end", "reason": {"kind": "completed"}}]
    if mode == "explicit-error":
        records.append({"type": "error", "message": "stand-in model failed"})
    if mode != "no-final":
        records.append({"type": "final", "text": "untrusted final answer"})
    return fake_claude(tmp_path / "dsh-manager-failure", f'''
import json, sys
sys.stdin.buffer.read()
for record in {records!r}:
    print(json.dumps(record), flush=True)
sys.exit({7 if mode == 'nonzero-final' else 0})
''')


@pytest.mark.parametrize("mode", ["success", "no-final", "nonzero-final", "explicit-error"])
def test_manager_rejects_failed_dsh_results_instead_of_answering_with_progress(
    tmp_path, monkeypatch, agent_db, mode,
):
    from unittest.mock import AsyncMock
    from looper.agents.bus import MessageBus
    from looper.agents.message import MANAGER_ADDRESS, MessageEnvelope, MessageType, channel_address
    from looper.agents.tiers import manager

    executable = _manager_failure_dsh(tmp_path, mode)
    launch = _run_launch(tmp_path, role="manager")
    settings = replace(launch.settings, profile=replace(launch.profile,
                       runtime=replace(launch.profile.runtime, executable=executable)))
    monkeypatch.setattr(manager, "load_agent_settings", AsyncMock(return_value=settings))
    monkeypatch.setattr(manager, "load_installed", AsyncMock(return_value=launch.library))

    async def run():
        steward = manager.ManagerAgent(config=manager.ManagerConfig(work_dir=tmp_path), runtime_root=tmp_path)
        steward._bus = MessageBus(tmp_path)
        steward._clean_start = True
        envelope = MessageEnvelope.create(source=channel_address("web"), target=MANAGER_ADDRESS,
                                           message_type=MessageType.USER_MESSAGE, payload={"text": "read state"})
        try:
            if mode == "success":
                assert await steward._run_turn("read state", envelope) == "untrusted final answer"
            else:
                with pytest.raises(RuntimeError):
                    await steward._run_turn("read state", envelope)
        finally:
            await steward._claude.stop()

    asyncio.run(asyncio.wait_for(run(), 8))
    recorded = agent_db.finish_record.call_args.kwargs
    assert recorded["status"] == ("completed" if mode == "success" else "failed")
    assert recorded["session_id"] == "failed-manager-session"


@pytest.mark.parametrize("verify", [False, True])
def test_worker_dsh_runs_without_room_or_vault(tmp_path, monkeypatch, agent_db, verify):
    from unittest.mock import AsyncMock
    from looper.agents.library import AgentLibrary
    from looper.agents.message import MessageEnvelope, MessageType, planner_address, worker_address
    from looper.agents.ops.room import RoomOps
    from looper.agents.room import RoomService
    from looper.agents.tiers import worker

    final = json.dumps({"passed": True, "findings": [], "summary": "review result"} if verify else
                       {"status": "completed", "changed_files": {}, "summary": "work result"})
    executable = _headless_stand_in(tmp_path, final=final)
    launch = _run_launch(tmp_path, role="reviewer" if verify else "worker")
    settings = replace(launch.settings, profile=replace(launch.profile,
                       runtime=replace(launch.profile.runtime, executable=executable)))
    monkeypatch.setattr(worker, "load_agent_settings", AsyncMock(return_value=settings))
    monkeypatch.setattr(worker, "load_installed", AsyncMock(return_value=AgentLibrary(role=launch.role.value)))
    monkeypatch.setattr(worker.db_projects, "get_task_project", AsyncMock(return_value={
        "id": "project", "agent_room": "off", "vault": "off"}))
    pool = worker.WorkerPool(config=worker.WorkerConfig(), runtime_root=tmp_path)
    pool.room_ops = RoomOps(RoomService(), url="http://127.0.0.1:19999")
    envelope = MessageEnvelope.create(source=planner_address("test"), target=worker_address("test"),
                                      message_type=MessageType.VERIFY_REQUEST if verify else MessageType.WORK_ITEM,
                                      payload={"task_id": "step", "parent_task_id": "test",
                                               "instruction": "run", "context": {"work_dir": str(tmp_path)}})
    outcome = asyncio.run(asyncio.wait_for(pool._execute(envelope, is_verify=verify), 8))
    assert (outcome.get("passed") if verify else outcome.get("status") == "completed"), outcome
    assert outcome["summary"] == ("review result" if verify else "work result")
    assert agent_db.finish_record.call_args.kwargs["output_text"] == final
    assert (tmp_path / "seen.json").exists()


@pytest.mark.parametrize("missing", ["executable", "mcp"])
@pytest.mark.parametrize("verify", [False, True])
def test_worker_dsh_configuration_failure_returns_a_failed_step(tmp_path, monkeypatch, agent_db, missing, verify):
    from unittest.mock import AsyncMock
    from looper.agents.message import MessageEnvelope, MessageType, planner_address, worker_address
    from looper.agents.tiers import worker

    launch = _run_launch(tmp_path, role="reviewer" if verify else "worker")
    executable = str(tmp_path / "missing-dsh") if missing == "executable" else _headless_stand_in(tmp_path)
    settings = replace(launch.settings, profile=replace(launch.profile,
                       runtime=replace(launch.profile.runtime, executable=executable)))
    if missing == "mcp":
        source = Path(launch.library.mcp_config)
        config = json.loads(source.read_text())
        config["mcpServers"]["looper"]["env"].pop("LOOPER_AGENT_TOKEN")
        source.write_text(json.dumps(config))
    monkeypatch.setattr(worker, "load_agent_settings", AsyncMock(return_value=settings))
    monkeypatch.setattr(worker, "load_installed", AsyncMock(return_value=launch.library))
    envelope = MessageEnvelope.create(source=planner_address("test"), target=worker_address("test"),
                                      message_type=MessageType.VERIFY_REQUEST if verify else MessageType.WORK_ITEM,
                                      payload={"task_id": "step", "parent_task_id": "test",
                                               "instruction": "run", "context": {"work_dir": str(tmp_path)}})
    outcome = asyncio.run(asyncio.wait_for(worker.WorkerPool(config=worker.WorkerConfig(),
        runtime_root=tmp_path)._execute(envelope, is_verify=verify), 5))
    reason = "installed executable" if missing == "executable" else "LOOPER_AGENT_TOKEN"
    if verify:
        assert outcome["passed"] is False and reason in outcome["summary"]
    else:
        assert outcome["status"] == "failed" and reason in outcome["error"]
    assert any(call.kwargs["kind"] == "error" and reason in call.kwargs["summary"]
               for call in agent_db.insert_activity.call_args_list)
    agent_db.start_record.assert_not_awaited()
    assert not (tmp_path / "seen.json").exists()


@pytest.mark.parametrize("cancel", [False, True])
def test_dsh_channel_timeout_or_cancel_reaps_process(tmp_path, cancel):
    from looper.agents import proc_tree

    runtime = runtimes.adapter("dsh")
    executable = _headless_stand_in(tmp_path, hang=True)
    launch = _run_launch(tmp_path)
    command = runtime.run_command(executable, cwd=tmp_path, prompt_file=_run_prompt_file(tmp_path), launch=launch)

    async def run():
        process = await proc_tree.spawn(*command, cwd=tmp_path, env=launch.environment(),
                                        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                                        stderr=asyncio.subprocess.PIPE)
        channel = runtime.run_channel(process, launch=launch, cwd=tmp_path)
        await channel.start("test")
        ready = asyncio.Event()
        results = []

        async def read():
            async for event in channel.events():
                if event["type"] == "system":
                    ready.set()
                if event["type"] == "result":
                    results.append(event)

        task = asyncio.create_task(read())
        try:
            await asyncio.wait_for(ready.wait(), 3)
            if cancel:
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            else:
                with pytest.raises(TimeoutError):
                    await asyncio.wait_for(task, 0.05)
            assert process.returncode is not None and not results
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await proc_tree.end(process, label="test DSH")

    asyncio.run(asyncio.wait_for(run(), 5))


@pytest.mark.parametrize("version,code,help_code", [("0.2.1-rc.2", 0, 0), ("0.2.1-rc.3", 0, 0),
                                                  ("unreadable", 0, 0), ("0.2.1-rc.2", 3, 0),
                                                  ("0.2.1-rc.2", 0, 2)])
def test_dsh_probe_is_isolated_and_pins_the_verified_version(tmp_path, monkeypatch, version, code, help_code):
    from fake_claude import fake_claude

    monkeypatch.setenv("DEEPSEEK_API_KEY", "ambient-key-must-not-be-read")
    monkeypatch.setenv("DSH_HOME", str(tmp_path / "operator"))
    seen = tmp_path / "probe.jsonl"
    executable = fake_claude(tmp_path / "dsh-probe", f'''
import json, os, pathlib, sys
assert not os.environ.get('DEEPSEEK_API_KEY')
for name in ('HOME', 'DSH_HOME', 'DSH_AGENTS_HOME'):
    assert not any(pathlib.Path(os.environ[name]).iterdir())
with open({str(seen)!r}, 'a') as f:
    f.write(json.dumps({{'argv': sys.argv[1:], 'home': os.environ['HOME']}}) + '\\n')
if sys.argv[1:] == ['--version']:
    print({version!r})
    sys.exit({code})
assert sys.argv[1:] == ['--help']
print('--profile --patch --json --session-id')
sys.exit({help_code})
''')
    probe = runtimes.adapter("dsh").probe(executable)
    supported = version == "0.2.1-rc.2" and code == 0 and help_code == 0
    assert (probe.error is None) is supported
    assert bool(probe.capabilities) is supported
    if supported:
        assert probe.version == version
    calls = [json.loads(line) for line in seen.read_text().splitlines()]
    assert all(not Path(call["home"]).exists() for call in calls)
    if supported:
        assert [call["argv"] for call in calls] == [["--version"], ["--help"]]


def test_dsh_probe_uses_physical_private_paths_under_a_symlinked_temp_directory(tmp_path, monkeypatch):
    from fake_claude import fake_claude
    from looper.agents.runtimes import dsh

    physical = tmp_path / "physical"
    physical.mkdir()
    alias = tmp_path / "temp-alias"
    alias.symlink_to(physical, target_is_directory=True)
    seen = tmp_path / "probe.jsonl"
    executable = fake_claude(tmp_path / "dsh", f'''
import json, os, pathlib, sys
root = pathlib.Path.cwd()
for name in ('HOME', 'DSH_HOME', 'DSH_AGENTS_HOME', 'XDG_CONFIG_HOME', 'XDG_DATA_HOME',
             'XDG_STATE_HOME', 'XDG_CACHE_HOME', 'XDG_RUNTIME_DIR', 'XDG_CONFIG_DIRS', 'XDG_DATA_DIRS'):
    assert pathlib.Path(os.environ[name]) == root / name.lower()
with open({str(seen)!r}, 'a') as f:
    f.write(json.dumps({{'argv': sys.argv[1:], 'root': str(root)}}) + '\\n')
print('dsh 0.2.1-rc.2' if sys.argv[1:] == ['--version'] else 'dsh --profile headless --json')
''')
    # Reproduce /var -> /private/var even on hosts without that system alias.
    monkeypatch.setattr(dsh.tempfile, "tempdir", str(alias))

    probe = runtimes.adapter("dsh").probe(executable)

    assert probe.error is None
    assert probe.version == dsh.SUPPORTED_VERSION
    assert probe.capabilities == dsh.DshRuntime.capabilities
    calls = [json.loads(line) for line in seen.read_text().splitlines()]
    assert [call["argv"] for call in calls] == [["--version"], ["--help"]]
    assert all(Path(call["root"]).parent == physical.resolve() for call in calls)
    assert all(not Path(call["root"]).exists() for call in calls)


def test_dsh_probe_missing_and_timeout_are_diagnostic(tmp_path, monkeypatch):
    from looper.agents.runtimes import dsh

    assert "not found" in runtimes.adapter("dsh").probe(str(tmp_path / "missing")).error
    calls = []

    def timeout(command, **kwargs):
        calls.append(kwargs)
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    monkeypatch.setattr(dsh.subprocess, "run", timeout)
    result = runtimes.adapter("dsh").probe("dsh")
    assert result.error and not result.capabilities
    assert calls[0]["timeout"] == 10


@pytest.mark.installed
def test_installed_dsh_probe_and_patch_composition_without_boot(tmp_path):
    """Dumping verifies composition only: it neither boots the runner nor certifies MCP/model startup."""
    executable = shutil.which("dsh")
    if executable is None:
        pytest.skip("Installed DSH is required for composition validation")
    runtime = runtimes.adapter("dsh")
    probe = runtime.probe(executable)
    if probe.version != "0.2.1-rc.2":
        pytest.skip("Composition is verified only against DSH 0.2.1-rc.2")
    assert probe.error is None
    launch = _run_launch(tmp_path)
    command = runtime.run_command(executable, cwd=tmp_path, prompt_file=_run_prompt_file(tmp_path), launch=launch)
    command = [*command[:command.index("--json")], "--dump-config"]
    env = launch.environment()
    env.pop("DEEPSEEK_API_KEY", None)
    done = subprocess.run(command, cwd=tmp_path, env=env, capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    assert "looper-bootstrap.mjs" in done.stdout and "headlessStartup" in done.stdout
    assert "test-dsh-key" not in done.stdout and "test-run-token" not in done.stdout
    patch = json.loads(Path(command[command.index("--patch") + 1]).read_text())
    configs = {row["id"]: row["config"] for row in patch if "config" in row}
    disabled = {row["id"] for row in patch if row.get("disabled")}
    assert configs["agent-default-model"] == {"provider": "deepseek-official", "model": "deepseek-flash"}
    assert configs["llm-deepseek"]["baseURL"] == PROVIDER.base_url
    assert configs["tools"] == {"mode": "native"}
    assert configs["sandbox-policy"] == {"mode": "danger-full-access", "workspaceRoot": str(tmp_path)}
    assert configs["approval"] == {"policy": "never"}
    assert "@deepseek-ai/dsh-fs-sandbox" in done.stdout and "@deepseek-ai/dsh-bash-sandbox" in done.stdout
    assert "@deepseek-ai/dsh-fs-local" not in done.stdout and "@deepseek-ai/dsh-bash-local" not in done.stdout
    assert {"credentials", "llm-pi-ai", "session-log-deepseek", "plugin-package-inventory-deepseek",
            "session-telemetry-otel", "web-search-deepseek", "agent-instructions"} <= disabled


@pytest.mark.installed
def test_official_loader_activates_only_the_inserted_required_runner(tmp_path):
    """The official patch matcher cannot rename modules; its loader must select the final entry per id."""
    executable = shutil.which("dsh")
    if executable is None:
        pytest.skip("Installed DSH is required for loader validation")
    launch = _run_launch(tmp_path)
    command = runtimes.adapter("dsh").run_command(executable, cwd=tmp_path,
                                                  prompt_file=_run_prompt_file(tmp_path), launch=launch)
    patch_file = Path(command[command.index("--patch") + 1])
    (tmp_path / "original.mjs").write_text("export function apply() { throw new Error('original runner activated'); }")
    (tmp_path / "replacement.mjs").write_text("export function apply(ctx, config) { ctx.seen.push(config); }")
    done = _node(tmp_path, '''
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
import {realpathSync, readFileSync} from 'node:fs';
import {pathToFileURL} from 'node:url';
const require = createRequire(realpathSync(process.argv[2]));
const load = name => import(pathToFileURL(require.resolve(`@deepseek-ai/${name}`)));
const {Context} = await load('cordis');
const {default: Loader} = await load('cordis-plugin-loader');
const {applyEntryPatches} = await load('cordis-plugin-include');
const rows = JSON.parse(readFileSync(process.argv[3], 'utf8'));
const patch = rows.at(-1);
const source = {id: 'headless-runner', name: pathToFileURL(process.cwd() + '/original.mjs').href};
const replacement = patch.insert[0];
assert.equal(replacement.id, source.id);
assert.deepEqual(replacement.inject, ['headlessStartup']);
assert.deepEqual(replacement.config, {
  task: {__jsExpr: 'ctx.headlessStartup.task'}, sessionId: {__jsExpr: 'ctx.headlessStartup.sessionId'},
  json: {__jsExpr: 'ctx.headlessStartup.json'},
});
const warnings = [];
const rowsComposed = applyEntryPatches([source], [patch], (...args) => warnings.push(args));
assert.deepEqual(warnings, []);
// Replace the two modules with local counting plugins; all id selection, dependency and config evaluation
// still run through the actual installed loader, without starting MCP or an LLM.
rowsComposed.at(-1).name = pathToFileURL(process.cwd() + '/replacement.mjs').href;
const ctx = new Context();
ctx.seen = [];
ctx.reflect.provide('headlessStartup', {task: '-', sessionId: 'saved', json: true});
await ctx.plugin(Loader, {baseUrl: pathToFileURL(process.cwd() + '/').href}).await();
await ctx.loader.root.update(rowsComposed);
await ctx.loader.await();
await ctx.loader.store['headless-runner'].fiber.await();
assert.equal(ctx.seen.length, 1);
assert.deepEqual(ctx.seen[0], {task: '-', sessionId: 'saved', json: true});
assert.equal(Object.keys(ctx.loader.store).length, 1);
await ctx.fiber.dispose();
''', executable, patch_file)
    assert done.returncode == 0, done.stdout + done.stderr


@pytest.mark.parametrize("runtime_type", ["claude-code", "omp"])
def test_existing_worker_runtime_still_accepts_success_before_nonzero_teardown(
    tmp_path, monkeypatch, agent_db, runtime_type,
):
    """DSH's stricter exit validation is confined to its channel; existing teardown policy stays intact."""
    from fake_claude import fake_claude
    from unittest.mock import AsyncMock
    from looper.agents.message import MessageEnvelope, MessageType, planner_address, worker_address
    from looper.agents.tiers import worker

    text = json.dumps({"status": "completed", "changed_files": {}, "summary": "legacy result"})
    if runtime_type == "claude-code":
        records = [{"type": "result", "subtype": "success", "is_error": False, "result": text}]
    else:
        records = [{"type": "session", "id": "legacy-session"},
                   {"type": "message_end", "message": {"role": "assistant", "content": [{"type": "text", "text": text}],
                                                           "stopReason": "stop", "usage": {}}},
                   {"type": "agent_end", "messages": []}]
    executable = fake_claude(tmp_path / "legacy-stand-in", f'''
import json, sys
sys.stdin.readline()
for record in {records!r}:
    print(json.dumps(record), flush=True)
sys.exit(7)
''')
    launch = _run_launch(tmp_path)
    runtime = runtimes.adapter(runtime_type)
    settings = replace(launch.settings, profile=replace(launch.profile, runtime=replace(
        launch.profile.runtime, type=runtime_type, executable=executable, capabilities=runtime.capabilities)))
    monkeypatch.setattr(worker, "load_agent_settings", AsyncMock(return_value=settings))
    envelope = MessageEnvelope.create(source=planner_address("test"), target=worker_address("legacy"),
                                      message_type=MessageType.WORK_ITEM,
                                      payload={"task_id": "step", "instruction": "run", "context": {"work_dir": str(tmp_path)}})
    pool = worker.WorkerPool(config=worker.WorkerConfig(), runtime_root=tmp_path)
    outcome = asyncio.run(asyncio.wait_for(pool._execute(envelope), 5))
    assert outcome["status"] == "completed" and outcome["summary"] == "legacy result"


def _tree_bytes(directory):
    return {str(path.relative_to(directory)): path.read_bytes()
            for path in directory.rglob("*") if path.is_file()}


def _kept_dsh_run(tmp_path, *, task_id="task"):
    from looper.agents.room import run_session_dir

    runtime = runtimes.adapter("dsh")
    launch = _run_launch(tmp_path)
    executable = _resident_dsh_stand_in(tmp_path)
    prompt = _run_prompt_file(tmp_path)
    directory = run_session_dir(tmp_path, "dsh", task_id, "worker-1")
    assert directory is not None
    command = runtime.run_command(executable, cwd=tmp_path, prompt_file=prompt, launch=launch,
                                  session_dir=directory)
    done = subprocess.run(command, input="original history", text=True, capture_output=True,
                          env=launch.environment(), cwd=tmp_path, timeout=60)
    assert done.returncode == 0, done.stderr
    sid = json.loads(done.stdout.splitlines()[0])["sessionId"]
    return runtime, launch, executable, prompt, directory, command, sid


def test_dsh_kept_command_names_the_actual_managed_owner(tmp_path):
    runtime, launch, executable, prompt, directory, command, sid = _kept_dsh_run(tmp_path)
    assert runtime.kept_session_dir(command) == directory
    home = Path(next(arg.split("=", 1)[1] for arg in command if arg.startswith("DSH_HOME=")))
    assert home == directory / "dsh"
    assert json.loads((home / "opaque-test-store").read_text())[sid]["turns"] == ["original history"]
    before = _tree_bytes(directory)
    prompt.write_text("updated consult instructions")
    changed = runtime.run_command(executable, cwd=tmp_path, prompt_file=prompt, launch=launch,
                                  session_dir=directory)
    assert runtime.kept_session_dir(changed) == directory
    assert (home / "opaque-test-store").read_bytes() == before["dsh/opaque-test-store"]


@pytest.mark.parametrize("command", [[], ["dsh", "DSH_HOME=/untrusted"],
    ["env", "DSH_HOME=/untrusted/dsh", "dsh", "--json", "-"],
    ["env", "HOME=/other/home", "DSH_HOME=/untrusted/dsh", "dsh", "--patch", "/other/config", "--json", "-"]])
def test_dsh_kept_owner_rejects_commands_outside_its_layout(command):
    assert runtimes.adapter("dsh").kept_session_dir(command) is None


def test_dsh_parallel_consults_resume_opaque_copies_and_leave_owner_unchanged(tmp_path, monkeypatch):
    from looper.agents.runtimes import ReadOnly

    runtime, launch, executable, prompt, directory, command, sid = _kept_dsh_run(tmp_path)
    # This slice checks session isolation; execution guard is implemented and verified separately.
    monkeypatch.setattr(runtime, "read_only", ReadOnly.PERMISSIONS)
    source = _tree_bytes(directory)
    prompt.write_text("fresh consult system prompt")
    mcp = Path(launch.library.mcp_config)
    mcp.write_text(mcp.read_text().replace("test-run-token", "fresh-consult-token"))
    consult = replace(launch, consult=True)

    def ask(number):
        fork = tmp_path / f"fork-{number}"
        argv = runtime.consult_command(executable, cwd=tmp_path, prompt_file=prompt, launch=consult,
                                       session_id=sid, session_dir=directory, fork_dir=fork)
        assert runtime.kept_session_dir(argv) == fork
        assert argv[-3:] == ["--session-id", sid, "-"]
        done = subprocess.run(argv, input=f"consult {number}", text=True, capture_output=True,
                              env=consult.environment(), cwd=tmp_path, timeout=60)
        assert done.returncode == 0, done.stderr
        assert json.loads(done.stdout.splitlines()[0])["sessionId"] == sid
        turns = json.loads((fork / "dsh/opaque-test-store").read_text())[sid]["turns"]
        assert turns == ["original history", f"consult {number}"]
        module = Path(argv[argv.index("--patch") + 1]).with_name("looper-bootstrap.mjs").read_text()
        assert "fresh consult system prompt" in module
        assert "fresh-consult-token" not in str(_tree_bytes(fork))
        assert "fresh-consult-token" in consult.environment().values()
        return fork

    with ThreadPoolExecutor(max_workers=2) as executor:
        forks = list(executor.map(ask, (1, 2)))
    assert forks[0] != forks[1]
    assert _tree_bytes(directory) == source


@pytest.mark.parametrize("invalid", ["missing", "empty", "missing-lock", "root-link", "file-link", "directory-link", "fifo"])
def test_dsh_consult_refuses_invalid_snapshot_without_creating_a_fork(tmp_path, monkeypatch, invalid):
    from looper.agents.runtimes import ReadOnly

    runtime, launch, executable, prompt, directory, command, sid = _kept_dsh_run(tmp_path)
    monkeypatch.setattr(runtime, "read_only", ReadOnly.PERMISSIONS)
    if invalid == "missing":
        shutil.rmtree(directory)
    elif invalid == "empty":
        shutil.rmtree(directory / "dsh")
        (directory / "dsh").mkdir()
    elif invalid == "missing-lock":
        (directory / ".session.lock").unlink()
    elif invalid == "root-link":
        link = tmp_path / "linked-owner"
        link.symlink_to(directory, target_is_directory=True)
        directory = link
    elif invalid in ("file-link", "directory-link"):
        (directory / "dsh/escape").symlink_to(prompt if invalid == "file-link" else tmp_path,
                                              target_is_directory=invalid == "directory-link")
    else:
        os.mkfifo(directory / "dsh/pipe")
    fork = tmp_path / "fork"
    with pytest.raises(RuntimeUnsupported):
        runtime.consult_command(executable, cwd=tmp_path, prompt_file=prompt, launch=replace(launch, consult=True),
                                session_id=sid, session_dir=directory, fork_dir=fork)
    assert not fork.exists()


def test_dsh_consult_copy_or_launch_failure_cleans_only_its_new_fork(tmp_path, monkeypatch):
    from looper.agents.runtimes import ReadOnly, dsh

    runtime, launch, executable, prompt, directory, command, sid = _kept_dsh_run(tmp_path)
    monkeypatch.setattr(runtime, "read_only", ReadOnly.PERMISSIONS)
    before = _tree_bytes(directory)
    copytree = dsh.shutil.copytree

    def failed_copy(source, destination, **kwargs):
        Path(destination).mkdir(parents=True)
        (Path(destination) / "partial").write_bytes(b"partial")
        raise OSError("copy failed")

    for fail_copy in (True, False):
        fork = tmp_path / f"failed-{fail_copy}"
        monkeypatch.setattr(dsh.shutil, "copytree", failed_copy if fail_copy else copytree)
        with pytest.raises((RuntimeUnsupported, OSError)):
            runtime.consult_command(executable if fail_copy else str(tmp_path / "absent"), cwd=tmp_path,
                                    prompt_file=prompt, launch=replace(launch, consult=True), session_id=sid,
                                    session_dir=directory, fork_dir=fork)
        assert not fork.exists()
        assert _tree_bytes(directory) == before
    existing = tmp_path / "existing"
    existing.mkdir()
    (existing / "keep").write_bytes(b"keep")
    with pytest.raises((RuntimeUnsupported, FileExistsError)):
        runtime.consult_command(executable, cwd=tmp_path, prompt_file=prompt, launch=replace(launch, consult=True),
                                session_id=sid, session_dir=directory, fork_dir=existing)
    assert (existing / "keep").read_bytes() == b"keep"


def test_dsh_consult_refuses_an_active_source_and_accepts_it_after_exit(tmp_path, monkeypatch):
    from looper.agents.runtimes import ReadOnly

    runtime, launch, executable, prompt, directory, command, sid = _kept_dsh_run(tmp_path)
    monkeypatch.setattr(runtime, "read_only", ReadOnly.PERMISSIONS)

    async def check():
        process = await asyncio.create_subprocess_exec(*command, cwd=tmp_path, env=launch.environment(),
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        channel = runtime.run_channel(process, launch=launch, cwd=tmp_path, session_dir=directory)
        before = _tree_bytes(directory)
        try:
            with pytest.raises(RuntimeUnsupported, match="active"):
                runtime.consult_command(executable, cwd=tmp_path, prompt_file=prompt, launch=replace(launch, consult=True),
                                        session_id=sid, session_dir=directory, fork_dir=tmp_path / "active-fork")
            assert not (tmp_path / "active-fork").exists()
            assert _tree_bytes(directory) == before
            await channel.start("finished turn")
            events = [event async for event in channel.events()]
            assert _dsh_result(events)["is_error"] is False
        finally:
            if process.returncode is None:
                process.kill()
            await process.wait()
        runtime.consult_command(executable, cwd=tmp_path, prompt_file=prompt, launch=replace(launch, consult=True),
                                session_id=sid, session_dir=directory, fork_dir=tmp_path / "ended-fork")

    asyncio.run(asyncio.wait_for(check(), 8))


def test_dsh_consult_uses_permissions_without_advertising_native_read_tools(tmp_path):
    runtime, launch, executable, prompt, directory, command, sid = _kept_dsh_run(tmp_path)
    from looper.agents.runtimes import Capability, ReadOnly

    assert runtime.read_only is ReadOnly.PERMISSIONS
    assert Capability.READ_TOOLS not in runtime.capabilities
    from looper.agents.consult import RUNTIME_READ_ONLY

    # The guard is trusted, so a consult runs in the step directory. A non-git
    # smoke project has no snapshot to fall back on.
    assert runtime.type in RUNTIME_READ_ONLY
    consult = runtime.consult_command(executable, cwd=tmp_path, prompt_file=prompt, launch=replace(launch, consult=True),
                                      session_id=sid, session_dir=directory, fork_dir=tmp_path / "fork")
    assert consult[-3:] == ["--session-id", sid, "-"]
    module = Path(consult[consult.index("--patch") + 1]).with_name("looper-bootstrap.mjs").read_text()
    assert '"readOnly": true' in module


def test_dsh_consult_resumes_without_the_original_runs_mcp_library(tmp_path):
    """ConsultService supplies a fresh tool-free Launch, never the ended run's token."""
    from looper.agents.agent_settings import AgentType
    from looper.agents.library import EMPTY_LIBRARY

    runtime, launch, executable, prompt, directory, command, sid = _kept_dsh_run(tmp_path)
    source = _tree_bytes(directory)
    consult = replace(launch, library=EMPTY_LIBRARY, consult=True)
    argv = runtime.consult_command(executable, cwd=tmp_path, prompt_file=prompt, launch=consult,
                                   session_id=sid, session_dir=directory, fork_dir=tmp_path / "consult")
    env = consult.environment()
    assert not any(key.startswith("LOOPER_DSH_MCP_SECRET_") for key in env)
    assert runtime.launch_record(consult)["runtime_catalog"]["loaded"] == []
    module = Path(argv[argv.index("--patch") + 1]).with_name("looper-bootstrap.mjs").read_text()
    assert '"readOnly": true' in module and '"mcp": []' in module
    done = subprocess.run(argv, input="read-only consult", text=True, capture_output=True,
                          env=env, cwd=tmp_path, timeout=60)
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout.splitlines()[0])["sessionId"] == sid
    assert _tree_bytes(directory) == source
    with pytest.raises(RuntimeUnsupported, match="Looper stdio bridge"):
        runtime.run_command(executable, cwd=tmp_path, prompt_file=prompt,
                            launch=replace(consult, consult=False, role=AgentType.PLANNER))


@pytest.mark.parametrize("consult,missing,with_mcp", [
    (False, False, False), (True, False, False), (True, True, False), (True, False, True)])
def test_dsh_tool_free_consult_has_a_price_only_with_complete_usage(tmp_path, consult, missing, with_mcp):
    from looper.agents.library import EMPTY_LIBRARY
    from looper.agents.run_usage import run_usage

    launch = _run_launch(tmp_path)
    launch = replace(launch, consult=consult, library=launch.library if with_mcp else EMPTY_LIBRARY)
    payload = (Path(__file__).parent / "fixtures/dsh_json/success.jsonl").read_text()
    rows = [json.loads(line) for line in payload.splitlines()]
    for row in rows:
        if row.get("phase") == "step_end":
            if missing:
                row.pop("usage", None)
            else:
                row["usage"] = DSH_STEP_USAGE
    payload = "\n".join(json.dumps(row) for row in rows) + "\n"

    async def run():
        process = await asyncio.create_subprocess_exec(sys.executable, "-c",
            "import sys; sys.stdout.write(sys.argv[1])", payload,
            stdout=asyncio.subprocess.PIPE)
        events = [event async for event in launch.runtime.parse_process(process, model=launch.model, launch=launch)]
        result = _dsh_result(events)
        complete = consult and not missing and not with_mcp
        assert result["usage_complete"] is complete
        cost = run_usage(result, price=launch.price).cost_usd
        assert (cost is not None) is complete
        if cost is not None:
            assert cost > 0

    asyncio.run(run())


@pytest.mark.installed
@pytest.mark.parametrize("role", ["worker", "reviewer"])
@pytest.mark.parametrize("catalog", [False, True])
def test_dsh_installed_run_without_platform_bridge(tmp_path, installed_dsh, role, catalog):
    from looper.agents.library import AgentLibrary
    from tests.dsh_installed_stand_in import mcp_command, read_records
    from tests.test_dsh_installed import _launch, _messages, _network_only_messages, _process, _success

    with _messages([["standalone report"]], installed_dsh, "http") as server:
        launch, _ = _launch(tmp_path, installed_dsh, server, role=role)
        source = tmp_path / "catalog-only.json"
        log = tmp_path / "catalog.jsonl"
        if catalog:
            peer = mcp_command(log, ["catalog_probe"])
            source.write_text(json.dumps({"mcpServers": {"catalog": {"command": peer[0], "args": peer[1:]}}}))
        launch = replace(launch, library=AgentLibrary(role=role, mcp_config=str(source) if catalog else ""))
        command = launch.runtime.run_command(installed_dsh[0], cwd=tmp_path,
            prompt_file=tmp_path / ".local/prompts/system.md", launch=launch)
        with _process(command, launch.environment(), tmp_path) as process:
            _success(process, "standalone report")
        assert len(server.requests) == 1
        assert not (tmp_path / "mcp.jsonl").exists()
        if catalog:
            assert any(row["method"] == "tools/list" for row in read_records(log))
        _network_only_messages(installed_dsh, server)


@pytest.mark.installed
def test_dsh_installed_tool_free_consult_counts_all_requests_and_refuses_auxiliary_tools(tmp_path, installed_dsh):
    from looper.agents.library import EMPTY_LIBRARY
    from looper.agents.run_usage import run_usage
    from tests.dsh_installed_stand_in import tool
    from tests.test_dsh_installed import _launch, _messages, _network_only_messages, _process, _success

    with _messages([["original report"], [tool("bash", {"command": "touch escaped.txt"})],
                    [tool("subagent", {"prompt": "spawn a child"})], ["consult answer"]],
                   installed_dsh, "http") as server:
        launch, command = _launch(tmp_path, installed_dsh, server)
        with _process(command, launch.environment(), tmp_path) as process:
            first = _success(process, "original report")
        system = json.dumps(server.requests[0]["body"]["system"])
        assert "your final report releases the next review or decision" in system
        assert "separate read-only consult" in system
        sid = next(row["sessionId"] for row in first if row["type"] == "session")
        source = _tree_bytes(tmp_path / "owner")
        consult = replace(launch, consult=True, library=EMPTY_LIBRARY)
        runtime = consult.runtime
        command = runtime.consult_command(installed_dsh[0], cwd=tmp_path,
            prompt_file=tmp_path / ".local/prompts/system.md", launch=consult, session_id=sid,
            session_dir=tmp_path / "owner", fork_dir=tmp_path / "consult")
        patch = json.loads(Path(command[command.index("--patch") + 1]).read_text())
        assert {row["id"] for row in patch if row.get("disabled")} >= {
            "compaction-basic", "command-compact", "session-title-llm", "web-search-deepseek"}

        async def run():
            process = await asyncio.create_subprocess_exec(*command, cwd=tmp_path, env=consult.environment(),
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            stderr = asyncio.create_task(process.stderr.read())
            try:
                await runtime.write_turn(process.stdin, "[CONSULT] Answer from the finished session.")
                events = [event async for event in runtime.parse_process(process, model=consult.model, launch=consult)]
                result = _dsh_result(events)
                assert not result["is_error"], (result, await stderr)
                assert result["result"] == "consult answer" and result["usage_complete"]
                usage = run_usage(result, price=consult.price)
                assert (usage.input_tokens, usage.output_tokens, usage.cache_read_tokens,
                        usage.cache_creation_tokens) == (33, 15, 0, 0)
                assert usage.cost_usd == pytest.approx(.0000279)
                assert len([event for event in events if event["type"] == "usage"]) == 3
                denied = runtime.permission_denials(result)
                assert {call["tool"] for call in denied} == {"bash", "subagent"}
            finally:
                if process.returncode is None:
                    process.kill()
                await process.wait()
                await stderr

        # This checks consult accounting and permissions, not installed CLI startup latency.
        # Match the 120-second process watchdog used by the installed DSH helper (_finish).
        asyncio.run(asyncio.wait_for(run(), 120))
        assert len(server.requests) == 4  # one original request, three consult steps; no auxiliary calls
        assert not (tmp_path / "escaped.txt").exists()
        assert _tree_bytes(tmp_path / "owner") == source
        _network_only_messages(installed_dsh, server)


def test_dsh_failed_native_restore_cleans_the_fork_and_preserves_source(tmp_path, monkeypatch):
    from looper.agents.consult import _cleanup
    from looper.agents.runtimes import ReadOnly

    runtime, launch, executable, prompt, directory, command, sid = _kept_dsh_run(tmp_path)
    monkeypatch.setattr(runtime, "read_only", ReadOnly.PERMISSIONS)
    before = _tree_bytes(directory)
    fork = tmp_path / "failed-restore"
    readonly = replace(launch, consult=True)
    argv = runtime.consult_command(executable, cwd=tmp_path, prompt_file=prompt, launch=readonly,
                                   session_id="not-in-the-snapshot", session_dir=directory, fork_dir=fork)

    async def restore():
        process = await asyncio.create_subprocess_exec(*argv, cwd=tmp_path, env=readonly.environment(),
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            channel = runtime.run_channel(process, launch=readonly, cwd=tmp_path, session_dir=fork)
            await channel.start("question")
            events = [event async for event in channel.events()]
            result = _dsh_result(events)
            assert result["is_error"] is True
            assert not any(event.get("subtype") == "init" for event in events)
        finally:
            if process.returncode is None:
                process.kill()
            await process.wait()
            _cleanup(None, fork)

    asyncio.run(asyncio.wait_for(restore(), 8))
    assert not fork.exists()
    assert _tree_bytes(directory) == before


@pytest.mark.parametrize("verify", [False, True])
def test_finished_dsh_room_worker_keeps_its_home_and_opening_session_id(tmp_path, monkeypatch, agent_db, verify):
    from unittest.mock import AsyncMock
    from looper.agents.message import MessageEnvelope, MessageType, planner_address, worker_address
    from looper.agents.ops.room import RoomOps
    from looper.agents.room import RoomService, run_session_dir
    from looper.agents.tiers import worker

    launch = _run_launch(tmp_path, role="reviewer" if verify else "worker")
    final = json.dumps({"passed": True, "findings": [], "summary": "reviewed"} if verify else
                       {"status": "completed", "changed_files": {}, "summary": "finished"})
    executable = _headless_stand_in(tmp_path, final=final)
    settings = replace(launch.settings, profile=replace(launch.profile,
                       runtime=replace(launch.profile.runtime, executable=executable)))
    monkeypatch.setattr(worker, "load_agent_settings", AsyncMock(return_value=settings))
    monkeypatch.setattr(worker, "load_installed", AsyncMock(return_value=launch.library))
    room = RoomService()
    monkeypatch.setattr(room, "first_turn", AsyncMock(return_value="room context"))
    workers = worker.WorkerPool(config=worker.WorkerConfig(), runtime_root=tmp_path)
    workers.room_ops = RoomOps(room, url="http://127.0.0.1:19999")
    envelope = MessageEnvelope.create(source=planner_address("task"), target=worker_address("worker-1"),
        message_type=MessageType.VERIFY_REQUEST if verify else MessageType.WORK_ITEM,
        payload={"task_id": "step", "parent_task_id": "task", "step_key": "s1", "instruction": "run",
                 "context": {"work_dir": str(tmp_path)}})
    result = asyncio.run(asyncio.wait_for(workers._execute(envelope, is_verify=verify), 8))
    assert result.get("passed") is True if verify else result.get("status") == "completed"
    kept = run_session_dir(tmp_path, "dsh", "task", "worker-1")
    assert kept is not None and kept.is_dir()
    command = agent_db.start_record.call_args.kwargs["metadata"]["command"]
    assert launch.runtime.kept_session_dir(command) == kept
    seen = json.loads((tmp_path / "seen.json").read_text())
    assert Path(seen["dsh_home"]) == kept / "dsh"
    assert agent_db.finish_record.call_args.kwargs["session_id"] == "stand-in-session"


# This uses the installed official registry and tool definitions, not a guard mock.
# Only the MCP peer and downstream delegation/job executors are disposable canaries.
_DSH_GUARD_PROBE = r'''
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
import {realpathSync, writeFileSync, readFileSync, existsSync, unlinkSync} from 'node:fs';
import {pathToFileURL} from 'node:url';
const require = createRequire(realpathSync(process.argv[2]));
const load = name => import(pathToFileURL(require.resolve(`@deepseek-ai/${name}`)));
const {Context} = await load('cordis');
const {createBootstrap} = await import(process.argv[3]);
const options = JSON.parse(readFileSync(process.argv[4], 'utf8'));
const ctx = new Context();
let effects = 0;
const canary = () => { effects++; writeFileSync('escaped.txt', 'mutation'); throw Error('canary executed'); };
const provide = (name, value) => ctx.reflect.provide(name, value);
await ctx.plugin((await load('dsh-session')).default).await();
await ctx.plugin((await load('dsh-session-projection')).default).await();
provide('workingDirectory', {get() {return process.cwd();}, async ensure() {return process.cwd();}});
await ctx.plugin((await load('dsh-system-prompt')).default, {}).await();
await ctx.plugin((await load('dsh-tools')).default, {mode: 'native'}).await();
await ctx.plugin((await load('dsh-sandbox-policy')).default,
  {mode: 'danger-full-access', workspaceRoot: process.cwd()}).await();
await ctx.plugin((await load('dsh-fs-sandbox')).default, {}).await();
await ctx.plugin((await load('dsh-subprocess-local')).default, {}).await();
for (const name of ['dsh-tool-fs', 'dsh-tool-fs-search']) {
  const module = await load(name);
  await ctx.plugin(module, name === 'dsh-tool-fs-search' ? {sampleOverCapGlobResults: false} : {}).await();
}
provide('shell', {sandboxMode: 'danger-full-access', execute: canary});
provide('shellEnv', {resolve() {return {};}});
await ctx.plugin(await load('dsh-tool-bash'), {enableRunInBackground: false}).await();
provide('workflowEngine', {run: canary});
await ctx.plugin(await load('dsh-tool-workflow'), {}).await();
provide('jobs', {attachController() {}, events: {subscribe() {}}, list() {return [];}, kill: canary, output: canary});
await ctx.plugin(await load('dsh-tool-jobs'), {}).await();
provide('subagents', {resolveMaxDepth() {}, getProvider() {
  return {name: 'canary', capabilities: {}, inheritsParentContext: false};
}});
await ctx.plugin(await load('dsh-tool-subagent'), {provider: 'canary', maxDepth: 'provider-managed'}).await();
const {defineTool} = await load('dsh-tools');
for (const name of ['mcp__looper__read', 'mcp__catalog__read', 'mcp__looper__unexpected', 'future_mutator']) {
  ctx.tools.register(defineTool({name, description: 'Spoof or future tool', parameters: {},
    output: {schema: {type: 'string'}}, async execute() {canary(); return '';} }));
}
let agent;
if (options.consult) {
  const session = ctx.sessions.create('writable-worker', {meta: {cwd: process.cwd()}});
  (await load('dsh-sandbox-policy')).setSandboxMode(session, 'danger-full-access');
  agent = {id: session.id, session, ctx};
}
const call = (name, args = {}) => ctx.tools.execute({callId: `call-${name}`, name,
  arguments: args, agent, signal: new AbortController().signal});
try {
  // Positive control: the very same official write definition really creates a file before policy.
  const args = {file_path: 'control.txt', content: 'positive control'};
  const written = await call('write', args);
  assert.equal(written.isError, false, JSON.stringify(written));
  assert.equal(readFileSync('control.txt', 'utf8'), args.content);
  unlinkSync('control.txt');
  if (options.consult) {
    // Persist and restore with the official backend/reader. The saved writable mode must not win.
    await ctx.plugin(await load('dsh-session-persistence-jsonl'), {root: process.cwd() + '/native-worker'}).await();
    const handle = await ctx.sessionPersistence.create(agent.session.header);
    await handle.append(Array.from({length: agent.session.seq}, (_, seq) => agent.session.eventAt(seq)));
    await handle.close();
    const reader = await ctx.sessionPersistence.open(agent.id, 'read');
    const stored = await reader.read();
    const {Session} = await load('dsh-session');
    agent.session = Session.fromRestore(agent.id, stored.events, reader.header,
      reader.inheritedEventCount, stored.eventState);
    await reader.close();
    assert.equal(ctx.sandboxPolicy.resolve({session: agent.session}).mode, 'danger-full-access');
  }
  writeFileSync('source.txt', 'needle\n');
  const plugin = await createBootstrap(options);
  // The real runner stops before stdin/model startup; the guard and MCP must already be installed.
  await assert.rejects(async () => plugin.apply(ctx, {}), /appExit/);
  assert.ok(plugin.platformTools.size > 0);
  // A late allow listener cannot overrule the monotonic guard.
  ctx.on('tools/pre-execute', () => Promise.resolve({kind: 'allow'}));
  for (const [name, args] of [['read', {file_path: 'source.txt'}],
      ['glob', {pattern: '*.txt'}], ['grep', {pattern: 'needle'}]]) {
    const result = await call(name, args);
    assert.equal(result.isError, false, JSON.stringify({name, result}));
    assert.match(JSON.stringify(result.content), name === 'read' ? /needle/ : /source.txt/);
  }
  for (const name of plugin.platformTools) {
    const result = await call(name);
    assert.equal(result.isError, false, JSON.stringify({name, result}));
    assert.match(JSON.stringify(result.content), /platform called/);
  }
  plugin.platformTools.add('future_mutator'); // The exported set is diagnostic, not mutable authority.
  const denied = [];
  for (const [name, args] of [
      ['write', {file_path: 'control.txt', content: 'positive control'}],
      ['edit', {file_path: 'source.txt', old_string: 'needle', new_string: 'changed'}],
      ['bash', {command: 'touch escaped.txt', description: 'mutation'}],
      ['run_code', {code: 'return await tools.write({file_path:"escaped.txt",content:"x"})', description: 'mutation'}],
      ['subagent', {prompt: 'write escaped.txt'}],
      ['workflow', {script: 'return 1', meta: {name: 'mutation'}}],
      ['job_list', {}], ['job_output', {job_id: 'job'}], ['job_kill', {job_id: 'job'}],
      ['mcp__looper__read', {}], ['mcp__catalog__read', {}], ['mcp__looper__unexpected', {}],
      ['future_mutator', {}]]) {
    // Even a presentation switch must not bypass the execution guard.
    ctx.tools.defaultMode = name === 'run_code' ? 'ptc' : 'native';
    // Prove these are registered tools, so UNKNOWN_TOOL cannot masquerade as a guard pass.
    assert.ok(ctx.tools.get(name), name);
    const result = await call(name, args);
    assert.equal(result.isError, true, JSON.stringify({name, result}));
    assert.match(result.content[0].text, /LOOPER_READ_ONLY/);
    denied.push({type: 'tool_call', callId: `call-${name}`, tool: name, input: args},
      {type: 'tool_result', callId: `call-${name}`, status: 'error', result: result.content[0].text});
  }
  assert.equal(effects, 0);
  assert.equal(existsSync('control.txt'), false);
  assert.equal(existsSync('escaped.txt'), false);
  assert.equal(readFileSync('source.txt', 'utf8'), 'needle\n');
  assert.equal(existsSync('external-started.txt'), false);
  assert.ok(plugin.diagnostics.some(row => row.serverName === 'catalog' && row.status === 'omitted'));
  console.log(JSON.stringify(denied));
} finally {
  await ctx.fiber.dispose();
}
'''


@pytest.mark.installed
@pytest.mark.parametrize("consult", [False, True], ids=["planner", "worker-consult"])
def test_dsh_official_registry_refuses_mutations_before_dispatch(tmp_path, consult):
    from looper.agents.runtimes import dsh

    executable = shutil.which("dsh")
    if executable is None:
        pytest.skip("Installed DSH is required for execution guard validation")
    launch = _run_launch(tmp_path, role="worker" if consult else "planner")
    launch = replace(launch, consult=consult)
    # Use the exact platform contract recorded by this run, including optional room/vault groups.
    platform = dsh._platform_tools(launch.role.value)
    names = platform["expectedTools"]
    peer = tmp_path / "mcp-peer.py"
    peer.write_text('''import json, sys
names = json.loads(sys.argv[1])
for line in sys.stdin:
    request = json.loads(line)
    if "id" not in request:
        continue
    method = request["method"]
    result = ({"protocolVersion": "2024-11-05", "capabilities": {"tools": {}},
               "serverInfo": {"name": "disposable-platform", "version": "1"}} if method == "initialize" else
              {"tools": [{"name": name, "description": "Platform tool", "inputSchema":
                          {"type": "object", "properties": {}}} for name in names]} if method == "tools/list" else
              {"content": [{"type": "text", "text": "platform called"}]})
    print(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result}), flush=True)
''')
    external = {"serverName": "catalog", "transport": "stdio", "command": sys.executable,
                "args": ["-c", "from pathlib import Path; Path('external-started.txt').touch()"]}
    own = {"serverName": "looper", "transport": "stdio", "command": sys.executable,
           "args": [str(peer), json.dumps(names)], **platform}
    options = {"executable": executable, "text": "read-only probe", "readOnly": True,
               "consult": consult, "mcp": [external, own]}
    if consult:
        # An ended writable run's opaque snapshot is copied; only its new overlay supplies policy.
        runtime, worker, stand_in, prompt, directory, command, sid = _kept_dsh_run(tmp_path)
        before = _tree_bytes(directory)
        argv = runtime.consult_command(stand_in, cwd=tmp_path, prompt_file=prompt,
                                       launch=replace(worker, consult=True), session_id=sid,
                                       session_dir=directory, fork_dir=tmp_path / "consult-fork")
        module = Path(argv[argv.index("--patch") + 1]).with_name("looper-bootstrap.mjs").read_text()
        assert '"readOnly": true' in module
        assert _tree_bytes(directory) == before
    config = tmp_path / "probe.json"
    config.write_text(json.dumps(options))
    done = _node(tmp_path, _DSH_GUARD_PROBE, executable, Path(dsh.__file__).with_name("dsh_bootstrap.mjs").as_uri(), config)
    assert done.returncode == 0, done.stdout + done.stderr
    if consult:
        assert _tree_bytes(directory) == before
    records = json.loads(done.stdout.splitlines()[-1])
    stream = dsh.DshStream()
    for record in records:
        stream.translate(record)
    denials = runtimes.adapter("dsh").permission_denials(stream._result(0))
    assert len(denials) == len(records) // 2
    assert {d["tool"] for d in denials} >= {"write", "edit", "bash", "run_code", "subagent", "workflow"}


def test_dsh_read_only_omits_external_mcp_and_records_reason_without_its_secrets(tmp_path):
    from looper.agents.runtimes import dsh

    launch = _run_launch(tmp_path, role="planner")
    source = Path(launch.library.mcp_config)
    config = json.loads(source.read_text())
    config["mcpServers"]["catalog"] = {"command": "external-command", "env": {"TOKEN": "external-secret"}}
    source.write_text(json.dumps(config))
    translated = dsh.mcp_config(launch.library, read_only=True)
    assert [s["serverName"] for s in translated.servers] == ["looper"]
    assert "external-secret" not in translated.env.values()
    record = runtimes.adapter("dsh").launch_record(launch)
    assert record["runtime_catalog"]["skipped"] == [
        {"name": "catalog", "reason": "external MCP omitted in a read-only run"}]
    assert "external-secret" not in json.dumps(record)


def test_dsh_denials_require_a_paired_error_with_the_exact_guard_reason(tmp_path):
    from looper.agents.runtimes import dsh

    stream = dsh.DshStream()
    reason = "Error: LOOPER_READ_ONLY: tool is not allowed in a read-only run"
    for record in [
        {"type": "tool_result", "callId": "orphan", "status": "error", "result": reason},
        {"type": "tool_call", "callId": "ordinary", "tool": "bash", "input": {"command": "false"}},
        {"type": "tool_result", "callId": "ordinary", "status": "error", "result": "Error: command failed"},
        {"type": "tool_call", "callId": "ok", "tool": "read", "input": {}},
        {"type": "tool_result", "callId": "ok", "status": "completed", "result": reason},
        {"type": "tool_call", "callId": "denied", "tool": "bash", "input": {"command": "touch x"}},
        {"type": "tool_result", "callId": "denied", "status": "error", "result": reason},
        {"type": "tool_result", "callId": "denied", "status": "error", "result": reason},
    ]:
        stream.translate(record)
    runtime = runtimes.adapter("dsh")
    assert runtime.permission_denials(stream._result(0)) == [{"tool": "bash", "input": "touch x", "id": "denied"}]


def test_dsh_serve_stand_in_delivers_a_file_that_the_reviewer_actually_reads(tmp_path):
    from looper.agents.tiers.worker import judge_result
    from tests.dsh_runtime_stand_in import install, read_calls

    bin_dir, state = install(tmp_path)
    for role, text in (("worker", "Start your step"), ("reviewer", "Review the step in the Task section")):
        launch = _run_launch(tmp_path, role=role)
        command = launch.runtime.run_command(str(bin_dir / "dsh"), cwd=tmp_path,
                                             prompt_file=_run_prompt_file(tmp_path), launch=launch)
        done = subprocess.run(command, input=text, text=True, capture_output=True,
                              env=launch.environment(), cwd=tmp_path, timeout=60)
        assert done.returncode == 0, done.stderr
        result = _dsh_result(asyncio.run(_dsh_output(tmp_path, done.stdout.encode())))
        judged = judge_result(result, work_dir=tmp_path, is_verify=role == "reviewer", runtime_label="DSH")
        assert judged.get("passed") is True if role == "reviewer" else judged["status"] == "completed"
    assert (tmp_path / "delivery.txt").read_text() == "delivered by disposable DSH\n"
    calls = read_calls(state)
    assert {row["role"] for row in calls} == {"worker", "reviewer"}
    assert len({row["session"] for row in calls}) == 2


@pytest.mark.skipif(not os.environ.get("LOOPER_TEST_DATABASE_URL"),
                    reason="disposable LOOPER_TEST_DATABASE_URL is required; serve readback is untested")
def test_dsh_disposable_serve_reads_back_all_four_roles_and_resident_owners(tmp_path, monkeypatch):
    """HTTP -> real tiers -> headless processes -> MCP -> PostgreSQL -> HTTP, without a model endpoint."""
    import time
    from evals.prompt_regression.runner import fresh_database, running_server
    from tests.dsh_runtime_stand_in import install, read_calls

    bin_dir, state = install(tmp_path)
    runtime, home = tmp_path / "runtime", tmp_path / "home"
    runtime.mkdir()
    home.mkdir()
    # Never discover an installed agent or inherit a channel credential in this server.
    monkeypatch.setenv("PATH", f"{bin_dir}:/usr/bin:/bin")
    for name in ("LOOPER_TELEGRAM_BOT_TOKEN", "TELEGRAM_BOT_TOKEN", "LARK_APP_SECRET",
                 "FEISHU_APP_SECRET", "LOOPER_LARK_APP_SECRET"):
        monkeypatch.delenv(name, raising=False)
    for name in ("CODEX_HOME", "GROK_HOME", "CLAUDE_CONFIG_DIR", "DSH_HOME", "DSH_AGENTS_HOME"):
        directory = home / name.lower()
        directory.mkdir()
        monkeypatch.setenv(name, str(directory))

    def wait_for(read, predicate, *, seconds=120):
        deadline = time.monotonic() + seconds
        while True:
            value = read()
            if predicate(value):
                return value
            assert time.monotonic() < deadline, value
            time.sleep(.1)

    with fresh_database(os.environ["LOOPER_TEST_DATABASE_URL"]) as (database_url, _):
        with running_server(database_url=database_url, runtime_root=runtime, bin_dir=bin_dir,
                            home=home, log_path=tmp_path / "serve.log", startup_timeout=90) as api:
            assert not api.base_url.endswith(":8767")
            channels = api.get("/api/channel-configs")["configs"]
            assert channels and all(not channel["enabled"] for channel in channels)
            registry = api.get("/api/registry")
            detected = [row for row in registry["runtimes"] if row["type"] == "dsh"]
            assert len(detected) == 1 and detected[0]["version"] == "0.2.1-rc.2"
            api.post("/api/runtimes/rt-dsh/probe", {})
            provider = api.post("/api/providers", {
                "name": "disposable-messages", "vendor_type": "custom",
                "endpoints": [{"protocol": "anthropic-messages",
                               "base_url": "https://deepseek.example.invalid/anthropic"}],
                "api_key": "synthetic-dsh-key",
            })["provider"]["provider_id"]
            model = api.post("/api/models", {
                "provider_id": provider, "name": "deepseek-flash", "protocol": "anthropic-messages",
            })["model"]["model_id"]
            profile = api.post("/api/agent-profiles", {
                "name": "disposable-dsh", "runtime_id": "rt-dsh", "model_id": model, "effort": "high",
            })["profile"]["profile_id"]
            for role in ("manager", "planner", "worker", "reviewer"):
                api.put(f"/api/agent-settings/roles/{role}", {"profile_id": profile})
            settings = api.get("/api/agent-settings")
            assert {settings["roles"][role]["values"]["profile_id"]
                    for role in ("manager", "planner", "worker", "reviewer")} == {profile}

            # Two Manager turns exercise actual per-turn process resume and its platform bridge.
            for number in (1, 2):
                api.post("/api/chat/send", {"text": f"read disposable status {number}",
                                           "session_id": "disposable-chat", "message_id": f"message-{number}"})
                wait_for(lambda: api.post("/api/v1/operator_chat/messages", {"session_id": "disposable-chat"})["result"]["messages"],
                         lambda rows: sum(row["role"] == "assistant" for row in rows) >= number)

            tasks = []
            for number in (1, 2):
                work = tmp_path / f"work-{number}"
                work.mkdir()
                project = api.post("/api/projects", {"name": f"disposable-{number}", "work_dir": str(work)})
                task = api.post("/api/v1/tasks/create", {"requirement": "Write delivery.txt and review its contents",
                                                       "project_id": project["project_id"]})["result"]["task_id"]
                tasks.append((task, work))
                api.post("/api/v1/ops/task_control/start", {"task_id": task})
            for task, work in tasks:
                detail = wait_for(lambda: api.get(f"/api/tasks/{task}"),
                                  lambda row: row["status"] in ("completed", "failed", "stopped", "cancelled"))
                if detail["status"] != "completed":
                    (tmp_path / "task-detail.json").write_text(json.dumps(detail, default=str))
                    (tmp_path / "serve-copy.log").write_text((tmp_path / "serve.log").read_text())
                assert detail["status"] == "completed", (detail, (tmp_path / "serve.log").read_text()[-4000:])
                assert (work / "delivery.txt").read_text() == "delivered by disposable DSH\n"

            records = api.post("/api/v1/runs/list", {"limit": 100})["result"]["records"]
            assert len(records) >= 10
            persisted = []
            for row in records:
                record = api.post("/api/v1/runs/get", {"record_id": row["id"]})["result"]["record"]
                persisted.append(record)
                assert record["status"] == "completed", record
                assert record["model"] == "deepseek-flash" and record["session_id"]
                assert record["cost_source"] == "provider_price" and record["cost_usd"] is None
                assert (record["input_tokens"], record["output_tokens"], record["cache_read_tokens"],
                        record["cache_creation_tokens"]) == (10, 4, 7, 2)
                metadata = record["metadata"]
                assert metadata["profile"]["runtime"]["type"] == "dsh"
                assert metadata["profile"]["provider"]["id"] == provider
                assert metadata["profile"]["id"] == profile
                assert metadata["usage_complete"] is False and metadata["cost_basis"] == "observed"
                events = api.post("/api/v1/runs/events", {"record_id": row["id"], "limit": 1000})["result"]["events"]
                assert {event["event"]["type"] for event in events} >= {"system", "assistant", "result"}
                result = next(event["event"] for event in events if event["event"]["type"] == "result")
                assert not result["is_error"] and result["usage_complete"] is False
                if record["agent_id"].startswith(("worker-", "reviewer-")):
                    assert result["observed_cost_usd"] == pytest.approx(.000008442)
                assert "synthetic-dsh-key" not in json.dumps([record, events])

            calls = read_calls(state)
            actual_owners = {row["session"]: Path(row["home"]).parent for row in calls}
            for record in persisted:
                assert runtimes.adapter("dsh").kept_session_dir(record["metadata"]["command"]) == \
                    actual_owners[record["session_id"]]
            managers = [row for row in calls if row["role"] == "manager"]
            assert len(managers) == 2
            assert managers[0]["session"] == managers[1]["session"]
            assert [row["resumed"] for row in managers] == [False, True]
            assert all(row["tool"] == "looper_overview" and row["tool_status"] == "ok" for row in managers)
            owners = {}
            for row in calls:
                owners.setdefault(row["home"], []).append(row)
            assert len(owners) == 7
            assert len({rows[0]["session"] for rows in owners.values()}) == 7
            assert {row["session_id"] for row in persisted} == {row["session"] for row in calls}
            manager_records = [row for row in persisted if row["agent_id"] == "manager"]
            assert len(manager_records) == 2
            assert {row["session_id"] for row in manager_records} == {managers[0]["session"]}
            for task, _work in tasks:
                task_records = [row for row in persisted if row["task_id"] == task]
                planner_records = [row for row in task_records if row["agent_id"] == f"planner-{task}"]
                assert len(planner_records) >= 2
                assert len({row["session_id"] for row in planner_records}) == 1
                assert sum(row["agent_id"].startswith("worker-") for row in task_records) == 1
                assert sum(row["agent_id"].startswith("reviewer-") for row in task_records) == 1
            planners = [rows for rows in owners.values() if rows[0]["role"] == "planner"]
            assert len(planners) == 2
            for turns in planners:
                assert len(turns) >= 2 and turns[0]["resumed"] is False
                assert all(row["resumed"] and row["session"] == turns[0]["session"] for row in turns[1:])
                assert all(row["read_only"] for row in turns)
                assert all(row["tool"] == "looper_task_profiles" and row["tool_status"] == "ok" for row in turns)


@pytest.mark.parametrize("effort", ["medium", "xhigh"])
def test_profile_problem_rejects_an_effort_dsh_cannot_run(effort):
    from looper.agents.agent_settings import AgentType, profile_problem

    runtime = runtimes.adapter("dsh")
    profile = resolved_profile("dsh-test", "DSH test", RuntimeSpec("dsh-test", "DSH", "dsh", "", runtime.capabilities),
                               PROVIDER, "deepseek-flash", "high")
    assert profile_problem(profile, AgentType.MANAGER) is None
    object.__setattr__(profile, "effort", effort)  # a stored profile bypasses the constructor's check
    problem = profile_problem(profile, AgentType.MANAGER)
    assert problem is not None and "effort" in problem
