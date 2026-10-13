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
)
from loopx.agents.tiers import planner as planner_module
from loopx.agents.tiers.planner import PlanStep, PlannerAgent, TaskPlan
from loopx.agents.tiers.task import Task, TaskRouter
from tests.test_planner_agent import WORK_OK, Harness, activity, chat, decide, drive, hold_first, plan, step

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
            live = Task(task_id="t1", requirement="build it", goal_id="", work_dir="", project_id="proj-1",
                        status="running", planner_id="planner-t1")
            queued = Task(task_id="t2", requirement="later", goal_id="", work_dir="", project_id="proj-1",
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



@pytest.mark.usefixtures("agent_db")
class TestPlannerChatThroughClaude:
    """A chat message goes to the Planner's Claude, which replies and takes at most one action."""

    def test_reply_and_forward_to_the_running_worker(self, planner_claude: Any, agent_db: Any) -> None:
        claude = planner_claude(
            plan(step("s1")),
            chat("好的，我让 Worker 改成红色背景。", "forward", step="s1", text="use a red background"),
            decide("accept"),
        )

        async def scenario(h: Harness) -> str:
            await h.start()
            await h.until(lambda: h.held)  # s1's Worker is running
            answer = await h.say("背景改成红色")
            await h.release(WORK_OK)
            await h.finished()
            return answer

        h = drive(claude, lambda envelope: None, scenario)
        (forwarded,) = h.forwarded
        assert forwarded.target == h.work_items[0].target
        assert forwarded.payload["text"].endswith("use a red background")
        (answer,) = [r for r in h.replies if r.target == WEB]
        assert answer.payload["text"] == "好的，我让 Worker 改成红色背景。"
        assert answer.correlation_id == "corr-chat"
        assert len(h.work_items) == 1  # no step was added
        turn = claude.turns()[1]["text"]
        assert turn.startswith("[CHAT]") and "背景改成红色" in turn
        assert f"Running now: s1 ({h.work_items[0].target.agent_id})" in turn
        assert any("User message passed to" in line for line in activity(agent_db))

    def test_reply_and_added_steps(self, planner_claude: Any) -> None:
        claude = planner_claude(
            plan(step("s1")),
            chat("会在第 1 步之后加上测试。", "add_steps", steps=[step("t1", "s1")]),
            decide("accept"),
            decide("accept"),
        )

        async def scenario(h: Harness) -> None:
            await h.start()
            await h.until(lambda: h.held)
            assert await h.say("也加上测试") == "会在第 1 步之后加上测试。"
            assert len(h.work_items) == 1  # the new step waits for s1
            await h.release(WORK_OK)
            await h.finished()

        h = drive(claude, hold_first(), scenario)
        assert [item.payload["instruction"] for item in h.work_items] == ["do s1", "do t1"]
        assert h.final().payload["status"] == "completed"  # type: ignore[union-attr]

    def test_forward_that_arrives_too_late_reaches_the_decision(self, planner_claude: Any) -> None:
        claude = planner_claude(
            plan(step("s1")),
            chat("我转给它。", "forward", step="s1", text="also log errors"),
            decide("retry", feedback="also log errors"),
            decide("accept"),
        )

        async def scenario(h: Harness) -> str:
            await h.start()
            await h.until(lambda: h.held)
            running = h.held.pop(0)  # its Worker has finished but the result is not in yet
            answer = await h.say("记得打日志")
            await h.answer(running, WORK_OK)
            await h.finished()
            return answer

        h = drive(claude, hold_first(), scenario)
        reply_text = [r.payload["text"] for r in h.replies if r.target == WEB][0]
        assert reply_text.startswith("我转给它。") and "已经结束" in reply_text
        decision = claude.turns()[2]["text"]
        assert "arrived after the Worker finished" in decision and "also log errors" in decision
        assert "also log errors" in h.work_items[1].payload["instruction"]

    def test_plain_reply_takes_no_action(self, planner_claude: Any) -> None:
        claude = planner_claude(plan(step("s1")), chat("第 1 步正在执行。"), decide("accept"))

        async def scenario(h: Harness) -> str:
            await h.start()
            await h.until(lambda: h.held)
            answer = await h.say("现在到哪一步了？")
            await h.release(WORK_OK)
            await h.finished()
            return answer

        h = drive(claude, lambda envelope: None, scenario)
        assert [r.payload["text"] for r in h.replies if r.target == WEB] == ["第 1 步正在执行。"]
        assert h.forwarded == [] and len(h.work_items) == 1

    def test_invalid_action_gets_a_repair_turn(self, planner_claude: Any) -> None:
        claude = planner_claude(
            plan(step("s1")),
            chat("转给 s2。", "forward", step="s2", text="x"),  # s2 does not exist
            chat("好的。"),
            decide("accept"),
        )

        async def scenario(h: Harness) -> str:
            await h.start()
            await h.until(lambda: h.held)
            answer = await h.say("hi")
            await h.release(WORK_OK)
            await h.finished()
            return answer

        h = drive(claude, lambda envelope: None, scenario)
        assert [r.payload["text"] for r in h.replies if r.target == WEB] == ["好的。"]
        repair = claude.turns()[2]["text"]
        assert repair.startswith("[CHAT] Your last reply broke the reply contract: step 's2' has no running Worker")

    def test_failed_chat_turn_is_answered(self, planner_claude: Any) -> None:
        claude = planner_claude(plan(step("s1")), "<error>", decide("accept"))

        async def scenario(h: Harness) -> str:
            await h.start()
            await h.until(lambda: h.held)
            answer = await h.say("hi")
            await h.release(WORK_OK)
            await h.finished()
            return answer

        h = drive(claude, lambda envelope: None, scenario)
        (answer,) = [r.payload["text"] for r in h.replies if r.target == WEB]
        assert answer.startswith("Planner 出错") and "error_during_execution" in answer
        assert h.final().payload["status"] == "completed"  # type: ignore[union-attr]
