"""Each bot runs as its own adapter that talks to its Manager (RFC scenario-managers §9.1): the channel id, the
Manager an adapter publishes to, a turned-off Manager's reply, two bots of one type on one bus, and the thread id."""

from __future__ import annotations

import asyncio
import dataclasses
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from looper.agents.bus import MessageBus
from looper.agents.channels.allowlist import SenderAllowlist
from looper.agents.channels.lark import LarkChannelAdapter
from looper.agents.channels.lark_bot import make_adapter
from looper.agents.channels.telegram import TelegramChannelAdapter
from looper.agents.message import (MessageEnvelope, MessageType, channel_address, manager_address)
from looper.agents.tiers.manager import TURNED_OFF_REPLY, _session_key
from tests.test_agent_channels import _allowlist, _lark_event, _tg_update
from tests.test_channel_post import _Bot
from tests.test_lark_transport import BOT_ID, OPEN_ID, StubTransport

REVIEW = "manager-review"


def _queue(bus: MessageBus, agent_id: str) -> list[MessageEnvelope]:
    queue = bus.get_queue(agent_id)
    items = []
    while not queue.empty():
        items.append(queue.get_nowait())
    return items


def _telegram(bus: MessageBus, bot: _Bot | None = None, channel_id: str | None = None, **kwargs: Any,
              ) -> TelegramChannelAdapter:
    adapter = TelegramChannelAdapter(bot_token="0:not-a-token", channel_id=channel_id,
                                     allowlist=_allowlist(channel_id or "channel-telegram", "7"), **kwargs)
    adapter._bus = bus
    adapter._app = SimpleNamespace(bot=bot or _Bot())
    adapter._acknowledge = lambda update: reactions.append(update)  # type: ignore[method-assign]
    reactions.clear()
    return adapter


reactions: list[Any] = []


def _lark(tmp_path: Path, bus: MessageBus, transport: StubTransport, **kwargs: Any) -> LarkChannelAdapter:
    kwargs.setdefault("channel_type", "lark")
    adapter = LarkChannelAdapter(runtime_root=tmp_path, transport=transport, bot_open_id=BOT_ID,
                                 allowlist=SenderAllowlist("channel-lark", frozenset({OPEN_ID})), **kwargs)
    adapter._bus = bus
    adapter._running = True
    return adapter


def test_an_added_telegram_bot_publishes_to_its_manager_from_its_own_channel(tmp_path: Path) -> None:
    async def main() -> None:
        bus = MessageBus(runtime_root=tmp_path)
        adapter = _telegram(bus, channel_id="channel-telegram-2", manager_id=REVIEW)
        assert adapter.channel_id == "channel-telegram-2"
        await adapter._forward(_tg_update(7), "hello")
        await adapter._cmd_status(_tg_update(7), None)
        sent = _queue(bus, REVIEW)
        assert len(sent) == 2 and _queue(bus, "manager") == []
        for envelope in sent:
            assert envelope.target.agent_id == REVIEW
            assert envelope.source.agent_id == "channel-telegram-2" and envelope.source.channel == "telegram"
            assert envelope.metadata["origin_channel_id"] == "channel-telegram-2"

    asyncio.run(main())


def test_the_builtin_telegram_adapter_is_unchanged(tmp_path: Path) -> None:
    async def main() -> None:
        bus = MessageBus(runtime_root=tmp_path)
        adapter = _telegram(bus)
        await adapter._forward(_tg_update(7), "hello")
        [envelope] = _queue(bus, "manager")
        assert envelope.source.agent_id == "channel-telegram" and envelope.target.agent_id == "manager"

    asyncio.run(main())


def test_a_telegram_channel_id_is_validated_and_only_the_builtin_row_reads_the_environment(
        monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValueError):
        TelegramChannelAdapter(channel_id="channel-lark-2", allowlist=_allowlist("channel-lark-2", "7"))
    monkeypatch.setenv("LOOPER_TELEGRAM_BOT_TOKEN", "9:from-env")
    added = TelegramChannelAdapter(channel_id="channel-telegram-2", allowlist=_allowlist("channel-telegram-2", "7"))
    builtin = TelegramChannelAdapter(allowlist=_allowlist("channel-telegram", "7"))
    assert added._bot_token == "" and builtin._bot_token == "9:from-env"


def test_lark_adapters_name_their_channel_and_manager(tmp_path: Path) -> None:
    async def main() -> None:
        bus = MessageBus(runtime_root=tmp_path)
        adapter = _lark(tmp_path, bus, StubTransport(), channel_id="channel-lark-3", manager_id=REVIEW)
        assert adapter.channel_id == "channel-lark-3"
        await adapter._handle_lark_event(_lark_event(OPEN_ID))
        [envelope] = _queue(bus, REVIEW)
        assert envelope.source.agent_id == "channel-lark-3" and envelope.source.channel == "lark"
        assert envelope.metadata["origin_channel_id"] == "channel-lark-3"
        assert _lark(tmp_path, bus, StubTransport()).channel_id == "channel-lark"
        assert _lark(tmp_path, bus, StubTransport(), channel_type="lark_bot").channel_id == "channel-lark_bot"
        with pytest.raises(ValueError):
            _lark(tmp_path, bus, StubTransport(), channel_id="channel-telegram-2")
        with pytest.raises(ValueError):
            _lark(tmp_path, bus, StubTransport(), channel_id="channel-lark_bot-2")

    asyncio.run(main())


def test_make_adapter_passes_the_channel_id_and_manager_through(tmp_path: Path) -> None:
    config = {"app_id": "cli_x", "domain": "feishu"}
    adapter = make_adapter(config, "secret", runtime_root=tmp_path, allowlist=SenderAllowlist("c", frozenset({"o"})),
                           on_rejected=None, bot_open_id="", channel_id="channel-lark_bot-4", manager_id=REVIEW)
    assert adapter.channel_id == "channel-lark_bot-4" and adapter._manager_id == REVIEW
    default = make_adapter(config, "secret", runtime_root=tmp_path, allowlist=SenderAllowlist("c", frozenset({"o"})),
                           on_rejected=None, bot_open_id="")
    assert default.channel_id == "channel-lark_bot" and default._manager_id == "manager"


# --- A turned-off Manager ---


def test_a_telegram_message_to_a_turned_off_manager_is_answered_and_goes_nowhere(tmp_path: Path) -> None:
    async def main() -> None:
        bus = MessageBus(runtime_root=tmp_path)
        bot = _Bot()
        adapter = _telegram(bus, bot, "channel-telegram-2", manager_id=REVIEW, manager_running=lambda m: False)
        await adapter._forward(_tg_update(7, chat_id=55), "hello")
        await adapter._cmd_status(_tg_update(7, chat_id=55), None)
        assert [(s["chat_id"], s["text"]) for s in bot.sent] == [(55, TURNED_OFF_REPLY)] * 2
        assert _queue(bus, REVIEW) == [] and reactions == []

    asyncio.run(main())


def test_a_running_default_or_unreadable_manager_is_published_to(tmp_path: Path) -> None:
    def broken(manager_id: str) -> bool:
        raise RuntimeError("fleet gone")

    async def main() -> None:
        bus = MessageBus(runtime_root=tmp_path)
        for kwargs, target in [
            ({"manager_id": REVIEW, "manager_running": lambda m: True}, REVIEW),
            ({"manager_running": lambda m: False}, "manager"),
            ({"manager_id": REVIEW, "manager_running": broken}, REVIEW),
        ]:
            bot = _Bot()
            adapter = _telegram(bus, bot, **kwargs)
            await adapter._forward(_tg_update(7), "hello")
            assert len(_queue(bus, target)) == 1 and bot.sent == []

    asyncio.run(main())


def test_lark_tells_a_turned_off_managers_senders(tmp_path: Path) -> None:
    async def main() -> None:
        bus = MessageBus(runtime_root=tmp_path)
        transport = StubTransport()
        adapter = _lark(tmp_path, bus, transport, channel_id="channel-lark-3", manager_id=REVIEW,
                        manager_running=lambda m: False)
        await adapter._handle_lark_event(_lark_event(OPEN_ID))
        group = _lark_event(OPEN_ID, chat_id="oc_g", chat_type="group", message_id="om_g",
                            mentions=[{"id": BOT_ID, "key": "@_user_1", "name": "bot"}], content="@bot hi")
        await adapter._handle_lark_event(group)
        assert transport.calls == [("send", {"chat_id": "oc_1", "text": TURNED_OFF_REPLY}),
                                   ("reply", {"message_id": "om_g", "text": TURNED_OFF_REPLY})]
        assert _queue(bus, REVIEW) == []
        adapter._manager_running = lambda m: True
        await adapter._handle_lark_event(_lark_event(OPEN_ID))
        assert len(_queue(bus, REVIEW)) == 1
        default = _lark(tmp_path, bus, StubTransport(), manager_running=lambda m: False)
        await default._handle_lark_event(_lark_event(OPEN_ID))
        assert len(_queue(bus, "manager")) == 1

    asyncio.run(main())


# --- Two bots of one type ---


def test_a_reply_goes_back_through_the_bot_the_message_came_from(tmp_path: Path) -> None:
    async def main() -> None:
        bus = MessageBus(runtime_root=tmp_path)
        first_bot, second_bot = _Bot(), _Bot()
        first = _telegram(bus, first_bot)
        second = _telegram(bus, second_bot, "channel-telegram-2", manager_id=REVIEW)
        await second._forward(_tg_update(7, chat_id=55), "hello")
        [original] = _queue(bus, REVIEW)
        reply = MessageEnvelope.create(
            source=manager_address(REVIEW), target=original.source, message_type=MessageType.AGENT_REPLY,
            payload={"text": "answer"}, correlation_id=original.correlation_id, reply_to=original.message_id,
            metadata={"origin_channel_id": "channel-telegram-2"})
        await first.deliver(reply)
        assert first_bot.sent == []
        await second.deliver(reply)
        assert [s["chat_id"] for s in second_bot.sent] == [55]

    asyncio.run(main())


# --- Thread ids ---


def _from(source: Any, context: dict[str, Any]) -> MessageEnvelope:
    return MessageEnvelope.create(source=source, target=manager_address(REVIEW), message_type=MessageType.USER_MESSAGE,
                                  payload={"text": "hi", "channel_context": context}, correlation_id="c")


def test_the_thread_id_names_the_bot_only_for_an_added_one() -> None:
    assert _session_key(_from(channel_address("telegram", "channel-telegram-2"), {"chat_id": 42})) \
        == "channel-telegram-2:42"
    assert _session_key(_from(channel_address("telegram", "channel-telegram"), {"chat_id": 42})) == "telegram:42"
    assert _session_key(_from(channel_address("lark", "channel-lark-3"),
                              {"chat_id": "oc_x", "chat_type": "group"})) == "channel-lark-3:oc_x"
    assert _session_key(_from(channel_address("telegram", "channel-telegram-2"), {"chat_id": -42})) is None


# --- PostgreSQL ---

from tests.test_channel_configs_db import DSN, _run  # noqa: E402


@pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")
def test_a_scenario_managers_bot_thread_belongs_to_it(monkeypatch: pytest.MonkeyPatch) -> None:
    from looper.agents.tiers.manager import ManagerAgent, ManagerConfig, default_definition
    from looper.db import chat as db_chat

    async def test(pool: Any) -> None:
        await pool.execute("INSERT INTO managers (id, name, system_prompt, enabled) VALUES ($1, $1, 'x', true)", REVIEW)
        definition = dataclasses.replace(default_definition(), id=REVIEW, name=REVIEW, builtin=False)
        mgr = ManagerAgent(config=ManagerConfig(work_dir=Path.cwd()), runtime_root=Path.cwd(), definition=definition)
        envelope = _from(channel_address("lark", "channel-lark-3"), {"chat_id": "oc_dm", "chat_type": "p2p"})
        await mgr._persist_chat(envelope, "user", "hello", "m1")
        row = await pool.fetchrow("SELECT session_id, channel FROM chat_messages")
        assert (row["session_id"], row["channel"]) == ("channel-lark-3:oc_dm", "lark")
        assert await pool.fetchval("SELECT manager_id FROM chat_threads WHERE session_id = 'channel-lark-3:oc_dm'") \
            == REVIEW
        assert [s["session_id"] for s in await db_chat.list_sessions(manager_id=REVIEW)] == ["channel-lark-3:oc_dm"]
        assert await db_chat.list_sessions() == []

    _run(test, monkeypatch)
