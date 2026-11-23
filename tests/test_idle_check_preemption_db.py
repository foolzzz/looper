"""User priority is retained across asynchronous idle-check preparation and cancellation."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from looper.agents import idle_checks, lanes, lanes_settings, session_routing
from looper.agents.tiers import background_turn, idle_check_turn
from looper.db import idle_checks as store
from tests.api_manager_support import USAGE, _manager, _message
from tests.db_support import DSN, run_db, until
from tests.fake_model_api import ResponseScript, text_events

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")


async def _row(pool):
    return await store.insert_running(pool, manager_id="manager", local_date=datetime.now(timezone.utc).date(),
        items=[{"section": "logins", "key": "login:synthetic", "since": None, "title": "Synthetic login", "facts": {}}],
        channel_id="channel-lark", chat="ou_idle_operator")


def test_user_queued_during_check_read_preempts_before_model_turn(monkeypatch, tmp_path):
    monkeypatch.setattr(lanes_settings, "_enabled", True)
    monkeypatch.setattr(session_routing, "_routing_enabled", False)
    async def check(pool):
        async with _manager(pool, tmp_path, monkeypatch,
                            [ResponseScript(events=text_events("User answer", usage=USAGE))]) as chat:
            check_id = await _row(pool)
            read, release = asyncio.Event(), asyncio.Event()
            original = store.get
            async def delayed_get(row_id):
                row = await original(row_id)
                read.set()
                await release.wait()
                return row
            monkeypatch.setattr(store, "get", delayed_get)
            post = AsyncMock()
            monkeypatch.setattr(idle_check_turn, "_post", post)
            settled = []
            settle = chat.bus.settle_handled
            def acknowledge(envelope):
                settled.append(envelope.message_id)
                settle(envelope)
            monkeypatch.setattr(chat.bus, "settle_handled", acknowledge)
            await chat.bus.start()
            await chat.manager.start(chat.bus)
            wake = idle_checks.due_envelope(check_id)
            await chat.bus.publish(wake)
            await asyncio.wait_for(read.wait(), 5)
            user = _message("Answer this first")
            try:
                await chat.bus.publish(user)
                main = chat.manager._sessions["main"]
                await until(lambda: chat.manager._router.queued_count(main, lanes.Lane.INTERACTIVE) == 1)
                release.set()
                await until(lambda: user.message_id in settled)
                assert len(chat.model.requests) == 1
                assert "[IDLE CHECK]" not in str(chat.model.requests[0].body)
                assert "Answer this first" in str(chat.model.requests[0].body)
                assert (await original(check_id))["state"] == "preempted"
                assert wake.message_id in settled
                post.assert_not_awaited()
            finally:
                release.set()
    run_db(check, monkeypatch, timeout=60)


def test_cancel_requested_after_model_return_prevents_delivery(monkeypatch, tmp_path):
    monkeypatch.setattr(lanes_settings, "_enabled", True)
    async def check(pool):
        async with _manager(pool, tmp_path, monkeypatch,
                            [ResponseScript(events=text_events("A stale reminder", usage=USAGE))]) as chat:
            check_id = await _row(pool)
            post = AsyncMock(return_value=True)
            monkeypatch.setattr(idle_check_turn, "_post", post)
            run = background_turn.run
            async def cancel_at_return(*args, **kwargs):
                result = await run(*args, **kwargs)
                kwargs["state"]["token"].cancel.request()
                return result
            monkeypatch.setattr(background_turn, "run", cancel_at_return)
            await chat.manager._handle(idle_checks.due_envelope(check_id))
            assert len(chat.model.requests) == 1
            assert (await store.get(check_id))["state"] == "preempted"
            post.assert_not_awaited()
            assert await pool.fetchval("SELECT count(*) FROM chat_messages") == 0
    run_db(check, monkeypatch, timeout=60)
