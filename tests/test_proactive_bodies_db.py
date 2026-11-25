"""Rendered proactive sends are retained without changing delivery or its switches."""
import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from looper.agents import chat_settings, turn_metrics
from looper.agents.channels.base import PostReceipt
from looper.agents.channels.registry import ChannelRegistry
from looper.agents.task_notice import NotifyTarget, TargetKind
from looper.db import proactive_deliveries as store
from tests.db_support import DSN, run_db, run_with_channel_configs

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")


@pytest.mark.parametrize("source", list(turn_metrics.ProactiveSource))
def test_every_typed_source_stores_the_body_before_transport_with_metrics_off(source, monkeypatch):
    monkeypatch.setattr(chat_settings, "_metrics_enabled", False)

    async def check(pool):
        observed = []

        async def post(target, text, **kwargs):
            observed.extend(await pool.fetch("SELECT * FROM proactive_deliveries"))
            return PostReceipt(True, external_ids=("synthetic-message",))

        registry = ChannelRegistry(SimpleNamespace())
        registry._channels["synthetic-channel"] = SimpleNamespace(post=post)
        text = "  Rendered **message**\n第二行\n"
        receipt = await registry.post("synthetic-channel", NotifyTarget(TargetKind.CHAT, "synthetic-chat"), text,
            idempotency_key="synthetic-key", **turn_metrics.proactive_kwargs(source, "synthetic-ref"))
        await asyncio.gather(*turn_metrics._pending)
        assert receipt.success
        assert len(observed) == 1 and observed[0]["body"] == text
        row = await store.get(pool, "synthetic-key")
        assert row["body"] == text and row["source"] == source.value
        assert row["outcome"] == "sent" and row["external_id"] == "synthetic-message"
        assert not row["body_truncated"] and row["body_stored_at"] is not None
    run_db(check, monkeypatch)


@pytest.mark.parametrize("text,truncated", [("a" * 32768, False), ("界" * 11000, True), ("", False)])
def test_body_is_bounded_in_utf8_without_changing_the_send(text, truncated, monkeypatch):
    async def check(pool):
        post = AsyncMock(return_value=PostReceipt(True))
        registry = ChannelRegistry(SimpleNamespace())
        registry._channels["synthetic-channel"] = SimpleNamespace(post=post)
        await registry.post("synthetic-channel", NotifyTarget(TargetKind.CHAT, "synthetic-chat"), text,
            proactive=turn_metrics.ProactiveRef(turn_metrics.ProactiveSource.SUBSCRIPTION))
        await asyncio.gather(*turn_metrics._pending)
        row = await pool.fetchrow("SELECT * FROM proactive_deliveries")
        assert len(row["body"].encode("utf-8")) <= 32768
        assert text.startswith(row["body"]) and row["body_truncated"] is truncated
        assert post.await_args.args[1] == text
    run_db(check, monkeypatch)


def test_storage_failure_warns_and_still_sends(monkeypatch, caplog):
    async def check(pool):
        monkeypatch.setattr(store, "store_body", AsyncMock(side_effect=RuntimeError("synthetic failure")))
        post = AsyncMock(return_value=PostReceipt(True))
        registry = ChannelRegistry(SimpleNamespace())
        registry._channels["synthetic-channel"] = SimpleNamespace(post=post)
        receipt = await registry.post("synthetic-channel", NotifyTarget(TargetKind.CHAT, "synthetic-chat"),
            "Keep sending", proactive=turn_metrics.ProactiveRef(turn_metrics.ProactiveSource.SUBSCRIPTION))
        await asyncio.gather(*turn_metrics._pending)
        assert receipt.success and post.await_count == 1
        assert "proactive body" in caplog.text
    run_db(check, monkeypatch)


def test_housekeeping_clears_expired_bodies_in_batches_and_keeps_rows(monkeypatch, tmp_path):
    from looper.agents import housekeeping

    async def check(pool):
        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(days=14)
        await pool.execute("INSERT INTO proactive_deliveries "
            "(source, channel_id, chat, chat_kind, body, body_stored_at) "
            "SELECT 'subscription', 'synthetic-channel', 'synthetic-chat', 'direct', 'old', $1::timestamptz "
            "- interval '1 second' FROM generate_series(1, 5001)", cutoff)
        for at in (cutoff, now):
            await pool.execute("INSERT INTO proactive_deliveries "
                "(source, channel_id, chat, chat_kind, body, body_stored_at) "
                "VALUES ('morning_brief', 'synthetic-channel', 'synthetic-chat', 'direct', 'kept', $1)", at)
        report = await housekeeping.run_once(tmp_path, now, current_runner=None, commands=None)
        assert not report.failed_steps and report.proactive_bodies == 5001
        assert await pool.fetchval("SELECT count(*) FROM proactive_deliveries") == 5003
        assert await pool.fetchval("SELECT count(body) FROM proactive_deliveries") == 2
        assert (await housekeeping.run_once(tmp_path, now, current_runner=None, commands=None)).proactive_bodies == 0
    run_db(check, monkeypatch)


@pytest.mark.parametrize("upgrade", [False, True])
def test_body_migration_is_additive_and_repeatable(upgrade, monkeypatch):
    from looper.db import connection

    async def check(pool):
        if upgrade:
            await pool.execute("ALTER TABLE proactive_deliveries DROP COLUMN body, "
                               "DROP COLUMN body_truncated, DROP COLUMN body_stored_at")
            await pool.execute("DELETE FROM schema_migrations WHERE name='proactive_bodies'")
        await connection.ensure_schema(pool)
        await connection.ensure_schema(pool)
        row = await pool.fetchrow("INSERT INTO proactive_deliveries (source, channel_id, chat, chat_kind) "
                                 "VALUES ('gate', 'synthetic-channel', 'synthetic-chat', 'direct') RETURNING *")
        assert row["body"] is None and row["body_truncated"] is False and row["body_stored_at"] is None
        assert await pool.fetchval("SELECT count(*) FROM schema_migrations WHERE name='proactive_bodies'") == int(upgrade)
    run_db(check, monkeypatch)


def test_operator_read_filters_pages_and_reports_cleared_bodies(monkeypatch):
    from looper.agents import api_v1
    from looper.agents.ops.proactive_sends import recent
    from looper.agents import proactive_delivery
    from zoneinfo import ZoneInfo

    monkeypatch.setattr(proactive_delivery, "load_policy", lambda: SimpleNamespace(timezone=ZoneInfo("Pacific/Kiritimati")))

    async def check(pool):
        instant = datetime(2026, 11, 24, 12, tzinfo=timezone.utc)
        for number, source in enumerate(("subscription", "morning_brief", "morning_brief")):
            await store.store_body({"dedupe_key": str(number), "source": source, "channel_id": "synthetic-channel",
                "chat": "synthetic-chat", "chat_kind": "direct"}, f"Text {number}")
        await pool.execute("UPDATE proactive_deliveries SET body_stored_at=$1", instant)
        page = await recent({"source": "morning_brief", "local_date": "2026-11-25", "limit": 1})
        assert [row["body"] for row in page["deliveries"]] == ["Text 2"]
        assert page["deliveries"][0]["local_date"] == "2026-11-25"
        assert page["next_before"] == page["deliveries"][0]["id"]
        last = await recent({"source": "morning_brief", "local_date": "2026-11-25", "limit": 1,
                             "before": page["next_before"]})
        assert [row["body"] for row in last["deliveries"]] == ["Text 1"] and last["next_before"] is None
        assert not (await recent({"local_date": "2026-11-24"}))["deliveries"]
        assert await store.clear_bodies(instant + timedelta(days=14)) == 3
        cleared = await recent({})
        assert len(cleared["deliveries"]) == 3 and all(row["body"] is None for row in cleared["deliveries"])
        api_v1.validate_arguments(api_v1.find("POST", "/api/v1/proactive_sends/list").result, cleared)
    run_db(check, monkeypatch)


def test_a_slow_earlier_receipt_does_not_replace_a_later_send(monkeypatch):
    async def check(pool):
        now = datetime.now(timezone.utc)
        row = {"dedupe_key": "synthetic-retry", "source": "gate", "channel_id": "synthetic-channel",
               "chat": "synthetic-chat", "chat_kind": "direct"}
        await store.insert({**row, "outcome": "sent", "external_id": "synthetic-final", "sent_at": now})
        result = await store.insert({**row, "outcome": "rejected", "sent_at": now - timedelta(seconds=1)})
        assert result["outcome"] == "sent" and result["external_id"] == "synthetic-final"
    run_db(check, monkeypatch)


def test_an_interrupted_capture_still_expires_under_delivery_row_retention(monkeypatch):
    async def check(pool):
        now = datetime.now(timezone.utc)
        await store.store_body({"dedupe_key": "interrupted-send", "source": "subscription",
            "channel_id": "synthetic-channel", "chat": "synthetic-chat", "chat_kind": "direct"}, "Captured")
        await pool.execute("UPDATE proactive_deliveries SET body_stored_at=$1", now - timedelta(days=91))
        assert await store.clear_bodies(now - timedelta(days=14)) == 1
        assert await pool.fetchval("SELECT count(*) FROM proactive_deliveries") == 1
        assert await store.purge(now - timedelta(days=90)) == 1
    run_db(check, monkeypatch)


@pytest.mark.parametrize("kind", ["morning_brief", "user_reminder"])
@pytest.mark.parametrize("storage_fails", [False, True])
def test_brief_and_reminder_senders_use_their_record_keys_and_exact_text(
        kind, storage_fails, monkeypatch, tmp_path, lark_cli_stand_in):
    import json
    from looper.agents import attention, morning_brief, reminders
    from looper.agents.channels.allowlist import SenderAllowlist
    from looper.agents.channels.lark import LarkChannelAdapter
    from looper.db import reminders as reminder_store
    from tests.test_morning_brief_db import MONDAY, OP, _zone, _operator, _summary, _collect

    monkeypatch.setattr(chat_settings, "_metrics_enabled", False)
    monkeypatch.setattr(chat_settings, "_delivery_enabled", False)
    monkeypatch.setattr(attention, "collect", _collect(_summary("empty")))
    if storage_fails:
        monkeypatch.setattr(store, "store_body", AsyncMock(side_effect=RuntimeError("synthetic write failure")))

    async def check(pool):
        await _zone(pool)
        await _operator(pool)
        registry = ChannelRegistry(SimpleNamespace())
        registry._channels["channel-lark"] = LarkChannelAdapter(runtime_root=tmp_path,
            allowlist=SenderAllowlist("channel-lark", frozenset({OP})))
        reminder = await reminder_store.create(text="Review the application", timezone="America/Los_Angeles",
            due_at=MONDAY + timedelta(hours=1) if kind == "morning_brief" else MONDAY,
            channel_id="channel-lark", chat=OP, created_channel="web", created_message_id=None)
        if kind == "morning_brief":
            chat_settings.set_morning_brief_enabled(True)
            await morning_brief.MorningBriefService(registry, clock=lambda: MONDAY).run_once()
            assert await pool.fetchval("SELECT status FROM morning_briefs") == "sent"
            key = morning_brief._key(MONDAY.date(), "channel-lark", OP)
        else:
            await pool.execute("INSERT INTO system_settings (key,value) VALUES ('reminders.enabled','true')")
            await reminders.ReminderService(registry, clock=lambda: MONDAY).run_once()
            assert await pool.fetchval("SELECT status FROM reminders") == "delivered"
            key = f"looper-reminder-{reminder['id']}"
        await asyncio.gather(*turn_metrics._pending)
        calls = [json.loads(line) for line in lark_cli_stand_in.read_text().splitlines()]
        sends = [args for args in calls if "--markdown" in args]
        assert len(sends) == 1
        sent_text = sends[0][sends[0].index("--markdown") + 1]
        row = await store.get(pool, key)
        assert row["source"] == kind and row["outcome"] == "sent" and row["decision"] is None
        assert row["body"] == (None if storage_fails else sent_text)
        if kind == "user_reminder":
            assert row["source_ref"] == str(reminder["id"])
    run_with_channel_configs(check, monkeypatch)
