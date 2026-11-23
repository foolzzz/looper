"""Digest pushes use the shared delivery policy on real PostgreSQL, with synthetic channels and processor."""

import asyncio
from datetime import datetime, timedelta, timezone
from dataclasses import replace
from types import SimpleNamespace

import pytest

from looper.agents import chat_settings, proactive_delivery as delivery, turn_metrics
from looper.agents.channels.registry import ChannelRegistry
from looper.agents.schedule_notify import Kind
from looper.agents.bus import MessageBus
from looper.agents.message import MessageEnvelope, MessageType, channel_address, manager_address
from looper.db import connection, proactive_deliveries, schedules as db_schedules, subscriptions as db_subs
from tests.db_support import DSN, run_db
from tests.bridge_schema_support import structure
from tests.schedule_support import TELEGRAM_CHAT, allow_telegram, with_sender
from tests.subscription_support import (DM, GROUP, add, confirm, cursor, fire, minutes_ago, scenario,
                                        scheduler_with, started)

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")
DAY = datetime(2026, 1, 15, 12, tzinfo=timezone.utc)
TEXT = "每日摘要 🍉\n第一条  保留空格\n第二条"


@pytest.fixture(autouse=True)
def _user_timezone_utc(monkeypatch):
    """The user's timezone as the turn context settings keep it in memory: UTC here."""
    monkeypatch.setattr(chat_settings, "_timezone", "UTC")


async def configured(env, *, channel="lark", chat=DM):
    if channel == "telegram":
        await allow_telegram(env)
    sid = await started(env, target={"channel": channel, "chat": chat}, subscription={"output_header": ""})
    await confirm(env, sid)
    top = await add(env.pool, "synthetic event", sent_at=minutes_ago(env, 1))
    return sid, top


@pytest.mark.parametrize("metrics_enabled,delivery_enabled", [(True, True), (True, False),
                                                             (False, True), (False, False)])
def test_registry_metrics_share_the_digest_decision(monkeypatch, tmp_path, metrics_enabled, delivery_enabled):
    async def check(env, poster, fake):
        env.clock.now = DAY
        sid, top = await configured(env)
        monkeypatch.setattr(chat_settings, "_metrics_enabled", metrics_enabled)
        chat_settings.set_delivery_enabled(delivery_enabled)
        registry = ChannelRegistry(SimpleNamespace())

        async def post(target, text, **kwargs):
            return await poster("channel-lark", target, text, **kwargs)

        registry._channels["channel-lark"] = SimpleNamespace(post=post)
        scheduler_with(env, poster=registry.post)
        run = await fire(env, sid)
        await asyncio.gather(*turn_metrics._pending)
        [sent] = poster.sent
        assert (sent["text"], sent["target"].id, sent["key"]) == (TEXT, DM, run["id"])
        assert run["stream"]["outcome"] == "delivered" and await cursor(env, sid) == top
        rows = await env.pool.fetch("SELECT * FROM proactive_deliveries")
        assert len(rows) == int(metrics_enabled or delivery_enabled)
        if rows:
            [row] = rows
            assert row["dedupe_key"] == (f"subscription_run:{run['id']}" if delivery_enabled else run["id"])
            assert row["source"] == "subscription" and row["outcome"] == "sent"
            assert row["external_id"] == "om_1" and row["sent_at"] is not None
            assert row["decision"] == ("send" if delivery_enabled else None)
            assert row["urgency"] == ("digest" if delivery_enabled else None)
            assert row["counted"] == (True if delivery_enabled else None)
            assert row["schedule_id"] == (sid if delivery_enabled else None)

    scenario(check, monkeypatch, tmp_path, reply=TEXT)


@pytest.mark.parametrize("explicit_off,channel,chat", [(False, "lark", DM), (True, "lark", DM),
                                                       (False, "telegram", TELEGRAM_CHAT)])
def test_disabled_digest_keeps_exact_posts_and_existing_writes(monkeypatch, tmp_path, explicit_off, channel, chat):
    async def check(env, poster, fake):
        sid, top = await configured(env, channel=channel, chat=chat)
        if explicit_off:
            chat_settings.set_delivery_enabled(False)

        async def forbidden(*args, **kwargs):
            pytest.fail("disabled delivery must not decide or record a receipt")

        monkeypatch.setattr(delivery, "decide", forbidden)
        monkeypatch.setattr(proactive_deliveries, "record_receipt", forbidden)
        run = await fire(env, sid)
        [post] = poster.sent
        assert (post["channel_id"], post["target"].id, post["text"].encode(), post["key"]) == (
            f"channel-{channel}", chat, TEXT.encode(), run["id"])
        assert (run["status"], run["stream"]["outcome"], run["stream"]["output_text"]) == (
            "completed", "delivered", TEXT)
        [part] = await db_subs.list_deliveries(run["id"])
        assert (part["state"], part["attempts"], part["text"], part["error"]) == ("sent", 1, TEXT, None)
        [notice] = await db_schedules.list_notifications(sid, limit=20, pool=env.pool)
        assert (notice["status"], notice["channel"], notice["chat"], notice["text"]) == ("sent", channel, chat, TEXT)
        assert await cursor(env, sid) == top
        assert await env.pool.fetchval("SELECT count(*) FROM proactive_deliveries") == 0

    scenario(check, monkeypatch, tmp_path, reply=TEXT)


@pytest.mark.parametrize("chat,hour,cap,recent,reason", [
    (DM, 12, False, False, "within_limits"),
    (GROUP, 12, True, False, "within_limits"),
    (DM, 23, False, False, "quiet_hours"),
    (GROUP, 23, False, False, "quiet_hours"),
    (DM, 12, True, False, "daily_cap"),
    (DM, 23, True, True, "recent_reply"),
    (GROUP, 23, True, "history", "recent_reply"),
    (DM, 23, True, "group", "quiet_hours"),
    (DM, 23, True, "expired", "quiet_hours"),
])
def test_digest_policy_sends_or_preserves_the_whole_output(monkeypatch, tmp_path, chat, hour, cap, recent, reason):
    async def check(env, poster, fake):
        env.clock.now = DAY.replace(hour=hour)
        sid, top = await configured(env, chat=chat)
        chat_settings.set_delivery_enabled(True)
        if cap:
            for n in range(5):
                await delivery.decide(delivery.Delivery(delivery.DeliveryKind.TASK_NOTICE, delivery.Urgency.NORMAL,
                                                        delivery.ChatKind.DIRECT),
                                      dedupe_key=f"earlier:{n}", channel_id="channel-lark", chat=chat,
                                      last_user_message_at=None, now=DAY)
        async def during():
            # A message in another chat must not grant this chat the recent-reply exemption.
            if recent == "history":
                await env.pool.execute("INSERT INTO chat_messages (session_id, message_id, role, channel, created_at) "
                                       "VALUES ($1, 'recent', 'user', 'lark', $2)",
                                       f"lark:{chat}", env.clock.now - timedelta(minutes=1))
                return
            bus = MessageBus(env.runtime_root)
            bus.subscribe_topic(MessageType.USER_MESSAGE, env.scheduler.observe_user)
            envelope = MessageEnvelope.create(
                source=channel_address("lark"), target=manager_address("manager"),
                message_type=MessageType.USER_MESSAGE,
                payload={"text": "synthetic reply", "channel_context": {"chat_id": "oc_native_dm",
                         "chat_type": "group" if recent == "group" else "p2p",
                         "sender_id": chat if recent else "ou_another"}})
            at = env.clock.now - timedelta(minutes=6 if recent == "expired" else 1)
            await bus.publish(replace(envelope, timestamp=at.isoformat()))
            await bus.shutdown()
        fake.during = during
        run = await fire(env, sid)
        sent = reason in {"within_limits", "recent_reply"}
        assert len(poster.sent) == int(sent)
        assert (run["status"], run["stream"]["outcome"], run["stream"]["output_text"]) == (
            "completed", "delivered" if sent else "dashboard_only", TEXT)
        [row] = await env.pool.fetch("SELECT * FROM proactive_deliveries WHERE source = 'subscription'")
        assert (row["urgency"], row["chat_kind"], row["decision"], row["reason"], row["counted"],
                row["sent_at"] is not None, row["schedule_id"]) == (
            "digest", "group" if chat == GROUP else "direct", "send" if sent else "digest", reason,
            reason == "within_limits" and chat == DM, sent, sid)
        assert row["digest_date"] == (None if sent else (DAY + timedelta(days=1)).date())
        assert await cursor(env, sid) == top
        assert len(fake.calls) == 1
        # A deferred output is not processed or posted again on the next firing.
        await fire(env, sid)
        assert len(fake.calls) == 1 and len(poster.sent) == int(sent)

    scenario(check, monkeypatch, tmp_path, reply=TEXT)


def test_segment_retries_share_one_digest_decision_and_one_daily_slot(monkeypatch, tmp_path):
    async def check(env, poster, fake):
        env.clock.now = DAY
        sid, _ = await configured(env)
        chat_settings.set_delivery_enabled(True)
        poster.fail_segments = {1: 1}
        run = await fire(env, sid)
        assert run["stream"]["outcome"] == "delivered" and poster.calls == 3 and len(poster.sent) == 2
        [row] = await env.pool.fetch("SELECT * FROM proactive_deliveries")
        assert row["urgency"] == "digest" and row["counted"] and row["sent_at"] is not None
        assert [m["key"] for m in poster.sent] == [run["id"], f"{run['id']}-2"]

    scenario(check, monkeypatch, tmp_path, reply=("x" * 99 + "\n") * 60)


def test_restart_reuses_the_original_decision_after_quiet_hours_begin(monkeypatch, tmp_path):
    async def check(env, poster, fake):
        env.clock.now = DAY
        sid, top = await configured(env)
        chat_settings.set_delivery_enabled(True)
        poster.fail_with = "synthetic refusal"
        run = await fire(env, sid)
        assert run["status"] == "failed" and poster.calls == 3
        await env.pool.execute("UPDATE schedule_runs SET status = 'running', finished_at = NULL, "
                               "runner_pid = 999999, runner_started = 'dead server' WHERE id = $1", run["id"])
        await env.pool.execute("UPDATE subscription_runs SET outcome = 'delivering' WHERE run_id = $1", run["id"])
        # A process stopped before exhausting the segment's attempts.
        await env.pool.execute("UPDATE subscription_deliveries SET attempts = 1 WHERE run_id = $1", run["id"])
        env.clock.now = DAY.replace(hour=23)
        poster.fail_with = None
        scheduler_with(env, poster=poster)
        await env.scheduler.tick()
        await env.scheduler.idle()
        assert (await db_subs.get_run(run["id"]))["outcome"] == "delivered"
        [row] = await env.pool.fetch("SELECT * FROM proactive_deliveries")
        assert row["reason"] == "within_limits" and row["sent_at"] is not None
        assert poster.calls == 4 and await cursor(env, sid) == top

    scenario(check, monkeypatch, tmp_path, reply=TEXT)


@pytest.mark.parametrize("failure", ["decision", "receipt", "channel"])
def test_delivery_failures_do_not_lose_or_duplicate_the_output(monkeypatch, tmp_path, failure):
    async def check(env, poster, fake):
        env.clock.now = DAY
        sid, top = await configured(env)
        chat_settings.set_delivery_enabled(True)

        async def broken(*args, **kwargs):
            raise RuntimeError("synthetic database failure")

        if failure == "decision":
            monkeypatch.setattr(delivery, "decide", broken)
        elif failure == "receipt":
            monkeypatch.setattr(proactive_deliveries, "record_receipt", broken)
        else:
            poster.break_after = 1
        run = await fire(env, sid)
        assert run["stream"]["output_text"] == TEXT
        if failure == "decision":
            assert run["status"] == "failed" and poster.calls == 0 and await cursor(env, sid) < top
        else:
            assert run["stream"]["outcome"] == ("delivered" if failure == "receipt" else "uncertain")
            assert poster.calls == 1 and await cursor(env, sid) == top
            [row] = await env.pool.fetch("SELECT * FROM proactive_deliveries")
            assert row["sent_at"] is None

    scenario(check, monkeypatch, tmp_path, reply=TEXT)


@pytest.mark.parametrize("case", ["quiet", "unconfirmed", "never", "alert"])
def test_ineligible_outputs_and_run_alerts_bypass_delivery(monkeypatch, tmp_path, case):
    async def check(env, poster, fake):
        env.clock.now = DAY.replace(hour=23)
        sid, _ = await configured(env)
        chat_settings.set_delivery_enabled(True)

        async def forbidden(*args, **kwargs):
            pytest.fail("only eligible digest pushes use delivery")

        monkeypatch.setattr(delivery, "decide", forbidden)
        if case == "quiet":
            fake.reply = "SKIP"
        elif case == "unconfirmed":
            await env.pool.execute("UPDATE subscriptions SET target_confirmed_at = NULL WHERE schedule_id = $1", sid)
        elif case == "never":
            await env.pool.execute("UPDATE schedules SET notify = 'never' WHERE id = $1", sid)
        else:
            sender = with_sender(env)
            schedule = await db_schedules.get_schedule(sid)
            assert await env.scheduler._notifier._deliver(schedule, Kind.FAILURES_ALERT, TEXT) == "sent"
            assert sender.sent == [("channel-lark", DM, TEXT)]
            return
        await fire(env, sid)
        assert poster.calls == 0
        assert await env.pool.fetchval("SELECT count(*) FROM proactive_deliveries") == 0

    scenario(check, monkeypatch, tmp_path, reply=TEXT)


def test_digest_urgency_upgrade_preserves_decisions_and_matches_fresh_schema(monkeypatch):
    async def check(pool):
        await delivery.decide(delivery.Delivery(delivery.DeliveryKind.TASK_NOTICE, delivery.Urgency.NORMAL,
                                               delivery.ChatKind.DIRECT),
                              dedupe_key="existing:synthetic", channel_id="channel-lark", chat=DM,
                              last_user_message_at=None, now=DAY)
        async with pool.acquire() as conn:
            expected = await structure(conn)
            await conn.execute("ALTER TABLE proactive_deliveries DROP CONSTRAINT proactive_deliveries_urgency_check, "
                               "ADD CONSTRAINT proactive_deliveries_urgency_check "
                               "CHECK (urgency IN ('normal', 'blocking', 'user_set'))")
            await conn.execute("DELETE FROM schema_migrations WHERE name IN ('proactive_digest', 'proactive_digest_online')")
            assert await connection.schema_change_pending(conn)
        await connection.ensure_schema(pool)
        await connection.ensure_schema(pool)
        async with pool.acquire() as conn:
            assert not await connection.schema_change_pending(conn)
            assert await structure(conn) == expected
            assert await conn.fetchval("SELECT convalidated FROM pg_constraint WHERE "
                                       "conrelid = 'proactive_deliveries'::regclass "
                                       "AND conname = 'proactive_deliveries_urgency_check'")
        await delivery.decide(delivery.Delivery(delivery.DeliveryKind.SUBSCRIPTION, delivery.Urgency.DIGEST,
                                               delivery.ChatKind.DIRECT),
                              dedupe_key="subscription_run:synthetic", channel_id="channel-lark", chat=DM,
                              last_user_message_at=None, now=DAY)
        assert await pool.fetchval("SELECT urgency FROM proactive_deliveries WHERE dedupe_key = 'existing:synthetic'") == "normal"
        assert await pool.fetchval("SELECT urgency FROM proactive_deliveries WHERE source = 'subscription'") == "digest"

    run_db(check, monkeypatch)
