"""User stop cancels API replies through the dashboard and channel entry points."""
from __future__ import annotations

import asyncio
from dataclasses import replace
import json
from pathlib import Path
from typing import Any

import pytest

from looper.agents import chat_settings
from looper.agents.message import MANAGER_ADDRESS, MessageEnvelope, MessageType, channel_address
from looper.agents.ops import Risk, Tool
from looper.agents.runtimes import StopMethod
from looper.agents.runtimes.api.loop import NormaAgentLoop, ToolCallItem, ToolResultItem
from looper.agents.tiers.manager import StopResult
from tests.db_support import DSN, run_db, until
from tests.fake_model_api import ResponseScript, call_item, completed, text_events
from tests.test_api_manager_turns_db import USAGE, _manager, _message, _replies
from tests.test_api_v1_auth import _bearer, _send, _serving
from tests.test_operator_auth import TOKEN

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")


@pytest.fixture(autouse=True)
def stop_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(chat_settings, "_stop_enabled", True)


async def _web_stop(chat: Any, root: Path, message: MessageEnvelope) -> tuple[dict[str, Any], float]:
    from looper.agents import api_v1
    from looper.agents.ops.clients import ClientOps
    from looper.agents.ops.service import Platform

    root.mkdir()
    async with _serving(root, listen=True) as server:
        server._manager = chat.manager
        server._client_ops = ClientOps(Platform(bus=chat.bus, task_router=None, manager=chat.manager))
        api_v1.find("POST", "/api/v1/operator_chat/stop")
        started = asyncio.get_running_loop().time()
        status, _, answer = await _send(server, "POST /api/v1/operator_chat/stop", _bearer(TOKEN),
                                       json.dumps({"message_id": message.message_id}))
        assert status == 200, answer
        return answer["result"], started


def test_dashboard_stop_closes_stream_and_keeps_only_partial_reply_and_pre_stop_events(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    async def run(pool: Any) -> None:
        script = ResponseScript(events=text_events("Partial answer.", usage=USAGE), gate=asyncio.Event())
        async with _manager(pool, tmp_path, monkeypatch, [script]) as chat:
            message = _message()
            answering = asyncio.create_task(chat.manager._handle(message))
            try:
                await asyncio.wait_for(script.gate_reached.wait(), 30)
                await until(lambda: chat.manager.turn_for(message.message_id).partial == "Partial answer.")
                before = await pool.fetch("SELECT seq, event FROM llm_record_events ORDER BY seq")
                assert [json.loads(row["event"])["type"] for row in before] == ["system"]
                stop, started = await _web_stop(chat, tmp_path / "http", message)
                assert stop["state"] == "stopping"
                await asyncio.wait_for(answering, 2)
                assert asyncio.get_running_loop().time() - started < 2
                await asyncio.wait_for(script.closed.wait(), 1)
                (reply,) = _replies(chat.bus)
                assert reply.payload["outcome"] == "stopped"
                assert reply.payload["text"] == "Partial answer.\n\n（已停止）"
                (record,) = await pool.fetch("SELECT * FROM llm_records")
                assert record["status"] == "interrupted" and record["output_text"] == "Partial answer."
                assert all(record[field] is None for field in (
                    "cost_usd", "input_tokens", "output_tokens", "cache_read_tokens", "cache_creation_tokens"))
                assert json.loads(record["metadata"])["user_stop"]["method"] == "cancel"
                assert await pool.fetch("SELECT seq, event FROM llm_record_events ORDER BY seq") == before
                session = chat.manager._claude
                assert await session._transcripts[session._session].items() == []
            finally:
                answering.cancel()
                await asyncio.gather(answering, return_exceptions=True)

    run_db(run, monkeypatch, timeout=45)


def test_stop_during_ops_call_keeps_its_result_and_marks_remaining_calls_stopped(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    async def run(pool: Any) -> None:
        entered, release = asyncio.Event(), asyncio.Event()
        ran = []

        async def operate(call: Any) -> dict[str, Any]:
            ran.append(call.arguments["number"])
            entered.set()
            await release.wait()
            return {"finished": call.arguments["number"]}

        tool = Tool("looper_wait", "Wait for a synthetic operation.",
                    {"type": "object", "properties": {"number": {"type": "integer"}},
                     "required": ["number"]}, Risk.READ, operate)
        calls = [call_item(json.dumps({"number": number}), name=tool.name,
                           call_id=f"call_{number}", id=f"fc_{number}") for number in (1, 2)]
        script = ResponseScript(events=[completed(calls, usage=USAGE)])
        async with _manager(pool, tmp_path, monkeypatch, [script]) as chat:
            chat.ops.set_scope(replace(chat.ops.scope, tools={tool.name: tool}))
            message = _message()
            answering = asyncio.create_task(chat.manager._handle(message))
            try:
                await asyncio.wait_for(entered.wait(), 15)
                assert await chat.manager.stop_turn(message.message_id, via="web") is StopResult.STOPPING
                assert await chat.manager.stop_turn(message.message_id, via="web") is StopResult.ALREADY_STOPPING
                assert not answering.done()
                release.set()
                await asyncio.wait_for(answering, 2)
                assert ran == [1] and len(chat.model.requests) == 1
                session = chat.manager._claude
                history = await session._transcripts[session._session].items()
                assert len([item for item in history if isinstance(item, ToolCallItem)]) == 2
                first, second = [item for item in history if isinstance(item, ToolResultItem)]
                assert first.call_id == "call_1" and first.ran and not first.is_error
                assert json.loads(first.text)["result"] == {"finished": 1}
                assert second.call_id == "call_2" and not second.ran and second.is_error
                assert json.loads(second.text)["error"]["code"] == "stopped"
                (record,) = await pool.fetch("SELECT cost_usd, metadata FROM llm_records")
                assert record["cost_usd"] == pytest.approx(0.000034)
                assert json.loads(record["metadata"])["user_stop"]["usage_complete"] is True
            finally:
                release.set()
                answering.cancel()
                await asyncio.gather(answering, return_exceptions=True)

    run_db(run, monkeypatch, timeout=45)


def test_stop_before_first_api_request_sends_nothing_and_ends_stopped(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    from looper.agents.runtimes.api.session import ApiSession

    run_turn = ApiSession.run_turn
    observed = []

    async def stop_before_run(self: ApiSession, text: str, on_event: Any, **kwargs: Any) -> Any:
        assert await manager.stop_turn(message.message_id, via="web") is StopResult.STOPPING
        result = await run_turn(self, text, on_event, **kwargs)
        observed.append(result)
        return result

    monkeypatch.setattr(ApiSession, "run_turn", stop_before_run)

    async def run(pool: Any) -> None:
        nonlocal manager, message
        async with _manager(pool, tmp_path, monkeypatch, []) as chat:
            manager, message = chat.manager, _message()
            await manager._handle(message)
            assert not chat.model.requests
            assert _replies(chat.bus)[0].payload["outcome"] == "stopped"
            assert observed[0].stopped is StopMethod.CANCEL and observed[0].usage_complete
            assert await pool.fetchval("SELECT status FROM llm_records") == "interrupted"
            assert await pool.fetchval("SELECT count(*) FROM llm_record_events") == 0

    manager = message = None
    run_db(run, monkeypatch, timeout=45)


@pytest.mark.parametrize("channel", ["telegram", "lark", "lark_bot"])
def test_channel_stop_command_cancels_api_manager_without_real_accounts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, channel: str,
) -> None:
    async def run(pool: Any) -> None:
        source = channel_address(channel)

        def message(text: str) -> MessageEnvelope:
            return MessageEnvelope.create(source=source, target=MANAGER_ADDRESS,
                message_type=MessageType.USER_MESSAGE, payload={"text": text, "channel_context": {
                    "chat_id": 99 if channel == "telegram" else "oc_test", "user_id": 7}})

        script = ResponseScript(events=text_events("Partial.", usage=USAGE), gate=asyncio.Event())
        async with _manager(pool, tmp_path, monkeypatch, [script]) as chat:
            chat.manager._running = True
            chat.bus.subscribe_topic(MessageType.USER_MESSAGE.value, chat.manager._receive, consumes=True)
            answering = asyncio.create_task(chat.manager._handle(message("Answer briefly.")))
            try:
                await asyncio.wait_for(script.gate_reached.wait(), 30)
                await chat.bus.publish(message("/stop"))
                await asyncio.wait_for(answering, 2)
                queue = chat.bus.get_queue(source.agent_id)
                replies = []
                while not queue.empty():
                    envelope = queue.get_nowait()
                    if envelope.message_type == MessageType.AGENT_REPLY:
                        replies.append(envelope.payload)
                assert any(reply["text"] == "已停止当前回复。" for reply in replies)
                assert any(reply.get("outcome") == "stopped" for reply in replies)
                assert len(chat.model.requests) == 1
            finally:
                answering.cancel()
                await asyncio.gather(answering, return_exceptions=True)

    run_db(run, monkeypatch, timeout=45)


def test_next_turn_receives_stop_note_and_only_completed_history(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    async def run(pool: Any) -> None:
        script = ResponseScript(events=text_events("Unfinished.", usage=USAGE), gate=asyncio.Event())
        scripts = [script, ResponseScript(events=text_events("Next reply.", usage=USAGE))]
        async with _manager(pool, tmp_path, monkeypatch, scripts) as chat:
            first = _message()
            answering = asyncio.create_task(chat.manager._handle(first))
            try:
                await asyncio.wait_for(script.gate_reached.wait(), 30)
                await chat.manager.stop_turn(first.message_id, via="web")
                await asyncio.wait_for(answering, 2)
                assert chat.manager._stopped_notes
                await chat.manager._handle(_message("Continue.", 1))
                sent = chat.model.requests[1].body["input"]
                assert len(sent) == 1 and sent[0]["role"] == "user"
                assert "[Looper] The user stopped your previous reply" in sent[0]["content"]
                assert chat.manager._stopped_notes == []
                assert _replies(chat.bus)[-1].payload["text"] == "Next reply."
            finally:
                answering.cancel()
                await asyncio.gather(answering, return_exceptions=True)

    run_db(run, monkeypatch, timeout=45)


def test_stop_after_final_response_preserves_complete_usage_and_ends_stopped(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    async def run(pool: Any) -> None:
        script = ResponseScript(events=text_events("Complete answer.", usage=USAGE))
        async with _manager(pool, tmp_path, monkeypatch, [script]) as chat:
            message = _message()
            run_loop = NormaAgentLoop.run

            async def stop_after_response(self: Any, **kwargs: Any) -> Any:
                result = await run_loop(self, **kwargs)
                assert await chat.manager.stop_turn(message.message_id, via="web") is StopResult.STOPPING
                return result

            monkeypatch.setattr(NormaAgentLoop, "run", stop_after_response)
            await chat.manager._handle(message)
            assert _replies(chat.bus)[0].payload["outcome"] == "stopped"
            (record,) = await pool.fetch("SELECT * FROM llm_records")
            assert record["status"] == "interrupted"
            assert record["cost_usd"] == pytest.approx(0.000034)
            stop = json.loads(record["metadata"])["user_stop"]
            assert stop["method"] == "cancel" and stop["usage_complete"] is True

    run_db(run, monkeypatch, timeout=45)
