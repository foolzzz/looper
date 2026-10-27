"""The Manager answers through a loopback model API with real registry and audit rows.

The existing CLI launch contract is covered by tests/test_current_tier_launch_db.py;
it exercises current registry declarations and persisted tier records.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from looper.db import agent_sessions
from tests.api_manager_support import CUSTOM_PROMPT, USAGE, _manager, _message, _replies
from tests.db_support import DSN, run_db
from tests.fake_model_api import ResponseScript, call_events, call_item, completed, text_events

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")



def _task_call() -> ResponseScript:
    events = call_events('{"action":"list"}', name="looper_tasks")
    events[-1] = completed([call_item('{"action":"list"}', name="looper_tasks")], usage=USAGE)
    return ResponseScript(events=events)


def test_api_manager_reply_persists_priced_usage_tool_audit_and_runtime(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    async def run(pool: Any) -> None:
        scripts = [_task_call(), ResponseScript(events=text_events("No tasks are waiting.", usage=USAGE))]
        async with _manager(pool, tmp_path, monkeypatch, scripts) as chat:
            message = _message()
            await chat.manager._handle(message)
            (reply,) = _replies(chat.bus)
            assert reply.payload["text"] == "No tasks are waiting."
            assert reply.payload["outcome"] == "answered"
            (record,) = await pool.fetch("SELECT * FROM llm_records WHERE agent_id='manager'")
            assert record["status"] == "completed"
            assert record["cost_source"] == "provider_price"
            assert record["cost_usd"] == pytest.approx(0.000068)
            assert (record["input_tokens"], record["output_tokens"], record["cache_read_tokens"],
                    record["cache_creation_tokens"]) == (18, 6, 4, 2)
            (action,) = await pool.fetch("SELECT * FROM manager_actions")
            assert (action["tool"], action["status"], action["channel"], action["message_id"]) == (
                "looper_tasks", "ok", "web", message.message_id)
            assert json.loads(action["arguments"]) == {"action": "list"}
            saved = await agent_sessions.get_session("manager")
            assert saved["runtime_type"] == "api"
            assert saved["session_id"] == record["session_id"]
            assert saved["session_id"].startswith("api-")
            results = [item for item in chat.model.requests[1].body["input"]
                       if item.get("type") == "function_call_output"]
            assert len(results) == 1 and results[0]["call_id"] == "call_1"
            assert json.loads(results[0]["output"])["status"] == "ok"

    run_db(run, monkeypatch, timeout=45)


def test_api_manager_answers_without_tool_calls_and_keeps_one_session_across_turns(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    async def run(pool: Any) -> None:
        scripts = [ResponseScript(events=text_events(answer, usage=USAGE)) for answer in ("First.", "Second.")]
        async with _manager(pool, tmp_path, monkeypatch, scripts) as chat:
            for index in range(2):
                await chat.manager._handle(_message("Reply briefly.", index))
            assert [reply.payload["text"] for reply in _replies(chat.bus)] == ["First.", "Second."]
            rows = await pool.fetch("SELECT session_id, cost_usd FROM llm_records ORDER BY id")
            assert len(rows) == len(chat.model.requests) == 2
            assert rows[0]["session_id"] == rows[1]["session_id"]
            saved = await agent_sessions.get_session("manager")
            assert saved["session_totals"]["input_tokens"] == 18
            assert saved["session_totals"]["output_tokens"] == 6
            assert sum(row["cost_usd"] for row in rows) == pytest.approx(0.000068)
            assert await pool.fetchval("SELECT count(*) FROM manager_actions") == 0

    run_db(run, monkeypatch, timeout=45)


def test_unknown_saved_api_session_retries_the_message_once_in_a_new_session(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    async def run(pool: Any) -> None:
        async with _manager(pool, tmp_path, monkeypatch,
                            [ResponseScript(events=text_events("Recovered.", usage=USAGE))], saved=True) as chat:
            await chat.manager._handle(_message("Answer this once."))
            (reply,) = _replies(chat.bus)
            assert reply.payload["text"] == "Recovered."
            assert len(chat.model.requests) == 1
            rows = await pool.fetch("SELECT status, error, session_id FROM llm_records ORDER BY id")
            assert len(rows) == 2
            assert rows[0]["status"] == "failed" and "could not be resumed" in rows[0]["error"]
            assert rows[1]["status"] == "completed" and rows[1]["session_id"] != "api-missing"
            assert (await agent_sessions.get_session("manager"))["session_id"] == rows[1]["session_id"]

    run_db(run, monkeypatch, timeout=45)


def test_api_instructions_match_preview_with_custom_prompt_before_runtime_note(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    from looper.agents.runtimes.api.adapter import RUNTIME_NOTE

    async def run(pool: Any) -> None:
        async with _manager(pool, tmp_path, monkeypatch,
                            [ResponseScript(events=text_events("Ready.", usage=USAGE))]) as chat:
            await chat.manager._handle(_message())
            assert _replies(chat.bus)[0].payload["outcome"] == "answered"
            process = chat.manager._process
            (preview,) = process.launch.runtime.prompt_blocks(process.launch, process.system_prompt.text)
            instructions = chat.model.requests[0].body["instructions"]
            assert instructions == preview.text
            looper_prompt = process.system_prompt.text.removeprefix(CUSTOM_PROMPT + "\n\n")
            assert instructions == CUSTOM_PROMPT + "\n\n" + RUNTIME_NOTE + "\n\n---\n\n" + looper_prompt
            assert instructions.count(RUNTIME_NOTE) == 1

    run_db(run, monkeypatch, timeout=45)
