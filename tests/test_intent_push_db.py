"""Intent delivery on disposable PostgreSQL, with scripted channels only."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from looper.agents import chat_settings, intent_push, intents, proactive_delivery as delivery, subscriptions
from looper.db import intents as store
from looper.agents.channels.base import PostOutcome, PostReceipt
from tests.db_support import DSN, run_db
from tests.leads_support import intent
from tests.subscription_support import DM, GROUP, add, lark_targets, source

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")
NOW = datetime.now(timezone.utc)


@pytest.mark.parametrize("enabled", [None, False, True])
def test_delivery_keeps_the_exact_intent_channel_calls(monkeypatch, enabled):
    """The same pushes, progress reply and final edit fallback, including targets and keys."""
    import hashlib
    import json
    from dataclasses import asdict

    async def check(pool):
        monkeypatch.setattr(f"{__name__}.NOW", datetime(2026, 11, 22, 14, tzinfo=timezone.utc))
        if enabled is not None:
            monkeypatch.setattr(chat_settings, "_delivery_enabled", enabled)
        else:
            assert not chat_settings.delivery_enabled()
        if not enabled:
            async def unexpected_decision(*args, **kwargs):
                pytest.fail("delivery is disabled")
            monkeypatch.setattr(delivery, "decide", unexpected_decision)
        monkeypatch.setattr(chat_settings, "_timezone", "UTC")
        env = await setup(pool, monkeypatch)
        iid = await env.create(created_at=NOW)
        await env.service.run_once()
        await env.create(state="merged", merged_into=iid, created_at=NOW)
        await env.service.run_once()
        env.channels.edit_ok = False
        async with pool.acquire() as conn, conn.transaction():
            await store.transition(conn, iid, "rejected", via="dashboard", now=NOW)
        await env.service.run_once()
        await env.service.run_once()
        calls = {"posts": [(channel, asdict(target), text, kwargs)
                           for channel, target, text, kwargs in env.channels.posts],
                 "edits": [(channel, asdict(target), external, text)
                           for channel, target, external, text in env.channels.edits]}
        digest = hashlib.sha256(json.dumps(calls, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        # Captured from the parent branch before adding the delivery decision.
        assert digest == "145740b09046d7c5676d98c9dde09f0c7bb36f88c83b4720af19494544abb5f6"
        assert [tuple(row) for row in await pool.fetch(
            "SELECT kind,state,attempts,external_id FROM intent_posts ORDER BY id")] == [
                ("push", "sent", 1, "om_push_1"), ("progress", "sent", 1, "om_push_2"),
                ("final", "sent", 1, "om_push_3")]
        decisions = await pool.fetch("SELECT source,urgency,decision,sent_at IS NOT NULL AS sent "
                                     "FROM proactive_deliveries ORDER BY id")
        assert [tuple(row) for row in decisions] == ([("intent", "normal", "send", True)] * 2 if enabled else [])
    run_db(check, monkeypatch)


@pytest.mark.parametrize("priority,reason", [("normal", "within_limits"), ("urgent", "within_limits"),
                                            ("quiet", None)])
def test_intent_priority_does_not_claim_blocking_or_user_set_urgency(monkeypatch, priority, reason):
    async def check(pool):
        monkeypatch.setattr(chat_settings, "_delivery_enabled", True)
        monkeypatch.setattr(chat_settings, "_timezone", "UTC")
        env = await setup(pool, monkeypatch)
        env.service._clock = lambda: NOW.replace(hour=14)
        await pool.execute("UPDATE intent_type_versions SET priority=$1 WHERE type_key='dev_task'", priority)
        await env.create()
        await env.service.run_once()
        rows = await pool.fetch("SELECT source,urgency,chat_kind,reason,counted,sent_at IS NOT NULL "
                                "FROM proactive_deliveries")
        assert [tuple(row) for row in rows] == ([] if reason is None else [
            ("intent", "normal", "direct", reason, True, True)])
        assert len(env.channels.posts) == (0 if reason is None else 1)
    run_db(check, monkeypatch)


@pytest.mark.parametrize("blocked", ["night", "cap", "drop"])
def test_delivery_defers_or_drops_without_claiming_an_intent_send(monkeypatch, blocked):
    async def check(pool):
        monkeypatch.setattr(chat_settings, "_delivery_enabled", True)
        monkeypatch.setattr(chat_settings, "_timezone", "UTC")
        env = await setup(pool, monkeypatch)
        now = NOW.replace(hour=23 if blocked == "night" else 14)
        env.service._clock = lambda: now
        await pool.execute("UPDATE intent_type_versions SET priority='urgent' WHERE type_key='dev_task'")
        await env.create()
        if blocked == "cap":
            for n in range(5):
                await delivery.decide(delivery.Delivery(delivery.DeliveryKind.TASK_NOTICE, delivery.Urgency.NORMAL,
                                                        delivery.ChatKind.DIRECT),
                                      dedupe_key=f"earlier:{n}", channel_id="channel-lark", chat=DM,
                                      last_user_message_at=None, now=now)
        elif blocked == "drop":
            monkeypatch.setattr(delivery, "deliver", lambda *args: delivery.Decision(
                delivery.Outcome.DROP, delivery.Reason.SILENT))
        await env.service.run_once()
        # A second pass keeps the original decision, even if the clock is outside quiet hours now.
        env.service._clock = lambda: now + timedelta(hours=10) if blocked == "night" else now
        await env.service.run_once()
        assert env.channels.posts == []
        assert [tuple(row) for row in await pool.fetch("SELECT state,attempts FROM intent_posts")] == [("pending", 0)]
        rows = await pool.fetch("SELECT decision,reason,counted,digest_date,sent_at FROM proactive_deliveries "
                                "WHERE source='intent'")
        expected = {"night": ("digest", "quiet_hours"), "cap": ("digest", "daily_cap"), "drop": ("drop", "silent")}
        assert len(rows) == 1 and tuple(rows[0]) == (*expected[blocked], False,
            None if blocked == "drop" else now.date()+timedelta(days=1), None)
    run_db(check, monkeypatch)


@pytest.mark.parametrize("recent", ["target_user", "other_chat", "assistant", "old_user"])
def test_intent_delivery_grace_is_only_for_a_recent_user_in_the_target_chat(monkeypatch, recent):
    async def check(pool):
        monkeypatch.setattr(chat_settings, "_delivery_enabled", True)
        monkeypatch.setattr(chat_settings, "_timezone", "UTC")
        env = await setup(pool, monkeypatch)
        now = NOW.replace(hour=23)
        env.service._clock = lambda: now
        await env.create()
        await pool.execute("INSERT INTO chat_messages(session_id,message_id,role,content,channel,created_at) "
                           "VALUES($1,'synthetic-recent',$2,'synthetic','lark',$3)",
                           f"lark:{DM}" if recent != "other_chat" else "lark:ou_another_synthetic_user",
                           "assistant" if recent == "assistant" else "user",
                           now-timedelta(minutes=6 if recent == "old_user" else 2))
        await env.service.run_once()
        assert len(env.channels.posts) == (1 if recent == "target_user" else 0)
        row = await pool.fetchrow("SELECT reason,counted FROM proactive_deliveries")
        assert tuple(row) == ("recent_reply" if recent == "target_user" else "quiet_hours", False)
    run_db(check, monkeypatch)


def test_intent_delivery_retry_keeps_one_decision_and_updates_the_receipt(monkeypatch):
    async def check(pool):
        monkeypatch.setattr(chat_settings, "_delivery_enabled", True)
        monkeypatch.setattr(chat_settings, "_timezone", "UTC")
        env = await setup(pool, monkeypatch)
        env.service._clock = lambda: NOW.replace(hour=14)
        await env.create()
        env.channels.outcome = PostOutcome.REJECTED
        await env.service.run_once()
        assert await pool.fetchval("SELECT count(*) FROM proactive_deliveries WHERE decision='send' AND outcome='rejected'") == 1
        env.channels.outcome = PostOutcome.SENT
        env.service._clock = lambda: NOW.replace(hour=23)
        await env.service.run_once()
        await env.service.run_once()
        assert len(env.channels.posts) == 2 and env.channels.posts[0] == env.channels.posts[1]
        assert [tuple(row) for row in await pool.fetch("SELECT decision,outcome,counted,sent_at IS NOT NULL "
                                                      "FROM proactive_deliveries")] == [("send", "sent", True, True)]
    run_db(check, monkeypatch)


@pytest.mark.parametrize("metrics", [False, True])
@pytest.mark.parametrize("enabled", [False, True])
def test_intent_decision_and_channel_retry_share_one_delivery_row(monkeypatch, metrics, enabled):
    from looper.agents import turn_metrics
    from looper.agents.channels.registry import ChannelRegistry

    async def check(pool):
        monkeypatch.setattr(chat_settings, "_delivery_enabled", enabled)
        monkeypatch.setattr(chat_settings, "_metrics_enabled", metrics)
        monkeypatch.setattr(chat_settings, "_timezone", "UTC")
        env = await setup(pool, monkeypatch)
        env.service._clock = lambda: NOW.replace(hour=14)

        async def post(target, text, **kwargs):
            return await env.channels.post("channel-lark", target, text, **kwargs)

        registry = ChannelRegistry(SimpleNamespace())
        registry._channels["channel-lark"] = SimpleNamespace(post=post)
        env.service._registry = registry
        iid = await env.create()
        env.channels.outcome = PostOutcome.REJECTED
        for outcome in ("rejected", "sent"):
            await env.service.run_once()
            await asyncio.gather(*turn_metrics._pending)
            rows = await pool.fetch("SELECT * FROM proactive_deliveries")
            assert len(rows) == 1  # Body capture is independent of the metrics and delivery switches.
            if rows:
                row = rows[0]
                pid = await pool.fetchval("SELECT id FROM intent_posts WHERE intent_id=$1", iid)
                assert (row["dedupe_key"], row["source"], row["source_ref"]) == (
                    f"intent-{iid}-{pid}", "intent", str(iid))
                assert row["body"] == env.channels.posts[-1][2]
                assert (row["decision"], row["counted"]) == (("send", True) if enabled else (None, None))
                assert row["outcome"] == outcome and row["sent_at"] is not None
                assert row["external_id"] == ("om_push_2" if outcome == "sent" else None)
            env.channels.outcome = PostOutcome.SENT
        assert len(env.channels.posts) == 2 and env.channels.posts[0] == env.channels.posts[1]

    run_db(check, monkeypatch)


def test_failed_delivery_decision_waits_without_spending_a_send_attempt(monkeypatch):
    from unittest.mock import AsyncMock

    async def check(pool):
        monkeypatch.setattr(chat_settings, "_delivery_enabled", True)
        env = await setup(pool, monkeypatch)
        await env.create()
        monkeypatch.setattr(delivery, "decide", AsyncMock(side_effect=RuntimeError("synthetic failure")))
        await env.service.run_once()
        assert env.channels.posts == []
        assert [tuple(row) for row in await pool.fetch("SELECT state,attempts FROM intent_posts")] == [("pending", 0)]
    run_db(check, monkeypatch)


def test_new_evidence_for_a_digested_parent_gets_its_own_digest_decision(monkeypatch):
    async def check(pool):
        monkeypatch.setattr(chat_settings, "_delivery_enabled", True)
        monkeypatch.setattr(chat_settings, "_timezone", "UTC")
        env = await setup(pool, monkeypatch)
        now = NOW.replace(hour=23)
        env.service._clock = lambda: now
        parent = await env.create()
        await env.service.run_once()
        evidence = await env.create(state="merged", merged_into=parent)
        await env.service.run_once()
        # Both texts stay available to the briefing, with stable decisions across polls.
        env.service._clock = lambda: now + timedelta(hours=10)
        await env.service.run_once()
        rows = await pool.fetch(
            "SELECT p.intent_id,p.kind,p.state,p.attempts,p.external_id,d.decision,d.digest_date,d.sent_at "
            "FROM intent_posts p JOIN proactive_deliveries d ON d.dedupe_key='intent-' || p.intent_id || '-' || p.id "
            "ORDER BY p.id")
        assert [tuple(row) for row in rows] == [
            (parent, "push", "pending", 0, None, "digest", now.date()+timedelta(days=1), None),
            (evidence, "progress", "pending", 0, None, "digest", now.date()+timedelta(days=1), None)]
        view = await intents.intent_view(await pool.fetchrow("SELECT * FROM intents WHERE id=$1", evidence), db=pool)
        target = await intents.intent_view(await pool.fetchrow("SELECT * FROM intents WHERE id=$1", parent), db=pool)
        assert await pool.fetchval("SELECT text FROM intent_posts WHERE intent_id=$1", evidence) == (
            intent_push.progress_text(view, target, timezone="Asia/Shanghai"))
        assert env.channels.posts == []
    run_db(check, monkeypatch)


@pytest.mark.parametrize("parent_first", [False, True])
@pytest.mark.parametrize("enabled", [False, True])
def test_progress_waits_for_parent_receipt_in_either_order(monkeypatch, parent_first, enabled):
    async def check(pool):
        monkeypatch.setattr(chat_settings, "_delivery_enabled", enabled)
        monkeypatch.setattr(chat_settings, "_timezone", "UTC")
        env = await setup(pool, monkeypatch)
        env.service._clock = lambda: NOW.replace(hour=14)
        parent = await env.create()
        # Polling the evidence first must still record its SEND decision without a channel call.
        async with pool.acquire() as conn:
            await env.service._insert(conn, parent, "push", "channel-lark", DM, "Synthetic parent")
        if parent_first:
            await env.service._locked(parent)
        evidence = await env.create(state="merged", merged_into=parent)
        await env.service._locked(evidence)
        if not parent_first:
            assert env.channels.posts == []
            if enabled:
                rows = await pool.fetch(
                    "SELECT p.state,p.attempts,d.decision,d.sent_at FROM intent_posts p "
                    "JOIN proactive_deliveries d ON d.dedupe_key='intent-' || p.intent_id || '-' || p.id "
                    "WHERE p.intent_id=$1", evidence)
                assert [tuple(row) for row in rows] == [("pending", 0, "send", None)]
            else:
                assert await pool.fetchval("SELECT count(*) FROM intent_posts WHERE intent_id=$1", evidence) == 0
            await env.service._locked(parent)
        await env.service.run_once()
        await env.service.run_once()
        assert len(env.channels.posts) == 2
        progress = await pool.fetchrow("SELECT * FROM intent_posts WHERE intent_id=$1", evidence)
        assert env.channels.posts[1][3] == {
            "reply_to": "om_push_1", "idempotency_key": f"intent-{evidence}-{progress['id']}"}
        assert (progress["state"], progress["attempts"], progress["external_id"]) == ("sent", 1, "om_push_2")
        assert await pool.fetchval("SELECT count(*) FROM proactive_deliveries") == 2 * int(enabled)
        assert await pool.fetchval("SELECT count(*) FROM proactive_deliveries WHERE sent_at IS NOT NULL") == 2 * int(enabled)
    run_db(check, monkeypatch)


class Channels:
    def __init__(self):
        self.posts = []
        self.edits = []
        self.outcome = PostOutcome.SENT
        self.edit_ok = True
        self.crash = False
        self.wait = None

    async def post(self, channel, target, text, **kwargs):
        self.posts.append((channel, target, text, kwargs))
        if self.wait:
            await self.wait.wait()
        if self.crash:
            raise RuntimeError("lost answer")
        return PostReceipt(self.outcome == PostOutcome.SENT,
                           external_ids=(f"om_push_{len(self.posts)}",) if self.outcome == PostOutcome.SENT else (),
                           outcome=self.outcome)

    async def edit(self, channel, target, external_id, text):
        self.edits.append((channel, target, external_id, text))
        return PostReceipt(self.edit_ok, outcome=PostOutcome.SENT if self.edit_ok else PostOutcome.REJECTED)


async def setup(pool, monkeypatch):
    await source(pool)
    await lark_targets(pool)
    await pool.execute("UPDATE channel_configs SET config=config || jsonb_build_object('operator_sender_id', $1::text) "
                       "WHERE channel_id='channel-lark'", DM)
    await pool.execute("INSERT INTO system_settings(key,value) VALUES('intents.enabled','true')")
    await pool.execute("INSERT INTO projects(id,name,work_dir) VALUES('project-push','演示项目','/tmp/synthetic')")
    await pool.execute("INSERT INTO schedules(id,project_id,name,cron,timezone,requirement,kind,notify_channel,notify_chat) "
                       "VALUES('schedule-push','project-push','意图','*/10 * * * *','Asia/Shanghai','Read','subscription','lark',$1)", DM)
    await pool.execute("INSERT INTO subscriptions(schedule_id,source_id,output_kind,target_confirmed_at,target_confirmed_version,chat_kinds,sender_kinds,max_age_minutes,max_events,text_max_chars) "
                       "VALUES('schedule-push','stream','intents',$1,'version-test',ARRAY['p2p'],ARRAY['user'],180,500,200)", NOW)
    async def version(schedule):
        return "version-test"
    monkeypatch.setattr(subscriptions, "config_version", version)
    seq = await add(pool, "Synthetic", sent_at=NOW, kind="p2p", chat_id="oc_synthetic_dm")
    channels = Channels()
    service = intent_push.IntentPushService(channels, clock=lambda: NOW)
    async def create(**kwargs):
        return await intent(pool, [seq], schedule_id="schedule-push", project_id="project-push", **kwargs)
    return SimpleNamespace(create=create, channels=channels, service=service, seq=seq)


@pytest.mark.parametrize("blocked", ["off", "no_schedule", "unconfirmed", "changed_version", "group", "not_allowed", "quiet", "old"])
def test_push_target_and_priority_checks(monkeypatch, blocked):
    async def check(pool):
        env = await setup(pool, monkeypatch)
        iid = await env.create()
        if blocked == "off":
            await pool.execute("DELETE FROM system_settings WHERE key='intents.enabled'")
        elif blocked == "no_schedule":
            await pool.execute("UPDATE intents SET schedule_id=NULL WHERE id=$1", iid)
        elif blocked == "unconfirmed":
            await pool.execute("UPDATE subscriptions SET target_confirmed_at=NULL")
        elif blocked == "changed_version":
            await pool.execute("UPDATE subscriptions SET target_confirmed_version='old'")
        elif blocked == "group":
            await pool.execute("UPDATE schedules SET notify_chat=$1", GROUP)
        elif blocked == "not_allowed":
            await pool.execute("UPDATE channel_configs SET config=config || '{\"allowed_open_ids\":[]}'::jsonb WHERE channel_id='channel-lark'")
        elif blocked == "quiet":
            await pool.execute("UPDATE intent_type_versions SET priority='quiet' WHERE type_key='dev_task'")
        else:
            await pool.execute("UPDATE intents SET created_at=$1 WHERE id=$2", NOW-timedelta(days=2), iid)
        await env.service.run_once()
        assert env.channels.posts == []
        assert await pool.fetchval("SELECT count(*) FROM intent_posts") == 0
    run_db(check, monkeypatch)


def test_push_once_progress_replies_and_extends_expiry(monkeypatch):
    async def check(pool):
        env = await setup(pool, monkeypatch)
        iid = await env.create(flagged=True)
        await env.service.run_once()
        first = env.channels.posts[0]
        assert first[1].id == DM and not first[1].group
        assert "⚠️" in first[2] and "项目：演示项目" in first[2]
        assert first[3]["idempotency_key"].startswith(f"intent-{iid}-")
        await env.service.run_once()
        assert len(env.channels.posts) == 1
        merged = await env.create(state="merged", merged_into=iid)
        await env.service.run_once()
        assert len(env.channels.posts) == 2
        assert env.channels.posts[1][3]["reply_to"] == "om_push_1"
        assert "新进展" in env.channels.posts[1][2]
        assert await pool.fetchval("SELECT state FROM intent_posts WHERE intent_id=$1", merged) == "sent"
        assert await pool.fetchval("SELECT expires_at FROM intents WHERE id=$1", iid) == NOW+timedelta(hours=24)
    run_db(check, monkeypatch)


def test_multi_instance_and_crash_never_resend(monkeypatch):
    async def check(pool):
        env = await setup(pool, monkeypatch)
        iid = await env.create()
        env.channels.wait = asyncio.Event()
        first = asyncio.create_task(env.service.run_once())
        while not env.channels.posts:
            await asyncio.sleep(0)
        second = intent_push.IntentPushService(env.channels, clock=lambda: NOW)
        await second.run_once()
        env.channels.wait.set()
        await first
        assert len(env.channels.posts) == 1
        await pool.execute("UPDATE intent_posts SET state='sending', external_id=NULL WHERE intent_id=$1", iid)
        await second.run_once()
        assert await pool.fetchval("SELECT state FROM intent_posts WHERE intent_id=$1", iid) == "uncertain"
        assert len(env.channels.posts) == 1
    run_db(check, monkeypatch, max_size=5)


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("outcome,crash,expected,attempts", [
    (PostOutcome.REJECTED, False, "failed", 3), (PostOutcome.UNKNOWN, False, "uncertain", 1),
    (PostOutcome.SENT, True, "uncertain", 1)])
def test_delivery_results_and_bounded_retries(monkeypatch, outcome, crash, expected, attempts, enabled):
    async def check(pool):
        monkeypatch.setattr(chat_settings, "_delivery_enabled", enabled)
        monkeypatch.setattr(chat_settings, "_timezone", "UTC")
        env = await setup(pool, monkeypatch)
        env.service._clock = lambda: NOW.replace(hour=14)
        await env.create()
        env.channels.outcome, env.channels.crash = outcome, crash
        for _ in range(5):
            await env.service.run_once()
        row = await pool.fetchrow("SELECT * FROM intent_posts")
        assert row["state"] == expected and row["attempts"] == attempts
        assert len(env.channels.posts) == attempts
        assert await pool.fetchval("SELECT count(*) FROM proactive_deliveries") == int(enabled)
        assert await pool.fetchval("SELECT count(*) FROM proactive_deliveries WHERE sent_at IS NOT NULL") == int(enabled)
        assert await pool.fetchval("SELECT count(*) FROM proactive_deliveries WHERE outcome='sent'") == 0
    run_db(check, monkeypatch)


@pytest.mark.parametrize("edit_ok", [True, False])
def test_expiry_final_edit_and_fallback(monkeypatch, edit_ok):
    async def check(pool):
        env = await setup(pool, monkeypatch)
        iid = await env.create()
        await env.service.run_once()
        env.channels.edit_ok = edit_ok
        await pool.execute("UPDATE intents SET expires_at=$1 WHERE id=$2", NOW-timedelta(seconds=1), iid)
        await env.service.expire()
        assert await pool.fetchval("SELECT state FROM intents WHERE id=$1", iid) == "expired"
        assert "⌛" in env.channels.edits[0][3] and "回复" not in env.channels.edits[0][3]
        assert await pool.fetchval("SELECT edited_at FROM intent_posts WHERE kind='push'") is not None
        if not edit_ok:
            assert env.channels.posts[-1][3]["reply_to"] == "om_push_1"
            assert env.channels.posts[-1][2].startswith("⌛") and "\n" not in env.channels.posts[-1][2]
        await env.service.run_once()
        assert len(env.channels.edits) == 1
    run_db(check, monkeypatch)


def test_merged_into_rejected_is_not_pushed(monkeypatch):
    async def check(pool):
        env = await setup(pool, monkeypatch)
        iid = await env.create(state="rejected")
        await env.create(state="merged", merged_into=iid)
        await env.service.run_once()
        assert env.channels.posts == []
    run_db(check, monkeypatch)


def test_an_intent_the_check_dropped_is_never_pushed(monkeypatch):
    async def check(pool):
        env = await setup(pool, monkeypatch)
        await env.create(state="dropped", handled_via="system", check_verdict="drop", check_reason="unsupported")
        await env.service.run_once()
        assert env.channels.posts == [] and await pool.fetchval("SELECT count(*) FROM intent_posts") == 0
    run_db(check, monkeypatch)


def test_frozen_push_and_progress_texts():
    view = {"id":"I-0042", "type":{"name":"bug 排查"}, "title":"支付回调偶发 500", "summary":"合成概括",
            "confidence":"high", "sources":[{"chat_kind":"p2p", "chat":"测试", "sender":"王五",
            "sent_at":"2026-11-20T06:05:00+00:00", "link":"https://synthetic.invalid/chat"}],
            "sources_total":2, "project":{"name":"支付服务"}, "effective_workflow":None, "flagged":False,
            "state":"pending", "task":None}
    assert intent_push.push_text(view, priority="normal", timezone="Asia/Shanghai") == (
        "💡 I-0042 · bug 排查（置信度：高）\n支付回调偶发 500\n合成概括\n"
        "来源：私聊 · 王五 · 11-20 14:05（https://synthetic.invalid/chat）等 2 条\n"
        "项目：支付服务（模板：项目默认）\n"
        "回复「建」或「建 I-0042」建任务并开始；「驳回 I-0042」驳回。24 小时没处理会过期。")
    view.update(project=None, candidates=[{"id":"p", "name":"支付服务"}], flagged=True)
    text = intent_push.push_text(view, priority="urgent", timezone="Asia/Shanghai")
    assert text.startswith("🚨 I-0042 · 紧急 · bug 排查") and "候选：1. 支付服务" in text
    assert "建 I-0042 <编号>" in text and "⚠️" in text
    text = intent_push.progress_text(view, view, timezone="Asia/Shanghai")
    assert text.startswith("🔄 I-0042 新进展\n") and "回复「建 I-0042」" in text
    view.update(state="tasked", task={"id":"task-synthetic"})
    text = intent_push.progress_text(view, view, timezone="Asia/Shanghai")
    assert "（已建 task-synthetic）新进展" in text and "回复" not in text


def test_failed_push_is_not_retried_after_operator_handles_intent(monkeypatch):
    async def check(pool):
        env = await setup(pool, monkeypatch)
        iid = await env.create()
        env.channels.outcome = PostOutcome.REJECTED
        await env.service.run_once()
        await pool.execute("UPDATE intents SET state='rejected',handled_at=$2,handled_via='dashboard' WHERE id=$1", iid, NOW)
        await env.service.run_once()
        assert len(env.channels.posts) == 1
    run_db(check, monkeypatch)


def test_confirmation_is_rechecked_before_the_network_call(monkeypatch):
    async def check(pool):
        env = await setup(pool, monkeypatch)
        await env.create()
        calls = 0
        original = intent_push.target_for
        async def changed(schedule):
            nonlocal calls
            calls += 1
            allowed = await original(schedule)
            if calls == 1:
                await pool.execute("UPDATE subscriptions SET target_confirmed_at=NULL")
            return allowed
        monkeypatch.setattr(intent_push, "target_for", changed)
        await env.service.run_once()
        assert calls == 2 and env.channels.posts == []
        assert await pool.fetchval("SELECT state FROM intent_posts") == "pending"
        await env.service.run_once()
        assert env.channels.posts == []
    run_db(check, monkeypatch)


def test_registry_edits_via_lark_and_telegram_without_a_network(tmp_path):
    from unittest.mock import AsyncMock
    from looper.agents.channels.lark import LarkChannelAdapter
    from looper.agents.channels.telegram import TelegramChannelAdapter
    from looper.agents.channels.registry import ChannelRegistry
    from looper.agents.subscription_runs import push_target

    async def check():
        registry = ChannelRegistry(SimpleNamespace())
        target = push_target("lark", DM)
        assert (await registry.edit("channel-lark", target, "om_1", "final")).result == PostOutcome.REJECTED
        lark = object.__new__(LarkChannelAdapter)
        lark._runtime_root = tmp_path
        lark._transport = SimpleNamespace(edit=AsyncMock())
        registry._channels["channel-lark"] = lark
        assert (await registry.edit("channel-lark", target, "om_1", "final")).success
        lark._transport.edit.assert_awaited_once_with("om_1", markdown="final")
        telegram = object.__new__(TelegramChannelAdapter)
        telegram._bus = None
        telegram._app = SimpleNamespace(bot=SimpleNamespace(edit_message_text=AsyncMock()))
        registry._channels["channel-telegram"] = telegram
        assert (await registry.edit("channel-telegram", push_target("telegram", "10001"), "42", "final")).success
        telegram._app.bot.edit_message_text.assert_awaited_once_with(chat_id=10001, message_id=42, text="final")
        assert (await registry.edit("channel-telegram", target, "invalid", "final")).result == PostOutcome.REJECTED
    asyncio.run(check())


@pytest.mark.parametrize("edit_ok", [True, False])
def test_handled_push_is_finalized_once_despite_type_renames(monkeypatch, edit_ok):
    """A final edit should happen once; renaming the type re-edits (or re-posts the fallback) every time."""
    async def check(pool):
        env = await setup(pool, monkeypatch)
        iid = await env.create()
        await env.service.run_once()
        assert len(env.channels.posts) == 1
        env.channels.edit_ok = edit_ok
        async with pool.acquire() as conn, conn.transaction():
            await store.transition(conn, iid, "rejected", via="dashboard", now=NOW)
        await env.service.run_once()
        for name in ("改名一", "改名二", "改名三"):
            await intents.type_save("dev_task", name=name)
            await env.service.run_once()
        finals = [p for p in env.channels.posts if p[2].startswith("❌")]
        assert len(env.channels.edits) == 1, f"{len(env.channels.edits)} edits of one handled push"
        assert len(finals) == (0 if edit_ok else 1), f"{len(finals)} fallback posts"
    run_db(check, monkeypatch)


def test_handled_push_is_not_reedited_for_new_sources(monkeypatch):
    """Slice b adds a merged row's sources to its target: a tasked target's push is re-edited each time."""
    async def check(pool):
        env = await setup(pool, monkeypatch)
        iid = await env.create()
        await env.service.run_once()
        async with pool.acquire() as conn, conn.transaction():
            await store.transition(conn, iid, "rejected", via="dashboard", now=NOW)
        await env.service.run_once()
        assert len(env.channels.edits) == 1
        seq = await add(pool, "Synthetic follow-up", sent_at=NOW, kind="p2p", chat_id="oc_synthetic_dm")
        await pool.execute("INSERT INTO intent_sources(intent_id,event_seq) VALUES($1,$2)", iid, seq)
        await env.service.run_once()
        assert len(env.channels.edits) == 1, f"{len(env.channels.edits)} edits"
    run_db(check, monkeypatch)




def test_expired_then_tasked_edits_once_per_transition(monkeypatch, tmp_path):
    from unittest.mock import AsyncMock

    async def check(pool):
        env = await setup(pool, monkeypatch)
        iid = await env.create()
        await pool.execute("UPDATE projects SET work_dir=$1 WHERE id='project-push'", str(tmp_path))
        await env.service.run_once()
        async with pool.acquire() as conn, conn.transaction():
            await store.transition(conn, iid, "expired", via="system", now=NOW)
        await env.service.run_once()
        await intents.build(iid, via="dashboard", now=NOW+timedelta(seconds=1),
                            platform=SimpleNamespace(bus=SimpleNamespace(publish=AsyncMock())))
        env.service._clock = lambda: NOW+timedelta(seconds=2)
        await env.service.run_once()
        await env.service.run_once()
        assert len(env.channels.edits) == 2
        assert env.channels.edits[0][3].startswith("⌛")
        assert env.channels.edits[1][3].startswith("✅")
        processed = []
        async def remember(number):
            processed.append(number)
        monkeypatch.setattr(env.service, "_locked", remember)
        await env.service.run_once()
        assert processed == []
    run_db(check, monkeypatch)


@pytest.mark.parametrize("legacy", [False, True])
def test_failed_push_expires_from_creation_even_without_a_receipt(monkeypatch, legacy):
    async def check(pool):
        env = await setup(pool, monkeypatch)
        iid = await env.create(created_at=NOW)
        env.channels.outcome = PostOutcome.REJECTED
        if legacy:
            await pool.execute("INSERT INTO intent_posts(intent_id,kind,channel_id,chat,text,state,attempts) "
                               "VALUES($1,'push','channel-lark',$2,'Synthetic','failed',3)", iid, DM)
        else:
            await env.service.run_once()
            assert await pool.fetchval("SELECT expires_at FROM intents WHERE id=$1", iid) == NOW+timedelta(hours=24)
        env.service._clock = lambda: NOW+timedelta(hours=24, seconds=1)
        await env.service.expire()
        assert await pool.fetchval("SELECT state FROM intents WHERE id=$1", iid) == "expired"
        assert env.channels.edits == []
        assert len(env.channels.posts) == (0 if legacy else 1)
    run_db(check, monkeypatch)


@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize("recovery", ["edit", "reply", "exhausted", "unknown"])
def test_final_notice_retries_with_backoff_and_reports_exhaustion(monkeypatch, legacy, recovery):
    from looper.agents import attention

    async def check(pool):
        env = await setup(pool, monkeypatch)
        iid = await env.create(created_at=NOW)
        await env.service.run_once()
        await pool.execute("UPDATE intents SET expires_at=$1 WHERE id=$2", NOW-timedelta(seconds=1), iid)
        env.channels.edit_ok = False
        env.channels.outcome = PostOutcome.UNKNOWN if recovery == "unknown" else PostOutcome.REJECTED
        await env.service.expire()
        if legacy:
            # The released writer marked the push edited even when both calls failed, then exhausted replies.
            await pool.execute("DELETE FROM intent_final_notices")
            await pool.execute("UPDATE intent_posts SET edited_at=$1 WHERE kind='push'", NOW)
            await pool.execute("UPDATE intent_posts SET attempts=3 WHERE kind='final'")
            await env.service.run_once()
        attempts = 1
        while attempts < 3:
            job = await pool.fetchrow("SELECT * FROM intent_final_notices")
            assert job["state"] == "pending" and job["attempts"] == attempts
            assert job["next_attempt_at"]-job["updated_at"] == timedelta(seconds=30*2**(attempts-1))
            posts, edits = len(env.channels.posts), len(env.channels.edits)
            # A fresh instance must honor the persisted backoff too.
            env.service = intent_push.IntentPushService(env.channels, clock=lambda: job["next_attempt_at"]-timedelta(microseconds=1))
            await env.service.run_once()
            assert (len(env.channels.posts), len(env.channels.edits)) == (posts, edits)
            env.service._clock = lambda: job["next_attempt_at"]
            if recovery == "edit":
                env.channels.edit_ok = True
            elif recovery == "reply":
                env.channels.outcome = PostOutcome.SENT
            await env.service.run_once()
            attempts += 1
            if recovery in ("edit", "reply"):
                break
        job = await pool.fetchrow("SELECT * FROM intent_final_notices")
        assert job["state"] == ("sent" if recovery in ("edit", "reply") else "failed")
        assert job["failure_reason"] == (None if job["state"] == "sent" else "final_notice_retry_exhausted")
        if recovery == "unknown":
            # Edits are idempotent; an uncertain fallback reply must never be sent twice.
            assert len(env.channels.posts) == 2
        posts, edits = len(env.channels.posts), len(env.channels.edits)
        env.service._clock = lambda: NOW+timedelta(days=2)
        await env.service.run_once()
        assert (len(env.channels.posts), len(env.channels.edits)) == (posts, edits)
        summary = await attention.collect()
        problems = next(s for s in summary["sections"] if s["kind"] == "deliveries")["items"]
        failures = [p for p in problems if p["facts"].get("reason") == "final_notice_retry_exhausted"]
        assert len(failures) == int(job["state"] == "failed")
        if failures:
            assert failures[0]["ref"] == f"I-{iid:04d}" and failures[0]["href"] == "#intents"
            assert "重试" in failures[0]["title"]  # visible on the existing dashboard too
            assert "重试" in attention.summary_text(summary)
        assert all(not target.group and target.id == DM for _, target, *_ in env.channels.posts+env.channels.edits)
    run_db(check, monkeypatch)


def test_final_notice_migration_is_additive_and_idempotent(monkeypatch):
    from looper.db import connection
    from looper.db.migrations import intent_final_notices as migration

    async def check(pool):
        env = await setup(pool, monkeypatch)
        iid = await env.create(created_at=NOW)
        env.channels.outcome = PostOutcome.REJECTED
        await env.service.run_once()
        await pool.execute("UPDATE intents SET expires_at=NULL WHERE id=$1", iid)
        before = [dict(r) for r in await pool.fetch("SELECT * FROM intents")]
        posts = [dict(r) for r in await pool.fetch("SELECT * FROM intent_posts")]
        await pool.execute("DROP TABLE intent_final_notices")
        assert await migration.pending(pool)
        await connection.ensure_schema(pool)
        await connection.ensure_schema(pool)
        assert not await migration.pending(pool)
        assert [dict(r) for r in await pool.fetch("SELECT * FROM intents")] == before
        assert [dict(r) for r in await pool.fetch("SELECT * FROM intent_posts")] == posts
    run_db(check, monkeypatch)


@pytest.mark.parametrize("blocked", ["off", "unconfirmed", "group"])
def test_final_notice_retry_rechecks_the_operator_dm_guard(monkeypatch, blocked):
    async def check(pool):
        env = await setup(pool, monkeypatch)
        iid = await env.create(created_at=NOW)
        await env.service.run_once()
        env.channels.edit_ok = False
        env.channels.outcome = PostOutcome.REJECTED
        await pool.execute("UPDATE intents SET expires_at=$1 WHERE id=$2", NOW-timedelta(seconds=1), iid)
        await env.service.expire()
        if blocked == "off":
            await pool.execute("DELETE FROM system_settings WHERE key='intents.enabled'")
        elif blocked == "unconfirmed":
            await pool.execute("UPDATE subscriptions SET target_confirmed_at=NULL")
        else:
            await pool.execute("UPDATE schedules SET notify_chat=$1", GROUP)
        env.service._clock = lambda: NOW+timedelta(hours=1)
        await env.service.run_once()
        assert len(env.channels.edits) == 1 and len(env.channels.posts) == 2
        assert await pool.fetchval("SELECT attempts FROM intent_final_notices") == 1
    run_db(check, monkeypatch)


@pytest.mark.parametrize("legacy", [False, True])
def test_new_transition_does_not_retry_the_old_expiry_reply(monkeypatch, legacy):
    async def check(pool):
        env = await setup(pool, monkeypatch)
        iid = await env.create(created_at=NOW)
        await env.service.run_once()
        env.channels.edit_ok = False
        env.channels.outcome = PostOutcome.REJECTED
        await pool.execute("UPDATE intents SET expires_at=$1 WHERE id=$2", NOW-timedelta(seconds=1), iid)
        await env.service.expire()
        if legacy:
            await pool.execute("DELETE FROM intent_final_notices")
            await pool.execute("UPDATE intent_posts SET edited_at=$1 WHERE kind='push'", NOW)
        await pool.execute("INSERT INTO tasks(id,project_id,requirement) VALUES('task-final','project-push','Synthetic')")
        async with pool.acquire() as conn, conn.transaction():
            await store.transition(conn, iid, "tasked", via="dashboard", now=NOW+timedelta(hours=1), task_id="task-final")
        env.service._clock = lambda: NOW+timedelta(hours=1)
        env.channels.outcome = PostOutcome.SENT
        await env.service.run_once()
        assert env.channels.posts[-1][2].startswith("✅")
        posts, edits = len(env.channels.posts), len(env.channels.edits)
        env.service._clock = lambda: NOW+timedelta(hours=2)
        await env.service.run_once()
        assert (len(env.channels.posts), len(env.channels.edits)) == (posts, edits)
        assert await pool.fetchval("SELECT count(*) FROM intent_posts WHERE kind='final' AND state='sent'") == 1
    run_db(check, monkeypatch)


def test_successful_legacy_transition_does_not_revive_an_older_failed_reply(monkeypatch):
    async def check(pool):
        env = await setup(pool, monkeypatch)
        iid = await env.create(created_at=NOW)
        await env.service.run_once()
        await pool.execute("INSERT INTO tasks(id,project_id,requirement) VALUES('task-final','project-push','Synthetic')")
        async with pool.acquire() as conn, conn.transaction():
            await store.transition(conn, iid, "tasked", via="dashboard", now=NOW+timedelta(hours=1), task_id="task-final")
        await pool.execute("UPDATE intent_posts SET edited_at=$1 WHERE kind='push'", NOW+timedelta(hours=1))
        await pool.execute("INSERT INTO intent_posts(intent_id,kind,channel_id,chat,text,state,attempts,updated_at) "
                           "VALUES($1,'final','channel-lark',$2,'Synthetic old expiry','failed',3,$3)", iid, DM, NOW)
        env.service._clock = lambda: NOW+timedelta(hours=2)
        await env.service.run_once()
        assert len(env.channels.posts) == 1 and env.channels.edits == []
        assert await pool.fetchval("SELECT count(*) FROM intent_final_notices") == 0
    run_db(check, monkeypatch)


@pytest.mark.parametrize("guard", ["before_processing", "before_edit"])
@pytest.mark.parametrize("attempts,delay", [(0, 30), (2, 60), (6, 300)])
def test_invalid_final_notice_target_defers_without_spending_attempts(monkeypatch, guard, attempts, delay):
    async def check(pool):
        env = await setup(pool, monkeypatch)
        iid = await env.create(created_at=NOW)
        await env.service.run_once()
        async with pool.acquire() as conn, conn.transaction():
            await store.transition(conn, iid, "rejected", via="dashboard", now=NOW)
        await pool.execute("INSERT INTO intent_final_notices(push_id,handled_at,attempts,next_attempt_at,updated_at) "
                           "SELECT id,$1,$2,$1,$1 FROM intent_posts WHERE intent_id=$3", NOW, attempts, iid)
        original = intent_push.target_for
        calls = 0
        async def invalid(schedule):
            nonlocal calls
            calls += 1
            return await original(schedule) if guard == "before_edit" and calls == 1 else None
        monkeypatch.setattr(intent_push, "target_for", invalid)
        await env.service.run_once()
        notice = await pool.fetchrow("SELECT * FROM intent_final_notices")
        assert notice["next_attempt_at"] == NOW+timedelta(seconds=delay)
        assert notice["state"] == "pending" and notice["attempts"] == attempts
        assert len(env.channels.posts) == 1 and env.channels.edits == []
        # A restarted poller must not select the blocked notice on every ten-second tick.
        service = intent_push.IntentPushService(env.channels, clock=lambda: NOW+timedelta(seconds=10))
        calls = 0
        await service.run_once()
        assert calls == 0
        service._clock = lambda: notice["next_attempt_at"]
        monkeypatch.setattr(intent_push, "target_for", original)
        await service.run_once()
        assert await pool.fetchval("SELECT state FROM intent_final_notices") == ("sent" if attempts < 3 else "failed")
    run_db(check, monkeypatch)


@pytest.mark.parametrize("phase", ["push", "final"])
def test_push_backlog_drains_oldest_first_over_passes(monkeypatch, phase):
    async def check(pool):
        env = await setup(pool, monkeypatch)
        ids = [await env.create(created_at=NOW) for _ in range(51)]
        if phase == "final":
            await env.service.run_once()
            await env.service.run_once()
            await pool.execute("UPDATE intents SET state='rejected',handled_at=$1,handled_via='dashboard'", NOW)
        for count in (50, 51):
            await env.service.run_once()
            if phase == "push":
                done = await pool.fetch("SELECT intent_id FROM intent_posts WHERE kind='push' ORDER BY intent_id")
                assert len(env.channels.posts) == count
            else:
                done = await pool.fetch("SELECT intent_id FROM intent_posts WHERE edited_at IS NOT NULL ORDER BY intent_id")
                assert len(env.channels.edits) == count
            assert [row["intent_id"] for row in done] == ids[:count]
        await env.service.run_once()
        assert len(env.channels.posts if phase == "push" else env.channels.edits) == 51
    run_db(check, monkeypatch)


def test_expiry_backlog_drains_oldest_first_over_passes(monkeypatch):
    async def check(pool):
        env = await setup(pool, monkeypatch)
        ids = [await env.create(created_at=NOW-timedelta(days=2)) for _ in range(51)]
        for count in (50, 51, 51):
            await env.service.expire()
            done = await pool.fetch("SELECT id FROM intents WHERE state='expired' ORDER BY id")
            assert [row["id"] for row in done] == ids[:count]
        assert env.channels.posts == [] and env.channels.edits == []
    run_db(check, monkeypatch)


@pytest.mark.parametrize("enabled", [False, True])
def test_followed_group_intents_go_only_through_the_delivery_layer(monkeypatch, enabled):
    async def check(pool):
        monkeypatch.setattr(chat_settings, "_delivery_enabled", enabled)
        monkeypatch.setattr(chat_settings, "_timezone", "UTC")
        env = await setup(pool, monkeypatch)
        now = NOW.replace(hour=14)
        env.service._clock = lambda: now
        ids = [await env.create(via="followed_group", created_at=now) for _ in range(6)]
        await env.service.run_once()
        if enabled:
            assert len(env.channels.posts) == 5
            decisions = await pool.fetch("SELECT urgency,decision,reason FROM proactive_deliveries ORDER BY id")
            assert [tuple(r) for r in decisions] == [("normal", "send", "within_limits")] * 5 + [
                ("normal", "digest", "daily_cap")]
            assert all(target.id == DM for _, target, _, _ in env.channels.posts)
            # A deferred pending push and progress must remain held if the delivery layer is later disabled.
            monkeypatch.setattr(chat_settings, "_delivery_enabled", False)
            await env.create(via="followed_group", state="merged", merged_into=ids[0], created_at=now)
            await env.service.run_once()
            assert len(env.channels.posts) == 5
            assert await pool.fetchval("SELECT count(*) FROM intent_posts WHERE kind='progress'") == 0
        else:
            assert await pool.fetchval("SELECT count(*) FROM intent_posts") == 0
            assert await pool.fetchval("SELECT count(*) FROM proactive_deliveries") == 0
            assert await pool.fetchval("SELECT count(*) FROM intents WHERE expires_at IS NOT NULL") == 6
            await env.create(via="subscription", created_at=now)
            await env.service.run_once()
            assert len(env.channels.posts) == 1 and env.channels.posts[0][1].id == DM
    run_db(check, monkeypatch)
