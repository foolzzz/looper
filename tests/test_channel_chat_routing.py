"""Native admission pins a destination before observation and keeps egress unchanged."""
from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest

from looper.agents.bus import MessageBus
from looper.agents.channels.chat_routing import RouteAction, RouteDecision
from looper.agents.channels.telegram import TelegramChannelAdapter
from looper.agents.managers import ChatStopSummary, chat_stop_reply
from looper.agents.message import MessageEnvelope, MessageType, channel_thread_id, manager_address
from looper.agents.tiers.manager import TURNED_OFF_REPLY
from tests.test_agent_channels import _allowlist, _tg_update
from tests.test_channel_post import _Bot

from looper.agents.channels.lark_bot import make_adapter
from tests.test_agent_channels import _lark_event
from tests.test_channel_bot_adapters import _lark, _queue
from tests.test_lark_transport import BOT_ID, OPEN_ID, OTHER_ID, StubTransport


def telegram(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, channel_id: str = "channel-telegram",
             owner: str = "manager", **kwargs: Any) -> tuple[Any, Any, Any, Any]:
    bus, bot = MessageBus(runtime_root=tmp_path), _Bot()
    adapter = TelegramChannelAdapter(channel_id=channel_id, manager_id=owner,
                                     allowlist=_allowlist(channel_id, "7"), **kwargs)
    adapter._bus, adapter._app = bus, SimpleNamespace(bot=bot)
    adapter._acknowledge = Mock()
    observation = AsyncMock()
    monkeypatch.setattr("looper.agents.channels.telegram.observe_chat", observation)
    return adapter, bus, bot, observation


@pytest.mark.parametrize("handler", ["_on_message", "_cmd_start", "_cmd_help", "_cmd_status", "_cmd_new",
                                     "_cmd_stop", "_cmd_profile", "_cmd_gates", "_cmd_gate"])
def test_bound_only_silences_every_telegram_entry_before_effects(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                               handler: str) -> None:
    async def main() -> None:
        resolver = Mock(return_value=RouteDecision(RouteAction.SILENT, "manager"))
        adapter, bus, bot, observed = telegram(tmp_path, monkeypatch, resolve_chat=resolver)
        if handler == "_cmd_help":
            await adapter._cmd_help(_tg_update(8), None)
            resolver.assert_not_called()
            observed.assert_not_awaited()
        update = _tg_update(7, "/gate 1 yes")
        await getattr(adapter, handler)(update, None)
        resolver.assert_called_once_with("channel-telegram", "99")
        observed.assert_awaited_once_with("channel-telegram", "99", suppressed=True)
        update.message.reply_text.assert_not_awaited()
        adapter._acknowledge.assert_not_called()
        assert not bot.sent and not adapter._correlation_chat and not adapter._status._open
        assert bus.get_queue("manager").empty()
        # Local commands remain usable with admission enabled: the silence check covers their actual reply path.
        resolver.return_value = RouteDecision(RouteAction.ADMIT, "manager")
        await getattr(adapter, handler)(update, None)
        if handler in {"_cmd_start", "_cmd_help"}:
            update.message.reply_text.assert_awaited_once()
        else:
            message = bus.get_queue("manager").get_nowait()
            if handler == "_cmd_status":
                assert message.payload == {"text": "当前任务进度怎么样？", "language": "zh",
                                           "channel_context": {"chat_id": 99}}
            adapter._acknowledge.assert_called_once()
        await bus.shutdown()
    asyncio.run(main())

@pytest.mark.parametrize("channel_id,chat_id", [("channel-telegram", 99), ("channel-telegram-2", -1001234567)])
@pytest.mark.parametrize("explicit,target", [(False, "manager-alpha"), (True, "manager-alpha"), (True, "manager-beta")])
def test_telegram_admission_preserves_chat_and_thread_identity(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                             channel_id: str, chat_id: int, explicit: bool,
                                                             target: str) -> None:
    async def main() -> None:
        adapter, bus, _, observed = telegram(tmp_path, monkeypatch, channel_id=channel_id, owner="manager-alpha",
            resolve_chat=lambda c, i: RouteDecision(RouteAction.ADMIT, target, explicit), manager_running=lambda manager: manager == target)
        update = _tg_update(7, "hello", chat_id, replies_to=17)
        await adapter._on_message(update, None)
        message = bus.get_queue(target).get_nowait()
        assert message.target.agent_id == target and message.source.agent_id == channel_id
        assert message.metadata == {"origin_channel_id": channel_id, "channel_manager_id": "manager-alpha",
                                    "channel_thread_id": channel_thread_id(channel_id, chat_id, target, "manager-alpha")}
        assert message.payload["channel_context"] == {"chat_id": chat_id, "user_id": 7, "username": "someone",
                                                       "message_id": "41", "reply_to_message_id": "17"}
        assert type(message.payload["channel_context"]["chat_id"]) is int
        observed.assert_awaited_once_with(channel_id, str(chat_id), suppressed=False)
        await bus.shutdown()
    asyncio.run(main())

@pytest.mark.parametrize("handler", ["_on_message", "_cmd_start", "_cmd_help"])
@pytest.mark.parametrize("running", [False, None])
def test_explicit_telegram_unavailable_target_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                         running: bool | None, handler: str) -> None:
    def available(manager: str) -> bool:
        assert manager == "manager-beta"
        if running is None:
            raise RuntimeError("availability unavailable")
        return running

    async def main() -> None:
        adapter, bus, bot, _ = telegram(tmp_path, monkeypatch,
            resolve_chat=lambda c, i: RouteDecision(RouteAction.ADMIT, "manager-beta", True), manager_running=available)
        update = _tg_update(7)
        await getattr(adapter, handler)(update, None)
        update.message.reply_text.assert_not_awaited()
        assert [item["text"] for item in bot.sent] == [TURNED_OFF_REPLY]
        assert bus.get_queue("manager-beta").empty() and bus.get_queue("manager").empty()
        adapter._acknowledge.assert_not_called()
        assert not adapter._correlation_chat
        await bus.shutdown()
    asyncio.run(main())

def test_telegram_pins_before_observation_and_keeps_admitted_replies(tmp_path: Path,
                                                                   monkeypatch: pytest.MonkeyPatch) -> None:
    async def main() -> None:
        destination = "manager-alpha"
        adapter, bus, bot, observed = telegram(tmp_path, monkeypatch,
            resolve_chat=lambda c, i: RouteDecision(RouteAction.ADMIT, destination, True), manager_running=lambda _: True)
        entered, release = asyncio.Event(), asyncio.Event()
        async def slow_observation(*args: Any, **kwargs: Any) -> None:
            entered.set()
            await release.wait()
        observed.side_effect = slow_observation
        pending = asyncio.create_task(adapter._on_message(_tg_update(7), None))
        await entered.wait()
        destination = "manager-beta"
        release.set()
        await pending
        original = bus.get_queue("manager-alpha").get_nowait()
        await adapter._on_message(_tg_update(7), None)
        assert bus.get_queue("manager-beta").get_nowait().target.agent_id == "manager-beta"
        destination = "manager-alpha"
        await adapter._on_message(_tg_update(7), None)
        assert bus.get_queue("manager-alpha").get_nowait().target.agent_id == "manager-alpha"
        reply = MessageEnvelope.create(source=manager_address("manager-alpha"), target=original.source,
            message_type=MessageType.AGENT_REPLY, payload={"text": "original answer"},
            correlation_id=original.correlation_id, metadata=original.metadata)
        destination = "manager-beta"
        await adapter.deliver(reply)
        assert [item["text"] for item in bot.sent] == ["original answer"]
        assert original.target.agent_id == "manager-alpha" and original.metadata["channel_thread_id"].endswith("#manager-alpha")
        await bus.shutdown()
    asyncio.run(main())

def test_unavailable_telegram_stop_uses_fleet_once_without_a_model_turn(tmp_path: Path,
                                                                    monkeypatch: pytest.MonkeyPatch) -> None:
    async def main() -> None:
        summary = ChatStopSummary(matched=2, managers=2, stopping=2)
        stop = AsyncMock(return_value=summary)
        adapter, bus, bot, _ = telegram(tmp_path, monkeypatch,
            resolve_chat=lambda c, i: RouteDecision(RouteAction.ADMIT, "manager-beta", True),
            manager_running=lambda _: False, stop_chat=stop)
        await adapter._cmd_stop(_tg_update(7, "/stop"), None)
        stop.assert_awaited_once_with("channel-telegram", "99", via="telegram")
        assert [item["text"] for item in bot.sent] == [chat_stop_reply(summary)]
        assert not adapter._correlation_chat and bus.get_queue("manager-beta").empty()
        adapter._acknowledge.assert_not_called()
        await bus.shutdown()
    asyncio.run(main())


@pytest.fixture
def lark_observations(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    seen: list[Any] = []

    async def observe(channel: str, chat: str, **kwargs: Any) -> None:
        seen.append((channel, chat, kwargs))

    monkeypatch.setattr("looper.agents.channels.lark.observe_chat", observe)
    return seen


@pytest.mark.parametrize("event", [
    _lark_event(OTHER_ID), _lark_event(OPEN_ID, chat_type="group"),
    _lark_event(OPEN_ID, sender_type="bot"), _lark_event(OPEN_ID, message_type="image"),
    _lark_event(OPEN_ID, " "),
], ids=["allowlist", "group-without-mention", "bot", "nontext", "empty"])
def test_lark_binding_cannot_bypass_native_eligibility(
    tmp_path: Path, lark_observations: list[Any], event: dict[str, Any],
) -> None:
    async def main() -> None:
        resolved = []
        bus, transport = MessageBus(runtime_root=tmp_path), StubTransport()
        def resolve(channel: str, chat: str) -> RouteDecision:
            resolved.append((channel, chat))
            return RouteDecision(RouteAction.ADMIT, "manager-bound", True)
        adapter = _lark(tmp_path, bus, transport, resolve_chat=resolve)
        await adapter._handle_lark_event(event)
        assert resolved == lark_observations == transport.calls == []
        await adapter.stop()
    asyncio.run(main())


@pytest.mark.parametrize("group", [False, True])
def test_lark_bound_only_is_silent_before_names_reactions_or_control(
    tmp_path: Path, lark_observations: list[Any], group: bool,
) -> None:
    async def main() -> None:
        bus, transport = MessageBus(runtime_root=tmp_path), StubTransport()
        adapter = _lark(tmp_path, bus, transport,
                        resolve_chat=lambda *_: RouteDecision(RouteAction.SILENT, "manager"))
        for text in ["hello", "/stop"]:
            await adapter._handle_lark_event(_lark_event(
                OPEN_ID, f"@bot {text}" if group else text, chat_type="group" if group else "p2p",
                mentions=[{"id": BOT_ID, "name": "bot"}] if group else []))
        assert len(lark_observations) == 2 and all(item[2] == {"suppressed": True} for item in lark_observations)
        assert transport.calls == [] and adapter._correlation_chat == {} and adapter._group_message == {}
        assert bus.journal.undelivered() == [] and not adapter._reactions._pending
        await adapter.stop()
    asyncio.run(main())


@pytest.mark.parametrize("availability", [False, "error"])
def test_lark_explicit_unavailable_target_never_falls_back(
    tmp_path: Path, lark_observations: list[Any], availability: Any,
) -> None:
    async def main() -> None:
        def running(manager: str) -> bool:
            if availability == "error":
                raise RuntimeError("availability failed")
            return availability
        bus, transport = MessageBus(runtime_root=tmp_path), StubTransport()
        adapter = _lark(tmp_path, bus, transport, manager_running=running,
                        resolve_chat=lambda *_: RouteDecision(RouteAction.ADMIT, "manager-bound", True))
        await adapter._handle_lark_event(_lark_event(OPEN_ID))
        assert transport.calls == [("send", {"chat_id": "oc_1", "text": TURNED_OFF_REPLY})]
        assert bus.journal.undelivered() == [] and adapter._correlation_chat == {} and not adapter._reactions._pending
        await adapter.stop()
    asyncio.run(main())


def test_lark_pins_route_before_sender_lookup_and_keeps_old_reply(
    tmp_path: Path, lark_observations: list[Any],
) -> None:
    async def main() -> None:
        bus, transport = MessageBus(runtime_root=tmp_path), StubTransport()
        looked_up, release = asyncio.Event(), asyncio.Event()
        async def names(ids: Any) -> dict[str, str]:
            looked_up.set()
            await release.wait()
            return {OPEN_ID: "Ada"}
        transport.names = names
        selected = "manager-alpha"
        adapter = _lark(tmp_path, bus, transport, manager_id="manager-owner",
                        manager_running=lambda manager: manager != "manager-owner",
                        resolve_chat=lambda *_: RouteDecision(RouteAction.ADMIT, selected, True))
        event = _lark_event(OPEN_ID, "@bot hi", chat_type="group", mentions=[{"id": BOT_ID, "name": "bot"}])
        first = asyncio.create_task(adapter._handle_lark_event(event))
        await asyncio.wait_for(looked_up.wait(), 2)
        selected = "manager-beta"
        release.set()
        await first
        [original] = _queue(bus, "manager-alpha")
        assert original.metadata["channel_thread_id"] == "lark:oc_1#manager-alpha"
        assert original.metadata["channel_manager_id"] == "manager-owner"
        await adapter._handle_lark_event(event)
        [next_message] = _queue(bus, "manager-beta")
        assert next_message.metadata["channel_thread_id"] == "lark:oc_1#manager-beta"
        reply = MessageEnvelope.create(source=manager_address("manager-alpha"), target=original.source,
            message_type=MessageType.AGENT_REPLY, correlation_id=original.correlation_id, payload={"text": "A done"})
        assert (await adapter.deliver(reply)).success
        assert ("reply", {"message_id": "om_1", "markdown": "A done"}) in transport.calls
        await asyncio.gather(*adapter._reactions._pending)
        assert ("react", {"message_id": "om_1"}) in transport.calls
        await adapter.stop()
    asyncio.run(main())


@pytest.mark.parametrize("group", [False, True])
def test_lark_unavailable_stop_calls_fleet_once_and_acknowledges_once(
    tmp_path: Path, lark_observations: list[Any], group: bool,
) -> None:
    async def main() -> None:
        calls = []
        summary = ChatStopSummary(stopping=2, matched=2, managers=2)
        async def stop(*args: Any, **kwargs: Any) -> ChatStopSummary:
            calls.append((args, kwargs))
            return summary
        bus, transport = MessageBus(runtime_root=tmp_path), StubTransport()
        adapter = _lark(tmp_path, bus, transport, manager_running=lambda _: False, stop_chat=stop,
                        resolve_chat=lambda *_: RouteDecision(RouteAction.ADMIT, "manager-gone", True))
        await adapter._handle_lark_event(_lark_event(OPEN_ID, "@bot /stop" if group else "/stop",
            chat_type="group" if group else "p2p", mentions=[{"id": BOT_ID, "name": "bot"}] if group else []))
        assert calls == [(("channel-lark", "oc_1"), {"via": "lark"})]
        destination = ("reply", {"message_id": "om_1", "text": chat_stop_reply(summary)}) if group else (
            "send", {"chat_id": "oc_1", "text": chat_stop_reply(summary)})
        assert transport.calls == [destination] and bus.journal.undelivered() == []
        assert adapter._correlation_chat == {} and not adapter._reactions._pending
        await adapter.stop()
    asyncio.run(main())


def test_lark_bot_factory_propagates_live_routing(tmp_path: Path) -> None:
    def resolve(*args: Any) -> RouteDecision:
        return RouteDecision(RouteAction.ADMIT, "manager-bound", True)
    async def stop(*args: Any, **kwargs: Any) -> ChatStopSummary:
        return ChatStopSummary()
    from looper.agents.channels.allowlist import SenderAllowlist
    adapter = make_adapter({"app_id": "cli_public", "domain": "feishu"}, "synthetic-secret", runtime_root=tmp_path,
        allowlist=SenderAllowlist("channel-lark_bot", frozenset({OPEN_ID})), on_rejected=None,
        bot_open_id=BOT_ID, resolve_chat=resolve, stop_chat=stop)
    assert adapter._resolve_chat is resolve and adapter._stop_chat is stop
    asyncio.run(adapter.stop())


@pytest.mark.parametrize("suppressed", [False, True])
@pytest.mark.parametrize("failure", ["observation", "logging"])
def test_observation_or_logging_failure_preserves_admission_and_silence(tmp_path, monkeypatch, suppressed, failure):
    from looper.agents.channels import chat_routing
    async def main():
        action = RouteAction.SILENT if suppressed else RouteAction.ADMIT
        adapter, bus, bot, _ = telegram(tmp_path, monkeypatch,
            resolve_chat=lambda *_: RouteDecision(action, "manager"))
        monkeypatch.setattr("looper.agents.channels.telegram.observe_chat", chat_routing.observe_chat)
        monkeypatch.setattr(chat_routing.storage, "record_seen", AsyncMock(side_effect=OSError("unavailable")))
        if failure == "logging":
            monkeypatch.setattr(chat_routing.logger, "info", Mock(side_effect=OSError("logging unavailable")))
            monkeypatch.setattr("looper.agents.channels.telegram.logger.info", chat_routing.logger.info)
        await adapter._on_message(_tg_update(7), None)
        assert len(bus.take_queue("manager")) == (0 if suppressed else 1)
        assert not bot.sent and not adapter._status._open
        await bus.shutdown()
    asyncio.run(main())


@pytest.mark.parametrize("kind", ["telegram", "lark", "lark_bot"])
@pytest.mark.parametrize("explicit,silent,running,reason,outcome", [
    (True, False, True, "explicit_binding", "admitted"),
    (False, False, True, "bot_manager", "admitted"),
    (False, True, True, "only_bound_chats", "silenced"),
    (True, False, False, "disabled_target", "answered"),
])
def test_native_routing_boundary_log(tmp_path, monkeypatch, caplog, lark_observations,
                                     kind, explicit, silent, running, reason, outcome):
    caplog.set_level("INFO")
    body, token = "sentinel-private-message", "sentinel-private-token"
    async def run():
        route = RouteDecision(RouteAction.SILENT if silent else RouteAction.ADMIT, "manager-review", explicit)
        options = dict(resolve_chat=lambda *_: route, manager_running=lambda _: running)
        if kind == "telegram":
            adapter, bus, _, _ = telegram(tmp_path, monkeypatch, **options)
            adapter._bot_token = token
            await adapter._on_message(_tg_update(7, body), None)
            chat = "99"
        else:
            bus = MessageBus(runtime_root=tmp_path)
            transport = StubTransport()
            transport.token = token
            adapter = _lark(tmp_path, bus, transport, channel_type=kind, **options)
            await adapter._handle_lark_event(_lark_event(OPEN_ID, body))
            chat = "oc_1"
        records = [r for r in caplog.records if r.msg.startswith("chat_route")]
        assert len(records) == 1
        record = records[0]
        assert record.levelname == "INFO"
        assert (record.channel_id, record.chat_id, record.manager_id) == (
            adapter.channel_id, chat, None if silent else "manager-review")
        assert (record.action, record.reason, record.outcome) == (
            "silent" if silent else "admit", reason, outcome)
        assert all(body not in str(r.__dict__) and token not in str(r.__dict__) for r in caplog.records)
        if kind != "telegram":
            await adapter.stop()
        await bus.shutdown()
    asyncio.run(run())
