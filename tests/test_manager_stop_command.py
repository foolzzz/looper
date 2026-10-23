"""``/stop`` in a chat: claimed from the bus, outside the Manager's queue, and stopping the chat's running turn."""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from typing import Any

import pytest

from looper.agents import chat_settings
from looper.agents.message import MANAGER_ADDRESS, MessageEnvelope, MessageType, channel_address
from looper.agents.tiers.manager import is_stop_command
from tests.db_support import until
from tests.test_manager_new_session import _drain
from tests.test_manager_stop import _manager, _Session

@pytest.fixture(autouse=True)
def _stop_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stopping a reply is off by default (``chat_settings``); these tests are about it working."""
    monkeypatch.setattr(chat_settings, "_stop_enabled", True)


TELEGRAM = channel_address("telegram")


def _tg(text: str, chat: int = 99) -> MessageEnvelope:
    return MessageEnvelope.create(
        source=TELEGRAM, target=MANAGER_ADDRESS, message_type=MessageType.USER_MESSAGE,
        payload={"text": text, "channel_context": {"chat_id": chat, "user_id": 7}}, correlation_id=f"c-{chat}-{text}",
    )


def _texts(queue: asyncio.Queue[MessageEnvelope]) -> list[str]:
    return [e.payload["text"] for e in _drain(queue) if e.message_type == MessageType.AGENT_REPLY]


def _setup(tmp: str) -> tuple[Any, Any, Any, Any, asyncio.Queue[MessageEnvelope]]:
    session = _Session()
    manager, bus, _ = _manager(Path(tmp), session)
    manager._running = True
    bus.subscribe_topic(MessageType.USER_MESSAGE.value, manager._receive, consumes=True)
    return session, manager, bus, bus.get_queue(TELEGRAM.agent_id), bus.get_queue("manager")


def test_is_stop_command() -> None:
    assert is_stop_command(" /STOP ") and is_stop_command("/stop")
    assert not is_stop_command("/stop now") and not is_stop_command("/stopx") and not is_stop_command("stop")


def test_stop_from_the_same_chat_stops_the_turn(agent_db: Any) -> None:
    async def run() -> None:
        with tempfile.TemporaryDirectory() as tmp:
            session, manager, bus, chat, inbox = _setup(tmp)
            handling = asyncio.create_task(manager._handle(_tg("写一篇很长的文章")))
            await until(session.running.is_set, timeout=10)
            stop = _tg("/stop")
            await bus.publish(stop)
            await handling
            assert inbox.empty()
            assert stop.message_id not in [e.message_id for e in bus.journal.undelivered()]
            texts = _texts(chat)
            assert "已停止当前回复。" in texts and any(t.endswith("（已停止）") for t in texts)
            (finish,) = agent_db.finish_record.await_args_list
            assert finish.kwargs["status"] == "interrupted"
            (_, notes), = [c.args for c in agent_db.add_metadata.await_args_list if "user_stop" in c.args[1]]
            assert notes["user_stop"]["via"] == "telegram"
            await bus.shutdown()

    asyncio.run(run())


def test_stop_from_another_chat_or_idle(agent_db: Any) -> None:
    async def run() -> None:
        with tempfile.TemporaryDirectory() as tmp:
            session, manager, bus, chat, inbox = _setup(tmp)
            await bus.publish(_tg("/stop"))
            await until(lambda: not chat.empty(), timeout=10)
            assert _texts(chat) == ["现在没有正在进行的回复。"]
            handling = asyncio.create_task(manager._handle(_tg("写一篇很长的文章")))
            await until(session.running.is_set, timeout=10)
            _drain(chat)
            await bus.publish(_tg("/stop", chat=5))
            await until(lambda: not chat.empty(), timeout=10)
            assert "现在没有正在进行的回复。" in _texts(chat)
            assert manager._running_token is not None and manager._running_token.stop is None
            await manager.stop_turn(manager._running_token.message_id, via="telegram")
            await handling
            await bus.shutdown()

    asyncio.run(run())


def test_stop_with_arguments_is_an_ordinary_message(agent_db: Any) -> None:
    async def run() -> None:
        with tempfile.TemporaryDirectory() as tmp:
            session, manager, bus, chat, inbox = _setup(tmp)
            await bus.publish(_tg("/stop now"))
            assert inbox.qsize() == 1
            await bus.shutdown()

    asyncio.run(run())


def test_stop_is_answered_by_looper_when_the_setting_is_off(agent_db: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(chat_settings, "_stop_enabled", False)

    async def run() -> None:
        with tempfile.TemporaryDirectory() as tmp:
            session, manager, bus, chat, inbox = _setup(tmp)
            handling = asyncio.create_task(manager._handle(_tg("写一篇很长的文章")))
            await until(session.running.is_set, timeout=10)
            _drain(chat)
            await bus.publish(_tg("/stop"))
            await until(lambda: not chat.empty(), timeout=10)
            assert _texts(chat) == ["停止功能暂未开放"]
            assert inbox.empty()  # not queued for the model
            assert manager._running_token is not None and manager._running_token.stop is None
            assert not session._stopped.is_set()  # nothing was interrupted
            await manager.stop_turn(manager._running_token.message_id, via="telegram")  # to end the stand-in's turn
            await handling
            await bus.shutdown()

    asyncio.run(run())


def test_stop_takes_the_whole_chat_with_its_queued_messages(agent_db: Any) -> None:
    async def run() -> None:
        with tempfile.TemporaryDirectory() as tmp:
            session, manager, bus, chat, inbox = _setup(tmp)
            first = _tg("写一篇很长的文章")
            handling = asyncio.create_task(manager._handle(first))
            await until(session.running.is_set, timeout=10)
            queued = [_tg("第二条"), _tg("第三条")]
            other = _tg("别的聊天", chat=5)
            for envelope in (*queued, other):
                manager._register_turn(envelope)
            _drain(chat)
            await bus.publish(_tg("/stop"))
            await until(lambda: "已停止当前回复，另取消了 2 条排队的消息。" in _texts(chat), timeout=10)
            await handling
            assert manager._turns.find(other.message_id) is not None
            assert manager._turns.find(other.message_id).stop is None
            for envelope in queued:
                assert manager._turns.find(envelope.message_id).stop is not None
            await bus.shutdown()

    asyncio.run(run())


@pytest.mark.parametrize("channel", ["telegram", "lark", "lark_bot"])
def test_stop_only_takes_its_bots_running_and_queued_turns(agent_db: Any, channel: str) -> None:
    async def run() -> None:
        with tempfile.TemporaryDirectory() as tmp:
            session, manager, bus, _, inbox = _setup(tmp)
            bot_a = channel_address(channel, f"channel-{channel}-1")
            bot_b = channel_address(channel, f"channel-{channel}-2")

            def message(text: str, source: Any) -> MessageEnvelope:
                return MessageEnvelope.create(
                    source=source, target=MANAGER_ADDRESS, message_type=MessageType.USER_MESSAGE,
                    payload={"text": text, "channel_context": {"chat_id": 99, "user_id": 7}},
                )

            first = message("写一篇很长的文章", bot_a)
            handling = asyncio.create_task(manager._handle(first))
            try:
                await until(session.running.is_set, timeout=10)
                queued_a = manager._register_turn(message("继续写", bot_a))
                queued_b = manager._register_turn(message("你好", bot_b))
                chat_b = bus.get_queue(bot_b.agent_id)
                await bus.publish(message("/stop", bot_b))
                await until(lambda: not chat_b.empty(), timeout=10)
                assert _texts(chat_b) == ["已停止当前回复。"]
                assert queued_b.stop is not None
                assert queued_a.stop is None
                assert manager._running_token is not None and manager._running_token.stop is None
                assert not session._stopped.is_set()

                chat_a = bus.get_queue(bot_a.agent_id)
                _drain(chat_a)
                await bus.publish(message("/stop", bot_a))
                await until(lambda: any("另取消了 1 条排队的消息" in text for text in _texts(chat_a)), timeout=10)
                await handling
                assert queued_a.stop is not None
                assert inbox.empty()
            finally:
                if not handling.done():
                    await manager.stop_turn(first.message_id, via=channel)
                    await handling
                await bus.shutdown()

    asyncio.run(run())


def test_stop_says_a_runtime_without_an_interrupt_cannot_stop_the_reply(agent_db: Any) -> None:
    async def run() -> None:
        with tempfile.TemporaryDirectory() as tmp:
            session, manager, bus, chat, inbox = _setup(tmp)
            session.can_interrupt = False
            handling = asyncio.create_task(manager._handle(_tg("写一篇很长的文章")))
            await until(session.running.is_set, timeout=10)
            _drain(chat)
            await bus.publish(_tg("/stop"))
            await until(lambda: not chat.empty(), timeout=10)
            assert _texts(chat) == ["当前运行时不能中断回复，回复会照常完成。"]
            assert inbox.empty()  # not queued for the model
            assert manager._running_token is not None and manager._running_token.stop is None
            assert not session._stopped.is_set()  # nothing was interrupted
            session.can_interrupt = True  # to end the stand-in's turn
            await manager.stop_turn(manager._running_token.message_id, via="telegram")
            await handling
            await bus.shutdown()

    asyncio.run(run())
