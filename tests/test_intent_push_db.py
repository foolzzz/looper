"""Intent delivery on disposable PostgreSQL, with scripted channels only."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from looper.agents import intent_push, intents, subscriptions
from looper.db import intents as store
from looper.agents.channels.base import PostOutcome, PostReceipt
from tests.db_support import DSN, run_db
from tests.leads_support import intent
from tests.subscription_support import DM, GROUP, add, lark_targets, source

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")
NOW = datetime.now(timezone.utc)


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


@pytest.mark.parametrize("outcome,crash,expected,attempts", [
    (PostOutcome.REJECTED, False, "failed", 3), (PostOutcome.UNKNOWN, False, "uncertain", 1),
    (PostOutcome.SENT, True, "uncertain", 1)])
def test_delivery_results_and_bounded_retries(monkeypatch, outcome, crash, expected, attempts):
    async def check(pool):
        env = await setup(pool, monkeypatch)
        await env.create()
        env.channels.outcome, env.channels.crash = outcome, crash
        for _ in range(5):
            await env.service.run_once()
        row = await pool.fetchrow("SELECT * FROM intent_posts")
        assert row["state"] == expected and row["attempts"] == attempts
        assert len(env.channels.posts) == attempts
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
