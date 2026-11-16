"""Hosted room deliveries require the runner's matching model receipt."""
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
from looper.agents.runtimes import ADAPTERS, RoomPush, room_push
from looper.agents.runtimes.channel import DeliveryResult
from looper.agents.runtimes.dsh_runner import DshRunnerRuntime
from looper.agents.runtimes.pi_runner import PiRunnerRuntime
from looper.agents.tiers.worker import (
    ConsumeStart, RunContext, WorkerConfig, WorkerPool, _Reminders, _SteeredDelivery, room_input,
)
from tests.run_handle_support import ScriptedRun


@pytest.mark.parametrize(("kind", "review", "receipt"), [
    *((kind, review, "applied") for kind in ("pi", "dsh") for review in (False, True)),
    *(("pi", False, receipt) for receipt in ("dropped", "missing", "later_applied", "out_of_order")),
])
def test_runner_room_receipts_control_the_read_cursor(tmp_path, monkeypatch, agent_db, kind, review, receipt):
    from looper.agents.tiers import worker

    monkeypatch.setattr(worker, "ADDRESSED_INTERVAL_SEC", 0)
    runtime = PiRunnerRuntime(None, pi=None) if kind == "pi" else DshRunnerRuntime(None, dsh=None)
    monkeypatch.setitem(ADAPTERS, kind, runtime)
    launch = Launch()
    profile = replace(launch.profile, runtime=replace(launch.profile.runtime, type=kind,
                                                     capabilities=runtime.capabilities))
    launch = replace(launch, settings=replace(launch.settings, profile=profile))
    assert room_push(launch).value == "runner"
    assert room_input(launch, is_verify=review) is RoomInput.PUSHED
    seat = Seat("reviewer@s1" if review else "worker@s1", "reviewer" if review else "worker",
                MemberKind.REVIEW if review else MemberKind.WORK, run="w1")
    assert "Do not poll your inbox" in room_section(seat, RoomInput.PUSHED)

    async def run():
        pool = WorkerPool(config=WorkerConfig(), runtime_root=tmp_path)
        texts = ["[Looper room]\n- #41 from worker@s2 (info): one\n- #42 from worker@s2 (info): two",
                 "[Looper room]\n- #43 from worker@s2 (info): three"]
        deliveries = [RoomDelivery(texts[0], 42, (41, 42), True, 0),
                      RoomDelivery(texts[1], 43, (43,), True, 0)]
        room = SimpleNamespace(delivery=AsyncMock(side_effect=deliveries), confirm=AsyncMock())
        pool.room_ops = SimpleNamespace(room=room)
        fallback = Mock(side_effect=AssertionError("runner receipts must not arm a fallback"))
        monkeypatch.setattr(pool, "_room_item_fallback", fallback)
        sent = []
        states = []

        def event(subtype, message_id):
            return {"type": "system", "subtype": subtype, "runner": {
                "message_id": message_id, "after_request_id": "request-1", "reason": "turn_ended"}}

        class Run(ScriptedRun):
            async def send(self, text, *, uuid=None):
                sent.append((text, uuid))
                return await super().send(text, uuid=uuid)

            async def events(self):
                state = pool._reminders["w1"]
                states.append(state)
                assert state.receipt is RoomPush.RUNNER
                assert not await pool.remind("w1")
                room.delivery.assert_not_awaited()
                yield {"type": "assistant", "message": {"content": [
                    {"type": "tool_use", "id": "tool-1", "name": "bash", "input": {"command": "true"}}]}}
                assert sent == [(texts[0], next(iter(state.uuids)))]
                first = sent[0][1]
                assert first.endswith("-42")
                assert state.uuids[first] == 42
                room.confirm.assert_not_awaited()
                assert state.steered[42].fallback is None
                # Other protocols' echoes are not the runner's deterministic model receipt.
                yield {"type": "system", "subtype": "codex_user_message", "text": texts[0]}
                yield {"type": "user", "isReplay": True, "uuid": first}
                room.confirm.assert_not_awaited()
                yield event("steer_applied", "another-message")
                room.confirm.assert_not_awaited()
                if receipt in {"later_applied", "out_of_order"}:
                    assert await pool.remind("w1")
                    second = sent[1][1]
                    assert second.endswith("-43") and state.uuids[second] == 43
                    assert state.steered[43].fallback is None
                    if receipt == "out_of_order":
                        yield event("steer_applied", second)
                        room.confirm.assert_not_awaited()
                        yield event("steer_applied", first)
                        assert [call.args[2] for call in room.confirm.await_args_list] == [42, 43]
                    else:
                        yield event("steer_dropped", first)
                        assert state.receipts_closed and not state.steered
                        yield event("steer_applied", second)
                        room.confirm.assert_not_awaited()
                        assert state.confirmed == 0
                elif receipt == "applied":
                    yield event("steer_applied", first)
                    room.confirm.assert_awaited_once_with("task", seat.key, 42)
                    yield event("steer_applied", first)
                    assert room.confirm.await_count == 1
                elif receipt == "dropped":
                    yield event("steer_dropped", first)
                    assert state.receipts_closed and not state.steered
                    room.confirm.assert_not_awaited()
                yield {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "tool-1"}]}}
                assert state.tools_running == 0
                report = {"passed": True, "summary": "done", "findings": []} if review else {
                    "status": "completed", "summary": "done", "changed_files": {}}
                yield {"type": "result", "subtype": "success", "result": json.dumps(report),
                       "user_message_uuids": [first]}

        handle = Run([])
        ctx = RunContext("w1", "task", "s1", str(tmp_path), "do the step", review, launch, runtime,
                         seat, 5, datetime.now(timezone.utc), True, [])
        recorder = Mock(cap_stop=None, record=AsyncMock(), finish=AsyncMock())
        try:
            result = await pool._consume_once(ctx, handle, recorder, start=ConsumeStart.NEW)
            assert result["passed"] if review else result["status"] == "completed"
            if receipt in {"missing", "dropped", "later_applied"}:
                room.confirm.assert_not_awaited()
            if receipt in {"dropped", "later_applied"}:
                state = states[0]
                await pool._confirm_delivery("w1", state, 43)
                room.confirm.assert_not_awaited()
                state.steered[43] = _SteeredDelivery(deliveries[1], observed=True)
                await pool._confirm_room_prefix("w1", state)
                assert 43 in state.steered
                room.confirm.assert_not_awaited()
            fallback.assert_not_called()
            phases = [call.kwargs["summary"] for call in agent_db.insert_activity.await_args_list
                      if call.kwargs["kind"] == "phase"]
            if receipt == "applied":
                assert "Room messages #41, #42 reached the model (after request request-1)" in phases
            if receipt in {"dropped", "later_applied"}:
                assert "Room delivery dropped (turn_ended); its messages stay unread" in phases
                assert "Planner message dropped (turn_ended); kept for the Planner's decision" not in phases
            assert phases.count("Planner message reached the model in the run's turn (after request request-1)") == 1
        finally:
            pool._drop_reminders("w1")
        assert states and "w1" not in pool._reminders

    asyncio.run(asyncio.wait_for(run(), 5))


@pytest.mark.parametrize("outcome", ["accepted", "rejected", "unknown", "unsupported"])
def test_runner_send_outcomes_preserve_unread_messages(tmp_path, monkeypatch, caplog, outcome):
    async def run():
        pool = WorkerPool(config=WorkerConfig(), runtime_root=tmp_path)
        delivery = RoomDelivery("a room message", 42, (42,), True, 0)
        confirm = AsyncMock()
        send = AsyncMock(return_value=DeliveryResult[outcome.upper()] if outcome != "unsupported" else None)
        if outcome == "unsupported":
            send.side_effect = RuntimeError("the runner cannot steer this run")
        fallback = Mock(side_effect=AssertionError("runner receipts must not arm a fallback"))
        monkeypatch.setattr(pool, "_room_item_fallback", fallback)
        pool._inputs["w1"] = SimpleNamespace(send=send)
        pool._reminders["w1"] = state = _Reminders(
            next_delivery=AsyncMock(return_value=delivery), confirm=confirm, through=40,
            receipt=RoomPush.RUNNER, tools_running=1)
        try:
            assert await pool.remind("w1") is (outcome == "accepted")
            send.assert_awaited_once()
            assert send.await_args.args == (delivery.text,)
            assert send.await_args.kwargs["uuid"].endswith("-42")
            confirm.assert_not_awaited()
            assert state.confirmed == 0
            written = outcome in {"accepted", "unknown"}
            assert state.through == (42 if written else 40)
            assert state.pending is not written
            assert bool(state.uuids) is written
            assert bool(state.steered) is written
            assert state.retry is None
            fallback.assert_not_called()
            if outcome == "unknown":
                assert "room delivery acceptance unknown; not retrying" in caplog.text
        finally:
            pool._drop_reminders("w1")

    asyncio.run(asyncio.wait_for(run(), 5))
