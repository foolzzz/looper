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
from looper.agents.room import MemberKind, RoomDelivery, RoomInput, Seat, room_section
from looper.agents.runtimes import ADAPTERS, Capability
from looper.agents.runtimes.channel import DeliveryResult
from looper.agents.tiers.worker import ConsumeStart, RunContext, WorkerConfig, WorkerPool, room_input
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
                assert state.confirm_on_item and not state.receipt_fallback
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

    from fake_grok_acp import FAKE_ACP
    from looper.agents.runtimes import grok_steer
    from looper.agents.runtimes.grok_steer import GrokChannel

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
        runtime = replace(launch.profile.runtime, type="grok", capabilities=ADAPTERS["grok"].capabilities | {Capability.STEER})
        launch = replace(launch, settings=replace(launch.settings, profile=replace(launch.profile, runtime=runtime)))
        pool = WorkerPool(config=WorkerConfig(), runtime_root=tmp_path)
        seat = Seat("worker@s1", "worker", MemberKind.WORK, run="w1")
        room = SimpleNamespace(delivery=AsyncMock(return_value=RoomDelivery("new instructions", 42, (42,), True, 0)),
                               confirm=AsyncMock())
        pool.room_ops = SimpleNamespace(room=room)
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
            room.confirm.assert_not_awaited()
            assert not (tmp_path / "steer-tool.done").exists()
            (tmp_path / "steer-tool.finish").touch()
            await asyncio.wait_for(tool_result.wait(), 2)
            room.confirm.assert_not_awaited()
            (tmp_path / "steer-model.start").touch()
            result = await asyncio.wait_for(consuming, 3)
            assert result["status"] == "completed"
            room.confirm.assert_awaited_once_with("task", seat.key, 42)
            requests = [json.loads(line) for line in (tmp_path / "steer-requests.jsonl").read_text().splitlines()]
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
