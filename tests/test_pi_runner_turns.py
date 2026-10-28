"""Installed Pi sessions exercise the real Responses client and host accounting."""
import asyncio
import json
from types import SimpleNamespace

import pytest

from fake_model_api import FakeModelAPI, ResponseScript, completed, event, text_item
from pi_runner_support import begin, pi_supervisor, raw_runner, require_pi, session_params

pytestmark = pytest.mark.installed
KEY = "!literal-secret-key"
OFFER = {"protocol_versions": ["0.1"], "looper_version": "test", "host_capabilities": {
    "tools_call": True, "budget_authorize": True, "bus": False, "exec_slots": False}}
USAGE = {"input_tokens": 1300, "input_tokens_details": {"cached_tokens": 300},
    "output_tokens": 200, "output_tokens_details": {"reasoning_tokens": 50}, "total_tokens": 1500}


def reply():
    item = text_item("Hello")
    return [event("response.output_item.added", output_index=0, item={**item, "content": []}),
        event("response.content_part.added", output_index=0, content_index=0,
            part={"type": "output_text", "text": "", "annotations": []}),
        event("response.output_text.delta", output_index=0, item_id=item["id"], content_index=0, delta="Hello"),
        event("response.output_item.done", output_index=0, item=item), completed([item], usage=USAGE)]


def run(coroutine):
    asyncio.run(asyncio.wait_for(coroutine, 25))


def test_turn_charges_each_request_exactly(tmp_path, monkeypatch):
    from looper.agents.runtimes.runner.turn import RunnerTurn

    async def exercise():
        async with FakeModelAPI([ResponseScript(events=reply())], api_key=KEY) as api, pi_supervisor(tmp_path) as supervisor:
            lease = await supervisor.acquire("pi")
            (lease.directory / "AGENTS.md").write_text("PROJECT-INSTRUCTIONS-MUST-NOT-LEAK")
            skills = lease.directory / ".pi/skills/leak"
            skills.mkdir(parents=True)
            (skills / "SKILL.md").write_text("---\nname: leak\ndescription: SKILL-MUST-NOT-LEAK\n---\nsecret skill")
            sid = await lease.open_session(session_params(lease, base_url=api.base_url, key=KEY))
            # Observe the wire without replacing host admission or accounting.
            authorized, ended = [], []
            original_authorize, original_end = RunnerTurn.authorize, RunnerTurn.request_end

            async def authorize(self, params):
                authorized.append(dict(params))
                return await original_authorize(self, params)

            async def request_end(self, params):
                ended.append(dict(params))
                return await original_end(self, params)

            monkeypatch.setattr(RunnerTurn, "authorize", authorize)
            monkeypatch.setattr(RunnerTurn, "request_end", request_end)
            _, events = await begin(lease.bridge, sid, "trace")
            summary = await lease.bridge.wait_settled(sid, "trace")
            assert summary.outcome == "completed" and summary.usage_complete
            assert [e["type"] for e in events] == ["system", "stream_event", "assistant", "usage", "result"]
            assert events[-1]["result"] == "Hello" and events[-1]["num_turns"] == 1
            assert len(authorized) == len(ended) == len(api.requests) == 1
            assert authorized[0]["kind"] == ended[0]["request_kind"] == "turn"
            assert authorized[0]["request_id"] == ended[0]["request_id"]
            assert ended[0]["status"] == "completed" and ended[0]["complete"]
            assert (summary.usage.input_tokens, summary.usage.output_tokens,
                summary.usage.cache_read_tokens, summary.usage.cache_creation_tokens) == (1000, 200, 300, 0)
            assert summary.usage.cost_usd == 0.0023
            request = api.requests[0]
            assert request.headers["authorization"] == f"Bearer {KEY}"
            assert request.body["instructions"].startswith("Only the injected instructions.")
            assert "MUST-NOT-LEAK" not in json.dumps(request.body)
            assert not request.body.get("tools")
            files = list(lease.state_dir.glob("*.jsonl"))
            assert len(files) == 1 and KEY not in files[0].read_text()
            assert (await lease.bridge.health())["sessions"] == 1
            # Credentials and model stores must stay memory-only.
            assert not list(lease._generation.directory.rglob("models*.json"))
            await lease.release()
    run(exercise())


def test_denied_request_never_reaches_the_provider(tmp_path):
    async def exercise():
        async with FakeModelAPI([], api_key=KEY) as api, pi_supervisor(tmp_path) as supervisor:
            lease = await supervisor.acquire("pi")
            sid = await lease.open_session(session_params(lease, base_url=api.base_url, key=KEY))
            _, events = await begin(lease.bridge, sid, "denied", cap=0.000001)
            summary = await lease.bridge.wait_settled(sid, "denied")
            assert summary.outcome == "capped" and summary.cap_reason == "run_cap"
            assert summary.usage_complete and summary.usage.cost_usd == 0
            assert events[-1]["num_turns"] == 0 and not api.requests
            await lease.release()
    run(exercise())


@pytest.mark.parametrize("status,kind", [(401, "PROVIDER_AUTH"), (403, "PROVIDER_AUTH"), (400, "PROVIDER_UNAVAILABLE")])
def test_provider_auth_failure_is_typed(tmp_path, status, kind):
    async def exercise():
        script = ResponseScript(status=status, error={"error": {"message": f"Invalid API key: {KEY}"}})
        async with FakeModelAPI([script], api_key=KEY) as api, pi_supervisor(tmp_path) as supervisor:
            lease = await supervisor.acquire("pi")
            generation = lease._generation
            sid = await lease.open_session(session_params(lease, base_url=api.base_url, key=KEY))
            _, events = await begin(lease.bridge, sid, "auth")
            summary = await lease.bridge.wait_settled(sid, "auth")
            assert summary.outcome == "failed" and len(api.requests) == 1
            assert events[-1]["runner"]["error_kind"] == kind
            assert events[-1]["num_turns"] == 1
            assert summary.usage_complete and summary.usage.cost_usd == 0
            assert KEY not in json.dumps(events, default=str)
            assert all(KEY not in file.read_text() for file in lease.state_dir.glob("*.jsonl"))
            await lease.release()
        assert KEY.encode() not in b"".join(generation.diagnostics)
    run(exercise())


def test_cancel_settles_a_streaming_request(tmp_path):
    async def exercise():
        script = ResponseScript(events=reply(), gate=asyncio.Event())
        async with FakeModelAPI([script], api_key=KEY) as api, pi_supervisor(tmp_path) as supervisor:
            lease = await supervisor.acquire("pi")
            sid = await lease.open_session(session_params(lease, base_url=api.base_url, key=KEY))
            turn, events = await begin(lease.bridge, sid, "cancel")
            ended, original = [], turn.request_end

            async def request_end(params):
                ended.append(dict(params))
                return await original(params)

            turn.request_end = request_end
            await script.gate_reached.wait()
            await lease.bridge.cancel_turn(sid, "cancel")
            summary = await asyncio.wait_for(lease.bridge.wait_settled(sid, "cancel"), 1)
            assert summary.outcome == "cancelled" and not summary.usage_complete
            assert len(ended) == 1 and ended[0]["status"] == "aborted" and not ended[0]["complete"]
            assert events[-1]["type"] == "result"
            assert (await lease.bridge.health())["inflight_requests"] == 0
            await lease.release()
    run(exercise())


@pytest.mark.parametrize("control", ["close", "cancel"])
def test_close_right_after_prompt_settles_once(tmp_path, control):
    async def exercise():
        _, pi = require_pi()
        async with FakeModelAPI([], api_key=KEY) as api, raw_runner(tmp_path, pi) as (process, send, receive):
            root = next(tmp_path.glob("pi-runner-*"))
            directory = root / "sessions/key"
            for name in ("agent", "native", "home"):
                (directory / name).mkdir(parents=True)
            lease = SimpleNamespace(session_key="key", directory=directory, home=directory / "home", state_dir=directory / "native")
            await send("initialize", OFFER)
            await receive()
            await send("session/new", session_params(lease, base_url=api.base_url, key=KEY), id="l-2")
            sid = (await receive())["result"]["session_id"]
            messages = [{"jsonrpc": "2.0", "id": "l-3", "method": "session/prompt", "params": {
                "session_id": sid, "trace_id": "close", "text": "Hello", "tools": []}},
                {"jsonrpc": "2.0", "id": "l-4", "method": f"session/{control}", "params": {"session_id": sid, "trace_id": "close"}}]
            process.stdin.write(b"".join(json.dumps(m).encode() + b"\n" for m in messages))
            await process.stdin.drain()
            accepted, second, third = [await receive() for _ in range(3)]
            settled, closed = (second, third) if control == "close" else (third, second)
            assert accepted["result"]["accepted"]
            assert settled["params"]["kind"] == "settled" and settled["params"]["outcome"] == "cancelled"
            assert settled["params"]["requests"] == 0
            assert closed["result"] == {"session_id": sid, "trace_id": "close"}
            await send("runner/health", {}, id="l-5")
            assert (await receive())["result"]["sessions"] == (0 if control == "close" else 1)
            assert not api.requests
    run(exercise())


def test_cancel_does_not_wait_for_authorization(tmp_path, monkeypatch):
    from looper.agents.runtimes.runner.turn import RunnerTurn

    async def exercise():
        entered, release = asyncio.Event(), asyncio.Event()
        original = RunnerTurn.authorize

        async def held(self, params):
            entered.set()
            await release.wait()
            return await original(self, params)

        monkeypatch.setattr(RunnerTurn, "authorize", held)
        async with FakeModelAPI([], api_key=KEY) as api, pi_supervisor(tmp_path) as supervisor:
            lease = await supervisor.acquire("pi")
            sid = await lease.open_session(session_params(lease, base_url=api.base_url, key=KEY))
            try:
                _, events = await begin(lease.bridge, sid, "held")
                await entered.wait()
                await lease.bridge.cancel_turn(sid, "held")
                summary = await asyncio.wait_for(lease.bridge.wait_settled(sid, "held"), 1)
                assert summary.outcome == "cancelled" and summary.usage_complete
                assert events[-1]["num_turns"] == 0 and not api.requests
            finally:
                release.set()
            # Processing the late denial must preserve the connection.
            assert (await lease.bridge.health())["running"] == 0
            await lease.release()
    run(exercise())


def test_running_session_rejects_conflicting_controls(tmp_path):
    from looper.agents.runtimes.runner.bridge import RpcFailure

    async def exercise():
        script = ResponseScript(events=reply(), gate=asyncio.Event())
        async with FakeModelAPI([script], api_key=KEY) as api, pi_supervisor(tmp_path) as supervisor:
            lease = await supervisor.acquire("pi")
            sid = await lease.open_session(session_params(lease, base_url=api.base_url, key=KEY))
            with pytest.raises(RpcFailure) as error:
                await lease.bridge.request("session/prompt", {"session_id": sid, "trace_id": "unsupported",
                    "text": "hello", "tools": [{"name": "lookup", "description": "read", "inputSchema": {"type": "object"}}]})
            assert error.value.code == -32007
            await begin(lease.bridge, sid, "active")
            await script.gate_reached.wait()
            for method, params, code in [
                ("session/prompt", {"trace_id": "other", "text": "hello", "tools": []}, -32003),
                ("session/cancel", {"trace_id": "other"}, -32005),
                ("session/close", {}, -32005),
            ]:
                with pytest.raises(RpcFailure) as error:
                    await lease.bridge.request(method, {"session_id": sid, **params})
                assert error.value.code == code
            script.gate.set()
            assert (await lease.bridge.wait_settled(sid, "active")).outcome == "completed"
            with pytest.raises(RpcFailure) as error:
                await lease.bridge.request("session/cancel", {"session_id": sid, "trace_id": "active"})
            assert error.value.code == -32004
            await lease.release()
    run(exercise())


@pytest.mark.parametrize("field,value,code", [
    ("cwd", "relative", -32602), ("state_dir", "/nonexistent-runner-state", -32602),
    ("provider.protocol", "anthropic", -32602), ("provider.api_key", "", -32602),
    ("system_prompt.mode", "unknown", -32602),
    ("tools.builtin", ["read"], -32007), ("tools.platform", [{"name": "lookup"}], -32007),
    ("agent", "missing", -32008), ("agent", "symlink", -32008),
])
def test_session_new_rejects_invalid_composition(tmp_path, field, value, code):
    async def exercise():
        _, pi = require_pi()
        async with raw_runner(tmp_path, pi) as (_, send, receive):
            root = next(tmp_path.glob("pi-runner-*"))
            directory = root / "sessions/key"
            for name in ("agent", "native", "home"):
                (directory / name).mkdir(parents=True)
            lease = SimpleNamespace(session_key="key", directory=directory, home=directory / "home", state_dir=directory / "native")
            params = session_params(lease, base_url="http://127.0.0.1:1/v1", key=KEY)
            if field == "agent":
                (directory / "agent").rmdir()
                if value == "symlink":
                    (directory / "agent").symlink_to(directory / "home", target_is_directory=True)
            elif "." in field:
                parent, name = field.split(".")
                params[parent][name] = value
            else:
                params[field] = value
            await send("initialize", OFFER)
            await receive()
            await send("session/new", params, id="l-2")
            assert (await receive())["error"]["code"] == code
            await send("runner/health", {}, id="l-3")
            assert (await receive())["result"]["sessions"] == 0
    run(exercise())


def test_unresponsive_cancel_releases_session_and_drops_late_events(tmp_path, monkeypatch):
    from looper.agents.runtimes.runner import pi_command

    main = pi_command.entry().resolve()
    sources = main.parent
    harness = tmp_path / "stalled-abort.ts"
    harness.write_text(f'''import * as rpc from {json.dumps((sources / 'rpc.ts').as_uri())};
import {{sessions}} from {json.dumps((sources / 'session.ts').as_uri())};
import {{onEvent}} from {json.dumps((sources / 'turn.ts').as_uri())};
import {{writeFileSync}} from 'node:fs';
let record;
rpc.method('test/stall', p => {{
  record = sessions.get(p.session_id);
  record.agent.abort = () => new Promise(() => {{}});
  const dispose = record.agent.dispose.bind(record.agent);
  record.agent.dispose = () => {{ dispose(); writeFileSync('disposed', 'yes'); }};
  return {{}};
}});
rpc.method('test/late', () => {{
  onEvent(record, {{type: 'message_update', assistantMessageEvent: {{type: 'text_delta', delta: 'late'}}}});
  onEvent(record, {{type: 'agent_settled'}});
  return {{}};
}});
await import({json.dumps(main.as_uri())});
''')
    monkeypatch.setattr(pi_command, "entry", lambda: harness)

    async def exercise():
        _, pi = require_pi()
        script = ResponseScript(events=reply(), gate=asyncio.Event())
        async with FakeModelAPI([script], api_key=KEY) as api, raw_runner(tmp_path, pi) as (_, send, receive):
            root = next(tmp_path.glob("pi-runner-*"))
            directory = root / "sessions/key"
            for name in ("agent", "native", "home"):
                (directory / name).mkdir(parents=True)
            lease = SimpleNamespace(session_key="key", directory=directory, home=directory / "home", state_dir=directory / "native")
            await send("initialize", OFFER)
            await receive()
            await send("session/new", session_params(lease, base_url=api.base_url, key=KEY), id="l-2")
            sid = (await receive())["result"]["session_id"]
            await send("session/prompt", {"session_id": sid, "trace_id": "stalled", "text": "hi", "tools": []}, id="l-3")
            assert (await receive())["result"]["accepted"]
            authorization = await receive()
            await send({"jsonrpc": "2.0", "id": authorization["id"], "result": {
                "session_id": sid, "trace_id": "stalled", "allow": True}})
            await script.gate_reached.wait()
            await send("test/stall", {"session_id": sid}, id="l-4")
            while (await receive()).get("id") != "l-4":
                pass
            await send("session/cancel", {"session_id": sid, "trace_id": "stalled"}, id="l-5")
            assert (await receive())["id"] == "l-5"
            ended, settled = await asyncio.wait_for(receive(), 5), await receive()
            assert ended["params"]["kind"] == "request_end" and not ended["params"]["complete"]
            assert settled["params"]["kind"] == "settled" and settled["params"]["outcome"] == "failed"
            assert settled["params"]["error"]["kind"] == "INTERNAL_ERROR"
            assert settled["params"]["requests"] == 1 and (root / "disposed").read_text() == "yes"
            await send("test/late", {}, id="l-6")
            assert (await receive())["id"] == "l-6"
            await send("runner/health", {}, id="l-7")
            assert (await receive())["result"]["sessions"] == 0
            await send("session/cancel", {"session_id": sid, "trace_id": "stalled"}, id="l-8")
            assert (await receive())["error"]["code"] == -32001
            assert len(api.requests) == 1
    run(exercise())


def test_output_truncation_is_typed(tmp_path):
    async def exercise():
        events = reply()
        terminal = events[-1]
        terminal["type"] = "response.incomplete"
        terminal["response"].update(status="incomplete", incomplete_details={"reason": "max_output_tokens"})
        async with FakeModelAPI([ResponseScript(events=events)], api_key=KEY) as api, pi_supervisor(tmp_path) as supervisor:
            lease = await supervisor.acquire("pi")
            sid = await lease.open_session(session_params(lease, base_url=api.base_url, key=KEY))
            _, output = await begin(lease.bridge, sid, "length")
            summary = await lease.bridge.wait_settled(sid, "length")
            assert summary.outcome == "failed" and summary.usage_complete
            assert output[-1]["runner"]["error_kind"] == "OUTPUT_TRUNCATED"
            await lease.release()
    run(exercise())
