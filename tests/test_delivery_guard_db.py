"""Hidden recovery turns persist separately without adding synthetic user chat messages."""

import asyncio
import json

import pytest

from looper.agents import chat_settings
from looper.agents.delivery_guard import FAILURE_NOTICE, FOLLOWUP_MESSAGE
from looper.db import chat as db_chat
from tests.api_manager_support import USAGE, _manager, _message, _replies
from tests.db_support import DSN, run_db
from tests.fake_model_api import ResponseScript, completed, text_events

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")


@pytest.mark.parametrize("endings,expected", [
    (["", "Recovered."], "Recovered."),
    (["", "", ""], FAILURE_NOTICE),
])
def test_recovery_records_and_chat_history(monkeypatch, tmp_path, endings, expected):
    monkeypatch.setattr(chat_settings, "_delivery_guard_enabled", True)

    async def run(pool):
        scripts = [ResponseScript(events=text_events(text, usage=USAGE) if text else [completed(usage=USAGE)])
                   for text in endings]
        async with _manager(pool, tmp_path, monkeypatch, scripts) as chat:
            message = _message("Reply briefly.")
            # The dashboard owns web history; seed the incoming message as its channel does.
            await db_chat.save_message(session_id="api-chat", message_id=message.message_id,
                                       role="user", content=message.payload["text"])
            await chat.manager._handle(message)
            (reply,) = _replies(chat.bus)
            assert reply.payload["text"] == expected
            rows = await pool.fetch("SELECT * FROM llm_records WHERE agent_id='manager' ORDER BY id")
            assert len(rows) == len(endings)
            assert len({row["session_id"] for row in rows}) == 1
            for index, row in enumerate(rows[1:], 1):
                assert row["input_text"] == FOLLOWUP_MESSAGE
                assert json.loads(row["metadata"])["delivery_guard"] == {
                    "followup": index, "for": message.message_id}
            assert await pool.fetchval("SELECT count(*) FROM chat_messages WHERE role='user'") == 1
            assert await pool.fetchval("SELECT count(*) FROM chat_messages WHERE content=$1", FOLLOWUP_MESSAGE) == 0
            # API runtime has no MID_TURN_INPUT; hidden turns still work without a resident write.
            assert len(chat.model.requests) == len(endings)

    run_db(run, monkeypatch, timeout=45)


def test_stop_during_followup_keeps_the_original_user_token(monkeypatch, tmp_path):
    monkeypatch.setattr(chat_settings, "_delivery_guard_enabled", True)
    monkeypatch.setattr(chat_settings, "_stop_enabled", True)

    async def run(pool):
        blocked = ResponseScript(events=text_events("Too late.", usage=USAGE), gate=asyncio.Event())
        async with _manager(pool, tmp_path, monkeypatch, [ResponseScript(events=[completed()]), blocked]) as chat:
            message = _message()
            handling = asyncio.create_task(chat.manager._handle(message))
            try:
                await asyncio.wait_for(blocked.gate_reached.wait(), 10)
                assert (await chat.manager.stop_turn(message.message_id, via="web")).value == "stopping"
                await asyncio.wait_for(handling, 10)
                (reply,) = _replies(chat.bus)
                assert reply.payload["outcome"] == "stopped"
                assert reply.payload["text"] != FAILURE_NOTICE
                rows = await pool.fetch("SELECT status FROM llm_records WHERE agent_id='manager' ORDER BY id")
                assert len(rows) == 2 and rows[-1]["status"] == "interrupted"
            finally:
                blocked.gate.set()
                handling.cancel()
                await asyncio.gather(handling, return_exceptions=True)

    run_db(run, monkeypatch, timeout=45)


def test_empty_completion_still_uses_its_existing_fallback(monkeypatch, tmp_path):
    from tests.test_completion_turn_db import _task, _end_and_tell, _goal_status, _events

    monkeypatch.setattr(chat_settings, "_delivery_guard_enabled", True)
    monkeypatch.setattr(chat_settings, "_completion_turns", chat_settings.CompletionTurns.DM_ONLY)

    async def run(pool):
        async with _manager(pool, tmp_path, monkeypatch, [ResponseScript(events=[completed()])]) as chat:
            await _task(pool, tmp_path)
            await _end_and_tell(chat, _goal_status())
            (event,) = await _events(pool)
            assert (event["state"], event["reason"]) == ("fallback", "empty_reply")
            assert await pool.fetchval("SELECT count(*) FROM llm_records WHERE agent_id='manager'") == 1
            assert len(chat.model.requests) == 1

    run_db(run, monkeypatch, timeout=45)
