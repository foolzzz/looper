"""Channel history provenance and the cursor used to read another session's activity."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path

import pytest

from looper.agents.bus import MessageBus
from looper.agents.channels.base import DeliveryReceipt
from looper.agents.channels.allowlist import SenderAllowlist
from looper.agents.channels.chat_history import CHAT_RECORD, chat_record, record_reply
from looper.agents.channels.lark import LarkChannelAdapter
from looper.agents.message import MANAGER_ADDRESS, MessageEnvelope, MessageType, channel_address
from looper.agents.managers import ManagerFleet
from looper.agents.supervisor import AgentSupervisor
from looper.agents.session_routing import SessionKey, SessionKind
from looper.agents.ops import TurnOrigin
from looper.agents.tiers import background_turn
from looper.agents.tiers.manager import ManagerAgent, ManagerConfig, _message_origin, _session_key
from looper.db import chat
from tests.db_support import DSN, run_db
from tests.lark_cli_stand_in import BOT_MENTION, BOT_OPEN_ID, OPEN_ID

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")


@pytest.mark.parametrize("channel", ["lark", "telegram"])
@pytest.mark.parametrize("case", ["stopped_main", "stopped_routed", "stopped_routed_queued", "queued_off"])
def test_fleet_reply_session_readback(monkeypatch, tmp_path, channel, case):
    async def body(pool):
        bus = MessageBus(runtime_root=tmp_path)
        manager = ManagerAgent(config=ManagerConfig(work_dir=tmp_path), runtime_root=tmp_path)
        manager._bus = bus
        fleet = ManagerFleet(platform=None, supervisor=AgentSupervisor(bus, tmp_path), runtime_root=tmp_path,
                             url="http://127.0.0.1:1", config=manager._config, broadcaster=None)
        chat_id = "731241" if channel == "telegram" else "oc_synthetic_dm"
        envelope = MessageEnvelope.create(
            source=channel_address(channel), target=MANAGER_ADDRESS, message_type=MessageType.USER_MESSAGE,
            payload={"text": "hello", "channel_context": {"chat_id": chat_id, "chat_type": "p2p"}})
        expected = "main"
        try:
            if case == "queued_off":
                await fleet._deal_with("manager", [envelope])
            else:
                session = manager._sessions["main"]
                if case in {"stopped_routed", "stopped_routed_queued"}:
                    session = manager._router.session(SessionKey(SessionKind.DM, f"channel-{channel}", chat_id))
                    expected = session.key
                token = manager._register_turn(envelope)
                if case == "stopped_routed_queued":
                    manager._router.dispatch(session, envelope)
                else:
                    session.running_token = token
                    token.partial = "partial answer"
                await manager.stop()
                await fleet._settle_retired("manager", manager)
            reply = bus.get_queue(f"channel-{channel}").get_nowait()
            await record_reply(channel, reply, DeliveryReceipt(channel=channel, success=True))
            row = await pool.fetchrow("SELECT manager_session, sender_kind, sender_name FROM chat_messages")
            assert tuple(row) == (expected, None, None)
        finally:
            await bus.shutdown()

    run_db(body, monkeypatch)


def test_native_lark_provenance_round_trip(monkeypatch, tmp_path: Path, lark_cli_stand_in) -> None:
    async def body(pool):
        bus = MessageBus(runtime_root=tmp_path)
        adapter = LarkChannelAdapter(runtime_root=tmp_path, bot_open_id=BOT_OPEN_ID,
                                     allowlist=SenderAllowlist("channel-lark", frozenset({OPEN_ID})))
        adapter._bus = bus
        manager = ManagerAgent(config=ManagerConfig(work_dir=tmp_path), runtime_root=tmp_path)
        manager._bus = bus
        try:
            await adapter._process_lark_event({"type": "im.message.receive_v1", "chat_id": "oc_synthetic",
                "chat_type": "group", "message_id": "om_synthetic", "message_type": "text", "content": "hello",
                "sender_id": OPEN_ID, "sender_type": "user", "mentions": [BOT_MENTION]})
            await asyncio.gather(*adapter._reactions._pending)
            envelope = bus.get_queue("manager").get_nowait()
            old_context = {k: v for k, v in envelope.payload["channel_context"].items() if k != "sender_kind"}
            old = replace(envelope, payload={**envelope.payload, "channel_context": old_context})
            assert _message_origin(envelope) == _message_origin(old)
            assert await manager._build_user_message("hello", envelope=envelope) == await manager._build_user_message(
                "hello", envelope=old)
            await manager._persist_chat(envelope, "user", envelope.payload["text"], envelope.message_id)
            await manager._send_reply(envelope, "answer")
            answer = bus.get_queue("channel-lark").get_nowait()
            receipt = await adapter.deliver(answer)
            assert receipt.success
            await record_reply("lark", answer, receipt)
            rows = await pool.fetch("SELECT role, manager_session, sender_kind, sender_name FROM chat_messages ORDER BY id")
            assert [tuple(row) for row in rows] == [
                ("user", "main", "person", "Ada Lovelace"), ("assistant", "main", None, None)]
        finally:
            await adapter.stop()
            await bus.shutdown()

    run_db(body, monkeypatch)


@pytest.mark.parametrize("routed", [False, True])
def test_user_row_provenance(monkeypatch, tmp_path: Path, routed: bool) -> None:
    async def body(pool):
        manager = ManagerAgent(config=ManagerConfig(work_dir=tmp_path), runtime_root=tmp_path)
        if routed:
            session = manager._router.session(SessionKey(SessionKind.GROUP, "channel-lark", "oc_synthetic"))
            manager._current.set(session)
        envelope = MessageEnvelope.create(
            source=channel_address("lark"), target=MANAGER_ADDRESS, message_type=MessageType.USER_MESSAGE,
            payload={"text": "hello", "channel_context": {
                "chat_id": "oc_synthetic", "chat_type": "group", "sender_id": "ou_synthetic",
                "sender_kind": "person", "sender_name": 'Raw "name"\n<test>'}})
        await manager._persist_chat(envelope, "user", "hello", envelope.message_id)
        row = await pool.fetchrow("SELECT manager_session, sender_kind, sender_name FROM chat_messages")
        expected = "group:channel-lark:oc_synthetic" if routed else "main"
        assert dict(row) == {"manager_session": expected, "sender_kind": "person",
                             "sender_name": 'Raw "name"\n<test>'}
        # Exercise the Manager's actual reply metadata, then the channel's delivery write.
        manager._bus = bus = MessageBus(runtime_root=tmp_path)
        await manager._send_reply(envelope, "answer")
        reply = bus.get_queue("channel-lark").get_nowait()
        await record_reply("lark", reply, DeliveryReceipt(channel="lark", success=True))
        assistant = await pool.fetchrow("SELECT manager_session, sender_kind, sender_name FROM chat_messages "
                                        "WHERE role = 'assistant'")
        assert dict(assistant) == {"manager_session": expected, "sender_kind": None, "sender_name": None}
        await manager._send_own_reply(envelope, "autonomous answer")
        await record_reply("lark", bus.get_queue("channel-lark").get_nowait(),
                           DeliveryReceipt(channel="lark", success=True))
        assert await pool.fetchval("SELECT manager_session FROM chat_messages WHERE content = 'autonomous answer'") == expected
        # Registration must preserve the executing context even when a routing policy blocks a later model turn.
        state = {}
        background_turn.register(manager, TurnOrigin.COMPLETION, envelope, state)
        background = replace(reply, payload={"text": "background answer"}, metadata=state["chat_record"])
        await record_reply("lark", background, DeliveryReceipt(channel="lark", success=True))
        stored = await pool.fetchrow("SELECT manager_session, sender_kind, sender_name FROM chat_messages "
                                     "WHERE content = 'background answer'")
        assert tuple(stored) == (expected, None, None)
        assert await chat.latest_id(_session_key(envelope), pool=pool) is not None
        await bus.shutdown()

    run_db(body, monkeypatch)


@pytest.mark.parametrize("kind", [None, "unknown", [], "bot"])
def test_sender_kind_validation(monkeypatch, tmp_path: Path, kind) -> None:
    async def body(pool):
        manager = ManagerAgent(config=ManagerConfig(work_dir=tmp_path), runtime_root=tmp_path)
        context = {"chat_id": "oc_synthetic", **({"sender_kind": kind} if kind is not None else {})}
        envelope = MessageEnvelope.create(source=channel_address("lark"), target=MANAGER_ADDRESS,
                                          message_type=MessageType.USER_MESSAGE,
                                          payload={"channel_context": context})
        await manager._persist_chat(envelope, "user", "hello", envelope.message_id)
        row = await pool.fetchrow("SELECT manager_session, sender_kind FROM chat_messages")
        assert dict(row) == {"manager_session": "main", "sender_kind": "bot" if kind == "bot" else None}

    run_db(body, monkeypatch)


@pytest.mark.parametrize("manager_session", [None, "group:channel-lark:oc_synthetic"])
def test_assistant_row_provenance(monkeypatch, manager_session) -> None:
    async def body(pool):
        record = chat_record("lark:oc_synthetic", "answer", manager_session=manager_session)
        assert ("manager_session" in record) is (manager_session is not None)
        envelope = MessageEnvelope.create(
            source=MANAGER_ADDRESS, target=channel_address("lark"), message_type=MessageType.AGENT_REPLY,
            payload={"text": "answer"}, metadata={CHAT_RECORD: record})
        await record_reply("lark", envelope, DeliveryReceipt(channel="lark", success=True))
        row = await pool.fetchrow("SELECT manager_session, sender_kind, sender_name FROM chat_messages")
        assert dict(row) == {"manager_session": manager_session, "sender_kind": None, "sender_name": None}

    run_db(body, monkeypatch)


def test_activity_since(monkeypatch) -> None:
    async def body(pool):
        assert await chat.latest_id("thread", pool=pool) is None
        empty = await chat.activity_since("thread", after_id=0, exclude_session="mine",
                                          exclude_message_id=None, limit=2, pool=pool)
        assert (empty.rows, empty.hidden, empty.last_id) == ([], 0, None)
        for mid, session, kind, name in [
            ("own", "mine", "person", "Self"), ("other-user", "theirs", "bot", "Bot"),
            ("other-answer", "theirs", None, None), ("web", None, None, None),
            ("current", "theirs", "person", "Member"),
        ]:
            await chat.save_message(session_id="thread", message_id=mid,
                                    role="assistant" if mid == "other-answer" else "user", content=mid,
                                    manager_session=session, sender_kind=kind, sender_name=name, pool=pool)
        last_id = await chat.latest_id("thread", pool=pool)
        # A newer row in another thread must not move this thread's cursor.
        await chat.save_message(session_id="unrelated", message_id="later", role="user", content="later", pool=pool)

        async def activity(*, after=0, limit=10, exclude="current"):
            return await chat.activity_since("thread", after_id=after, exclude_session="mine",
                                             exclude_message_id=exclude, limit=limit, pool=pool)

        result = await activity()
        assert [r["content"] for r in result.rows] == ["other-user", "other-answer", "web"]
        assert [r["id"] for r in result.rows] == sorted(r["id"] for r in result.rows)
        assert result.rows[0]["sender_kind"] == "bot" and result.rows[0]["sender_name"] == "Bot"
        assert set(result.rows[0]) == {"id", "role", "content", "created_at", "sender_kind", "sender_name"}
        assert (result.hidden, result.last_id) == (0, last_id)
        limited = await activity(limit=2)
        assert [r["content"] for r in limited.rows] == ["other-answer", "web"]
        assert (limited.hidden, limited.last_id) == (1, last_id)
        caught_up = await activity(after=last_id)
        assert (caught_up.rows, caught_up.hidden, caught_up.last_id) == ([], 0, last_id)
        assert [r["content"] for r in (await activity(exclude=None)).rows] == [
            "other-user", "other-answer", "web", "current"]

    run_db(body, monkeypatch)
