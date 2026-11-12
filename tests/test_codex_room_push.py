"""Room deliveries follow Codex's probed steering and acceptance boundary."""
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
from looper.agents.runtimes import ADAPTERS, Capability, room_push
from looper.agents.runtimes.channel import DeliveryResult
from looper.agents.runtimes.codex import CodexStream
from looper.agents.tiers.worker import ConsumeStart, RunContext, WorkerConfig, WorkerPool, _Reminders, room_input
from tests.run_handle_support import ScriptedRun


def _launch(steer: bool) -> Launch:
    launch = Launch()
    runtime = replace(launch.profile.runtime, type="codex",
                      capabilities=ADAPTERS["codex"].capabilities | ({Capability.STEER} if steer else set()))
    return replace(launch, settings=replace(launch.settings, profile=replace(launch.profile, runtime=runtime)))


@pytest.mark.parametrize(("runtime", "steer", "expected"), [
    ("codex", True, "steer"), ("codex", False, "none"), ("claude-code", False, "user_turn"),
    *((name, True, "none") for name in ("cursor-agent", "grok", "omp", "pi", "dsh")),
])
def test_only_opted_in_runtimes_deliver_room_messages(runtime, steer, expected):
    assert room_push(SimpleNamespace(runtime=ADAPTERS[runtime], steers=steer)).value == expected


@pytest.mark.parametrize("outcome", ["accepted", "rejected", "unknown", "ended"])
def test_steering_delivery_confirms_only_on_acceptance(tmp_path, caplog, outcome):
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
                                                  through=40, confirm_on_accept=True)
        assert not await pool.remind("w1")
        select.assert_not_awaited()
        send.assert_not_awaited()
        state.tools_running = 1
        assert await pool._flush_reminder("w1") is (outcome == "accepted")
        select.assert_awaited_once_with(40)
        send.assert_awaited_once()
        assert send.await_args.args == (delivery.text,)
        assert send.await_args.kwargs["uuid"].endswith("-42")
        if outcome == "accepted":
            confirm.assert_awaited_once_with(42)
            assert state.confirmed == 42
        else:
            confirm.assert_not_awaited()
            assert state.confirmed == 0
        written = outcome in ("accepted", "unknown")
        assert state.through == (42 if written else 40)
        assert state.pending is not written
        assert bool(state.uuids) is written
        if outcome == "unknown":
            assert "room delivery acceptance unknown; not retrying" in caplog.text

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

        class Run(ScriptedRun):
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
                    assert state.tools_running == 1 and not state.waiting
                    assert self.inputs == [delivery.text]
                    room.confirm.assert_awaited_once_with("task", seat.key, 42)
                for event in stream.translate({"type": "item.completed", "item": {**item, "exit_code": 0}}):
                    yield event
                if steer:
                    assert state.tools_running == 0 and state.confirm_on_accept
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
