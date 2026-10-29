"""DSH exact SDK gate, isolated composition, stdio lifecycle and wheel entry."""
import asyncio
import json
from pathlib import Path
import signal
import subprocess
import zipfile

import pytest

from dsh_runner_support import OFFER, dsh_supervisor, guard_env, raw_runner, require_dsh
from looper.agents import proc_tree
from looper.agents.runtimes.runner import dsh_command
from looper.agents.runtimes.runner.bridge import RunnerBridge
from looper.agents.runtimes.runner.supervisor import RunnerUnavailable

pytestmark = pytest.mark.installed
CAPABILITIES = {"max_sessions": 20, "per_session_isolation": True, "per_session_credentials": True,
    "cancel": True, "usage": {"per_request": True, "buckets": True, "reasoning_in_output": True},
    "authorize_covers": ["turn", "compaction"]}
PACKAGES = {"dsh-launch-environment": ["createLaunchEnvironmentSnapshot"],
    "dsh-llm": ["createUserMessage", "isAgentLoopRequest"],
    "dsh-llm-deepseek": ["registerDeepSeekProvider", "resolveAdapterOptions", "catalogModelInfo"],
    "dsh-session": ["buildForkSeed"],
    "dsh-session-persistence": ["SessionPersistenceNotFoundError", "SessionAlreadyOwnedError"],
    **{name: [] for name in ("dsh-session-persistence-jsonl", "dsh-agent", "dsh-agent-loop", "dsh-tools",
                            "dsh-tool-fs", "dsh-tool-fs-search", "cordis")}}


def run(coroutine):
    asyncio.run(asyncio.wait_for(coroutine, 120))


def fake_package(tmp_path, *, version="0.2.1-rc.2", dependency="0.2.1-rc.2", missing=False, delay=False):
    root = tmp_path / "fake-dsh"
    (root / "lib").mkdir(parents=True)
    (root / "package.json").write_text(json.dumps({"name": "@deepseek-ai/dsh", "version": version,
        "type": "module", "main": "lib/index.js"}))
    (root / "lib/index.js").write_text("")
    executable = root / "lib/bin.js"
    executable.write_text("")
    (root / "lib/profile-boot.js").write_text('''
import {writeFileSync} from 'node:fs';
export async function runProfile() {
  writeFileSync('boot.started', 'yes');
''' + ("  await new Promise(resolve => setTimeout(resolve, 30000));\n" if delay else "") + '''
  const ctx = {agents: {}, llm: {}, sessionPersistence: {}, tools: {}, systemPrompt: {},
    fiber: {async dispose() {}}};
  return {ctx, shutdown: {shutdown() {process.exit(99);}}};
}
''')
    for name, exports in PACKAGES.items():
        package = root / "node_modules/@deepseek-ai" / name
        (package / "lib").mkdir(parents=True)
        (package / "package.json").write_text(json.dumps({"name": f"@deepseek-ai/{name}",
            "version": "4.0.0" if name == "cordis" else dependency if name == "dsh-llm" else "0.2.1-rc.2",
            "type": "module", "exports": {".": {"default": "./lib/index.js"}}}))
        (package / "lib/index.js").write_text("\n".join(f"export function {export}() {{}}"
            for export in exports if not (missing and export == "registerDeepSeekProvider")))
    return executable


def harness(tmp_path, monkeypatch, body):
    main = dsh_command.entry().resolve()
    script = tmp_path / "harness.ts"
    script.write_text(f'''import * as rpc from {json.dumps((main.parents[2] / 'common/rpc.ts').as_uri())};
import * as sdk from {json.dumps((main.parent / 'sdk.ts').as_uri())};
{body}
await import({json.dumps(main.as_uri())});
''')
    monkeypatch.setattr(dsh_command, "entry", lambda: script)


def test_initialize_reports_the_gated_sdk(tmp_path):
    async def exercise():
        _, installed = require_dsh()
        async with dsh_supervisor(tmp_path) as supervisor:
            lease = await supervisor.acquire("dsh")
            assert lease.bridge.capabilities == CAPABILITIES
            health = await lease.bridge.health()
            assert set(health) == {"sessions", "running", "inflight_requests", "tool_processes",
                                   "rss_bytes", "event_loop_lag_ms"}
            assert all(type(value) is int and value >= 0 for value in health.values())
            assert {key: health[key] for key in ("sessions", "running", "inflight_requests", "tool_processes")} == dict.fromkeys(
                ("sessions", "running", "inflight_requests", "tool_processes"), 0)
            await lease.release()
        async with raw_runner(tmp_path, installed) as (_, send, receive):
            await send("runner/health", id="l-0")
            assert (await receive())["error"]["code"] == -32600
            await send("initialize", OFFER)
            result = (await receive())["result"]
            assert result == {"protocol_version": "0.1", "runner": {"kind": "dsh", "version": "0.1.0"},
                "sdk": {"name": "@deepseek-ai/dsh", "version": "0.2.1-rc.2"}, "capabilities": CAPABILITIES}
            await send("initialize", OFFER, id="l-2")
            assert (await receive())["error"]["code"] == -32600
            for index, method in enumerate(("nope/x", "shutdown"), 3):
                await send(method, id=f"l-{index}")
                assert (await receive())["error"]["code"] == -32601
    run(exercise())


@pytest.mark.parametrize("case,diagnostic", [("version", "0.2.1-rc.3"), ("old", "0.2.0"),
    ("dependency", "@deepseek-ai/dsh-llm version 0.2.1-rc.1"), ("missing", "registerDeepSeekProvider"),
    ("outside", "not inside"), ("protocol", "0.1"), ("node", "22.19")])
def test_unverified_sdk_is_refused(tmp_path, monkeypatch, case, diagnostic):
    executable = fake_package(tmp_path, version="0.2.1-rc.3" if case == "version" else
        "0.2.0" if case == "old" else "0.2.1-rc.2", dependency="0.2.1-rc.1" if case == "dependency"
        else "0.2.1-rc.2", missing=case == "missing")
    if case == "outside":
        executable = tmp_path / "outside.js"
        executable.write_text("")
    if case == "node":
        harness(tmp_path, monkeypatch, "Object.defineProperty(process.versions, 'node', {value: '22.18.0'});")
    offer = {**OFFER, "protocol_versions": ["9.9"]} if case == "protocol" else OFFER

    async def exercise():
        async with raw_runner(tmp_path, executable) as (_, send, receive):
            await send("initialize", offer)
            error = (await receive())["error"]
            assert error["code"] == -32009
            assert diagnostic in error["message"]
            assert str(tmp_path) not in error["message"] and "\n" not in error["message"]
        async with dsh_supervisor(tmp_path, dsh=executable) as supervisor:
            if case == "protocol":
                async def connect(process, kind):
                    bridge = RunnerBridge(process.stdout, process.stdin, kind)
                    try:
                        await bridge.request("initialize", offer)
                    except BaseException:
                        await bridge.close()
                        raise
                    return bridge
                supervisor.connect = connect
            for _ in range(2):
                with pytest.raises(RunnerUnavailable):
                    await supervisor.acquire("dsh")
            state = supervisor._kinds["dsh"]
            assert state.fatal and state.number == 1 and state.restart is None
    run(exercise())


def test_boot_mounts_only_the_runner_composition(tmp_path, monkeypatch):
    _, installed = require_dsh()
    harness(tmp_path, monkeypatch, '''rpc.method('test/composition', async () => {
  const chunks = [];
  for await (const chunk of sdk.ctx.llm.stream({provider: 'deepseek-official', model: 'deepseek-chat', messages: []}))
    chunks.push(chunk);
  return {tools: sdk.ctx.tools.schemas().map(tool => tool.name).sort(), chunks,
    home: process.env.HOME, cwd: process.cwd()};
});''')
    home = tmp_path / "host-home"
    home.mkdir()
    sentinel = home / "unchanged.txt"
    sentinel.write_text("host home must remain unchanged")
    monkeypatch.setenv("HOME", str(home))

    async def exercise():
        async with raw_runner(tmp_path, installed) as (_, send, receive):
            await send("initialize", OFFER)
            assert "result" in await receive()
            await send("test/composition", id="l-2")
            result = (await receive())["result"]
            assert result["tools"] == ["edit", "exit_plan_mode", "glob", "grep", "read",
                                       "read_image", "todo_write", "working_directory", "write"]
            finish = result["chunks"][-1]
            assert finish["type"] == "finish" and finish["reason"]["kind"] == "error"
            assert finish["reason"]["failure"]["code"] in {"NO_ADAPTER", "LOOPER_UNATTRIBUTED"}
            root = Path(result["cwd"])
            assert Path(result["home"]).is_relative_to(root)
            assert (Path(result["home"]) / ".dsh").is_dir()
            overlay = root / "looper-overlay.json"
            assert overlay.stat().st_mode & 0o777 == 0o600
            assert not (tmp_path / "network.jsonl").exists() or not (tmp_path / "network.jsonl").read_text()
            assert list(home.iterdir()) == [sentinel]
            assert sentinel.read_text() == "host home must remain unchanged"
    run(exercise())


@pytest.mark.parametrize("end", ["eof", "sigterm"])
def test_runner_exits_cleanly(tmp_path, end):
    async def exercise():
        _, installed = require_dsh()
        async with raw_runner(tmp_path, installed) as (process, send, receive):
            await send("initialize", OFFER)
            assert "result" in await receive()
            assert set(proc_tree.tree_of(process)) == {process.pid}
            if end == "eof":
                process.stdin.close()
            else:
                process.send_signal(signal.SIGTERM)
            assert await asyncio.wait_for(process.wait(), 5) == 0
            assert not proc_tree.tree_of(process)
            assert await process.stdout.read() == b""
    run(exercise())


def test_sigterm_during_boot_exits_cleanly(tmp_path):
    executable = fake_package(tmp_path, delay=True)

    async def exercise():
        async with raw_runner(tmp_path, executable) as (process, send, _):
            await send("initialize", OFFER)
            root, = tmp_path.glob("dsh-runner-*")
            async with asyncio.timeout(10):
                while not (root / "boot.started").exists():
                    await asyncio.sleep(0.01)
            process.send_signal(signal.SIGTERM)
            assert await asyncio.wait_for(process.wait(), 5) == 0
            assert await process.stdout.read() == b""
    run(exercise())


def test_corrupt_host_frame_ends_the_runner(tmp_path):
    async def exercise():
        async with raw_runner(tmp_path, fake_package(tmp_path)) as (process, _, __):
            process.stdin.write(b"not-json\n")
            await process.stdin.drain()
            assert await asyncio.wait_for(process.wait(), 5) == 2
            assert await process.stdout.read() == b""
            assert b"protocol violation" in await process.stderr.read()
    run(exercise())


def test_sdk_console_output_uses_stderr(tmp_path, monkeypatch):
    _, installed = require_dsh()
    harness(tmp_path, monkeypatch, "rpc.method('test/log', () => { console.log('DSH SDK diagnostic'); return {}; });")

    async def exercise():
        async with raw_runner(tmp_path, installed) as (process, send, receive):
            await send("initialize", OFFER)
            assert "result" in await receive()
            await send("test/log", id="l-2")
            assert (await receive())["result"] == {}
            process.stdin.close()
            assert await asyncio.wait_for(process.wait(), 5) == 0
            assert b"DSH SDK diagnostic" in await process.stderr.read()
            assert await process.stdout.read() == b""
    run(exercise())


def test_wheel_contains_the_dsh_runner(tmp_path):
    from test_pi_packaging import build_wheel

    wheel, _ = build_wheel(tmp_path)
    prefix = "looper/agents/runtimes/runner/"
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
        assert {prefix + name for name in ("dsh_runner/package.json", "dsh_runner/src/main.ts",
                "common/package.json", "common/rpc.ts")} <= set(names)
        assert not any(name.startswith("runners/") for name in names)
        assert all(name == prefix + "dsh_runner/package.json" or
                   (name.startswith(prefix + "dsh_runner/src/") and name.endswith(".ts"))
                   for name in names if name.startswith(prefix + "dsh_runner/"))


def test_installed_wheel_launches_the_dsh_runner(tmp_path):
    from test_pi_packaging import install_wheel

    node, installed = require_dsh()
    work, python, env = install_wheel(tmp_path)
    env.update(guard_env(work))
    done = subprocess.run([str(python), "-I", "-c", '''
import asyncio, json, sys, sysconfig
from pathlib import Path
from looper.agents.runtimes.runner import dsh_command
from looper.agents.runtimes.runner.supervisor import RunnerSupervisor
assert dsh_command.entry().is_relative_to(Path(sysconfig.get_path('purelib')))
async def main():
    root = Path('wheel-runtime').absolute()
    env = dict(zip(('NODE_OPTIONS', 'LOCAL_NETWORK_LOG', 'LOCAL_MESSAGES_PORT'), sys.argv[3:]))
    handshakes = []
    async def connect(process, kind):
        bridge = await dsh_command.connect(process, kind)
        handshakes.append(bridge.capabilities)
        return bridge
    supervisor = RunnerSupervisor(root, {'dsh': dsh_command.command(sys.argv[1], sys.argv[2],
        dsh_command.state_root(root))}, session_cap=20, env={'dsh': env}, connect=connect)
    try:
        lease = await supervisor.acquire('dsh')
        assert (await lease.bridge.health())['sessions'] == 0
        await lease.release()
    finally:
        await supervisor.stop()
    print(json.dumps(handshakes[0]))
asyncio.run(asyncio.wait_for(main(), 45))
''', node, installed, env["NODE_OPTIONS"], env["LOCAL_NETWORK_LOG"], env["LOCAL_MESSAGES_PORT"]],
        cwd=work, env=env, capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stdout + done.stderr
    assert json.loads(done.stdout) == CAPABILITIES


@pytest.mark.parametrize("case", ["relative", "missing", "file", "symlink", "extra", "unknown"])
def test_invalid_runner_arguments_are_refused(tmp_path, monkeypatch, case):
    original = dsh_command.command

    def command(node, executable, state):
        args = original(node, executable, state)
        if case == "relative":
            args[-1] = "relative-state"
        elif case == "missing":
            args[-1] = str(tmp_path / "missing")
        elif case in {"file", "symlink"}:
            invalid = tmp_path / "invalid-state"
            if case == "file":
                invalid.write_text("")
            else:
                invalid.symlink_to(state, target_is_directory=True)
            args[-1] = str(invalid)
        elif case == "extra":
            args.append("--extra")
        else:
            args[2] = "--unknown"
        return args

    monkeypatch.setattr(dsh_command, "command", command)

    async def exercise():
        async with raw_runner(tmp_path, fake_package(tmp_path)) as (process, _, __):
            assert await asyncio.wait_for(process.wait(), 5) == 2
            assert await process.stdout.read() == b""
            assert len((await process.stderr.read()).splitlines()) == 1
    run(exercise())


@pytest.mark.parametrize("capability", ["tools_call", "budget_authorize"])
def test_host_capabilities_are_required(tmp_path, capability):
    async def exercise():
        async with raw_runner(tmp_path, fake_package(tmp_path)) as (_, send, receive):
            await send("initialize", {**OFFER, "host_capabilities": {
                **OFFER["host_capabilities"], capability: False}})
            assert (await receive())["error"]["code"] == -32602
    run(exercise())


def test_state_root_is_private_and_shared_across_generations(tmp_path):
    root = tmp_path / "new-runtime"
    state = dsh_command.state_root(root)
    assert state == root / "runners/state/dsh-native"
    marker = state / "persisted-session.jsonl"
    marker.write_text("persisted")
    assert dsh_command.state_root(root) == state and marker.read_text() == "persisted"
    for path in (root, root / "runners", root / "runners/state", state):
        assert path.stat().st_mode & 0o777 == 0o700


@pytest.mark.parametrize("part", ["runtime", "runners", "state", "dsh-native"])
@pytest.mark.parametrize("kind", ["file", "symlink"])
def test_state_root_refuses_unsafe_directories(tmp_path, part, kind):
    root = tmp_path / "runtime"
    target = root / "runners/state/dsh-native"
    invalid = next(path for path in (root, *target.parents[:2], target) if path.name == part)
    invalid.parent.mkdir(parents=True, exist_ok=True)
    if kind == "file":
        invalid.write_text("do not replace")
    else:
        outside = tmp_path / "outside"
        outside.mkdir()
        invalid.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="directory"):
        dsh_command.state_root(root)


def test_command_requires_absolute_node(tmp_path):
    from looper.agents.runtimes import RuntimeUnsupported

    with pytest.raises(RuntimeUnsupported, match="absolute Node"):
        dsh_command.command("node", "dsh", tmp_path)


@pytest.mark.parametrize("fail", [False, True])
def test_connect_uses_boot_timeout_and_closes_on_failure(monkeypatch, fail):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    bridge = SimpleNamespace(initialize=AsyncMock(side_effect=ValueError("refused") if fail else None),
                             close=AsyncMock(), control_timeout=30)
    calls = []

    def create(stdout, stdin, kind, *, control_timeout):
        calls.append((stdout, stdin, kind, control_timeout))
        return bridge

    monkeypatch.setattr(dsh_command, "RunnerBridge", create)

    async def exercise():
        process = SimpleNamespace(stdout=object(), stdin=object())
        if fail:
            with pytest.raises(ValueError, match="refused"):
                await dsh_command.connect(process, "dsh")
            bridge.close.assert_awaited_once()
        else:
            assert await dsh_command.connect(process, "dsh") is bridge
            assert bridge.control_timeout == 10
            bridge.close.assert_not_awaited()
        assert calls == [(process.stdout, process.stdin, "dsh", 30)]
        bridge.initialize.assert_awaited_once_with(permitted=("turn", "compaction"))
    run(exercise())


@pytest.mark.parametrize("end", ["eof", "sigterm"])
def test_runner_exits_before_initialize(tmp_path, end):
    async def exercise():
        async with raw_runner(tmp_path, fake_package(tmp_path)) as (process, send, receive):
            await send("runner/health")
            assert (await receive())["error"]["code"] == -32600
            if end == "eof":
                process.stdin.close()
            else:
                process.send_signal(signal.SIGTERM)
            assert await asyncio.wait_for(process.wait(), 5) == 0
            assert not proc_tree.tree_of(process)
    run(exercise())


def test_entry_reports_an_incomplete_installation(tmp_path, monkeypatch):
    from looper.agents.runtimes import RuntimeUnsupported

    monkeypatch.setattr(dsh_command, "files", lambda _: tmp_path)
    with pytest.raises(RuntimeUnsupported, match="DSH runner is missing"):
        dsh_command.entry()


@pytest.mark.parametrize("failure", ["exception", "missing-service"])
def test_composition_failure_is_retryable_and_scrubbed(tmp_path, failure):
    executable = fake_package(tmp_path)
    profile = executable.parent / "profile-boot.js"
    profile.write_text("export async function runProfile() {" + (
        "throw Error('/private/secret/composition');" if failure == "exception" else
        "return {ctx: {agents: {}, fiber: {async dispose() {}}}};"
    ) + "}")

    async def exercise():
        async with raw_runner(tmp_path, executable) as (_, send, receive):
            await send("initialize", OFFER)
            assert (await receive())["error"] == {"code": -32603,
                "message": "DSH composition failed to boot", "data": {}}
        async with dsh_supervisor(tmp_path, dsh=executable) as supervisor:
            with pytest.raises(RunnerUnavailable):
                await supervisor.acquire("dsh")
            assert not supervisor._kinds["dsh"].fatal
    run(exercise())


def test_runner_support_preserves_guard_endpoint_and_preloads(tmp_path, monkeypatch):
    harness(tmp_path, monkeypatch, '''rpc.method('test/network', async () => {
  try { await fetch('http://127.0.0.1:1/guard'); } catch {}
  return {port: process.env.LOCAL_MESSAGES_PORT, options: process.env.NODE_OPTIONS};
});''')
    env = guard_env(tmp_path, 12345)
    env["LOCAL_NETWORK_LOG"] = str(tmp_path / "custom-network.jsonl")
    env["NODE_OPTIONS"] += " --no-warnings"

    async def exercise():
        async with raw_runner(tmp_path, fake_package(tmp_path), env=env) as (_, send, receive):
            await send("initialize", OFFER)
            assert "result" in await receive()
            await send("test/network", id="l-2")
            result = (await receive())["result"]
            assert result["port"] == "12345" and result["options"] == env["NODE_OPTIONS"]
            records = [json.loads(line) for line in Path(env["LOCAL_NETWORK_LOG"]).read_text().splitlines()]
            assert len(records) == 1 and records[0]["url"] == "http://127.0.0.1:1/guard"
            assert not (tmp_path / "network.jsonl").exists()
    run(exercise())
