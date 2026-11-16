"""Hosted run contracts use real session RPCs, without starting a runner process."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from looper.agents.message import MessageEnvelope, MessageType, planner_address, worker_address
from looper.agents.tiers.worker import RunReply, WorkerConfig, WorkerPool, WorkerProcess, delivery_uuid
from looper.agents.runtimes import ApiProtocol
from looper.agents.runtimes.api.adapter import KEY_ENV
from looper.agents.runtimes.api.loop import ToolCall
from looper.agents.runtimes.channel import DeliveryResult, RunStartError
from looper.agents.runtimes.runner import protocol as p
from looper.agents.runtimes.runner.channel import NoTools, RunnerChannel
from looper.agents.runtimes.runner.session import Descriptor, RunnerSession
from looper.agents.runtimes.runner.supervisor import RunnerUnavailable
from runner_pipe_support import FakeSupervisor, connection, send


def run(coro):
    asyncio.run(asyncio.wait_for(coro, 10))


async def limits():
    return {"run_cap": None, "task_cap": None, "task_spent": 0}


def channel(tmp_path, supervisor):
    prompt = tmp_path / "prompt.md"
    prompt.write_text("Run instructions")
    descriptor = Descriptor("fake", "worker", "account", ApiProtocol.OPENAI_RESPONSES,
        "https://example.invalid", "model", None, 200000, str(prompt),
        builtin=("read", "bash", "edit", "write"), max_requests=7)
    session = RunnerSession("worker-run", supervisor, "fake", owned=False, state_dir=tmp_path)
    return RunnerChannel(session, descriptor, cwd=str(tmp_path), env={KEY_ENV: "secret-key"},
        tools=None, provider_id="account", model="model", price=lambda m, u: 0.01, limits=limits)


async def launch(handle, reader, writer, sid="s", *, refusal=None):
    task = asyncio.create_task(handle.launch())
    request = await p.read_frame(reader)
    assert request.method == "session/new"
    assert request.params["tools"]["builtin"] == ["read", "bash", "edit", "write"]
    await send(writer, p.Response(request.id, result={"session_id": sid}) if refusal is None else
               p.Response(request.id, error={"code": int(refusal), "message": "secret-key private failure"}))
    await task


async def begin(handle, reader, writer, *, accept=True):
    await handle.start("hello")
    request = await p.read_frame(reader)
    assert request.method == "session/prompt" and request.params["tools"] == []
    turn = {k: request.params[k] for k in ("session_id", "trace_id")}
    if accept:
        await send(writer, p.Response(request.id, result={**turn, "accepted": True}))
        await handle.wait_input_ready()
    return request, turn


async def update(writer, turn, kind, **fields):
    await send(writer, p.Notification("session/update", {**turn, "kind": kind, **fields}))


async def settle(writer, turn, outcome="completed", requests=0):
    await update(writer, turn, "settled", outcome=outcome, final_text="answer",
                 requests=requests, usage_complete=True)


async def events(handle):
    return [event async for event in handle.events()]


async def close(handle, reader, writer, *, method="wait"):
    task = asyncio.create_task(getattr(handle, method)())
    request = await p.read_frame(reader)
    assert request.method == "session/close" and "trace_id" not in request.params
    await send(writer, p.Response(request.id, result={}))
    return await task


async def no_frame(reader):
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(p.read_frame(reader), 0.02)


def test_run_streams_events_then_closes_on_wait(tmp_path):
    async def exercise():
        async with connection() as (bridge, reader, writer, failures):
            supervisor = FakeSupervisor(bridge, tmp_path)
            handle = channel(tmp_path, supervisor)
            assert handle.pid is None and handle.channel is handle
            assert Descriptor.parse(handle.command).role == "worker"
            assert "secret-key" not in str(handle.command)
            with pytest.raises(RuntimeError):
                await handle.start("too early")
            with pytest.raises(RuntimeError):
                await handle.wait_input_ready()
            await launch(handle, reader, writer)
            _, turn = await begin(handle, reader, writer)
            await update(writer, turn, "message", model="model", content=[{"type": "text", "text": "hello"}])
            await update(writer, turn, "tool_result", call_id="call", text="business error", is_error=True)
            await send(writer, p.Request("r-1", "budget/authorize", {**turn, "request_id": "q", "kind": "turn",
                "provider": "account", "model": "model", "estimated_input_tokens": 1, "max_output_tokens": 1}))
            assert (await p.read_frame(reader)).result["allow"]
            await update(writer, turn, "request_end", request_id="q", request_kind="turn", model="model",
                status="ok", input_uncached=1, output=2, cache_read=0, cache_write=0, reasoning=None, complete=True)
            await settle(writer, turn, requests=1)
            output = await events(handle)
            assert [e["type"] for e in output] == ["system", "assistant", "user", "usage", "result"]
            assert output[2]["message"]["content"][0]["is_error"]
            assert not output[-1]["is_error"] and output[-1]["num_turns"] == 1
            with pytest.raises(RuntimeError, match="the run has ended"):
                await handle.wait_input_ready()
            assert await close(handle, reader, writer) == 0
            assert not supervisor.leases[0].valid and not bridge.sessions and not failures
            assert await handle.error_output() == ""
    run(exercise())


@pytest.mark.parametrize("outcome", ["queued", "NOT_RUNNING", "TRACE_MISMATCH", "late", "UNSUPPORTED"])
def test_send_maps_steer_outcomes(tmp_path, outcome):
    async def exercise():
        async with connection() as (bridge, reader, writer, failures):
            handle = channel(tmp_path, FakeSupervisor(bridge, tmp_path))
            assert await handle.send("before start") is DeliveryResult.REJECTED
            await launch(handle, reader, writer)
            _, turn = await begin(handle, reader, writer)
            handle.send_timeout = 0.02
            sending = asyncio.create_task(handle.send("follow up", uuid="stable"))
            request = await p.read_frame(reader)
            assert request.method == "session/steer"
            assert request.params == {**turn, "message_id": "stable", "text": "follow up", "if_idle": "reject"}
            if outcome in {"queued", "late"}:
                if outcome == "late":
                    assert await sending is DeliveryResult.UNKNOWN
                    assert not bridge._error
                await send(writer, p.Response(request.id, result={**turn, "status": "queued"}))
                if outcome == "queued":
                    assert await sending is DeliveryResult.ACCEPTED
            else:
                await send(writer, p.Response(request.id, error={"code": int(p.ErrorCode[outcome]), "message": "refused", "data": turn}))
                if outcome == "UNSUPPORTED":
                    with pytest.raises(RuntimeError, match="cannot steer this run"):
                        await sending
                else:
                    assert await sending is DeliveryResult.REJECTED
            handle.end_input()
            assert await handle.send("closed") is DeliveryResult.REJECTED
            await no_frame(reader)
            await settle(writer, turn)
            await events(handle)
            assert await close(handle, reader, writer) == 0
            assert not failures
    run(exercise())


@pytest.mark.parametrize("late", [False, True])
@pytest.mark.parametrize("source", ["generated", "planner", "room"])
@pytest.mark.parametrize("reason", ["turn_ended", "session_closed"])
def test_dropped_steer_is_reported_once(tmp_path, late, source, reason):
    async def exercise():
        async with connection() as (bridge, reader, writer, _):
            handle = channel(tmp_path, FakeSupervisor(bridge, tmp_path))
            await launch(handle, reader, writer)
            _, turn = await begin(handle, reader, writer)
            handle.send_timeout = 0.02
            pool = WorkerPool(config=WorkerConfig(), runtime_root=tmp_path)
            pool._bus = SimpleNamespace(publish=AsyncMock())
            pool._inputs["w1"] = handle

            async def deliver(text, index):
                if source == "planner":
                    envelope = MessageEnvelope.create(
                        source=planner_address("task"), target=worker_address("w1"),
                        message_type=MessageType.WORKER_INPUT, payload={"text": text})
                    await pool._deliver_input(envelope)
                    ack = pool._bus.publish.await_args.args[0]
                    assert ack.message_type is MessageType.WORKER_INPUT_RESULT
                    assert ack.reply_to == envelope.message_id == message_ids[-1]
                    return DeliveryResult(ack.payload["delivered"])
                uuid = f"looper-reminder-012345abcdef-{index}" if source == "room" else None
                return await handle.send(text, uuid=uuid)

            # A steer queued during the last request's tool batch can be dropped
            # when the next model request hits the request limit.
            await update(writer, turn, "message", model="model", content=[{
                "type": "tool_use", "id": "tool", "name": "bash", "input": {"command": "true"}}])
            stream = handle.events()
            prefix = [await anext(stream), await anext(stream)]
            assert [event["type"] for event in prefix] == ["system", "assistant"]
            message_ids = []
            for index, text in enumerate(("applied", "dropped"), 1):
                sending = asyncio.create_task(deliver(text, index))
                request = await p.read_frame(reader)
                assert request.method == "session/steer" and request.params["text"] == text
                message_ids.append(request.params["message_id"])
                if late and index == 2:
                    assert await sending is DeliveryResult.UNKNOWN
                await send(writer, p.Response(request.id, result={**turn, "status": "queued"}))
                if not late or index == 1:
                    assert await sending is DeliveryResult.ACCEPTED
            await update(writer, turn, "steer_applied", message_id=message_ids[0], after_request_id="q",
                         after_tool_batch=True, new_turn=False)
            await update(writer, turn, "tool_result", call_id="tool", text="done", is_error=False)
            await update(writer, turn, "steer_dropped", message_id=message_ids[1], reason=reason)
            await settle(writer, turn, outcome="max_requests")
            output = prefix + [event async for event in stream]
            assert [e["subtype"] for e in output if e["type"] == "system"] == [
                "init", "steer_applied", "steer_dropped"]
            assert output[-1]["subtype"] == "error_max_turns"
            expected = [] if source == "room" else ["dropped"]
            assert handle.dropped_input() == expected
            handle.dropped_input().append("caller mutation")
            assert handle.dropped_input() == expected
            wp = WorkerProcess("w1", "step", "plan", handle=handle)
            await pool._finish_run(wp, None, RunReply(planner_address("task"), "reply", {}),
                                   AsyncMock(return_value={"status": "completed"}))
            result = pool._bus.publish.await_args.args[0].payload
            assert result.get("missed_input", []) == expected
            await close(handle, reader, writer)
    run(exercise())


@pytest.mark.parametrize("accepted", [True, False])
def test_end_cancels_then_closes(tmp_path, accepted):
    async def exercise():
        async with connection() as (bridge, reader, writer, failures):
            supervisor = FakeSupervisor(bridge, tmp_path)
            handle = channel(tmp_path, supervisor)
            await launch(handle, reader, writer)
            prompt, turn = await begin(handle, reader, writer, accept=accepted)
            ending = asyncio.create_task(handle.end())
            request = await p.read_frame(reader)
            assert request.method == "session/cancel" and request.params == turn
            await send(writer, p.Response(request.id, result=turn))
            await settle(writer, turn, outcome="cancelled")
            # The unaccepted prompt's late response must also be consumed.
            if not accepted:
                await send(writer, p.Response(prompt.id, result={**turn, "accepted": True}))
            request = await p.read_frame(reader)
            assert request.method == "session/close"
            await send(writer, p.Response(request.id, result={}))
            await ending
            assert len([e for e in await events(handle) if e["type"] == "result"]) == 1
            assert not supervisor.leases[0].valid and not bridge.sessions and not failures
            await handle.end()
            await no_frame(reader)
    run(exercise())


@pytest.mark.parametrize("failure", ["host_capacity", "runner_capacity", "unavailable"])
def test_launch_failures_are_run_start_errors(tmp_path, failure):
    async def exercise():
        async with connection() as (bridge, reader, writer, _):
            error = (p.ProtocolError(p.ErrorCode.CAPACITY) if failure == "host_capacity" else
                     RunnerUnavailable("restart backoff") if failure == "unavailable" else None)
            supervisor = FakeSupervisor(bridge, tmp_path, failure=error)
            handle = channel(tmp_path, supervisor)
            with pytest.raises(RunStartError, match="unavailable" if failure == "unavailable" else "CAPACITY") as caught:
                if failure == "runner_capacity":
                    await launch(handle, reader, writer, refusal=p.ErrorCode.CAPACITY)
                else:
                    await handle.launch()
            assert "secret-key" not in str(caught.value)
            assert not any(lease.valid for lease in supervisor.leases)
    run(exercise())


def test_lost_generation_ends_events(tmp_path):
    async def exercise():
        async with connection() as (bridge, reader, writer, failures):
            supervisor = FakeSupervisor(bridge, tmp_path)
            handle = channel(tmp_path, supervisor)
            await launch(handle, reader, writer)
            await begin(handle, reader, writer)
            writer.close()
            output = await events(handle)
            assert output[-1]["is_error"] and output[-1]["runner"]["outcome"] == "lost"
            assert "stdout EOF" in output[-1]["errors"][0]
            assert await handle.send("lost") is DeliveryResult.REJECTED
            assert await handle.wait() == 0
            assert not supervisor.leases[0].valid and len(failures) == 1
    run(exercise())


def test_steer_reaches_only_its_run(tmp_path):
    async def exercise():
        async with connection() as (bridge, reader, writer, failures):
            supervisor = FakeSupervisor(bridge, tmp_path)
            handles, turns = [], []
            for sid in ("first", "second"):
                handle = channel(tmp_path, supervisor)
                await launch(handle, reader, writer, sid)
                _, turn = await begin(handle, reader, writer)
                handles.append(handle)
                turns.append(turn)
            sending = asyncio.create_task(handles[0].send("first only"))
            request = await p.read_frame(reader)
            assert request.method == "session/steer"
            assert all(request.params[k] == v for k, v in turns[0].items())
            assert request.params["trace_id"] != turns[1]["trace_id"]
            await send(writer, p.Response(request.id, result={**turns[0], "status": "queued"}))
            assert await sending is DeliveryResult.ACCEPTED
            await no_frame(reader)
            for handle, turn in zip(handles, turns):
                await settle(writer, turn)
                await events(handle)
                await close(handle, reader, writer)
            assert not any(lease.valid for lease in supervisor.leases) and not failures
    run(exercise())


def test_no_tools_refuses_platform_tool_without_running_it():
    async def exercise():
        tools = NoTools()
        assert await tools.specs() == []
        outcome = await tools.call(ToolCall("call", "looper_missing", "{}"))
        assert outcome.text == "Looper has no platform tool looper_missing for this run"
        assert outcome.is_error and not outcome.ran
    run(exercise())


@pytest.mark.parametrize("failure", ["rejected", "exception"])
def test_run_failure_still_emits_one_terminal_event(tmp_path, failure, monkeypatch):
    async def exercise():
        async with connection() as (bridge, reader, writer, failures):
            handle = channel(tmp_path, FakeSupervisor(bridge, tmp_path))
            await launch(handle, reader, writer)
            if failure == "exception":
                async def fail(*args):
                    raise ValueError("private failure details")
                monkeypatch.setattr(handle.session, "run_turn", fail)
                await handle.start("hello")
            else:
                request, turn = await begin(handle, reader, writer, accept=False)
                await send(writer, p.Response(request.id, error={"code": int(p.ErrorCode.INVALID_PARAMS),
                    "message": "private rejection details", "data": turn}))
            output = await events(handle)
            assert [event["type"] for event in output] == (["result"] if failure == "exception" else ["system", "result"])
            assert output[-1]["is_error"]
            assert output[-1]["errors"] == (["the fake runner run failed (ValueError)"] if failure == "exception"
                                           else ["runner rejected the prompt"])
            with pytest.raises(RuntimeError, match="the run has ended"):
                await handle.wait_input_ready()
            assert await handle.send("ended") is DeliveryResult.REJECTED
            await close(handle, reader, writer)
            assert not failures
    run(exercise())


@pytest.mark.parametrize("boundary", ["before-send", "before-rpc", "before-write"])
def test_room_steer_after_last_tool_result_keeps_report(tmp_path, boundary, monkeypatch):
    async def exercise():
        async with connection() as (bridge, reader, writer, failures):
            handle = channel(tmp_path, FakeSupervisor(bridge, tmp_path))
            await launch(handle, reader, writer)
            _, turn = await begin(handle, reader, writer)
            stream = handle.events()
            assert (await anext(stream))["subtype"] == "init"
            await update(writer, turn, "message", model="model", content=[{
                "type": "tool_use", "id": "tool", "name": "bash", "input": {"command": "true"}}])
            assert (await anext(stream))["type"] == "assistant"
            if boundary == "before-write":
                entered = asyncio.Event()
                steer = handle.session.steer

                async def observed_steer(*args, **kwargs):
                    entered.set()
                    return await steer(*args, **kwargs)

                monkeypatch.setattr(handle.session, "steer", observed_steer)
                await bridge._write_lock.acquire()
                try:
                    sending = asyncio.create_task(handle.send("room delivery", uuid=delivery_uuid(42)))
                    await entered.wait()
                    await update(writer, turn, "tool_result", call_id="tool", text="done", is_error=False)
                    assert (await anext(stream))["type"] == "user"
                finally:
                    bridge._write_lock.release()
                assert await sending is DeliveryResult.REJECTED
            elif boundary == "before-rpc":
                # The send task starts while the tool runs; its child RPC task is
                # scheduled behind a final tool-result event already ready to deliver.
                sending = asyncio.create_task(handle.send("room delivery", uuid=delivery_uuid(42)))
                asyncio.get_running_loop().call_soon(handle._on_event, {
                    "type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "tool"}]}})
                assert await sending is DeliveryResult.REJECTED
            else:
                await update(writer, turn, "tool_result", call_id="tool", text="done", is_error=False)
            if boundary != "before-write":
                assert (await anext(stream))["type"] == "user"
            report = json.dumps({"status": "completed", "summary": "done", "changed_files": {}})
            await update(writer, turn, "text_delta", text=report)
            assert (await anext(stream))["type"] == "stream_event"
            if boundary == "before-send":
                assert await handle.send("room delivery", uuid=delivery_uuid(42)) is DeliveryResult.REJECTED
            await no_frame(reader)
            assert not bridge._error and not bridge._pending
            # Explicit Planner input retains its ordinary in-trace steering contract.
            sending = asyncio.create_task(handle.send("planner follow up"))
            request = await p.read_frame(reader)
            assert request.method == "session/steer" and request.params["text"] == "planner follow up"
            await send(writer, p.Response(request.id, result={**turn, "status": "queued"}))
            assert await sending is DeliveryResult.ACCEPTED
            await update(writer, turn, "settled", outcome="completed", final_text=report,
                         requests=0, usage_complete=True)
            output = [event async for event in stream]
            assert output[-1]["result"] == report
            assert handle.dropped_input() == []
            await close(handle, reader, writer)
            assert not failures
    run(exercise())
