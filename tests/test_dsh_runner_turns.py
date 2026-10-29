"""Installed DSH turn authorization, HTTP accounting and durable event contracts."""
import asyncio
import json
from pathlib import Path
import socket
import threading
import time

import pytest

from dsh_installed_stand_in import MessagesServer, Reply, tool
from dsh_runner_support import OFFER, guard_env, raw_lease, raw_runner, require_dsh, session_params
from looper.agents.runtimes.runner import dsh_command
from test_dsh_runner import harness

pytestmark = pytest.mark.installed


def run(coroutine):
    asyncio.run(asyncio.wait_for(coroutine, 120))


class Client:
    """Answer host-side authorization while preserving every frame's wire order."""
    def __init__(self, send, receive, denial=None):
        self.send, self.receive, self.denial = send, receive, denial
        self.frames, self.authorized, self.counter = [], {}, 0
        self.auto_authorize = True

    async def read(self):
        frame = await self.receive()
        self.frames.append(frame)
        if frame.get("method") == "budget/authorize":
            params = frame["params"]
            self.authorized[params["request_id"]] = params
            if self.auto_authorize:
                await self.send({"jsonrpc": "2.0", "id": frame["id"], "result": {
                    "allow": self.denial is None, **({"reason": self.denial} if self.denial else {})}})
        return frame

    async def call(self, method, params=None, *, code=None):
        self.counter += 1
        ident = f"l-{self.counter}"
        await self.send(method, params, id=ident)
        while True:
            frame = await self.read()
            if frame.get("id") == ident:
                if code is not None:
                    assert frame["error"]["code"] == code, frame
                    return frame["error"]
                assert "result" in frame, frame
                return frame["result"]

    async def start(self):
        await self.call("initialize", OFFER)

    async def prompt(self, sid, trace="trace", text="Disposable prompt"):
        assert await self.call("session/prompt", {
            "session_id": sid, "trace_id": trace, "text": text, "tools": []}) == {
                "session_id": sid, "trace_id": trace, "accepted": True}

    def updates(self, sid, trace="trace", kind=None):
        return [frame["params"] for frame in self.frames if frame.get("method") == "session/update"
                and frame["params"]["session_id"] == sid and frame["params"]["trace_id"] == trace
                and (kind is None or frame["params"]["kind"] == kind)]

    async def settled(self, sid, trace="trace"):
        while not self.updates(sid, trace, "settled"):
            await self.read()
        results = self.updates(sid, trace, "settled")
        assert len(results) == 1
        return results[0]

    async def requested(self, server, count=1):
        # Reading authorization frames makes progress; the HTTP event is the
        # positive control that the approved request actually left the runner.
        while len(self.authorized) < count:
            await self.read()
        assert await asyncio.to_thread(server.received.wait, 10)
        deadline = time.monotonic() + 10
        while len(server.requests) < count and time.monotonic() < deadline:
            await asyncio.sleep(.01)
        assert len(server.requests) == count, server.requests


def params_for(tmp_path, server, *, key="key", api_key="synthetic-dsh-runner-key", section="Disposable section"):
    params = session_params(raw_lease(tmp_path, key),
        system_prompt={"mode": "append", "text": section})
    params["provider"].update(base_url=server.url, api_key=api_key)
    return params


def assert_accounted(client, sid, *, trace="trace", count=1):
    ends = client.updates(sid, trace, "request_end")
    assert len(ends) == count
    ids = [end["request_id"] for end in ends]
    assert len(set(ids)) == count
    for end in ends:
        authorization = client.authorized[end["request_id"]]
        assert authorization["session_id"] == sid and authorization["trace_id"] == trace
        assert authorization["provider"] == "account"
        assert authorization["model"] == "deepseek-v4-pro[1m]"
        assert authorization["estimated_input_tokens"] > 0
        assert authorization["max_output_tokens"] > 0
        auth_index = next(index for index, frame in enumerate(client.frames)
            if frame.get("method") == "budget/authorize" and frame["params"]["request_id"] == end["request_id"])
        end_index = next(index for index, frame in enumerate(client.frames)
            if frame.get("method") == "session/update" and frame["params"] == end)
        assert auth_index < end_index
    return ends


def test_turn_charges_each_request_exactly(tmp_path):
    async def exercise():
        _, installed = require_dsh()
        replies = [Reply([tool("read", {"file_path": "note.txt"})],
                         usage={"input": 13, "output": 7, "cache_read": 2, "cache_write": 3}),
                   Reply(["Finished reading"], usage={"input": 17, "output": 9, "cache_read": 5, "cache_write": 4})]
        with MessagesServer(replies) as server:
            async with raw_runner(tmp_path, installed, env=guard_env(tmp_path, server.port)) as (_, send, receive):
                client = Client(send, receive)
                await client.start()
                params = params_for(tmp_path, server)
                (Path(params["cwd"]) / "note.txt").write_text("Disposable note")
                sid = (await client.call("session/new", params))["session_id"]
                await client.prompt(sid)
                settled = await client.settled(sid)
                events = client.updates(sid)
                durable = [row["kind"] for row in events if row["kind"] in ("message", "tool_result")]
                assert durable == ["message", "tool_result", "message"]
                messages = client.updates(sid, kind="message")
                assert messages[0]["content"][0]["type"] == "tool_use"
                assert messages[0]["content"][0]["input"] == {"file_path": "note.txt"}
                assert "Disposable note" in client.updates(sid, kind="tool_result")[0]["text"]
                ends = assert_accounted(client, sid, count=2)
                # DSH TokenUsage's inputTokens already excludes both cache buckets.
                assert [(e["input_uncached"], e["output"], e["cache_read"], e["cache_write"], e["reasoning"])
                        for e in ends] == [(13, 7, 2, 3, None), (17, 9, 5, 4, None)]
                assert all(e["request_kind"] == "turn" and e["complete"] and e["status"] == "completed" for e in ends)
                assert [request["request_id"] for request in server.requests] == [e["request_id"] for e in ends]
                assert settled["outcome"] == "completed" and settled["requests"] == 2
                assert settled["final_text"] == "Finished reading" and settled["usage_complete"]
                await client.call("session/close", {"session_id": sid})
    run(exercise())


@pytest.mark.parametrize("reason,outcome", [("run_cap", "capped"), ("max_requests", "max_requests")])
def test_denied_request_never_reaches_the_provider(tmp_path, reason, outcome):
    async def exercise():
        _, installed = require_dsh()
        with MessagesServer([["must not be requested"]]) as server:
            async with raw_runner(tmp_path, installed, env=guard_env(tmp_path, server.port)) as (_, send, receive):
                client = Client(send, receive, reason)
                await client.start()
                sid = (await client.call("session/new", params_for(tmp_path, server)))["session_id"]
                await client.prompt(sid)
                result = await client.settled(sid)
                assert len(client.authorized) == 1
                assert not server.requests and not client.updates(sid, kind="request_end")
                assert result["outcome"] == outcome and result["requests"] == 0
                assert result["usage_complete"]
    run(exercise())


@pytest.mark.parametrize("case,complete,status,error", [
    ("401", True, "http_401", "PROVIDER_AUTH"),
    ("500", True, "http_500", "PROVIDER_UNAVAILABLE"),
    ("cut", False, "error", "PROVIDER_UNAVAILABLE"),
    ("connection", True, "error", "PROVIDER_UNAVAILABLE")])
def test_provider_failures_account_exactly(tmp_path, case, complete, status, error):
    async def exercise():
        _, installed = require_dsh()
        reply = Reply(["Partial output"], cut=case == "cut", status=int(case) if case.isdigit() else 200)
        with MessagesServer([reply]) as server:
            if case == "connection":
                with socket.socket() as sock:
                    sock.bind(("127.0.0.1", 0))
                    port = sock.getsockname()[1]
            else:
                port = server.port
            async with raw_runner(tmp_path, installed, env=guard_env(tmp_path, port)) as (_, send, receive):
                client = Client(send, receive)
                await client.start()
                params = params_for(tmp_path, server)
                params["provider"]["base_url"] = f"http://127.0.0.1:{port}"
                sid = (await client.call("session/new", params))["session_id"]
                await client.prompt(sid)
                result = await client.settled(sid)
                end, = assert_accounted(client, sid)
                assert end["complete"] is complete and end["status"] == status
                if case != "cut":
                    assert [end[k] for k in ("input_uncached", "output", "cache_read", "cache_write")] == [0, 0, 0, 0]
                assert result["outcome"] == "failed" and result["error"]["kind"] == error
                assert result["usage_complete"] is complete
                assert len(server.requests) == (0 if case == "connection" else 1)
    run(exercise())


def test_cancel_settles_a_streaming_request(tmp_path):
    async def exercise():
        _, installed = require_dsh()
        with MessagesServer([Reply(["held"], hold=threading.Event()), ["Next prompt completed"]]) as server:
            async with raw_runner(tmp_path, installed, env=guard_env(tmp_path, server.port)) as (_, send, receive):
                client = Client(send, receive)
                await client.start()
                sid = (await client.call("session/new", params_for(tmp_path, server)))["session_id"]
                await client.prompt(sid)
                await client.requested(server)
                await client.call("session/cancel", {"session_id": sid, "trace_id": "other"}, code=-32005)
                assert await client.call("session/cancel", {"session_id": sid, "trace_id": "trace"}) == {
                    "session_id": sid, "trace_id": "trace"}
                assert not client.updates(sid, kind="settled"), "cancel must be acknowledged before settling"
                result = await client.settled(sid)
                end, = assert_accounted(client, sid)
                assert result["outcome"] == "cancelled" and not result["usage_complete"]
                assert end["status"] == "aborted" and not end["complete"]
                assert len(server.requests) == 1
                await client.prompt(sid, "next")
                assert (await client.settled(sid, "next"))["outcome"] == "completed"
                assert len(server.requests) == 2
    run(exercise())


@pytest.mark.parametrize("cancel_first", [False, True])
def test_close_right_after_prompt_settles_once(tmp_path, monkeypatch, cancel_first):
    session_file = dsh_command.entry().resolve().parent / "session.ts"
    harness(tmp_path, monkeypatch, f"""const {{sessions}} = await import({json.dumps(session_file.as_uri())});
rpc.method('test/park-prompt', p => {{
  const agent = sessions.get(p.session_id).agent;
  agent.followup = message => agent.send(message, 'next-turn', false);
  return {{}};
}});""")

    async def exercise():
        _, installed = require_dsh()
        with MessagesServer([Reply(["held"], hold=threading.Event())]) as server:
            async with raw_runner(tmp_path, installed, env=guard_env(tmp_path, server.port)) as (_, send, receive):
                client = Client(send, receive)
                await client.start()
                sid = (await client.call("session/new", params_for(tmp_path, server)))["session_id"]
                # Keep the accepted input parked in the real inbox, so close
                # deterministically runs before any turn/start event.
                await client.call("test/park-prompt", {"session_id": sid})
                await client.prompt(sid)
                if cancel_first:
                    await client.call("session/cancel", {"session_id": sid, "trace_id": "trace"})
                assert await client.call("session/close", {"session_id": sid, "trace_id": "trace"}) == {
                    "session_id": sid, "trace_id": "trace"}
                assert (await client.settled(sid))["outcome"] == "cancelled"
                await client.call("session/cancel", {"session_id": sid, "trace_id": "trace"}, code=-32001)
                await client.call("session/prompt", {"session_id": sid, "trace_id": "next", "text": "x", "tools": []}, code=-32001)
                await client.call("runner/health")
                assert len(client.updates(sid, kind="settled")) == 1
                assert not client.authorized and not server.requests
                settled_index = next(i for i, frame in enumerate(client.frames)
                    if frame.get("method") == "session/update" and frame["params"]["kind"] == "settled")
                assert not any(frame.get("method") == "session/update" and frame["params"]["session_id"] == sid
                               for frame in client.frames[settled_index + 1:])
    run(exercise())


def test_prompt_validation_and_running_turn(tmp_path):
    async def exercise():
        _, installed = require_dsh()
        with MessagesServer([Reply(["held"], hold=threading.Event())]) as server:
            async with raw_runner(tmp_path, installed, env=guard_env(tmp_path, server.port)) as (_, send, receive):
                client = Client(send, receive)
                await client.start()
                sid = (await client.call("session/new", params_for(tmp_path, server)))["session_id"]
                valid = {"session_id": sid, "trace_id": "trace", "text": "x", "tools": []}
                for override in ({"trace_id": ""}, {"trace_id": None}, {"text": 1}, {"tools": None}):
                    await client.call("session/prompt", {**valid, **override}, code=-32602)
                await client.call("session/prompt", {**valid, "tools": [{"name": "future"}]}, code=-32007)
                await client.call("session/cancel", {"session_id": sid, "trace_id": "trace"}, code=-32004)
                await client.prompt(sid)
                await client.requested(server)
                await client.call("session/prompt", valid, code=-32003)
                await client.call("session/close", {"session_id": sid, "trace_id": "other"}, code=-32005)
                health = await client.call("runner/health")
                assert health["running"] == 1 and health["inflight_requests"] == 1
                await client.call("session/cancel", {"session_id": sid, "trace_id": "trace"})
                await client.settled(sid)
                health = await client.call("runner/health")
                assert health["running"] == 0 and health["inflight_requests"] == 0
    run(exercise())


def test_two_sessions_keep_keys_prompts_and_cancel_apart(tmp_path):
    async def exercise():
        _, installed = require_dsh()
        first, second = threading.Event(), threading.Event()
        with MessagesServer([Reply(["A held"], hold=first), Reply(["B complete"], hold=second)]) as server:
            async with raw_runner(tmp_path, installed, env=guard_env(tmp_path, server.port)) as (_, send, receive):
                client = Client(send, receive)
                await client.start()
                a = (await client.call("session/new", params_for(tmp_path, server, key="a", api_key="K1", section="P1 only")))["session_id"]
                b = (await client.call("session/new", params_for(tmp_path, server, key="b", api_key="K2", section="P2 only")))["session_id"]
                await client.prompt(a, "a", "A user text")
                await client.requested(server)
                await client.prompt(b, "b", "B user text")
                await client.requested(server, 2)
                for request, key, section, other in zip(server.requests, ("K1", "K2"), ("P1 only", "P2 only"), ("P2 only", "P1 only")):
                    body = json.dumps(request["body"])
                    assert request["api_key"] == key and section in body and other not in body
                    assert request["request_id"] in client.authorized
                await client.call("session/cancel", {"session_id": a, "trace_id": "a"})
                assert (await client.settled(a, "a"))["outcome"] == "cancelled"
                second.set()
                assert (await client.settled(b, "b"))["outcome"] == "completed"
                assert len(server.requests) == 2
                assert_accounted(client, a, trace="a")
                assert_accounted(client, b, trace="b")
    run(exercise())


def test_runner_kill_mid_turn_then_resume(tmp_path):
    async def exercise():
        _, installed = require_dsh()
        state = dsh_command.state_root(tmp_path / "shared-runtime")
        with MessagesServer([["Turn one"], Reply(["held turn two"], hold=threading.Event()), ["Turn three"]]) as server:
            env = guard_env(tmp_path, server.port)
            async with raw_runner(tmp_path, installed, env=env, state_root=state) as (process, send, receive):
                client = Client(send, receive)
                await client.start()
                params = params_for(tmp_path, server)
                sid = (await client.call("session/new", params))["session_id"]
                await client.prompt(sid, "one", "Remember durable turn one")
                assert (await client.settled(sid, "one"))["outcome"] == "completed"
                await client.prompt(sid, "two", "Interrupted turn two")
                await client.requested(server, 2)
                process.kill()
                assert await asyncio.wait_for(process.wait(), 5) < 0
            async with raw_runner(tmp_path, installed, env=env, state_root=state) as (_, send, receive):
                client = Client(send, receive)
                await client.start()
                assert await client.call("session/load", {**params, "session_id": sid}) == {"session_id": sid, "resumed": True}
                await client.prompt(sid, "three", "Continue turn three")
                assert (await client.settled(sid, "three"))["outcome"] == "completed"
                assert len(server.requests) == 3
                assert "Remember durable turn one" in json.dumps(server.requests[2]["body"])
    run(exercise())


def test_unattributed_and_compaction_requests(tmp_path, monkeypatch):
    _, installed = require_dsh()
    session_file = dsh_command.entry().resolve().parent / "session.ts"
    harness(tmp_path, monkeypatch, f"""
const {{createRequire}} = await import('node:module');
const {{realpathSync}} = await import('node:fs');
const {{pathToFileURL}} = await import('node:url');
const require = createRequire(realpathSync({json.dumps(installed)}));
const {{defineTool}} = await import(pathToFileURL(require.resolve('@deepseek-ai/dsh-tools')));
const {{sessions}} = await import({json.dumps(session_file.as_uri())});
let release;
rpc.method('test/stream', async p => {{
  const record = sessions.get(p.session_id), chunks = [];
  for await (const chunk of sdk.ctx.llm.stream({{
      sessionId: record.id, provider: record.route, model: record.wireModel,
      ...(p.compaction ? {{purpose: 'compaction'}} : {{}}),
      messages: [sdk.dsh.llm.createUserMessage({{content: [{{type: 'text', text: 'Synthetic compaction'}}]}})]
  }})) chunks.push(chunk);
  return {{chunks}};
}});
rpc.method('test/slow-tool', p => {{
  const record = sessions.get(p.session_id);
  record.allowed.add('slow_probe');
  record.agentCtx.tools.register(defineTool({{name: 'slow_probe', description: 'Synthetic slow scoped tool',
    parameters: {{}}, output: {{schema: {{type: 'string'}}}}, async execute() {{
      await rpc.notify('test/tool-open', {{}});
      await new Promise(resolve => {{ release = resolve; }});
      return 'Synthetic tool finished';
    }}
  }}));
  return {{}};
}});
rpc.method('test/release', () => {{release(); return {{}};}});
""")

    async def exercise():
        with MessagesServer([[tool("slow_probe", {})], ["Compacted"], ["Final answer"]]) as server:
            async with raw_runner(tmp_path, installed, env=guard_env(tmp_path, server.port)) as (process, send, receive):
                client = Client(send, receive)
                await client.start()
                sid = (await client.call("session/new", params_for(tmp_path, server)))["session_id"]
                chunks = (await client.call("test/stream", {"session_id": sid}))["chunks"]
                assert chunks == [{"type": "finish", "reason": {"kind": "error", "failure": {
                    "message": "Looper refused a model request it cannot attribute", "code": "LOOPER_UNATTRIBUTED"}}}]
                assert not client.authorized and not server.requests
                await client.call("test/slow-tool", {"session_id": sid})
                await client.prompt(sid)
                while not any(frame.get("method") == "test/tool-open" for frame in client.frames):
                    await client.read()
                await client.call("test/stream", {"session_id": sid, "compaction": True})
                await client.call("test/release")
                result = await client.settled(sid)
                assert result["outcome"] == "completed" and result["requests"] == 3
                ends = assert_accounted(client, sid, count=3)
                assert [end["request_kind"] for end in ends] == ["turn", "compaction", "turn"]
                assert [auth["kind"] for auth in client.authorized.values()] == ["turn", "compaction", "turn"]
                assert len(server.requests) == 3
                process.stdin.close()
                assert await asyncio.wait_for(process.wait(), 5) == 0
                assert b"usage_unattributed session_id=" in await process.stderr.read()
    run(exercise())


@pytest.mark.parametrize("authorization_error", [False, True])
def test_pending_authorization_is_reconciled_before_settling(tmp_path, authorization_error):
    async def exercise():
        _, installed = require_dsh()
        with MessagesServer([["must remain unsent"]]) as server:
            async with raw_runner(tmp_path, installed, env=guard_env(tmp_path, server.port)) as (_, send, receive):
                client = Client(send, receive)
                client.auto_authorize = False
                await client.start()
                sid = (await client.call("session/new", params_for(tmp_path, server)))["session_id"]
                await client.prompt(sid)
                while not client.authorized:
                    await client.read()
                auth = next(frame for frame in client.frames if frame.get("method") == "budget/authorize")
                if authorization_error:
                    await send({"jsonrpc": "2.0", "id": auth["id"], "error": {
                        "code": -32603, "message": "Synthetic host authorization failure"}})
                else:
                    await client.call("session/cancel", {"session_id": sid, "trace_id": "trace"})
                    assert not client.updates(sid, kind="settled")
                    await send({"jsonrpc": "2.0", "id": auth["id"], "result": {"allow": True}})
                result = await client.settled(sid)
                assert not server.requests
                if authorization_error:
                    assert not client.updates(sid, kind="request_end")
                    assert result["outcome"] == "failed" and result["error"] == {
                        "kind": "INTERNAL_ERROR", "message": "Looper could not authorize the model request"}
                else:
                    end, = assert_accounted(client, sid)
                    assert end["status"] == "aborted" and end["complete"]
                    assert [end[k] for k in ("input_uncached", "output", "cache_read", "cache_write")] == [0, 0, 0, 0]
                    assert result["outcome"] == "cancelled" and result["usage_complete"]
    run(exercise())


@pytest.mark.parametrize("usage", [{"input": -1, "output": 5}, {"input": 11, "output": 9007199254740992}])
def test_malformed_usage_never_establishes_complete_accounting(tmp_path, usage):
    async def exercise():
        _, installed = require_dsh()
        with MessagesServer([Reply(["Synthetic response"], usage=usage)]) as server:
            async with raw_runner(tmp_path, installed, env=guard_env(tmp_path, server.port)) as (_, send, receive):
                client = Client(send, receive)
                await client.start()
                sid = (await client.call("session/new", params_for(tmp_path, server)))["session_id"]
                await client.prompt(sid)
                result = await client.settled(sid)
                end, = assert_accounted(client, sid)
                assert not end["complete"] and not result["usage_complete"]
                assert result["outcome"] == "failed" and result["error"]["kind"] == "INTERNAL_ERROR"
                assert len(result["error"]["message"]) <= 512
                assert len(server.requests) == 1 and server.requests[0]["request_id"] == end["request_id"]
    run(exercise())


def test_cancel_releases_an_agent_that_does_not_settle(tmp_path, monkeypatch):
    session_file = dsh_command.entry().resolve().parent / "session.ts"
    harness(tmp_path, monkeypatch, f"""const {{sessions}} = await import({json.dumps(session_file.as_uri())});
rpc.method('test/stuck-agent', p => {{
  const record = sessions.get(p.session_id);
  record.agent.cancel = () => {{}};
  return {{}};
}});""")

    async def exercise():
        _, installed = require_dsh()
        with MessagesServer([Reply(["held"], hold=threading.Event())]) as server:
            async with raw_runner(tmp_path, installed, env=guard_env(tmp_path, server.port)) as (_, send, receive):
                client = Client(send, receive)
                await client.start()
                sid = (await client.call("session/new", params_for(tmp_path, server)))["session_id"]
                await client.prompt(sid)
                await client.requested(server)
                await client.call("test/stuck-agent", {"session_id": sid})
                await client.call("session/cancel", {"session_id": sid, "trace_id": "trace"})
                result = await client.settled(sid)
                assert result["outcome"] == "failed" and result["error"] == {
                    "kind": "INTERNAL_ERROR", "message": "cancel did not settle; the session was released"}
                end, = assert_accounted(client, sid)
                assert end["status"] == "aborted" and not end["complete"]
                await client.call("session/prompt", {"session_id": sid, "trace_id": "next", "text": "x", "tools": []}, code=-32001)
                await client.call("runner/health")
                assert len(client.updates(sid, kind="settled")) == 1
    run(exercise())
