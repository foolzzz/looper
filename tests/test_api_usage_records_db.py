"""API audit records retain priced observations and identify requests with unknown usage."""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

import pytest

from looper.agents.claude_cli import ClaudeRunRecorder
from looper.agents.tiers.manager import StopResult
from tests.db_support import DSN, run_db, until
from tests.fake_model_api import ResponseScript, text_events
from tests.test_api_manager_turns_db import USAGE, _manager, _message, _task_call

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")


async def _record(pool: Any) -> Any:
    (record,) = await pool.fetch("SELECT * FROM llm_records WHERE agent_id='manager'")
    return record


def test_completed_turn_records_catalog_price_and_all_request_tokens(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    async def run(pool: Any) -> None:
        scripts = [_task_call(), ResponseScript(events=text_events("Done.", usage=USAGE))]
        async with _manager(pool, tmp_path, monkeypatch, scripts) as chat:
            await chat.manager._handle(_message())
            record = await _record(pool)
            assert record["status"] == "completed"
            assert record["cost_source"] == "provider_price"
            assert record["cost_usd"] == pytest.approx(0.000068)
            assert (record["input_tokens"], record["output_tokens"], record["cache_read_tokens"],
                    record["cache_creation_tokens"]) == (18, 6, 4, 2)

    run_db(run, monkeypatch, timeout=45)


def test_record_metadata_counts_requests_retries_and_upstream_ids(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    async def run(pool: Any) -> None:
        scripts = [_task_call(), ResponseScript(status=429, headers={"retry-after-ms": "1"}),
                   ResponseScript(events=text_events("Done.", usage=USAGE, id="resp_final"))]
        async with _manager(pool, tmp_path, monkeypatch, scripts) as chat:
            await chat.manager._handle(_message())
            metadata = json.loads((await _record(pool))["metadata"])
            assert metadata["api"] == {
                "protocol": "openai-responses", "requests": 2, "retries": 1,
                "incomplete": 0, "upstream_ids": ["resp_1", "resp_final"],
            }
            assert len(chat.model.requests) == 3

    run_db(run, monkeypatch, timeout=45)


def test_aborted_request_keeps_completed_tokens_and_unknown_total_cost(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    async def run(pool: Any) -> None:
        blocked = ResponseScript(events=text_events("Partial answer.", usage=USAGE), gate=asyncio.Event(),
                                 headers={"x-request-id": "request_aborted"})
        async with _manager(pool, tmp_path, monkeypatch, [_task_call(), blocked]) as chat:
            message = _message()
            answering = asyncio.create_task(chat.manager._handle(message))
            try:
                await asyncio.wait_for(blocked.gate_reached.wait(), 15)
                await until(lambda: chat.manager.turn_for(message.message_id).partial == "Partial answer.")
                assert await chat.manager.stop_turn(message.message_id, via="web") is StopResult.STOPPING
                await asyncio.wait_for(answering, 2)
                record = await _record(pool)
                assert record["status"] == "interrupted"
                assert record["cost_usd"] is None
                assert (record["input_tokens"], record["output_tokens"], record["cache_read_tokens"],
                        record["cache_creation_tokens"]) == (9, 3, 2, 1)
                metadata = json.loads(record["metadata"])
                assert metadata["user_stop"]["usage_complete"] is False
                assert metadata["api"] == {
                    "protocol": "openai-responses", "requests": 2, "retries": 0,
                    "incomplete": 1, "upstream_ids": ["resp_1", "request_aborted"],
                }
            finally:
                answering.cancel()
                await asyncio.gather(answering, return_exceptions=True)

    run_db(run, monkeypatch, timeout=45)


def test_response_without_usage_marks_the_usage_event_incomplete(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    observed = []
    account = ClaudeRunRecorder.account

    def observe(self: ClaudeRunRecorder, event: dict[str, Any]) -> None:
        observed.append(event)
        account(self, event)

    monkeypatch.setattr(ClaudeRunRecorder, "account", observe)

    async def run(pool: Any) -> None:
        async with _manager(pool, tmp_path, monkeypatch,
                            [ResponseScript(events=text_events("Unpriced answer."))]) as chat:
            await chat.manager._handle(_message())
            record = await _record(pool)
            assert record["cost_usd"] is None
            assert json.loads(record["metadata"])["api"]["incomplete"] == 1
            usage_events = [event for event in observed if event.get("type") == "usage"]
            assert usage_events and all(event["usage_complete"] is False for event in usage_events)

    run_db(run, monkeypatch, timeout=45)


@pytest.mark.parametrize("outcome", ["completed", "failed", "cancelled"])
def test_every_request_logs_only_structured_operational_fields(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture, outcome: str,
) -> None:
    caplog.set_level(logging.INFO, logger="looper.agents.runtimes.api")
    private_text = "Synthetic message content that must stay out of request logs."

    async def run(pool: Any) -> None:
        script = (ResponseScript(status=401, headers={"x-request-id": "request_denied-test-key"}) if outcome == "failed"
                  else ResponseScript(events=text_events(private_text, usage=USAGE),
                                      headers={"x-request-id": "request_stream"},
                                      gate=asyncio.Event() if outcome == "cancelled" else None))
        scripts = [_task_call(), script] if outcome == "completed" else [script]
        async with _manager(pool, tmp_path, monkeypatch, scripts) as chat:
            message = _message(private_text)
            answering = asyncio.create_task(chat.manager._handle(message))
            try:
                if outcome == "cancelled":
                    await asyncio.wait_for(script.gate_reached.wait(), 30)
                    await until(lambda: chat.manager.turn_for(message.message_id).partial == private_text)
                    assert await chat.manager.stop_turn(message.message_id, via="web") is StopResult.STOPPING
                await asyncio.wait_for(answering, 30)
                if outcome == "failed":
                    metadata = json.loads((await _record(pool))["metadata"])
                    assert metadata["api"]["upstream_ids"] == ["request_denied-***"]
            finally:
                answering.cancel()
                await asyncio.gather(answering, return_exceptions=True)
        lines = [record for record in caplog.records
                 if record.name == "looper.agents.runtimes.api" and record.getMessage().startswith("api_request ")]
        assert len(lines) == (2 if outcome == "completed" else 1)
        for index, line in enumerate(lines):
            assert line.levelno == logging.INFO
            fields = line.api_request
            assert set(fields) == {
                "agent", "protocol", "model", "host", "status", "duration_ms", "first_token_ms", "retries",
                "input_tokens", "cache_read_tokens", "cache_write_tokens", "output_tokens", "complete", "upstream_id",
                "request", "outcome",
            }
            assert fields["request"] == index
            assert fields["outcome"] == {"completed": "answered", "failed": "failed", "cancelled": "cancelled"}[outcome]
            assert (fields["agent"], fields["protocol"], fields["model"], fields["host"]) == (
                "manager", "openai-responses", "test-model", "127.0.0.1")
            assert fields["status"] == (401 if outcome == "failed" else 200)
            assert isinstance(fields["duration_ms"], int) and fields["duration_ms"] >= 0
            assert fields["retries"] == 0
            assert fields["complete"] is (outcome == "completed")
            assert fields["upstream_id"] == {
                "completed": "resp_1", "failed": "request_denied-***", "cancelled": "request_stream",
            }[outcome]
            if outcome == "completed":
                assert (fields["input_tokens"], fields["cache_read_tokens"], fields["cache_write_tokens"],
                        fields["output_tokens"]) == (9, 2, 1, 3)
                if index == 0:  # A tool-only response has no text token.
                    assert fields["first_token_ms"] is None
                else:
                    assert isinstance(fields["first_token_ms"], int) and fields["first_token_ms"] >= 0
            else:
                assert all(fields[key] is None for key in
                           ("input_tokens", "cache_read_tokens", "cache_write_tokens", "output_tokens"))
        for record in caplog.records:
            assert "test-key" not in repr(record.__dict__)
            assert private_text not in repr(record.__dict__)

    run_db(run, monkeypatch, timeout=45)
