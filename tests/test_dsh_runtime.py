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
from looper.agents.profile_registry import CatalogModel, ProviderSpec, RegistryError, RuntimeSpec, check_pairing
from looper.agents.runtimes import ApiProtocol, AuthKind, RuntimeUnsupported


MODELS = ("deepseek-flash", "deepseek-flash[1m]", "deepseek-v4-pro", "deepseek-v4-pro[1m]")
PROVIDER = ProviderSpec("gateway", "Messages gateway", AuthKind.API_KEY,
                        frozenset({ApiProtocol.ANTHROPIC_MESSAGES}),
                        base_url="https://gateway.example/anthropic/v1/", api_key="test-dsh-key")


def _register_prompt_runtime(monkeypatch):
    """Use the executable adapter's prompt hooks in the existing preview checks."""
    from looper.agents.runtimes import dsh

    runtime = dsh.DshRuntime()
    monkeypatch.setitem(runtimes.ADAPTERS, "dsh", runtime)
    return runtime


def _prompt_launch(monkeypatch, custom=""):
    from looper.agents.agent_settings import AgentSettings, AgentType, Launch
    from looper.agents.profile_registry import BUILTIN_PROFILE

    runtime = _register_prompt_runtime(monkeypatch)
    profile = replace(BUILTIN_PROFILE, custom_prompt=custom, model="deepseek-flash", provider=PROVIDER,
                      runtime=replace(BUILTIN_PROFILE.runtime, type=runtime.type))
    return Launch(AgentSettings(profile=profile), role=AgentType.WORKER)


def _node(tmp_path, script, *args):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required for DSH bootstrap validation")
    return subprocess.run([node, "--input-type=module", "-", *map(str, args)], input=script,
                          cwd=tmp_path, capture_output=True, text=True, timeout=20)


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


@pytest.mark.parametrize("model", MODELS)
@pytest.mark.parametrize("effort", [None, "", "low", "high", "max"])
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
    assert env[catalog["headerRefs"]["Authorization"]] == "Bearer catalog-secret"
    assert all("SECRET" in reference for reference in translated.env)
    for secret in ("fresh-run-token", "catalog-secret"):
        assert secret not in repr(translated)
        assert all(secret not in body for body in files.values())
    source.write_text(source.read_text().replace("fresh-run-token", "rotated-run-token"))
    updated = dsh.environment(launch, home)
    assert updated[own["envRefs"]["LOOPER_AGENT_TOKEN"]] == "rotated-run-token"
    assert "fresh-run-token" not in updated.values()


def test_dsh_mcp_requires_platform_bridge_even_without_catalog(tmp_path, monkeypatch):
    from looper.agents.library import AgentLibrary
    from looper.agents.runtimes import dsh

    with pytest.raises(RuntimeUnsupported, match="Looper stdio bridge"):
        dsh.mcp_config(AgentLibrary(role="planner"))


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


@pytest.mark.parametrize("raw_usage,truncated,missing", [
    (None, False, "input_tokens"),
    ({}, False, "output_tokens"),
    ("invalid", False, "input_tokens"),
    (True, False, "input_tokens"),
    ({key: value for key, value in DSH_STEP_USAGE.items() if key != "cacheReadTokens"}, False, "cache_read_tokens"),
    ({key: value for key, value in DSH_STEP_USAGE.items() if key != "cacheWriteTokens"}, False,
     "cache_creation_tokens"),
    (DSH_STEP_USAGE, True, None),
    *[({**DSH_STEP_USAGE, key: invalid}, False, name)
      for key, name in (("inputTokens", "input_tokens"), ("outputTokens", "output_tokens"),
                        ("cacheReadTokens", "cache_read_tokens"), ("cacheWriteTokens", "cache_creation_tokens"))
      for invalid in (True, False, -1, "10", 1.5, None)],
    *[({**DSH_STEP_USAGE, key: invalid}, False, None)
      for key in ("totalTokens", "reasoningTokens") for invalid in (True, -1, "10")],
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


def _run_launch(tmp_path, *, budget=None, role="worker"):
    from looper.agents.agent_settings import AgentSettings, AgentType, Launch
    from looper.agents.library import AgentLibrary
    from looper.agents.ops.service import server_config
    from looper.agents.profile_registry import AgentProfile

    runtime = runtimes.adapter("dsh")
    source = tmp_path / "mcp.json"
    source.write_text(json.dumps({"mcpServers": {
        "looper": server_config("http://127.0.0.1:19999", "test-run-token"),
    }}))
    profile = AgentProfile("dsh-test", "DSH test", RuntimeSpec("dsh-test", "DSH", "dsh", "", runtime.capabilities),
                           PROVIDER, "deepseek-flash", "high")
    return Launch(AgentSettings(profile=profile, max_budget_usd=budget),
                  AgentLibrary(mcp_config=str(source), role=role), role=AgentType(role))


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


def test_dsh_is_registered_with_only_implemented_capabilities():
    from looper.agents.runtimes import Capability, ResidentTransport
    from looper.agents.runtimes.dsh import DshRuntime

    runtime = runtimes.adapter("dsh")
    assert isinstance(runtime, DshRuntime)
    assert runtime.default_executable == "dsh"
    assert runtime.capabilities == frozenset({Capability.RUN, Capability.MCP})
    assert not runtime.run_input and not runtime.can_replace_system_prompt
    assert runtime.resident_transport is ResidentTransport.PER_TURN
    with pytest.raises(RuntimeUnsupported, match="resident"):
        runtime.resident_session("owner")
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
        channel = runtime.run_channel(process, launch=launch, cwd=tmp_path)
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
    assert Path(seen["home"]).is_relative_to(tmp_path / ".local" / "dsh" / "owners")
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
    done = subprocess.run(command, cwd=tmp_path, env=env, capture_output=True, text=True, timeout=10)
    assert done.returncode == 0, done.stderr
    assert "looper-bootstrap.mjs" in done.stdout and "headlessStartup" in done.stdout
    assert "test-dsh-key" not in done.stdout and "test-run-token" not in done.stdout
    patch = json.loads(Path(command[command.index("--patch") + 1]).read_text())
    configs = {row["id"]: row["config"] for row in patch if "config" in row}
    disabled = {row["id"] for row in patch if row.get("disabled")}
    assert configs["agent-default-model"] == {"provider": "deepseek-official", "model": "deepseek-flash"}
    assert configs["llm-deepseek"]["baseURL"] == PROVIDER.base_url
    assert configs["tools"] == {"mode": "native"}
    assert {"credentials", "llm-pi-ai", "session-log-deepseek", "plugin-package-inventory-deepseek",
            "session-telemetry-otel", "web-search-deepseek", "agent-instructions"} <= disabled


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
