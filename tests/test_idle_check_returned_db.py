"""A return to the check's direct chat voids a delivery, including during its decision."""
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from looper.agents import chat_settings, idle_checks, lanes_settings, proactive_delivery
from looper.agents.channels.base import PostReceipt
from looper.agents.message import MANAGER_ADDRESS, MessageEnvelope, MessageType, channel_address
from looper.agents.tiers import idle_check_turn
from looper.agents.tiers.manager import ManagerAgent, ManagerConfig
from looper.db import idle_checks as store
from tests.api_manager_support import USAGE, _manager
from tests.db_support import DSN, run_db
from tests.fake_model_api import ResponseScript, text_events

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")
CHANNEL, OPERATOR, CHAT = "channel-lark", "ou_synthetic_returned", "oc_synthetic_returned_dm"


async def user_message(pool, tmp_path, *, channel=CHANNEL, chat=CHAT, sender=OPERATOR, group=False, at):
    kind = "telegram" if channel == "channel-telegram" else "lark"
    await pool.execute("INSERT INTO channel_configs (channel_id, channel_type) VALUES ($1, $2) "
                       "ON CONFLICT DO NOTHING", channel, kind)
    envelope = MessageEnvelope.create(source=channel_address(kind, channel), target=MANAGER_ADDRESS,
        message_type=MessageType.USER_MESSAGE, payload={"text": "I am here", "channel_context": {
            "chat_id": chat, "sender_id": sender, "chat_type": "group" if group else "p2p"}})
    manager = ManagerAgent(config=ManagerConfig(work_dir=tmp_path), runtime_root=tmp_path)
    await manager._persist_user(envelope, "I am here")
    # Only the clock is controlled; storage and direct-chat provenance use the real Manager path.
    assert await pool.fetchval("UPDATE chat_messages SET created_at=$1 WHERE message_id=$2 RETURNING id",
                               at, envelope.message_id) is not None


async def running(pool, *, channel=CHANNEL, operator=OPERATOR):
    await pool.execute("INSERT INTO system_settings (key,value) VALUES ('manager.idle_checks.enabled','true')")
    check_id = await store.insert_running(pool, manager_id="manager", local_date=datetime.now(timezone.utc).date(),
        items=[{"section": "logins", "key": "login:synthetic", "title": "Login", "facts": {}}],
        channel_id=channel, chat=operator)
    return await store.get(check_id)


@pytest.fixture(autouse=True)
def policy(monkeypatch):
    monkeypatch.setattr(chat_settings, "_delivery_enabled", True)
    monkeypatch.setattr(proactive_delivery, "load_policy", lambda: proactive_delivery.DeliveryPolicy(
        timezone=timezone.utc, quiet_hours=None))


@pytest.mark.parametrize("where,expected", [("same", "preempted"), ("before", "sent"),
    ("equal", "sent"), ("group", "sent"), ("other_chat", "sent"), ("other_channel", "sent")])
def test_only_a_new_message_in_this_direct_chat_voids_the_send(monkeypatch, tmp_path, where, expected):
    async def run(pool):
        check = await running(pool)
        at = check["created_at"] + timedelta(seconds={"before": -1, "equal": 0}.get(where, 1))
        await user_message(pool, tmp_path, channel="channel-lark-2" if where == "other_channel" else CHANNEL,
            chat={"group": "oc_synthetic_group", "other_chat": "oc_synthetic_other_dm"}.get(where, CHAT),
            sender="ou_synthetic_other" if where == "other_chat" else OPERATOR, group=where == "group", at=at)
        post = AsyncMock(return_value=PostReceipt(True))
        assert await idle_checks.deliver_result(check, "Please check the login.", post=post) == expected
        row = await store.get(check["id"])
        assert (row["state"], row["reason"]) == (expected, None)
        assert post.await_count == (0 if expected == "preempted" else 1)
        assert await pool.fetchval("SELECT count(*) FROM proactive_deliveries") == (
            0 if expected == "preempted" else 1)
    run_db(run, monkeypatch)


@pytest.mark.parametrize("channel,operator,chat", [(CHANNEL, OPERATOR, CHAT),
    ("channel-telegram", "123456789", "123456789")], ids=["lark", "telegram"])
def test_message_arriving_during_the_decision_is_checked_again_before_claim(monkeypatch, tmp_path,
                                                                          channel, operator, chat):
    async def run(pool):
        check = await running(pool, channel=channel, operator=operator)
        decide = proactive_delivery.decide

        async def decided(*args, **kwargs):
            result = await decide(*args, **kwargs)
            await user_message(pool, tmp_path, channel=channel, sender=operator, chat=chat,
                               at=check["created_at"] + timedelta(seconds=1))
            return result

        monkeypatch.setattr(proactive_delivery, "decide", decided)
        post = AsyncMock()
        assert await idle_checks.deliver_result(check, "Please check the login.", post=post) == "preempted"
        post.assert_not_awaited()
        assert (await store.get(check["id"]))["state"] == "preempted"
        assert await pool.fetchval("SELECT outcome FROM proactive_deliveries") is None
    run_db(run, monkeypatch)


def test_lark_last_user_message_at_reaches_the_delivery_layer(monkeypatch, tmp_path):
    async def run(pool):
        check = await running(pool)
        at = check["created_at"] - timedelta(minutes=5)
        await user_message(pool, tmp_path, at=at)
        decide = AsyncMock(wraps=proactive_delivery.decide)
        monkeypatch.setattr(proactive_delivery, "decide", decide)
        post = AsyncMock(return_value=PostReceipt(True))
        assert await idle_checks.deliver_result(check, "Please check the login.", post=post) == "sent"
        assert decide.await_args.kwargs["last_user_message_at"] == at
        post.assert_awaited_once()
    run_db(run, monkeypatch)


def test_unavailable_user_history_warns_and_keeps_the_delivery_path(monkeypatch, caplog):
    async def run(pool):
        check = await running(pool)
        fetchval = type(pool).fetchval

        async def failing_history(self, query, *args, **kwargs):
            if query.startswith("SELECT max(") and "FROM chat_messages" in query:
                raise RuntimeError("synthetic history unavailable")
            return await fetchval(self, query, *args, **kwargs)

        monkeypatch.setattr(type(pool), "fetchval", failing_history)
        post = AsyncMock(return_value=PostReceipt(True))
        assert await idle_checks.deliver_result(check, "Please check the login.", post=post) == "sent"
        post.assert_awaited_once()
        assert any(r.levelname == "WARNING" and "user" in r.message for r in caplog.records)
    run_db(run, monkeypatch)


def test_returned_user_adds_the_existing_stopped_note_exactly_once(monkeypatch, tmp_path):
    async def run(pool):
        monkeypatch.setattr(lanes_settings, "enabled", lambda: True)
        post = AsyncMock()
        monkeypatch.setattr(idle_check_turn, "_post", post)
        script = ResponseScript(events=text_events("Please check the login.", usage=USAGE))
        async with _manager(pool, tmp_path, monkeypatch, [script]) as chat:
            check = await running(pool)
            await user_message(pool, tmp_path, at=check["created_at"] + timedelta(seconds=1))
            wake = idle_checks.due_envelope(check["id"])
            await chat.manager._handle(wake)
            await chat.manager._handle(wake)
            assert (await store.get(check["id"]))["state"] == "preempted"
            assert chat.manager._sessions["main"].stopped_notes == [
                "[Looper] Before this message, your idle check was interrupted so you could answer. "
                "Nothing was sent from it."]
            assert len(chat.model.requests) == 1
            post.assert_not_awaited()
    run_db(run, monkeypatch, timeout=60)
