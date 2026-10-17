"""The Manager records the user's Telegram/Lark messages in chat_messages (tagged by channel) so the console
shows every channel's conversation, and names the record of each answer for the channel, which stores it once it
has tried to send it (``chat_history``); web turns keep their own path."""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from fake_claude import fake_claude
from looper.agents.bus import MessageBus
from looper.agents.channels.chat_history import CHAT_RECORD
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


def _records(replies: list[MessageEnvelope]) -> list[Any]:
    """What each answer the Manager sent asks its channel to store: ``(session_id, message_id)``, or None."""
    return [(r.metadata[CHAT_RECORD]["session_id"], r.metadata[CHAT_RECORD]["message_id"])
            if CHAT_RECORD in r.metadata else None for r in replies]


@pytest.mark.usefixtures("agent_db")
class TestChannelPersistence:
    def _run(self, source: Any, env_kwargs: dict[str, Any], *, text: str = "hello", fail: bool = False,
             ) -> tuple[list[dict[str, Any]], MessageEnvelope, list[MessageEnvelope]]:
        """What the Manager stored and the answers it sent its channel for the message ``text``; storing fails
        throughout when ``fail``."""
        saved: list[dict[str, Any]] = []

        async def _save(**kwargs: Any) -> None:
            if fail:
                raise OSError("the database is gone")
            saved.append(kwargs)

        envelope = _user(source, text, **env_kwargs)

        async def _go() -> list[MessageEnvelope]:
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                mgr = ManagerAgent(config=ManagerConfig(claude_bin=_reply_claude(root), work_dir=root),
                                   runtime_root=root)
                mgr._bus = MessageBus(runtime_root=root)
                with pytest.MonkeyPatch.context() as mp:
                    mp.setattr(manager_mod.db_chat, "save_message", AsyncMock(side_effect=_save))
                    await mgr._handle(envelope)
                await mgr.stop()
                replies = [e for e in await mgr._bus.drain_queue(source.agent_id)
                           if e.message_type is MessageType.AGENT_REPLY]
                await mgr._bus.shutdown()
                return replies

        replies = asyncio.run(_go())
        return saved, envelope, replies

    def test_a_telegram_turn_is_recorded_and_its_answer_left_to_the_channel(self) -> None:
        saved, envelope, replies = self._run(TELEGRAM, {"chat_id": "12345"})
        # The user's turn under telegram:<chat_id>, tagged telegram; the answer only once the channel sent it.
        assert [(s["session_id"], s["role"], s["content"], s["channel"]) for s in saved] == [
            ("telegram:12345", "user", "hello", "telegram")]
        assert [r.payload["text"] for r in replies] == ["ok"]
        assert _records(replies) == [("telegram:12345", f"{envelope.message_id}:reply")]

    def test_a_web_turn_is_not_recorded_here(self) -> None:
        # Web keeps its own persistence path; the Manager must not double-write it.
        saved, _, replies = self._run(WEB, {"session_id": "session-abc"})
        assert saved == [] and _records(replies) == [None]

    def test_the_users_new_is_stored_like_its_answer(self) -> None:
        saved, envelope, replies = self._run(TELEGRAM, {"chat_id": "12345"}, text="/new")
        assert [(s["role"], s["content"]) for s in saved] == [("user", "/new")]
        assert _records(replies) == [("telegram:12345", f"{envelope.message_id}:reply")]

    def test_a_storage_failure_does_not_keep_the_answer_from_the_user(self) -> None:
        saved, _, replies = self._run(TELEGRAM, {"chat_id": "12345"}, fail=True)
        assert saved == [] and [r.payload["text"] for r in replies] == ["ok"]


LARK = channel_address("lark", "channel-lark")


@pytest.mark.usefixtures("agent_db")
class TestStoredChats:
    """Every chat is stored but a Telegram group (its id is negative): nothing from one reaches chat_messages, neither
    the message, nor the Manager's reply to it, nor a reply Claude sends on its own afterwards. A Lark group is, under
    ``lark:<chat_id>``: only the messages that @-mention the bot come in from one."""

    @staticmethod
    def _stored(source: Any, context: dict[str, Any]) -> tuple[list[dict[str, Any]], list[Any]]:
        """What the Manager stored, and the records its answers name (``_records``): its answer to ``hello``, then a
        reply Claude sends on its own."""
        saved: list[dict[str, Any]] = []

        async def _save(**kwargs: Any) -> None:
            saved.append(kwargs)

        async def _go() -> list[MessageEnvelope]:
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
                replies = [e for e in await mgr._bus.drain_queue(source.agent_id)
                           if e.message_type is MessageType.AGENT_REPLY]
                await mgr._bus.shutdown()
                return replies

        replies = asyncio.run(_go())
        assert [r.payload["text"] for r in replies] == ["ok", "done on my own"]
        return saved, _records(replies)

    def test_a_telegram_group_message_and_every_reply_to_it_are_not_stored(self) -> None:
        assert self._stored(TELEGRAM, {"chat_id": "-1001234567890"}) == ([], [None, None])

    @pytest.mark.parametrize(("source", "context", "session"), [
        (TELEGRAM, {"chat_id": "12345"}, "telegram:12345"),
        (LARK, {"chat_id": "oc_dm", "chat_type": "p2p", "sender_id": "ou_1"}, "lark:oc_dm"),
        (LARK, {"chat_id": "oc_team", "chat_type": "group", "sender_id": "ou_1", "sender_name": "Ada"},
         "lark:oc_team"),
    ])
    def test_a_message_and_every_reply_to_it_are_stored(self, source: Any, context: dict[str, Any],
                                                        session: str) -> None:
        saved, records = self._stored(source, context)
        assert [(s["session_id"], s["role"], s["content"]) for s in saved] == [(session, "user", "hello")]
        # Both answers are left to the channel, under the same chat: the answer, and what Claude said on its own.
        assert [r[0] for r in records] == [session, session]
        assert records[0][1].endswith(":reply") and records[1][1].startswith("auto-")
