"""Proactive posts and bus notices reach PostgreSQL after delivery through the Lark stand-in."""
import asyncio
from datetime import datetime, timezone
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


def test_channel_receipt_updates_the_existing_gate_reminder_decision(monkeypatch, tmp_path, lark_cli_stand_in):
    monkeypatch.setattr(chat_settings, "_metrics_enabled", True)

    async def check(pool):
        now = datetime.now(timezone.utc)
        decision = dict(await pool.fetchrow(
            "INSERT INTO proactive_deliveries (source, source_ref, channel_id, chat, chat_kind, dedupe_key, "
            "decision, reason, counted, local_date, decided_at, urgency, step_id) "
            "VALUES ('gate_reminder', 'synthetic-gate', 'channel-lark', $1, 'direct', 'gate_message:17', "
            "'send', 'urgent', false, $2, $3, 'blocking', 'synthetic-gate') RETURNING *", OPEN_ID, now.date(), now))
        assert decision["outcome"] is None and decision["sent_at"] is None
        adapter = LarkChannelAdapter(runtime_root=tmp_path,
                                     allowlist=SenderAllowlist("channel-lark", frozenset({OPEN_ID})))
        registry = ChannelRegistry(SimpleNamespace())
        registry._channels[adapter.channel_id] = adapter
        receipt = await registry.post(adapter.channel_id, NotifyTarget(TargetKind.USER, OPEN_ID), "A reminder",
            idempotency_key="gate-17", **turn_metrics.proactive_kwargs(
                turn_metrics.ProactiveSource.GATE_REMINDER, "synthetic-gate", dedupe_key="gate_message:17"))
        assert receipt.success and receipt.external_ids
        await asyncio.gather(*turn_metrics._pending)
        rows = await pool.fetch("SELECT * FROM proactive_deliveries")
        assert len(rows) == 1
        row = dict(rows[0])
        assert now <= row["sent_at"] <= datetime.now(timezone.utc)
        assert row == {**decision, "outcome": "sent", "external_id": receipt.external_ids[0], "sent_at": row["sent_at"],
                       "body": "A reminder", "body_stored_at": row["body_stored_at"]}

    run_db(check, monkeypatch)


@pytest.mark.parametrize("metrics_on", [False, True])
def test_registry_and_bus_notice_record_their_typed_sources(monkeypatch, tmp_path, lark_cli_stand_in, metrics_on):
    monkeypatch.setattr(chat_settings, "_metrics_enabled", metrics_on)

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
            yield envelope  # Durable bus replay keeps the same envelope id.

        adapter._bus = SimpleNamespace(subscribe=subscribe)
        adapter._running = True
        await adapter._outbound_loop()
        await asyncio.gather(*turn_metrics._pending)
        rows = await pool.fetch("SELECT source, source_ref, chat, outcome, external_id, body FROM proactive_deliveries ORDER BY id")
        assert [(r["source"], r["source_ref"], r["chat"], r["outcome"], r["body"]) for r in rows] == [
            ("gate", "gate-a", OPEN_ID, "sent", "A question"),
            ("task_notice", "task-a", OPEN_ID, "sent", "The task ended")]
        if metrics_on:
            assert all(r["external_id"] for r in rows)

    run_db(run, monkeypatch)


def test_off_gate_and_subscription_keep_the_sent_text_and_record_bodies(
        monkeypatch, tmp_path, lark_cli_stand_in):
    from datetime import datetime, timezone
    from unittest.mock import AsyncMock
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
        assert [args.args for args in post.call_args_list] == [
            ("channel-lark", target, "A question"), ("channel-lark", target, "A digest")]
        assert [args.kwargs["idempotency_key"] for args in post.call_args_list] == [
            "gate-1", subscriptions.idempotency_keys("run-a", 1)[0]]
        await asyncio.gather(*turn_metrics._pending)
        assert [tuple(row) for row in await pool.fetch(
            "SELECT source, body, decision FROM proactive_deliveries ORDER BY id")] == [
                ("gate", "A question", None), ("subscription", "A digest", None)]

    run_db(run, monkeypatch)


@pytest.mark.parametrize("key", ["gate-17", "intent-3-9", "run-synthetic-1"])
def test_three_delivery_retries_update_one_row(monkeypatch, key):
    from unittest.mock import AsyncMock
    from looper.agents.channels.base import PostOutcome, PostReceipt

    monkeypatch.setattr(chat_settings, "_metrics_enabled", True)

    async def check(pool):
        registry = ChannelRegistry(SimpleNamespace())
        registry._channels["channel-lark"] = SimpleNamespace(post=AsyncMock(side_effect=[
            PostReceipt(False, outcome=PostOutcome.REJECTED),
            PostReceipt(False, outcome=PostOutcome.UNKNOWN),
            PostReceipt(True, external_ids=("synthetic-receipt",)),
        ]))
        target = NotifyTarget(TargetKind.USER, OPEN_ID)
        ref = turn_metrics.ProactiveRef(turn_metrics.ProactiveSource.GATE, "synthetic-gate")
        for outcome in ("rejected", "unknown", "sent"):
            await registry.post("channel-lark", target, "Question", idempotency_key=key, proactive=ref)
            await asyncio.gather(*turn_metrics._pending)
        rows = await pool.fetch("SELECT dedupe_key, outcome, external_id FROM proactive_deliveries")
        assert [tuple(row) for row in rows] == [(key, "sent", "synthetic-receipt")]
    run_db(check, monkeypatch)


@pytest.mark.parametrize("cards", [False, True])
def test_gate_parent_fallback_updates_the_original_delivery(monkeypatch, cards):
    from unittest.mock import AsyncMock
    from looper.agents import gate_courier
    from looper.agents.channels.base import PostOutcome, PostReceipt

    monkeypatch.setattr(chat_settings, "_metrics_enabled", True)

    async def check(pool):
        registry = ChannelRegistry(SimpleNamespace())
        post = AsyncMock(side_effect=[
            PostReceipt(False, parent_gone=True, outcome=PostOutcome.REJECTED),
            PostReceipt(True, external_ids=("synthetic-fallback",)),
        ])
        registry._channels["channel-lark"] = SimpleNamespace(**{"post_card" if cards else "post": post})
        courier = gate_courier.GateCourier(registry.post, post_card=registry.post_card if cards else None)
        if cards:
            monkeypatch.setattr(courier, "_card", AsyncMock(return_value=SimpleNamespace(text="Question")))
        monkeypatch.setattr(gate_courier.db_user_gates, "gate_state", AsyncMock(return_value="discussing"))
        monkeypatch.setattr(gate_courier.db_user_gates, "record_post", AsyncMock())
        monkeypatch.setattr(courier, "_reply_to", AsyncMock(return_value="synthetic-recalled"))
        await courier._handle({"id": 17, "step_id": "synthetic-gate", "task_id": "synthetic-task", "kind": "reply",
                               "state": "discussing", "text": "Question", "origin_channel_id": "channel-lark",
                               "task_chat_id": OPEN_ID, "chat_kind": "user", "chat_group": False}, None)
        await asyncio.gather(*turn_metrics._pending)
        assert [call.kwargs["idempotency_key"] for call in post.call_args_list] == ["gate-17", "gate-17-own"]
        rows = await pool.fetch("SELECT dedupe_key, outcome, external_id FROM proactive_deliveries")
        assert [tuple(row) for row in rows] == [("gate-17", "sent", "synthetic-fallback")]
    run_db(check, monkeypatch)
