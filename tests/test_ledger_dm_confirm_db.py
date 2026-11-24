"""Typed ledger confirmations run before the Manager model, only in direct chats."""

import json
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest

from looper.agents import ledger_routing
from looper.agents.gate_routing import NOT_A_REPLY
from looper.agents.message import AgentAddress, MessageEnvelope, MessageType, manager_address
from looper.db import ledger
from tests import ledger_support as support
from tests.db_support import DSN, run_db
from tests.test_manager_stop import _Session, _manager, _replies, _with_ops

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")


def message(text, *, channel="lark", sender="operator-synthetic", group=False, chat_type=None):
    return MessageEnvelope.create(
        source=AgentAddress("channel", f"channel-{channel}", channel), target=manager_address("manager"),
        message_type=MessageType.USER_MESSAGE, payload={"text": text, "channel_context": {
            "chat_id": "-123" if group else "123", "sender_id": sender,
            "chat_type": chat_type or ("group" if group else "p2p"), "message_id": "synthetic-message"}})


async def seed(pool, *, on=True):
    await pool.execute("INSERT INTO system_settings(key,value) VALUES('ledger.enabled',$1)", str(on).lower())
    for channel in ("lark", "lark_bot", "telegram"):
        await pool.execute("INSERT INTO channel_configs(channel_id,channel_type,display_name,config) "
                           "VALUES($1,$2,'Synthetic bot',$3::jsonb)", f"channel-{channel}", channel,
                           json.dumps({"operator_sender_id": "operator-synthetic"}))
    await support.add_own(pool, kind="decision", actor=support.RELAYED, title="Use PostgreSQL")


async def handle(tmp_path, envelope):
    session = _Session(hold=False)
    manager, bus, _ = _manager(tmp_path, session)
    queue = bus.get_queue(envelope.source.agent_id)
    _with_ops(manager)
    manager._route_to_gate = AsyncMock(return_value=NOT_A_REPLY)
    manager._persist_user = AsyncMock()
    try:
        await manager._handle(envelope)
        return session, _replies(queue)
    finally:
        await bus.shutdown()


@pytest.mark.parametrize("channel", ["lark", "lark_bot", "telegram"])
def test_operator_dm_confirms_without_a_model_turn(monkeypatch, tmp_path, agent_db, channel):
    async def check(pool):
        await seed(pool)
        envelope = message("确认 L-0001", channel=channel)
        session, replies = await handle(tmp_path, envelope)
        item = await ledger.get_item(1)
        assert item["assurance"] == "user"
        assert not session.turns
        assert replies[-1]["text"] == "L-0001 已确认：Use PostgreSQL"
        event = (await ledger.list_events(1))[-1]
        via = "telegram" if channel == "telegram" else "lark"
        assert (event["action"], event["actor_kind"], event["via"], event["actor_id"]) == (
            "confirm", "user", via, f"{via}:operator-synthetic")
        assert (event["channel_id"], event["chat"], event["manager_id"], event["message_id"]) == (
            f"channel-{channel}", "123", "manager", envelope.message_id)
    run_db(check, monkeypatch)


@pytest.mark.parametrize("sender", ["other-synthetic", None])
def test_non_operator_is_refused_without_a_model_turn(monkeypatch, tmp_path, agent_db, sender):
    async def check(pool):
        await seed(pool)
        session, replies = await handle(tmp_path, message("确认 L-0001", sender=sender))
        assert not session.turns
        assert replies[-1]["text"] == "账本只能由你本人在私聊确认。"
        assert (await ledger.get_item(1))["assurance"] == "relayed"
        assert len(await ledger.list_events(1)) == 1
    run_db(check, monkeypatch)


@pytest.mark.parametrize("identifier", ["L-9999", "L-0", "L-" + "9" * 5000])
def test_unknown_or_invalid_id_has_a_code_reply(monkeypatch, tmp_path, agent_db, identifier):
    async def check(pool):
        await seed(pool)
        session, replies = await handle(tmp_path, message(f"确认 {identifier}"))
        assert not session.turns
        expected = "没有 L-9999 这条账本事项。" if identifier == "L-9999" else (
            "账本编号无效，请使用 L-0001 这样的正整数编号。")
        assert replies[-1]["text"] == expected
        assert len(await ledger.list_events(1)) == 1
    run_db(check, monkeypatch)


def test_multiple_items_close_pending_and_keep_repeated_confirmation_idempotent(monkeypatch, tmp_path, agent_db):
    async def check(pool):
        await seed(pool)
        await support.add_own(pool, title="Reply by Friday")
        command = "确认 L-1 L-2 L-1"
        session, replies = await handle(tmp_path, message(command))
        assert not session.turns
        assert replies[-1]["text"] == "L-0001 已确认：Use PostgreSQL\nL-0002 已结项：Reply by Friday\nL-0001 之前已确认"
        session, replies = await handle(tmp_path, message(command))
        assert not session.turns and "L-0002 之前已结项" in replies[-1]["text"]
        assert len(await ledger.list_events(1)) == len(await ledger.list_events(2)) == 2
        assert (await ledger.get_item(2))["status"] == "done"
    run_db(check, monkeypatch)


@pytest.mark.parametrize("source", ["gate", "task", "confirmation"])
def test_sourced_items_reply_where_to_act_without_writing(monkeypatch, tmp_path, agent_db, source):
    async def check(pool):
        await seed(pool)
        item = await support.add_sourced(pool, await support.seed(pool), source)
        session, replies = await handle(tmp_path, message(f"确认 {item['id']}"))
        assert not session.turns
        assert {"gate": "关卡", "task": "任务", "confirmation": "原会话"}[source] in replies[-1]["text"]
        assert len(await ledger.list_events(2)) == 1
    run_db(check, monkeypatch)


@pytest.mark.parametrize("text,options", [
    ("确认 L-0001", {"group": True}), ("确认 L-0001", {"channel": "web"}),
    ("确认 L-0001", {"chat_type": "unknown"}), ("确认", {}),
    ("确认一下 L-0001 是什么", {}), ("L-0001 不对", {}),
    ("确认 L-1 后开始", {}), ("确认 L1", {}), ("确认 1", {}),
])
def test_unmatched_text_and_non_dm_keep_going_to_the_model(monkeypatch, tmp_path, agent_db, text, options):
    async def check(pool):
        await seed(pool)
        session, _ = await handle(tmp_path, message(text, **options))
        assert len(session.turns) == 1 and text in session.turns[0]
        assert (await ledger.get_item(1))["assurance"] == "relayed"
    run_db(check, monkeypatch)


def test_disabled_ledger_passes_through_and_freezes_items(monkeypatch, tmp_path, agent_db):
    async def check(pool):
        await seed(pool, on=False)
        before = [dict(row) for row in await pool.fetch("SELECT * FROM ledger_items ORDER BY id")]
        with monkeypatch.context() as baseline:
            baseline.setattr(ledger_routing, "route", AsyncMock(return_value=None))
            old_session, old_replies = await handle(tmp_path, message("确认 L-0001"))
        session, replies = await handle(tmp_path, message("确认 L-0001"))
        assert session.turns == old_session.turns
        assert [r["text"] for r in replies] == [r["text"] for r in old_replies]
        assert len(session.turns) == 1 and "确认 L-0001" in session.turns[0]
        assert replies[-1]["text"] == "好的"
        assert [dict(row) for row in await pool.fetch("SELECT * FROM ledger_items ORDER BY id")] == before
        assert len(await ledger.list_events(1)) == 1
    run_db(check, monkeypatch)


@pytest.mark.parametrize("case", ["unset_operator", "api_client", "wrong_bot", "cancelled", "work_item"])
def test_identity_and_item_state_boundaries(monkeypatch, tmp_path, agent_db, case):
    async def check(pool):
        await seed(pool)
        envelope = message("确认 L-0001")
        if case == "unset_operator":
            await pool.execute("UPDATE channel_configs SET config='{}'")
        elif case == "api_client":
            envelope.payload["channel_context"]["client"] = {"id": "synthetic-client"}
        elif case == "wrong_bot":
            envelope = replace(envelope, source=AgentAddress("channel", "channel-lark_bot-2", "lark_bot"))
        elif case == "cancelled":
            await ledger.apply("cancel", support.USER, item_id=1)
        else:
            item = await support.add_own(pool, kind="work_item")
            envelope.payload["text"] = f"确认 {item['id']}"
        before = await pool.fetchval("SELECT count(*) FROM ledger_events")
        session, replies = await handle(tmp_path, envelope)
        assert not session.turns
        if case in ("cancelled", "work_item"):
            assert "不能确认" in replies[-1]["text"]
        else:
            assert replies[-1]["text"] == "账本只能由你本人在私聊确认。"
        assert await pool.fetchval("SELECT count(*) FROM ledger_events") == before
    run_db(check, monkeypatch)


def test_preparation_and_disabling_between_items_do_not_write(monkeypatch):
    async def check(pool):
        await seed(pool)
        await support.add_own(pool, kind="decision", actor=support.RELAYED)
        routed = await ledger_routing.route(message("确认 L-1 L-2"))
        assert len(await ledger.list_events(1)) == 1
        apply = ledger.apply
        async def disable_after_first(*args, **kwargs):
            item = await apply(*args, **kwargs)
            await pool.execute("UPDATE system_settings SET value='false' WHERE key='ledger.enabled'")
            return item
        monkeypatch.setattr(ledger, "apply", disable_after_first)
        assert await routed.action() == "L-0001 已确认：Use PostgreSQL\n账本已关闭，未处理后续事项。"
        assert (await ledger.get_item(1))["assurance"] == "user"
        assert (await ledger.get_item(2))["assurance"] == "relayed"
        assert len(await ledger.list_events(2)) == 1
    run_db(check, monkeypatch)


def test_concurrent_confirmation_returns_already_done(monkeypatch):
    async def check(pool):
        await seed(pool)
        routed = await ledger_routing.route(message("确认 L-1"))
        apply = ledger.apply
        async def concurrent_confirmation(*args, **kwargs):
            await apply("confirm", support.USER, item_id=1)
            return await apply(*args, **kwargs)
        monkeypatch.setattr(ledger, "apply", concurrent_confirmation)
        assert await routed.action() == "L-0001 之前已确认"
        assert len(await ledger.list_events(1)) == 2
    run_db(check, monkeypatch)
