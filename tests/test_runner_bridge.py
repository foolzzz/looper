"""Transport proofs use real pipes and the production wire codec."""
import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
import json
import os
from pathlib import Path

import pytest

from looper.agents.run_usage import Usage
from looper.agents.runtimes.pricing import ModelPrice, usage_event
from looper.agents.runtimes.runner import protocol as p
from looper.agents.runtimes.runner.bridge import RpcFailure, RunnerBridge
from looper.agents.runtimes.runner.turn import RunnerTurn


async def pipe():
    read_fd, write_fd = os.pipe()
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader()
    read_transport, _ = await loop.connect_read_pipe(
        lambda: asyncio.StreamReaderProtocol(reader), os.fdopen(read_fd, "rb"))
    protocol = asyncio.StreamReaderProtocol(asyncio.StreamReader())
    transport, _ = await loop.connect_write_pipe(lambda: protocol, os.fdopen(write_fd, "wb"))
    return reader, asyncio.StreamWriter(transport, protocol, None, loop), read_transport


@asynccontextmanager
async def connection(handshake=None, **options):
    host_reader, runner_writer, rt = await pipe()
    runner_reader, host_writer, ht = await pipe()
    failures = []
    bridge = RunnerBridge(host_reader, host_writer, "fake", on_failure=failures.append, **options)
    try:
        init = asyncio.create_task(bridge.initialize())
        req = await p.read_frame(runner_reader)
        caps = {"cancel": True, "usage": {"per_request": True}, "authorize_covers": ["turn"],
                "steer": {"in_trace": True, "boundary": "after_tool_batch", "idle_start": False}}
        result = {"protocol_version": "0.1",
            "runner": {"kind": "fake", "version": "1"}, "sdk": {"name": "fake", "version": "1"},
            "capabilities": caps, **(handshake or {})}
        await send(runner_writer, p.Response(req.id, result=result))
        await init
        yield bridge, runner_reader, runner_writer, failures
    finally:
        runner_writer.close()
        await bridge.close()
        rt.close()
        ht.close()
        await runner_writer.wait_closed()


async def send(writer, message):
    writer.write(p.encode_frame(message))
    await writer.drain()


async def update(writer, kind, **fields):
    await send(writer, p.Notification("session/update", dict(session_id="s", trace_id="t", kind=kind, **fields)))


NEW = next(c["message"]["params"] for c in json.loads(
    (Path(__file__).resolve().parents[1] / "runners/protocol/cases.json").read_text())
    if c["name"] == "new")


async def open_turn(bridge, reader, writer, turn=None, events=None):
    opening = asyncio.create_task(bridge.open_session(NEW))
    req = await p.read_frame(reader)
    await send(writer, p.Response(req.id, result={"session_id": "s"}))
    assert await opening == "s"
    turn, events = turn or Turn(), events if events is not None else []
    beginning = asyncio.create_task(bridge.begin_turn("s", "t", events.append, turn, text="hello"))
    req = await p.read_frame(reader)
    return turn, events, beginning, req


async def accept(writer, req):
    await send(writer, p.Response(req.id, result={"session_id": "s", "trace_id": "t", "accepted": True}))


@dataclass
class Summary:
    usage: Usage
    totals: Usage
    model_usage: dict
    outcome: str
    cap_reason: str | None
    usage_complete: bool


class Turn:
    def __init__(self, gate=None):
        self.gate, self.entered = gate, asyncio.Event()
        self.usage, self.stopped = Usage(), False

    def stop(self):
        self.stopped = True

    async def call_tool(self, params):
        self.entered.set()
        if self.gate:
            await self.gate.wait()
        return {"text": "business failure", "is_error": True, "ran": True}

    async def authorize(self, params):
        self.entered.set()
        if self.gate:
            await self.gate.wait()
        return {"allow": True}

    async def request_end(self, params):
        usage = Usage(input_tokens=params["input_uncached"], output_tokens=params["output"],
            cache_read_tokens=params["cache_read"], cache_creation_tokens=params["cache_write"])
        self.usage = self.usage.plus(usage)
        return usage_event(params["model"], usage,
            trace_id=params["trace_id"], request_id=params["request_id"], usage_complete=params["complete"])

    def finish(self, params):
        usage = replace(self.usage, cost_usd=ModelPrice(1, 2, 3, 4).cost(self.usage)
                        if params["usage_complete"] else None)
        # A previous resident turn is deliberately included only in the summary's session totals.
        return Summary(usage, usage.plus(Usage(input_tokens=100)), {"model": {
            "inputTokens": (usage.input_tokens or 0) + 100, "outputTokens": usage.output_tokens,
            "cacheReadInputTokens": usage.cache_read_tokens, "cacheCreationInputTokens": usage.cache_creation_tokens}},
                       params["outcome"], "run_cap" if params["outcome"] == "capped" else None,
                       params["usage_complete"])


def run(coro):
    asyncio.run(asyncio.wait_for(coro, 5))


@pytest.mark.parametrize("budget", ["uncapped", "reserved", "capped"])
def test_connection_loss_settles_all_real_turns_and_releases_reservations(budget):
    async def exercise():
        async with connection() as (bridge, reader, writer, failures):
            turns, results = [], []
            caps = {"run_cap": None if budget == "uncapped" else 1, "task_cap": None, "task_spent": 0}
            async def limits():
                return caps
            async def cancel(reason):
                # Loss settlement must not try to cancel through a dead connection.
                assert bridge._error is None
            for sid in ("s", "other"):
                opening = asyncio.create_task(bridge.open_session(dict(NEW, session_key=sid)))
                request = await p.read_frame(reader)
                await send(writer, p.Response(request.id, result={"session_id": sid}))
                assert await opening == sid
                turn = RunnerTurn(session_id=sid, trace_id="t", role="manager", tools=None,
                    provider_id="account", model="model", price=lambda model, usage: ModelPrice(1, 2, 3, 4).cost(usage),
                    limits=limits, cancel=cancel)
                events = []
                usage_seen = asyncio.Event()
                def on_event(event, events=events, usage_seen=usage_seen):
                    events.append(event)
                    if event["type"] == "usage":
                        usage_seen.set()
                beginning = asyncio.create_task(bridge.begin_turn(sid, "t", on_event, turn, text="hello"))
                request = await p.read_frame(reader)
                await send(writer, p.Response(request.id, result={"session_id": sid, "trace_id": "t", "accepted": True}))
                await beginning
                for number, request_id in enumerate(("done", "pending"), 1):
                    await send(writer, p.Request(f"r-{number}", "budget/authorize", dict(session_id=sid,
                        trace_id="t", request_id=request_id, kind="turn", provider="account", model="model",
                        estimated_input_tokens=5, max_output_tokens=2)))
                    assert (await p.read_frame(reader)).result["allow"]
                await send(writer, p.Notification("session/update", dict(session_id=sid, trace_id="t",
                    kind="request_end", request_id="done", request_kind="turn", model="model", status="ok",
                    input_uncached=5, output=2, cache_read=0, cache_write=0, reasoning=None, complete=True)))
                await usage_seen.wait()
                turns.append(turn)
                results.append(events)
            if budget == "capped":
                caps["run_cap"] = 0
                for turn in turns:
                    denied = await turn.authorize(dict(session_id=turn.session_id, trace_id="t", request_id="denied",
                        kind="turn", provider="account", model="model", estimated_input_tokens=1, max_output_tokens=1))
                    assert denied["reason"] == "run_cap"
            writer.close()
            summaries = await asyncio.gather(*(bridge.wait_settled(sid, "t") for sid in ("s", "other")))
            await bridge.close()
            await bridge.close()
            assert len(failures) == 1
            for turn, events, summary in zip(turns, results, summaries):
                assert summary.outcome == "lost" and not summary.usage_complete
                assert summary.usage.input_tokens == 5 and summary.usage.output_tokens == 2
                assert summary.usage.cost_usd is summary.totals.cost_usd is None
                assert not turn._requests  # No outstanding monetary reservation survives loss.
                terminals = [event for event in events if event["type"] == "result"]
                assert len(terminals) == 1
                assert terminals[0]["runner"]["outcome"] == "lost"
                assert terminals[0]["errors"] == ["runner-exited: stdout EOF"]
                assert terminals[0]["num_turns"] == 1
                assert (await bridge.wait_settled(turn.session_id, "t")) is summary
    run(exercise())


def test_loss_settles_usage_when_inflight_settings_read_fails():
    async def exercise():
        async with connection() as (bridge, reader, writer, failures):
            reading, release = asyncio.Event(), asyncio.Event()
            reads = 0
            async def limits():
                nonlocal reads
                reads += 1
                if reads > 1:
                    reading.set()
                    await release.wait()
                    raise OSError("settings unavailable")
                return {"run_cap": 1, "task_cap": None, "task_spent": 0}
            turn = RunnerTurn(session_id="s", trace_id="t", role="manager", tools=None,
                provider_id="account", model="model", price=lambda model, usage: 0.01, limits=limits)
            _, events, beginning, prompt = await open_turn(bridge, reader, writer, turn)
            await accept(writer, prompt)
            await beginning
            await send(writer, p.Request("r-1", "budget/authorize", dict(session_id="s", trace_id="t",
                request_id="q", kind="turn", provider="account", model="model",
                estimated_input_tokens=5, max_output_tokens=2)))
            assert (await p.read_frame(reader)).result["allow"]
            await update(writer, "request_end", request_id="q", request_kind="turn", model="model", status="ok",
                input_uncached=5, output=2, cache_read=0, cache_write=0, reasoning=None, complete=True)
            await reading.wait()
            writer.close()
            # EOF is observed while the usage callback is still in flight.
            assert await reader.read() == b""
            release.set()
            summary = await bridge.wait_settled("s", "t")
            assert summary.outcome == "lost" and not summary.usage_complete
            assert summary.usage.input_tokens == 5 and summary.usage.cost_usd is None
            assert not turn._requests and reads == 2
            terminals = [event for event in events if event["type"] == "result"]
            assert len(terminals) == 1
            assert terminals[0]["errors"] == ["runner-exited: stdout EOF"]
            assert len(failures) == 1
    run(exercise())


def test_scripted_stream_order_and_interleaved_responses():
    async def exercise():
        async with connection() as (bridge, reader, writer, _):
            turn, events, beginning, prompt = await open_turn(bridge, reader, writer)
            # Events and a reverse call may precede acceptance.
            await update(writer, "text_delta", text="hello\u2028world")
            tool = dict(session_id="s", trace_id="t", call_id="call", name="looper_tasks", arguments={})
            await send(writer, p.Request("r-1", "tools/call", tool))
            reply = await p.read_frame(reader)
            assert reply.result == dict(session_id="s", trace_id="t", text="business failure", is_error=True, ran=True)
            await send(writer, p.Request("r-2", "budget/authorize", dict(session_id="s", trace_id="t", request_id="q",
                kind="turn", provider="account", model="model", estimated_input_tokens=5, max_output_tokens=2)))
            assert (await p.read_frame(reader)).result["allow"] is True
            await accept(writer, prompt)
            await beginning
            first = asyncio.create_task(bridge.request("session/cancel", {"session_id": "s", "trace_id": "t"}))
            second = asyncio.create_task(bridge.request("session/cancel", {"session_id": "s", "trace_id": "t"}))
            a, b = await p.read_frame(reader), await p.read_frame(reader)
            for req in (b, a):
                await send(writer, p.Response(req.id, result=req.params))
            assert await first == await second == {"session_id": "s", "trace_id": "t"}
            steering = asyncio.create_task(bridge.steer("s", "t", "m", "guidance"))
            req = await p.read_frame(reader)
            await send(writer, p.Response(req.id, result={"session_id": "s", "trace_id": "t", "status": "queued"}))
            assert (await steering)["status"] == "queued"
            assert not any(e.get("subtype") == "steer_applied" for e in events)
            content = [{"type": "text", "text": "verbatim"},
                       {"type": "tool_use", "id": "call", "name": "looper_tasks", "input": {}}]
            await update(writer, "message", model="model", content=content)
            await update(writer, "tool_result", call_id="call", text="answer", is_error=False)
            await update(writer, "request_end", request_id="q", request_kind="turn", model="model", status="ok",
                         input_uncached=5, output=2, cache_read=3, cache_write=4, reasoning=9, complete=True)
            await update(writer, "steer_applied", message_id="m", after_request_id="q", after_tool_batch=True, new_turn=False)
            await update(writer, "settled", outcome="completed", final_text="done", requests=1, usage_complete=True)
            summary = await bridge.wait_settled("s", "t")
            assert [e["type"] for e in events] == ["system", "stream_event", "assistant", "user", "usage", "system", "result"]
            assert events[0]["subtype"] == "init" and events[0]["session_id"] == "s"
            assert events[1]["event"]["delta"] == {"type": "text_delta", "text": "hello\u2028world"}
            assert events[2]["message"] == {"role": "assistant", "model": "model", "content": content}
            assert events[3]["message"]["content"][0]["tool_use_id"] == "call"
            assert events[4]["usage"] == dict(input_tokens=5, output_tokens=2, cache_read_tokens=3, cache_creation_tokens=4)
            assert events[-1]["subtype"] == "success" and events[-1]["result"] == "done"
            assert bridge.sessions["s"].result_event["trace_id"] == "t"
            assert events[-1]["modelUsage"] is summary.model_usage
            assert summary.usage.input_tokens == 5 and events[-1]["modelUsage"]["model"]["inputTokens"] == 105
            assert summary.usage.cost_usd == 0.000034
    run(exercise())


@pytest.mark.parametrize("code", [p.ErrorCode.PROVIDER_AUTH, p.ErrorCode.SESSION_BUSY,
                                 p.ErrorCode.CAPACITY, p.ErrorCode.INVALID_PARAMS])
def test_prompt_rejection_is_local_and_session_can_retry(code):
    async def exercise():
        async with connection() as (bridge, reader, writer, failures):
            active_turn, events, beginning, prompt = await open_turn(bridge, reader, writer)
            await accept(writer, prompt)
            await beginning
            opening = asyncio.create_task(bridge.open_session(dict(NEW, session_key="other")))
            request = await p.read_frame(reader)
            await send(writer, p.Response(request.id, result={"session_id": "other"}))
            await opening
            rejected_turn, rejected_events = Turn(), []
            beginning = asyncio.create_task(bridge.begin_turn(
                "other", "rejected", rejected_events.append, rejected_turn, text="hello",
                tools=[{"name": "unknown", "description": "unknown tool", "inputSchema": {}}]))
            request = await p.read_frame(reader)
            entry = bridge.sessions["other"]
            worker = entry.worker
            await send(writer, p.Response(request.id, error={"code": int(code), "message": code.name,
                "data": {"session_id": "other", "trace_id": "rejected"}}))
            with pytest.raises(RpcFailure) as failure:
                await beginning
            assert failure.value.code == code
            assert entry.state == "idle" and worker.done() and entry.stopped
            assert rejected_turn.stopped and not rejected_events
            assert bridge.sessions["s"].state == "running" and not active_turn.stopped
            assert not failures and not bridge.writer.is_closing()
            await update(writer, "settled", outcome="completed", final_text="active done", requests=0,
                         usage_complete=True)
            assert (await bridge.wait_settled("s", "t")).outcome == "completed"
            assert events[-1]["result"] == "active done"
            retry_events = []
            retry = asyncio.create_task(bridge.begin_turn(
                "other", "retry", retry_events.append, Turn(), text="retry"))
            request = await p.read_frame(reader)
            await send(writer, p.Response(request.id, result={"session_id": "other", "trace_id": "retry",
                "accepted": True}))
            await retry
            await send(writer, p.Notification("session/update", dict(kind="settled", session_id="other",
                trace_id="retry", outcome="completed", final_text="retry done", requests=0, usage_complete=True)))
            assert (await bridge.wait_settled("other", "retry")).outcome == "completed"
            assert retry_events[-1]["result"] == "retry done" and not rejected_events
            assert not failures
    run(exercise())


@pytest.mark.parametrize("fault", ["eof", "timeout"])
@pytest.mark.parametrize("settled", [False, True], ids=["missing-settled", "queued-settled"])
def test_missing_acceptance_preserves_usage_and_emits_one_terminal(fault, settled):
    async def exercise():
        async with connection(control_timeout=0.1) as (bridge, reader, writer, failures):
            turn, events, beginning, _ = await open_turn(bridge, reader, writer)
            await update(writer, "request_end", request_id="q", request_kind="turn", model="model", status="ok",
                         input_uncached=5, output=2, cache_read=3, cache_write=4, reasoning=None, complete=True)
            if settled:
                await update(writer, "settled", outcome="completed", final_text="done", requests=1,
                             usage_complete=True)
            if fault == "eof":
                writer.close()
            with pytest.raises(ConnectionError if fault == "eof" else TimeoutError):
                await beginning
            summary = await bridge.wait_settled("s", "t")
            assert summary.outcome == ("completed" if settled else "lost")
            assert summary.usage.input_tokens == 5 and summary.usage.output_tokens == 2
            assert summary.usage_complete is settled
            assert [event["type"] for event in events] == ["usage", "result"]
            terminal = events[-1]
            assert terminal["result"] == ("done" if settled else "")
            assert terminal["subtype"] == ("success" if settled else "error_during_execution")
            assert terminal["is_error"] is not settled
            assert terminal["runner"]["outcome"] == summary.outcome
            assert terminal["usage_complete"] is settled
            assert terminal["session_id"] == "s" and terminal["trace_id"] == "t"
            assert terminal["usage"]["input_tokens"] == 5
            assert terminal["total_cost_usd"] == summary.totals.cost_usd
            assert turn.stopped and not bridge.sessions["s"].accepted
            await bridge.close()
            assert len(failures) == 1
            assert len([event for event in events if event["type"] == "result"]) == 1
    run(exercise())


@pytest.mark.parametrize("method", ["tools/call", "budget/authorize"])
def test_cancel_settles_while_reverse_callback_is_pending_and_rejects_stale_trace(method):
    async def exercise():
        gate = asyncio.Event()
        async with connection() as (bridge, reader, writer, _):
            turn, events, beginning, prompt = await open_turn(bridge, reader, writer, Turn(gate))
            await accept(writer, prompt)
            await beginning
            tool = dict(session_id="s", trace_id="stale", call_id="c", name="looper_tasks", arguments={})
            if method == "budget/authorize":
                tool = dict(session_id="s", trace_id="stale", request_id="q", kind="turn", provider="account",
                            model="model", estimated_input_tokens=1, max_output_tokens=1)
            await send(writer, p.Request("r-1", method, tool))
            assert (await p.read_frame(reader)).error["code"] == p.ErrorCode.TRACE_MISMATCH
            assert not turn.entered.is_set()
            await send(writer, p.Request("r-2", method, dict(tool, trace_id="t")))
            await turn.entered.wait()
            cancelling = asyncio.create_task(bridge.cancel_turn("s", "t"))
            cancel = await p.read_frame(reader)
            assert turn.stopped
            await update(writer, "settled", outcome="cancelled", final_text="", requests=0, usage_complete=True)
            summary = await bridge.wait_settled("s", "t")
            assert summary.outcome == "cancelled" and events[-1]["subtype"] == "error_user_stop"
            await send(writer, p.Response(cancel.id, result=cancel.params))
            await cancelling
            gate.set()
            late = (await p.read_frame(reader)).result
            assert late.get("ran") is True if method == "tools/call" else late["reason"] == "stopped"
            await send(writer, p.Request("r-3", method, dict(tool, trace_id="t")))
            stopped = (await p.read_frame(reader)).result
            assert stopped.get("ran") is False if method == "tools/call" else stopped["allow"] is False
    run(exercise())


@pytest.mark.parametrize("fault", [b"", b"not json\n", b"x" * (p.MAX_FRAME_BYTES + 1) + b"\n", "mismatch"],
                         ids=["eof", "malformed", "oversized", "wrong-trace"])
def test_connection_fault_fails_pending_and_emits_one_lost_terminal(fault):
    async def exercise():
        async with connection() as (bridge, reader, writer, failures):
            _, events, beginning, prompt = await open_turn(bridge, reader, writer)
            await accept(writer, prompt)
            await beginning
            await update(writer, "request_end", request_id="q", request_kind="turn", model="model", status="ok",
                         input_uncached=5, output=2, cache_read=3, cache_write=4, reasoning=None, complete=True)
            pending = asyncio.create_task(bridge.request("session/cancel", {"session_id": "s", "trace_id": "t"}))
            control = await p.read_frame(reader)
            if fault == "mismatch":
                await send(writer, p.Response(control.id, result={"session_id": "s", "trace_id": "wrong"}))
            elif fault:
                writer.write(fault)
                try:
                    await writer.drain()
                except BrokenPipeError:
                    pass
            else:
                writer.close()
            with pytest.raises(ConnectionError):
                await pending
            summary = await bridge.wait_settled("s", "t")
            assert summary.outcome == "lost" and not summary.usage_complete
            assert summary.usage.input_tokens == 5
            assert summary.usage.cost_usd is None
            terminal = [e for e in events if e["type"] == "result"]
            assert len(terminal) == 1 and terminal[0]["subtype"] == "error_during_execution"
            assert terminal[0]["errors"] and len(failures) == 1
            await bridge.close()
    run(exercise())


@pytest.mark.parametrize("outcome,subtype", [("capped", "error_max_budget_usd"),
    ("max_requests", "error_max_turns"), ("failed", "error_during_execution")])
def test_terminal_outcomes_and_unknown_reverse_method(outcome, subtype):
    async def exercise():
        async with connection() as (bridge, reader, writer, _):
            _, events, beginning, prompt = await open_turn(bridge, reader, writer)
            await accept(writer, prompt)
            await beginning
            # Unknown methods cannot be constructed with the schema-checked encoder.
            writer.write(b'{"jsonrpc":"2.0","id":"r-9","method":"future/call","params":{}}\n')
            await writer.drain()
            assert (await p.read_frame(reader)).error["code"] == p.ErrorCode.METHOD_NOT_FOUND
            await update(writer, "settled", outcome=outcome, final_text="", requests=0, usage_complete=True,
                         error={"kind": "PROVIDER_AUTH", "message": "provider rejected credentials"})
            summary = await bridge.wait_settled("s", "t")
            assert events[-1]["subtype"] == subtype and summary.outcome == outcome
            assert events[-1]["runner"]["error_kind"] == "PROVIDER_AUTH"
            if outcome == "capped":
                assert events[-1]["runner"]["cap_reason"] == "run_cap"
                assert "over" not in events[-1]["errors"][0]
    run(exercise())


@pytest.mark.parametrize("handshake", [{"protocol_version": "2.0"},
    {"runner": {"kind": "wrong", "version": "1"}}, {"capabilities": {}}])
def test_initialize_rejects_incompatible_runner(handshake):
    async def exercise():
        with pytest.raises(p.ProtocolError):
            async with connection(handshake=handshake):
                pytest.fail("an incompatible runner must not open sessions")
    run(exercise())


@pytest.mark.parametrize("kind", ["settled", "request_end"])
def test_post_settlement_traffic_is_fatal_except_stale_delta(kind):
    async def exercise():
        async with connection() as (bridge, reader, writer, failures):
            _, events, beginning, prompt = await open_turn(bridge, reader, writer)
            await accept(writer, prompt)
            await beginning
            settled = dict(outcome="completed", final_text="done", requests=0, usage_complete=True)
            await update(writer, "settled", **settled)
            await bridge.wait_settled("s", "t")
            await update(writer, "text_delta", text="stale")
            tool = dict(session_id="s", trace_id="t", call_id="c", name="looper_tasks", arguments={})
            await send(writer, p.Request("r-1", "tools/call", tool))
            assert (await p.read_frame(reader)).result["ran"] is False
            fields = settled if kind == "settled" else dict(request_id="q", request_kind="turn", model="model",
                status="ok", input_uncached=1, output=0, cache_read=0, cache_write=0, reasoning=None, complete=True)
            await update(writer, kind, **fields)
            assert await reader.read() == b""
            await bridge.close()
            assert len(failures) == 1
            assert len([e for e in events if e["type"] == "result"]) == 1
    run(exercise())


def test_bounded_queue_overflow_preserves_observed_usage_and_loses_turn():
    async def exercise():
        async with connection(max_updates=1) as (bridge, reader, writer, failures):
            entered, release, events = asyncio.Event(), asyncio.Event(), []

            async def consume(event):
                events.append(event)
                if event["type"] == "stream_event":
                    entered.set()
                    await release.wait()

            _, _, beginning, prompt = await open_turn(bridge, reader, writer)
            bridge.sessions["s"].on_event = consume
            await accept(writer, prompt)
            await beginning
            await update(writer, "text_delta", text="first")
            await entered.wait()
            try:
                await update(writer, "request_end", request_id="q", request_kind="turn", model="model", status="ok",
                    input_uncached=7, output=0, cache_read=0, cache_write=0, reasoning=None, complete=True)
                await update(writer, "settled", outcome="completed", final_text="", requests=1, usage_complete=True)
                assert await reader.read() == b""
            finally:
                release.set()
            summary = await bridge.wait_settled("s", "t")
            assert summary.outcome == "lost" and summary.usage.input_tokens == 7
            assert "queue" in str(failures[0]) and not summary.usage_complete
    run(exercise())


def test_prompt_timeout_cannot_be_reopened_by_late_acceptance():
    async def exercise():
        async with connection(control_timeout=0.05) as (bridge, reader, writer, _):
            _, events, beginning, prompt = await open_turn(bridge, reader, writer)
            with pytest.raises(TimeoutError):
                await beginning
            try:
                await accept(writer, prompt)
            except (BrokenPipeError, ConnectionResetError):
                pass
            summary = await bridge.wait_settled("s", "t")
            entry = bridge.sessions["s"]
            assert entry.prompt_sent and not entry.session_started
            assert summary.outcome == "lost" and entry.state == "poisoned"
            assert len(events) == 1 and events[0]["type"] == "result"
            assert events[0]["subtype"] == "error_during_execution"
            assert events[0]["runner"]["outcome"] == "lost"
            assert not events[0]["usage_complete"]
    run(exercise())


def test_host_callback_exception_is_a_bounded_safe_rpc_error():
    class BrokenTurn(Turn):
        async def call_tool(self, params):
            raise ValueError("private-tool-input-and-credentials" * 100)

    async def exercise():
        async with connection() as (bridge, reader, writer, failures):
            _, events, beginning, prompt = await open_turn(bridge, reader, writer, BrokenTurn())
            await accept(writer, prompt)
            await beginning
            await send(writer, p.Request("r-1", "tools/call", dict(session_id="s", trace_id="t",
                call_id="c", name="looper_tasks", arguments={})))
            reply = await p.read_frame(reader)
            assert reply.error["code"] == p.ErrorCode.INTERNAL_ERROR
            assert "private" not in reply.error["message"] and len(reply.error["message"]) < 512
            await update(writer, "settled", outcome="completed", final_text="done", requests=0, usage_complete=True)
            assert (await bridge.wait_settled("s", "t")).outcome == "completed"
            assert events[-1]["subtype"] == "success" and not failures
    run(exercise())
