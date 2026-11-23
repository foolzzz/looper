"""``/status`` while the status card is on: code answers the operator at once, and stores nothing.

Off, Telegram still rewrites it into a progress question and the Manager still gives it to the model. The
launched-process comparison is two separate first turns, so a resume cannot make the second one differ.
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from looper.agents import chat_commands, chat_settings, status_card
from looper.agents.bus import MessageBus
from looper.agents.channels.chat_history import CHAT_RECORD
from looper.agents.message import MANAGER_ADDRESS, MessageEnvelope, MessageType, channel_address
from looper.agents.tiers.manager_session import MAIN_SESSION
from tests.db_support import run_db, until
from tests.test_manager_new_session import _drain, _web
from tests.test_manager_stop import _Session, _manager

TELEGRAM = channel_address("telegram")
READY = "please reply with the word ready"
_LOOP = re.compile(r"http://127\.0\.0\.1:\d+\S*")
# The builtin MCP config is named by a hash of its content, which includes this endpoint's port and token.
_MCP_CONFIG = re.compile(r"/\.local/mcp/[0-9a-f]{64}\.json")


@pytest.fixture(autouse=True)
def _card_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """The card and stopping a reply stay off unless a test turns the card on."""
    monkeypatch.setattr(status_card, "_enabled", False)
    monkeypatch.setattr(chat_settings, "_stop_enabled", False)


def _allowlist(channel_id: str, *ids: str) -> Any:
    from looper.agents.channels.allowlist import SenderAllowlist
    return SenderAllowlist(channel_id, frozenset(ids))


def _manager_messages(bus: MessageBus) -> list[MessageEnvelope]:
    queue = bus.get_queue("manager")
    messages = []
    while not queue.empty():
        messages.append(queue.get_nowait())
    return messages


def _tg_update(user_id: int, text: str = "/status", chat_id: int = 99, *, message_id: int = 41,
               replies_to: int | None = None) -> Any:
    from unittest.mock import AsyncMock
    replied = None if replies_to is None else SimpleNamespace(message_id=replies_to)
    return SimpleNamespace(
        update_id=1,
        effective_user=SimpleNamespace(id=user_id, username="someone"),
        effective_chat=SimpleNamespace(id=chat_id),
        message=SimpleNamespace(text=text, reply_text=AsyncMock(), message_id=message_id, reply_to_message=replied),
    )


def _telegram(tmp_path: Path, update: Any, *ids: str) -> list[MessageEnvelope]:
    from looper.agents.channels.telegram import TelegramChannelAdapter

    async def run() -> list[MessageEnvelope]:
        bus = MessageBus(runtime_root=tmp_path)
        adapter = TelegramChannelAdapter(bot_token="0:not-a-token", allowlist=_allowlist("channel-telegram", *ids))
        adapter._bus = bus
        await adapter._cmd_status(update, None)
        messages = _manager_messages(bus)
        await bus.shutdown()
        return messages

    return asyncio.run(run())


def _envelope(source: Any, text: str, context: dict[str, Any], correlation: str) -> MessageEnvelope:
    return MessageEnvelope.create(
        source=source, target=MANAGER_ADDRESS, message_type=MessageType.USER_MESSAGE,
        payload={"text": text, "channel_context": context}, correlation_id=correlation,
    )


def _live(tmp: Path) -> tuple[Any, Any]:
    session = _Session(hold=False)
    manager, bus, _ = _manager(tmp, session)
    manager._running = True
    bus.subscribe_topic(MessageType.USER_MESSAGE.value, manager._receive, consumes=True)
    return manager, bus


def _replies(queue: asyncio.Queue[MessageEnvelope]) -> list[MessageEnvelope]:
    return [envelope for envelope in _drain(queue) if envelope.message_type == MessageType.AGENT_REPLY]


@pytest.mark.parametrize("text", ["/status", " /STATUS ", "/status@bot", "/Status@Looper_bot"])
def test_is_status_matches_the_whole_command(text: str) -> None:
    assert chat_commands.is_status(text)


@pytest.mark.parametrize("text", ["/status now", "/statusx", "status", "/help", "", "/status @bot"])
def test_is_status_leaves_anything_else_alone(text: str) -> None:
    assert not chat_commands.is_status(text)


def test_telegram_status_off_still_asks_about_progress(tmp_path: Path) -> None:
    update = _tg_update(7, "/status", replies_to=17)
    (message,) = _telegram(tmp_path, update, "7")
    assert message.payload["text"] == "当前任务进度怎么样？"
    assert message.payload["channel_context"] == {"chat_id": 99}


def test_telegram_status_on_forwards_the_typed_command(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(status_card, "_enabled", True)
    (message,) = _telegram(tmp_path, _tg_update(7, "/status@looper"), "7")
    assert message.payload["text"] == "/status@looper"
    assert message.payload["channel_context"]["user_id"] == 7
    assert message.payload["channel_context"]["chat_id"] == 99


def test_telegram_status_unreadable_is_forwarded_as_before(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    def broken() -> bool:
        raise RuntimeError("switch down")

    monkeypatch.setattr(status_card, "enabled", broken)
    with caplog.at_level("WARNING"):
        (message,) = _telegram(tmp_path, _tg_update(7), "7")
    assert message.payload["text"] == "当前任务进度怎么样？"
    assert message.payload["channel_context"] == {"chat_id": 99}
    assert "cannot read whether /status is on" in caplog.text


def test_only_the_operator_is_answered_and_the_reply_is_not_stored(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Operator, group, another person, and an API client. A refusal builds no snapshot."""
    built: list[str] = []

    async def snapshot(**kwargs: Any) -> dict[str, Any]:
        built.append(kwargs["source"])
        return {"read_at": "x", "text": "截至 11-22 15:05\n管家：空闲；排队 0 条", "sections": []}

    async def operator_id(channel_id: str) -> str:
        assert channel_id == "channel-telegram"
        return "7"

    monkeypatch.setattr(status_card, "_enabled", True)
    monkeypatch.setattr(status_card, "snapshot", snapshot)
    monkeypatch.setattr("looper.agents.operator_identity.operator_sender_id", operator_id)

    async def run() -> None:
        manager, bus = _live(tmp_path)
        web = channel_address("web")
        cases = (
            ("operator", _envelope(TELEGRAM, "/status", {"chat_id": 99, "user_id": 7}, "op"),
             TELEGRAM, "截至 11-22 15:05\n管家：空闲；排队 0 条"),
            ("group", _envelope(TELEGRAM, "/status", {"chat_id": -100, "user_id": 7}, "group"),
             TELEGRAM, status_card.FORBIDDEN),
            ("other", _envelope(TELEGRAM, "/status", {"chat_id": 99, "user_id": 8}, "other"),
             TELEGRAM, status_card.FORBIDDEN),
            ("client", _envelope(web, "/status", {"session_id": "s", "client": {"id": "cli-1"}}, "client"),
             web, status_card.FORBIDDEN),
        )
        try:
            for name, envelope, source, expected in cases:
                before = len(built)
                await bus.publish(envelope)
                await until(lambda envelope=envelope: not manager._status_tasks, timeout=2)
                reply = _replies(bus.get_queue(source.agent_id))[-1]
                assert reply.payload["text"] == expected, name
                assert CHAT_RECORD not in reply.metadata, name
                assert reply.reply_to == envelope.message_id
                assert (len(built) == before + 1) is (name == "operator")
                assert not manager._turns.live()
                assert bus.get_queue(manager.agent_id).empty()
            assert built == ["status"]
        finally:
            await manager.stop()
            await bus.shutdown()

    asyncio.run(run())


def test_status_answers_during_a_turn_without_waiting_for_it(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A turn that has already run for five minutes does not delay ``/status``, and the model is not asked."""
    from datetime import datetime, timedelta, timezone

    from looper.agents.ops import Turn, TurnOrigin
    from looper.agents.tiers.manager import ManagerAgent
    from looper.agents.tiers.turn_token import TurnToken

    prepared: list[str] = []

    async def prepare(self: Any, *args: Any, **kwargs: Any) -> Any:
        prepared.append("turn")
        raise AssertionError("/status started a model turn")

    async def operator_id(channel_id: str) -> str:
        return "7"

    monkeypatch.setattr(status_card, "_enabled", True)
    monkeypatch.setattr(ManagerAgent, "_prepare_turn", prepare)
    monkeypatch.setattr("looper.agents.operator_identity.operator_sender_id", operator_id)

    async def run() -> None:
        manager, bus = _live(tmp_path)
        main = manager._sessions[MAIN_SESSION]
        token = TurnToken(message_id="running-1", client_message_id=None, channel="web", chat="console",
                          received_at=datetime.now(timezone.utc) - timedelta(minutes=5),
                          ops_turn=Turn(channel="web", chat="console", origin=TurnOrigin.USER))
        main.running_token = token
        await main.turn_lock.acquire()
        chat = bus.get_queue(TELEGRAM.agent_id)
        try:
            await bus.publish(_envelope(TELEGRAM, "/status", {"chat_id": 99, "user_id": 7}, "now"))
            await until(lambda: not chat.empty(), timeout=1)
            reply = _replies(chat)[-1]
            assert reply.payload["text"].startswith("截至 ")
            assert "在回答你（控制台" in reply.payload["text"]
            assert CHAT_RECORD not in reply.metadata
            assert not manager._turns.live()
            assert prepared == []
            await asyncio.gather(*manager._status_tasks)
        finally:
            if main.turn_lock.locked():
                main.turn_lock.release()
            await manager.stop()
            await bus.shutdown()

    asyncio.run(run())


def test_status_off_is_an_ordinary_model_turn(tmp_path: Path, agent_db: Any) -> None:
    async def run() -> None:
        session = _Session(hold=False)
        manager, bus, _ = _manager(tmp_path, session)
        manager._running = True
        bus.subscribe_topic(MessageType.USER_MESSAGE.value, manager._receive, consumes=True)
        envelope = _web("/status")
        await bus.publish(envelope)
        assert manager._turns.live() and bus.get_queue(manager.agent_id).qsize() == 1
        assert not manager._status_tasks
        await manager._handle(envelope)
        assert session.turns and all("/status" in turn for turn in session.turns)
        assert all("当前任务进度怎么样" not in turn for turn in session.turns)
        await bus.shutdown()

    asyncio.run(run())


def test_status_now_is_not_the_command(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(status_card, "_enabled", True)

    async def run() -> None:
        manager, bus = _live(tmp_path)
        await bus.publish(_web("/status now"))
        assert manager._turns.live()
        assert not manager._status_tasks
        assert bus.get_queue(manager.agent_id).qsize() == 1
        await bus.shutdown()

    asyncio.run(run())


@pytest.mark.parametrize("on", [True, False])
def test_startup_answers_a_queued_status_only_while_the_card_is_on(
        on: bool, tmp_path: Path, agent_db: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    if on:
        monkeypatch.setattr(status_card, "_enabled", True)

    async def hang() -> None:
        await asyncio.Event().wait()

    async def run() -> None:
        session = _Session(hold=False)
        manager, bus, _ = _manager(tmp_path, session)
        manager._message_loop = hang
        envelope = _web("/status")
        await bus.publish(envelope)
        await bus.start()
        replies: list[MessageEnvelope] = []
        bus.subscribe_topic(MessageType.AGENT_REPLY.value, replies.append)
        await manager.start(bus)
        try:
            inbox = bus.get_queue(manager.agent_id)
            if on:
                await until(lambda: any(item.reply_to == envelope.message_id for item in replies), timeout=5)
                (reply,) = [item for item in replies if item.reply_to == envelope.message_id]
                assert reply.payload["text"].startswith("截至 ")
                assert not manager._turns.live()
                assert envelope.message_id not in [item.message_id for item in _drain(inbox)]
                assert session.turns == []
            else:
                assert manager._turns.live()
                assert [item.payload["text"] for item in _drain(inbox)] == ["/status"]
                assert replies == []
                assert session.turns == []
        finally:
            await manager.stop()
            await bus.shutdown()

    asyncio.run(run())


def test_stopping_the_manager_does_not_cancel_an_answer(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    gate = asyncio.Event()

    async def paused(envelope: Any, **kwargs: Any) -> str:
        await gate.wait()
        return "截至 11-22 15:05"

    monkeypatch.setattr(status_card, "_enabled", True)
    monkeypatch.setattr(status_card, "command_reply", paused)

    async def run() -> None:
        manager, bus = _live(tmp_path)
        try:
            await bus.publish(_web("/status"))
            await until(lambda: bool(manager._status_tasks), timeout=2)
            await manager.stop()
            assert manager._status_tasks and all(not task.done() for task in manager._status_tasks)
        finally:
            gate.set()
            await asyncio.gather(*list(manager._status_tasks), return_exceptions=True)
            await bus.shutdown()

    asyncio.run(run())


def _turn_text(root: Path) -> str:
    raw = json.loads((root / "turns.jsonl").read_text().splitlines()[0])
    return raw if isinstance(raw, str) else json.dumps(raw, ensure_ascii=False, sort_keys=True)


def _scrub(value: Any, prompt_file: str, root: str) -> Any:
    """The prompt file, this runtime root, the recorder's loopback port and the content-addressed MCP config
    filename are not part of the comparison. The config file is named by a hash of its content, and that content
    carries this endpoint's port and a random token."""
    if isinstance(value, str):
        text = _LOOP.sub("http://127.0.0.1:PORT/r/ROUTE", value.replace(prompt_file, "PROMPT").replace(root, "ROOT"))
        return _MCP_CONFIG.sub("/.local/mcp/CONFIG.json", text)
    if isinstance(value, tuple):
        return tuple(_scrub(item, prompt_file, root) for item in value)
    if isinstance(value, list):
        return [_scrub(item, prompt_file, root) for item in value]
    return value


def _comparable(signature: Any, prompt_file: str, root: str) -> Any:
    """Drop the two hashes that name this process's ops endpoint.

    Index 2 is the environment hash: each endpoint's loopback port is in the environment. Index 3 is the library
    hash: the builtin MCP server item hashes a config that contains that port and a random token. The command's
    ``--mcp-config`` path is that file's content hash; ``_scrub`` replaces the filename and every other argument
    is compared as itself.
    """
    scrubbed = _scrub(signature, prompt_file, root)
    return scrubbed[:2] + scrubbed[4:]


def test_off_the_launched_manager_matches_an_ordinary_message(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Two first turns, switch off: the same prompt, tools and process signature, and no status-card row."""
    from looper.agents.ops.service import ManagerOps
    from looper.agents.tiers.manager import ManagerAgent

    async def test(pool: Any) -> None:
        from looper.agents.bus import MessageBus
        from looper.agents.message import MessageType, channel_address, manager_address
        from looper.agents.tiers.manager import ManagerAgent, ManagerConfig
        from tests.manager_mcp_stand_in import manager_endpoint, mcp_manager_claude

        async def attach(launch: Any, **kwargs: Any) -> Any:
            return launch

        seen: list[dict[str, Any]] = []
        listed_last: list[Any] = [None]
        prepare, listing = ManagerAgent._prepare_turn, ManagerOps.current_listing

        async def keep(self: Any, *args: Any, **kwargs: Any) -> Any:
            result = await prepare(self, *args, **kwargs)
            if result.signature is not None:
                seen.append({"prompt": result.system_prompt.text, "signature": result.signature,
                             "prompt_file": str(result.prompt_file), "tools": listed_last[0]})
            return result

        async def listed(self: Any, *args: Any, **kwargs: Any) -> Any:
            tools = await listing(self, *args, **kwargs)
            listed_last[0] = json.loads(json.dumps(tools))
            return tools

        monkeypatch.setattr("looper.agents.llmrec.control.attach", attach)
        monkeypatch.setattr(ManagerAgent, "_prepare_turn", keep)
        monkeypatch.setattr(ManagerOps, "current_listing", listed)
        web = channel_address("web")
        turns: list[str] = []
        for text, name in ((READY, "ready"), ("/status", "status")):
            root = tmp_path / name
            root.mkdir()
            bus = MessageBus(runtime_root=root)
            manager = ManagerAgent(config=ManagerConfig(claude_bin=mcp_manager_claude(root, [[]]), work_dir=root),
                                   runtime_root=root)
            manager._bus = bus
            envelope = MessageEnvelope.create(
                source=web, target=manager_address(manager.agent_id), message_type=MessageType.USER_MESSAGE,
                payload={"text": text, "channel_context": {"session_id": "chat-1"}}, correlation_id=name,
                metadata={"origin_channel_id": "channel-web"})
            async with manager_endpoint(manager, bus, SimpleNamespace(has_planner=lambda task_id: False), root):
                await manager._handle(envelope)
                await manager.stop()
            await bus.shutdown()
            turns.append(_turn_text(root))
            await pool.execute("DELETE FROM chat_messages")
            await pool.execute("DELETE FROM chat_threads")
            await pool.execute("DELETE FROM manager_sessions")
            await pool.execute("DELETE FROM agent_sessions")

        ready, status = seen
        assert turns[1].count("/status") >= 1 and "当前任务进度怎么样" not in turns[1]
        assert READY in turns[0]
        assert turns[0].replace(READY, "MSG") == turns[1].replace("/status", "MSG")
        assert ready["prompt"].replace(READY, "MSG") == status["prompt"].replace("/status", "MSG")
        assert ready["tools"] == status["tools"] and ready["tools"]
        assert _comparable(ready["signature"], ready["prompt_file"], str(tmp_path / "ready")) == \
            _comparable(status["signature"], status["prompt_file"], str(tmp_path / "status"))
        assert await pool.fetchval("SELECT count(*) FROM system_settings WHERE key = $1",
                                   status_card.ENABLED_KEY) == 0

    run_db(test, monkeypatch, timeout=120)
