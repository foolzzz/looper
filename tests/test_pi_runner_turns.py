"""Installed Pi sessions exercise the real Responses client and host accounting."""
import asyncio
import json
import socket
import ssl
import subprocess
from types import SimpleNamespace

import pytest

from fake_model_api import FakeModelAPI, ResponseScript, completed, event, failed, text_item
from pi_runner_support import TEST_TIMEOUT, begin, pi_supervisor, raw_runner, require_pi, session_params

pytestmark = pytest.mark.installed
KEY = "!literal-secret-key"
OFFER = {"protocol_versions": ["0.1"], "looper_version": "test", "host_capabilities": {
    "tools_call": True, "budget_authorize": True, "bus": False, "exec_slots": False}}
USAGE = {"input_tokens": 1300, "input_tokens_details": {"cached_tokens": 300},
    "output_tokens": 200, "output_tokens_details": {"reasoning_tokens": 50}, "total_tokens": 1500}
CHAT_USAGE = {"prompt_tokens": 1300, "prompt_tokens_details": {"cached_tokens": 300},
    "completion_tokens": 200, "completion_tokens_details": {"reasoning_tokens": 50}, "total_tokens": 1500}


def chat_chunk(choices, **fields):
    return {"id": "chat_1", "object": "chat.completion.chunk", "created": 0,
        "model": "test-model", "choices": choices, **fields}


def reply():
    item = text_item("Hello")
    return [event("response.output_item.added", output_index=0, item={**item, "content": []}),
        event("response.content_part.added", output_index=0, content_index=0,
            part={"type": "output_text", "text": "", "annotations": []}),
        event("response.output_text.delta", output_index=0, item_id=item["id"], content_index=0, delta="Hello"),
        event("response.output_item.done", output_index=0, item=item), completed([item], usage=USAGE)]


def run(coroutine):
    asyncio.run(asyncio.wait_for(coroutine, TEST_TIMEOUT))


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


@pytest.fixture
def sent_headers(tmp_path):
    """Observe the real runner's HTTP client, independently of its accounting."""
    log = tmp_path / "sent-headers.jsonl"
    observer = tmp_path / "observe-http.mjs"
    observer.write_text(r"""
import {subscribe} from 'node:diagnostics_channel';
import {appendFileSync} from 'node:fs';
subscribe('undici:client:sendHeaders', ({headers}) => {
  const id = /^x-looper-request-id: ([^\r\n]+)\r?$/im.exec(headers)?.[1];
  appendFileSync(LOG_PATH, JSON.stringify(id ?? null) + '\n');
});
""".replace("LOG_PATH", json.dumps(str(log))))
    return {"NODE_OPTIONS": f"--import={observer.as_uri()}"}, lambda: [
        json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []


@pytest.mark.parametrize("protocol,blocked,line_ending,final_ending", [
    ("openai-responses", False, "\n", None), ("openai-chat", False, "\n", None),
    ("openai-chat", False, "\r\n", None), ("openai-chat", False, "\r", None),
    ("openai-chat", False, "\n", "\n"), ("openai-chat", False, "\n", ""),
    ("openai-responses", True, "\n", None), ("openai-chat", True, "\n", None),
], ids=["responses", "chat", "chat-crlf", "chat-cr", "chat-eof-line", "chat-eof-no-newline",
    "bad-port-responses", "bad-port-chat"])
def test_send_headers_correlates_real_http_attempts(tmp_path, monkeypatch, sent_headers, protocol, blocked, line_ending, final_ending):
    from looper.agents.runtimes.runner.turn import RunnerTurn

    async def exercise():
        ended, original = [], RunnerTurn.request_end

        async def request_end(self, params):
            ended.append(dict(params))
            return await original(self, params)

        monkeypatch.setattr(RunnerTurn, "request_end", request_end)
        environment, sent = sent_headers
        choices = [{"index": 0, "delta": {"content": "Hello"}, "finish_reason": "stop"}]
        events = reply() if protocol == "openai-responses" else [
            chat_chunk(choices), chat_chunk([], usage=CHAT_USAGE), "[DONE]"]
        async with FakeModelAPI([ResponseScript(events=events, sse_line_ending=line_ending, sse_final_ending=final_ending)], api_key=KEY) as api, pi_supervisor(tmp_path, env=environment) as supervisor:
            lease = await supervisor.acquire("pi")
            params = session_params(lease, base_url="http://127.0.0.1:6000/v1" if blocked else api.base_url, key=KEY)
            params["provider"]["protocol"] = protocol
            sid = await lease.open_session(params)
            await begin(lease.bridge, sid, "sent-headers")
            summary = await lease.bridge.wait_settled(sid, "sent-headers")
            assert len(ended) == (4 if blocked else 1)
            assert all(p["complete"] for p in ended)
            assert summary.usage_complete
            if blocked:
                assert not api.requests and not sent()
                assert summary.outcome == "failed" and summary.usage.cost_usd == 0
                assert all(p[field] == 0 for p in ended for field in ("input_uncached", "output", "cache_read", "cache_write"))
            else:
                assert summary.outcome == "completed" and summary.usage.cost_usd == 0.0023
                assert sent() == [ended[0]["request_id"]]
                assert api.requests[0].headers["x-looper-request-id"] == ended[0]["request_id"]
            await lease.release()
    run(exercise())


@pytest.mark.parametrize("protocol,usage_location", [
    ("openai-responses", "separate"), ("openai-chat", "separate"), ("openai-chat", "terminal"),
    ("openai-chat", "choice"),
], ids=["responses", "chat-separate-usage", "chat-terminal-usage", "chat-choice-usage"])
@pytest.mark.parametrize("ending", ["disconnect", "missing-sentinel"])
def test_final_usage_requires_normal_protocol_end(tmp_path, monkeypatch, protocol, usage_location, ending):
    from looper.agents.runtimes.runner.turn import RunnerTurn

    async def exercise():
        ended, original = [], RunnerTurn.request_end

        async def request_end(self, params):
            ended.append(dict(params))
            return await original(self, params)

        monkeypatch.setattr(RunnerTurn, "request_end", request_end)
        events = reply() if protocol == "openai-responses" else [
            chat_chunk([{"index": 0, "delta": {"content": "Hello"}, "finish_reason": "stop"}]),
            chat_chunk([], usage=CHAT_USAGE)]
        if protocol == "openai-chat" and usage_location == "terminal":
            events = [chat_chunk(events[0]["choices"], usage=CHAT_USAGE)]
        elif protocol == "openai-chat" and usage_location == "choice":
            events = [chat_chunk([{**events[0]["choices"][0], "usage": CHAT_USAGE}])]
        # Responses ends with its completed event; Chat requires the trailing [DONE].
        expected_complete = ending == "missing-sentinel" and protocol == "openai-responses"
        attempts = 1 if ending == "missing-sentinel" else 4
        scripts = [ResponseScript(events=events, disconnect_after=len(events) if ending == "disconnect" else None)
            for _ in range(attempts)]
        async with FakeModelAPI(scripts, api_key=KEY) as api, pi_supervisor(tmp_path) as supervisor:
            lease = await supervisor.acquire("pi")
            params = session_params(lease, base_url=api.base_url, key=KEY)
            params["provider"]["protocol"] = protocol
            sid = await lease.open_session(params)
            await begin(lease.bridge, sid, "final-usage")
            summary = await lease.bridge.wait_settled(sid, "final-usage")
            assert len(ended) == len(api.requests) == attempts
            assert all(p["complete"] is expected_complete for p in ended)
            assert summary.usage_complete is expected_complete
            assert summary.usage.cost_usd == (0.0023 if expected_complete else None)
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
            assert not summary.usage_complete and summary.usage.cost_usd is None
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


def test_cancel_ack_does_not_wait_for_authorization(tmp_path, monkeypatch):
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
                await asyncio.wait_for(lease.bridge.cancel_turn(sid, "held"), 1)
                release.set()
                summary = await asyncio.wait_for(lease.bridge.wait_settled(sid, "held"), 1)
                assert summary.outcome == "cancelled" and summary.usage_complete
                assert events[-1]["num_turns"] == 0 and not api.requests
            finally:
                release.set()
            # Processing the late denial must preserve the connection.
            assert (await lease.bridge.health())["running"] == 0
            await lease.release()
    run(exercise())


@pytest.mark.parametrize("protocol", ["openai-responses", "openai-chat"])
@pytest.mark.parametrize("allow,delay", [(True, 0.1), (False, 0.1), (True, 4.2)],
    ids=["allow-after-abort", "deny-after-abort", "allow-after-cancel-deadline"])
def test_cancel_accounts_for_late_authorization_before_settled(tmp_path, allow, delay, protocol, sent_headers):
    async def exercise():
        _, pi = require_pi()
        environment, sent = sent_headers
        async with FakeModelAPI([], api_key=KEY) as api, raw_runner(tmp_path, pi, env=environment) as (_, send, receive):
            root = next(tmp_path.glob("pi-runner-*"))
            directory = root / "sessions/key"
            for name in ("agent", "native", "home"):
                (directory / name).mkdir(parents=True)
            lease = SimpleNamespace(session_key="key", directory=directory, home=directory / "home", state_dir=directory / "native")
            await send("initialize", OFFER)
            await receive()
            params = session_params(lease, base_url=api.base_url, key=KEY)
            params["provider"]["protocol"] = protocol
            await send("session/new", params, id="l-2")
            sid = (await receive())["result"]["session_id"]
            await send("session/prompt", {"session_id": sid, "trace_id": "late", "text": "hi", "tools": []}, id="l-3")
            assert (await receive())["result"]["accepted"]
            authorization = await receive()
            assert authorization["method"] == "budget/authorize"
            await send("session/cancel", {"session_id": sid, "trace_id": "late"}, id="l-4")
            assert (await receive())["id"] == "l-4"
            # Let cancellation reach the SDK before the host releases its reply.
            await asyncio.sleep(delay)
            await send({"jsonrpc": "2.0", "id": authorization["id"], "result": {
                "session_id": sid, "trace_id": "late", "allow": allow, **({} if allow else {"reason": "stopped"})}})
            updates = []
            while True:
                frame = await receive()
                updates.append(frame["params"])
                if updates[-1]["kind"] == "settled":
                    break
            ended = [p for p in updates if p["kind"] == "request_end"]
            assert len(ended) == int(allow)
            if allow:
                assert ended[0]["request_id"] == authorization["params"]["request_id"]
                assert ended[0]["status"] == "aborted" and ended[0]["complete"]
                assert all(ended[0][field] == 0 for field in ("input_uncached", "output", "cache_read", "cache_write"))
            assert updates[-1]["outcome"] == "cancelled" and updates[-1]["usage_complete"]
            assert updates[-1]["requests"] == int(allow)
            # A barrier also catches duplicate or late accounting after settled.
            await send("runner/health", {}, id="l-5")
            health = await receive()
            assert health["id"] == "l-5"
            assert health["result"]["running"] == health["result"]["inflight_requests"] == 0
            assert not api.requests and not sent()
    run(exercise())


@pytest.mark.parametrize("protocol", ["openai-responses", "openai-chat"])
@pytest.mark.parametrize("disconnect", [True, False], ids=["body-sent", "connection-refused"])
def test_transport_failure_accounts_for_every_attempt(tmp_path, monkeypatch, disconnect, protocol, sent_headers):
    from looper.agents.runtimes.runner.turn import RunnerTurn

    async def exercise():
        environment, sent = sent_headers
        ended, original = [], RunnerTurn.request_end

        async def request_end(self, params):
            ended.append(dict(params))
            return await original(self, params)

        monkeypatch.setattr(RunnerTurn, "request_end", request_end)
        scripts = [ResponseScript(disconnect_before_headers=True) for _ in range(4)]
        async with FakeModelAPI(scripts, api_key=KEY) as api, pi_supervisor(tmp_path, env=environment) as supervisor:
            # Close an ephemeral socket so connect fails before any HTTP bytes are sent.
            with socket.socket() as refused:
                refused.bind(("127.0.0.1", 0))
                base_url = api.base_url if disconnect else f"http://127.0.0.1:{refused.getsockname()[1]}/v1"
            lease = await supervisor.acquire("pi")
            params = session_params(lease, base_url=base_url, key=KEY)
            params["provider"]["protocol"] = protocol
            sid = await lease.open_session(params)
            await begin(lease.bridge, sid, "transport")
            summary = await lease.bridge.wait_settled(sid, "transport")
            assert summary.outcome == "failed"
            assert len(ended) == 4 and len({p["request_id"] for p in ended}) == 4
            assert all(p["complete"] is (not disconnect) for p in ended)
            assert all(p[field] == 0 for p in ended for field in ("input_uncached", "output", "cache_read", "cache_write"))
            assert summary.usage_complete is (not disconnect)
            assert summary.usage.cost_usd is None if disconnect else summary.usage.cost_usd == 0
            assert len(api.requests) == (4 if disconnect else 0)
            assert sent() == ([p["request_id"] for p in ended] if disconnect else [])
            await lease.release()
    run(exercise())


@pytest.mark.parametrize("ending", ["disconnect", "eof", "error"])
def test_nonterminal_response_usage_leaves_sent_requests_incomplete(tmp_path, monkeypatch, ending):
    from looper.agents.runtimes.runner.turn import RunnerTurn

    async def exercise():
        ended, original = [], RunnerTurn.request_end

        async def request_end(self, params):
            ended.append(dict(params))
            return await original(self, params)

        monkeypatch.setattr(RunnerTurn, "request_end", request_end)
        events = [event("response.created", response={"usage": USAGE})]
        if ending == "error":
            events.append(event("error", code="server_error", message="scripted failure"))
        scripts = [ResponseScript(events=events, disconnect_after=1 if ending == "disconnect" else None)
            for _ in range(4)]
        async with FakeModelAPI(scripts, api_key=KEY) as api, pi_supervisor(tmp_path) as supervisor:
            lease = await supervisor.acquire("pi")
            sid = await lease.open_session(session_params(lease, base_url=api.base_url, key=KEY))
            await begin(lease.bridge, sid, "nonterminal-usage")
            summary = await lease.bridge.wait_settled(sid, "nonterminal-usage")
            assert summary.outcome == "failed"
            assert len(ended) == len(api.requests) == 4
            assert len({p["request_id"] for p in ended}) == 4
            assert all(p["status"] == "error" and not p["complete"] for p in ended)
            assert all(p[field] == 0 for p in ended for field in ("input_uncached", "output", "cache_read", "cache_write"))
            assert not summary.usage_complete and summary.usage.cost_usd is None
            assert (await lease.bridge.health())["inflight_requests"] == 0
            await lease.release()
    run(exercise())


@pytest.mark.parametrize("ending", ["disconnect", "eof", "finish"])
@pytest.mark.parametrize("location,usage", [("chunk", {}), ("chunk", CHAT_USAGE), ("choice", CHAT_USAGE)],
    ids=["placeholder", "cumulative", "choice-cumulative"])
def test_nonterminal_chat_usage_leaves_sent_requests_incomplete(tmp_path, monkeypatch, ending, location, usage):
    from looper.agents.runtimes.runner.turn import RunnerTurn

    async def exercise():
        ended, original = [], RunnerTurn.request_end

        async def request_end(self, params):
            ended.append(dict(params))
            return await original(self, params)

        monkeypatch.setattr(RunnerTurn, "request_end", request_end)
        choice = {"index": 0, "delta": {"content": "Hello"}, "finish_reason": None}
        events = [chat_chunk([{**choice, **({"usage": usage} if location == "choice" else {})}],
            **({"usage": usage} if location == "chunk" else {}))]
        if ending == "finish":
            events += [chat_chunk([{**choice, "delta": {}, "finish_reason": "stop"}]), "[DONE]"]
        attempts = 1 if ending == "finish" else 4
        scripts = [ResponseScript(events=events, disconnect_after=1 if ending == "disconnect" else None)
            for _ in range(attempts)]
        async with FakeModelAPI(scripts, api_key=KEY) as api, pi_supervisor(tmp_path) as supervisor:
            lease = await supervisor.acquire("pi")
            params = session_params(lease, base_url=api.base_url, key=KEY)
            params["provider"]["protocol"] = "openai-chat"
            sid = await lease.open_session(params)
            await begin(lease.bridge, sid, "chat-nonterminal")
            summary = await lease.bridge.wait_settled(sid, "chat-nonterminal")
            assert summary.outcome == ("completed" if ending == "finish" else "failed")
            assert len(ended) == len(api.requests) == attempts
            assert len({p["request_id"] for p in ended}) == attempts
            assert all(not p["complete"] for p in ended)
            assert not summary.usage_complete and summary.usage.cost_usd is None
            assert all(r.path == "/v1/chat/completions" for r in api.requests)
            assert (await lease.bridge.health())["inflight_requests"] == 0
            await lease.release()
    run(exercise())


@pytest.mark.parametrize("usage_location", ["separate", "terminal"])
@pytest.mark.parametrize("usage,location,counts", [
    (CHAT_USAGE, "chunk", (1000, 200, 300, 0)),
    (CHAT_USAGE, "choice", (1000, 200, 300, 0)),
    ({**CHAT_USAGE, "prompt_tokens_details": {"cached_tokens": 300, "cache_write_tokens": 100}}, "chunk", (900, 200, 300, 100)),
    ({"prompt_tokens": 1300, "completion_tokens": 200, "prompt_cache_hit_tokens": 300}, "chunk", (1000, 200, 300, 0)),
    ({"prompt_tokens": 0, "completion_tokens": 0}, "chunk", (0, 0, 0, 0)),
    (None, "chunk", None), ({}, "chunk", None),
    ({**CHAT_USAGE, "completion_tokens": "200"}, "chunk", None),
    ({**CHAT_USAGE, "prompt_tokens": -1}, "chunk", None),
    ({**CHAT_USAGE, "completion_tokens": 0.5}, "chunk", None),
    ({**CHAT_USAGE, "prompt_tokens_details": {"cached_tokens": 1400}}, "chunk", None),
    ({**CHAT_USAGE, "total_tokens": 1400}, "chunk", None),
    ({**CHAT_USAGE, "completion_tokens_details": {"reasoning_tokens": 201}}, "chunk", None),
], ids=["charged", "choice", "cache-write", "deepseek-cache", "zero", "missing", "empty",
    "string", "negative", "fraction", "cache-exceeds-input", "total-mismatch", "reasoning-exceeds-output"])
def test_chat_accounts_only_for_valid_terminal_usage(tmp_path, monkeypatch, usage, location, counts, usage_location):
    from looper.agents.runtimes.runner.turn import RunnerTurn

    async def exercise():
        ended, original = [], RunnerTurn.request_end

        async def request_end(self, params):
            ended.append(dict(params))
            return await original(self, params)

        monkeypatch.setattr(RunnerTurn, "request_end", request_end)
        choice = {"index": 0, "delta": {}, "finish_reason": "stop"}
        events = [chat_chunk([{**choice, "delta": {"content": "Hello"}, "finish_reason": None}], usage=CHAT_USAGE)]
        if location == "choice":
            events.append(chat_chunk([{**choice, "usage": usage}]))
        elif usage_location == "terminal":
            events.append(chat_chunk([choice], usage=usage))
        else:
            events += [chat_chunk([choice]), chat_chunk([], usage=usage)]
        events.append("[DONE]")
        async with FakeModelAPI([ResponseScript(events=events)], api_key=KEY) as api, pi_supervisor(tmp_path) as supervisor:
            lease = await supervisor.acquire("pi")
            params = session_params(lease, base_url=api.base_url, key=KEY)
            params["provider"]["protocol"] = "openai-chat"
            sid = await lease.open_session(params)
            await begin(lease.bridge, sid, "chat-terminal", cap=1 if counts is not None else None)
            summary = await lease.bridge.wait_settled(sid, "chat-terminal")
            assert summary.outcome == "completed"
            assert len(ended) == len(api.requests) == 1
            assert ended[0]["complete"] is (counts is not None)
            assert summary.usage_complete is (counts is not None)
            if counts is None:
                assert summary.usage.cost_usd is None
            else:
                assert tuple(ended[0][field] for field in ("input_uncached", "output", "cache_read", "cache_write")) == counts
                assert (summary.usage.input_tokens, summary.usage.output_tokens,
                    summary.usage.cache_read_tokens, summary.usage.cache_creation_tokens) == counts
                assert summary.usage.cost_usd == round(sum(n * price for n, price in zip(counts, (1, 2, 3, 4))) / 1_000_000, 10)
            await lease.release()
    run(exercise())


@pytest.mark.parametrize("terminal_type", ["failed", "completed", "incomplete"])
@pytest.mark.parametrize("usage,counts", [
    (USAGE, (1000, 200, 300, 0)),
    ({**USAGE, "input_tokens_details": {"cached_tokens": 300, "cache_write_tokens": 100}}, (900, 200, 300, 100)),
    ({"input_tokens": 0, "output_tokens": 0}, (0, 0, 0, 0)),
    (None, None), ({}, None),
    ({**USAGE, "output_tokens": "200"}, None),
    ({**USAGE, "input_tokens": -1}, None),
    ({**USAGE, "output_tokens": 0.5}, None),
    ({**USAGE, "input_tokens_details": {"cached_tokens": 1400}}, None),
], ids=["charged", "cache-write", "zero", "missing", "empty", "string", "negative", "fraction", "cache-exceeds-input"])
def test_response_accounts_for_terminal_usage(tmp_path, monkeypatch, usage, counts, terminal_type):
    from looper.agents.runtimes.runner.turn import RunnerTurn

    async def exercise():
        ended, original = [], RunnerTurn.request_end

        async def request_end(self, params):
            ended.append(dict(params))
            return await original(self, params)

        monkeypatch.setattr(RunnerTurn, "request_end", request_end)
        terminal = failed()
        terminal["type"] = f"response.{terminal_type}"
        terminal["response"]["status"] = terminal_type
        if terminal_type == "incomplete":
            terminal["response"]["incomplete_details"] = {"reason": "max_output_tokens"}
        terminal["response"]["usage"] = usage
        # An earlier usage object must not make a missing/invalid terminal one complete.
        attempts = 4 if terminal_type == "failed" else 1
        scripts = [ResponseScript(events=[event("response.created", response={"usage": USAGE}), terminal]) for _ in range(attempts)]
        async with FakeModelAPI(scripts, api_key=KEY) as api, pi_supervisor(tmp_path) as supervisor:
            lease = await supervisor.acquire("pi")
            sid = await lease.open_session(session_params(lease, base_url=api.base_url, key=KEY))
            await begin(lease.bridge, sid, "failed-usage")
            summary = await lease.bridge.wait_settled(sid, "failed-usage")
            assert summary.outcome == ("completed" if terminal_type == "completed" else "failed")
            assert len(ended) == len(api.requests) == attempts
            assert len({p["request_id"] for p in ended}) == attempts
            complete = terminal_type == "completed" and counts is not None
            assert all(p["complete"] is complete for p in ended)
            assert summary.usage_complete is complete
            if not complete:
                assert summary.usage.cost_usd is None
            else:
                fields = ("input_uncached", "output", "cache_read", "cache_write")
                assert all(tuple(p[field] for field in fields) == counts for p in ended)
                assert (summary.usage.input_tokens, summary.usage.output_tokens,
                    summary.usage.cache_read_tokens, summary.usage.cache_creation_tokens) == tuple(attempts * n for n in counts)
                assert summary.usage.cost_usd == round(attempts * sum(n * price for n, price in zip(counts, (1, 2, 3, 4))) / 1_000_000, 10)
            await lease.release()
    run(exercise())


@pytest.mark.parametrize("protocol", ["openai-responses", "openai-chat"])
def test_https_to_http_port_preserves_complete_zero_usage(tmp_path, monkeypatch, protocol, sent_headers):
    from looper.agents.runtimes.runner.turn import RunnerTurn

    received = []

    class PlainHTTPModelAPI(FakeModelAPI):
        async def _handle(self, reader, writer):
            task = asyncio.current_task()
            self._handlers.add(task)
            self._writers.add(writer)
            try:
                received.append(await reader.read(4096))
                writer.write(b"HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
                await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()
                self._writers.discard(writer)
                self._handlers.discard(task)

    async def exercise():
        environment, sent = sent_headers
        ended, original = [], RunnerTurn.request_end

        async def request_end(self, params):
            ended.append(dict(params))
            return await original(self, params)

        monkeypatch.setattr(RunnerTurn, "request_end", request_end)
        async with PlainHTTPModelAPI([]) as api, pi_supervisor(tmp_path, env=environment) as supervisor:
            # Positive control: the same port accepts plaintext HTTP requests.
            reader, writer = await asyncio.open_connection("127.0.0.1", api._server.sockets[0].getsockname()[1])
            try:
                writer.write(b"POST /v1/responses HTTP/1.1\r\nContent-Length: 2\r\n\r\n{}")
                await writer.drain()
                assert b"400 Bad Request" in await reader.read()
            finally:
                writer.close()
                await writer.wait_closed()
            assert len(received) == 1 and received[0].startswith(b"POST ")
            received.clear()
            lease = await supervisor.acquire("pi")
            params = session_params(lease, base_url=api.base_url.replace("http://", "https://"), key=KEY)
            params["provider"]["protocol"] = protocol
            sid = await lease.open_session(params)
            await begin(lease.bridge, sid, "tls-protocol", cap=1)
            summary = await lease.bridge.wait_settled(sid, "tls-protocol")
            assert summary.outcome == "failed" and summary.cap_reason is None
            assert len(ended) == len(received) == 4
            assert len({p["request_id"] for p in ended}) == 4
            assert all(data.startswith(b"\x16\x03") and KEY.encode() not in data for data in received)
            assert all(p["complete"] for p in ended)
            assert not sent()
            assert all(p[field] == 0 for p in ended for field in ("input_uncached", "output", "cache_read", "cache_write"))
            assert summary.usage_complete and summary.usage.cost_usd == 0
            await lease.release()
    run(exercise())


@pytest.mark.parametrize("expired", [False, True], ids=["untrusted", "expired"])
def test_tls_certificate_failure_preserves_complete_zero_usage(tmp_path, monkeypatch, expired):
    from looper.agents.runtimes.runner.turn import RunnerTurn

    key, cert = tmp_path / "key.pem", tmp_path / "cert.pem"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256",
        "-nodes", "-keyout", str(key), "-out", str(cert), "-days", "1", "-subj", "/CN=localhost"],
        check=True, capture_output=True, timeout=5)
    if expired:
        csr, config = tmp_path / "request.csr", tmp_path / "ca.cnf"
        (tmp_path / "index").touch()
        (tmp_path / "serial").write_text("01\n")
        config.write_text(f"[ca]\ndefault_ca=test\n[test]\ndatabase={tmp_path}/index\n"
            f"serial={tmp_path}/serial\nnew_certs_dir={tmp_path}\ndefault_md=sha256\n"
            "policy=subject\n[subject]\ncommonName=supplied\n")
        subprocess.run(["openssl", "req", "-new", "-key", str(key), "-out", str(csr), "-subj", "/CN=localhost"],
            check=True, capture_output=True, timeout=5)
        expired_cert = tmp_path / "expired.pem"
        subprocess.run(["openssl", "ca", "-batch", "-selfsign", "-config", str(config), "-cert", str(cert),
            "-keyfile", str(key), "-in", str(csr), "-out", str(expired_cert),
            "-startdate", "20000101000000Z", "-enddate", "20000102000000Z"],
            check=True, capture_output=True, timeout=5)
        cert = expired_cert
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)

    class TLSModelAPI(FakeModelAPI):
        async def __aenter__(self):
            self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0, ssl=context)
            self.base_url = f"https://127.0.0.1:{self._server.sockets[0].getsockname()[1]}/v1"
            return self

    async def exercise():
        ended, original = [], RunnerTurn.request_end

        async def request_end(self, params):
            ended.append(dict(params))
            return await original(self, params)

        monkeypatch.setattr(RunnerTurn, "request_end", request_end)
        environment = {"NODE_EXTRA_CA_CERTS": str(cert)} if expired else {}
        async with TLSModelAPI([ResponseScript(status=401)], api_key=KEY) as api, pi_supervisor(tmp_path, env=environment) as supervisor:
            # Positive control: the TLS listener accepts and records HTTP when certificate validation is disabled.
            reader, writer = await asyncio.open_connection("127.0.0.1", api._server.sockets[0].getsockname()[1],
                ssl=ssl._create_unverified_context())
            try:
                writer.write(f"POST /v1/responses HTTP/1.1\r\nAuthorization: Bearer {KEY}\r\nContent-Length: 2\r\n\r\n{{}}".encode())
                await writer.drain()
                assert b"401" in await reader.read()
            finally:
                writer.close()
                await writer.wait_closed()
            assert len(api.requests) == 1
            api.requests.clear()
            lease = await supervisor.acquire("pi")
            sid = await lease.open_session(session_params(lease, base_url=api.base_url, key=KEY))
            await begin(lease.bridge, sid, "tls", cap=1)
            summary = await lease.bridge.wait_settled(sid, "tls")
            assert summary.outcome == "failed" and summary.cap_reason is None
            assert len(ended) == 4 and len({p["request_id"] for p in ended}) == 4
            assert all(p["complete"] for p in ended)
            assert all(p[field] == 0 for p in ended for field in ("input_uncached", "output", "cache_read", "cache_write"))
            assert summary.usage_complete and summary.usage.cost_usd == 0
            assert not api.requests
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
                await lease.bridge.request("session/prompt", {"session_id": sid, "trace_id": "invalid",
                    "text": "hello", "tools": [{"name": "lookup", "description": "read", "inputSchema": {"type": "array"}}]})
            assert error.value.code == -32602
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
    ("tools.builtin", ["unknown"], -32602), ("tools.platform", [{"name": "lookup"}], -32007),
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
            assert summary.outcome == "failed" and not summary.usage_complete
            assert output[-1]["runner"]["error_kind"] == "OUTPUT_TRUNCATED"
            await lease.release()
    run(exercise())
