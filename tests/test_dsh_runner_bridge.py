"""Delayed authorization replies cross real DSH and production bridge boundaries."""
import asyncio
import threading

import pytest

from dsh_installed_stand_in import MessagesServer, Reply
from dsh_runner_support import guard_env, raw_runner, require_dsh
from looper.agents.runtimes.runner import protocol as p
from looper.agents.runtimes.runner.bridge import RunnerBridge
from looper.agents.runtimes.runner.turn import RunnerTurn
from test_dsh_runner_turns import params_for, run

pytestmark = pytest.mark.installed


@pytest.mark.parametrize("reply_timing", ["before-settled", "after-settled"])
def test_old_authorization_reply_cannot_interrupt_resumed_turn(tmp_path, monkeypatch, reply_timing):
    async def exercise():
        _, installed = require_dsh()
        finish_new = threading.Event()
        with MessagesServer([Reply(["Resumed answer"], hold=finish_new)]) as server:
            async with raw_runner(tmp_path, installed, env=guard_env(tmp_path, server.port)) as (process, _, _):
                failures, updates, held = [], [], []
                bridge = RunnerBridge(process.stdout, process.stdin, "dsh", on_failure=failures.append)
                waiting, release, delivered = asyncio.Event(), asyncio.Event(), asyncio.Event()
                write, enqueue = bridge._write, bridge._enqueue

                async def delay_reply(message, entry=None):
                    if isinstance(message, p.Response) and message.result.get("trace_id") == "old":
                        held.append(message)
                        waiting.set()
                        await release.wait()
                        await write(message, entry)
                        delivered.set()
                    else:
                        await write(message, entry)

                def record_update(update):
                    updates.append(update)
                    enqueue(update)

                monkeypatch.setattr(bridge, "_write", delay_reply)
                monkeypatch.setattr(bridge, "_enqueue", record_update)
                try:
                    await bridge.initialize(permitted=("turn", "compaction"))
                    params = params_for(tmp_path, server)
                    params["provider"]["reasoning"] = True
                    params["limits"]["tool_output_bytes"] = 102400
                    sid = await bridge.open_session(params)

                    async def limits():
                        return {"run_cap": None, "task_cap": None, "task_spent": 0}

                    def turn(trace):
                        return RunnerTurn(session_id=sid, trace_id=trace, role="manager", tools=None,
                            provider_id=params["provider"]["account_id"], model=params["provider"]["model"],
                            price=lambda model, usage: 0, limits=limits)

                    old_events = []
                    await bridge.begin_turn(sid, "old", old_events.append, turn("old"), text="Cancel this input")
                    await asyncio.wait_for(waiting.wait(), 5)
                    assert held[0].result["allow"] is True
                    await bridge.cancel_turn(sid, "old")
                    if reply_timing == "before-settled":
                        release.set()
                    old_summary = await asyncio.wait_for(bridge.wait_settled(sid, "old"), 6)
                    assert old_summary.outcome == "cancelled"
                    assert old_summary.usage_complete is (reply_timing == "before-settled")
                    assert not server.requests
                    if reply_timing == "before-settled":
                        await bridge.close_session(sid)
                        assert await bridge.load_session({**params, "session_id": sid}) == sid
                    else:
                        # Forced cancellation already released the SDK session;
                        # retain the host entry and reload its durable id.
                        assert await bridge.request("session/load", {**params, "session_id": sid}) == {
                            "session_id": sid, "resumed": True}

                    new_events = []
                    await bridge.begin_turn(sid, "new", new_events.append, turn("new"), text="Continue normally")
                    assert await asyncio.to_thread(server.received.wait, 5)
                    assert len(server.requests) == 1
                    boundary = len(updates)
                    release.set()
                    await asyncio.wait_for(delivered.wait(), 5)
                    assert (await bridge.health())["running"] == 1
                    # Replay a consumed id, then an id never issued. Neither may
                    # disconnect the shared subprocess or emit an old update.
                    await write(held[0])
                    await write(p.Response("r-999999", result={"allow": True}))
                    await write(p.Response("r-999999", result={"allow": True}))
                    assert (await bridge.health())["running"] == 1
                    assert not failures
                    finish_new.set()
                    new_summary = await bridge.wait_settled(sid, "new")
                    assert new_summary.outcome == "completed" and new_summary.usage_complete
                    assert new_events[-1]["result"] == "Resumed answer"
                    assert not failures
                    assert not [u for u in updates[boundary:] if u["trace_id"] == "old"]
                    old_updates = [u for u in updates if u["trace_id"] == "old"]
                    ends = [u for u in old_updates if u["kind"] == "request_end"]
                    assert len(ends) == (1 if reply_timing == "before-settled" else 0)
                    if ends:
                        assert ends[0]["status"] == "aborted" and ends[0]["complete"]
                        assert old_updates.index(ends[0]) < next(i for i, u in enumerate(old_updates)
                            if u["kind"] == "settled")
                    assert len([e for e in old_events if e["type"] == "result"]) == 1
                    assert len([e for e in new_events if e["type"] == "result"]) == 1
                    assert (await bridge.health())["inflight_requests"] == 0
                    await bridge.close_session(sid)
                finally:
                    release.set()
                    finish_new.set()
                    await bridge.close()
                assert await asyncio.wait_for(process.wait(), 5) == 0
                stderr = (await process.stderr.read()).decode()
                assert stderr.count("ignored unknown response id") == 1

    run(exercise())
