"""Idle wakes take real Manager turns, with only loopback models and synthetic posts."""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from looper.agents import chat_settings, completion_events, idle_checks, lanes_settings, proactive_delivery, turn_metrics
from looper.agents.channels.base import PostReceipt
from looper.agents.managers import ManagerFleet
from looper.agents.supervisor import AgentSupervisor
from looper.agents.message import MANAGER_ADDRESS, MessageEnvelope, MessageType, channel_address
from looper.agents.runtimes.api.session import ApiSession
from looper.agents.tiers import idle_check_turn
from looper.db import idle_checks as store
from tests.api_manager_support import USAGE, _manager, _message
from tests.db_support import DSN, run_db, until
from tests.fake_model_api import ResponseScript, call_events, call_item, completed, text_events

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")
NOTE = ("[Looper] Before this message, your idle check was interrupted so you could answer. "
        "Nothing was sent from it.")


async def _check(pool):
    return await store.insert_running(pool, manager_id="manager", local_date=datetime.now(timezone.utc).date(),
        items=[{"section": "logins", "key": "login:synthetic", "title": "Synthetic login", "since": None,
                "facts": {"fix": "Run synthetic login"}}], channel_id="channel-lark", chat="ou_synthetic_idle")


def _script(text, gate=None):
    return ResponseScript(events=text_events(text, usage=USAGE), gate=gate)


@pytest.fixture(autouse=True)
def _policy(monkeypatch):
    monkeypatch.setattr(chat_settings, "_metrics_enabled", True)
    monkeypatch.setattr(chat_settings, "_delivery_enabled", True)
    monkeypatch.setattr(lanes_settings, "enabled", lambda: True)
    monkeypatch.setattr(proactive_delivery, "load_policy", lambda: proactive_delivery.DeliveryPolicy(
        timezone=timezone.utc, quiet_hours=None))


@pytest.mark.parametrize("reply,state,reason", [("NO_REPLY", "silent", None),
    ("Synthetic login needs attention.", "sent", None), ("NO_REPLY but also hello", "failed", "invalid_reply")])
def test_idle_reply_records_its_origin_and_sends_only_a_valid_message(monkeypatch, tmp_path, reply, state, reason):
    async def run(pool):
        post = AsyncMock(return_value=PostReceipt(True, external_ids=("synthetic-idle-reply",)))
        monkeypatch.setattr(idle_check_turn, "_post", post)
        async with _manager(pool, tmp_path, monkeypatch, [_script(reply)]) as chat:
            check_id = await _check(pool)
            wake = idle_checks.due_envelope(check_id)
            await chat.manager._handle(wake)
            row = await store.get(check_id)
            assert (row["state"], row["reason"]) == (state, reason)
            assert row["record_id"] is not None
            await until(lambda: not turn_metrics._pending, "idle turn metrics written")
            metadata = json.loads(await pool.fetchval("SELECT metadata FROM llm_records WHERE id=$1", row["record_id"]))
            assert metadata["idle_check"] == {"check_id": check_id}
            assert metadata["turn_metrics"] == {"origin": "idle_check", "tool_calls": 0,
                "delivery": "failed" if reason else "reply"}
            assert metadata["lane"] == {"lane": "background", "preempted_by": None}
            prompt = json.dumps(chat.model.requests[0].body["input"])
            assert "[IDLE CHECK]" in prompt and "Synthetic login" in prompt
            assert "Projects:" not in prompt
            assert not chat.manager.busy and chat.manager._sessions["main"].running_token is None
            await chat.manager._handle(wake)  # replay cannot run or send twice
            assert len(chat.model.requests) == 1
            if state == "sent":
                post.assert_awaited_once()
                assert post.await_args.kwargs["idempotency_key"] == f"idle-{check_id}"
                text = post.await_args.args[2]
                assert text == reply + "\n\n" + idle_checks.DETAILS_LINE
                record = await pool.fetchrow("SELECT role, content FROM chat_messages WHERE message_id=$1", f"idle-{check_id}")
                assert tuple(record) == ("assistant", text)
                assert await pool.fetchval("SELECT outcome FROM proactive_deliveries WHERE dedupe_key=$1", f"idle-{check_id}") == "sent"
            else:
                post.assert_not_awaited()
                assert await pool.fetchval("SELECT count(*) FROM chat_messages") == 0
    run_db(run, monkeypatch, timeout=60)


@pytest.mark.parametrize("via_stop", [False, True])
def test_main_user_preempts_the_check_and_gets_one_note_after_its_wake_is_settled(monkeypatch, tmp_path, via_stop):
    monkeypatch.setattr(ApiSession, "can_interrupt", True)
    async def run(pool):
        post = AsyncMock()
        monkeypatch.setattr(idle_check_turn, "_post", post)
        gate = asyncio.Event()
        check = _script("A stale reply must not go out", gate)
        async with _manager(pool, tmp_path, monkeypatch, [check, _script("User answer")]) as chat:
            check_id = await _check(pool)
            wake = idle_checks.due_envelope(check_id)
            settled = []
            original = chat.bus.settle_handled
            def settle(envelope):
                settled.append(envelope.message_id)
                original(envelope)
            monkeypatch.setattr(chat.bus, "settle_handled", settle)
            await chat.bus.start()
            await chat.manager.start(chat.bus)
            await chat.bus.publish(wake)
            await asyncio.wait_for(check.gate_reached.wait(), 15)
            user = _message("Please answer me")
            if via_stop:
                fleet = ManagerFleet(platform=chat.ops.platform, supervisor=AgentSupervisor(chat.bus, tmp_path),
                    runtime_root=tmp_path, url="http://127.0.0.1:1", config=chat.manager._config, broadcaster=None)
                fleet._agents[chat.manager.agent_id] = chat.manager
                chat.manager.stop_chat = fleet.stop_chat
                token = chat.manager._sessions["main"].running_token
                monkeypatch.setattr(chat_settings, "_stop_enabled", True)
                for channel_id, sender, chat_type in [
                    ("channel-lark-other", "ou_synthetic_idle", "p2p"),
                    ("channel-lark", "ou_synthetic_other", "p2p"),
                    ("channel-lark", "ou_synthetic_idle", "group"),
                ]:
                    unrelated = MessageEnvelope.create(source=channel_address("lark", channel_id),
                        target=MANAGER_ADDRESS, message_type=MessageType.USER_MESSAGE,
                        payload={"text": "/stop", "channel_context": {"chat_id": "oc_synthetic_idle_dm",
                            "chat_type": chat_type, "sender_id": sender}})
                    await chat.bus.publish(unrelated)
                    await asyncio.gather(*chat.manager._stop_tasks)
                    assert token.stop is None
                    assert (await store.get(check_id))["state"] == "running"
                stop = MessageEnvelope.create(source=channel_address("lark"), target=MANAGER_ADDRESS,
                    message_type=MessageType.USER_MESSAGE, payload={"text": "/stop", "channel_context": {
                        "chat_id": "oc_synthetic_idle_dm", "chat_type": "p2p", "sender_id": "ou_synthetic_idle"}})
                await chat.bus.publish(stop)
                await asyncio.gather(*chat.manager._stop_tasks)
                assert token.stop is not None
                await until(lambda: wake.message_id in settled, "operator stop ended the check")
            await chat.bus.publish(user)
            await until(lambda: user.message_id in settled, "idle check fallback and user reply")
            row = await store.get(check_id)
            assert (row["state"], row["reason"]) == ("preempted", None)
            assert settled == [wake.message_id, user.message_id]
            request = json.dumps(chat.model.requests[1].body["input"])
            assert request.count(NOTE) == 1
            record = json.loads(await pool.fetchval("SELECT metadata FROM llm_records WHERE id=$1", row["record_id"]))
            assert record["lane"]["preempted_by"] == (None if via_stop else user.message_id)
            assert record["turn_metrics"]["delivery"] == "stopped"
            post.assert_not_awaited()
    run_db(run, monkeypatch, timeout=60)


def test_noninterruptible_idle_turn_uses_the_shared_late_end_before_answering_main(monkeypatch, tmp_path):
    assert idle_check_turn.IDLE_TURN_SECONDS == 90
    monkeypatch.setattr(idle_check_turn, "IDLE_TURN_SECONDS", 3.0)
    monkeypatch.setattr(completion_events, "END_SECONDS", 0.02)
    monkeypatch.setattr(ApiSession, "can_interrupt", False)
    async def run(pool):
        post = AsyncMock()
        monkeypatch.setattr(idle_check_turn, "_post", post)
        check = _script("Never sent", asyncio.Event())
        async with _manager(pool, tmp_path, monkeypatch, [check, _script("User answer")]) as chat:
            check_id = await _check(pool)
            from looper.agents.tiers import background_turn

            end_late = background_turn.end_late
            async def ending(*args, **kwargs):
                row = await store.get(check_id)
                assert (row["state"], row["reason"]) == ("failed", "turn_timeout")
                assert check.gate_reached.is_set()  # the runtime, rather than preparation, reached the deadline
                await end_late(*args, **kwargs)
            monkeypatch.setattr(background_turn, "end_late", ending)
            telling = asyncio.create_task(chat.manager._handle(idle_checks.due_envelope(check_id)))
            await asyncio.wait_for(check.gate_reached.wait(), 15)
            await asyncio.wait_for(telling, 15)
            row = await store.get(check_id)
            assert (row["state"], row["reason"]) == ("failed", "turn_timeout")
            assert not chat.manager.busy
            await asyncio.wait_for(chat.manager._handle(_message()), 15)
            assert len(chat.model.requests) == 2
            post.assert_not_awaited()
    run_db(run, monkeypatch, timeout=60)


def test_turned_off_manager_fails_the_wake_without_starting_a_turn(monkeypatch, tmp_path):
    async def run(pool):
        async with _manager(pool, tmp_path, monkeypatch, []) as chat:
            check_id = await _check(pool)
            chat.manager._turned_off = True
            await chat.manager._handle(idle_checks.due_envelope(check_id))
            row = await store.get(check_id)
            assert (row["state"], row["reason"], row["record_id"]) == ("failed", "manager_off", None)
            assert not chat.model.requests
    run_db(run, monkeypatch)


def test_idle_turn_rejects_write_calls_through_real_manager_ops(monkeypatch, tmp_path):
    raw = json.dumps({"action": "restart", "task_id": "synthetic-task"})
    events = call_events(raw, name="looper_task_control", call_id="idle-call", id="fc_idle")
    events[-1] = completed([call_item(raw, name="looper_task_control", call_id="idle-call", id="fc_idle")], usage=USAGE)
    async def run(pool):
        async with _manager(pool, tmp_path, monkeypatch,
                            [ResponseScript(events=events), _script("NO_REPLY")]) as chat:
            check_id = await _check(pool)
            await chat.manager._handle(idle_checks.due_envelope(check_id))
            assert (await store.get(check_id))["state"] == "silent"
            actions = await pool.fetch("SELECT tool, status, error_code FROM manager_actions")
            assert [tuple(row) for row in actions] == [("looper_task_control", "error", "needs_user_turn")]
            assert await pool.fetchval("SELECT count(*) FROM tasks") == 0
    run_db(run, monkeypatch, timeout=60)
