"""Retries traverse the installed SDK and a loopback provider, with host accounting."""
import asyncio
import json
import time

import pytest

import dsh_installed_stand_in as peer
from dsh_installed_stand_in import MessagesServer, Reply
from dsh_runner_support import guard_env, raw_runner, require_dsh
from looper.agents.runtimes.pricing import ModelPrice
from looper.agents.runtimes.runner import dsh_command
from looper.agents.runtimes.runner.turn import RunnerTurn
from test_dsh_runner import harness
from test_dsh_runner_requests import rewrite_events
from test_dsh_runner_turns import Client, assert_accounted, params_for, run

pytestmark = pytest.mark.installed
BUSY = "Service is too busy. We advise users to temporarily switch to alternative LLM API service providers."


class BudgetClient(Client):
    """Use the real host admission and price reconciliation for each wire frame."""
    def __init__(self, send, receive):
        super().__init__(send, receive)
        self.auto_authorize = False
        self.host = None
        self.summary = None

    async def read(self):
        frame = await super().read()
        if frame.get("method") == "budget/authorize":
            decision = await self.host.authorize(frame["params"])
            await self.send({"jsonrpc": "2.0", "id": frame["id"], "result": decision})
        elif frame.get("method") == "session/update":
            event = frame["params"]
            if event["kind"] == "request_end":
                await self.host.request_end(event)
            elif event["kind"] == "settled":
                self.summary = await self.host.finish(event)
        return frame

    async def session(self, tmp_path, server, *, deny_second=None, run_cap=1):
        await self.start()
        sid = (await self.call("session/new", params_for(tmp_path, server)))["session_id"]

        async def limits():
            return {"run_cap": 0 if deny_second == "run_cap" and len(self.authorized) > 1 else run_cap,
                    "task_cap": None, "task_spent": 0}

        self.host = RunnerTurn(session_id=sid, trace_id="trace", role="manager", tools=None,
            provider_id="account", model="deepseek-v4-pro[1m]", limits=limits,
            max_requests=1 if deny_second == "max_requests" else 30,
            price=lambda model, usage: ModelPrice(1, 2, 3, 4).cost(usage))
        return sid


async def diagnostics(process):
    process.stdin.close()
    assert await asyncio.wait_for(process.wait(), 5) == 0
    return (await process.stderr.read()).decode()


def test_busy_retries_are_separately_authorized_and_priced(tmp_path):
    async def exercise():
        _, installed = require_dsh()
        with MessagesServer([Reply(status=503, error_message=BUSY)] * 2 + [["Recovered"]]) as server:
            async with raw_runner(tmp_path, installed, env=guard_env(tmp_path, server.port)) as (process, send, receive):
                client = BudgetClient(send, receive)
                sid = await client.session(tmp_path, server)
                await client.prompt(sid)
                result = await client.settled(sid)
                ends = assert_accounted(client, sid, count=3)
                assert result["outcome"] == "completed" and result["final_text"] == "Recovered"
                assert result["usage_complete"]
                assert len(client.authorized) == 3
                assert [r["request_id"] for r in server.requests] == [e["request_id"] for e in ends]
                assert [e["status"] for e in ends] == ["http_503", "http_503", "completed"]
                for end in ends[:2]:
                    assert end["complete"]
                    assert [end[k] for k in ("input_uncached", "output", "cache_read", "cache_write")] == [0] * 4
                assert (ends[-1]["input_uncached"], ends[-1]["output"]) == (11, 5)
                assert client.summary.usage.cost_usd == pytest.approx(0.000021)
                lines = [line for line in (await diagnostics(process)).splitlines() if line.startswith("provider_retry ")]
                assert len(lines) == 2
                for number, line in enumerate(lines, 1):
                    assert f"request_id={ends[number - 1]['request_id']}" in line
                    assert "status=503" in line and f"attempt={number}" in line and "delay_ms=" in line
                    delay = int(line.split("delay_ms=", 1)[1].split()[0])
                    base = 1000 * 2 ** (number - 1)
                    assert base <= delay <= base * 1.2
                    assert "synthetic-dsh-runner-key" not in line and BUSY not in line
    run(exercise())


@pytest.mark.parametrize("usage,cost", [
    ({"input": 11, "output": 0, "cache_read": 0, "cache_write": 0}, 0.000032),
    ({"input": 13, "output": 0, "cache_read": 2, "cache_write": 3}, 0.000052),
])
def test_empty_response_retry_preserves_terminal_usage(tmp_path, usage, cost):
    async def exercise():
        _, installed = require_dsh()
        with MessagesServer([Reply(usage=usage), ["Recovered"]]) as server:
            async with raw_runner(tmp_path, installed, env=guard_env(tmp_path, server.port)) as (process, send, receive):
                client = BudgetClient(send, receive)
                sid = await client.session(tmp_path, server, run_cap=1)
                await client.prompt(sid)
                result = await client.settled(sid)
                ends = assert_accounted(client, sid, count=2)
                assert result["outcome"] == "completed" and result["final_text"] == "Recovered"
                assert result["usage_complete"] and all(end["complete"] for end in ends)
                assert ends[0]["status"] == "error" and ends[1]["status"] == "completed"
                assert [ends[0][k] for k in ("input_uncached", "output", "cache_read", "cache_write")] == [
                    usage[k] for k in ("input", "output", "cache_read", "cache_write")]
                assert (ends[1]["input_uncached"], ends[1]["output"]) == (11, 5)
                assert len(server.requests) == len(client.authorized) == 2
                assert client.summary.usage.cost_usd == pytest.approx(cost)
                assert "code=EMPTY_RESPONSE" in await diagnostics(process)
    run(exercise())


@pytest.mark.parametrize("status", [401, 429, 503])
@pytest.mark.parametrize("run_cap", [1, None])
def test_truncated_http_rejection_retries_with_complete_accounting(tmp_path, status, run_cap):
    async def exercise():
        _, installed = require_dsh()
        with MessagesServer([Reply(status=status, cut=True), ["Recovered"]]) as server:
            async with raw_runner(tmp_path, installed, env=guard_env(tmp_path, server.port)) as (process, send, receive):
                client = BudgetClient(send, receive)
                sid = await client.session(tmp_path, server, run_cap=run_cap)
                await client.prompt(sid)
                result = await client.settled(sid)
                ends = assert_accounted(client, sid, count=2)
                assert result["outcome"] == "completed" and result["final_text"] == "Recovered"
                assert result["usage_complete"] and all(end["complete"] for end in ends)
                assert [end["status"] for end in ends] == [f"http_{status}", "completed"]
                assert [ends[0][k] for k in ("input_uncached", "output", "cache_read", "cache_write")] == [0] * 4
                assert (ends[1]["input_uncached"], ends[1]["output"]) == (11, 5)
                assert len(server.requests) == len(client.authorized) == 2
                assert client.summary.usage.cost_usd == pytest.approx(0.000021)
                log = await diagnostics(process)
                assert "code=TRANSPORT" in log and "usage_incomplete" not in log
    run(exercise())


def test_truncated_http_rejection_exhaustion_remains_completely_accounted(tmp_path):
    async def exercise():
        _, installed = require_dsh()
        with MessagesServer([Reply(status=401, cut=True)] * 4) as server:
            async with raw_runner(tmp_path, installed, env=guard_env(tmp_path, server.port)) as (process, send, receive):
                client = BudgetClient(send, receive)
                sid = await client.session(tmp_path, server, run_cap=1)
                await client.prompt(sid)
                result = await client.settled(sid)
                ends = assert_accounted(client, sid, count=4)
                assert result["outcome"] == "failed" and result["usage_complete"]
                assert all(end["complete"] and end["status"] == "http_401" for end in ends)
                assert all([end[k] for k in ("input_uncached", "output", "cache_read", "cache_write")] == [0] * 4
                           for end in ends)
                assert len(server.requests) == len(client.authorized) == 4
                assert client.summary.usage.cost_usd == 0
                log = await diagnostics(process)
                assert log.count("provider_retry ") == 3 and "usage_incomplete" not in log
    run(exercise())


def test_retry_after_waits_before_dispatch(tmp_path):
    async def exercise():
        _, installed = require_dsh()
        with MessagesServer([Reply(status=429, headers={"Retry-After": "1"}), ["Recovered"]]) as server:
            async with raw_runner(tmp_path, installed, env=guard_env(tmp_path, server.port)) as (_, send, receive):
                client = BudgetClient(send, receive)
                sid = await client.session(tmp_path, server)
                await client.prompt(sid)
                assert (await client.settled(sid))["outcome"] == "completed"
                assert_accounted(client, sid, count=2)
                assert (server.requests[1]["at"] - server.requests[0]["at"]) / 1e9 >= 1
    run(exercise())


def test_retry_exhaustion_preserves_last_provider_error(tmp_path):
    async def exercise():
        _, installed = require_dsh()
        replies = [Reply(status=503, error_message=f"{BUSY} rejection {i}") for i in range(1, 5)]
        with MessagesServer(replies) as server:
            async with raw_runner(tmp_path, installed, env=guard_env(tmp_path, server.port)) as (process, send, receive):
                client = BudgetClient(send, receive)
                sid = await client.session(tmp_path, server)
                await client.prompt(sid)
                result = await client.settled(sid)
                assert result["outcome"] == "failed"
                assert result["error"]["message"] == f"{BUSY} rejection 4 (after 4 attempts)"
                assert len(server.requests) == len(client.authorized) == 4
                assert_accounted(client, sid, count=4)
                assert len([line for line in (await diagnostics(process)).splitlines()
                            if line.startswith("provider_retry ")]) == 3
    run(exercise())


@pytest.mark.parametrize("reason,outcome", [("run_cap", "capped"), ("max_requests", "max_requests")])
def test_second_attempt_denial_stops_dispatch(tmp_path, reason, outcome):
    async def exercise():
        _, installed = require_dsh()
        with MessagesServer([Reply(status=503), ["Must stay unsent"]]) as server:
            async with raw_runner(tmp_path, installed, env=guard_env(tmp_path, server.port)) as (_, send, receive):
                client = BudgetClient(send, receive)
                sid = await client.session(tmp_path, server, deny_second=reason)
                await client.prompt(sid)
                result = await client.settled(sid)
                assert result["outcome"] == outcome and result["usage_complete"]
                assert len(client.authorized) == 2 and len(server.requests) == 1
                assert_accounted(client, sid)
    run(exercise())


@pytest.mark.parametrize("control", ["cancel", "close", "settle"])
def test_stop_interrupts_backoff(tmp_path, monkeypatch, control):
    if control == "settle":
        source = dsh_command.entry().resolve().parent
        harness(tmp_path, monkeypatch, f"""rpc.method('test/settle', async p => {{
  const {{sessions}} = await import({json.dumps((source / 'session.ts').as_uri())});
  const {{settle}} = await import({json.dumps((source / 'turn.ts').as_uri())});
  const record = sessions.get(p.session_id);
  await settle(record, record.turn, {{kind: 'INTERNAL_ERROR', message: 'Synthetic settlement'}});
  return {{}};
}});""")
    async def exercise():
        _, installed = require_dsh()
        with MessagesServer([Reply(status=503, headers={"Retry-After": "120"}), ["Must stay unsent"]]) as server:
            async with raw_runner(tmp_path, installed, env=guard_env(tmp_path, server.port)) as (process, send, receive):
                client = BudgetClient(send, receive)
                sid = await client.session(tmp_path, server)
                await client.prompt(sid)
                while not client.updates(sid, kind="request_end"):
                    await client.read()
                async def retry_line():
                    while line := await process.stderr.readline():
                        if line.startswith(b"provider_retry "):
                            return line.decode()
                    raise AssertionError("retry did not enter backoff")
                line = await asyncio.wait_for(retry_line(), 2)
                assert "delay_ms=30000" in line
                started = time.monotonic()
                method = "test/settle" if control == "settle" else f"session/{control}"
                await client.call(method, {"session_id": sid, "trace_id": "trace"})
                result = await asyncio.wait_for(client.settled(sid), 2)
                expected = "failed" if control == "settle" else "cancelled"
                assert result["outcome"] == expected and time.monotonic() - started < 2
                assert len(client.authorized) == len(server.requests) == 1
                assert_accounted(client, sid)
                assert (await client.call("runner/health"))["inflight_requests"] == 0
    run(exercise())


@pytest.mark.parametrize("status", [408, 409, 425, 500, 502, 504, 529])
def test_transient_http_statuses_are_retried(tmp_path, status):
    async def exercise():
        _, installed = require_dsh()
        # Force a nonretryable SDK code so this proves the HTTP status table.
        with MessagesServer([Reply(status=status, error_type="invalid_request_error"), ["Recovered"]]) as server:
            async with raw_runner(tmp_path, installed, env=guard_env(tmp_path, server.port)) as (_, send, receive):
                client = BudgetClient(send, receive)
                sid = await client.session(tmp_path, server)
                await client.prompt(sid)
                assert (await client.settled(sid))["outcome"] == "completed"
                ends = assert_accounted(client, sid, count=2)
                assert len(server.requests) == 2 and ends[0]["status"] == f"http_{status}"
    run(exercise())


@pytest.mark.parametrize("error_type", ["rate_limit_error", "api_error", "overloaded_error"])
@pytest.mark.parametrize("run_cap", [1, None])
def test_sdk_retryable_stream_errors_before_output_recover(tmp_path, monkeypatch, error_type, run_cap):
    original = peer.messages_wire

    def wire(reply, index):
        if index == 0:
            return f"event: error\ndata: {json.dumps({'type': 'error', 'error': {'type': error_type, 'message': BUSY}})}\n\n"
        return original(reply, index)

    monkeypatch.setattr(peer, "messages_wire", wire)

    async def exercise():
        _, installed = require_dsh()
        with MessagesServer([["unused"], ["Recovered"]]) as server:
            async with raw_runner(tmp_path, installed, env=guard_env(tmp_path, server.port)) as (_, send, receive):
                client = BudgetClient(send, receive)
                sid = await client.session(tmp_path, server, run_cap=run_cap)
                await client.prompt(sid)
                result = await client.settled(sid)
                assert result["outcome"] == "completed" and result["final_text"] == "Recovered"
                ends = assert_accounted(client, sid, count=2)
                assert ends[0]["status"] == "error" and ends[0]["complete"]
                assert [ends[0][k] for k in ("input_uncached", "output", "cache_read", "cache_write")] == [0] * 4
                assert (ends[1]["input_uncached"], ends[1]["output"]) == (11, 5)
                assert len(server.requests) == 2
                assert result["usage_complete"]
                assert client.summary.usage.cost_usd == pytest.approx(0.000021)
    run(exercise())


@pytest.mark.parametrize("case", ["text", "block-start", "400"])
def test_output_and_bad_requests_are_not_retried(tmp_path, monkeypatch, case):
    if case == "block-start":
        def start_only(events):
            events[1]["content_block"]["text"] = ""
            return events[:2] + [{"type": "error", "error": {"type": "overloaded_error", "message": BUSY}}]
        rewrite_events(monkeypatch, start_only)
    async def exercise():
        _, installed = require_dsh()
        reply = Reply(["Partial output"], cut=case == "text", status=400 if case == "400" else 200)
        with MessagesServer([reply]) as server:
            async with raw_runner(tmp_path, installed, env=guard_env(tmp_path, server.port)) as (_, send, receive):
                client = BudgetClient(send, receive)
                sid = await client.session(tmp_path, server)
                await client.prompt(sid)
                result = await client.settled(sid)
                assert result["outcome"] == "failed"
                assert len(client.authorized) == len(server.requests) == 1
                assert_accounted(client, sid)
                if case == "text":
                    assert client.updates(sid, kind="text_delta")
                elif case == "block-start":
                    assert not client.updates(sid, kind="text_delta")
    run(exercise())


@pytest.mark.parametrize("run_cap", [1, None])
def test_connection_lost_before_headers_is_retried(tmp_path, run_cap):
    async def exercise():
        _, installed = require_dsh()
        with MessagesServer([Reply(disconnect=True), ["Recovered"]]) as server:
            async with raw_runner(tmp_path, installed, env=guard_env(tmp_path, server.port)) as (_, send, receive):
                client = BudgetClient(send, receive)
                sid = await client.session(tmp_path, server, run_cap=run_cap)
                await client.prompt(sid)
                result = await client.settled(sid)
                assert result["outcome"] == "completed"
                ends = assert_accounted(client, sid, count=2)
                assert len(server.requests) == 2 and ends[0]["status"] == "error"
                assert ends[0]["complete"]
                assert [ends[0][k] for k in ("input_uncached", "output", "cache_read", "cache_write")] == [0] * 4
                assert (ends[1]["input_uncached"], ends[1]["output"]) == (11, 5)
                assert result["usage_complete"]
                assert client.summary.usage.cost_usd == pytest.approx(0.000021)
    run(exercise())
