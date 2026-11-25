"""Task end notices on the Manager → queue → channel path; no real chat is contacted."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from looper.agents import chat_settings, completion_events, proactive_delivery as delivery, turn_metrics
from looper.agents.bus import MessageBus
from looper.agents.channels.allowlist import SenderAllowlist
from looper.agents.channels.base import DeliveryReceipt
from looper.agents.channels.lark import LarkChannelAdapter
from looper.agents.channels.outbound import OutboxSource
from looper.agents.channels.telegram import TelegramChannelAdapter
from looper.agents.message import MANAGER_ADDRESS, TASK_ROUTER_ADDRESS, MessageEnvelope, MessageType
from looper.agents.task_notice import Notice, NotifyChannel, NotifyTarget, TargetKind
from looper.agents.tiers.manager import ManagerAgent, ManagerConfig
from looper.db import chat, projects, tasks
from tests.db_support import DSN, run_db

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")
DAY = datetime(2026, 11, 20, 12, tzinfo=timezone.utc)
NIGHT = DAY.replace(hour=23)


async def prepare(pool, tmp_path, monkeypatch, channel="lark", group=False, status="completed", now=DAY,
                  channel_id=None):
    target = NotifyTarget(TargetKind.CHAT if group or channel == "telegram" else TargetKind.USER,
                          "4242" if channel == "telegram" else "oc_team" if group else "ou_requester",
                          mention="ou_requester" if group and channel != "telegram" else None, group=group)
    channel_id = channel_id or f"channel-{channel}"
    project = await projects.create_project(name="notice-test", work_dir=str(tmp_path), pool=pool)
    await tasks.create_task(task_id="task-notice", requirement="Synthetic task", project_id=project["id"],
                            work_dir=str(tmp_path), origin_channel_id=channel_id, **target.columns(),
                            origin_reply_route={"chat_id": "oc_direct"}, pool=pool)
    monkeypatch.setattr(chat_settings, "_timezone", "UTC")
    real_decide = delivery.decide
    monkeypatch.setattr(delivery, "decide", lambda *a, **kw: real_decide(*a, **kw, now=now))
    text = ("✅ 任务完成" if status == "completed" else "❌ 任务失败") + " · example\n原文 *code*\n/#tasks/task-notice"
    notice = Notice(NotifyChannel(channel), target, text, channel_id=channel_id)
    bus = MessageBus(runtime_root=tmp_path)
    manager = ManagerAgent(config=ManagerConfig(work_dir=tmp_path), runtime_root=tmp_path)
    manager._bus = bus
    status_envelope = MessageEnvelope.create(source=TASK_ROUTER_ADDRESS, target=MANAGER_ADDRESS,
        message_type=MessageType.GOAL_STATUS, payload={"task_id": "task-notice", "status": status,
                                                    "summary": "Synthetic result", "notice": notice.payload()},
        correlation_id="notice-correlation", metadata={"origin_channel_id": channel_id})
    await manager._handle_goal_status(status_envelope)
    queued = bus.get_queue(channel_id).get_nowait()
    await bus.shutdown()
    if channel == "telegram":
        adapter = TelegramChannelAdapter(bot_token="0:synthetic",
                                         allowlist=SenderAllowlist(channel_id, frozenset()))
        sender = AsyncMock(return_value=DeliveryReceipt(channel, True))
        monkeypatch.setattr(adapter, "_send_answer", sender)
    else:
        adapter = LarkChannelAdapter(runtime_root=tmp_path, channel_type=channel,
                                    allowlist=SenderAllowlist(channel_id, frozenset()))
        sender = AsyncMock(return_value=DeliveryReceipt(channel, True))
        monkeypatch.setattr(adapter, "_send_notice", sender)
    return adapter, sender, notice, queued, status_envelope


@pytest.mark.parametrize("channel,group,status", [
    ("telegram", False, "completed"), ("telegram", True, "failed"),
    ("lark", False, "failed"), ("lark", True, "completed"), ("lark_bot", False, "completed"),
])
def test_off_keeps_exact_envelope_and_channel_calls(tmp_path, monkeypatch, channel, group, status):
    async def check(pool):
        adapter, sender, notice, queued, _ = await prepare(pool, tmp_path, monkeypatch, channel, group, status)
        decide = AsyncMock(side_effect=RuntimeError("off must not decide"))
        monkeypatch.setattr(delivery, "decide", decide)
        assert queued.payload == notice.payload()
        assert queued.metadata == {"origin_channel_id": notice.channel_id,
                                   "proactive": {"source": "task_notice", "source_ref": "task-notice", "manager_id": "manager"}}
        assert (await adapter.deliver(queued)).success
        decide.assert_not_awaited()
        [call] = sender.await_args_list
        assert call.args == (int(notice.target.id) if channel == "telegram" else notice.target, notice.text)
        if channel == "telegram":
            # The answer call also names the message (per part) and its outbox source for the outbound gateway,
            # which stays inert while it is off.
            assert set(call.kwargs) == {"message_id", "source"} and call.kwargs["source"] is OutboxSource.NOTICE
            assert call.kwargs["message_id"].startswith(queued.message_id)
        else:
            assert call.kwargs == {}
        [row] = await pool.fetch("SELECT * FROM proactive_deliveries")
        assert row["body"] == notice.text and row["decision"] is None
    run_db(check, monkeypatch)


@pytest.mark.parametrize("channel,group,status", [
    ("telegram", False, "completed"), ("lark", False, "failed"), ("lark_bot", True, "completed"),
])
def test_on_sends_same_notice_and_records_normal_urgency(tmp_path, monkeypatch, channel, group, status):
    async def check(pool):
        chat_settings.set_delivery_enabled(True)
        adapter, sender, notice, queued, _ = await prepare(pool, tmp_path, monkeypatch, channel, group, status)
        assert (await adapter.deliver(queued)).success
        assert sender.await_args.args == (int(notice.target.id) if channel == "telegram" else notice.target, notice.text)
        row = await pool.fetchrow("SELECT * FROM proactive_deliveries")
        assert (row["source"], row["urgency"], row["chat_kind"], row["channel_id"], row["chat"],
                row["task_id"], row["decision"], row["counted"], row["outcome"], row["sent_at"] is not None) == (
            "task_notice", "normal", "group" if group else "direct", notice.channel_id, notice.target.id,
            "task-notice", "send", not group, "sent", True)
    run_db(check, monkeypatch)


@pytest.mark.parametrize("channel", ["lark", "lark_bot", "telegram"])
@pytest.mark.parametrize("metrics_enabled,delivery_enabled", [(True, True), (True, False),
                                                             (False, True), (False, False)])
def test_outbound_metrics_share_the_notice_decision(tmp_path, monkeypatch, channel, metrics_enabled, delivery_enabled):
    async def check(pool):
        monkeypatch.setattr(chat_settings, "_metrics_enabled", metrics_enabled)
        chat_settings.set_delivery_enabled(delivery_enabled)
        adapter, sender, notice, queued, status = await prepare(pool, tmp_path, monkeypatch, channel=channel)
        sender.return_value = DeliveryReceipt(channel, True, external_id="synthetic-receipt")

        async def subscribe(channel_id):
            yield queued

        adapter._bus = SimpleNamespace(subscribe=subscribe, runtime_root=tmp_path)
        adapter._running = True
        await adapter._outbound_loop()
        await asyncio.gather(*turn_metrics._pending)
        sender.assert_awaited_once()
        assert sender.await_args.args == (int(notice.target.id) if channel == "telegram" else notice.target, notice.text)
        rows = await pool.fetch("SELECT * FROM proactive_deliveries")
        assert len(rows) == 1
        if rows:
            [row] = rows
            assert row["dedupe_key"] == (f"task_notice:{status.message_id}" if delivery_enabled else queued.message_id)
            assert row["outcome"] == "sent" and row["external_id"] == "synthetic-receipt"
            assert row["sent_at"] is not None
            assert row["decision"] == ("send" if delivery_enabled else None)
            assert row["counted"] == (True if delivery_enabled else None)
            assert row["task_id"] == ("task-notice" if delivery_enabled else None)
    run_db(check, monkeypatch)


@pytest.mark.parametrize("over_cap", [False, True])
def test_on_defers_without_channel_calls_at_night_or_cap(tmp_path, monkeypatch, over_cap):
    async def check(pool):
        chat_settings.set_delivery_enabled(True)
        adapter, sender, notice, queued, _ = await prepare(pool, tmp_path, monkeypatch, now=DAY if over_cap else NIGHT)
        if over_cap:
            for n in range(5):
                await delivery.decide(delivery.Delivery(delivery.DeliveryKind.TASK_NOTICE, delivery.Urgency.NORMAL,
                                                       delivery.ChatKind.DIRECT), dedupe_key=f"earlier:{n}",
                                      channel_id=notice.channel_id, chat=notice.target.id, last_user_message_at=None)
        await adapter.deliver(queued)
        sender.assert_not_awaited()
        row = await pool.fetchrow("SELECT * FROM proactive_deliveries WHERE task_id = 'task-notice'")
        assert (row["decision"], row["reason"], row["digest_date"], row["sent_at"]) == (
            "digest", "daily_cap" if over_cap else "quiet_hours", (NIGHT + timedelta(days=1)).date(), None)
    run_db(check, monkeypatch)


@pytest.mark.parametrize("same_chat", [False, True])
def test_recent_user_reply_only_bypasses_quiet_hours_for_its_chat(tmp_path, monkeypatch, same_chat):
    async def check(pool):
        chat_settings.set_delivery_enabled(True)
        adapter, sender, _, queued, _ = await prepare(pool, tmp_path, monkeypatch, now=NIGHT)
        session = "lark:oc_direct" if same_chat else "lark:oc_other"
        await chat.save_message(session_id=session, message_id="user-message", role="user", content="Synthetic")
        await pool.execute("UPDATE chat_messages SET created_at = $1", NIGHT - timedelta(minutes=1))
        await adapter.deliver(queued)
        assert sender.await_count == int(same_chat)
        row = await pool.fetchrow("SELECT * FROM proactive_deliveries")
        assert (row["reason"], row["counted"]) == ("recent_reply" if same_chat else "quiet_hours", False)
    run_db(check, monkeypatch)


@pytest.mark.parametrize("session", [
    "channel-lark-2:oc_direct#manager-beta",  # the chat has since moved to another Manager
    "channel-lark-3:oc_direct#manager-beta",  # another bot's chat must not give this bot a grace period
])
def test_recent_reply_follows_the_bot_and_chat_across_manager_routing(tmp_path, monkeypatch, session):
    async def check(pool):
        chat_settings.set_delivery_enabled(True)
        adapter, sender, _, queued, _ = await prepare(pool, tmp_path, monkeypatch, now=NIGHT,
                                                      channel_id="channel-lark-2")
        await chat.save_message(session_id=session, message_id="routed-user-message", role="user", content="Synthetic")
        await pool.execute("UPDATE chat_messages SET created_at = $1", NIGHT - timedelta(minutes=1))
        await adapter.deliver(queued)
        same_bot = session == "channel-lark-2:oc_direct#manager-beta"
        assert sender.await_count == int(same_bot)
        row = await pool.fetchrow("SELECT * FROM proactive_deliveries")
        assert row["reason"] == ("recent_reply" if same_bot else "quiet_hours")
    run_db(check, monkeypatch)


def test_failed_send_reuses_decision_on_completion_fallback(tmp_path, monkeypatch):
    async def check(pool):
        chat_settings.set_delivery_enabled(True)
        adapter, sender, _, queued, status = await prepare(pool, tmp_path, monkeypatch)
        sender.return_value = DeliveryReceipt("lark", False, error="synthetic refusal")
        assert not (await adapter.deliver(queued)).success
        assert await pool.fetchval("SELECT sent_at FROM proactive_deliveries") is None
        event = dict(await pool.fetchrow("SELECT * FROM manager_events"))
        bus = SimpleNamespace(publish=AsyncMock())
        await completion_events._send_notice(bus, event)
        fallback = bus.publish.await_args.args[0]
        sender.return_value = DeliveryReceipt("lark", True)
        assert (await adapter.deliver(fallback)).success
        rows = await pool.fetch("SELECT * FROM proactive_deliveries")
        assert len(rows) == 1 and rows[0]["sent_at"] is not None
        assert rows[0]["dedupe_key"] == f"task_notice:{status.message_id}"
    run_db(check, monkeypatch)


def test_completion_reply_and_other_chat_notices_do_not_use_task_delivery(tmp_path, monkeypatch):
    async def check(pool):
        adapter, sender, _, queued, _ = await prepare(pool, tmp_path, monkeypatch)
        chat_settings.set_delivery_enabled(True)
        decide = AsyncMock(side_effect=RuntimeError("unmarked notice"))
        monkeypatch.setattr(delivery, "decide", decide)
        event = dict(await pool.fetchrow("SELECT * FROM manager_events"))
        bus = SimpleNamespace(publish=AsyncMock())
        await completion_events._send_reply(bus, MANAGER_ADDRESS, [event], "Synthetic completion", None)
        assert (await adapter.deliver(bus.publish.await_args.args[0])).success
        assert (await adapter.deliver(queued)).success
        assert sender.await_count == 2
        decide.assert_not_awaited()
        rows = await pool.fetch("SELECT source, body, decision FROM proactive_deliveries ORDER BY id")
        assert [tuple(row) for row in rows] == [
            ("completion", "Synthetic completion", None), ("task_notice", queued.payload["text"], None)]
    run_db(check, monkeypatch)


def test_drop_never_calls_channel_and_failed_receipt_record_does_not_resend(tmp_path, monkeypatch):
    async def check(pool):
        from looper.db import proactive_deliveries

        chat_settings.set_delivery_enabled(True)
        adapter, sender, _, queued, _ = await prepare(pool, tmp_path, monkeypatch)
        real_deliver = delivery.deliver
        monkeypatch.setattr(delivery, "deliver", lambda *args: delivery.Decision(delivery.Outcome.DROP,
                                                                                delivery.Reason.SILENT))
        await adapter.deliver(queued)
        sender.assert_not_awaited()
        assert await pool.fetchval("SELECT decision FROM proactive_deliveries") == "drop"
        await pool.execute("DELETE FROM proactive_deliveries")
        monkeypatch.setattr(delivery, "deliver", real_deliver)
        monkeypatch.setattr(proactive_deliveries, "record_receipt",
                            AsyncMock(side_effect=RuntimeError("synthetic outage")))
        assert (await adapter.deliver(queued)).success
        sender.assert_awaited_once()
    run_db(check, monkeypatch)


def test_decision_failure_still_sends_original_notice(tmp_path, monkeypatch):
    async def check(pool):
        chat_settings.set_delivery_enabled(True)
        adapter, sender, notice, queued, _ = await prepare(pool, tmp_path, monkeypatch)
        monkeypatch.setattr(delivery, "decide", AsyncMock(side_effect=RuntimeError("synthetic outage")))
        assert (await adapter.deliver(queued)).success
        assert sender.await_args.args == (notice.target, notice.text)
    run_db(check, monkeypatch)
