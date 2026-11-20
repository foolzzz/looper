"""Proactive posts and bus notices reach PostgreSQL after delivery through the Lark stand-in."""
import asyncio
from types import SimpleNamespace

import pytest

from lark_cli_stand_in import OPEN_ID
from tests.db_support import DSN, run_db
from looper.agents import chat_settings, turn_metrics
from looper.agents.channels.allowlist import SenderAllowlist
from looper.agents.channels.lark import LarkChannelAdapter
from looper.agents.channels.registry import ChannelRegistry
from looper.agents.message import MANAGER_ADDRESS, MessageEnvelope, MessageType, channel_address
from looper.agents.task_notice import Notice, NotifyChannel, NotifyTarget, TargetKind

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")


def test_registry_and_bus_notice_record_their_typed_sources(monkeypatch, tmp_path, lark_cli_stand_in):
    monkeypatch.setattr(chat_settings, "_metrics_enabled", True)

    async def run(pool):
        adapter = LarkChannelAdapter(runtime_root=tmp_path,
                                     allowlist=SenderAllowlist("channel-lark", frozenset({OPEN_ID})))
        registry = ChannelRegistry(SimpleNamespace())
        registry._channels[adapter.channel_id] = adapter
        target = NotifyTarget(TargetKind.USER, OPEN_ID)
        await registry.post(adapter.channel_id, target, "A question",
                            proactive=turn_metrics.ProactiveRef(turn_metrics.ProactiveSource.GATE, "gate-a"))
        await registry.post(adapter.channel_id, target, "An ordinary post")
        notice = Notice(NotifyChannel.LARK, target, "The task ended", adapter.channel_id)
        envelope = MessageEnvelope.create(source=MANAGER_ADDRESS, target=channel_address("lark"),
                                         message_type=MessageType.CHAT_NOTICE, payload=notice.payload(),
                                         metadata={"proactive": {"source": "task_notice", "source_ref": "task-a",
                                                                 "manager_id": "manager"}})

        async def subscribe(channel):
            yield envelope

        adapter._bus = SimpleNamespace(subscribe=subscribe)
        adapter._running = True
        await adapter._outbound_loop()
        await asyncio.gather(*turn_metrics._pending)
        rows = await pool.fetch("SELECT source, source_ref, chat, outcome, external_id FROM proactive_deliveries ORDER BY id")
        assert [(r["source"], r["source_ref"], r["chat"], r["outcome"]) for r in rows] == [
            ("gate", "gate-a", OPEN_ID, "sent"), ("task_notice", "task-a", OPEN_ID, "sent")]
        assert all(r["external_id"] for r in rows)

    run_db(run, monkeypatch)


def test_off_gate_and_subscription_keep_the_original_calls_and_write_no_metrics(
        monkeypatch, tmp_path, lark_cli_stand_in):
    from datetime import datetime, timezone
    from unittest.mock import AsyncMock, call
    from looper.agents import gate_courier, subscription_runs, subscriptions

    monkeypatch.setattr(chat_settings, "_metrics_enabled", False)

    async def run(pool):
        adapter = LarkChannelAdapter(runtime_root=tmp_path,
                                     allowlist=SenderAllowlist("channel-lark", frozenset({OPEN_ID})))
        registry = ChannelRegistry(SimpleNamespace())
        registry._channels[adapter.channel_id] = adapter
        post = AsyncMock(wraps=registry.post)
        target = NotifyTarget(TargetKind.USER, OPEN_ID)
        courier = gate_courier.GateCourier(post)
        monkeypatch.setattr(gate_courier.db_user_gates, "gate_state", AsyncMock(return_value="discussing"))
        monkeypatch.setattr(gate_courier.db_user_gates, "record_post", AsyncMock())
        monkeypatch.setattr(courier, "_reply_to", AsyncMock(return_value=None))
        await courier._handle({"id": 1, "step_id": "gate-a", "task_id": "task-a", "kind": "reply",
                               "state": "discussing", "text": "A question", "origin_channel_id": "channel-lark",
                               "task_chat_id": OPEN_ID, "chat_kind": "user", "chat_group": False}, None)
        stream = {"target_channel": "lark", "target_chat": OPEN_ID}
        segment = {"segment": 1, "state": "pending", "attempts": 0, "text": "A digest"}
        monkeypatch.setattr(subscription_runs.db_subs, "get_run", AsyncMock(return_value=stream))
        monkeypatch.setattr(subscription_runs.db_subs, "settle_sending", AsyncMock())
        monkeypatch.setattr(subscription_runs.db_subs, "list_deliveries", AsyncMock(return_value=[segment]))
        monkeypatch.setattr(subscription_runs.db_subs, "claim_segment", AsyncMock(return_value=True))

        async def mark_segment(*args, **kwargs):
            segment["state"] = "sent"

        monkeypatch.setattr(subscription_runs.db_subs, "mark_segment", mark_segment)
        runs = subscription_runs.SubscriptionRuns(poster=post, own_apps=AsyncMock(return_value=[]),
                                                  runtime_root=tmp_path, clock=lambda: datetime.now(timezone.utc))
        monkeypatch.setattr(runs, "_refusal", AsyncMock(return_value=None))
        monkeypatch.setattr(runs, "_finish_push", AsyncMock())
        await runs._push("run-a")
        assert post.call_args_list == [
            call("channel-lark", target, "A question", reply_to=None, idempotency_key="gate-1"),
            call("channel-lark", target, "A digest", idempotency_key=subscriptions.idempotency_keys("run-a", 1)[0])]
        assert await pool.fetchval("SELECT count(*) FROM proactive_deliveries") == 0

    run_db(run, monkeypatch)
