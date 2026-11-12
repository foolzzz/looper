"""A waiting Worker keeps its input and wakes without an inbox polling turn."""
from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from looper.agents.bus import MessageBus
from looper.agents.room import RoomDelivery
from looper.agents.message import MessageEnvelope, MessageType, planner_address, worker_address
from looper.agents.runtimes import ADAPTERS, Capability
from looper.agents.runtimes.channel import DeliveryResult
from looper.agents.runtimes.claude_code import ClaudeCodeRuntime
from looper.agents.tiers import worker
from tests.run_handle_support import ScriptedRun
from tests.runtime_model_helpers import builtin_launch


def _result(report: Any, **fields: Any) -> dict[str, Any]:
    return {"type": "result", "subtype": "success", "is_error": False,
            "result": json.dumps(report) if isinstance(report, dict) else report, **fields}


def _waiting(**fields: Any) -> dict[str, Any]:
    return _result({"status": "waiting", "waiting_for": "the build", "check_after_seconds": 600, **fields})


def _completed() -> dict[str, Any]:
    return _result({"status": "completed", "summary": "build passed", "changed_files": {}})


@pytest.mark.parametrize("report,expected", [
    ({"status": "waiting", "waiting_for": "the build", "check_after_seconds": 30}, ("the build", 30)),
    ({"status": "waiting", "waiting_for": "x" * 300, "check_after_seconds": 1800}, ("x" * 300, 1800)),
    *(({"status": "waiting", "waiting_for": "the build", "check_after_seconds": value},
       "check_after_seconds must be an integer from 30 to 1800") for value in (10, 5000, True, 30.0, None)),
    *(({"status": "waiting", "waiting_for": value, "check_after_seconds": 600},
       "waiting_for must be a short text (at most 300 characters)") for value in ("", "   ", "x" * 301, None)),
    ({"status": "completed"}, None),
    ("still building", None),
])
def test_waiting_report_validation(report: Any, expected: Any) -> None:
    found = worker.waiting_report(_result(report))
    if isinstance(expected, tuple):
        assert found == worker.WaitingReport(*expected)
    else:
        assert found == expected


def test_unsuccessful_result_cannot_request_waiting() -> None:
    assert worker.waiting_report({**_waiting(), "is_error": True}) is None
    assert worker.waiting_report({**_waiting(), "subtype": "error_max_turns"}) is None


def test_ending_with_a_waiting_report_fails_with_the_reason(tmp_path: Path) -> None:
    outcome = worker.judge_result(_waiting(), work_dir=tmp_path, is_verify=False)
    assert outcome["status"] == "failed"
    assert "while waiting for: the build" in outcome["error"]


class _Run(ScriptedRun):
    def __init__(self) -> None:
        super().__init__([])
        self.events_in: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        self.results_seen: asyncio.Queue[int] = asyncio.Queue()
        self.writes: asyncio.Queue[str] = asyncio.Queue()
        self.result_count = 0
        self.ended_after: list[int] = []
        self.delivery = DeliveryResult.ACCEPTED
        self.send_error: Exception | None = None

    def turn(self, result: dict[str, Any], *, init: bool = True) -> None:
        if init:
            self.events_in.put_nowait({"type": "system", "subtype": "init", "session_id": "waiting-session"})
            self.events_in.put_nowait({"type": "assistant", "message": {"content": []}})
        self.events_in.put_nowait(result)

    async def events(self) -> AsyncIterator[dict[str, Any]]:
        while (event := await self.events_in.get()) is not None:
            if event.get("type") == "result":
                self.result_count += 1
            yield event
            if event.get("type") == "result":
                self.results_seen.put_nowait(self.result_count)

    async def send(self, text: str, *, uuid: str | None = None) -> DeliveryResult:
        if self.send_error is not None:
            raise self.send_error
        self.inputs.append(text)
        self.writes.put_nowait(text)
        return self.delivery

    def end_input(self) -> None:
        self.ended_after.append(self.result_count)
        self.events_in.put_nowait(None)

    async def end(self) -> None:
        await super().end()
        self.events_in.put_nowait(None)


@asynccontextmanager
async def _pool(root: Path, monkeypatch: pytest.MonkeyPatch, run: _Run, *,
                autonomous: bool = True, timeout: int = 0, room_ops: Any = None):
    class Runtime(ClaudeCodeRuntime):
        hosts_runs = True
        capabilities = ClaudeCodeRuntime.capabilities if autonomous else (
            ClaudeCodeRuntime.capabilities - {Capability.AUTONOMOUS_TURNS})

        def hosted_run(self, executable: str, **kwargs: Any) -> _Run:
            return run

    monkeypatch.setitem(ADAPTERS, "claude-code", Runtime())
    monkeypatch.setattr(worker, "load_agent_settings", AsyncMock(return_value=builtin_launch().settings))
    monkeypatch.setattr(worker, "_load_settings", AsyncMock(return_value=worker.WorkerSettings(timeout, 0, "work")))
    monkeypatch.setattr(worker, "load_task_limit", AsyncMock(return_value=1))
    bus = MessageBus(runtime_root=root)
    results: list[MessageEnvelope] = []
    bus.subscribe_topic(MessageType.WORK_RESULT, results.append)
    pool = worker.WorkerPool(config=worker.WorkerConfig(), runtime_root=root)
    pool.room_ops = room_ops
    await pool.start(bus)
    try:
        item = MessageEnvelope.create(
            source=planner_address("task"), target=worker_address("w1"), message_type=MessageType.WORK_ITEM,
            payload={"task_id": "step", "parent_task_id": "task", "instruction": "check the build",
                     "context": {"work_dir": str(root)}},
        )
        await pool._dispatch(item)
        await run.started.wait()
        yield pool, results
    finally:
        await pool.stop()
        await bus.shutdown()


def test_waiting_keeps_input_until_an_autonomous_turn_completes(tmp_path, monkeypatch, agent_db):
    async def scenario():
        run = _Run()
        async with _pool(tmp_path, monkeypatch, run) as (pool, results):
            run.turn(_waiting())
            assert await run.results_seen.get() == 1
            assert run.ended_after == []
            assert await pool.send_input("w1", "the build is ready") is DeliveryResult.ACCEPTED
            run.turn({**_completed(), "origin": "task-notification"}, init=False)
            # An autonomous background notification need not carry another init event.
            await pool._tasks["w1"]
            assert results[0].payload["status"] == "completed"
            assert run.ended_after == [2]
            assert not pool.accepts_input("w1")
            phases = [call.kwargs.get("metadata") for call in agent_db.insert_activity.await_args_list]
            assert {"state": "waiting", "check_after_seconds": 600} in phases

    asyncio.run(asyncio.wait_for(scenario(), 5))


@pytest.mark.parametrize("trigger", ["system", "assistant"])
def test_a_new_turn_cancels_the_waiting_check(tmp_path, monkeypatch, agent_db, trigger):
    async def scenario():
        release, sleeping, cancelled = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def sleep(seconds):
            sleeping.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise

        monkeypatch.setattr(worker, "_waiting_sleep", sleep)
        run = _Run()
        async with _pool(tmp_path, monkeypatch, run) as (pool, results):
            run.turn(_waiting())
            await sleeping.wait()
            run.events_in.put_nowait({"type": "system", "subtype": "init"} if trigger == "system" else
                                    {"type": "assistant", "message": {"content": []}})
            await cancelled.wait()
            release.set()
            run.turn(_completed(), init=False)
            await pool._tasks["w1"]
            assert results[0].payload["status"] == "completed"
            assert not run.inputs
            assert any(call.kwargs.get("metadata") == {"state": "resumed"}
                       for call in agent_db.insert_activity.await_args_list)

    asyncio.run(asyncio.wait_for(scenario(), 5))


@pytest.mark.parametrize("fixture", ["background_agent", "agent_team_worker"])
def test_subagent_events_preserve_the_waiting_check_and_reminders(tmp_path, monkeypatch, agent_db, fixture):
    events = [json.loads(line) for line in (
        Path(__file__).parent / "fixtures" / "claude_stream" / f"{fixture}.jsonl").read_text().splitlines()]
    background = [event for event in events if event.get("parent_tool_use_id")]
    assert any(event["type"] == "assistant" for event in background)
    assert any(event["type"] == "user" for event in background)

    async def scenario():
        processed, sleeping, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

        class Run(_Run):
            async def events(self):
                async for event in super().events():
                    yield event
                    if event is background[-1]:
                        processed.set()

        async def sleep(seconds):
            sleeping.set()
            await release.wait()

        monkeypatch.setattr(worker, "_waiting_sleep", sleep)
        run = Run()
        async with _pool(tmp_path, monkeypatch, run, timeout=1) as (pool, results):
            pool._reminders["w1"] = state = worker._Reminders(next_delivery=AsyncMock(return_value=RoomDelivery(
                "the room has new information", 42, (42,), True, 0)), confirm=AsyncMock())
            run.turn(_waiting())
            await sleeping.wait()
            for event in background:
                run.events_in.put_nowait(event)
            await processed.wait()
            assert state.waiting and state.tools_running == 0
            assert not any(call.kwargs.get("metadata") == {"state": "resumed"}
                           for call in agent_db.insert_activity.await_args_list)
            assert await pool.remind("w1")
            assert await run.writes.get() == "the room has new information"
            release.set()
            assert "600 s have passed since you began waiting for: the build" in await run.writes.get()
            await pool._tasks["w1"]
            assert results[0].payload["status"] == "failed"
            assert "timeout_seconds" in results[0].payload["error"]
            assert "while waiting for: the build" in results[0].payload["error"]

    asyncio.run(asyncio.wait_for(scenario(), 5))


def test_the_check_timer_writes_a_wake_message(tmp_path, monkeypatch, agent_db):
    async def scenario():
        monkeypatch.setattr(worker, "_waiting_sleep", AsyncMock())
        run = _Run()
        async with _pool(tmp_path, monkeypatch, run) as (pool, results):
            run.turn(_waiting())
            assert await run.writes.get() == (
                "[Looper] 600 s have passed since you began waiting for: the build. Check on it, then go on, "
                "wait again with a waiting report, or end with your report.")
            run.turn(_completed())
            await pool._tasks["w1"]
            assert results[0].payload["status"] == "completed"

    asyncio.run(asyncio.wait_for(scenario(), 5))


@pytest.mark.parametrize("send_error", [False, True])
def test_a_check_that_cannot_be_delivered_ends_the_waiting_run(tmp_path, monkeypatch, agent_db, send_error):
    async def scenario():
        monkeypatch.setattr(worker, "_waiting_sleep", AsyncMock())
        run = _Run()
        run.delivery = DeliveryResult.REJECTED
        run.send_error = BrokenPipeError() if send_error else None
        async with _pool(tmp_path, monkeypatch, run) as (pool, results):
            run.turn(_waiting())
            await pool._tasks["w1"]
            assert run.ended_after == [1]
            assert results[0].payload["status"] == "failed"
            assert "while waiting for: the build" in results[0].payload["error"]

    asyncio.run(asyncio.wait_for(scenario(), 5))


def test_a_room_reminder_is_written_while_waiting_without_tools(tmp_path, monkeypatch, agent_db):
    async def scenario():
        run = _Run()
        async with _pool(tmp_path, monkeypatch, run) as (pool, _):
            pool._reminders["w1"] = state = worker._Reminders(next_delivery=AsyncMock(return_value=RoomDelivery(
                "the room has new information", 42, (42,), True, 0)), confirm=AsyncMock())
            assert not await pool.remind("w1")
            run.turn(_waiting())
            await run.results_seen.get()
            assert state.tools_running == 0 and state.waiting
            assert await run.writes.get() == "the room has new information"
            run.turn(_completed())
            await pool._tasks["w1"]

    asyncio.run(asyncio.wait_for(scenario(), 5))


@pytest.mark.parametrize("trigger", ["tool", "wait"])
def test_unread_messages_at_start_are_delivered_without_a_new_post(tmp_path, monkeypatch, agent_db, trigger):
    from types import SimpleNamespace
    from looper.agents import room as room_module
    from looper.agents.ops.room import RoomOps

    async def scenario():
        room = room_module.RoomService()
        monkeypatch.setattr(room, "_context", AsyncMock(return_value=SimpleNamespace(members=[])))
        monkeypatch.setattr(room, "enabled", AsyncMock(return_value=True))
        monkeypatch.setattr(room, "presence_changed", AsyncMock())
        monkeypatch.setattr(room_module.db_room, "last_read", AsyncMock(return_value=0))
        monkeypatch.setattr(room_module.db_room, "messages", AsyncMock(return_value=[{
            "id": 42, "sender_seat": "worker@earlier", "kind": "answer", "audience": ["worker@step"],
            "audience_all": False, "body": "Use port 8123",
        }]))
        run = _Run()
        ops = RoomOps(room, url="http://127.0.0.1:1")
        async with _pool(tmp_path, monkeypatch, run, room_ops=ops) as (pool, results):
            assert "They will be delivered to you." in run.prompt
            assert "Use port 8123" in run.prompt
            if trigger == "wait":
                run.turn(_waiting())
            else:
                run.events_in.put_nowait({"type": "assistant", "message": {"content": [
                    {"type": "tool_use", "id": "read-file", "name": "Read", "input": {"file_path": "app.py"}},
                ]}})
            delivered = await run.writes.get()
            assert "[Looper room]" in delivered and "#42" in delivered and "Use port 8123" in delivered
            assert pool.room_in_flight("w1") == 42
            run.turn(_completed())
            await pool._tasks["w1"]
            assert results[0].payload["status"] == "completed"
            assert run.writes.empty()

    asyncio.run(asyncio.wait_for(scenario(), 5))


def test_a_runtime_without_autonomous_turns_ends_on_waiting(tmp_path, monkeypatch, agent_db):
    async def scenario():
        run = _Run()
        async with _pool(tmp_path, monkeypatch, run, autonomous=False) as (pool, results):
            run.turn(_waiting())
            await pool._tasks["w1"]
            assert run.ended_after == [1]
            assert results[0].payload["status"] == "failed"
            assert "while waiting for: the build" in results[0].payload["error"]

    asyncio.run(asyncio.wait_for(scenario(), 5))


@pytest.mark.parametrize("missing_report", [False, True])
def test_a_second_invalid_turn_ends_after_one_repair(tmp_path, monkeypatch, agent_db, missing_report):
    async def scenario():
        run = _Run()
        async with _pool(tmp_path, monkeypatch, run) as (pool, results):
            if missing_report:
                run.turn(_waiting())
                await run.results_seen.get()
            invalid = _result("still building") if missing_report else _waiting(check_after_seconds=True)
            run.turn(invalid)
            await run.results_seen.get()
            assert run.ended_after == []
            expected = (
                "[Looper] Your message ended without a JSON report while you were waiting. End it with a waiting "
                "report to keep waiting, or with your report." if missing_report else
                "[Looper] Your waiting report was not taken: check_after_seconds must be an integer from 30 to "
                "1800. End your message with a valid waiting report or with your report.")
            assert await run.writes.get() == expected
            run.turn(invalid)
            await pool._tasks["w1"]
            assert run.inputs == [expected]
            assert run.ended_after == [3 if missing_report else 2]
            assert results[0].payload["status"] == "failed"

    asyncio.run(asyncio.wait_for(scenario(), 5))


@pytest.mark.parametrize("timeout", [0, 1])
def test_a_run_that_ends_while_waiting_names_what_it_waited_for(tmp_path, monkeypatch, agent_db, timeout):
    async def scenario():
        run = _Run()
        async with _pool(tmp_path, monkeypatch, run, timeout=timeout) as (pool, results):
            run.turn(_waiting())
            await run.results_seen.get()
            if not timeout:
                run.events_in.put_nowait(None)
            await pool._tasks["w1"]
            assert results[0].payload["status"] == "failed"
            assert "while waiting for: the build" in results[0].payload["error"]

    asyncio.run(asyncio.wait_for(scenario(), 5))


def test_the_waiting_contract_is_selected_by_runtime(tmp_path, monkeypatch, agent_db):
    from looper.agents.runtimes.codex import CodexRuntime
    from looper.agents.system_prompt import WAITING_CONTRACT, WORK_CONTRACT, work_contract

    assert work_contract(CodexRuntime()) == WORK_CONTRACT

    async def scenario():
        run = _Run()
        async with _pool(tmp_path, monkeypatch, run) as (pool, _):
            run.turn(_completed())
            await pool._tasks["w1"]
            metadata = agent_db.start_record.await_args.kwargs["metadata"]
            assert WAITING_CONTRACT in metadata["system_prompt"]
            assert metadata["prompt_layers"]["contract"]["name"] == "WORK_CONTRACT"

    asyncio.run(asyncio.wait_for(scenario(), 5))


def test_a_reminder_only_turn_rearms_the_original_wait(tmp_path, monkeypatch, agent_db):
    async def scenario():
        sleeps = asyncio.Queue()

        async def sleep(seconds):
            sleeps.put_nowait(seconds)
            await asyncio.Event().wait()

        monkeypatch.setattr(worker, "_waiting_sleep", sleep)
        run = _Run()
        async with _pool(tmp_path, monkeypatch, run) as (pool, results):
            pool._reminders["w1"] = state = worker._Reminders(next_delivery=AsyncMock(return_value=RoomDelivery(
                "the room has new information", 42, (42,), True, 0)), confirm=AsyncMock())
            run.turn(_waiting())
            original = await sleeps.get()
            await run.results_seen.get()
            assert await pool.remind("w1")
            state.confirm.assert_not_awaited()
            run.events_in.put_nowait({"type": "user", "isReplay": True, "uuid": next(iter(state.uuids)),
                                     "message": {"role": "user", "content": "the room has new information"}})
            run.turn(_result("room acknowledged", user_message_uuids=list(state.uuids)))
            await run.results_seen.get()
            remaining = await sleeps.get()
            assert 0 < remaining <= original <= 600
            assert state.waiting and not run.ended_after
            state.confirm.assert_awaited_once_with(42)
            assert run.inputs == ["the room has new information"]
            run.turn(_completed(), init=False)
            await pool._tasks["w1"]
            assert results[0].payload["status"] == "completed"

    asyncio.run(asyncio.wait_for(scenario(), 5))


@pytest.mark.parametrize("init", [False, True])
def test_a_room_reminder_turn_can_complete_the_work(tmp_path, monkeypatch, agent_db, init):
    async def scenario():
        run = _Run()
        async with _pool(tmp_path, monkeypatch, run) as (pool, results):
            pool._reminders["w1"] = state = worker._Reminders(next_delivery=AsyncMock(return_value=RoomDelivery(
                "the room has new information", 42, (42,), True, 0)), confirm=AsyncMock())
            run.turn(_waiting(check_after_seconds=1800))
            await run.results_seen.get()
            assert await pool.remind("w1")
            run.turn({**_completed(), "user_message_uuids": list(state.uuids)}, init=init)
            await run.results_seen.get()
            assert run.ended_after == [2]
            state.confirm.assert_awaited_once_with(42)
            await pool._tasks["w1"]
            assert results[0].payload["status"] == "completed"
            assert not pool.accepts_input("w1")
            assert run.inputs == ["the room has new information"]

    asyncio.run(asyncio.wait_for(scenario(), 5))


@pytest.mark.parametrize("seconds", [30, True])
def test_a_room_reminder_turn_processes_a_new_waiting_report(tmp_path, monkeypatch, agent_db, seconds):
    async def scenario():
        sleeps = asyncio.Queue()

        async def sleep(delay):
            sleeps.put_nowait(delay)
            await asyncio.Event().wait()

        monkeypatch.setattr(worker, "_waiting_sleep", sleep)
        run = _Run()
        async with _pool(tmp_path, monkeypatch, run) as (pool, results):
            pool._reminders["w1"] = state = worker._Reminders(next_delivery=AsyncMock(return_value=RoomDelivery(
                "the room has new information", 42, (42,), True, 0)), confirm=AsyncMock())
            run.turn(_waiting(check_after_seconds=1800))
            await sleeps.get()
            await run.results_seen.get()
            assert await pool.remind("w1")
            run.turn({**_waiting(waiting_for="the tests", check_after_seconds=seconds),
                      "user_message_uuids": list(state.uuids)})
            await run.results_seen.get()
            assert not run.ended_after
            if seconds is True:
                assert len(run.inputs) == 2
                assert "waiting report was not taken" in run.inputs[-1]
                run.turn(_completed())
            else:
                assert 0 < await sleeps.get() <= seconds
                assert state.waiting
                assert len(run.inputs) == 1
                run.events_in.put_nowait(None)
            await pool._tasks["w1"]
            if seconds is True:
                assert results[0].payload["status"] == "completed"
            else:
                assert results[0].payload["status"] == "failed"
                assert "while waiting for: the tests" in results[0].payload["error"]

    asyncio.run(asyncio.wait_for(scenario(), 5))


def test_a_valid_waiting_report_resets_the_repair_allowance(tmp_path, monkeypatch, agent_db):
    async def scenario():
        run = _Run()
        async with _pool(tmp_path, monkeypatch, run) as (pool, results):
            for event in (_waiting(check_after_seconds=10), _waiting(), _result("still building")):
                run.turn(event)
                await run.results_seen.get()
            assert not run.ended_after
            assert len(run.inputs) == 2
            assert "was not taken" in run.inputs[0] and "without a JSON report" in run.inputs[1]
            run.turn(_completed())
            await pool._tasks["w1"]
            assert results[0].payload["status"] == "completed"

    asyncio.run(asyncio.wait_for(scenario(), 5))


@pytest.fixture
def kept_manifest():
    from looper import components

    # Keeper startup hashes the checkout; the stream deadline tests only replay.
    return components.loaded_manifest()


@pytest.mark.parametrize(("room_pending", "resume_during_delivery"), [(False, False), (True, False), (True, True)])
def test_an_adopted_wait_is_checked_at_the_replay_boundary(tmp_path, monkeypatch, agent_db, kept_manifest,
                                                        room_pending, resume_during_delivery):
    from datetime import datetime, timezone
    from types import SimpleNamespace
    from unittest.mock import Mock

    from looper.agents.kept_run import KeptRun, KeptRunState

    async def scenario():
        launch = builtin_launch()
        runtime = ClaudeCodeRuntime()
        pool = worker.WorkerPool(config=worker.WorkerConfig(), runtime_root=tmp_path)
        delivery = RoomDelivery("A question posted while Looper was down", 42, (42,), True, 0)
        if room_pending:
            pool._reminders["w1"] = worker._Reminders(pending=True, next_delivery=AsyncMock(return_value=delivery))
        handle = KeptRun(runtime, ["claude"], launch=launch, cwd=tmp_path,
                         session_dir=None, label="waiting-run", run_dir=tmp_path / "kept",
                         runtime_root=tmp_path, epoch=2, on_stale=lambda reason: None)
        handle.state = KeptRunState.ATTACHED
        handle._process.stdout.replay_until = 1
        handle.end, handle.wait = AsyncMock(), AsyncMock(return_value=0)
        checked = asyncio.Event()
        resumed = asyncio.Event()
        sleep = AsyncMock(side_effect=AssertionError("an adopted wait must be checked immediately"))
        monkeypatch.setattr(worker, "_waiting_sleep", sleep)
        accounted, persisted = [], []
        recorder = SimpleNamespace(cap_stop=None, account=accounted.append,
                                   record=AsyncMock(side_effect=persisted.append), finish=AsyncMock())

        written = []

        async def send(text, **kwargs):
            assert handle.replay_finished.is_set()
            assert not agent_db.insert_activity.await_args_list
            written.append(text)
            if text == delivery.text:
                assert worker.uuid_through(kwargs["uuid"]) == 42
                checked.set()
                if resume_during_delivery:
                    await resumed.wait()
                return DeliveryResult.ACCEPTED
            assert text.startswith("[Looper] 600 s have passed since you began waiting for: the build.")
            checked.set()
            return DeliveryResult.ACCEPTED

        async def events():
            yield _waiting()
            assert not checked.is_set()
            handle.replay_finished.set()
            await checked.wait()
            yield {"type": "assistant", "message": {"content": []}}
            yield _completed()
            resumed.set()

        handle._channel = SimpleNamespace(events=events, send=send, end_input=Mock(), start=AsyncMock())
        ctx = worker.RunContext("w1", "task", None, str(tmp_path), "", False, launch, runtime,
                                None, 0, datetime.now(timezone.utc), False, ["claude"])
        outcome = await pool._consume(ctx, handle, recorder, start=worker.ConsumeStart.ADOPTED)
        assert outcome["status"] == "completed"
        if room_pending:
            assert written == [delivery.text]
            assert pool._reminders["w1"].through == 42
        assert accounted == [_waiting()] and persisted[-1] == _completed()
        assert not any((call.kwargs.get("metadata") or {}).get("state") == "waiting"
                       for call in agent_db.insert_activity.await_args_list)
        sleep.assert_not_awaited()
        handle._channel.start.assert_not_awaited()

    asyncio.run(asyncio.wait_for(scenario(), 5))


def test_an_adopted_wait_over_budget_ends_without_a_check_or_live_event(tmp_path, monkeypatch, agent_db, kept_manifest):
    from datetime import datetime, timezone
    from types import SimpleNamespace
    from unittest.mock import Mock

    from looper.agents.claude_cli import CapStop, soft_cap_result
    from looper.agents.kept_run import KeptRun, KeptRunState
    from looper.agents.run_usage import Usage

    async def scenario():
        launch, runtime = builtin_launch(), ClaudeCodeRuntime()
        handle = KeptRun(runtime, ["claude"], launch=launch, cwd=tmp_path,
                         session_dir=None, label="waiting-run", run_dir=tmp_path / "kept",
                         runtime_root=tmp_path, epoch=2, on_stale=lambda reason: None)
        handle.state = KeptRunState.ATTACHED
        handle._process.stdout.replay_until = 1
        handle.end, handle.wait = AsyncMock(), AsyncMock(return_value=0)
        capped = soft_cap_result(.2, .1)
        recorder = SimpleNamespace(cap_stop=None, record=AsyncMock(), finish=AsyncMock())

        def account(event):
            recorder.cap_stop = CapStop(capped, Usage(cost_usd=.2))

        recorder.account = account
        sleep = AsyncMock(side_effect=AssertionError("an over-budget wait must not arm a timer"))
        monkeypatch.setattr(worker, "_waiting_sleep", sleep)

        async def events():
            yield _waiting()
            handle.replay_finished.set()
            await asyncio.Event().wait()  # No live event can rescue a budget check waiting on output.

        channel = SimpleNamespace(events=events, send=AsyncMock(return_value=DeliveryResult.ACCEPTED),
                                  end_input=Mock(), start=AsyncMock())
        handle._channel = channel
        ctx = worker.RunContext("w1", "", None, str(tmp_path), "", False, launch, runtime,
                                None, 0, datetime.now(timezone.utc), False, ["claude"])
        pool = worker.WorkerPool(config=worker.WorkerConfig(), runtime_root=tmp_path)
        outcome = await pool._consume(ctx, handle, recorder, start=worker.ConsumeStart.ADOPTED)
        assert outcome["status"] == "failed" and "per-run spend cap" in outcome["error"]
        recorder.record.assert_awaited_once_with(capped)
        assert recorder.finish.await_args.kwargs["result_event"] == capped
        channel.send.assert_not_awaited()
        channel.end_input.assert_called_once()
        handle.end.assert_awaited_once()
        sleep.assert_not_awaited()

    asyncio.run(asyncio.wait_for(scenario(), 5))
