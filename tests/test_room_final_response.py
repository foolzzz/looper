"""A room read racing the last tool result must not displace the step report."""
from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from looper.agents.agent_settings import Launch
from looper.agents.room import MemberKind, RoomService, Seat
from looper.agents.runtimes import ADAPTERS, Capability
from looper.agents.runtimes.dsh_runner import DshRunnerRuntime
from looper.agents.runtimes.pi_runner import PiRunnerRuntime
from looper.agents.tiers.worker import ConsumeStart, RunContext, WorkerConfig, WorkerPool
from tests.run_handle_support import ScriptedRun


@pytest.mark.parametrize("runtime_type", ["codex", "coco", "omp", "pi", "grok", "pi-runner", "dsh-runner"])
def test_room_read_crossing_last_tool_result_preserves_report_and_unread(
    tmp_path, monkeypatch, agent_db, runtime_type,
):
    from looper.agents import room as room_module

    kind = runtime_type.removesuffix("-runner")
    if runtime_type.endswith("-runner"):
        adapter = PiRunnerRuntime(None, pi=None) if kind == "pi" else DshRunnerRuntime(None, dsh=None)
        monkeypatch.setitem(ADAPTERS, kind, adapter)
    launch = Launch()
    runtime = replace(launch.profile.runtime, type=kind, capabilities=ADAPTERS[kind].capabilities | {Capability.STEER})
    launch = replace(launch, settings=replace(launch.settings, profile=replace(launch.profile, runtime=runtime)))

    async def run():
        pool = WorkerPool(config=WorkerConfig(), runtime_root=tmp_path)
        seat = Seat("worker@s1", "worker", MemberKind.WORK, run="w1")
        room = RoomService(worker_pool=pool)
        pool.room_ops = SimpleNamespace(room=room)
        monkeypatch.setattr(room, "enabled", AsyncMock(return_value=True))
        cursor = 0

        async def mark_read(_task, _seat, through):
            nonlocal cursor
            cursor = through

        mark = AsyncMock(side_effect=mark_read)
        monkeypatch.setattr(room_module.db_room, "mark_read", mark)
        monkeypatch.setattr(room_module.db_room, "last_read", AsyncMock(side_effect=lambda *_: cursor))
        row = {"id": 42, "sender_seat": "worker@s2", "kind": "question", "body": "new instructions",
               "audience": [seat.key], "audience_all": False}
        reading, release = asyncio.Event(), asyncio.Event()

        async def messages(_task, *, after=0):
            reading.set()
            await release.wait()
            return [row] if after < row["id"] else []

        monkeypatch.setattr(room_module.db_room, "messages", AsyncMock(side_effect=messages))
        report = json.dumps({"status": "completed", "summary": "step report", "changed_files": {}})

        class Run(ScriptedRun):
            async def events(self):
                state = pool._reminders["w1"]
                state.pending = False
                yield {"type": "assistant", "message": {"content": [
                    {"type": "tool_use", "id": "tool", "name": "bash", "input": {"command": "true"}}]}}
                sending = asyncio.create_task(pool.remind("w1"))
                try:
                    await reading.wait()
                    yield {"type": "user", "message": {"content": [
                        {"type": "tool_result", "tool_use_id": "tool", "content": "done"}]}}
                    yield {"type": "assistant", "message": {"content": [{"type": "text", "text": report}]}}
                    assert state.tools_running == 0
                    release.set()
                    await sending
                    yield {"type": "result", "subtype": "success",
                           "result": "Reply to the room message" if self.inputs else report}
                finally:
                    release.set()
                    sending.cancel()
                    await asyncio.gather(sending, return_exceptions=True)

        handle = Run([])
        ctx = RunContext("w1", "task", "s1", str(tmp_path), "do the step", False, launch, launch.runtime,
                         seat, 3, datetime.now(timezone.utc), False, [])
        recorder = Mock(cap_stop=None, record=AsyncMock(), finish=AsyncMock())
        result = await pool._consume_once(ctx, handle, recorder, start=ConsumeStart.NEW)
        assert result["status"] == "completed", result
        assert result["summary"] == "step report"
        assert handle.inputs == []
        mark.assert_not_awaited()
        assert cursor == 0
        # The same seat's next run lists the untouched message again.
        next_seat = replace(seat, run="w2")
        delivery = await room.delivery("task", next_seat)
        assert delivery is not None and delivery.ids == (42,)
        assert "new instructions" in delivery.text

    asyncio.run(asyncio.wait_for(run(), 5))
