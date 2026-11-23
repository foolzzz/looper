"""A return to the check's direct chat voids a delivery, including during its decision."""
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from looper.agents import chat_settings, idle_checks, lanes_settings, proactive_delivery
from looper.agents.channels.base import PostReceipt
from looper.agents.message import channel_thread_id
from looper.agents.tiers import idle_check_turn
from looper.db import idle_checks as store
from tests.api_manager_support import USAGE, _manager
from tests.db_support import DSN, run_db
from tests.fake_model_api import ResponseScript, text_events

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")
CHANNEL, CHAT = "channel-lark", "ou_synthetic_returned"


async def user_message(pool, *, channel=CHANNEL, chat=CHAT, at):
    await pool.execute("INSERT INTO chat_messages (session_id, message_id, role, content, channel, created_at) "
                       "VALUES ($1, 'synthetic-return', 'user', 'I am here', 'lark', $2)",
                       channel_thread_id(channel, chat, "manager", "manager"), at)


async def running(pool):
    check_id = await store.insert_running(pool, manager_id="manager", local_date=datetime.now(timezone.utc).date(),
        items=[{"section": "logins", "key": "login:synthetic", "title": "Login", "facts": {}}],
        channel_id=CHANNEL, chat=CHAT)
    return await store.get(check_id)


@pytest.fixture(autouse=True)
def policy(monkeypatch):
    monkeypatch.setattr(chat_settings, "_delivery_enabled", True)
    monkeypatch.setattr(proactive_delivery, "load_policy", lambda: proactive_delivery.DeliveryPolicy(
        timezone=timezone.utc, quiet_hours=None))


@pytest.mark.parametrize("where,expected", [("same", "preempted"), ("before", "sent"),
    ("equal", "sent"), ("group", "sent"), ("other_chat", "sent"), ("other_channel", "sent")])
def test_only_a_new_message_in_this_direct_chat_voids_the_send(monkeypatch, where, expected):
    async def run(pool):
        check = await running(pool)
        at = check["created_at"] + timedelta(seconds={"before": -1, "equal": 0}.get(where, 1))
        await user_message(pool, channel="channel-lark-2" if where == "other_channel" else CHANNEL,
            chat={"group": "oc_synthetic_group", "other_chat": "ou_synthetic_other"}.get(where, CHAT), at=at)
        post = AsyncMock(return_value=PostReceipt(True))
        assert await idle_checks.deliver_result(check, "Please check the login.", post=post) == expected
        row = await store.get(check["id"])
        assert (row["state"], row["reason"]) == (expected, None)
        assert post.await_count == (0 if expected == "preempted" else 1)
        assert await pool.fetchval("SELECT count(*) FROM proactive_deliveries") == (
            0 if expected == "preempted" else 1)
    run_db(run, monkeypatch)


def test_message_arriving_during_the_decision_is_checked_again_before_claim(monkeypatch):
    async def run(pool):
        check = await running(pool)
        decide = proactive_delivery.decide

        async def decided(*args, **kwargs):
            result = await decide(*args, **kwargs)
            await user_message(pool, at=check["created_at"] + timedelta(seconds=1))
            return result

        monkeypatch.setattr(proactive_delivery, "decide", decided)
        post = AsyncMock()
        assert await idle_checks.deliver_result(check, "Please check the login.", post=post) == "preempted"
        post.assert_not_awaited()
        assert (await store.get(check["id"]))["state"] == "preempted"
        assert await pool.fetchval("SELECT outcome FROM proactive_deliveries") is None
    run_db(run, monkeypatch)


def test_unavailable_user_history_warns_and_keeps_the_delivery_path(monkeypatch, caplog):
    async def run(pool):
        check = await running(pool)
        fetchval = type(pool).fetchval

        async def failing_history(self, query, *args, **kwargs):
            if "max(created_at) FROM chat_messages" in query:
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
            await user_message(pool, at=check["created_at"] + timedelta(seconds=1))
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
