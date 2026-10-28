"""Real Node transport and installed SDK gate, without starting any sessions."""
import asyncio
import json
from pathlib import Path
import signal

import pytest

from looper.agents import proc_tree
from looper.agents.runtimes.runner.supervisor import RunnerUnavailable
from pi_runner_support import pi_supervisor, raw_runner, require_pi

pytestmark = pytest.mark.installed
OFFER = {"protocol_versions": ["0.1"], "looper_version": "test", "host_capabilities": {
    "tools_call": True, "budget_authorize": True, "bus": False, "exec_slots": False}}
CAPABILITIES = {"max_sessions": 10, "per_session_isolation": True, "per_session_credentials": True,
    "cancel": True, "steer": {"in_trace": True, "boundary": "after_tool_batch", "idle_start": False},
    "usage": {"per_request": True, "buckets": True, "reasoning_in_output": True},
    "authorize_covers": ["turn", "retry", "compaction"]}


def fake_package(tmp_path, version="1.3.0", *, ai_version="1.3.0", missing=False):
    root = tmp_path / "fake-pi"
    (root / "dist").mkdir(parents=True)
    (root / "package.json").write_text(json.dumps({
        "name": "@earendil-works/pi-coding-agent", "version": version, "type": "module"}))
    executable = root / "dist/cli.js"
    executable.write_text("")
    names = ["createAgentSession", "ModelRuntime", "SessionManager", "SettingsManager",
        "DefaultResourceLoader", "createReadToolDefinition", "createGrepToolDefinition", "createFindToolDefinition",
        "createLsToolDefinition", "createEditToolDefinition", "createWriteToolDefinition", "createBashToolDefinition"]
    (root / "dist/index.js").write_text("console.log('sdk diagnostic');\n" + "\n".join(
        f"export function {name}() {{}}" for name in names if not (missing and name == "createAgentSession")))
    paths = root / "dist/core/tools/path-utils.js"
    paths.parent.mkdir(parents=True)
    paths.write_text("export function resolveToCwd() {}\nexport function resolveReadPathAsync() {}")
    ai = root / "node_modules/@earendil-works/pi-ai"
    (ai / "dist/api").mkdir(parents=True)
    (ai / "package.json").write_text(json.dumps({"name": "@earendil-works/pi-ai",
        "version": ai_version, "type": "module"}))
    (ai / "dist/index.js").write_text("export class InMemoryCredentialStore {}\n"
        "export function createAssistantMessageEventStream() {}\nexport function isContextOverflow() {}")
    for name in ("openai-responses", "openai-completions"):
        (ai / f"dist/api/{name}.js").write_text("export function streamSimple() {}")
    return executable


def test_initialize_reports_the_gated_sdk(tmp_path):
    async def exercise():
        _, pi = require_pi()
        async with raw_runner(tmp_path, pi) as (_, send, receive):
            await send("runner/health", {}, id="l-0")
            assert (await receive())["error"]["code"] == -32600
            await send("initialize", OFFER)
            result = (await receive())["result"]
            assert result["capabilities"] == CAPABILITIES
            assert result["protocol_version"] == "0.1"
            assert result["runner"] == {"kind": "pi", "version": "0.1.0"}
            assert result["sdk"]["name"] == "@earendil-works/pi-coding-agent"
            assert result["sdk"]["version"].startswith("1.3.")
            await send("initialize", OFFER, id="l-2")
            assert (await receive())["error"]["code"] == -32600
    asyncio.run(exercise())


@pytest.mark.parametrize("case,diagnostic", [
    ("version", "1.4.0"), ("missing", "createAgentSession"), ("protocol", "0.1"),
    ("old", "1.2.9"), ("ai", "1.4.0"), ("outside", "not inside"),
])
def test_unverified_sdk_is_refused(tmp_path, case, diagnostic):
    async def exercise():
        _, installed_pi = require_pi()
        pi = installed_pi if case == "protocol" else fake_package(tmp_path,
            "1.4.0" if case == "version" else "1.2.9" if case == "old" else "1.3.0",
            ai_version="1.4.0" if case == "ai" else "1.3.0", missing=case == "missing")
        if case == "outside":
            pi = tmp_path / "outside-cli"
            pi.write_text("")
        offer = {**OFFER, "protocol_versions": ["9.9"]} if case == "protocol" else OFFER
        async with raw_runner(tmp_path, pi) as (_, send, receive):
            await send("initialize", offer)
            error = (await receive())["error"]
            assert error["code"] == -32009
            assert diagnostic in error["message"]
            assert "\n" not in error["message"]
        # A version refusal is terminal for a kind, rather than a crash restart loop.
        # For the unsupported protocol offer, connect injects that offer on the real bridge.
        async with pi_supervisor(tmp_path, pi=pi) as supervisor:
            if case == "protocol":
                from looper.agents.runtimes.runner.bridge import RunnerBridge

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
                    await supervisor.acquire("pi")
            state = supervisor._kinds["pi"]
            assert state.fatal and state.number == 1 and state.restart is None
    asyncio.run(exercise())


def test_health_and_unknown_methods(tmp_path):
    async def exercise():
        async with pi_supervisor(tmp_path) as supervisor:
            lease = await supervisor.acquire("pi")
            try:
                health = await lease.bridge.health()
                assert set(health) == {"sessions", "running", "inflight_requests", "tool_processes",
                    "rss_bytes", "event_loop_lag_ms"}
                assert all(type(value) is int and value >= 0 for value in health.values())
                assert health["sessions"] == 0
            finally:
                await lease.release()
        _, pi = require_pi()
        async with raw_runner(tmp_path, pi) as (_, send, receive):
            await send("initialize", OFFER)
            await receive()
            await send("nope/x", {"session_id": "s", "trace_id": "t"}, id="l-2")
            error = (await receive())["error"]
            assert error == {"code": -32601, "message": "method not found",
                "data": {"session_id": "s", "trace_id": "t"}}
            await send("session/new", {}, id="l-3")
            assert (await receive())["error"]["code"] == -32602
            await send("runner/health", {}, id="l-4")
            assert (await receive())["result"]["sessions"] == 0
    asyncio.run(exercise())


@pytest.mark.parametrize("end", ["eof", "sigterm"])
def test_runner_exits_cleanly(tmp_path, end):
    async def exercise():
        _, pi = require_pi()
        async with raw_runner(tmp_path, pi) as (process, send, receive):
            await send("initialize", OFFER)
            await receive()
            children = set(proc_tree.tree_of(process)) - {process.pid}
            if end == "eof":
                process.stdin.close()
            else:
                process.send_signal(signal.SIGTERM)
            assert await asyncio.wait_for(process.wait(), 5) == 0
            assert not children
            assert not proc_tree.tree_of(process)
            assert await process.stdout.read() == b""
    asyncio.run(exercise())


@pytest.mark.parametrize("frame", [b"not-json\n", b"\xff\n", b'{"jsonrpc":"1.0"}\n',
    b"x" * (4 * 1024 * 1024 + 1)])
def test_corrupt_host_frame_ends_the_runner(tmp_path, frame):
    async def exercise():
        _, pi = require_pi()
        async with raw_runner(tmp_path, pi) as (process, _, __):
            process.stdin.write(frame)
            await process.stdin.drain()
            assert await asyncio.wait_for(process.wait(), 5) == 2
            assert b"protocol violation" in await process.stderr.read()
            assert await process.stdout.read() == b""
    asyncio.run(exercise())


def test_sdk_console_output_uses_stderr(tmp_path):
    async def exercise():
        async with raw_runner(tmp_path, fake_package(tmp_path)) as (process, send, receive):
            await send("initialize", OFFER)
            assert (await receive())["result"]["sdk"]["version"] == "1.3.0"
            process.stdin.close()
            assert await asyncio.wait_for(process.wait(), 5) == 0
            assert b"sdk diagnostic" in await process.stderr.read()
            assert await process.stdout.read() == b""
    asyncio.run(exercise())


def test_transport_orders_sessions_and_settles_reverse_requests(tmp_path, monkeypatch):
    from looper.agents.runtimes.runner import pi_command

    rpc_url = (Path(__file__).resolve().parents[1] / "runners/pi/src/rpc.ts").as_uri()
    harness = tmp_path / "transport.ts"
    harness.write_text(f'''import * as rpc from {json.dumps(rpc_url)};
rpc.addSecret('test-secret');
rpc.method('initialize', () => ({{}}));
rpc.method('work', async p => {{
  if (p.wait) await new Promise(resolve => setTimeout(resolve, 20));
  return {{tag: p.tag, text: p.text}};
}}, {{session: true}});
rpc.method('reverse', p => rpc.request('tools/call', p));
rpc.method('abort', async p => {{
  const controller = new AbortController();
  const reply = rpc.request('tools/call', p, {{signal: controller.signal}});
  controller.abort();
  try {{ await reply; }} catch {{ return {{aborted: true}}; }}
}});
rpc.method('error', () => {{ throw new rpc.RpcError(-32003, 'test-secret ' + 'x'.repeat(700)); }});
rpc.method('burst', async p => {{
  await Promise.all([
    rpc.notify('test/update', p),
    rpc.notify('test/update', {{tag: 'after-large'}}),
  ]);
  return {{}};
}});
rpc.start();
''')
    monkeypatch.setattr(pi_command, "entry", lambda: harness)

    async def exercise():
        _, pi = require_pi()
        async with raw_runner(tmp_path, pi) as (process, send, receive):
            await send("initialize", {})
            await receive()
            # One chunk, two sessions: the second A request must wait for the first.
            messages = [{"jsonrpc": "2.0", "id": f"l-{i}", "method": "work", "params": p}
                for i, p in enumerate([
                    {"session_id": "a", "tag": "a1", "wait": True},
                    {"session_id": "a", "tag": "a2"}, {"session_id": "b", "tag": "b"}], 2)]
            process.stdin.write(b"".join(json.dumps(m).encode() + b"\n" for m in messages))
            await process.stdin.drain()
            assert [(await receive())["result"]["tag"] for _ in messages] == ["b", "a1", "a2"]
            await send("reverse", {"session_id": "a", "trace_id": "t"}, id="l-5")
            reverse = await receive()
            assert reverse["id"] == "r-1"
            await send({"jsonrpc": "2.0", "id": "r-1", "error": {"code": -32003, "message": "test-secret"}})
            error = (await receive())["error"]
            assert error == {"code": -32003, "message": "[redacted]",
                "data": {"session_id": "a", "trace_id": "t"}}
            await send("abort", {}, id="l-6")
            assert (await receive())["id"] == "r-2"
            assert (await receive())["result"] == {"aborted": True}
            await send({"jsonrpc": "2.0", "id": "r-2", "result": {}})
            # Independent requests may finish in either order; correlate replies by id.
            await send("work", {"text": "é" * 40000}, id="l-7")
            await send("error", {}, id="l-8")
            replies = [await receive(), await receive()]
            assert {reply["id"] for reply in replies} == {"l-7", "l-8"}
            by_id = {reply["id"]: reply for reply in replies}
            assert by_id["l-7"]["result"]["text"] == "é" * 40000
            error = by_id["l-8"]["error"]
            assert len(error["message"]) == 512 and error["message"].startswith("[redacted]")
            # Queue both writes together so the second must wait for the large frame's drain.
            await send("burst", {"text": "é" * 200000}, id="l-9")
            assert (await receive())["params"] == {"text": "é" * 200000}
            assert (await receive())["params"] == {"tag": "after-large"}
            assert (await receive()) == {"jsonrpc": "2.0", "id": "l-9", "result": {}}
            await send({"jsonrpc": "2.0", "id": "r-999", "result": {}})
            assert await asyncio.wait_for(process.wait(), 5) == 2
            assert b"never issued" in await process.stderr.read()
    asyncio.run(exercise())


def test_sigterm_waits_for_hooks_after_sdk_import(tmp_path, monkeypatch):
    from looper.agents.runtimes.runner import pi_command

    main = pi_command.entry().resolve().as_uri()
    rpc_url = (Path(__file__).resolve().parents[1] / "runners/pi/src/rpc.ts").as_uri()
    marker = tmp_path / "hook-finished"
    harness = tmp_path / "shutdown.ts"
    harness.write_text(f'''import {{onShutdown}} from {json.dumps(rpc_url)};
import {{writeFile}} from 'node:fs/promises';
onShutdown(async () => {{
  await new Promise(resolve => setTimeout(resolve, 50));
  await writeFile({json.dumps(str(marker))}, 'done');
}});
await import({json.dumps(main)});
''')
    monkeypatch.setattr(pi_command, "entry", lambda: harness)

    async def exercise():
        _, pi = require_pi()
        async with raw_runner(tmp_path, pi) as (process, send, receive):
            await send("initialize", OFFER)
            assert "result" in await receive()
            process.send_signal(signal.SIGTERM)
            assert await asyncio.wait_for(process.wait(), 5) == 0
            assert marker.read_text() == "done"
    asyncio.run(exercise())


def test_old_node_is_refused_during_initialize(tmp_path, monkeypatch):
    from looper.agents.runtimes.runner import pi_command

    main = pi_command.entry().resolve().as_uri()
    harness = tmp_path / "old-node.ts"
    harness.write_text("Object.defineProperty(process.versions, 'node', {value: '22.18.0'});\n"
        f"await import({json.dumps(main)});\n")
    monkeypatch.setattr(pi_command, "entry", lambda: harness)

    async def exercise():
        _, pi = require_pi()
        async with raw_runner(tmp_path, pi) as (_, send, receive):
            await send("initialize", OFFER)
            error = (await receive())["error"]
            assert error["code"] == -32009 and "22.18.0" in error["message"]
    asyncio.run(exercise())
