"""Real DSH subprocess regressions for provider accounting and turn settlement."""
import asyncio
import json
import subprocess
import time

import pytest

import dsh_installed_stand_in as peer
from dsh_runner_support import guard_env, raw_runner, require_dsh
from looper.agents.runtimes.runner import dsh_command
from test_dsh_runner import harness
from test_dsh_runner_turns import Client, assert_accounted, params_for, run

pytestmark = pytest.mark.installed


def rewrite_events(monkeypatch, transform):
    original = peer.messages_wire

    def wire(reply, index):
        events = [json.loads(frame.split("data: ", 1)[1])
                  for frame in original(reply, index).strip().split("\n\n")]
        return "".join(f"event: {event['type']}\ndata: {json.dumps(event)}\n\n"
                       for event in transform(events))

    monkeypatch.setattr(peer, "messages_wire", wire)


@pytest.mark.parametrize("eof_delay", [0, 1], ids=["immediate-eof", "delayed-eof"])
@pytest.mark.parametrize("output_source", ["message_delta", "message_stop"])
def test_terminal_usage_completes_before_http_eof(tmp_path, monkeypatch, eof_delay, output_source):
    if output_source == "message_stop":
        def transform(events):
            events[-1]["usage"] = events[-2].pop("usage")
            return events
        rewrite_events(monkeypatch, transform)

    async def exercise():
        _, installed = require_dsh()
        with peer.MessagesServer([peer.Reply(["Provider response"], eof_delay=eof_delay)]) as server:
            async with raw_runner(tmp_path, installed, env=guard_env(tmp_path, server.port)) as (process, send, receive):
                client = Client(send, receive)
                await client.start()
                sid = (await client.call("session/new", params_for(tmp_path, server)))["session_id"]
                await client.prompt(sid)
                result = await client.settled(sid)
                end, = assert_accounted(client, sid)
                assert result["outcome"] == "completed" and result["final_text"] == "Provider response"
                assert end["status"] == "completed"
                assert (end["input_uncached"], end["output"]) == (11, 5)
                assert len(server.requests) == 1
                process.stdin.close()
                assert await asyncio.wait_for(process.wait(), 5) == 0
                diagnostics = (await process.stderr.read()).decode()
                assert end["complete"] and result["usage_complete"], (end, diagnostics)
                assert "usage_incomplete" not in diagnostics

    run(exercise())


@pytest.mark.parametrize("case", ["drained-without-stop", "dropped-before-stop", "truncated-rejection"])
def test_incomplete_requests_diagnose_missing_conditions(tmp_path, monkeypatch, case):
    if case == "drained-without-stop":
        rewrite_events(monkeypatch, lambda events: events[:-1])

    async def exercise():
        _, installed = require_dsh()
        reply = peer.Reply(["Partial response"], cut=case != "drained-without-stop",
                           status=401 if case == "truncated-rejection" else 200)
        # A truncated HTTP body is normalized by the SDK to TRANSPORT.
        count = 4 if case == "truncated-rejection" else 1
        with peer.MessagesServer([reply] * count) as server:
            async with raw_runner(tmp_path, installed, env=guard_env(tmp_path, server.port)) as (process, send, receive):
                client = Client(send, receive)
                await client.start()
                sid = (await client.call("session/new", params_for(tmp_path, server)))["session_id"]
                await client.prompt(sid)
                result = await client.settled(sid)
                ends = assert_accounted(client, sid, count=count)
                assert all(not end["complete"] for end in ends) and not result["usage_complete"]
                assert len(server.requests) == count
                process.stdin.close()
                assert await asyncio.wait_for(process.wait(), 5) == 0
                diagnostics = (await process.stderr.read()).decode()
                lines = [line for line in diagnostics.splitlines() if line.startswith("usage_incomplete ")]
                assert len(lines) == count
                for end, line in zip(ends, lines):
                    assert f"request_id={end['request_id']}" in line
                    assert "message_stop=false" in line and "terminal_usage=false" in line
                    missing = line.split("missing=", 1)[1].split()[0].split(",")
                    assert {"message_stop", "terminal_usage"} <= set(missing)
                    if case == "drained-without-stop":
                        assert "body_done=true" in line
                    else:
                        assert "body_done=false" in line and "body_drained_on_error" in missing
                assert "Partial response" not in diagnostics and "Disposable prompt" not in diagnostics

    run(exercise())


@pytest.mark.parametrize("counts", [{}, {"input_tokens": 0}, {"output_tokens": 0},
    {"input_tokens": 0, "output_tokens": 0}, {"input_tokens": 0, "interim_output_tokens": 2}])
def test_terminal_usage_requires_provider_counts(tmp_path, monkeypatch, counts):
    def transform(events):
        events[0]["message"]["usage"] = {"output_tokens": 0,
            **{k: v for k, v in counts.items() if k == "input_tokens"}}
        events[-2]["usage"] = {k: v for k, v in counts.items() if k == "output_tokens"}
        events[-1]["usage"] = {}
        if "interim_output_tokens" in counts:
            events.insert(-2, {"type": "message_delta", "delta": {},
                              "usage": {"output_tokens": counts["interim_output_tokens"]}})
        return events

    rewrite_events(monkeypatch, transform)

    async def exercise():
        _, installed = require_dsh()
        with peer.MessagesServer([peer.Reply(["Provider response"], eof_delay=1)]) as server:
            async with raw_runner(tmp_path, installed, env=guard_env(tmp_path, server.port)) as (process, send, receive):
                client = Client(send, receive)
                await client.start()
                sid = (await client.call("session/new", params_for(tmp_path, server)))["session_id"]
                await client.prompt(sid)
                result = await client.settled(sid)
                end, = assert_accounted(client, sid)
                complete = {"input_tokens", "output_tokens"} <= counts.keys()
                assert result["outcome"] == "completed" and result["final_text"] == "Provider response"
                assert end["complete"] is complete and result["usage_complete"] is complete
                assert end["input_uncached"] == 0
                assert end["output"] == counts.get("output_tokens", counts.get("interim_output_tokens", 0))
                assert len(server.requests) == 1
                process.stdin.close()
                assert await asyncio.wait_for(process.wait(), 5) == 0
                diagnostics = (await process.stderr.read()).decode()
                if not complete:
                    assert "missing=terminal_usage" in diagnostics and "message_stop=true" in diagnostics
                else:
                    assert "usage_incomplete" not in diagnostics

    run(exercise())


@pytest.mark.parametrize("error_type,kind", [("authentication_error", "PROVIDER_AUTH"),
    ("rate_limit_error", "PROVIDER_UNAVAILABLE"), ("api_error", "PROVIDER_UNAVAILABLE")])
def test_in_band_provider_errors_have_protocol_types(tmp_path, monkeypatch, error_type, kind):
    rewrite_events(monkeypatch, lambda events: events[:1] + [{"type": "error", "error": {
        "type": error_type, "message": "Synthetic stream failure"}}])

    async def exercise():
        _, installed = require_dsh()
        count = 1 if error_type == "authentication_error" else 4
        with peer.MessagesServer([["unused"]] * count) as server:
            async with raw_runner(tmp_path, installed, env=guard_env(tmp_path, server.port)) as (_, send, receive):
                client = Client(send, receive)
                await client.start()
                sid = (await client.call("session/new", params_for(tmp_path, server)))["session_id"]
                await client.prompt(sid)
                result = await client.settled(sid)
                ends = assert_accounted(client, sid, count=count)
                assert result["outcome"] == "failed" and result["error"]["kind"] == kind
                complete = error_type != "authentication_error"
                assert all(end["status"] == "error" and end["complete"] is complete for end in ends)
                assert result["usage_complete"] is complete
                if complete:
                    assert all([end[k] for k in ("input_uncached", "output", "cache_read", "cache_write")]
                               == [0] * 4 for end in ends)
                assert len(server.requests) == count

    run(exercise())


def test_settled_assistant_is_already_on_disk_and_survives_kill(tmp_path, monkeypatch):
    # Keep the writer's batch timer and the SDK's incidental step checkpoints
    # from hiding a missing runner barrier. The real writer still buffers,
    # compresses and fsyncs through sessionPersistence.flush().
    harness(tmp_path, monkeypatch, """rpc.method('test/hold-batching', () => {
  const timer = globalThis.setTimeout;
  globalThis.setTimeout = (callback, ms, ...args) => timer(callback, ms === 200 ? 30000 : ms, ...args);
  sdk.ctx.sessions.flush = async () => true;
  return {};
});""")

    async def exercise():
        node, installed = require_dsh()
        state = dsh_command.state_root(tmp_path / "durable-runtime")
        answer = "Assistant answer must survive immediate termination"
        with peer.MessagesServer([[answer], ["Resumed answer"]]) as server:
            env = guard_env(tmp_path, server.port)
            async with raw_runner(tmp_path, installed, env=env, state_root=state) as (process, send, receive):
                client = Client(send, receive)
                await client.start()
                params = params_for(tmp_path, server)
                sid = (await client.call("session/new", params))["session_id"]
                await client.call("test/hold-batching")
                await client.prompt(sid)
                assert (await client.settled(sid))["final_text"] == answer
                # Read immediately, before any close, subsequent RPC or timer wait.
                snapshots = [path.read_bytes() for path in state.rglob("*.jsonl.zstd")]
                process.kill()
                assert await asyncio.wait_for(process.wait(), 5) < 0
                assert snapshots, "positive control: a session log was created"
                # Each durable append is its own Zstd frame; Node decodes one
                # frame per call, so inspect the whole captured generation.
                decode = """let data = require('node:fs').readFileSync(0);
while (data.length) {
  const {buffer, engine} = require('node:zlib').zstdDecompressSync(data, {info: true});
  process.stdout.write(buffer);
  data = data.subarray(engine.bytesWritten);
}"""
                logs = [subprocess.run([node, "-e", decode],
                    input=data, capture_output=True, check=True, timeout=5).stdout.decode() for data in snapshots]
                assert any('"assistant/message"' in log and answer in log for log in logs)
            async with raw_runner(tmp_path, installed, env=env, state_root=state) as (_, send, receive):
                client = Client(send, receive)
                await client.start()
                await client.call("session/load", {**params, "session_id": sid})
                await client.prompt(sid, "resumed", "Continue")
                assert (await client.settled(sid, "resumed"))["outcome"] == "completed"
                assert len(server.requests) == 2
                assert answer in json.dumps(server.requests[-1]["body"])

    run(exercise())


@pytest.mark.parametrize("control", ["cancel", "close"])
@pytest.mark.parametrize("decision", ["allow", "deny", "error"])
def test_cancel_deadline_includes_pending_authorization(tmp_path, control, decision):
    async def exercise():
        _, installed = require_dsh()
        with peer.MessagesServer([["Resumed answer"]]) as server:
            async with raw_runner(tmp_path, installed, env=guard_env(tmp_path, server.port)) as (_, send, receive):
                client = Client(send, receive)
                client.auto_authorize = False
                await client.start()
                params = params_for(tmp_path, server)
                sid = (await client.call("session/new", params))["session_id"]
                await client.prompt(sid)
                while not client.authorized:
                    await client.read()
                auth = next(frame for frame in client.frames if frame.get("method") == "budget/authorize")
                started = time.monotonic()
                await asyncio.wait_for(client.call(f"session/{control}", {
                    "session_id": sid, "trace_id": "trace"}), 5.5)
                result = await asyncio.wait_for(client.settled(sid), 5.5)
                assert time.monotonic() - started < 5.5
                assert result["outcome"] == "cancelled"
                assert not result["usage_complete"]
                # Load the same durable id before replying to authorization:
                # removing only the runner registry leaves DSH's writer locked.
                assert await client.call("session/load", {**params, "session_id": sid}) == {
                    "session_id": sid, "resumed": True}
                health = await client.call("runner/health")
                assert health["sessions"] == 1
                assert health["running"] == health["inflight_requests"] == 0
                boundary = len(client.frames)
                reply = {"error": {"code": -32603, "message": "Synthetic authorization failure"}} if decision == "error" else {
                    "result": {"allow": decision == "allow"}}
                await send({"jsonrpc": "2.0", "id": auth["id"], **reply})
                await client.call("runner/health")
                assert not server.requests
                assert len(client.updates(sid, kind="settled")) == 1
                ends = assert_accounted(client, sid, count=0)
                assert not any(frame.get("method") == "session/update" for frame in client.frames[boundary:])
                client.auto_authorize = True
                await client.prompt(sid, "resumed", "Continue")
                resumed = await client.settled(sid, "resumed")
                assert resumed["outcome"] == "completed" and resumed["final_text"] == "Resumed answer"
                resumed_end, = assert_accounted(client, sid, trace="resumed")
                assert [request["request_id"] for request in server.requests] == [resumed_end["request_id"]]
                await client.call("session/close", {"session_id": sid})
                assert len(client.updates(sid, kind="request_end")) == len(ends)
                assert len(client.updates(sid, kind="settled")) == 1

    run(exercise())


@pytest.mark.parametrize("decision", ["allow", "deny", "error"])
def test_authorization_after_cancel_is_reconciled_before_settled(tmp_path, decision):
    async def exercise():
        _, installed = require_dsh()
        with peer.MessagesServer([["Must remain unsent"]]) as server:
            async with raw_runner(tmp_path, installed, env=guard_env(tmp_path, server.port)) as (_, send, receive):
                client = Client(send, receive)
                client.auto_authorize = False
                await client.start()
                sid = (await client.call("session/new", params_for(tmp_path, server)))["session_id"]
                await client.prompt(sid)
                while not client.authorized:
                    await client.read()
                auth = next(frame for frame in client.frames if frame.get("method") == "budget/authorize")
                await client.call("session/cancel", {"session_id": sid, "trace_id": "trace"})
                await asyncio.sleep(.1)
                reply = {"error": {"code": -32603, "message": "Synthetic authorization failure"}} if decision == "error" else {
                    "result": {"allow": decision == "allow"}}
                await send({"jsonrpc": "2.0", "id": auth["id"], **reply})
                result = await asyncio.wait_for(client.settled(sid), 4)
                assert result["outcome"] == "cancelled" and result["usage_complete"]
                expected = 1 if decision == "allow" else 0
                assert result["requests"] == expected
                ends = assert_accounted(client, sid, count=expected)
                if ends:
                    end, = ends
                    assert end["status"] == "aborted" and end["complete"]
                    assert [end[k] for k in ("input_uncached", "output", "cache_read", "cache_write")] == [0, 0, 0, 0]
                    kinds = [event["kind"] for event in client.updates(sid)]
                    assert kinds.index("request_end") < kinds.index("settled")
                await client.call("session/close", {"session_id": sid})
                assert not server.requests and len(client.updates(sid, kind="request_end")) == expected

    run(exercise())


def test_failed_flush_cannot_report_a_completed_turn(tmp_path, monkeypatch):
    harness(tmp_path, monkeypatch, """rpc.method('test/fail-flush', () => {
  sdk.ctx.sessionPersistence.flush = async () => { throw new Error('Synthetic flush failure'); };
  return {};
});""")

    async def exercise():
        _, installed = require_dsh()
        with peer.MessagesServer([["Unflushed answer"]]) as server:
            async with raw_runner(tmp_path, installed, env=guard_env(tmp_path, server.port)) as (_, send, receive):
                client = Client(send, receive)
                await client.start()
                sid = (await client.call("session/new", params_for(tmp_path, server)))["session_id"]
                await client.call("test/fail-flush")
                await client.prompt(sid)
                result = await client.settled(sid)
                assert result["outcome"] == "failed" and result["error"]["kind"] == "INTERNAL_ERROR"
                assert len(server.requests) == 1
                assert_accounted(client, sid)

    run(exercise())
