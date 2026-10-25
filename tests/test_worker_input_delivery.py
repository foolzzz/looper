"""Worker input must settle before the Planner gives up on its acknowledgement."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest

from looper.agents.bus import MessageBus
from looper.agents.message import MessageType
from looper.agents.tiers import planner as planner_module, worker as worker_module
from looper.agents.tiers.planner import PlanStep, PlannerAgent, TaskPlan
from looper.agents.tiers.planner_protocol import ChatAction, ChatReply
from looper.agents.tiers.worker import WorkerConfig, WorkerPool


class Channel:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.settled = asyncio.Event()
        self.sent: list[str] = []
        self.error: Exception | None = None

    async def send(self, text: str) -> None:
        self.started.set()
        try:
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
