"""API context handoffs and recovery through the real Manager entry point."""
from __future__ import annotations

import json
from contextlib import AsyncExitStack
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from looper.agents import session_switch
from looper.agents.tiers import manager as manager_module
from looper.db import agent_sessions
from tests.db_support import DSN, run_db
from tests.fake_model_api import ResponseScript, call_events, event, text_events
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
    def test_rebuilt_manager_hands_off_stored_context_before_the_next_message(self, monkeypatch, tmp_path):
        from manager_mcp_stand_in import manager_endpoint

        async def run(pool):
            scripts = [ResponseScript(events=text_events("Keep this evidence.", usage=HIGH_USAGE)),
                       ResponseScript(events=text_events(SUMMARY, usage=USAGE)),
                       ResponseScript(events=text_events("Continued.", usage=USAGE))]
            async with _manager(pool, tmp_path, monkeypatch, scripts) as chat:
                await chat.manager._handle(_message("Keep the early constraints."))
                old = (await agent_sessions.get_session("manager"))["session_id"]
                assert await pool.fetchval(
                    "SELECT context_tokens FROM api_sessions WHERE session_id=$1", old) == 150_001
                await chat.manager.stop()
                rebuilt = manager_module.ManagerAgent(
                    config=manager_module.ManagerConfig(work_dir=tmp_path), runtime_root=tmp_path)
                rebuilt._bus = chat.bus
                try:
                    async with manager_endpoint(
                            rebuilt, chat.bus, SimpleNamespace(has_planner=lambda _: False), tmp_path):
                        _replies(chat.bus)
                        await rebuilt._handle(_message("Continue after restart.", 1))
                        assert "[HANDOFF]" in _input(chat.model.requests[1])
                        assert "Continue after restart." not in _input(chat.model.requests[1])
                        assert "Keep this evidence." in _input(chat.model.requests[1])
                        assert SUMMARY in _input(chat.model.requests[2])
                        assert [r.payload["text"] for r in _replies(chat.bus)] == ["Continued."]
                        compactions = await pool.fetchval(
                            "SELECT compactions FROM llm_records ORDER BY id DESC LIMIT 1")
                        assert json.loads(compactions) == [{"trigger": "auto", "pre_tokens": 150_001}]
                        assert await pool.fetchval(
                            "SELECT count(*) FROM api_sessions WHERE session_id=$1", old) == 0
                finally:
                    await rebuilt.stop()
        run_db(run, monkeypatch, timeout=45)

    @pytest.mark.parametrize("restart", [False, True])
    @pytest.mark.parametrize("code", ["invalid_api_key", "context_length_exceeded"])
    def test_handoff_survives_failed_first_request_until_history_is_committed(
        self, monkeypatch, tmp_path: Path, restart: bool, code: str,
    ) -> None:
        from manager_mcp_stand_in import manager_endpoint

        async def run(pool):
            rejection = ResponseScript(status=401 if code == "invalid_api_key" else 400,
                                       error={"error": {"code": code, "message": "request rejected"}})
            scripts = [ResponseScript(events=text_events("First.", usage=HIGH_USAGE)),
                       ResponseScript(events=text_events(SUMMARY, usage=USAGE)), rejection]
            if code == "context_length_exceeded":
                scripts.append(rejection)  # The automatic retry also fails.
            scripts += [ResponseScript(events=text_events("Recovered.", usage=USAGE)),
                        ResponseScript(events=text_events("Still here.", usage=USAGE))]
            async with _manager(pool, tmp_path, monkeypatch, scripts) as chat:
                await chat.manager._handle(_message("Keep the early constraints."))
                old = (await agent_sessions.get_session("manager"))["session_id"]
                _replies(chat.bus)
                await chat.manager._handle(_message("Continue.", 1))
                assert _replies(chat.bus)[0].payload["outcome"] == "failed"
                assert SUMMARY in _input(chat.model.requests[2])
                assert (await agent_sessions.get_session("manager"))["pending_handoff"] == SUMMARY
                assert await pool.fetchval("SELECT count(*) FROM api_sessions WHERE session_id=$1", old) == 0
                manager = chat.manager
                async with AsyncExitStack() as stack:
                    if restart:
                        await manager.stop()
                        manager = manager_module.ManagerAgent(
                            config=manager_module.ManagerConfig(work_dir=tmp_path), runtime_root=tmp_path)
                        manager._bus = chat.bus
                        stack.push_async_callback(manager.stop)
                        await stack.enter_async_context(manager_endpoint(
                            manager, chat.bus, SimpleNamespace(has_planner=lambda _: False), tmp_path))
                    await manager._handle(_message("Try again.", 2))
                    assert [r.payload["text"] for r in _replies(chat.bus)] == ["Recovered."]
                    recovered = chat.model.requests[-1].body["input"]
                    assert SUMMARY in json.dumps(recovered)
                    assert len(recovered) == 1 and "Try again." in recovered[0]["content"]
                    assert "pending_handoff" not in await agent_sessions.get_session("manager")
                    await manager._handle(_message("Continue normally.", 3))
                    assert [r.payload["text"] for r in _replies(chat.bus)] == ["Still here."]
                    assert _input(chat.model.requests[-1]).count(SUMMARY) == 1
                    assert "[RESUME]" not in chat.model.requests[-1].body["input"][-1]["content"]
                    assert len(chat.model.requests) == len(scripts)
        run_db(run, monkeypatch, timeout=45)

    def test_failed_handoff_persistence_retains_old_history_and_session_pointer(
        self, monkeypatch, tmp_path: Path,
    ) -> None:
        async def run(pool):
            scripts = [ResponseScript(events=text_events("Keep this evidence.", usage=HIGH_USAGE)),
                       ResponseScript(events=text_events(SUMMARY, usage=USAGE))]
            async with _manager(pool, tmp_path, monkeypatch, scripts) as chat:
                await chat.manager._handle(_message("Keep every change read-only."))
                old = (await agent_sessions.get_session("manager"))["session_id"]
                _replies(chat.bus)
                await pool.execute("ALTER TABLE agent_sessions ADD CONSTRAINT reject_handoff "
                                   "CHECK (pending_handoff IS NULL)")
                await chat.manager._handle(_message("Continue.", 1))
                assert _replies(chat.bus)[0].payload["outcome"] == "failed"
                assert len(chat.model.requests) == 2
                assert (await agent_sessions.get_session("manager"))["session_id"] == old
                assert chat.manager._session_id == old
                history = await pool.fetchval(
                    "SELECT jsonb_agg(item ORDER BY seq)::text FROM api_session_items WHERE session_id=$1", old)
                assert "Keep this evidence." in history and SUMMARY in history
        run_db(run, monkeypatch, timeout=45)

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
                       ResponseScript(events=text_events(SUMMARY, usage=USAGE)),
                       ResponseScript(events=text_events("Recovered.", usage=USAGE))]
            async with _manager(pool, tmp_path, monkeypatch, scripts) as chat:
                await chat.manager._handle(_message())
                _replies(chat.bus)
                await chat.manager._handle(_message("Answer once.", 1))
                assert [r.payload["text"] for r in _replies(chat.bus)] == ["Recovered."]
                assert len(chat.model.requests) == 4
                assert "context full:" in _input(chat.model.requests[3])
                assert SUMMARY in _input(chat.model.requests[3])
        run_db(run, monkeypatch, timeout=45)

    @pytest.mark.parametrize("restart", [False, True])
    def test_first_request_overflow_summarizes_old_history_before_deleting_it(
        self, monkeypatch, tmp_path: Path, restart: bool,
    ) -> None:
        async def run(pool):
            import asyncio

            gate = asyncio.Event()
            summary = ResponseScript(events=text_events(SUMMARY, usage=USAGE), gate=gate)
            scripts = [ResponseScript(events=call_events('{"action":"list"}', name="looper_tasks")),
                       ResponseScript(events=text_events("Audit evidence: 17 entries.", usage=USAGE)),
                       ResponseScript(status=400, error={"error": {"code": "context_length_exceeded",
                                                                 "message": "context full"}}),
                       summary, ResponseScript(events=text_events("Recovered.", usage=USAGE))]
            async with _manager(pool, tmp_path, monkeypatch, scripts) as chat:
                await chat.manager._handle(_message("Keep all changes read-only."))
                old = (await agent_sessions.get_session("manager"))["session_id"]
                count = await pool.fetchval("SELECT count(*) FROM api_session_items WHERE session_id=$1", old)
                assert count > 0
                if restart:
                    await chat.manager._claude.stop()
                _replies(chat.bus)
                pasted = "New logs: " + "log line\n" * 10_000
                turn = asyncio.create_task(chat.manager._handle(_message(pasted, 1)))
                try:
                    await asyncio.wait_for(summary.gate_reached.wait(), 10)
                    handoff = chat.model.requests[3].body
                    assert handoff["tools"] == []
                    # The failed request added only its new message; no part of it reaches the summary.
                    assert handoff["input"][:-1] == chat.model.requests[2].body["input"][:-1]
                    assert "[HANDOFF]" in _input(chat.model.requests[3])
                    assert "New logs:" not in _input(chat.model.requests[3])
                    assert "Keep all changes read-only." in _input(chat.model.requests[3])
                    assert "Audit evidence: 17 entries." in _input(chat.model.requests[3])
                    assert any(item.get("type") == "function_call_output" for item in handoff["input"])
                    assert await pool.fetchval(
                        "SELECT count(*) FROM api_session_items WHERE session_id=$1", old) == count
                    assert (await agent_sessions.get_session("manager"))["session_id"] == old
                finally:
                    gate.set()
                    await turn
                assert [r.payload["text"] for r in _replies(chat.bus)] == ["Recovered."]
                assert len(chat.model.requests) == 5
                resumed = chat.model.requests[4].body["input"][0]["content"]
                assert SUMMARY in resumed and pasted in resumed
                assert "context full:" in resumed
                assert await pool.fetchval("SELECT count(*) FROM api_sessions WHERE session_id=$1", old) == 0
                assert await pool.fetchval("SELECT count(*) FROM api_session_items WHERE session_id=$1", old) == 0
        run_db(run, monkeypatch, timeout=45)

    @pytest.mark.parametrize("code", ["invalid_request_error", "context_length_exceeded"])
    def test_first_request_overflow_keeps_old_history_when_summary_fails(
        self, monkeypatch, tmp_path: Path, code: str,
    ) -> None:
        async def run(pool):
            scripts = [ResponseScript(events=text_events("Keep this evidence.", usage=USAGE)),
                       ResponseScript(status=400, error={"error": {"code": "context_length_exceeded",
                                                                 "message": "context full"}}),
                       ResponseScript(status=400, error={"error": {"code": code, "message": "no summary"}})]
            async with _manager(pool, tmp_path, monkeypatch, scripts) as chat:
                await chat.manager._handle(_message("Keep every change read-only."))
                old = (await agent_sessions.get_session("manager"))["session_id"]
                count = await pool.fetchval("SELECT count(*) FROM api_session_items WHERE session_id=$1", old)
                assert count > 0
                _replies(chat.bus)
                await chat.manager._handle(_message("New logs.", 1))
                (reply,) = _replies(chat.bus)
                assert reply.payload["outcome"] == "failed"
                assert len(chat.model.requests) == 3
                assert "[HANDOFF]" in _input(chat.model.requests[2])
                assert (await agent_sessions.get_session("manager"))["session_id"] == old
                assert await pool.fetchval(
                    "SELECT count(*) FROM api_session_items WHERE session_id=$1", old) == count
        run_db(run, monkeypatch, timeout=45)

    @pytest.mark.parametrize("handoff_ready", [False, True])
    def test_new_session_first_request_overflow_retries_in_a_new_session(
        self, monkeypatch, tmp_path: Path, handoff_ready: bool,
    ) -> None:
        async def run(pool):
            scripts = [ResponseScript(status=400, error={"error": {"code": "context_length_exceeded",
                                                                 "message": "context full"}}),
                       ResponseScript(events=text_events("Recovered.", usage=USAGE))]
            if handoff_ready:
                scripts[:0] = [ResponseScript(events=text_events("First.", usage=HIGH_USAGE)),
                               ResponseScript(events=text_events(SUMMARY, usage=USAGE))]
            async with _manager(pool, tmp_path, monkeypatch, scripts) as chat:
                if handoff_ready:
                    await chat.manager._handle(_message("Keep the early constraints."))
                    _replies(chat.bus)
                await chat.manager._handle(_message("Answer once.", 1))
                assert [r.payload["text"] for r in _replies(chat.bus)] == ["Recovered."]
                assert len(chat.model.requests) == len(scripts)
                assert "context full:" in _input(chat.model.requests[-1])
                if handoff_ready:
                    assert SUMMARY in _input(chat.model.requests[-2])
                    assert SUMMARY in _input(chat.model.requests[-1])
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
    def test_failed_handoff_retains_history_until_a_summary_succeeds(self, monkeypatch, tmp_path: Path, code) -> None:
        async def run(pool):
            scripts = [ResponseScript(events=text_events("First.", usage=HIGH_USAGE)),
                       ResponseScript(status=400, error={"error": {"code": code, "message": "no summary"}}),
                       ResponseScript(events=text_events(SUMMARY, usage=USAGE)),
                       ResponseScript(events=text_events("Recovered.", usage=USAGE))]
            async with _manager(pool, tmp_path, monkeypatch, scripts) as chat:
                await chat.manager._handle(_message())
                old = (await agent_sessions.get_session("manager"))["session_id"]
                count = await pool.fetchval("SELECT count(*) FROM api_session_items WHERE session_id=$1", old)
                assert count > 0
                _replies(chat.bus)
                await chat.manager._handle(_message("Continue.", 1))
                assert _replies(chat.bus)[0].payload["outcome"] == "failed"
                assert len(chat.model.requests) == 2
                assert (await agent_sessions.get_session("manager"))["session_id"] == old
                assert await pool.fetchval(
                    "SELECT count(*) FROM api_session_items WHERE session_id=$1", old) == count
                await chat.manager._handle(_message("Continue after the failed summary.", 2))
                assert _replies(chat.bus)[0].payload["text"] == "Recovered."
                assert len(chat.model.requests) == 4
                assert chat.model.requests[2].body["tools"] == []
                assert "First." in _input(chat.model.requests[2])
                resumed = _input(chat.model.requests[3])
                assert "context full:" in resumed
                assert "Handoff from your previous session" in resumed and SUMMARY in resumed
                assert await pool.fetchval("SELECT count(*) FROM api_sessions WHERE session_id=$1", old) == 0
        run_db(run, monkeypatch, timeout=45)

    @pytest.mark.parametrize("saved_history", [False, True])
    def test_overflow_after_visible_text_fails_without_replay_and_next_turn_recovers(
        self, monkeypatch, tmp_path: Path, saved_history: bool,
    ) -> None:
        async def run(pool):
            overflow = ResponseScript(events=[
                event("response.output_text.delta", output_index=0, item_id="msg_1",
                      content_index=0, delta="Unfinished prefix."),
                event("response.failed", response={"id": "resp_overflow", "status": "failed",
                      "error": {"code": "context_length_exceeded", "message": "full"}}),
            ])
            scripts = ([ResponseScript(events=text_events("Earlier evidence.", usage=USAGE))]
                       if saved_history else [])
            scripts.append(overflow)
            if saved_history:
                scripts.append(ResponseScript(events=text_events(SUMMARY, usage=USAGE)))
            scripts.append(ResponseScript(events=text_events("Recovered.", usage=USAGE)))
            async with _manager(pool, tmp_path, monkeypatch, scripts) as chat:
                if saved_history:
                    await chat.manager._handle(_message("Keep the evidence."))
                    _replies(chat.bus)
                await chat.manager._handle(_message("Answer once.", 1))
                (failed,) = _replies(chat.bus)
                assert failed.payload["outcome"] == "failed"
                assert "context window" in failed.payload["text"]
                assert len(chat.model.requests) == 1 + int(saved_history)
                await chat.manager._handle(_message("Continue.", 2))
                assert _replies(chat.bus)[0].payload["text"] == "Recovered."
                assert len(chat.model.requests) == len(scripts)
                if saved_history:
                    assert "Earlier evidence." in _input(chat.model.requests[2])
                    assert "Unfinished prefix." not in _input(chat.model.requests[2])
                    assert SUMMARY in _input(chat.model.requests[3])
                else:
                    assert chat.model.requests[1].body["tools"]
                    assert len(chat.model.requests[1].body["input"]) == 1
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
