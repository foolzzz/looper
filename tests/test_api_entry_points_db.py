"""The in-process Manager runtime at registry, chat, channel and doctor entry points."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import json
import importlib.metadata
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import pytest

from looper.agents import chat_settings
from looper.agents.channels import lark_cli
from looper.agents.channels.lark import LarkChannelAdapter
from looper.agents.channels.telegram import TelegramChannelAdapter
from looper.agents.message import MessageType, channel_address
from looper.db import agent_sessions
from tests.db_support import DSN, run_db
from tests.fake_model_api import FakeModelAPI, ResponseScript, text_events
from tests.test_agent_channels import OPEN_ID, _allowlist, _lark_event, _tg_update
from tests.test_api_manager_turns_db import USAGE, _manager, _message, _profile
from tests.test_api_v1_auth import _bearer, _send, _serving
from tests.test_api_v1_registry_db import _api
from tests.test_cli_entry import _doctor_env, _run_looper
from tests.test_operator_auth import TOKEN

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")


@asynccontextmanager
async def _commands(chat: Any, root: Path, monkeypatch: pytest.MonkeyPatch, channel: str):
    """Use adapter handlers without polling Telegram or starting lark-cli."""
    async def names(*args: Any, **kwargs: Any) -> dict[str, str]:
        return {OPEN_ID: "Example sender"}

    async def react(*args: Any, **kwargs: Any) -> None:
        pass

    monkeypatch.setattr(lark_cli, "names", names)
    monkeypatch.setattr(LarkChannelAdapter, "_react", react)
    if channel == "telegram":
        adapter = TelegramChannelAdapter(bot_token="0:not-a-token", allowlist=_allowlist("channel-telegram", "7"))
    else:
        adapter = LarkChannelAdapter(runtime_root=root, allowlist=_allowlist("channel-lark", OPEN_ID))
    adapter._bus = chat.bus
    sequence = 0

    async def send(text: str):
        nonlocal sequence
        sequence += 1
        if channel == "telegram":
            update = _tg_update(7, text, message_id=sequence)
            handler = adapter._cmd_stop if text == "/stop" else (
                adapter._cmd_profile if text.startswith("/profile") else adapter._on_message)
            await handler(update, SimpleNamespace(args=text.split()[1:]))
        else:
            await adapter._handle_lark_event(_lark_event(OPEN_ID, text, message_id=f"om_{sequence}"))
        envelope = chat.bus.get_queue("manager").get_nowait()
        assert envelope.message_type is MessageType.USER_MESSAGE
        assert envelope.payload["text"] == text
        return envelope

    try:
        yield send
    finally:
        await adapter._reactions.stop()
        if channel == "lark":
            await adapter.stop()


def _replies(chat: Any, channel: str) -> list[dict[str, Any]]:
    queue = chat.bus.get_queue(channel_address(channel).agent_id)
    replies = []
    while not queue.empty():
        envelope = queue.get_nowait()
        if envelope.message_type is MessageType.AGENT_REPLY:
            replies.append(envelope.payload)
    return replies


@pytest.mark.parametrize("client", [False, True], ids=["dashboard", "loopercli"])
def test_v1_registry_lists_api_as_in_process_manager_without_executable(tmp_path, monkeypatch, client):
    async def run(pool):
        async with FakeModelAPI([]) as model:
            await _profile(pool, model.base_url)
            async with _api(tmp_path, pool, monkeypatch) as call:
                status, answer = await call("ops.agent_profiles.list" if client else "registry.view", client=client)
                assert status == 200, answer
                view = answer["result"]
                kind = next(row for row in view["runtime_types"] if row["type"] == "api")
                assert kind["in_process"] is True and kind["roles"] == ["manager"]
                runtime = next(row for row in view["runtimes"] if row["runtime_id"] == "rt-api-test")
                if not client:
                    assert runtime["in_process"] is True and runtime["path"] is None
                assert runtime["roles"] == ["manager"]
                assert not model.requests

    run_db(run, monkeypatch, timeout=30)


def test_v1_registry_explains_missing_package_without_registering_runtime(tmp_path, monkeypatch):
    original = importlib.metadata.version

    def version(name):
        if name == "openai":
            raise importlib.metadata.PackageNotFoundError(name)
        return original(name)

    monkeypatch.setattr(importlib.metadata, "version", version)

    async def run(pool):
        async with _api(tmp_path, pool, monkeypatch) as call:
            status, answer = await call("registry.view")
            assert status == 200, answer
            view = answer["result"]
            kind = next(row for row in view["runtime_types"] if row["type"] == "api")
            assert kind["probe_error"] == (
                "the openai Python package is not installed with this Looper; reinstall Looper")
            assert not any(row["type"] == "api" for row in view["runtimes"])

    run_db(run, monkeypatch, timeout=30)


@pytest.mark.parametrize("channel", ["telegram", "lark"])
def test_channel_profile_command_lists_and_selects_api_manager(tmp_path, monkeypatch, channel):
    async def run(pool):
        async with _manager(pool, tmp_path, monkeypatch, []) as chat:
            async with _commands(chat, tmp_path, monkeypatch, channel) as send:
                await chat.manager._handle(await send("/profile"))
                view = await chat.manager.session_view()
                choice = next(row for row in view["choices"] if row["profile_id"] == "agent-api-test")
                assert f"{choice['number']}. {choice['name']}" in _replies(chat, channel)[0]["text"]
                await chat.manager._handle(await send(f"/profile {choice['number']}"))
                assert (await agent_sessions.get_session("manager"))["selected_profile_id"] == "agent-api-test"
                assert (await chat.manager.session_view())["selected"]["profile_id"] == "agent-api-test"
                assert not _replies(chat, channel)[0].get("error")
                assert not chat.model.requests

    run_db(run, monkeypatch, timeout=30)


@pytest.mark.parametrize("channel", ["telegram", "lark"])
def test_channel_stop_command_cancels_streaming_api_manager(tmp_path, monkeypatch, channel):
    monkeypatch.setattr(chat_settings, "_stop_enabled", True)

    async def run(pool):
        script = ResponseScript(events=text_events("Partial reply.", usage=USAGE), gate=asyncio.Event())
        async with _manager(pool, tmp_path, monkeypatch, [script]) as chat:
            async with _commands(chat, tmp_path, monkeypatch, channel) as send:
                message = await send("Answer briefly.")
                answering = asyncio.create_task(chat.manager._handle(message))
                try:
                    await asyncio.wait_for(script.gate_reached.wait(), 20)
                    chat.manager._running = True
                    assert chat.manager._receive(await send("/stop"))
                    await asyncio.wait_for(answering, 2)
                    await asyncio.wait_for(script.closed.wait(), 2)
                    replies = _replies(chat, channel)
                    assert any(reply.get("outcome") == "stopped" for reply in replies)
                    assert any(reply["text"] == "已停止当前回复。" for reply in replies)
                finally:
                    answering.cancel()
                    await asyncio.gather(answering, return_exceptions=True)

    run_db(run, monkeypatch, timeout=40)


def test_dashboard_turn_reports_api_manager_can_interrupt(tmp_path, monkeypatch):
    monkeypatch.setattr(chat_settings, "_stop_enabled", True)

    async def run(pool):
        script = ResponseScript(events=text_events("Streaming.", usage=USAGE), gate=asyncio.Event())
        async with _manager(pool, tmp_path, monkeypatch, [script]) as chat:
            message = _message()
            answering = asyncio.create_task(chat.manager._handle(message))
            try:
                await asyncio.wait_for(script.gate_reached.wait(), 20)
                async with _serving(tmp_path) as server:
                    server._manager = chat.manager
                    status, _, answer = await _send(server, "POST /api/v1/operator_chat/turn", _bearer(TOKEN),
                                                   json.dumps({"session_id": "api-chat"}))
                assert status == 200, answer
                assert answer["result"]["turn"]["state"] == "running"
                assert answer["result"]["turn"]["can_interrupt"] is True
            finally:
                await chat.manager.stop_turn(message.message_id, via="web")
                await asyncio.wait_for(answering, 2)

    run_db(run, monkeypatch, timeout=40)


def test_doctor_output_is_unchanged_when_api_session_profile_is_selected(tmp_path, monkeypatch):
    async def run(pool):
        async with FakeModelAPI([]) as model:
            await _profile(pool, model.base_url)
            await pool.execute("DELETE FROM agent_setting_layers WHERE scope = 'role' AND role = 'manager'")
            schema = await pool.fetchval("SELECT current_schema()")
            parts = urlsplit(DSN)
            dsn = urlunsplit(parts._replace(query=urlencode([
                *parse_qsl(parts.query), ("options", f"-csearch_path={schema}"),
            ])))
            env = _doctor_env(tmp_path, claude="logged_in", database_url=dsn)
            before = await asyncio.to_thread(_run_looper, "doctor", cwd=tmp_path, env=env)
            assert "ok" in before[1] and "PostgreSQL" in before[1]
            assert "cannot connect" not in before[1], before
            await agent_sessions.select_profile("manager", "agent-api-test")
            after = await asyncio.to_thread(_run_looper, "doctor", cwd=tmp_path, env=env)
            assert after == before
            assert not model.requests

    run_db(run, monkeypatch, timeout=45)
