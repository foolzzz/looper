"""API context handoffs and recovery through the real Manager entry point."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from looper.agents import session_switch
from looper.agents.tiers import manager as manager_module
from looper.db import agent_sessions
from tests.db_support import DSN, run_db
from tests.fake_model_api import ResponseScript, call_events, text_events
from tests.api_manager_support import USAGE, _manager, _message, _replies


HIGH_USAGE = {**USAGE, "input_tokens": 150_001}
SUMMARY = "Continue the requested work; the proposed deletion still needs confirmation."


def _input(request: Any) -> str:
    return json.dumps(request.body["input"], ensure_ascii=False)


def test_context_reason_preserves_the_session_binding() -> None:
    switch = session_switch.context_full(150_001, 140_000, runtime="api", profile="agent", provider="account")
    assert switch.reason is session_switch.SwitchReason.CONTEXT_FULL
    assert switch.describe() == "context full: its context reached 150001 tokens, over its limit of 140000"
    assert (switch.old_runtime, switch.new_runtime, switch.old_profile, switch.new_profile,
            switch.old_provider, switch.new_provider) == ("api", "api", "agent", "agent", "account", "account")


def test_api_handoff_prompt_names_its_fact_and_authority_boundaries() -> None:
    prompt = manager_module.api_handoff_prompt("context full: bounded history", 1234)
    assert prompt.startswith("[HANDOFF]")
    for text in ("context full: bounded history", "1234", "active goal", "constraints", "evidence",
                 "confirmations", "commitments", "next steps", "uncertainty", "invent", "only"):
        assert text in prompt


@pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")
class TestManagerContext:
    def test_full_context_hands_off_before_the_next_user_message(self, monkeypatch, tmp_path: Path) -> None:
        async def run(pool):
            scripts = [ResponseScript(events=text_events("First.", usage=HIGH_USAGE)),
                       ResponseScript(events=text_events(SUMMARY, usage=USAGE)),
                       ResponseScript(events=text_events("Second.", usage=USAGE))]
            async with _manager(pool, tmp_path, monkeypatch, scripts) as chat:
                await chat.manager._handle(_message("First message."))
                old = (await agent_sessions.get_session("manager"))["session_id"]
                await chat.manager._handle(_message("Second message.", 1))
                assert [r.payload["text"] for r in _replies(chat.bus)] == ["First.", "Second."]
                assert "[HANDOFF]" in _input(chat.model.requests[1])
                assert "Second message." not in _input(chat.model.requests[1])
                records = await pool.fetch("SELECT session_id FROM llm_records ORDER BY id")
                assert records[0]["session_id"] == records[1]["session_id"] == old
                assert records[2]["session_id"] != old
        run_db(run, monkeypatch, timeout=45)

    def test_new_context_starts_with_the_reason_and_actual_summary(self, monkeypatch, tmp_path: Path) -> None:
        async def run(pool):
            scripts = [ResponseScript(events=text_events("First.", usage=HIGH_USAGE)),
                       ResponseScript(events=text_events(SUMMARY, usage=USAGE)),
                       ResponseScript(events=text_events("Second.", usage=USAGE))]
            async with _manager(pool, tmp_path, monkeypatch, scripts) as chat:
                await chat.manager._handle(_message())
                await chat.manager._handle(_message("Continue.", 1))
                inputs = chat.model.requests[2].body["input"]
                assert len(inputs) == 1
                text = inputs[0]["content"]
                assert text.startswith("[RESUME]") and "context full:" in text and SUMMARY in text
        run_db(run, monkeypatch, timeout=45)

    def test_context_switch_deletes_the_old_transcript(self, monkeypatch, tmp_path: Path) -> None:
        async def run(pool):
            scripts = [ResponseScript(events=text_events("First.", usage=HIGH_USAGE)),
                       ResponseScript(events=text_events(SUMMARY, usage=USAGE)),
                       ResponseScript(events=text_events("Second.", usage=USAGE))]
            async with _manager(pool, tmp_path, monkeypatch, scripts) as chat:
                await chat.manager._handle(_message())
                old = (await agent_sessions.get_session("manager"))["session_id"]
                assert await pool.fetchval("SELECT count(*) FROM api_session_items WHERE session_id=$1", old) > 0
                await chat.manager._handle(_message("Continue.", 1))
                assert await pool.fetchval("SELECT count(*) FROM api_sessions WHERE session_id=$1", old) == 0
                assert await pool.fetchval("SELECT count(*) FROM api_session_items WHERE session_id=$1", old) == 0
        run_db(run, monkeypatch, timeout=45)

    def test_context_switch_accounts_the_previous_context_before_handoff(self, monkeypatch, tmp_path: Path) -> None:
        async def run(pool):
            scripts = [ResponseScript(events=text_events("First.", usage=HIGH_USAGE)),
                       ResponseScript(events=text_events(SUMMARY, usage=USAGE)),
                       ResponseScript(events=text_events("Second.", usage=USAGE))]
            async with _manager(pool, tmp_path, monkeypatch, scripts) as chat:
                await chat.manager._handle(_message())
                tokens = chat.manager._claude.context_tokens
                await chat.manager._handle(_message("Continue.", 1))
                compactions = await pool.fetchval("SELECT compactions FROM llm_records ORDER BY id DESC LIMIT 1")
                assert json.loads(compactions) == [{"trigger": "auto", "pre_tokens": tokens}]
        run_db(run, monkeypatch, timeout=45)

    def test_handoff_has_no_tools_and_hallucinated_calls_reach_no_ops(self, monkeypatch, tmp_path: Path) -> None:
        async def run(pool):
            scripts = [ResponseScript(events=text_events("First.", usage=HIGH_USAGE)),
                       ResponseScript(events=call_events('{"action":"list"}', name="looper_tasks")),
                       ResponseScript(events=text_events(SUMMARY, usage=USAGE)),
                       ResponseScript(events=text_events("Second.", usage=USAGE))]
            async with _manager(pool, tmp_path, monkeypatch, scripts) as chat:
                await chat.manager._handle(_message())
                await chat.manager._handle(_message("Continue.", 1))
                assert chat.model.requests[1].body.get("tools", []) == []
                assert chat.model.requests[2].body.get("tools", []) == []
                assert await pool.fetchval("SELECT count(*) FROM manager_actions") == 0
                assert SUMMARY in _input(chat.model.requests[3])
        run_db(run, monkeypatch, timeout=45)

    def test_api_handoff_uses_the_original_api_prompt(self, monkeypatch, tmp_path: Path) -> None:
        async def run(pool):
            scripts = [ResponseScript(events=text_events("First.", usage=HIGH_USAGE)),
                       ResponseScript(events=text_events(SUMMARY, usage=USAGE)),
                       ResponseScript(events=text_events("Second.", usage=USAGE))]
            async with _manager(pool, tmp_path, monkeypatch, scripts) as chat:
                await chat.manager._handle(_message())
                tokens, limit = chat.manager._claude.context_tokens, chat.manager._claude.context_limit
                await chat.manager._handle(_message("Continue.", 1))
                expected = manager_module.api_handoff_prompt(
                    f"context full: its context reached {tokens} tokens, over its limit of {limit}",
                    manager_module.HANDOFF_MAX_CHARS)
                assert chat.model.requests[1].body["input"][-1]["content"] == expected
        run_db(run, monkeypatch, timeout=45)

    def test_new_command_removes_the_dropped_api_history(self, monkeypatch, tmp_path: Path) -> None:
        async def run(pool):
            async with _manager(pool, tmp_path, monkeypatch,
                                [ResponseScript(events=text_events("First.", usage=USAGE))]) as chat:
                await chat.manager._handle(_message())
                old = (await agent_sessions.get_session("manager"))["session_id"]
                assert await pool.fetchval("SELECT count(*) FROM api_session_items WHERE session_id=$1", old) > 0
                await chat.manager._handle(_message("/new", 1))
                assert await pool.fetchval("SELECT count(*) FROM api_sessions WHERE session_id=$1", old) == 0
                assert await pool.fetchval("SELECT count(*) FROM api_session_items WHERE session_id=$1", old) == 0
        run_db(run, monkeypatch, timeout=45)

    @pytest.mark.parametrize("damage", ["protocol", "malformed", "version"])
    def test_unresumable_history_is_never_sent_and_answers_once(self, monkeypatch, tmp_path: Path, damage) -> None:
        async def run(pool):
            scripts = [ResponseScript(events=text_events("First.", usage=USAGE)),
                       ResponseScript(events=text_events("Recovered.", usage=USAGE))]
            async with _manager(pool, tmp_path, monkeypatch, scripts) as chat:
                await chat.manager._handle(_message("Secret old history."))
                _replies(chat.bus)
                if damage == "protocol":
                    await pool.execute("UPDATE api_sessions SET protocol='anthropic-messages'")
                else:
                    item = {} if damage == "malformed" else {
                        "version": 999, "kind": "message", "role": "user", "text": "Stored history."}
                    await pool.execute("UPDATE api_session_items SET item=$1::jsonb", json.dumps(item))
                await chat.manager._handle(_message("Answer once.", 1))
                assert [r.payload["text"] for r in _replies(chat.bus)] == ["Recovered."]
                assert len(chat.model.requests) == 2
                assert len(chat.model.requests[1].body["input"]) == 1
        run_db(run, monkeypatch, timeout=45)

    def test_first_request_overflow_retries_with_the_context_reason(self, monkeypatch, tmp_path: Path) -> None:
        async def run(pool):
            scripts = [ResponseScript(events=text_events("First.", usage=USAGE)),
                       ResponseScript(status=400, error={"error": {"code": "context_length_exceeded",
                                                                 "message": "context full"}}),
                       ResponseScript(events=text_events("Recovered.", usage=USAGE))]
            async with _manager(pool, tmp_path, monkeypatch, scripts) as chat:
                await chat.manager._handle(_message())
                _replies(chat.bus)
                await chat.manager._handle(_message("Answer once.", 1))
                assert [r.payload["text"] for r in _replies(chat.bus)] == ["Recovered."]
                assert len(chat.model.requests) == 3
                assert "context full:" in _input(chat.model.requests[2])
        run_db(run, monkeypatch, timeout=45)

    def test_new_session_first_request_overflow_retries_in_a_new_session(self, monkeypatch, tmp_path: Path) -> None:
        async def run(pool):
            scripts = [ResponseScript(status=400, error={"error": {"code": "context_length_exceeded",
                                                                 "message": "context full"}}),
                       ResponseScript(events=text_events("Recovered.", usage=USAGE))]
            async with _manager(pool, tmp_path, monkeypatch, scripts) as chat:
                await chat.manager._handle(_message("Answer once."))
                assert [r.payload["text"] for r in _replies(chat.bus)] == ["Recovered."]
                assert len(chat.model.requests) == 2
                assert "context full:" in _input(chat.model.requests[1])
        run_db(run, monkeypatch, timeout=45)

    def test_repeated_first_request_overflow_is_not_retried_forever(self, monkeypatch, tmp_path: Path) -> None:
        async def run(pool):
            overflow = ResponseScript(status=400, error={"error": {"code": "context_length_exceeded",
                                                                 "message": "context full"}})
            async with _manager(pool, tmp_path, monkeypatch, [overflow, overflow]) as chat:
                await chat.manager._handle(_message("Answer once."))
                (reply,) = _replies(chat.bus)
                assert reply.payload["outcome"] == "failed"
                assert "context window" in reply.payload["text"]
                assert len(chat.model.requests) == 2
        run_db(run, monkeypatch, timeout=45)

    @pytest.mark.parametrize("code", ["invalid_request_error", "context_length_exceeded"])
    def test_failed_handoff_resumes_from_the_brief_alone(self, monkeypatch, tmp_path: Path, code) -> None:
        async def run(pool):
            scripts = [ResponseScript(events=text_events("First.", usage=HIGH_USAGE)),
                       ResponseScript(status=400, error={"error": {"code": code, "message": "no summary"}}),
                       ResponseScript(events=text_events("Recovered.", usage=USAGE))]
            async with _manager(pool, tmp_path, monkeypatch, scripts) as chat:
                await chat.manager._handle(_message())
                _replies(chat.bus)
                await chat.manager._handle(_message("Continue.", 1))
                assert _replies(chat.bus)[0].payload["text"] == "Recovered."
                resumed = _input(chat.model.requests[2])
                assert "context full:" in resumed
                assert "Handoff from your previous session" not in resumed
        run_db(run, monkeypatch, timeout=45)

    def test_later_request_overflow_fails_then_next_turn_switches(self, monkeypatch, tmp_path: Path) -> None:
        async def run(pool):
            scripts = [ResponseScript(events=call_events('{"action":"list"}', name="looper_tasks")),
                       ResponseScript(status=400, error={"error": {"code": "context_length_exceeded",
                                                                 "message": "context full"}}),
                       ResponseScript(events=text_events(SUMMARY, usage=USAGE)),
                       ResponseScript(events=text_events("Recovered.", usage=USAGE))]
            async with _manager(pool, tmp_path, monkeypatch, scripts) as chat:
                await chat.manager._handle(_message())
                (failed,) = _replies(chat.bus)
                assert failed.payload["outcome"] == "failed"
                assert "context window" in failed.payload["text"]
                await chat.manager._handle(_message("Continue.", 1))
                assert _replies(chat.bus)[0].payload["text"] == "Recovered."
                assert "[HANDOFF]" in _input(chat.model.requests[2])
                assert "context full:" in _input(chat.model.requests[3])
        run_db(run, monkeypatch, timeout=45)
