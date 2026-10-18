"""Chatting with a running task's Planner from the web Chat page."""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest

from looper.agents.bus import MessageBus
from looper.agents.channels.web import WebChannelAdapter
from looper.agents.message import (
    MANAGER_ADDRESS,
    MessageEnvelope,
    MessageType,
    channel_address,
    planner_address,
)
from looper.agents.tiers import planner as planner_module
from looper.agents.tiers.planner import PlanStep, PlannerAgent, TaskPlan
from looper.agents.tiers.task import TaskRouter
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
    @pytest.mark.parametrize(("status", "expected"), [("completed", "completed"), (None, "规划中")])
    def test_finished_plan_rejects_instruction(self, status: str | None, expected: str) -> None:
        """A finished task, or one with no plan yet, answers without touching the plan."""
        plan = None
        if status is not None:
            plan = _executing_plan(PlanStep(step_id="s1", index=0, instruction="x", status="verified"))
            plan.status = status
        planner, sent = _run_planner(plan, _user_message("t1", "one more thing"))

        assert planner._plan is plan and (plan is None or len(plan.steps) == 1)
        assert _work_items(sent) == []
        (reply,) = _replies(sent)
        assert expected in reply.payload["text"]


class TestChatTargets:
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

    @pytest.mark.parametrize(("raw", "expected"), [
        (None, MANAGER_ADDRESS),
        ({}, MANAGER_ADDRESS),
        ({"kind": "manager"}, MANAGER_ADDRESS),
        ({"kind": "planner", "task_id": "t1"}, planner_address("t1")),  # a running Planner
    ])
    def test_manager_is_the_default(self, raw: Any, expected: Any) -> None:
        from looper.agents.chat_targets import resolve_chat_target
        with tempfile.TemporaryDirectory() as tmp:
            assert resolve_chat_target(raw, self._router(tmp)) == expected

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
        from looper.agents.chat_targets import ChatTargetError, resolve_chat_target
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
        assert h.work_items[1].payload["feedback"] == ["also log errors"]

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
        assert h.forwarded == [] and len(h.work_items) == 1  # the plain reply took no action
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


class TestPlannerRoomDelivery:
    def test_reply_failure_keeps_messages_and_plan_for_retry(self, tmp_path: Path,
                                                            monkeypatch: pytest.MonkeyPatch) -> None:
        from looper.agents.tiers.planner_protocol import ChatAction, ChatReply

        async def run() -> None:
            rows = [{"id": 1, "sender_seat": "worker@s1", "kind": "question", "body": "which interface?"}]
            pending = AsyncMock(side_effect=lambda task_id: list(rows))
            delivered = AsyncMock()
            room = SimpleNamespace(planner_reply=AsyncMock(side_effect=[RuntimeError("storage unavailable"), None]),
                                   wake_planner_after=Mock())
            planner = PlannerAgent(task_id="t1", runtime_root=tmp_path, ops=SimpleNamespace(room=room))
            planner._activity = AsyncMock()
            planner._ask = AsyncMock(return_value=ChatReply("use v2", ChatAction.FORWARD, "s1", "use v2", ()))
            planner._apply_gate_changes = AsyncMock(return_value=False)
            planner._apply_chat = AsyncMock()
            monkeypatch.setattr(planner_module.db_room, "planner_pending", pending)
            monkeypatch.setattr(planner_module.db_room, "planner_delivered", delivered)
            plan = _executing_plan()

            await planner._deliver_room_messages(plan)
            delivered.assert_not_awaited()
            planner._apply_gate_changes.assert_not_awaited()
            planner._apply_chat.assert_not_awaited()
            assert planner._room_failed[0] == 1
            room.wake_planner_after.assert_called_once_with("t1", planner_module.ROOM_RETRY_SEC)
            assert planner._activity.await_args.args[0] == "error"
            await planner._deliver_room_messages(plan)
            assert planner._ask.await_count == 1
            assert room.wake_planner_after.call_count == 2
            assert 0 < room.wake_planner_after.call_args.args[1] <= planner_module.ROOM_RETRY_SEC

            rows.append({**rows[0], "id": 2})
            await planner._deliver_room_messages(plan)
            delivered.assert_awaited_once_with("t1", [1, 2])
            planner._apply_gate_changes.assert_awaited_once()
            planner._apply_chat.assert_awaited_once()
            assert planner._room_failed == (0, 0.0)

        asyncio.run(run())

    def test_failed_turn_schedules_retry_without_another_message(self, tmp_path: Path,
                                                               monkeypatch: pytest.MonkeyPatch) -> None:
        async def run() -> None:
            row = {"id": 1, "sender_seat": "worker@s1", "kind": "question", "body": "which interface?"}
            room = SimpleNamespace(planner_reply=AsyncMock(), wake_planner_after=Mock())
            planner = PlannerAgent(task_id="t1", runtime_root=tmp_path, ops=SimpleNamespace(room=room))
            planner._activity = AsyncMock()
            planner._ask = AsyncMock(side_effect=planner_module.PlannerTurnError("bad reply"))
            delivered = AsyncMock()
            monkeypatch.setattr(planner_module.db_room, "planner_pending", AsyncMock(return_value=[row]))
            monkeypatch.setattr(planner_module.db_room, "planner_delivered", delivered)
            monkeypatch.setattr(planner_module, "ROOM_RETRY_SEC", 1000)

            await planner._deliver_room_messages(_executing_plan())
            room.wake_planner_after.assert_called_once_with("t1", 1000)
            await planner._deliver_room_messages(_executing_plan())
            assert planner._ask.await_count == 1
            assert room.wake_planner_after.call_count == 2
            assert 0 < room.wake_planner_after.call_args.args[1] <= 1000
            delivered.assert_not_awaited()
            room.planner_reply.assert_not_awaited()

            planner._room_failed = (1, planner_module.time.monotonic() - 2000)
            await planner._deliver_room_messages(_executing_plan())
            assert planner._ask.await_count == 2
            assert room.wake_planner_after.call_args.args == ("t1", 1000)

        asyncio.run(run())

    def test_room_batch_budget_leaves_unshown_messages_pending(self, tmp_path: Path,
                                                              monkeypatch: pytest.MonkeyPatch) -> None:
        from looper.agents.room import REF_MAX_CHARS
        from looper.agents.tiers.planner_protocol import ChatAction, ChatReply

        async def run() -> None:
            rows = [{"id": i, "sender_seat": "worker@s1", "kind": "question", "body": "x" * 1500,
                     "refs": [{"file": "f" * 1000}]} for i in range(1, 15)]
            remaining = list(rows)

            async def delivered(task_id: str, ids: list[int]) -> None:
                remaining[:] = [r for r in remaining if r["id"] not in ids]

            room = SimpleNamespace(planner_reply=AsyncMock(return_value={"id": 30}), wake_planner_after=Mock())
            planner = PlannerAgent(task_id="t1", runtime_root=tmp_path, ops=SimpleNamespace(room=room))
            planner._activity = AsyncMock()
            planner._ask = AsyncMock(return_value=ChatReply("use v2", ChatAction.NONE, "", "", ()))
            planner._apply_gate_changes = AsyncMock(return_value=False)
            monkeypatch.setattr(planner_module.db_room, "planner_pending",
                                AsyncMock(side_effect=lambda task_id: list(remaining)))
            monkeypatch.setattr(planner_module.db_room, "planner_delivered", delivered)
            plan = _executing_plan(*[
                PlanStep(step_id=f"s{i}", index=i, instruction="work " * 100) for i in range(50)
            ])

            await planner._deliver_room_messages(plan)
            message = planner._ask.await_args.args[1]
            shown = room.planner_reply.await_args.args[2]
            assert 0 < len(shown) < len(rows)
            assert shown == rows[:len(shown)]
            assert remaining == rows[len(shown):]
            assert len(message) <= planner_module.ROOM_BATCH_MAX_CHARS
            assert "f" * (REF_MAX_CHARS + 1) not in message
            assert f"{len(remaining)} more message(s) wait for the next turn" in message
            assert f"#{remaining[0]['id']} from " not in message
            room.wake_planner_after.assert_called_once_with("t1", 0)

            while remaining:
                await planner._deliver_room_messages(plan)
            assert [r["id"] for call in room.planner_reply.await_args_list for r in call.args[2]] == \
                [r["id"] for r in rows]

        asyncio.run(run())
