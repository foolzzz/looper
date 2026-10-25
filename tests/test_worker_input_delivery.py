"""Worker input must settle before the Planner gives up on its acknowledgement."""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from fake_claude import fake_claude
from looper.agents.agent_settings import AgentSettings
from looper.agents.bus import MessageBus
from looper.agents.message import MessageEnvelope, MessageType, planner_address, worker_address
from looper.agents.profile_registry import LegacyProviderSpec, RuntimeSpec
from looper.agents.runtimes import ApiProtocol, AuthKind, Capability, adapter
from looper.agents.runtimes import codex_steer, omp_steer, pi_steer
from looper.agents.tiers import planner as planner_module, worker as worker_module
from looper.agents.tiers.planner import PlanStep, PlannerAgent, TaskPlan
from looper.agents.tiers.planner_protocol import ChatAction, ChatReply
from looper.agents.tiers.worker import WorkerConfig, WorkerPool
from tests.runtime_model_helpers import resolved_profile


_DEFAULT_DELIVERY_TIMEOUT = worker_module.WORKER_INPUT_DELIVERY_TIMEOUT


class Channel:
    send_timeout = 0.01

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.settled = asyncio.Event()
        self.sent: list[str] = []
        self.written: list[str] = []
        self.error: Exception | None = None

    async def wait_input_ready(self) -> None:
        pass

    async def send(self, text: str) -> None:
        self.started.set()
        try:
            self.written.append(text)
            async with asyncio.timeout(self.send_timeout):
                await self.release.wait()
            if self.error:
                raise self.error
            self.sent.append(text)
        finally:
            self.settled.set()


@pytest.fixture(autouse=True)
def budgets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(worker_module, "WORKER_INPUT_DELIVERY_TIMEOUT", 0.03)
    monkeypatch.setattr(planner_module, "WORKER_INPUT_TIMEOUT", 0.5)


@asynccontextmanager
async def delivery(tmp_path: Path):
    bus = MessageBus(runtime_root=tmp_path)
    pool = WorkerPool(config=WorkerConfig(), runtime_root=tmp_path)
    planner = PlannerAgent(task_id="t1", runtime_root=tmp_path)
    step = PlanStep(step_id="s1", index=0, instruction="write it", status="dispatched", worker_id="w1")
    plan = TaskPlan(plan_id="p1", goal_id="", requirement="build it", work_dir=str(tmp_path), steps=[step])
    planner._bus, planner._plan = bus, plan
    channel, ready = Channel(), asyncio.Event()
    pool._inputs["w1"], pool._input_ready["w1"] = channel, ready
    acknowledgements = []
    bus.subscribe_topic(MessageType.WORKER_INPUT_RESULT, acknowledgements.append)
    await pool.start(bus)
    await asyncio.sleep(0)  # register the real bus consumer
    try:
        yield pool, planner, plan, step, channel, ready, acknowledgements
    finally:
        await pool.stop()
        await bus.shutdown()


async def correct(planner: PlannerAgent, plan: TaskPlan, step: PlanStep, caller: str) -> None:
    if caller == "chat":
        await planner._apply_chat(plan, ChatReply("Passing it on.", ChatAction.FORWARD, step.label, "use blue", ()), None)
    else:
        await planner._nudge_worker(plan, step, "use blue")


@pytest.mark.parametrize("caller", ["chat", "supervise"])
@pytest.mark.parametrize("blocked_at", ["readiness", "send"])
def test_expired_input_is_acknowledged_and_never_delivered_later(
    tmp_path: Path, agent_db: Any, caller: str, blocked_at: str,
) -> None:
    async def run() -> None:
        async with delivery(tmp_path) as (_, planner, plan, step, channel, ready, acks):
            if blocked_at == "send":
                ready.set()
            before = asyncio.all_tasks()
            await correct(planner, plan, step, caller)
            assert [ack.payload for ack in acks] == [{"delivered": False}]
            assert step.missed_messages == ["use blue"]
            assert agent_db.store.steps["t1"]["s1"]["missed_messages"] == ["use blue"]
            if blocked_at == "send":
                assert channel.started.is_set() and channel.settled.is_set()
            ready.set()
            channel.release.set()
            await asyncio.sleep(0)
            assert channel.sent == []
            assert not (asyncio.all_tasks() - before), "the bus delivery task survived refusal"
            assert not planner._bus._reply_futures

    asyncio.run(asyncio.wait_for(run(), 2))


@pytest.mark.parametrize("caller", ["chat", "supervise"])
def test_starting_worker_acceptance_after_four_seconds_is_acknowledged_once(
    tmp_path: Path, agent_db: Any, monkeypatch: pytest.MonkeyPatch, caller: str,
) -> None:
    # Scale the production deadline and a runtime's 4s acceptance timeout together.
    monkeypatch.setattr(worker_module, "WORKER_INPUT_DELIVERY_TIMEOUT", _DEFAULT_DELIVERY_TIMEOUT / 5)
    monkeypatch.setattr(planner_module, "WORKER_INPUT_TIMEOUT", 5 / 5)

    async def run() -> None:
        async with delivery(tmp_path) as (_, planner, plan, step, channel, ready, acks):
            channel.send_timeout = 4 / 5
            loop = asyncio.get_running_loop()
            opening = loop.call_later(0.05, ready.set)
            accepting = loop.call_later(0.83, channel.release.set)
            try:
                await planner._save(plan, step)
                await correct(planner, plan, step, caller)
                assert [ack.payload for ack in acks] == [{"delivered": True}]
                assert len(channel.written) == 1 and channel.sent == channel.written
                assert channel.sent[0].endswith("use blue")
                assert step.missed_messages == []
                assert agent_db.store.steps["t1"]["s1"]["missed_messages"] == []
                assert not planner._bus._reply_futures
            finally:
                opening.cancel()
                accepting.cancel()

    asyncio.run(asyncio.wait_for(run(), 2))


@pytest.mark.parametrize("startup_delay, acceptance_timeout", [(0, 0.04), (0.02, 0.02)])
def test_insufficient_acceptance_budget_refuses_before_writing(
    tmp_path: Path, agent_db: Any, startup_delay: float, acceptance_timeout: float,
) -> None:
    async def run() -> None:
        async with delivery(tmp_path) as (_, planner, plan, step, channel, ready, acks):
            channel.send_timeout = acceptance_timeout
            opening = asyncio.get_running_loop().call_later(startup_delay, ready.set)
            try:
                await correct(planner, plan, step, "chat")
                assert [ack.payload for ack in acks] == [{"delivered": False}]
                assert step.missed_messages == ["use blue"]
                assert not channel.started.is_set() and channel.written == []
                channel.release.set()
                await asyncio.sleep(0)
                assert channel.sent == []
            finally:
                opening.cancel()

    asyncio.run(asyncio.wait_for(run(), 2))


def test_readiness_and_send_share_one_budget(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(worker_module, "WORKER_INPUT_DELIVERY_TIMEOUT", 0.06)

    async def run() -> None:
        async with delivery(tmp_path) as (_, planner, _, step, channel, ready, acks):
            loop = asyncio.get_running_loop()
            opening = loop.call_later(0.04, ready.set)
            accepting = loop.call_later(0.08, channel.release.set)
            try:
                assert not await planner._send_worker_input(step, "use blue", "corr")
                assert [ack.payload for ack in acks] == [{"delivered": False}]
                assert channel.sent == []
            finally:
                opening.cancel()
                accepting.cancel()

    asyncio.run(asyncio.wait_for(run(), 2))


@pytest.mark.parametrize("caller", ["chat", "supervise"])
def test_ready_before_deadline_delivers_exactly_once(tmp_path: Path, agent_db: Any, caller: str) -> None:
    async def run() -> None:
        async with delivery(tmp_path) as (_, planner, plan, step, channel, ready, acks):
            request = asyncio.create_task(correct(planner, plan, step, caller))
            await asyncio.sleep(0)
            assert not request.done() and not acks and not channel.sent
            ready.set()
            await channel.started.wait()
            assert not request.done() and not acks  # readiness is not an acknowledgement
            channel.release.set()
            await request
            assert [ack.payload for ack in acks] == [{"delivered": True}]
            assert len(channel.sent) == 1 and channel.sent[0].endswith("use blue")
            assert step.missed_messages == []

    asyncio.run(asyncio.wait_for(run(), 2))


def test_pool_stop_settles_pending_bus_delivery(tmp_path: Path) -> None:
    async def run() -> None:
        async with delivery(tmp_path) as (pool, planner, _, step, channel, ready, acks):
            ready.set()
            request = asyncio.create_task(planner._send_worker_input(step, "use blue", "corr"))
            await channel.started.wait()
            await pool.stop()
            assert not await request
            assert [ack.payload for ack in acks] == [{"delivered": False}]
            assert channel.settled.is_set() and not pool._input_deliveries
            channel.release.set()
            await asyncio.sleep(0)
            assert channel.sent == []

    asyncio.run(asyncio.wait_for(run(), 2))


@pytest.mark.parametrize("state", ["absent", "finished", "blank", "broken_pipe", "reset", "refused"])
def test_unavailable_input_returns_false(tmp_path: Path, state: str) -> None:
    async def run() -> None:
        async with delivery(tmp_path) as (pool, planner, _, step, channel, ready, acks):
            ready.set()
            channel.release.set()
            if state in ("absent", "finished"):
                pool._inputs.pop("w1")
                if state == "absent":
                    pool._input_ready.pop("w1")
            channel.error = {"broken_pipe": BrokenPipeError(), "reset": ConnectionResetError(),
                             "refused": RuntimeError("closed")}.get(state)
            assert not await planner._send_worker_input(step, " " if state == "blank" else "use blue", "corr")
            assert [ack.payload for ack in acks] == [{"delivered": False}]
            assert channel.sent == []

    asyncio.run(asyncio.wait_for(run(), 2))


@pytest.mark.parametrize("blocked_at", ["readiness", "send"])
def test_cancellation_leaves_no_late_send(tmp_path: Path, blocked_at: str) -> None:
    async def run() -> None:
        async with delivery(tmp_path) as (pool, _, _, _, channel, ready, _):
            if blocked_at == "send":
                ready.set()
            task = asyncio.create_task(pool.send_input("w1", "use blue"))
            await asyncio.sleep(0)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            ready.set()
            channel.release.set()
            await asyncio.sleep(0)
            assert channel.sent == [] and task.done()
            if blocked_at == "send":
                assert channel.settled.is_set()

    asyncio.run(asyncio.wait_for(run(), 2))


_STARTING_RUNTIME = r'''
import json, sys, time

print('STARTED', flush=True)

def emit(value):
    print(json.dumps(value), flush=True)

accepted = []
for line in sys.stdin:
    request = json.loads(line)
    with open(LOG, 'a') as log:
        log.write(json.dumps(request) + '\n')
    command = request.get('method', request.get('type'))
    if command == 'initialized':
        continue
    if RUNTIME == 'codex':
        result = {'thread': {'id': 'thread'}} if command == 'thread/start' else (
            {'turn': {'id': 'turn'}} if command == 'turn/start' else {})
        if command == 'turn/steer':
            accepted.append(request['params']['input'][0]['text'])
            time.sleep(ACK_DELAY)
        emit({'id': request['id'], 'result': result})
    else:
        data = {'sessionId': 'session'} if command == 'get_state' else (
            {'disposition': 'started'} if command == 'prompt' else {'disposition': 'queued'})
        if command == 'steer':
            accepted.append(request['message'])
            time.sleep(ACK_DELAY)
        emit({'type': 'response', 'id': request['id'], 'command': command, 'success': True, 'data': data})

report = json.dumps({'status': 'completed', 'summary': json.dumps(accepted), 'changed_files': {}})
if RUNTIME == 'codex':
    emit({'method': 'item/completed', 'params': {'item': {'id': 'answer', 'type': 'agentMessage', 'text': report}}})
    emit({'method': 'turn/completed', 'params': {'turn': {'id': 'turn', 'status': 'completed'}}})
else:
    emit({'type': 'message_end', 'message': {'role': 'assistant', 'model': 'test-model', 'stopReason': 'stop',
          'content': [{'type': 'text', 'text': report}], 'usage': {'input': 1, 'output': 1}}})
    emit({'type': 'agent_settled' if RUNTIME == 'pi' else 'agent_end', 'aborted': False})
'''


@pytest.mark.parametrize("runtime", ["codex", "omp", "pi"])
@pytest.mark.parametrize("caller", ["chat", "supervise"])
@pytest.mark.parametrize("startup", ["within_budget", "insufficient_budget", "past_deadline"])
def test_runtime_handshake_preserves_the_post_write_acceptance_budget(
    tmp_path: Path, agent_db: Any, monkeypatch: pytest.MonkeyPatch, runtime: str, caller: str, startup: str,
) -> None:
    monkeypatch.setattr(worker_module, "WORKER_INPUT_DELIVERY_TIMEOUT", 1.8)
    monkeypatch.setattr(planner_module, "WORKER_INPUT_TIMEOUT", 2.0)
    monkeypatch.setattr(codex_steer, "_REQUEST_TIMEOUT_SEC", 1.6)
    monkeypatch.setattr(omp_steer, "_STEER_TIMEOUT_SEC", 1.6)
    monkeypatch.setattr(pi_steer, "_STEER_TIMEOUT_SEC", 1.6)
    log = tmp_path / "requests.jsonl"
    executable = fake_claude(tmp_path / "runtime", f"RUNTIME = {runtime!r}\nLOG = {str(log)!r}\n"
                            f"ACK_DELAY = {1.54 if startup == 'within_budget' else 0}\n" + _STARTING_RUNTIME)
    runtime_adapter = adapter(runtime)
    profile = resolved_profile("profile", "test", RuntimeSpec("runtime", runtime, runtime, executable,
        frozenset(runtime_adapter.capabilities | {Capability.STEER})),
        LegacyProviderSpec("provider", "test", AuthKind.API_KEY, frozenset({ApiProtocol.OPENAI_RESPONSES}),
                           base_url="http://127.0.0.1:1/v1", api_key="fake-key"), "test-model")
    monkeypatch.setattr(worker_module, "load_agent_settings", AsyncMock(return_value=AgentSettings(profile=profile)))

    async def run() -> None:
        async with delivery(tmp_path) as (pool, planner, plan, step, _, _, acks):
            # Dispatch a real process. Hold its stdout reader after start() has returned,
            # leaving the runtime's real handshake incomplete when the correction arrives.
            pool._inputs.pop("w1")
            pool._input_ready.pop("w1")
            reading, handshake = asyncio.Event(), asyncio.Event()
            factory = runtime_adapter.run_channel

            def channel_factory(*args: Any, **kwargs: Any) -> Any:
                channel = factory(*args, **kwargs)
                events = channel.events

                async def delayed_events():
                    assert await args[0].stdout.readline() == b"STARTED\n"
                    reading.set()
                    await handshake.wait()
                    async for event in events():
                        yield event

                channel.events = delayed_events
                return channel

            monkeypatch.setattr(runtime_adapter, "run_channel", channel_factory)
            results: asyncio.Queue[MessageEnvelope] = asyncio.Queue()
            planner._bus.subscribe_topic(MessageType.WORK_RESULT, results.put_nowait)
            await planner._save(plan, step)
            await planner._bus.publish(MessageEnvelope.create(
                source=planner_address("t1"), target=worker_address("w1"), message_type=MessageType.WORK_ITEM,
                payload={"task_id": "s1", "instruction": "write it", "context": {"work_dir": str(tmp_path)}},
            ))
            await asyncio.wait_for(reading.wait(), 3)
            opening = None
            send_input = pool.send_input

            async def deliver_input(worker_id: str, text: str) -> bool:
                nonlocal opening
                if startup != "past_deadline":
                    opening = asyncio.get_running_loop().call_later(
                        0.12 if startup == "within_budget" else 0.4, handshake.set)
                return await send_input(worker_id, text)

            monkeypatch.setattr(pool, "send_input", deliver_input)
            try:
                await correct(planner, plan, step, caller)
                delivered = startup == "within_budget"
                assert [ack.payload for ack in acks] == [{"delivered": delivered}]
                assert step.missed_messages == ([] if delivered else ["use blue"])
                assert agent_db.store.steps["t1"]["s1"]["missed_messages"] == step.missed_messages
                # Complete the original turn and read back what the process actually accepted,
                # including after a refused correction's handshake eventually finishes.
                handshake.set()
                await pool._inputs["w1"].wait_input_ready()
                process = pool._active["w1"].process
                process.stdin.close()
                result = await asyncio.wait_for(results.get(), 3)
                assert result.payload["status"] == "completed", result.payload
                accepted = json.loads(result.payload["summary"])
                assert len(accepted) == int(delivered)
                if delivered:
                    assert accepted[0].endswith("use blue")
                requests = [json.loads(line) for line in log.read_text().splitlines()]
                writes = [r for r in requests if r.get("method", r.get("type")) in ("turn/steer", "steer")]
                assert len(writes) == int(delivered)
                if delivered:
                    commands = [r.get("method", r.get("type")) for r in requests]
                    assert commands.index("turn/start" if runtime == "codex" else "prompt") < commands.index(
                        "turn/steer" if runtime == "codex" else "steer")
                await process.wait()
                assert not planner._bus._reply_futures
            finally:
                if opening is not None:
                    opening.cancel()

    asyncio.run(asyncio.wait_for(run(), 8))
