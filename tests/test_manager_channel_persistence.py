"""The Manager records Telegram/Lark turns in chat_messages (tagged by channel) so the
console shows every channel's conversation; web turns keep their own path."""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from fake_claude import fake_claude
from looper.agents.bus import MessageBus
from looper.agents.message import MANAGER_ADDRESS, MessageEnvelope, MessageType, channel_address
from looper.agents.tiers import manager as manager_mod
from looper.agents.tiers.manager import ManagerAgent, ManagerConfig

WEB = channel_address("web")
TELEGRAM = channel_address("telegram", "channel-telegram")


def _reply_claude(tmp: Path) -> str:
    """A fake claude that answers every turn with a plain text reply."""
    return fake_claude(tmp / "fake-claude", (
        "import json\n"
        "print(json.dumps({'type': 'system', 'subtype': 'init', 'model': 'm', 'session_id': 's-1', 'tools': []}))\n"
        "print(json.dumps({'type': 'assistant', 'message': {'content': [{'type': 'text', 'text': 'ok'}]}}))\n"
        "print(json.dumps({'type': 'result', 'subtype': 'success', 'is_error': False, 'result': 'ok'}))\n"
    ))


def _user(source: Any, text: str, *, chat_id: str | None = None, session_id: str | None = None,
          corr: str = "c1") -> MessageEnvelope:
    context: dict[str, Any] = {}
    if chat_id is not None:
        context["chat_id"] = chat_id
    if session_id is not None:
        context["session_id"] = session_id
    return MessageEnvelope.create(
        source=source, target=MANAGER_ADDRESS, message_type=MessageType.USER_MESSAGE,
        payload={"text": text, "channel_context": context}, correlation_id=corr,
    )


@pytest.mark.usefixtures("agent_db")
class TestChannelPersistence:
    def _run(self, source: Any, env_kwargs: dict[str, Any]) -> list[dict[str, Any]]:
        saved: list[dict[str, Any]] = []

        async def _save(**kwargs: Any) -> None:
            saved.append(kwargs)

        async def _go() -> None:
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                mgr = ManagerAgent(config=ManagerConfig(claude_bin=_reply_claude(root), work_dir=root),
                                   runtime_root=root)
                mgr._bus = MessageBus(runtime_root=root)
                with pytest.MonkeyPatch.context() as mp:
                    mp.setattr(manager_mod.db_chat, "save_message", AsyncMock(side_effect=_save))
                    await mgr._handle(_user(source, "hello", **env_kwargs))
                await mgr.stop()
                await mgr._bus.shutdown()

        asyncio.run(_go())
        return saved

    def test_a_telegram_turn_and_reply_are_recorded_with_the_channel(self) -> None:
        saved = self._run(TELEGRAM, {"chat_id": "12345"})
        # One user turn and one assistant reply, both under telegram:<chat_id>, tagged telegram.
        assert [s["role"] for s in saved] == ["user", "assistant"]
        assert all(s["session_id"] == "telegram:12345" and s["channel"] == "telegram" for s in saved)
        assert saved[0]["content"] == "hello"

    def test_a_web_turn_is_not_recorded_here(self) -> None:
        # Web keeps its own persistence path; the Manager must not double-write it.
        saved = self._run(WEB, {"session_id": "session-abc"})
        assert saved == []


LARK = channel_address("lark", "channel-lark")


@pytest.mark.usefixtures("agent_db")
class TestStoredChats:
    """Every chat is stored but a Telegram group (its id is negative): nothing from one reaches chat_messages, neither
    the message, nor the Manager's reply to it, nor a reply Claude sends on its own afterwards. A Lark group is, under
    ``lark:<chat_id>``: only the messages that @-mention the bot come in from one."""

    @staticmethod
    def _stored(source: Any, context: dict[str, Any]) -> list[dict[str, Any]]:
        saved: list[dict[str, Any]] = []

        async def _save(**kwargs: Any) -> None:
            saved.append(kwargs)

        async def _go() -> None:
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                mgr = ManagerAgent(config=ManagerConfig(claude_bin=_reply_claude(root), work_dir=root),
                                   runtime_root=root)
                mgr._bus = MessageBus(runtime_root=root)
                envelope = MessageEnvelope.create(
                    source=source, target=MANAGER_ADDRESS, message_type=MessageType.USER_MESSAGE,
                    payload={"text": "hello", "channel_context": context}, correlation_id="c1")
                with pytest.MonkeyPatch.context() as mp:
                    mp.setattr(manager_mod.db_chat, "save_message", AsyncMock(side_effect=_save))
                    await mgr._handle(envelope)
                    await mgr._send_own_reply(envelope, "done on my own")  # a reply Claude sends on its own
                await mgr.stop()
                await mgr._bus.shutdown()

        asyncio.run(_go())
        return saved

    def test_a_telegram_group_message_and_every_reply_to_it_are_not_stored(self) -> None:
        assert self._stored(TELEGRAM, {"chat_id": "-1001234567890"}) == []

    @pytest.mark.parametrize(("source", "context", "session"), [
        (TELEGRAM, {"chat_id": "12345"}, "telegram:12345"),
        (LARK, {"chat_id": "oc_dm", "chat_type": "p2p", "sender_id": "ou_1"}, "lark:oc_dm"),
        (LARK, {"chat_id": "oc_team", "chat_type": "group", "sender_id": "ou_1", "sender_name": "Ada"},
         "lark:oc_team"),
    ])
    def test_a_message_and_every_reply_to_it_are_stored(self, source: Any, context: dict[str, Any],
                                                        session: str) -> None:
        saved = self._stored(source, context)
        assert [(s["session_id"], s["role"], s["content"]) for s in saved] == [
            (session, "user", "hello"), (session, "assistant", "ok"), (session, "assistant", "done on my own")]
