"""Chatting with a running task's Planner from the web Chat page."""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from typing import Any

import pytest

from loopx.agents.bus import MessageBus
from loopx.agents.channels.web import WebChannelAdapter
from loopx.agents.message import (
    MessageEnvelope,
    MessageType,
    channel_address,
    planner_address,
    worker_address,
)
from loopx.agents.tiers import planner as planner_module
from loopx.agents.tiers.planner import PlanStep, PlannerAgent, TaskPlan
from loopx.agents.tiers.task import Task, TaskRouter

WEB = channel_address("web")


def _user_message(task_id: str, text: str) -> MessageEnvelope:
    return MessageEnvelope.create(
        source=WEB,
        target=planner_address(task_id),
        message_type=MessageType.USER_MESSAGE,
        payload={"text": text},
        correlation_id="corr-chat",
        metadata={"origin_channel_id": WEB.agent_id},
    )


def _run_planner(plan: TaskPlan | None, *envelopes: MessageEnvelope) -> tuple[PlannerAgent, list[MessageEnvelope]]:
    """Feed envelopes to a Planner and return everything it published."""
    sent: list[MessageEnvelope] = []

    async def _run() -> PlannerAgent:
        with tempfile.TemporaryDirectory() as tmp:
            bus = MessageBus(runtime_root=Path(tmp))

            async def _record(envelope: MessageEnvelope) -> None:
                sent.append(envelope)

            bus.publish = _record  # type: ignore[method-assign]
            # Nothing answers WORKER_INPUT here, so forwarding gives up quickly.
            planner_module.WORKER_INPUT_TIMEOUT = 0.05
            planner = PlannerAgent(task_id="t1", runtime_root=Path(tmp))
            planner._bus = bus
            planner._plan = plan
            for envelope in envelopes:
                await planner._handle(envelope)
            await bus.shutdown()
            return planner

    return asyncio.run(_run()), sent


def _executing_plan(*steps: PlanStep) -> TaskPlan:
    plan = TaskPlan(plan_id="p1", goal_id="", requirement="build it", work_dir="/tmp/proj")
    plan.status = "executing"
    plan.steps = list(steps)
    return plan


def _replies(sent: list[MessageEnvelope]) -> list[MessageEnvelope]:
    return [e for e in sent if e.message_type == MessageType.AGENT_REPLY]


def _work_items(sent: list[MessageEnvelope]) -> list[MessageEnvelope]:
    return [e for e in sent if e.message_type == MessageType.WORK_ITEM]


class TestPlannerUserMessage:
    def test_instruction_queues_after_running_steps(self) -> None:
        running = PlanStep(step_id="s1", index=0, instruction="build it", status="dispatched")
        planner, sent = _run_planner(_executing_plan(running), _user_message("t1", "also add tests"))

        steps = planner._plan.steps  # type: ignore[union-attr]
        assert [s.instruction for s in steps] == ["build it", "also add tests"]
        added = steps[1]
        assert added.depends_on == ["s1"]
        assert added.status == "pending"
        assert _work_items(sent) == []

        (reply,) = _replies(sent)
        assert reply.target == WEB
        assert reply.correlation_id == "corr-chat"
        assert "2" in reply.payload["text"]

    def test_queued_instruction_runs_when_previous_step_finishes(self) -> None:
        running = PlanStep(step_id="s1", index=0, instruction="build it", status="dispatched")
        result = MessageEnvelope.create(
            source=WEB,  # source is irrelevant to the planner
            target=planner_address("t1"),
            message_type=MessageType.WORK_RESULT,
            payload={"task_id": "s1", "status": "completed"},
        )
        planner, sent = _run_planner(
            _executing_plan(running), _user_message("t1", "also add tests"), result,
        )

        (item,) = _work_items(sent)
        assert item.payload["instruction"] == "also add tests"
        assert item.payload["context"] == {"work_dir": "/tmp/proj"}
        assert planner._plan.steps[1].status == "dispatched"  # type: ignore[union-attr]

    def test_instruction_dispatches_at_once_when_nothing_is_running(self) -> None:
        done = PlanStep(step_id="s1", index=0, instruction="build it", status="verified")
        planner, sent = _run_planner(_executing_plan(done), _user_message("t1", "tidy up"))

        (item,) = _work_items(sent)
        assert item.payload["instruction"] == "tidy up"
        assert planner._plan.steps[1].depends_on == []  # type: ignore[union-attr]
        assert len(_replies(sent)) == 1

    def test_finished_plan_rejects_instruction(self) -> None:
        plan = _executing_plan(PlanStep(step_id="s1", index=0, instruction="x", status="verified"))
        plan.status = "completed"
        planner, sent = _run_planner(plan, _user_message("t1", "one more thing"))

        assert len(planner._plan.steps) == 1  # type: ignore[union-attr]
        assert _work_items(sent) == []
        (reply,) = _replies(sent)
        assert "completed" in reply.payload["text"]

    def test_message_before_plan_exists_is_answered(self) -> None:
        planner, sent = _run_planner(None, _user_message("t1", "hello"))

        assert planner._plan is None
        assert len(_replies(sent)) == 1
        assert _work_items(sent) == []

    def test_depends_on_survives_plan_persistence(self) -> None:
        async def _run() -> list[str]:
            with tempfile.TemporaryDirectory() as tmp:
                first = PlannerAgent(task_id="t1", runtime_root=Path(tmp))
                first._save_plan(_executing_plan(
                    PlanStep(step_id="s1", index=0, instruction="a", status="dispatched"),
                    PlanStep(step_id="s2", index=1, instruction="b", depends_on=["s1"]),
                ))
                second = PlannerAgent(task_id="t1", runtime_root=Path(tmp))
                second._load_plan()
                return second._plan.steps[1].depends_on  # type: ignore[union-attr]

        assert asyncio.run(_run()) == ["s1"]


class TestChatTargets:
    def test_running_planners_lists_live_instances_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            router = TaskRouter(runtime_root=Path(tmp))
            live = Task(task_id="t1", requirement="build it", goal_id="", work_dir="",
                        status="running", planner_id="planner-t1")
            queued = Task(task_id="t2", requirement="later", goal_id="", work_dir="",
                          status="pending")
            router._tasks = {"t1": live, "t2": queued}
            router._planners = {"t1": PlannerAgent(task_id="t1", runtime_root=Path(tmp))}

            assert router.running_planners() == [{
                "task_id": "t1",
                "planner_id": "planner-t1",
                "requirement": "build it",
                "status": "running",
            }]
            assert router.has_planner("t1")
            assert not router.has_planner("t2")

    def test_web_message_can_target_a_planner(self) -> None:
        async def _run() -> MessageEnvelope:
            with tempfile.TemporaryDirectory() as tmp:
                bus = MessageBus(runtime_root=Path(tmp))
                adapter = WebChannelAdapter()
                await adapter.start(bus)
                await adapter.handle_user_message(
                    text="also add tests", session_id="s1", target=planner_address("t1"),
                )
                envelope = await bus.get_queue("planner-t1").get()
                assert bus.get_queue("manager").empty()
                await adapter.stop()
                await bus.shutdown()
                return envelope

        envelope = asyncio.run(_run())
        assert envelope.message_type == MessageType.USER_MESSAGE
        assert envelope.payload["text"] == "also add tests"
        assert envelope.source == WEB


class TestResolveChatTarget:
    def _router(self, tmp: str) -> TaskRouter:
        router = TaskRouter(runtime_root=Path(tmp))
        router._planners = {"t1": PlannerAgent(task_id="t1", runtime_root=Path(tmp))}
        return router

    @pytest.mark.parametrize("raw", [None, {}, {"kind": "manager"}])
    def test_manager_is_the_default(self, raw: Any) -> None:
        from loopx.agents.chat_targets import resolve_chat_target
        from loopx.agents.message import MANAGER_ADDRESS
        with tempfile.TemporaryDirectory() as tmp:
            assert resolve_chat_target(raw, self._router(tmp)) == MANAGER_ADDRESS

    def test_running_planner(self) -> None:
        from loopx.agents.chat_targets import resolve_chat_target
        with tempfile.TemporaryDirectory() as tmp:
            target = {"kind": "planner", "task_id": "t1"}
            assert resolve_chat_target(target, self._router(tmp)) == planner_address("t1")

    @pytest.mark.parametrize(
        ("raw", "status"),
        [
            ({"kind": "planner", "task_id": "gone"}, "409 Conflict"),
            ({"kind": "planner"}, "409 Conflict"),
            ({"kind": "worker", "task_id": "t1"}, "400 Bad Request"),
            ("planner", "400 Bad Request"),
        ],
    )
    def test_rejected_targets(self, raw: Any, status: str) -> None:
        from loopx.agents.chat_targets import ChatTargetError, resolve_chat_target
        with tempfile.TemporaryDirectory() as tmp:
            with pytest.raises(ChatTargetError) as excinfo:
                resolve_chat_target(raw, self._router(tmp))
            assert excinfo.value.status == status



class TestPlannerForwardsToRunningWorker:
    def _run(self, delivered: bool) -> tuple[PlannerAgent, list[MessageEnvelope], list[MessageEnvelope]]:
        forwarded: list[MessageEnvelope] = []

        async def _run() -> tuple[PlannerAgent, list[MessageEnvelope]]:
            with tempfile.TemporaryDirectory() as tmp:
                bus = MessageBus(runtime_root=Path(tmp))

                def worker_pool(envelope: MessageEnvelope) -> bool:
                    forwarded.append(envelope)
                    asyncio.get_event_loop().create_task(bus.publish(MessageEnvelope.create(
                        source=envelope.target, target=envelope.source,
                        message_type=MessageType.WORKER_INPUT_RESULT,
                        payload={"delivered": delivered}, reply_to=envelope.message_id,
                    )))
                    return True

                bus.subscribe_topic(MessageType.WORKER_INPUT, worker_pool, consumes=True)
                planner = PlannerAgent(task_id="t1", runtime_root=Path(tmp))
                planner._bus = bus
                planner._plan = _executing_plan(
                    PlanStep(step_id="s1", index=0, instruction="build it", status="dispatched", worker_id="worker-9"),
                )
                await planner._handle(_user_message("t1", "use a red background"))
                replies = []
                queue = bus.get_queue(WEB.agent_id)
                while not queue.empty():
                    replies.append(queue.get_nowait())
                await bus.shutdown()
                return planner, replies

        planner, replies = asyncio.run(_run())
        return planner, replies, forwarded

    def test_message_goes_to_the_running_worker(self) -> None:
        planner, replies, forwarded = self._run(delivered=True)
        (sent,) = forwarded
        assert sent.target == worker_address("worker-9")
        assert "use a red background" in sent.payload["text"]
        assert len(planner._plan.steps) == 1  # no new step  # type: ignore[union-attr]
        (reply,) = [r for r in replies if r.message_type == MessageType.AGENT_REPLY]
        assert "worker-9" in reply.payload["text"]

    def test_undelivered_message_becomes_a_step(self) -> None:
        planner, replies, _ = self._run(delivered=False)
        steps = planner._plan.steps  # type: ignore[union-attr]
        assert [s.instruction for s in steps] == ["build it", "use a red background"]
        (reply,) = [r for r in replies if r.message_type == MessageType.AGENT_REPLY]
        assert "第 2 步" in reply.payload["text"]
