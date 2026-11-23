"""Memory DM actions use the shown revision and never leak into intent routing."""

import asyncio
import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, Mock

import pytest

from looper.agents import intent_routing, memory_actions, memory_push, memory_routing, memory_write
from tests import memory_support as sup
from tests.db_support import DSN, run_db
from tests.test_memory_routing import envelope

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")


async def seed(pool, *, channel="lark", complete=True):
    await pool.execute("INSERT INTO system_settings(key,value) VALUES('memory_write',$1)",
                       json.dumps({"enabled": True, "push_channel": "channel-" + channel}))
    operator = "70001" if channel == "telegram" else "ou_operator_synthetic"
    await pool.execute("INSERT INTO channel_configs(channel_id,channel_type,display_name,enabled,config) "
                       "VALUES($1,$2,'Synthetic bot',true,$3::jsonb)", "channel-" + channel, channel,
                       json.dumps({"operator_sender_id": operator}))
    number = await sup.candidate(pool)
    row = await sup.revise(pool, number, now=datetime.now(timezone.utc))
    chat = "70001" if channel == "telegram" else "ou_operator_synthetic"
    post = await pool.fetchval("INSERT INTO memory_posts(candidate_id,kind,revision,content_hash,complete,channel_id,"
        "chat,text,state,external_id) VALUES($1,'push',1,$2,$3,$4,$5,'Synthetic push','sent','om_push_synthetic') "
        "RETURNING id", number, row["content_hash"], complete, "channel-" + channel, chat)
    return number, row, post


async def execute(message):
    routed = await memory_routing.route(message)
    if routed is None:
        return None
    return await routed.action() if routed.action else routed.reply


@pytest.mark.parametrize("channel", ["lark", "lark_bot", "telegram"])
@pytest.mark.parametrize("command", ["记", "记 M-1", "记 m-0001"])
def test_confirm_records_exact_shown_dm_revision(monkeypatch, channel, command):
    async def check(pool):
        number, row, post = await seed(pool, channel=channel)
        wake = Mock()
        monkeypatch.setattr(memory_write, "wake", wake)
        monkeypatch.setattr(memory_routing, "WAIT_SECONDS", 0)
        deferred = AsyncMock()
        monkeypatch.setattr(memory_push, "defer_result", deferred)
        sender = "70001" if channel == "telegram" else "ou_operator_synthetic"
        message = envelope(command, channel=channel, sender=sender,
                           quote="om_push_synthetic" if command == "记" else None)
        prepared = await memory_routing.route(message)
        assert await pool.fetchval("SELECT state FROM memory_candidates WHERE id=$1", number) == "pending"
        reply = await prepared.action()
        assert reply == "M-0001 已确认，写入后通知你"
        event = await pool.fetchrow("SELECT * FROM memory_candidate_events WHERE action='confirm'")
        assert (event["via"], event["post_id"], event["content_hash"], event["channel_id"], event["chat"],
                event["message_id"]) == ("telegram" if channel == "telegram" else "lark", post, row["content_hash"],
                                        "channel-" + channel, sender,
                                        "om_reply_synthetic")
        wake.assert_called_once()
        deferred.assert_awaited_once_with(number, channel_id="channel-" + channel,
                                         chat=sender)
    run_db(check, monkeypatch)


@pytest.mark.parametrize("text", ["记 M-1 多余", "记 M-1 I-2", "重试 M-1 M-2", "重试 M-1 x"])
def test_claimed_memory_forms_with_invalid_grammar_never_act(monkeypatch, text):
    async def check(pool):
        await seed(pool)
        act = AsyncMock()
        monkeypatch.setattr(memory_actions, "act", act)
        reply = await execute(envelope(text))
        assert reply and "回复" in reply
        act.assert_not_awaited()
    run_db(check, monkeypatch)


@pytest.mark.parametrize("text", ["随便说点什么", "建", "重试", "记 M-1 extra"])
def test_every_memory_quote_is_claimed_without_guessing_an_action(monkeypatch, text):
    async def check(pool):
        await seed(pool)
        act = AsyncMock()
        monkeypatch.setattr(memory_actions, "act", act)
        assert await execute(envelope(text, quote="om_push_synthetic"))
        act.assert_not_awaited()
    run_db(check, monkeypatch)


@pytest.mark.parametrize("channel,sender,group", [("lark", "ou_operator_synthetic", True),
    ("lark", "ou_other_synthetic", False), ("telegram", "70001", True),
    ("telegram", "70002", False), ("web", "ou_operator_synthetic", False),
    ("email", "ou_operator_synthetic", False)])
def test_only_the_operator_in_supported_bot_dm_can_act(monkeypatch, channel, sender, group):
    async def check(pool):
        await seed(pool)
        reply = await execute(envelope("驳回 M-1", channel=channel, sender=sender, group=group))
        assert reply == "记忆候选只能由你本人在私聊或记忆页处理"
        assert await pool.fetchval("SELECT state FROM memory_candidates") == "pending"
    run_db(check, monkeypatch)


def test_memory_reject_quoting_intent_push_never_reaches_intent_router(monkeypatch):
    async def check(pool):
        await seed(pool)
        intent = AsyncMock()
        monkeypatch.setattr(intent_routing, "route", intent)
        message = envelope("驳回 M-0001 不需要", quote="om_intent_push_synthetic")
        result = await memory_routing.route(message)
        if result is None:
            await intent_routing.route(message, platform=object())
        assert await result.action() == "❌ M-0001 已驳回"
        intent.assert_not_awaited()
        assert await pool.fetchval("SELECT state FROM memory_candidates") == "rejected"
        assert await pool.fetchval("SELECT note FROM memory_candidate_events WHERE action='reject'") == "不需要"
        assert await memory_routing.route(envelope("驳回 I-1", quote="om_intent_push_synthetic")) is None
    run_db(check, monkeypatch)


@pytest.mark.parametrize("condition,expected", [("hash", "M-0001 在推送后改过，请看最新的推送"),
    ("incomplete", "M-0001 内容较长，请到记忆页确认："), ("expired", "M-0001 已过期"),
    ("rejected", "M-0001 已驳回"), ("written", "M-0001 已写入")])
def test_confirm_refusals_do_not_change_state(monkeypatch, condition, expected):
    async def check(pool):
        number, row, post = await seed(pool)
        if condition == "hash":
            await pool.execute("UPDATE memory_posts SET content_hash=$1", "f" * 64)
        elif condition == "incomplete":
            await pool.execute("UPDATE memory_posts SET complete=false")
        elif condition == "expired":
            await pool.execute("UPDATE memory_candidates SET expires_at=now()-interval '1 second'")
        elif condition == "rejected":
            await memory_actions.act("reject", "M-1")
        else:
            await memory_actions.act("confirm", "M-1", content_hash=row["content_hash"])
            await sup.move(pool, number, "writing", "claim")
            await sup.move(pool, number, "written", "write", commit_sha="a" * 40)
        before = await pool.fetchval("SELECT count(*) FROM memory_candidate_events")
        reply = await execute(envelope("记", quote="om_push_synthetic"))
        assert reply.startswith(expected)
        assert await pool.fetchval("SELECT count(*) FROM memory_candidate_events") == before
    run_db(check, monkeypatch)


def test_multiple_ids_share_one_wait_deadline_and_report_individual_results(monkeypatch):
    async def check(pool):
        first, row, _ = await seed(pool)
        await pool.execute("UPDATE memory_candidates SET note_path=NULL WHERE id=$1", first)
        second = await sup.candidate(pool, statement="A second synthetic preference")
        second_row = await sup.revise(pool, second, note_path=None, now=datetime.now(timezone.utc))
        await pool.execute("INSERT INTO memory_posts(candidate_id,kind,revision,content_hash,complete,channel_id,"
            "chat,text,state) VALUES($1,'push',1,$2,true,'channel-lark','ou_operator_synthetic','Push','sent')",
            second, second_row["content_hash"])
        monkeypatch.setattr(memory_routing, "WAIT_SECONDS", .02)
        deferred = AsyncMock()
        monkeypatch.setattr(memory_push, "defer_result", deferred)
        def wake():
            async def finish():
                await sup.move(pool, first, "writing", "claim")
                await sup.move(pool, first, "written", "write", commit_sha="b" * 40)
            asyncio.create_task(finish())
        monkeypatch.setattr(memory_write, "wake", wake)
        monkeypatch.setattr(memory_routing, "POLL_SECONDS", .005)
        reply = await execute(envelope("记 M-1 M-2"))
        assert "✅ M-0001" in reply and "bbbbbbbb" in reply
        assert "mem/prefs/weekly.md" in reply and "mem/prefs/_index.md" in reply
        assert "M-0002 已确认，写入后通知你" in reply
        deferred.assert_awaited_once_with(second, channel_id="channel-lark", chat="ou_operator_synthetic")
    run_db(check, monkeypatch)


def test_blocked_retry_wakes_writer_and_returns_completed_result(monkeypatch):
    async def check(pool):
        number, row, _ = await seed(pool)
        await memory_actions.act("confirm", "M-1", content_hash=row["content_hash"])
        await sup.move(pool, number, "writing", "claim")
        await sup.move(pool, number, "blocked", "block", reason="dirty_target")
        def wake():
            async def finish():
                await sup.move(pool, number, "writing", "claim")
                await sup.move(pool, number, "written", "write", commit_sha="c" * 40)
            asyncio.create_task(finish())
        monkeypatch.setattr(memory_write, "wake", wake)
        monkeypatch.setattr(memory_routing, "POLL_SECONDS", .005)
        reply = await execute(envelope("重试 M-1"))
        assert "✅ M-0001" in reply and "cccccccc" in reply
        assert await pool.fetchval("SELECT count(*) FROM memory_posts WHERE kind='final'") == 0
    run_db(check, monkeypatch)


@pytest.mark.parametrize("intents_enabled", [False, True])
def test_manager_routes_memory_before_intents_even_when_quoted_intent_is_pending(
        monkeypatch, tmp_path, agent_db, intents_enabled):
    from looper.agents import leads_model
    from looper.agents.gate_routing import NOT_A_REPLY
    from tests import leads_support as leads, subscription_support as stream
    from tests.test_manager_stop import _Session, _manager, _with_ops

    async def check(pool):
        await seed(pool)
        # agent_db keeps unrelated Manager reads in memory; restore the real settings read for this schema.
        schema_pool = pool
        async def settings(keys, *, pool=None):
            rows = await (pool or schema_pool).fetch(
                "SELECT key,value FROM system_settings WHERE key=ANY($1::text[])", list(keys))
            return {row["key"]: row["value"] for row in rows}
        agent_db.get_settings.side_effect = settings
        await stream.source(pool)
        event = await stream.add(pool, "Synthetic request", sent_at=datetime.now(timezone.utc))
        iid = await leads.intent(pool, [event])
        await pool.execute("INSERT INTO intent_posts(intent_id,kind,channel_id,chat,text,state,external_id) "
            "VALUES($1,'push','channel-lark','oc_dm_synthetic','Synthetic intent','sent','om_intent_synthetic')", iid)
        monkeypatch.setattr(leads_model, "intents_enabled", AsyncMock(return_value=intents_enabled))
        session = _Session(hold=False)
        manager, bus, _ = _manager(tmp_path, session)
        _with_ops(manager)
        manager._route_to_gate = AsyncMock(return_value=NOT_A_REPLY)
        manager._persist_user = AsyncMock()
        reply = AsyncMock()
        manager._send_reply = reply
        routed_intent = AsyncMock(wraps=intent_routing.route)
        monkeypatch.setattr(intent_routing, "route", routed_intent)
        await manager._handle(envelope("驳回 M-0001", quote="om_intent_synthetic"))
        assert not session.turns
        assert await pool.fetchval("SELECT state FROM memory_candidates") == "rejected"
        assert await pool.fetchval("SELECT state FROM intents WHERE id=$1", iid) == "pending"
        routed_intent.assert_not_awaited()
        assert reply.call_args.args[1] == "❌ M-0001 已驳回"
        manager._persist_user.assert_awaited_once()
        await bus.shutdown()
    run_db(check, monkeypatch)


@pytest.mark.parametrize("command,confirms", [("记", False), ("记 M-1", True)])
def test_bare_quote_binds_old_revision_but_explicit_id_uses_latest_push(monkeypatch, command, confirms):
    from looper.agents.memory_model import Action, Actor, MadeBy, Via

    async def check(pool):
        number, _, old_post = await seed(pool)
        current = await sup.revise(pool, number, action=Action.EDIT, actor=Actor.USER, via=Via.DASHBOARD,
            made_by=MadeBy.OPERATOR, changes=[{**sup.NOTE, "content": sup.NOTE["content"] + "New revision.\n"},
                                            sup.INDEX], now=datetime.now(timezone.utc))
        latest = await pool.fetchval("INSERT INTO memory_posts(candidate_id,kind,revision,content_hash,complete,"
            "channel_id,chat,text,state,external_id) VALUES($1,'push',2,$2,true,'channel-lark',"
            "'ou_operator_synthetic','Revised push','sent','om_revised_synthetic') RETURNING id",
            number, current["content_hash"])
        monkeypatch.setattr(memory_routing, "WAIT_SECONDS", 0)
        monkeypatch.setattr(memory_push, "defer_result", AsyncMock())
        monkeypatch.setattr(memory_write, "wake", Mock())
        reply = await execute(envelope(command, quote="om_push_synthetic"))
        if confirms:
            assert reply == "M-0001 已确认，写入后通知你"
            assert await pool.fetchval("SELECT post_id FROM memory_candidate_events WHERE action='confirm'") == latest
        else:
            assert reply == "M-0001 在推送后改过，请看最新的推送"
            assert await pool.fetchval("SELECT state FROM memory_candidates") == "pending"
    run_db(check, monkeypatch)


def test_late_result_subscription_does_not_wait_for_writer_candidate_lock(monkeypatch):
    from looper.agents.memory_model import Action, Actor, State, Via
    from looper.db import memory as db_memory

    async def check(pool):
        number, _, _ = await seed(pool)
        holding, release = asyncio.Event(), asyncio.Event()
        writer = None
        async def write():
            async with pool.acquire() as conn, conn.transaction():
                await db_memory.transition(conn, number, State.WRITING, action=Action.CLAIM, actor=Actor.SYSTEM,
                                           via=Via.SYSTEM, now=datetime.now(timezone.utc))
                holding.set()
                await release.wait()
        def wake():
            nonlocal writer
            writer = asyncio.create_task(write())
        monkeypatch.setattr(memory_write, "wake", wake)
        monkeypatch.setattr(memory_routing, "WAIT_SECONDS", .02)
        monkeypatch.setattr(memory_routing, "POLL_SECONDS", .005)
        try:
            reply = await asyncio.wait_for(execute(envelope("记 M-1")), timeout=.5)
            assert holding.is_set() and not release.is_set()
            assert reply == "M-0001 已确认，写入后通知你"
            final = await pool.fetchrow("SELECT * FROM memory_posts WHERE kind='final'")
            assert final["state"] == "pending" and final["error"] is None
        finally:
            release.set()
            if writer is not None:
                await writer
    run_db(check, monkeypatch)


def test_confirmation_and_late_result_reservation_are_atomic(monkeypatch):
    from looper.db.memory import MemoryRefused

    async def check(pool):
        await seed(pool)
        reserve = AsyncMock(side_effect=MemoryRefused("Synthetic reservation refusal", code="invalid"))
        monkeypatch.setattr(memory_push, "reserve_result", reserve)
        wake = Mock()
        monkeypatch.setattr(memory_write, "wake", wake)
        reply = await execute(envelope("记 M-1"))
        assert "当前不能处理" in reply
        assert await pool.fetchval("SELECT state FROM memory_candidates") == "pending"
        assert await pool.fetchval("SELECT count(*) FROM memory_candidate_events WHERE action='confirm'") == 0
        assert await pool.fetchval("SELECT count(*) FROM memory_posts WHERE kind='final'") == 0
        wake.assert_not_called()
    run_db(check, monkeypatch)
