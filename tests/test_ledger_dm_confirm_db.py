"""Typed ledger confirmations run before the Manager model, only in direct chats."""

import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from looper.agents import ledger_routing
from looper.agents.gate_routing import NOT_A_REPLY
from looper.agents.message import AgentAddress, MessageEnvelope, MessageType, manager_address
from looper.db import ledger
from tests import ledger_support as support
from tests.db_support import DSN, run_db
from tests.test_manager_stop import _Session, _manager, _replies, _with_ops
from tests.test_lark_transport import StubTransport

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


@pytest.mark.parametrize("case", ["other_sender", "missing_sender", "unset_operator", "api_client", "wrong_bot"])
def test_non_operator_reaches_the_model_unchanged(monkeypatch, tmp_path, agent_db, case):
    async def check(pool):
        await seed(pool)
        envelope = message("确认 L-0001")
        if case == "other_sender":
            envelope.payload["channel_context"]["sender_id"] = "other-synthetic"
        elif case == "missing_sender":
            envelope.payload["channel_context"]["sender_id"] = None
        elif case == "unset_operator":
            await pool.execute("UPDATE channel_configs SET config='{}'")
        elif case == "api_client":
            envelope.payload["channel_context"]["client"] = {"id": "synthetic-client"}
        else:
            envelope = replace(envelope, source=AgentAddress("channel", "channel-lark_bot-2", "lark_bot"))
        with monkeypatch.context() as baseline:
            baseline.setattr(ledger_routing, "route", AsyncMock(return_value=None))
            old_session, old_replies = await handle(tmp_path, envelope)
        session, replies = await handle(tmp_path, envelope)
        assert len(session.turns) == 1
        assert session.turns == old_session.turns
        assert [r["text"] for r in replies] == [r["text"] for r in old_replies]
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


@pytest.mark.parametrize("case", ["cancelled", "work_item"])
def test_item_state_boundaries(monkeypatch, tmp_path, agent_db, case):
    async def check(pool):
        await seed(pool)
        envelope = message("确认 L-0001")
        if case == "cancelled":
            await ledger.apply("cancel", support.USER, item_id=1)
        else:
            item = await support.add_own(pool, kind="work_item")
            envelope.payload["text"] = f"确认 {item['id']}"
        before = await pool.fetchval("SELECT count(*) FROM ledger_events")
        session, replies = await handle(tmp_path, envelope)
        assert not session.turns
        assert "不能确认" in replies[-1]["text"]
        assert await pool.fetchval("SELECT count(*) FROM ledger_events") == before
    run_db(check, monkeypatch)


@pytest.mark.parametrize("kind", ["direct", "forward", "reply", "quote", "external_reply"])
def test_telegram_only_intercepts_the_operators_own_command(monkeypatch, tmp_path, agent_db, kind):
    from looper.agents.bus import MessageBus
    from looper.agents.channels.allowlist import SenderAllowlist
    from looper.agents.channels.telegram import TelegramChannelAdapter

    async def check(pool):
        await seed(pool)
        await pool.execute("UPDATE channel_configs SET config=$1::jsonb WHERE channel_id='channel-telegram'",
                           json.dumps({"operator_sender_id": "7"}))
        bus = MessageBus(runtime_root=tmp_path / "telegram")
        adapter = TelegramChannelAdapter(bot_token="0:not-a-token",
            allowlist=SenderAllowlist("channel-telegram", frozenset({"7"})))
        adapter._bus = bus
        incoming = SimpleNamespace(text="确认 L-0001", message_id=41, reply_to_message=None)
        if kind == "forward":
            incoming.forward_origin = SimpleNamespace(type="hidden_user", sender_user_name="Synthetic sender")
        elif kind == "reply":
            incoming.reply_to_message = SimpleNamespace(message_id=40, text="确认 L-0001")
        elif kind == "quote":
            incoming.quote = SimpleNamespace(text="确认 L-0001")
        elif kind == "external_reply":
            incoming.external_reply = SimpleNamespace(origin=SimpleNamespace(type="channel"))
        update = SimpleNamespace(update_id=1, effective_user=SimpleNamespace(id=7, username="synthetic"),
                                 effective_chat=SimpleNamespace(id=123), message=incoming)
        try:
            await adapter._on_message(update, None)
            envelope = bus.get_queue("manager").get_nowait()
        finally:
            await adapter._reactions.stop()
            await bus.shutdown()
        assert envelope.payload["text"] == "确认 L-0001"
        session, _ = await handle(tmp_path, envelope)
        assert len(session.turns) == (0 if kind == "direct" else 1)
        assert (await ledger.get_item(1))["assurance"] == ("user" if kind == "direct" else "relayed")
        assert len(await ledger.list_events(1)) == (2 if kind == "direct" else 1)
    run_db(check, monkeypatch)


@pytest.mark.parametrize("channel", ["lark", "lark_bot"])
@pytest.mark.parametrize("kind", ["direct", "forward", "reply"])
def test_lark_forward_and_quoted_reply_reach_the_model(monkeypatch, tmp_path, agent_db, channel, kind):
    from looper.agents.bus import MessageBus
    from looper.agents.channels.allowlist import SenderAllowlist
    from looper.agents.channels.lark import LarkChannelAdapter

    async def check(pool):
        await seed(pool)
        bus = MessageBus(runtime_root=tmp_path / channel)
        adapter = LarkChannelAdapter(runtime_root=tmp_path, channel_type=channel, transport=StubTransport(),
            allowlist=SenderAllowlist(f"channel-{channel}", frozenset({"operator-synthetic"})))
        adapter._bus = bus
        event = {"type": "im.message.receive_v1", "chat_id": "123", "chat_type": "p2p",
                 "message_id": "synthetic-message", "sender_id": "operator-synthetic", "sender_type": "user",
                 "message_type": "merge_forward" if kind == "forward" else "text", "content": "确认 L-0001"}
        if kind == "reply":
            event["reply_to"] = "synthetic-parent"
        try:
            await adapter._handle_lark_event(event)
            envelope = bus.get_queue("manager").get_nowait()
        finally:
            await adapter.stop()
            await bus.shutdown()
        assert envelope.payload["text"] == ("" if kind == "forward" else "确认 L-0001")
        session, _ = await handle(tmp_path, envelope)
        assert len(session.turns) == (0 if kind == "direct" else 1)
        assert (await ledger.get_item(1))["assurance"] == ("user" if kind == "direct" else "relayed")
        assert len(await ledger.list_events(1)) == (2 if kind == "direct" else 1)
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
