"""Hosted Worker runs keep the pool's lifecycle, accounting and room contracts."""
from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest

from looper.agents import proc_tree
from looper.agents.bus import MessageBus
from looper.agents.message import MessageEnvelope, MessageType, planner_address, worker_address
from looper.agents.ops.room import RoomOps
from looper.agents.run_usage import Usage
from looper.agents.runtimes import ADAPTERS, CostSource
from looper.agents.runtimes.api.loop import ToolCall
from looper.agents.runtimes.channel import RunStartError
from looper.agents.runtimes.claude_code import ClaudeCodeRuntime
from looper.agents.runtimes.pricing import usage_event
from looper.agents.tiers import worker
from looper.agents.tiers.worker import WorkerConfig, WorkerPool
from tests.run_handle_support import ScriptedRun
from tests.runtime_model_helpers import builtin_launch


def _events() -> list[dict[str, Any]]:
    return [{"type": "system", "subtype": "init", "session_id": "hosted-session"},
            {"type": "result", "subtype": "success", "is_error": False,
             "result": json.dumps({"status": "completed", "summary": "done", "changed_files": {}})}]


def _item(root: Path, worker_id: str = "w1") -> MessageEnvelope:
    return MessageEnvelope.create(
        source=planner_address("t1"), target=worker_address(worker_id), message_type=MessageType.WORK_ITEM,
        payload={"task_id": "s1", "parent_task_id": "t1", "instruction": "do it",
                 "context": {"work_dir": str(root)}},
    )


@asynccontextmanager
async def _pool(root: Path, monkeypatch: pytest.MonkeyPatch, *runs: ScriptedRun,
                timeout: int = 0, cap: float | None = None):
    calls: list[dict[str, Any]] = []
    remaining = iter(runs)

    class Runtime(ClaudeCodeRuntime):
        hosts_runs = True

        def hosted_run(self, executable: str, **kwargs: Any) -> ScriptedRun:
            calls.append(kwargs)
            return next(remaining)

        def cost_source(self, provider: Any) -> CostSource:
            return CostSource.PROVIDER_PRICE if cap is not None else super().cost_source(provider)

    monkeypatch.setitem(ADAPTERS, "claude-code", Runtime())
    settings = replace(builtin_launch().settings, max_budget_usd=cap)
    monkeypatch.setattr(worker, "load_agent_settings", AsyncMock(return_value=settings))
    monkeypatch.setattr(worker, "_load_settings", AsyncMock(return_value=worker.WorkerSettings(timeout, 0, "work")))
    monkeypatch.setattr(worker, "load_task_limit", AsyncMock(return_value=1))
    monkeypatch.setattr(proc_tree, "spawn", Mock(side_effect=AssertionError("a hosted run spawned a process")))
    bus = MessageBus(runtime_root=root)
    results: list[MessageEnvelope] = []
    bus.subscribe_topic(MessageType.WORK_RESULT, results.append)
    pool = WorkerPool(config=WorkerConfig(), runtime_root=root)
    await pool.start(bus)
    try:
        yield pool, calls, results
    finally:
        await pool.stop()
        await bus.shutdown()


async def _finish(pool: WorkerPool, root: Path, worker_id: str = "w1") -> None:
    await pool._dispatch(_item(root, worker_id))
    await pool._tasks[worker_id]
    await asyncio.sleep(0)  # the done callback releases the slot
    assert worker_id not in pool._slots and worker_id not in pool._active


def test_hosted_run_completes_without_a_process(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                              agent_db: Any) -> None:
    run = ScriptedRun(_events())

    async def scenario() -> None:
        async with _pool(tmp_path, monkeypatch, run) as (pool, calls, results):
            await _finish(pool, tmp_path)
            assert results[0].payload["status"] == "completed"
            assert agent_db.start_record.await_args.kwargs["metadata"]["command"] == run.command
            assert agent_db.finish_record.await_args.kwargs["status"] == "completed"
            assert calls[0]["tools"] is None
            assert run.launch_calls == run.wait_calls == 1 and run.end_calls == 0

    asyncio.run(asyncio.wait_for(scenario(), 5))


@pytest.mark.parametrize("reason", ["kill", "timeout", "cap"])
def test_hosted_run_is_ended_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                agent_db: Any, reason: str) -> None:
    events = _events()
    if reason == "cap":
        events.insert(1, usage_event(None, Usage(input_tokens=1_000_000, output_tokens=0,
                                               cache_read_tokens=0, cache_creation_tokens=0)))
    run = ScriptedRun(events, hold=reason != "cap")

    async def scenario() -> None:
        async with _pool(tmp_path, monkeypatch, run, timeout=1 if reason == "timeout" else 0,
                         cap=.01 if reason == "cap" else None) as (pool, _, results):
            if reason == "kill":
                await pool._dispatch(_item(tmp_path))
                await run.started.wait()
                assert await pool.kill("w1")
                assert results == []
            else:
                await _finish(pool, tmp_path)
                assert results[0].payload["status"] == "failed"
                assert ("timeout_seconds" if reason == "timeout" else "per-run spend cap") in results[0].payload["error"]
            assert run.end_calls == 1 and not pool._slots
        assert run.end_calls == 1

    asyncio.run(asyncio.wait_for(scenario(), 5))


def test_hosted_start_failure_fails_like_a_missing_executable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                           agent_db: Any) -> None:
    failed = ScriptedRun([], launch_error=RunStartError("the pi runner has no free session (CAPACITY)"))
    next_run = ScriptedRun(_events())

    async def scenario() -> None:
        async with _pool(tmp_path, monkeypatch, failed, next_run) as (pool, _, results):
            await _finish(pool, tmp_path)
            assert results[0].payload["status"] == "failed"
            assert results[0].payload["error"] == "Cannot start Claude Code: the pi runner has no free session (CAPACITY)"
            assert agent_db.finish_record.await_args.kwargs["status"] == "failed"
            await _finish(pool, tmp_path, "w2")
            assert results[1].payload["status"] == "completed" and next_run.launch_calls == 1

    asyncio.run(asyncio.wait_for(scenario(), 5))


def test_hosted_run_gets_room_tools_in_process(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                             agent_db: Any) -> None:
    run = ScriptedRun(_events(), hold=True)

    async def scenario() -> None:
        async with _pool(tmp_path, monkeypatch, run) as (pool, calls, results):
            room = Mock(presence_changed=AsyncMock(), first_turn=AsyncMock(return_value="room"),
                        roster=AsyncMock(return_value={"members": []}))
            ops = RoomOps(room, url="http://127.0.0.1:1")
            issue = Mock(wraps=ops.issue)
            monkeypatch.setattr(ops, "issue", issue)
            monkeypatch.setattr(ops, "library_with_server", AsyncMock(side_effect=AssertionError("MCP file written")))
            pool.room_ops = ops
            await pool._dispatch(_item(tmp_path))
            task = pool._tasks["w1"]
            await run.started.wait()
            tools = calls[0]["tools"]
            token = tools._token
            listing = ops.listing("Bearer " + token)
            assert listing
            assert [(spec.name, spec.description, spec.schema) for spec in await tools.specs()] == [
                (tool["name"], tool["description"], tool["inputSchema"]) for tool in listing]
            assert ops.authorized("Bearer " + token) and issue.call_count == 1
            invalid = await tools.call(ToolCall("bad-call", "looper_room_roster", "[]"))
            assert invalid.is_error and not invalid.ran
            live = await tools.call(ToolCall("live-call", "looper_room_roster", "{}"))
            assert not live.is_error and live.ran
            assert json.loads(live.text) == {"status": "ok", "result": {"members": []}}
            run.release.set()
            await task
            assert results[0].payload["status"] == "completed"
            answer = await tools.call(ToolCall("call", "looper_room_roster", "{}"))
            assert answer.is_error and "this run's token no longer works" in answer.text
            assert not ops._files

    asyncio.run(asyncio.wait_for(scenario(), 5))


def test_dropped_input_reaches_the_result(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, agent_db: Any) -> None:
    async def scenario() -> None:
        events = [*_events()[:1],
                  {"type": "system", "subtype": "steer_applied", "runner": {"after_request_id": "request-1"}},
                  {"type": "system", "subtype": "steer_dropped", "runner": {"reason": "turn_ended"}},
                  *_events()[1:]]
        async with _pool(tmp_path, monkeypatch, ScriptedRun(events, dropped_input=["late steer"])) as (
                pool, _, results):
            await _finish(pool, tmp_path)
            assert results[0].payload["missed_input"] == ["late steer"]
            phases = [call.kwargs["summary"] for call in agent_db.insert_activity.await_args_list
                      if call.kwargs["kind"] == "phase"]
            assert phases.count("Planner message reached the model in the run's turn (after request request-1)") == 1
            assert phases.count("Planner message dropped (turn_ended); kept for the Planner's decision") == 1

    asyncio.run(asyncio.wait_for(scenario(), 5))


def test_pool_stop_ends_hosted_runs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, agent_db: Any) -> None:
    run = ScriptedRun(_events(), hold=True)

    async def scenario() -> None:
        async with _pool(tmp_path, monkeypatch, run) as (pool, _, results):
            await pool._dispatch(_item(tmp_path))
            await run.started.wait()
            await pool.stop()
            assert run.end_calls == 1 and not pool._slots and not pool._active and results == []
        assert run.end_calls == 1

    asyncio.run(asyncio.wait_for(scenario(), 5))


def test_channel_start_exception_ends_the_hosted_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                   agent_db: Any) -> None:
    run = ScriptedRun([], start_error=RuntimeError("session refused the turn"))

    async def scenario() -> None:
        async with _pool(tmp_path, monkeypatch, run) as (pool, _, results):
            await _finish(pool, tmp_path)
            assert run.end_calls == 1 and run.wait_calls == 0
            assert results[0].payload["status"] == "failed"
            assert results[0].payload["error"] == "session refused the turn"
            assert agent_db.finish_record.await_args.kwargs["status"] != "running"

    asyncio.run(asyncio.wait_for(scenario(), 5))


@pytest.mark.parametrize("task_id, task_cap, spent", [("t1", 8, "3.5"), ("t1", 8, None),
                                                      ("t1", None, 0), ("", 8, 0)])
def test_run_limits_read_task_costs_only_when_capped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                  agent_db: Any, task_id: str, task_cap: float | None,
                                                  spent: str | float | None) -> None:
    costs = AsyncMock(return_value={"t1": {"cost_usd": spent}})
    monkeypatch.setattr(worker.db_llm_records, "task_costs", costs)
    launch = builtin_launch()
    launch = replace(launch, settings=replace(launch.settings, max_budget_usd=2))
    limits = WorkerPool._run_limits(launch, {"task_budget_usd": task_cap}, task_id)
    cap = task_cap if task_id else None
    assert asyncio.run(limits()) == {"run_cap": 2, "task_cap": cap,
                                     "task_spent": (float(spent) if spent is not None else None) if cap else 0}
    if cap is not None:
        costs.assert_awaited_once_with([task_id])
    else:
        costs.assert_not_awaited()
