"""Grok room acknowledgements require an observed model boundary, with no timer fallback."""
from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from looper.agents.agent_settings import Launch
from looper.agents.room import MemberKind, RoomDelivery, RoomInput, RoomService, Seat, room_section
from looper.agents.runtimes import ADAPTERS, Capability, RoomPush
from looper.agents.runtimes.channel import DeliveryResult
from looper.agents.tiers.worker import ConsumeStart, RunContext, WorkerConfig, WorkerPool, WorkerProcess, room_input
from tests.run_handle_support import ScriptedRun


@pytest.mark.parametrize("outcome", ["read", "unseen", "rejected"])
def test_grok_room_read_follows_receipts_without_timeout(tmp_path, agent_db, monkeypatch, outcome):
    from looper.agents.tiers import worker

    monkeypatch.setattr(worker, "ADDRESSED_INTERVAL_SEC", 0)
    monkeypatch.setattr(worker, "ROOM_ITEM_TIMEOUT_SEC", 0.01)

    async def run():
        launch = Launch()
        runtime = replace(launch.profile.runtime, type="grok", capabilities=ADAPTERS["grok"].capabilities | {Capability.STEER})
        launch = replace(launch, settings=replace(launch.settings, profile=replace(launch.profile, runtime=runtime)))
        pool = WorkerPool(config=WorkerConfig(), runtime_root=tmp_path)
        seat = Seat("worker@s1", "worker", MemberKind.WORK, run="w1")
        mode = room_input(launch, is_verify=False)
        assert mode is RoomInput.PUSHED
        assert "Do not poll your inbox" in room_section(seat, mode)
        deliveries = [RoomDelivery("first", 42, (42,), True, 0), RoomDelivery("second", 43, (43,), True, 0)]
        room = SimpleNamespace(delivery=AsyncMock(side_effect=deliveries), confirm=AsyncMock())
        pool.room_ops = SimpleNamespace(room=room)
        sent = []

        class Run(ScriptedRun):
            async def send(self, text, *, uuid=None):
                sent.append(uuid)
                return DeliveryResult.REJECTED if outcome == "rejected" else DeliveryResult.ACCEPTED

            async def events(self):
                yield {"type": "assistant", "message": {"content": [
                    {"type": "tool_use", "name": "read_file", "input": {}, "id": "tool"}]}}
                state = pool._reminders["w1"]
                assert state.receipt is RoomPush.STEER and not state.receipt_fallback
                # A steering run's send is scheduled off the events iterator.
                for _ in range(100):
                    if sent:
                        break
                    await asyncio.sleep(0.01)
                assert sent
                room.confirm.assert_not_awaited()
                if outcome == "read":
                    assert await pool.remind("w1")
                    assert len(sent) == 2
                    # A later confirmed delivery cannot advance over an unseen earlier one.
                    yield {"type": "user", "isReplay": True, "uuid": sent[1]}
                    room.confirm.assert_not_awaited()
                    yield {"type": "user", "isReplay": True, "uuid": sent[0]}
                    assert [c.args[2] for c in room.confirm.await_args_list] == [42, 43]
                    yield {"type": "user", "isReplay": True, "uuid": sent[0]}
                    assert room.confirm.await_count == 2
                else:
                    await asyncio.sleep(0.03)
                    room.confirm.assert_not_awaited()
                    assert all(entry.fallback is None for entry in state.steered.values())
                    assert state.through == (0 if outcome == "rejected" else 42)
                yield {"type": "result", "subtype": "success", "result": json.dumps(
                    {"status": "completed", "summary": "done", "changed_files": {}})}

        ctx = RunContext("w1", "task", "s1", str(tmp_path), "do the step", False, launch, launch.runtime,
                         seat, 5, datetime.now(timezone.utc), False, [])
        recorder = Mock(cap_stop=None, record=AsyncMock(), finish=AsyncMock())
        try:
            result = await pool._consume_once(ctx, Run([]), recorder, start=ConsumeStart.NEW)
            assert result["status"] == "completed"
            if outcome != "read":
                room.confirm.assert_not_awaited()
        finally:
            pool._drop_reminders("w1")

    asyncio.run(asyncio.wait_for(run(), 5))


def test_grok_room_push_can_accept_while_worker_consumes_a_tool_event(tmp_path, agent_db, monkeypatch):
    import os
    import sys

    from fake_claude import fake_claude
    from fake_grok_acp import FAKE_ACP
    from looper.agents import room as room_module
    from looper.agents.runtimes import grok_steer
    from looper.agents.runtimes.grok import GrokRuntime
    from looper.agents.runtimes.grok_steer import GrokChannel

    executable = fake_claude(tmp_path / "grok", "import os\n"
                            f"os.environ['FAKE_ACP_ROOT'] = {str(tmp_path)!r}\n" + FAKE_ACP)
    probe = GrokRuntime().probe(executable)
    assert probe.error is None and Capability.STEER in probe.capabilities
    # A synchronous room send from the event consumer must not block the ACP reader.
    monkeypatch.setattr(grok_steer, "_SEND_TIMEOUT_SEC", 0.25)

    async def run():
        home = tmp_path / "home"
        home.mkdir()
        report = json.dumps({"status": "completed", "summary": "done", "changed_files": {}})
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-u", "-c", FAKE_ACP, "agent", "--always-approve", "stdio",
            cwd=tmp_path, env={**os.environ, "HOME": str(home), "GROK_HOME": str(home),
                               "FAKE_ACP_ROOT": str(tmp_path), "FAKE_ACP_STEER_RESULT": report},
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        channel = GrokChannel(process, model="grok-4.7", cwd=str(tmp_path))
        launch = Launch()
        runtime = replace(launch.profile.runtime, type="grok", capabilities=probe.capabilities)
        launch = replace(launch, settings=replace(launch.settings, profile=replace(launch.profile, runtime=runtime)))
        pool = WorkerPool(config=WorkerConfig(), runtime_root=tmp_path)
        seat = Seat("worker@s1", "worker", MemberKind.WORK, run="w1")
        room = RoomService(worker_pool=pool)
        monkeypatch.setattr(room, "enabled", AsyncMock(return_value=True))
        cursor = 0

        async def mark_read(task_id, seat_key, through):
            nonlocal cursor
            assert (task_id, seat_key) == ("task", seat.key)
            cursor = max(cursor, through)

        mark = AsyncMock(side_effect=mark_read)
        monkeypatch.setattr(room_module.db_room, "mark_read", mark)
        monkeypatch.setattr(room_module.db_room, "last_read", AsyncMock(side_effect=lambda *_: cursor))
        row = {"id": 42, "sender_seat": "worker@s2", "kind": "question", "body": "new instructions",
               "audience": [seat.key], "audience_all": False}
        monkeypatch.setattr(room_module.db_room, "messages", AsyncMock(
            side_effect=lambda _, *, after=0: [row] if after < row["id"] else []))
        pool.room_ops = SimpleNamespace(room=room)
        pool._active["w1"] = WorkerProcess("w1", "s1", "p1", parent_task_id="task")
        activity = AsyncMock()
        monkeypatch.setattr(pool, "_activity", activity)
        accepted, tool_result = asyncio.Event(), asyncio.Event()
        original_send = channel.send
        outcomes = []

        async def send(*args, **kwargs):
            outcome = await original_send(*args, **kwargs)
            outcomes.append(outcome)
            accepted.set()
            return outcome

        monkeypatch.setattr(channel, "send", send)

        class Run(ScriptedRun):
            @property
            def channel(self):
                return channel

            async def wait(self):
                return await process.wait()

        async def record(event):
            content = (event.get("message") or {}).get("content")
            if isinstance(content, list) and any(b.get("type") == "tool_result" for b in content):
                tool_result.set()

        recorder = Mock(cap_stop=None, record=AsyncMock(side_effect=record), finish=AsyncMock())
        ctx = RunContext("w1", "task", "s1", str(tmp_path), "STEER_TOOL", False, launch, launch.runtime,
                         seat, 5, datetime.now(timezone.utc), False, [])
        consuming = asyncio.create_task(pool._consume_once(ctx, Run([]), recorder, start=ConsumeStart.NEW))
        try:
            await asyncio.wait_for(accepted.wait(), 3)
            assert outcomes == [DeliveryResult.ACCEPTED]
            assert pool._reminders["w1"].sent == 1
            mark.assert_not_awaited()
            assert cursor == 0
            assert not (tmp_path / "steer-tool.done").exists()
            (tmp_path / "steer-tool.finish").touch()
            await asyncio.wait_for(tool_result.wait(), 2)
            mark.assert_not_awaited()
            assert cursor == 0
            (tmp_path / "steer-model.start").touch()
            result = await asyncio.wait_for(consuming, 3)
            assert result["status"] == "completed"
            mark.assert_awaited_once_with("task", seat.key, 42)
            assert cursor == 42
            assert await room.delivery("task", seat) is None
            requests = [json.loads(line) for line in (tmp_path / "steer-requests.jsonl").read_text().splitlines()]
            activity.assert_any_await("task", "w1", "phase", "Room messages delivered: #42",
                                      metadata={"room_delivery": {"ids": [42], "through": 42,
                                                                 "chars": len(requests[0]["params"]["text"])}})
            assert [r["method"] for r in requests] == ["_x.ai/interject"]
            assert process.returncode == 0
        finally:
            pool._drop_reminders("w1")
            channel.end_input()
            consuming.cancel()
            await asyncio.gather(consuming, return_exceptions=True)
            if process.returncode is None:
                process.kill()
            await process.wait()

    asyncio.run(asyncio.wait_for(run(), 8))


@pytest.mark.parametrize("final_started", [False, True])
def test_grok_room_delivery_after_last_tool_keeps_report_and_unread_cursor(
    tmp_path, agent_db, monkeypatch, final_started,
):
    from looper.agents import room as room_module
    from looper.agents.runtimes.grok_acp import PROTOCOL_VERSION
    from looper.agents.runtimes.grok_steer import GrokChannel

    async def run():
        stdout = asyncio.StreamReader()
        requests = []
        prompt_sent, delivery_requested, delivery_release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        result_recorded, record_release, send_attempted = asyncio.Event(), asyncio.Event(), asyncio.Event()

        def emit(message):
            stdout.feed_data((json.dumps({"jsonrpc": "2.0", **message}) + "\n").encode())

        def update(kind, **fields):
            emit({"method": "session/update", "params": {"sessionId": "session", "update": {
                "sessionUpdate": kind, **fields}}})

        class Writer:
            closed = False

            def write(self, data):
                request = json.loads(data)
                requests.append(request)
                if request["method"] == "initialize":
                    emit({"id": request["id"], "result": {"protocolVersion": PROTOCOL_VERSION}})
                elif request["method"] == "session/new":
                    emit({"id": request["id"], "result": {"sessionId": "session"}})
                elif request["method"] == "session/prompt":
                    prompt_sent.set()

            async def drain(self):
                pass

            def is_closing(self):
                return self.closed

            def close(self):
                self.closed = True
                stdout.feed_eof()

        channel = GrokChannel(SimpleNamespace(stdin=Writer(), stdout=stdout), model="grok", cwd=str(tmp_path))
        launch = Launch()
        runtime = replace(launch.profile.runtime, type="grok", capabilities=ADAPTERS["grok"].capabilities | {Capability.STEER})
        launch = replace(launch, settings=replace(launch.settings, profile=replace(launch.profile, runtime=runtime)))
        pool = WorkerPool(config=WorkerConfig(), runtime_root=tmp_path)
        seat = Seat("worker@s1", "worker", MemberKind.WORK, run="w1")
        room = RoomService(worker_pool=pool)
        monkeypatch.setattr(room, "enabled", AsyncMock(return_value=True))
        mark = AsyncMock()
        monkeypatch.setattr(room_module.db_room, "mark_read", mark)
        monkeypatch.setattr(room_module.db_room, "last_read", AsyncMock(return_value=0))
        row = {"id": 42, "sender_seat": "worker@s2", "kind": "question", "body": "new instructions",
               "audience": [seat.key], "audience_all": False}
        monkeypatch.setattr(room_module.db_room, "messages", AsyncMock(return_value=[row]))

        async def delivery(after):
            delivery_requested.set()
            await delivery_release.wait()
            return await room.delivery("task", seat, after=after)

        pool.room_ops = SimpleNamespace(room=SimpleNamespace(delivery=lambda *_, after: delivery(after),
                                                           confirm=room.confirm))
        original_send = channel.send

        async def send(*args, **kwargs):
            try:
                return await original_send(*args, **kwargs)
            finally:
                send_attempted.set()

        monkeypatch.setattr(channel, "send", send)

        class Run(ScriptedRun):
            @property
            def channel(self):
                return channel

        async def record(event):
            content = (event.get("message") or {}).get("content")
            if isinstance(content, list) and any(b.get("type") == "tool_result" for b in content):
                result_recorded.set()
                await record_release.wait()

        recorder = Mock(cap_stop=None, record=AsyncMock(side_effect=record), finish=AsyncMock())
        ctx = RunContext("w1", "task", "s1", str(tmp_path), "do the step", False, launch, launch.runtime,
                         seat, 5, datetime.now(timezone.utc), False, [])
        consuming = asyncio.create_task(pool._consume_once(ctx, Run([]), recorder, start=ConsumeStart.NEW))
        report = json.dumps({"status": "completed", "summary": "done", "changed_files": {}})
        try:
            await prompt_sent.wait()
            update("tool_call", toolCallId="last-tool", title="read_file", rawInput={})
            await delivery_requested.wait()
            update("tool_call_update", toolCallId="last-tool", status="completed", rawOutput={})
            if final_started:
                update("agent_message_chunk", content={"type": "text", "text": report})
            await result_recorded.wait()
            # The Worker is still recording the result, but the ACP reader already
            # knows no tool remains. A delayed delivery must not write an interjection.
            delivery_release.set()
            await send_attempted.wait()
            assert all(request["method"] != "_x.ai/interject" for request in requests)
            mark.assert_not_awaited()
            record_release.set()
            if not final_started:
                update("agent_message_chunk", content={"type": "text", "text": report})
            emit({"id": next(r["id"] for r in requests if r["method"] == "session/prompt"),
                  "result": {"stopReason": "end_turn"}})
            result = await consuming
            assert result["status"] == "completed" and result["summary"] == "done"
            mark.assert_not_awaited()
            unread = await room.delivery("task", seat)
            assert unread is not None and unread.ids == (42,)
        finally:
            record_release.set()
            delivery_release.set()
            pool._drop_reminders("w1")
            channel.end_input()
            consuming.cancel()
            await asyncio.gather(consuming, return_exceptions=True)

    asyncio.run(asyncio.wait_for(run(), 5))
