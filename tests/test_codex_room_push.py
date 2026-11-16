"""Room deliveries follow steering and user-message receipt boundaries."""
from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from looper.agents.agent_settings import Launch
from looper.agents.room import MemberKind, RoomDelivery, RoomInput, Seat, room_section
from looper.agents.runtimes import ADAPTERS, Capability, RoomPush, room_push
from looper.agents.runtimes.channel import DeliveryResult
from looper.agents.runtimes.codex import CodexStream
from looper.agents.runtimes.dsh_runner import DshRunnerRuntime
from looper.agents.runtimes.omp import OmpStream
from looper.agents.runtimes.omp_steer import OmpChannel
from looper.agents.runtimes.pi import PiStream
from looper.agents.runtimes.pi_runner import PiRunnerRuntime
from looper.agents.runtimes.pi_steer import PiChannel
from looper.agents.tiers.worker import ConsumeStart, RunContext, WorkerConfig, WorkerPool, _Reminders, room_input
from tests.run_handle_support import ScriptedRun


def _launch(steer: bool, runtime_type: str = "codex") -> Launch:
    launch = Launch()
    runtime = replace(launch.profile.runtime, type=runtime_type,
                      capabilities=(ADAPTERS[runtime_type].capabilities - {Capability.STEER})
                      | ({Capability.STEER} if steer else set()))
    return replace(launch, settings=replace(launch.settings, profile=replace(launch.profile, runtime=runtime)))


@pytest.mark.parametrize(("runtime", "steer", "expected"), [
    ("codex", True, "steer"), ("codex", False, "none"), ("claude-code", False, "user_turn"),
    ("omp", True, "steer"), ("omp", False, "none"), ("pi", True, "steer"), ("pi", False, "none"),
    *((name, True, "none") for name in ("cursor-agent", "grok", "dsh")),
    pytest.param(PiRunnerRuntime(None, pi=None), False, "runner", id="pi-runner"),
    pytest.param(DshRunnerRuntime(None, dsh=None), False, "runner", id="dsh-runner"),
    pytest.param(SimpleNamespace(room_push=RoomPush.RUNNER, hosts_runs=False), True, "none", id="unhosted-runner"),
])
def test_only_opted_in_runtimes_deliver_room_messages(runtime, steer, expected):
    adapter = ADAPTERS[runtime] if isinstance(runtime, str) else runtime
    assert room_push(SimpleNamespace(runtime=adapter, steers=steer)).value == expected


def test_pi_runner_does_not_inherit_cli_text_receipts():
    runtime = PiRunnerRuntime(None, pi=None)
    assert room_push(SimpleNamespace(runtime=runtime, steers=True)) is RoomPush.RUNNER


@pytest.mark.parametrize("outcome", ["accepted", "rejected", "unknown", "ended"])
def test_steering_delivery_waits_for_history_after_acceptance(tmp_path, caplog, outcome):
    async def run():
        pool = WorkerPool(config=WorkerConfig(), runtime_root=tmp_path)
        confirm = AsyncMock()
        delivery = RoomDelivery("colleague's message", 42, (41, 42), True, 0)
        select = AsyncMock(return_value=delivery)
        send = AsyncMock(return_value=DeliveryResult[outcome.upper()] if outcome != "ended" else None)
        if outcome == "ended":
            send.side_effect = RuntimeError("no active turn")
        pool._inputs["w1"] = SimpleNamespace(send=send)
        pool._reminders["w1"] = state = _Reminders(next_delivery=select, confirm=confirm,
                                                  through=40, receipt=RoomPush.STEER)
        assert not await pool.remind("w1")
        select.assert_not_awaited()
        send.assert_not_awaited()
        state.tools_running = 1
        assert await pool._flush_reminder("w1") is (outcome == "accepted")
        select.assert_awaited_once_with(40)
        send.assert_awaited_once()
        assert send.await_args.args == (delivery.text,)
        assert send.await_args.kwargs["uuid"].endswith("-42")
        confirm.assert_not_awaited()
        assert state.confirmed == 0
        written = outcome in ("accepted", "unknown")
        assert state.through == (42 if written else 40)
        assert state.pending is not written
        assert bool(state.uuids) is written
        if outcome == "unknown":
            assert "room delivery acceptance unknown; not retrying" in caplog.text

    asyncio.run(asyncio.wait_for(run(), 5))


@pytest.mark.parametrize("runtime_type", ["omp", "pi"])
def test_rpc_room_ack_is_consumed_while_the_delivery_waits(tmp_path, agent_db, monkeypatch, caplog, runtime_type):
    from looper.agents.runtimes import omp_steer, pi_steer

    monkeypatch.setattr(omp_steer if runtime_type == "omp" else pi_steer, "_STEER_TIMEOUT_SEC", 0.05)

    async def run():
        stdout, requests, delivered = asyncio.StreamReader(), asyncio.Queue(), asyncio.Event()
        closed = False

        class Input:
            def write(self, line):
                requests.put_nowait(json.loads(line))

            async def drain(self):
                pass

            def is_closing(self):
                return closed

            def close(self):
                nonlocal closed
                closed = True
                stdout.feed_eof()

        channel = {"omp": OmpChannel, "pi": PiChannel}[runtime_type](
            SimpleNamespace(stdout=stdout, stdin=Input()), model="test-model", cwd=str(tmp_path))
        launch = _launch(True, runtime_type)
        pool = WorkerPool(config=WorkerConfig(), runtime_root=tmp_path)
        pool._active["w1"] = SimpleNamespace(parent_task_id="task")
        pool._activity = AsyncMock(side_effect=lambda *a, **kw: delivered.set())
        text = "[Looper room]\n- #42 from worker@s2 (info): bluebird"
        room = SimpleNamespace(delivery=AsyncMock(return_value=RoomDelivery(text, 42, (42,), True, 0)),
                               confirm=AsyncMock())
        pool.room_ops = SimpleNamespace(room=room)

        def emit(raw):
            stdout.feed_data((json.dumps(raw) + "\n").encode())

        async def peer():
            while True:
                request = await requests.get()
                command = request["type"]
                emit({"type": "response", "id": request["id"], "command": command, "success": True,
                      "data": {"sessionId": "session", "disposition": "started" if command == "prompt" else "queued"}})
                if command == "prompt":
                    emit({"type": "message_end", "message": {"role": "assistant", "stopReason": "toolUse", "content": [
                        {"type": "toolCall", "id": "cmd", "name": "bash", "arguments": {"command": "sleep 25"}}]}})
                if command == "steer":
                    assert request["message"] == text
                    # Only the real channel's events iterator can consume the ACK above.
                    await asyncio.wait_for(delivered.wait(), 1)
                    emit({"type": "message_end", "message": {"role": "toolResult", "toolCallId": "cmd",
                                                               "content": "done"}})
                    emit({"type": "message_end", "message": {"role": "user", "content": text}})
                    emit({"type": "message_end", "message": {"role": "assistant", "stopReason": "stop", "content": [
                        {"type": "text", "text": json.dumps({"status": "completed", "summary": "done"})}]}})
                    emit({"type": "agent_end", "isTerminal": True} if runtime_type == "omp" else
                         {"type": "agent_settled", "aborted": False})
                    return

        class Run(ScriptedRun):
            @property
            def channel(self):
                return channel

        seat = Seat("worker@s1", "worker", MemberKind.WORK, run="w1")
        ctx = RunContext("w1", "task", "s1", str(tmp_path), "do the step", False, launch, launch.runtime,
                         seat, 2, datetime.now(timezone.utc), False, [])
        recorder = Mock(cap_stop=None, record=AsyncMock(), finish=AsyncMock())
        server = asyncio.create_task(peer())
        try:
            result = await pool._consume_once(ctx, Run([]), recorder, start=ConsumeStart.NEW)
            await server
            assert result["status"] == "completed", result
            room.confirm.assert_awaited_once_with("task", seat.key, 42)
            assert "acceptance unknown" not in caplog.text
        finally:
            pool._drop_reminders("w1")
            channel.end_input()
            server.cancel()
            await asyncio.gather(server, return_exceptions=True)

    asyncio.run(asyncio.wait_for(run(), 4))


@pytest.mark.parametrize("combined", [False, True])
def test_history_receipts_match_ids_and_never_skip_unseen_deliveries(tmp_path, monkeypatch, combined):
    from looper.agents.tiers import worker

    monkeypatch.setattr(worker, "ADDRESSED_INTERVAL_SEC", 0)

    async def run():
        pool = WorkerPool(config=WorkerConfig(), runtime_root=tmp_path)
        confirm = AsyncMock()
        texts = ["[Looper room]\n- #41 from worker@s2 (info): one\n- #42 from worker@s2 (info): two",
                 "[Looper room]\n- #43 from worker@s2 (info): three"]
        deliveries = [RoomDelivery(texts[0], 42, (41, 42), True, 0),
                      RoomDelivery(texts[1], 43, (43,), True, 0)]
        pool._inputs["w1"] = SimpleNamespace(send=AsyncMock(return_value=DeliveryResult.ACCEPTED))
        pool._reminders["w1"] = state = _Reminders(next_delivery=AsyncMock(side_effect=deliveries),
                                                  confirm=confirm, receipt=RoomPush.STEER, tools_running=1)
        try:
            assert await pool.remind("w1")
            assert await pool.remind("w1")
            # A partial delivery, an unrelated user turn and a bare id mention prove nothing.
            for text in ("[Looper room]\n- #41 from worker@s2 (info): one", "initial prompt", "#42 #43"):
                await pool._room_history_item("w1", state, text)
            confirm.assert_not_awaited()
            if not combined:
                await pool._room_history_item("w1", state, texts[1])
                confirm.assert_not_awaited()
            await pool._room_history_item("w1", state, "other input\n" + "\n".join(texts))
            assert [call.args[0] for call in confirm.await_args_list] == [42, 43]
            await pool._room_history_item("w1", state, texts[0])
            assert confirm.await_count == 2
            assert not state.steered
        finally:
            pool._drop_reminders("w1")

    asyncio.run(asyncio.wait_for(run(), 5))


@pytest.mark.parametrize("steer", [True, False])
@pytest.mark.parametrize("review", [True, False])
def test_codex_stream_delivers_only_during_tools_and_matches_guidance(tmp_path, agent_db, steer, review):
    async def run():
        launch = _launch(steer)
        pool = WorkerPool(config=WorkerConfig(), runtime_root=tmp_path)
        seat = Seat("reviewer@s1" if review else "worker@s1", "reviewer" if review else "worker",
                    MemberKind.REVIEW if review else MemberKind.WORK, run="w1")
        delivery = RoomDelivery("the code word is bluebird", 42, (42,), True, 0)
        room = SimpleNamespace(delivery=AsyncMock(return_value=delivery), confirm=AsyncMock())
        pool.room_ops = SimpleNamespace(room=room)
        mode = room_input(launch, is_verify=review)
        assert mode is (RoomInput.PUSHED if steer else RoomInput.PULL)
        assert ("Do not poll your inbox" in room_section(seat, mode)) is steer
        captured = []
        sent = asyncio.Event()

        class Run(ScriptedRun):
            async def send(self, text, **kwargs):
                result = await super().send(text, **kwargs)
                sent.set()
                return result

            async def events(self):
                state = pool._reminders.get("w1")
                assert (state is not None) is steer
                assert not await pool.remind("w1")
                assert not self.inputs
                room.delivery.assert_not_awaited()
                stream = CodexStream()
                item = {"id": "cmd", "type": "command_execution", "command": "sleep 30"}
                for event in stream.translate({"type": "item.started", "item": item}):
                    yield event
                if steer:
                    await asyncio.wait_for(sent.wait(), 1)
                    assert state.tools_running == 1 and not state.waiting
                    assert self.inputs == [delivery.text]
                    room.confirm.assert_not_awaited()
                for event in stream.translate({"type": "item.completed", "item": {**item, "exit_code": 0}}):
                    yield event
                if steer:
                    assert state.tools_running == 0 and state.receipt is RoomPush.STEER
                    captured.append(state)
                    assert not await pool.remind("w1")
                    assert self.inputs == [delivery.text]
                else:
                    assert not self.inputs
                    room.confirm.assert_not_awaited()
                report = {"passed": True, "summary": "done", "findings": []} if review else {
                    "status": "completed", "summary": "done", "changed_files": {}}
                yield {"type": "result", "subtype": "success", "result": json.dumps(report)}

        handle = Run([])
        ctx = RunContext("w1", "task", "s1", str(tmp_path), "do the step", review, launch, launch.runtime,
                         seat, 5, datetime.now(timezone.utc), False, [])
        recorder = Mock(cap_stop=None, record=AsyncMock(), finish=AsyncMock())
        result = await pool._consume_once(ctx, handle, recorder, start=ConsumeStart.NEW)
        assert (result["passed"] if review else result["status"] == "completed")
        assert bool(captured) is steer

    asyncio.run(asyncio.wait_for(run(), 5))


@pytest.mark.parametrize(("runtime_type", "outcome"), [
    ("omp", "receipt"), ("pi", "receipt"), ("codex", "receipt"),
    ("omp", "fallback"), ("pi", "ended"), ("omp", "unknown"),
    ("omp", "late_ack"), ("omp", "late_unknown"), ("pi", "ended_armed"),
])
def test_rpc_room_receipts_and_fallback_follow_tool_batch(
    tmp_path, agent_db, monkeypatch, caplog, runtime_type, outcome,
):
    from looper.agents.tiers import worker

    monkeypatch.setattr(worker, "ROOM_ITEM_TIMEOUT_SEC", 0.05)

    async def run():
        launch = _launch(True, runtime_type)
        pool = WorkerPool(config=WorkerConfig(), runtime_root=tmp_path)
        text = "[Looper room]\n- #42 from worker@s2 (info): 你好 /skill"
        delivery = RoomDelivery(text, 42, (42,), True, 0)
        confirmed = asyncio.Event()
        send_started, acknowledge = asyncio.Event(), asyncio.Event()
        late = outcome in ("late_ack", "late_unknown")
        unknown = outcome in ("unknown", "late_unknown")
        room = SimpleNamespace(delivery=AsyncMock(return_value=delivery),
                               confirm=AsyncMock(side_effect=lambda *args: confirmed.set()))
        pool.room_ops = SimpleNamespace(room=room)
        seat = Seat("worker@s1", "worker", MemberKind.WORK, run="w1")
        assert room_input(launch, is_verify=False) is RoomInput.PUSHED
        stream = {"omp": OmpStream, "pi": PiStream, "codex": CodexStream}[runtime_type]()

        def tool_event(call_id, *, done=False):
            if runtime_type == "codex":
                return {"type": "item.completed" if done else "item.started", "item": {
                    "id": call_id, "type": "command_execution", "command": "sleep 25", "exit_code": 0}}
            message = ({"role": "toolResult", "toolCallId": call_id, "content": "done"} if done else {
                "role": "assistant", "content": [{"type": "toolCall", "id": call_id,
                                                   "name": "bash", "arguments": {"command": "sleep 25"}}]})
            return {"type": "message_end", "message": message}

        def receipt(value):
            if runtime_type == "codex":
                return [{"type": "system", "subtype": "user_message_receipt", "text": value}]
            return stream.translate({"type": "message_end", "message": {"role": "user", "content": value}})

        class Run(ScriptedRun):
            async def send(self, value, **kwargs):
                await super().send(value, **kwargs)
                send_started.set()
                if late:
                    await acknowledge.wait()
                return DeliveryResult.UNKNOWN if unknown else DeliveryResult.ACCEPTED

            async def events(self):
                state = pool._reminders["w1"]
                assert not await pool.remind("w1")
                if late:
                    state.pending = False  # A new post below arrives after the tool batch starts.
                for event in receipt("initial prompt"):
                    yield event
                for call_id in ("cmd-1", "cmd-2"):
                    for event in stream.translate(tool_event(call_id)):
                        yield event
                if late:
                    sending = asyncio.create_task(pool.remind("w1"))
                await asyncio.wait_for(send_started.wait(), 1)
                assert state.tools_running == 2 and self.inputs == [text]
                await asyncio.sleep(0.08)
                room.confirm.assert_not_awaited()
                assert state.steered[42].fallback is None
                if runtime_type == "codex":
                    # Codex shows the history item immediately, even while the tools still run.
                    for event in receipt(text):
                        yield event
                    room.confirm.assert_awaited_once_with("task", seat.key, 42)
                for event in stream.translate(tool_event("cmd-1", done=True)):
                    yield event
                assert state.tools_running == 1
                if runtime_type != "codex":
                    await asyncio.sleep(0.08)
                    room.confirm.assert_not_awaited()
                    assert state.steered[42].fallback is None
                if outcome != "ended":
                    for event in stream.translate(tool_event("cmd-2", done=True)):
                        yield event
                    assert state.tools_running == 0
                    if late:
                        assert state.steered[42].fallback is None
                        acknowledge.set()
                        assert await sending is (not unknown)
                    if outcome in ("fallback", "late_ack"):
                        timer = state.steered[42].fallback
                        # Later tool batches cannot reset this delivery's receipt window.
                        for done in (False, True):
                            for event in stream.translate(tool_event("cmd-3", done=done)):
                                yield event
                        assert state.steered[42].fallback is timer
                        await asyncio.wait_for(confirmed.wait(), 1)
                    elif runtime_type != "codex" and outcome == "receipt":
                        for value in ("Planner steer", "[Looper room]\n- #41 from worker@s2 (info): other"):
                            for event in receipt(value):
                                yield event
                        room.confirm.assert_not_awaited()
                        for event in receipt(text):
                            yield event
                    elif unknown:
                        await asyncio.sleep(0.08)
                        room.confirm.assert_not_awaited()
                yield {"type": "result", "subtype": "success", "result": json.dumps({
                    "status": "completed", "summary": "done", "changed_files": {}})}

        ctx = RunContext("w1", "task", "s1", str(tmp_path), "do the step", False, launch, launch.runtime,
                         seat, 5, datetime.now(timezone.utc), False, [])
        recorder = Mock(cap_stop=None, record=AsyncMock(), finish=AsyncMock())
        try:
            result = await pool._consume_once(ctx, Run([]), recorder, start=ConsumeStart.NEW)
            assert result["status"] == "completed"
            if outcome in ("receipt", "fallback", "late_ack"):
                room.confirm.assert_awaited_once_with("task", seat.key, 42)
            else:
                await asyncio.sleep(0.08)
                room.confirm.assert_not_awaited()
            assert ("room delivery history item timed out" in caplog.text) is (outcome in ("fallback", "late_ack"))
        finally:
            pool._drop_reminders("w1")

    asyncio.run(asyncio.wait_for(run(), 5))
