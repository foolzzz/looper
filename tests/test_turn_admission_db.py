"""Real Manager entries wait before records and preserve code-only account notices."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import json
from unittest.mock import AsyncMock

import pytest

from looper.agents import admission, chat_settings, completion_events, lanes_settings, session_routing
from looper.agents.channels.web import WebChannelAdapter
from looper.agents.lanes import Lane
from looper.agents.message import MANAGER_ADDRESS, TASK_ROUTER_ADDRESS, MessageEnvelope, MessageType, channel_address
from looper.agents.runtimes import adapter
from looper.agents.runtimes.api.session import ApiSession
from looper.agents.runtimes.omp import ResidentOmp
from looper.db import provider_limits, projects, tasks
from tests.api_manager_support import USAGE, _manager, _message, _replies
from tests.db_support import DSN, run_db, until, v1_api
from tests.fake_model_api import ResponseScript, text_events

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")
PROVIDER = "prov-api-test"


def _controller(monkeypatch):
    controller = admission.Controller()
    controller.active = True
    monkeypatch.setattr(admission, "_controller", controller)
    for name in ("slot", "transition", "wake", "snapshot", "register", "release"):
        monkeypatch.setattr(admission, name, getattr(controller, name))
    monkeypatch.setattr(admission, "_freeze_alert", AsyncMock())
    monkeypatch.setattr(lanes_settings, "enabled", lambda: True)
    monkeypatch.setattr(session_routing, "routes", lambda _: False)
    return controller


def _script(text):
    return ResponseScript(events=text_events(text, usage=USAGE))


def test_frozen_web_turn_has_one_phase_no_record_then_clear_answers_and_history_excludes_notice(
    monkeypatch, tmp_path,
):
    async def run(pool):
        c = _controller(monkeypatch)
        async with _manager(pool, tmp_path, monkeypatch, [_script("Account recovered.")]) as chat:
            await pool.execute("UPDATE llm_providers SET max_concurrency=1 WHERE id=$1", PROVIDER)
            reset = datetime.now(timezone.utc) + timedelta(hours=1)
            await provider_limits.upsert(PROVIDER, "seven_day", {
                "status": "rejected", "source": "claude_stream", "observed_at": datetime.now(timezone.utc),
                "resets_at": reset,
            }, pool=pool)
            await c.transition(PROVIDER, admission.QuotaFailure(reset, None))
            adapter = WebChannelAdapter(port=0)
            await chat.bus.start()
            await chat.manager.start(chat.bus)
            await adapter.start(chat.bus)
            try:
                result = await adapter.handle_user_message(text="Keep this question.",
                    session_id="admission-web-chat", message_id="synthetic-web-message")
                queue = adapter._sse_queues[result["correlation_id"]]
                await until(lambda: bool(c.waiters) and any("额度已用完" in str(item)
                    for item in queue._queue), "frozen phase reached the real web adapter")
                for _ in range(3):
                    await c.wake()
                    await asyncio.sleep(0)
                frozen = [item for item in queue._queue if "额度已用完" in str(item)]
                assert len(frozen) == 1 and frozen[0]["kind"] == "agent.phase"
                assert "seven_day" in frozen[0]["payload"]["label"]
                assert chat.model.requests == []
                assert await pool.fetchval("SELECT count(*) FROM llm_records") == 0
                assert await pool.fetchval("SELECT count(*) FROM chat_messages WHERE role='assistant'") == 0
                async with v1_api(tmp_path / "clear-api", pool) as call:
                    status, _ = await call("providers.limits_clear", {"provider_id": PROVIDER})
                    assert status == 200
                await until(lambda: any(item["kind"] == "answer.final" for item in queue._queue),
                            "clear released the waiting message")
                assert len(chat.model.requests) == 1
                assert await pool.fetchval("SELECT count(*) FROM llm_records") == 1
                assert await pool.fetchval("SELECT content FROM chat_messages WHERE role='assistant'") == "Account recovered."
                brief = await chat.manager._recent_conversations(_message("Next question."), "Next question.")
                assert "Account recovered." in "\n".join(brief)
                assert "额度已用完" not in "\n".join(brief)
                record = await pool.fetchrow("SELECT input_text, metadata FROM llm_records")
                assert "额度已用完" not in record["input_text"] + record["metadata"]
                assert json.loads(record["metadata"])["admission_lane"] == Lane.INTERACTIVE.value
            finally:
                await adapter.stop()
                await c.stop()

    run_db(run, monkeypatch, timeout=45)


@pytest.mark.parametrize("can_interrupt", [False, True])
@pytest.mark.parametrize("fleet", [False, True])
def test_stop_of_a_waiting_user_turn_leaves_no_record_waiter_or_grant(monkeypatch, tmp_path, can_interrupt, fleet):
    monkeypatch.setattr(chat_settings, "_stop_enabled", True)
    from looper.agents.tiers.manager import ManagerAgent
    monkeypatch.setattr(ManagerAgent, "can_interrupt", property(lambda self: can_interrupt))

    async def run(pool):
        c = _controller(monkeypatch)
        async with _manager(pool, tmp_path, monkeypatch, []) as chat:
            if fleet:
                from looper.agents.managers import ManagerFleet
                # The fleet's chat-stop path must use the same waiting-turn rule.
                owner = object.__new__(ManagerFleet)
                monkeypatch.setattr(owner, "agents", lambda: [chat.manager])
                chat.manager.stop_chat = owner.stop_chat
            await pool.execute("UPDATE llm_providers SET max_concurrency=1 WHERE id=$1", PROVIDER)
            async with c.slot(PROVIDER, Lane.USER_TASK, owner="synthetic-worker"):
                await chat.bus.start()
                await chat.manager.start(chat.bus)
                message = _message("Cancel while waiting.")
                await chat.bus.publish(message)
                await until(lambda: len(c.waiters) == 1, "manager queued behind worker")
                await chat.bus.publish(_message("/stop", index=1))
                await until(lambda: not c.waiters and not chat.manager._turns.live(), "stop cleaned the pending acquire")
                assert chat.model.requests == []
                assert await pool.fetchval("SELECT count(*) FROM llm_records") == 0
                assert list(c.running) == ["synthetic-worker"]
                assert (await c.snapshot(PROVIDER))["waiting"]["interactive"] == 0
            assert not c.running

    run_db(run, monkeypatch, timeout=35)


def test_lark_freeze_is_one_progress_message_and_is_absent_from_resume_history(
    monkeypatch, tmp_path, lark_cli_stand_in,
):
    from looper.agents.channels.allowlist import SenderAllowlist
    from looper.agents.channels.chat_history import record_reply
    from looper.agents.channels.lark import LarkChannelAdapter
    from looper.agents.channels.status_line import StatusKind, status_kind

    async def run(pool):
        c = _controller(monkeypatch)
        async with _manager(pool, tmp_path, monkeypatch, [_script("Lark answer recovered.")]) as chat:
            await c.transition(PROVIDER, admission.QuotaFailure(None, None))
            adapter = LarkChannelAdapter(runtime_root=tmp_path,
                allowlist=SenderAllowlist("lark", frozenset({"ou_synthetic000"})))
            message = MessageEnvelope.create(source=channel_address("lark"), target=MANAGER_ADDRESS,
                message_type=MessageType.USER_MESSAGE, payload={"text": "Keep my Lark question.",
                    "channel_context": {"chat_id": "oc_synthetic000", "chat_type": "p2p",
                                        "sender_id": "ou_synthetic000"}},
                metadata={"origin_channel_id": "channel-lark"})
            pending = asyncio.create_task(chat.manager._handle(message))
            queue = chat.bus.get_queue("channel-lark")
            try:
                await until(lambda: c.waiters and any("额度已用完" in str(item.payload)
                    for item in queue._queue), "freeze reached the Lark outbound queue")
                freeze = [item for item in queue._queue if "额度已用完" in str(item.payload)]
                assert len(freeze) == 1
                assert status_kind(freeze[0].payload) is StatusKind.PROGRESS
                receipt = await adapter.deliver(freeze[0])
                assert receipt.success
                await record_reply("lark", freeze[0], receipt, runtime_root=tmp_path)
                calls = [json.loads(line) for line in lark_cli_stand_in.read_text().splitlines()]
                assert len([call for call in calls if "额度已用完" in " ".join(call)]) == 1
                assert await pool.fetchval("SELECT count(*) FROM chat_messages WHERE role='assistant'") == 0
                assert await pool.fetchval("SELECT count(*) FROM llm_records") == 0
                assert chat.model.requests == []
                await c.transition(PROVIDER, admission.OperatorClear())
                await asyncio.wait_for(pending, 15)
                answers = [item for item in queue._queue if item.message_type is MessageType.AGENT_REPLY]
                assert len(answers) == 1 and answers[0].payload["text"] == "Lark answer recovered."
                receipt = await adapter.deliver(answers[0])
                assert receipt.success
                await record_reply("lark", answers[0], receipt, runtime_root=tmp_path)
                assert await pool.fetchval("SELECT content FROM chat_messages WHERE role='assistant'") == "Lark answer recovered."
                brief = "\n".join(await chat.manager._recent_conversations(message, "Next question."))
                assert "Lark answer recovered." in brief
                assert "额度已用完" not in brief
            finally:
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
                await adapter._reactions.stop()
                await adapter._channel_outbox.stop()
                await c.stop()

    run_db(run, monkeypatch, timeout=45)


def test_saved_manager_session_retry_at_cap_one_acquires_once(monkeypatch, tmp_path):
    async def run(pool):
        c = _controller(monkeypatch)
        acquire = AsyncMock(wraps=c.acquire)
        monkeypatch.setattr(c, "acquire", acquire)
        async with _manager(pool, tmp_path, monkeypatch, [_script("Recovered once.")], saved=True) as chat:
            await pool.execute("UPDATE llm_providers SET max_concurrency=1 WHERE id=$1", PROVIDER)
            await asyncio.wait_for(chat.manager._handle(_message("Retry the missing session.")), 15)
            assert [reply.payload["text"] for reply in _replies(chat.bus)] == ["Recovered once."]
            assert len(chat.model.requests) == 1
            assert acquire.await_count == 1
            assert not c.waiters and not c.running and not c.record_owners
            rows = await pool.fetch("SELECT status FROM llm_records ORDER BY id")
            assert [row["status"] for row in rows] == ["failed", "completed"]

    run_db(run, monkeypatch, timeout=35)


async def _completion(chat, pool, root):
    project = await projects.create_project(name="admission-project", work_dir=str(root), pool=pool)
    await tasks.create_task(task_id="admission-task", requirement="synthetic completed task",
        project_id=project["id"], work_dir=str(root), status="completed", title="admission-task", pool=pool,
        origin_thread="api-chat")
    await chat.manager._handle(MessageEnvelope.create(
        source=TASK_ROUTER_ADDRESS, target=MANAGER_ADDRESS, message_type=MessageType.GOAL_STATUS,
        payload={"task_id": "admission-task", "goal_id": "admission-goal", "status": "completed",
                 "summary": "## Done\nSynthetic completion."},
        metadata={"task_title": "admission-task", "origin_thread": "api-chat", "origin_channel_id": "channel-web"}))
    return completion_events.wake(MANAGER_ADDRESS, completion_events.Target("manager", "channel-web", "api-chat"))


@pytest.mark.parametrize("lanes_on", [False, True])
def test_background_at_high_utilization_preempts_without_runtime_or_times_out(monkeypatch, tmp_path, lanes_on):
    monkeypatch.setattr(chat_settings, "_completion_turns", chat_settings.CompletionTurns.DM_ONLY)
    monkeypatch.setattr(ApiSession, "can_interrupt", False)
    monkeypatch.setattr(completion_events, "TURN_SECONDS", 3)

    async def run(pool):
        c = _controller(monkeypatch)
        monkeypatch.setattr(lanes_settings, "enabled", lambda: lanes_on)
        async with _manager(pool, tmp_path, monkeypatch, [_script("User goes first.")]) as chat:
            await provider_limits.upsert(PROVIDER, "seven_day", {
                "status": "allowed", "utilization": .95, "source": "claude_stream",
                "observed_at": datetime.now(timezone.utc),
            }, pool=pool)
            due = await _completion(chat, pool, tmp_path)
            settled = []
            original = chat.bus.settle_handled
            def settle(envelope):
                settled.append(envelope.message_id)
                original(envelope)
            monkeypatch.setattr(chat.bus, "settle_handled", settle)
            await chat.bus.start()
            await chat.manager.start(chat.bus)
            await chat.bus.publish(due)
            await until(lambda: len(c.waiters) == 1, "completion waits at 95 percent")
            assert c.waiters[0].lane is Lane.BACKGROUND
            token = chat.manager._sessions["main"].running_token
            assert token is not None and token.admission_waiting
            assert chat.model.requests == []
            assert await pool.fetchval("SELECT count(*) FROM llm_records") == 0
            user = _message("Answer before the completion.")
            await chat.bus.publish(user)
            await until(lambda: user.message_id in settled, "completion fallback then user answer")
            row = await pool.fetchrow("SELECT state, reason, delivery FROM manager_events")
            assert tuple(row) == ("fallback", "turn_stopped" if lanes_on else "turn_timeout", "sent")
            assert token.preempted_by == (user.message_id if lanes_on else None)
            assert len(chat.model.requests) == 1
            assert any(reply.payload["text"] == "User goes first." for reply in _replies(chat.bus))
            assert settled.index(due.message_id) < settled.index(user.message_id)
            assert not c.waiters and not c.running

    run_db(run, monkeypatch, timeout=40)


def test_draining_waiting_manager_shutdown_keeps_message_unsettled_without_record(monkeypatch, tmp_path):
    async def run(pool):
        c = _controller(monkeypatch)
        c.draining = lambda: True
        async with _manager(pool, tmp_path, monkeypatch, []) as chat:
            await chat.bus.start()
            await chat.manager.start(chat.bus)
            user = _message("Answer after restart.")
            await chat.bus.publish(user)
            await until(lambda: len(c.waiters) == 1, "drain blocks admission")
            assert not c.running and chat.model.requests == []
            assert await pool.fetchval("SELECT count(*) FROM llm_records") == 0
            await chat.manager.stop()
            assert any(envelope.message_id == user.message_id for envelope, _ in chat.manager.stopped_in_flight)
            assert not c.waiters and not c.running
            assert await pool.fetchval("SELECT count(*) FROM llm_records") == 0

    run_db(run, monkeypatch, timeout=35)


def test_user_arrival_preempts_waiting_background_on_real_noninterruptible_runtime(monkeypatch, tmp_path):
    monkeypatch.setattr(chat_settings, "_completion_turns", chat_settings.CompletionTurns.DM_ONLY)
    # A regression must fail on arrival classification, before the background timeout can release it.
    monkeypatch.setattr(completion_events, "TURN_SECONDS", 60)

    async def run(pool):
        c = _controller(monkeypatch)
        async with _manager(pool, tmp_path, monkeypatch, [_script("Interactive answer.")]) as chat:
            await provider_limits.upsert(PROVIDER, "seven_day", {
                "status": "allowed", "utilization": .95, "source": "claude_stream",
                "observed_at": datetime.now(timezone.utc),
            }, pool=pool)
            due = await _completion(chat, pool, tmp_path)
            settled = []
            original = chat.bus.settle_handled

            def settle(envelope):
                settled.append(envelope.message_id)
                original(envelope)

            monkeypatch.setattr(chat.bus, "settle_handled", settle)
            await chat.bus.start()
            await chat.manager.start(chat.bus)
            await chat.bus.publish(due)
            await until(lambda: len(c.waiters) == 1, "background waits before any model request")
            session = chat.manager._sessions["main"]
            token = session.running_token
            assert token is not None and token.admission_waiting
            assert chat.model.requests == []
            assert await pool.fetchval("SELECT count(*) FROM llm_records") == 0
            # The prior resident can be a per-turn runtime with no interrupt channel.
            # This is its actual capability, rather than an overridden API capability.
            await session.claude.stop()
            session.claude = ResidentOmp(label=chat.manager.agent_id)
            session.claude_runtime = adapter("omp")
            assert not session.claude.can_interrupt
            user = _message("Answer while the background is waiting.")
            await chat.bus.publish(user)
            await until(lambda: user.message_id in settled, "user preempts without a runtime interrupt", timeout=8)
            assert token.cancel.requested and token.preempted_by == user.message_id
            assert settled.index(due.message_id) < settled.index(user.message_id)
            row = await pool.fetchrow("SELECT state, reason, delivery FROM manager_events")
            assert tuple(row) == ("fallback", "turn_stopped", "sent")
            assert [reply.payload["text"] for reply in _replies(chat.bus) if
                    reply.payload["text"] == "Interactive answer."] == ["Interactive answer."]
            assert len(chat.model.requests) == 1
            assert not c.waiters and not c.running
            await c.stop()

    run_db(run, monkeypatch, timeout=35)
