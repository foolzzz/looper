"""Opt-in, offline verification of exactly dsh 0.2.1-rc.2; no real model eval.

Run: LOOPER_TEST_INSTALLED_DSH=1 uv run --extra test python -m pytest tests/test_dsh_installed.py
Missing/mismatched installations are explicit gaps (skips), never substituted versions.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import threading
import time
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from looper.agents.agent_settings import AgentSettings, AgentType, Launch
from looper.agents.library import AgentLibrary
from looper.agents.profile_registry import LegacyProviderSpec as ProviderSpec, RuntimeSpec
from tests.runtime_model_helpers import resolved_profile
from looper.agents.runtimes import ApiProtocol, AuthKind
from looper.agents.runtimes import dsh
from tests.dsh_installed_stand_in import FetchMessages, MessagesServer, NETWORK_GUARD, mcp_command, read_records, tool


pytestmark = pytest.mark.skipif(os.environ.get("LOOPER_TEST_INSTALLED_DSH") != "1",
                                reason="Installed dsh verification requires LOOPER_TEST_INSTALLED_DSH=1")
VERSION = "0.2.1-rc.2"
KEY = "synthetic-offline-key"
LITERAL = 'Looper literal {{unknown}} / {{ secrets }} 中文 ${value}'


@pytest.fixture
def installation(tmp_path, monkeypatch):
    executable, node = shutil.which("dsh"), shutil.which("node")
    if not executable or not node:
        pytest.skip(f"GAP: installed dsh {VERSION} and Node are required; no replacement/download")
    guard = tmp_path / "network-guard.mjs"
    guard.write_text(NETWORK_GUARD)
    # Construct an allowlist from scratch. Never inspect or inherit an operator credential.
    env = {"PATH": os.pathsep.join((str(Path(node).parent), os.defpath)),
           "NODE_OPTIONS": f"--import={guard.as_uri()}", "LOCAL_NETWORK_LOG": str(tmp_path / "network.jsonl"),
           "LOCAL_MESSAGES_PORT": "0", "LANG": "en_US.UTF-8", "NO_COLOR": "1"}
    for name in ("HOME", "DSH_HOME", "DSH_AGENTS_HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME",
                 "XDG_STATE_HOME", "XDG_CACHE_HOME", "XDG_RUNTIME_DIR", "XDG_CONFIG_DIRS", "XDG_DATA_DIRS"):
        directory = tmp_path / "empty" / name.lower()
        directory.mkdir(parents=True, mode=0o700)
        env[name] = str(directory)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    from looper import child_env
    monkeypatch.setattr(child_env, "inherited", lambda: dict(env))
    version = subprocess.run([executable, "--version"], env=env, cwd=tmp_path,
                             capture_output=True, text=True, timeout=10)
    if version.returncode != 0 or version.stdout.strip().removeprefix("dsh ") != VERSION:
        pytest.skip(f"GAP: exact dsh {VERSION} unavailable (version check exit {version.returncode}); no substitution")
    help_result = subprocess.run([executable, "--help"], env=env, cwd=tmp_path,
                                 capture_output=True, text=True, timeout=10)
    assert help_result.returncode == 0, help_result.stderr
    assert "--profile" in help_result.stdout and "--dump-config" in help_result.stdout
    assert not read_records(Path(env["LOCAL_NETWORK_LOG"]))
    return executable, node, env


def _launch(tmp_path, installation, server, *, role="worker", gate=None, mode="ready"):
    executable, _, env = installation
    env["LOCAL_MESSAGES_PORT"] = str(server.port)
    runtime = dsh.DshRuntime()
    names = [] if mode == "missing-tool" else dsh._platform_tools(role)["expectedTools"]
    peer = mcp_command(tmp_path / "mcp.jsonl", names, gate=gate, mode=mode)
    source = tmp_path / "mcp.json"
    source.write_text(json.dumps({"mcpServers": {"looper": {
        "command": peer[0], "args": peer[1:],
        "env": {"LOOPER_AGENT_URL": server.url, "LOOPER_AGENT_TOKEN": "synthetic-platform-token"}}}}))
    provider = ProviderSpec("offline", "Local synthetic Messages", AuthKind.API_KEY,
                            frozenset({ApiProtocol.ANTHROPIC_MESSAGES}), base_url=server.url, api_key=KEY)
    profile = resolved_profile("offline", "Offline", RuntimeSpec("offline", "DSH", "dsh", executable,
                           runtime.capabilities), provider, "deepseek-flash", "high")
    launch = Launch(AgentSettings(profile=profile), AgentLibrary(mcp_config=str(source), role=role),
                    role=AgentType(role))
    prompt = tmp_path / ".local" / "prompts" / "system.md"
    prompt.parent.mkdir(parents=True, exist_ok=True)
    prompt.write_text(LITERAL)
    command = runtime.run_command(executable, cwd=tmp_path, prompt_file=prompt, launch=launch,
                                  session_dir=tmp_path / "owner")
    return launch, command


@contextmanager
def _messages(replies, installation, transport):
    if transport == "in-memory-fetch":
        with FetchMessages(replies, installation[2]) as peer:
            yield peer
        return
    try:
        peer = MessagesServer(replies)
    except PermissionError:
        pytest.skip("GAP: sandbox forbids loopback bind; installed HTTP success/resume path unverified")
    with peer as server:
        yield server


@contextmanager
def _process(command, env, cwd, prompt="offline first turn"):
    process = subprocess.Popen(command, env=env, cwd=cwd, text=True, stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
    try:
        process.stdin.write(prompt)
        process.stdin.close()
        process.stdin = None
        yield process
    finally:
        # Include MCP peers if startup or an assertion fails, rather than leaking a server.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.communicate(timeout=5)


def _finish(process):
    stdout, stderr = process.communicate(timeout=25)
    rows = [json.loads(line) for line in stdout.splitlines() if line.strip()]
    return process.returncode, rows, stderr


def _success(process, text):
    code, rows, stderr = _finish(process)
    assert code == 0, stderr + json.dumps(rows)
    assert any(row.get("phase") == "turn_end" and row["reason"]["kind"] == "completed" for row in rows), rows
    assert [row["text"] for row in rows if row["type"] == "final"] == [text]
    return rows


def _wait_method(log, method, process):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if any(row["method"] == method for row in read_records(log)):
            return
        if process.poll() is not None:
            pytest.fail(f"dsh exited before MCP {method}: {_finish(process)}")
        threading.Event().wait(.02)
    pytest.fail(f"MCP did not reach {method} within startup deadline")


def _network_only_messages(installation, server):
    rows = read_records(Path(installation[2]["LOCAL_NETWORK_LOG"]))
    connections = [row for row in rows if row["kind"] == "connect"]
    assert all(row["allowed"] for row in connections), rows
    if isinstance(server, FetchMessages):
        assert not connections, rows
    else:
        assert connections, rows
    assert all(row["host"] == "127.0.0.1" for row in rows if row["kind"] == "dns"), rows
    fetches = [row for row in rows if row["kind"] == "fetch"]
    assert len(fetches) == len(server.requests), rows
    assert all(row["url"] == server.url + "/v1/messages" and row["method"] == "POST" for row in fetches), rows
    assert all(row["path"] == "/v1/messages" and row["api_key"] == KEY for row in server.requests)
    # Uploads can piggyback on a Messages request; count/path checks alone miss those.
    for row in server.requests:
        assert not {"dsh_session_log", "dsh_plugin_packages"} & row["body"].keys()


def _node(tmp_path, installation, script, *args, env=None):
    return subprocess.run([installation[1], "--input-type=module", "-", installation[0], *map(str, args)],
                          input=script, cwd=tmp_path, env=env or installation[2],
                          capture_output=True, text=True, timeout=15)


def _dump(tmp_path, installation, command, env):
    done = subprocess.run([*command[:command.index("--json")], "--dump-config"],
                          cwd=tmp_path, env=env, capture_output=True, text=True, timeout=10)
    assert done.returncode == 0, done.stderr
    parsed = _node(tmp_path, installation, '''
import {createRequire} from 'node:module';
import {realpathSync} from 'node:fs';
const require = createRequire(realpathSync(process.argv[2]));
console.log(JSON.stringify(require('yaml').parse(process.argv[3])));
''', done.stdout)
    assert parsed.returncode == 0, parsed.stderr
    return json.loads(parsed.stdout)


def test_installed_dsh_dump_upload_policy_and_required_wrapper_exports(tmp_path, installation):
    launch, command = _launch(tmp_path, installation, SimpleNamespace(url="http://127.0.0.1:1", port=1))
    env = launch.environment()
    env.pop("DEEPSEEK_API_KEY")
    entries = _dump(tmp_path, installation, command, env)
    by_id = {row["id"]: row for row in entries}
    for name in ("hmr", "settings", "config-editor", "plugin-manager", "tool-plugin-manager", "credentials",
                 "authorization", "deepseek-account", "llm-deepseek-account", "llm-pi-ai", "otel",
                 "session-log-deepseek", "plugin-package-inventory-deepseek", "session-title-llm",
                 "agent-instructions", "session-telemetry-otel", "web-search-deepseek", "web-deepseek-api"):
        assert by_id[name]["disabled"] is True, name
    assert by_id["session-telemetry-otel"]["config"] == {"mode": "DISABLED"}
    assert by_id["tool-web"]["config"]["search"] is False
    assert all(row.get("disabled") is True for row in entries
               if "product-analytics" in row.get("name", ""))
    assert by_id["agent-default-model"]["config"] == {"provider": "deepseek-official", "model": "deepseek-flash"}
    assert by_id["llm-deepseek"]["config"]["baseURL"] == "http://127.0.0.1:1"
    assert by_id["tools"]["config"] == {"mode": "native"}
    runner = by_id["headless-runner"]
    assert runner["name"].endswith("looper-bootstrap.mjs")
    assert runner["inject"] == ["headlessStartup"]
    assert set(runner["config"]) == {"task", "sessionId", "json"}
    assert not by_id["headless-runner"].get("disabled", False)
    checked = _node(tmp_path, installation, '''
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
import {realpathSync} from 'node:fs';
import {pathToFileURL} from 'node:url';
const require = createRequire(realpathSync(process.argv[2]));
const load = name => import(pathToFileURL(require.resolve(`@deepseek-ai/${name}`)));
const runner = await load('dsh-headless'), mcp = await load('dsh-mcp-client');
const wrapper = await import(process.argv[3]);
assert.equal(wrapper.name, runner.name);
assert.equal(wrapper.Config, runner.Config);
assert.equal(typeof wrapper.apply, 'function');
assert.deepEqual(wrapper.inject, [...new Set([...runner.inject, ...mcp.inject, 'systemPrompt'])]);
''', runner["name"])
    assert checked.returncode == 0, checked.stderr
    assert KEY not in json.dumps(entries) and "synthetic-platform-token" not in json.dumps(entries)
    assert read_records(Path(installation[2]["LOCAL_NETWORK_LOG"])) == []


def test_installed_dsh_dump_success_does_not_hide_required_boot_failure(tmp_path, installation):
    launch, command = _launch(tmp_path, installation, SimpleNamespace(url="http://127.0.0.1:1", port=1))
    broken = tmp_path / "broken-required-runner.mjs"
    broken.write_text("export function apply() { throw Error('synthetic-required-boot-failure'); }")
    patch_path = Path(command[command.index("--patch") + 1])
    patches = json.loads(patch_path.read_text())
    patches[-1]["insert"][0]["name"] = broken.as_uri()
    patch_path.write_text(json.dumps(patches))
    _dump(tmp_path, installation, command, launch.environment())  # Composition exit 0 is a positive control.
    with _process(command, launch.environment(), tmp_path) as process:
        code, rows, stderr = _finish(process)
    assert code != 0, (rows, stderr)
    assert "synthetic-required-boot-failure" in stderr + json.dumps(rows)
    assert read_records(Path(installation[2]["LOCAL_NETWORK_LOG"])) == []


def test_installed_dsh_network_recorder_counts_and_blocks_nonlocal_attempts(tmp_path, installation):
    checked = _node(tmp_path, installation, '''
import assert from 'node:assert/strict';
import net from 'node:net';
assert.throws(() => fetch('https://telemetry.invalid/upload'), /refused fetch/);
assert.throws(() => net.connect({host: '192.0.2.1', port: 443}), /refused network/);
''')
    assert checked.returncode == 0, checked.stderr
    rows = read_records(Path(installation[2]["LOCAL_NETWORK_LOG"]))
    assert [row["kind"] for row in rows] == ["fetch", "connect"]
    assert rows[1]["allowed"] is False


def test_installed_dsh_in_memory_messages_stand_in_positive_control(tmp_path, installation):
    with FetchMessages([[tool("mcp__looper__probe", {})], ["synthetic final"]], installation[2]) as server:
        installation[2]["LOCAL_MESSAGES_PORT"] = str(server.port)
        checked = _node(tmp_path, installation, r'''
import assert from 'node:assert/strict';
for (const expected of ['tool_use', 'text']) {
  const response = await fetch('http://127.0.0.1:1/v1/messages', {
    method: 'POST', headers: {'x-api-key': 'synthetic-offline-key'},
    body: JSON.stringify({messages: [], stream: true}),
  });
  assert.equal(response.status, 200);
  const events = (await response.text()).split('\n').filter(line => line.startsWith('data: '))
    .map(line => JSON.parse(line.slice(6)));
  assert.equal(events[0].type, 'message_start');
  assert.equal(events[1].content_block.type, expected);
  assert.equal(events.at(-1).type, 'message_stop');
}
''')
        assert checked.returncode == 0, checked.stderr
        assert len(server.requests) == 2
        _network_only_messages(installation, server)


def test_installed_dsh_operator_marker_positive_control_uses_only_temporary_homes(tmp_path, installation):
    operator = tmp_path / "synthetic-operator"
    for directory in (operator / ".dsh", operator / ".agents"):
        directory.mkdir(parents=True)
        (directory / "AGENTS.md").write_text("SYNTHETIC_OPERATOR_MARKER_" + directory.name)
    script = '''
import {createRequire} from 'node:module';
import {realpathSync} from 'node:fs';
import {pathToFileURL} from 'node:url';
const require = createRequire(realpathSync(process.argv[2]));
const {loadBaselineInstructions} = await import(pathToFileURL(require.resolve('@deepseek-ai/dsh-agent-instructions')));
console.log(JSON.stringify(await loadBaselineInstructions({cwd: process.cwd(), projectRoot: process.cwd(), maxBytes: 65536})));
'''
    control = _node(tmp_path, installation, script, env={**installation[2], "HOME": str(operator),
                    "DSH_HOME": str(operator / ".dsh"), "DSH_AGENTS_HOME": str(operator / ".agents")})
    assert control.returncode == 0, control.stderr
    assert "SYNTHETIC_OPERATOR_MARKER_.dsh" in control.stdout
    assert "SYNTHETIC_OPERATOR_MARKER_.agents" in control.stdout
    isolated = _node(tmp_path, installation, script)
    assert isolated.returncode == 0, isolated.stderr
    assert "SYNTHETIC_OPERATOR_MARKER" not in isolated.stdout


@pytest.mark.parametrize("consult", [False, True], ids=["planner", "restored-worker"])
def test_installed_dsh_official_execution_guard_without_a_model(tmp_path, installation, consult):
    # Share the existing registry canaries, using this file's exact version gate,
    # empty homes, scrubbed environment, local peer and fail-closed network recorder.
    from tests.test_dsh_runtime import _DSH_GUARD_PROBE

    platform = dsh._platform_tools("worker" if consult else "planner")
    peer = mcp_command(tmp_path / "mcp.jsonl", platform["expectedTools"])
    options = {"executable": installation[0], "text": LITERAL, "readOnly": True, "consult": consult,
               "mcp": [{"serverName": "catalog", "transport": "stdio", "command": "must-not-start"},
                       {"serverName": "looper", "transport": "stdio", "command": peer[0],
                        "args": peer[1:], **platform}]}
    config = tmp_path / "guard-probe.json"
    config.write_text(json.dumps(options))
    result = _node(tmp_path, installation, _DSH_GUARD_PROBE,
                   Path(dsh.__file__).with_name("dsh_bootstrap.mjs").as_uri(), config)
    assert result.returncode == 0, result.stdout + result.stderr
    records = json.loads(result.stdout.splitlines()[-1])
    assert {row["tool"] for row in records if row["type"] == "tool_call"} >= {
        "write", "edit", "bash", "run_code", "subagent", "workflow", "job_kill"}
    assert any(row["method"] == "tools/call" for row in read_records(tmp_path / "mcp.jsonl"))
    assert read_records(Path(installation[2]["LOCAL_NETWORK_LOG"])) == []


@pytest.mark.parametrize("transport", ["http", "in-memory-fetch"])
def test_installed_dsh_bootstrap_ready_literal_prompt_and_two_persisted_turns(tmp_path, installation, transport):
    gate = tmp_path / "mcp-ready"
    name = dsh._platform_tools("worker")["expectedTools"][0]
    with _messages([[tool(f"mcp__looper__{name}", {})], ["first complete"], ["second complete"]],
                   installation, transport) as server:
        launch, command = _launch(tmp_path, installation, server, gate=gate)
        with _process(command, launch.environment(), tmp_path) as process:
            _wait_method(tmp_path / "mcp.jsonl", "tools/list", process)
            threading.Event().wait(.2)
            assert not server.requests, "Model dispatched before MCP was ready"
            gate.touch()
            first = _success(process, "first complete")
        sid = next(row["sessionId"] for row in first if row["type"] == "session")
        resumed = [*command[:-1], "--session-id", sid, "-"]
        with _process(resumed, launch.environment(), tmp_path, "offline second turn") as process:
            second = _success(process, "second complete")
        assert next(row["sessionId"] for row in second if row["type"] == "session") == sid
        assert len(server.requests) == 3
        assert LITERAL in json.dumps(server.requests[0]["body"], ensure_ascii=False)
        history = json.dumps(server.requests[2]["body"])
        assert "offline first turn" in history and "first complete" in history and "offline second turn" in history
        calls = [row for row in read_records(tmp_path / "mcp.jsonl") if row["method"] == "tools/call"]
        assert len(calls) == 1 and calls[0]["params"]["name"] == name
        assert any(row["type"] == "tool_result" and "platform called" in row.get("result", "") for row in first)
        _network_only_messages(installation, server)


@pytest.mark.parametrize("mode", ["crash", "missing-tool"])
def test_installed_dsh_required_mcp_failure_exits_nonzero_without_model_requests(tmp_path, installation, mode):
    # No listener is necessary: the preloaded recorder would count even an attempted
    # fetch/connect. No request is permitted until the required bridge is ready.
    server = SimpleNamespace(url="http://127.0.0.1:1", port=1)
    launch, command = _launch(tmp_path, installation, server, role="manager", mode=mode)
    with _process(command, launch.environment(), tmp_path) as process:
        code, rows, stderr = _finish(process)
    assert code != 0, (rows, stderr)
    assert "Required Looper MCP bridge" in stderr + json.dumps(rows)
    assert any(row["method"] == "tools/list" for row in read_records(tmp_path / "mcp.jsonl"))
    assert read_records(Path(installation[2]["LOCAL_NETWORK_LOG"])) == []


@pytest.mark.parametrize("transport", ["http", "in-memory-fetch"])
def test_installed_dsh_write_positive_control_then_resumed_read_only_guard(tmp_path, installation, transport):
    mutation = tool("write", {"file_path": "guard-canary.txt", "content": "written"})
    with _messages([[mutation], ["writable complete"], [mutation, tool("bash", {
            "description": "Disposable canary", "command": "touch escaped.txt"})], ["readonly complete"]],
                   installation, transport) as server:
        launch, command = _launch(tmp_path, installation, server)
        with _process(command, launch.environment(), tmp_path) as process:
            first = _success(process, "writable complete")
        canary = tmp_path / "guard-canary.txt"
        assert canary.read_text() == "written"  # Same tool/args actually worked before guard.
        canary.unlink()
        sid = next(row["sessionId"] for row in first if row["type"] == "session")
        prompt = tmp_path / ".local" / "prompts" / "system.md"
        readonly = replace(launch, consult=True)
        resumed = launch.runtime.run_command(installation[0], cwd=tmp_path, prompt_file=prompt,
                                             launch=readonly, session_dir=tmp_path / "owner")
        resumed = [*resumed[:-1], "--session-id", sid, "-"]
        with _process(resumed, readonly.environment(), tmp_path) as process:
            second = _success(process, "readonly complete")
        denied = [row for row in second if row["type"] == "tool_result" and row.get("status") == "error"]
        assert len(denied) == 2 and all("LOOPER_READ_ONLY" in row["result"] for row in denied), second
        assert not canary.exists() and not (tmp_path / "escaped.txt").exists()
        _network_only_messages(installation, server)


@pytest.mark.parametrize("transport", ["http", "in-memory-fetch"])
def test_installed_dsh_shell_scrubs_synthetic_secrets_and_operator_instructions(tmp_path, installation, transport):
    operator = tmp_path / "synthetic-operator"
    instructions = operator / ".agents" / "AGENTS.md"
    instructions.parent.mkdir(parents=True)
    marker = "SYNTHETIC_OPERATOR_INSTRUCTIONS_MUST_NOT_LOAD"
    instructions.write_text(marker)
    # The separate marker control verifies discovery with this same installation.
    command = "test -z \"$DEEPSEEK_API_KEY\" && test -z \"$LOOPER_AGENT_TOKEN\" && " \
              "test -z \"$SYNTHETIC_SECRET\" && printf scrubbed > scrubbed.txt"
    with _messages([[tool("bash", {"description": "Check synthetic environment", "command": command})],
                         ["scrub complete"]], installation, transport) as server:
        launch, argv = _launch(tmp_path, installation, server)
        launch = replace(launch, settings=replace(launch.settings, env=(("SYNTHETIC_SECRET", "synthetic-secret"),
                         ("HOME", str(operator)), ("DSH_AGENTS_HOME", str(operator / ".agents")))))
        with _process(argv, launch.environment(), tmp_path) as process:
            rows = _success(process, "scrub complete")
        assert (tmp_path / "scrubbed.txt").read_text() == "scrubbed", rows
        assert marker not in json.dumps(server.requests)
        assert KEY not in json.dumps(rows)
        _network_only_messages(installation, server)
