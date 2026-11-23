"""Intent pushes and typed/button races on disposable PostgreSQL; no chat API is contacted."""

import asyncio
import json
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from looper.agents import card_actions as cards, intent_cards, intent_push, intent_routing, intents
from looper.agents.channels.base import PostOutcome, PostReceipt
from looper.agents.message import AgentAddress, MessageEnvelope, MessageType, manager_address
from looper.db import card_actions as db_cards
from tests import leads_support as leads, subscription_support as stream
from tests.db_support import DSN, run_db

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")
NOW = datetime.now(timezone.utc)


async def seed(pool, monkeypatch, tmp_path, *, channel="lark_bot", on=True, project=True):
    channel_id, sender = f"channel-{channel}", "271828" if channel == "telegram" else stream.DM
    await stream.source(pool)
    await pool.execute("INSERT INTO channel_configs(channel_id,channel_type,config) VALUES($1,$2,$3::jsonb)",
                       channel_id, channel, json.dumps({"operator_sender_id": sender}))
    await pool.execute("INSERT INTO system_settings(key,value) VALUES('intents.enabled','true')")
    if on:
        await pool.execute("INSERT INTO system_settings(key,value) VALUES($1,'true')", cards.ENABLED_KEY)
    await pool.execute("INSERT INTO projects(id,name,work_dir,task_workspaces) VALUES('project-card','Synthetic',$1,'in_place')",
                       str(tmp_path))
    await pool.execute("INSERT INTO schedules(id,project_id,name,cron,timezone,requirement,kind) "
                       "VALUES('schedule-card','project-card','Synthetic','*/10 * * * *','UTC','Read','subscription')")
    seq = await stream.add(pool, "Synthetic request", sent_at=NOW)
    iid = await leads.intent(pool, [seq], schedule_id="schedule-card", project_id="project-card" if project else None)
    monkeypatch.setattr(intent_push, "target_for", AsyncMock(return_value=(channel_id, sender)))
    posts, card_posts = [], []

    async def post(channel, target, text, **kwargs):
        posts.append((channel, target.id, text, kwargs))
        return PostReceipt(True, external_ids=("17",))

    async def post_card(channel, target, card, **kwargs):
        card_posts.append((channel, target.id, card, kwargs))
        await cards.bind(card, "17", sender)
        return PostReceipt(True, external_ids=("17",))

    registry = SimpleNamespace(post=post, post_card=post_card, edit=AsyncMock(return_value=PostReceipt(True)),
                               edit_card=AsyncMock(return_value=PostReceipt(True)))
    service = intent_push.IntentPushService(registry, clock=lambda: NOW)
    platform = SimpleNamespace(bus=SimpleNamespace(publish=AsyncMock()))
    intent_cards.install(platform)
    return SimpleNamespace(iid=iid, channel=channel, channel_id=channel_id, sender=sender, service=service,
                           platform=platform, registry=registry, posts=posts, card_posts=card_posts)


def event(env, choice="build", **fields):
    card = env.card_posts[0][2]
    button = next(button for button in card.buttons if button.value.endswith(":" + choice))
    return cards.Press(env.channel_id, fields.pop("presser", env.sender), button.value,
                       chat=fields.pop("chat", env.sender), message_id=fields.pop("message_id", "17"), **fields)


def command(env, text):
    return MessageEnvelope.create(source=AgentAddress("channel", env.channel_id, env.channel),
        target=manager_address("manager"), message_type=MessageType.USER_MESSAGE,
        payload={"text": text, "channel_context": {"chat_id": env.sender, "sender_id": env.sender,
                 "chat_type": "p2p", "message_id": "18"}})


def test_off_push_has_exact_original_call_and_no_card_writes(monkeypatch, tmp_path):
    async def check(pool):
        env = await seed(pool, monkeypatch, tmp_path, on=False)
        row = await pool.fetchrow("SELECT * FROM intents WHERE id=$1", env.iid)
        text = intent_push.push_text(await intents.intent_view(row), priority="normal", timezone="UTC")
        await env.service.run_once()
        post_id = await pool.fetchval("SELECT id FROM intent_posts")
        assert env.posts == [(env.channel_id, env.sender, text,
                              {"reply_to": None, "idempotency_key": f"intent-{env.iid}-{post_id}"})]
        assert env.card_posts == []
        assert await pool.fetchval("SELECT count(*) FROM card_actions") == 0
    run_db(check, monkeypatch)


@pytest.mark.parametrize("cards_on", [False, True])
def test_intent_push_keeps_measurements_for_cards_and_text(monkeypatch, tmp_path, cards_on):
    from looper.agents import chat_settings, turn_metrics

    async def check(pool):
        env = await seed(pool, monkeypatch, tmp_path, on=cards_on)
        monkeypatch.setattr(chat_settings, "metrics_enabled", lambda: True)
        await env.service.run_once()
        calls = env.card_posts if cards_on else env.posts
        assert len(calls) == 1
        proactive = calls[0][3]["proactive"]
        assert proactive.source == turn_metrics.ProactiveSource.INTENT
        assert proactive.source_ref == str(env.iid)
    run_db(check, monkeypatch)


@pytest.mark.parametrize("channel", ["lark_bot", "telegram"])
def test_buttons_build_once_using_the_same_task_service(monkeypatch, tmp_path, channel):
    async def check(pool):
        env = await seed(pool, monkeypatch, tmp_path, channel=channel)
        await env.service.run_once()
        card = env.card_posts[0][2]
        assert [button.label for button in card.buttons] == ["建", "驳回"]
        assert env.posts == [] and card.text == await pool.fetchval("SELECT text FROM intent_posts")
        result = await cards.press(event(env))
        assert result.card and not result.card.buttons
        assert await pool.fetchval("SELECT state FROM intents") == "tasked"
        assert await pool.fetchval("SELECT count(*) FROM tasks") == 1
        saved = await db_cards.get(card.token)
        assert saved["handled_via"] == "button" and saved["outcome"] == "ok"
        assert (await cards.press(event(env))).toast == cards.HANDLED
        assert await pool.fetchval("SELECT count(*) FROM tasks") == 1
    run_db(check, monkeypatch, max_size=5)


@pytest.mark.parametrize("typed_first", [False, True])
def test_typed_and_button_rejection_settle_the_same_record(monkeypatch, tmp_path, typed_first):
    async def check(pool):
        env = await seed(pool, monkeypatch, tmp_path)
        await env.service.run_once()
        routed = await intent_routing.route(command(env, f"驳回 I-{env.iid:04d}"), platform=env.platform)
        assert routed and routed.action
        if typed_first:
            await routed.action()
            assert (await cards.press(event(env, "reject"))).toast == cards.HANDLED
        else:
            await cards.press(event(env, "reject"))
            assert "驳回过" in await routed.action()
        saved = await db_cards.get(env.card_posts[0][2].token)
        assert saved["handled_via"] == ("typed" if typed_first else "button")
        assert saved["choice"] == "reject" and saved["outcome"] == "ok"
        assert await pool.fetchval("SELECT state FROM intents") == "rejected"
    run_db(check, monkeypatch)


@pytest.mark.parametrize("field,value", [("presser", "ou_other_synthetic"), ("chat", "oc_forwarded"),
                                        ("message_id", "99")])
def test_wrong_sender_or_chat_or_message_cannot_claim(monkeypatch, tmp_path, field, value):
    async def check(pool):
        env = await seed(pool, monkeypatch, tmp_path)
        await env.service.run_once()
        result = await cards.press(event(env, **{field: value}))
        assert result.level == "error"
        assert await pool.fetchval("SELECT handled_at FROM card_actions") is None
        assert await pool.fetchval("SELECT state FROM intents") == "pending"
        assert (await cards.press(event(env, "reject"))).level == "success"
    run_db(check, monkeypatch)


def test_changed_parameters_invalidate_the_old_card(monkeypatch, tmp_path):
    async def check(pool):
        env = await seed(pool, monkeypatch, tmp_path)
        await env.service.run_once()
        await pool.execute("UPDATE intents SET summary='Changed synthetic request'")
        result = await cards.press(event(env))
        assert result.toast == cards.STALE
        assert await pool.fetchval("SELECT count(*) FROM tasks") == 0
        assert await pool.fetchval("SELECT outcome FROM card_actions") == "stale"
    run_db(check, monkeypatch)


def test_missing_project_does_not_consume_a_button(monkeypatch, tmp_path):
    async def check(pool):
        env = await seed(pool, monkeypatch, tmp_path, project=False)
        await env.service.run_once()
        result = await cards.press(event(env))
        assert "项目" in result.toast and result.card.buttons
        assert await pool.fetchval("SELECT handled_at FROM card_actions") is None
        assert (await cards.press(event(env, "reject"))).level == "success"
    run_db(check, monkeypatch)


def test_concurrent_typed_build_and_button_reject_have_one_winner(monkeypatch, tmp_path):
    async def check(pool):
        env = await seed(pool, monkeypatch, tmp_path)
        await env.service.run_once()
        routed = await intent_routing.route(command(env, f"建 I-{env.iid:04d}"), platform=env.platform)
        assert routed and routed.action
        await asyncio.gather(routed.action(), cards.press(event(env, "reject")))
        state = await pool.fetchval("SELECT state FROM intents")
        assert state in ("tasked", "rejected")
        assert await pool.fetchval("SELECT count(*) FROM tasks") == int(state == "tasked")
        saved = await db_cards.get(env.card_posts[0][2].token)
        assert saved["outcome"] == "ok" and saved["handled_via"] in ("typed", "button")
    run_db(check, monkeypatch, max_size=5)


@pytest.mark.parametrize("changed", ["operator", "target"])
def test_current_operator_and_confirmed_target_are_rechecked(monkeypatch, tmp_path, changed):
    async def check(pool):
        env = await seed(pool, monkeypatch, tmp_path)
        await env.service.run_once()
        if changed == "operator":
            await pool.execute("UPDATE channel_configs SET config=config || '{\"operator_sender_id\":\"ou_other_synthetic\"}'::jsonb")
        else:
            intent_push.target_for.return_value = None
        assert (await cards.press(event(env))).level in ("error", "warning")
        assert await pool.fetchval("SELECT handled_at FROM card_actions") is None
        await pool.execute("UPDATE channel_configs SET config=jsonb_build_object('operator_sender_id',$1::text)", env.sender)
        intent_push.target_for.return_value = (env.channel_id, env.sender)
        assert (await cards.press(event(env, "reject"))).level == "success"
    run_db(check, monkeypatch)


def test_typed_reply_updates_the_posted_card_without_a_plain_text_fallback(monkeypatch, tmp_path):
    async def check(pool):
        env = await seed(pool, monkeypatch, tmp_path)
        await env.service.run_once()
        await intents.reject(env.iid, via="lark")
        await env.service.run_once()
        env.registry.edit.assert_not_awaited()
        env.registry.edit_card.assert_awaited_once()
        card = env.registry.edit_card.await_args.args[-1]
        assert not card.buttons and "已驳回" in card.text
        assert len(env.card_posts) == 1 and env.posts == []
    run_db(check, monkeypatch)


def test_explicitly_rejected_delivery_lapses_its_unused_card(monkeypatch, tmp_path):
    async def check(pool):
        env = await seed(pool, monkeypatch, tmp_path)
        original = env.registry.post_card
        env.registry.post_card = AsyncMock(return_value=PostReceipt(False, outcome=PostOutcome.REJECTED))
        await env.service.run_once()
        assert await pool.fetchval("SELECT outcome FROM card_actions") == "lapsed"
        env.registry.post_card = original
        await env.service.run_once()
        assert (await cards.press(event(env, "reject"))).level == "success"
    run_db(check, monkeypatch)


def test_a_button_losing_to_typed_build_never_retries_start(monkeypatch, tmp_path):
    async def check(pool):
        env = await seed(pool, monkeypatch, tmp_path)
        await env.service.run_once()
        execute = intent_routing._execute

        async def typed_wins(*args, **kwargs):
            await intents.build(env.iid, via="lark", platform=env.platform)
            return await execute(*args, **kwargs)

        monkeypatch.setattr(intent_routing, "_execute", typed_wins)
        await cards.press(event(env))
        assert await pool.fetchval("SELECT count(*) FROM tasks") == 1
        assert env.platform.bus.publish.await_count == 1
    run_db(check, monkeypatch, max_size=5)
