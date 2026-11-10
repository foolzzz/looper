"""Workflow notices and delivery records on the normal task/channel paths."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from looper.agents.channels.telegram import TelegramChannelAdapter
from looper.agents.channels.allowlist import SenderAllowlist
from looper.agents.message import MessageEnvelope, MessageType, TASK_ROUTER_ADDRESS
from looper.agents.tiers.manager import ManagerAgent, ManagerConfig
from looper.db import llm_records
from tests.test_planner_agent import FINAL_RESULT, REVIEW_FAIL, REVIEW_PASS, WORK_FAILED, WORK_OK, always, chat, decide, drive, plan, step
from tests.test_review_panel_planner import configure


@pytest.mark.parametrize("notices", [True, False])
def test_plan_and_review_phase_reach_telegram_transport(planner_claude, agent_db, monkeypatch, notices):
    configure(agent_db, monkeypatch, phase_notices=notices)
    claude = planner_claude(plan(step("s1", review=True)), decide("review"), decide("accept"))
    sent = []

    async def scenario(h):
        await h.start()
        assert (await h.finished()).payload["status"] == "completed"
        manager = ManagerAgent(config=ManagerConfig(work_dir=h.work_dir), runtime_root=h.work_dir)
        manager._bus = h.bus
        channel = TelegramChannelAdapter(bot_token="0:synthetic", allowlist=SenderAllowlist(
            "channel-telegram", frozenset({"7"})))
        channel._correlation_chat["corr-1"] = 7

        async def send_message(**kwargs):
            sent.append(kwargs["text"])
            return SimpleNamespace(message_id=len(sent))

        channel._app = SimpleNamespace(bot=SimpleNamespace(send_message=send_message))
        try:
            for status in h.statuses:
                if status.message_type is MessageType.TASK_ACCEPTED and notices:
                    await manager._handle_task_accepted(MessageEnvelope.create(
                        source=TASK_ROUTER_ADDRESS, target=manager.agent_address,
                        message_type=MessageType.TASK_ACCEPTED, payload=status.payload,
                        metadata={"origin_channel_id": "channel-telegram"}))
                    assert h.bus.get_queue("channel-telegram").empty()
                if status.message_type is not MessageType.GOAL_STATUS or status.payload["status"] == "completed":
                    continue
                routed = MessageEnvelope.create(source=TASK_ROUTER_ADDRESS, target=manager.agent_address,
                    message_type=MessageType.GOAL_STATUS, payload=status.payload, correlation_id="corr-1",
                    metadata={"origin_channel_id": "channel-telegram"})
                await manager._handle_goal_status(routed)
                await channel.deliver(h.bus.get_queue("channel-telegram").get_nowait())
        finally:
            await manager.stop()

    drive(claude, always(WORK_OK, REVIEW_PASS), scenario)
    if notices:
        assert sum("已拆成" in text and "s1" in text and "review_a" in text for text in sent) == 1
        assert sum("评审第 1 轮" in text and "blocking" in text for text in sent) == 1
    else:
        assert sent and all(text.startswith("进度更新: ") for text in sent)


@pytest.mark.parametrize("failed", [False, True])
def test_workflow_delivery_appends_process_record(planner_claude, agent_db, monkeypatch, failed):
    configure(agent_db, monkeypatch)
    monkeypatch.setattr(llm_records, "process_record_runs", AsyncMock(return_value=[{
        "role": "worker", "model": "reported-model", "metadata": {
            "profile": {"name": "dev", "runtime": {"type": "codex"}},
            "model_choice": {"model": "requested-model"}, "fallback_from": "first-profile"}}]), raising=False)
    replies = [plan(step("s1")), decide("fail", reason="Stop") if failed else decide("accept")]
    h = drive(planner_claude(*replies), always(WORK_FAILED if failed else WORK_OK))
    summary = h.final().payload["summary"]
    assert "\n\n## Process record\n" in summary
    assert all(text in summary for text in ("Roles", "Reviews", "Verification", "Escalations", "dev",
                                            "reported-model", "requested-model", "first-profile"))
    if not failed:
        assert summary.startswith(FINAL_RESULT + "\n\n## Process record\n")


@pytest.mark.parametrize("failed", [False, True])
def test_task_without_workflow_keeps_delivery_bytes(planner_claude, agent_db, monkeypatch, failed):
    records = AsyncMock(side_effect=AssertionError("legacy delivery must not query workflow records"))
    monkeypatch.setattr(llm_records, "process_record_runs", records, raising=False)
    reply = decide("fail", reason="Stop") if failed else decide("accept")
    h = drive(planner_claude(plan(step("s1")), reply), always(WORK_FAILED if failed else WORK_OK))
    expected = ("## Why it ended\nStop\n\n## Done\n- nothing\n\n## Not done\n"
                "- s1 (failed): failed: tests fail") if failed else FINAL_RESULT
    assert h.final().payload["summary"] == expected
    assert not records.called


@pytest.mark.parametrize("notices", [True, False])
def test_verification_start_and_result_notices(planner_claude, agent_db, monkeypatch, notices):
    from tests.test_workflow_rules import configure as rules, VERIFY

    rules(agent_db, monkeypatch, phase_notices=notices)
    claude = planner_claude(plan(step("s1"), {**step("v1", "s1"), "role": VERIFY}),
                            decide("accept"), decide("accept"))
    h = drive(claude, always({**WORK_OK, "changed_files": {}, "verification": {"head": "abc123", "git": True}}))
    texts = [e.payload["phase_text"] for e in h.statuses if "phase_text" in e.payload]
    assert sum("验证第 1 轮（v1）：开始" in text for text in texts) == int(notices)
    assert sum("验证第 1 轮（v1）：通过" in text for text in texts) == int(notices)
    assert "HEAD abc123" in h.final().payload["summary"]


def test_exhaustion_emits_one_escalation_notice(planner_claude, agent_db, monkeypatch):
    from tests.test_workflow_rules import configure as rules, GATE

    rules(agent_db, monkeypatch, False, phase_notices=True, max_step_retries=0, escalate_on_exhaustion=True)
    claude = planner_claude(plan(step("s1")),
        decide("replan", steps=[GATE, step("s2", "g1")], reason="Ask before continuing"))

    async def scenario(h):
        await h.start()
        await h.until(lambda: h.planner._plan and h.planner._plan.waiting_gates())

    h = drive(claude, always(WORK_FAILED), scenario)
    texts = [e.payload["phase_text"] for e in h.statuses if "phase_text" in e.payload]
    assert sum("s1 已升级" in text and "等你决定" in text for text in texts) == 1


def test_blocked_condition_emits_one_notice(planner_claude, agent_db, monkeypatch):
    from tests.test_workflow_rules import configure as rules, GATE

    rules(agent_db, monkeypatch, False, phase_notices=True, dependency_conditions=True)
    claude = planner_claude(plan(step("s1"), GATE,
        {**step("s2", "g1"), "ready_when": {"step": "g1", "on": "passed"}}),
        decide("accept", gates=[{"id": "g1", "action": "cancel", "resolution": "Source unavailable"}]),
        chat("Discard the blocked step."))
    h = drive(claude, always(WORK_OK))
    texts = [e.payload["phase_text"] for e in h.statuses if "phase_text" in e.payload]
    assert sum("s2 的开始条件无法满足" in text and "g1" in text for text in texts) == 1


def test_workflow_without_panel_preserves_every_review_round(planner_claude, agent_db, monkeypatch):
    configure(agent_db, monkeypatch, review_panel=None, phase_notices=True)
    claude = planner_claude(plan(step("s1", review=True)), decide("review"),
        decide("retry", feedback="Add the missing tests"), decide("review"), decide("accept"))
    verdicts = iter([REVIEW_FAIL, REVIEW_PASS])
    h = drive(claude, lambda e: next(verdicts) if e.message_type is MessageType.VERIFY_REQUEST else WORK_OK)
    summary = h.final().payload["summary"]
    assert "s1 第 1 轮 / builtin: 失败, 1 blocking" in summary
    assert "s1 第 2 轮 / builtin: 通过, 0 blocking" in summary


def test_unavailable_runtime_records_are_explicit_without_losing_delivery(planner_claude, agent_db, monkeypatch):
    configure(agent_db, monkeypatch)
    monkeypatch.setattr(llm_records, "process_record_runs", AsyncMock(side_effect=OSError("Unavailable")))
    h = drive(planner_claude(plan(step("s1")), decide("accept")), always(WORK_OK))
    assert h.final().payload["status"] == "completed"
    assert "运行记录读取失败" in h.final().payload["summary"]
