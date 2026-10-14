"""Tests for channel adapters and agent system integration."""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from typing import Any

import pytest

from loopx.agents.bus import MessageBus
from loopx.agents.channels.registry import ChannelRegistry
from loopx.agents.channels.web import WebChannelAdapter
from loopx.agents.message import (
    MANAGER_ADDRESS,
    PLANNER_ADDRESS,
    AgentAddress,
    MessageEnvelope,
    MessageType,
    channel_address,
    worker_address,
)


OPEN_ID = "ou_7dab8a3d3cdcc9da365777c7ad535d62"
OTHER_OPEN_ID = "ou_0f1e2d3c4b5a69788796a5b4c3d2e1f0"


class TestWebChannelAdapter:
    def test_handle_user_message(self) -> None:
        async def _run() -> None:
            with tempfile.TemporaryDirectory() as tmp:
                bus = MessageBus(runtime_root=Path(tmp))
                adapter = WebChannelAdapter()
                await adapter.start(bus)

                result = await adapter.handle_user_message(
                    text="hello",
                    session_id="s1",
                )

                assert "correlation_id" in result
                assert "events_url" in result
                assert result["correlation_id"]

                # Message should be in the manager's queue
                q = bus.get_queue("manager")
                assert not q.empty()
                env = await q.get()
                assert env.message_type == MessageType.USER_MESSAGE
                assert env.payload["text"] == "hello"

                await adapter.stop()
                await bus.shutdown()

        asyncio.run(_run())

    def test_deliver_to_sse(self) -> None:
        async def _run() -> None:
            with tempfile.TemporaryDirectory() as tmp:
                bus = MessageBus(runtime_root=Path(tmp))
                adapter = WebChannelAdapter()
                await adapter.start(bus)

                result = await adapter.handle_user_message(text="test")
                corr_id = result["correlation_id"]

                reply = MessageEnvelope.create(
                    source=MANAGER_ADDRESS,
                    target=channel_address("web", "channel-web"),
                    message_type=MessageType.AGENT_REPLY,
                    payload={"text": "response here"},
                    correlation_id=corr_id,
                )
                receipt = await adapter.deliver(reply)
                assert receipt.success

                await adapter.stop()
                await bus.shutdown()

        asyncio.run(_run())

    def test_deliver_no_stream(self) -> None:
        async def _run() -> None:
            with tempfile.TemporaryDirectory() as tmp:
                bus = MessageBus(runtime_root=Path(tmp))
                adapter = WebChannelAdapter()
                await adapter.start(bus)

                reply = MessageEnvelope.create(
                    source=MANAGER_ADDRESS,
                    target=channel_address("web", "channel-web"),
                    message_type=MessageType.AGENT_REPLY,
                    payload={"text": "no stream"},
                    correlation_id="nonexistent-corr",
                )
                receipt = await adapter.deliver(reply)
                assert not receipt.success
                assert "no active SSE stream" in (receipt.error or "")

                await adapter.stop()
                await bus.shutdown()

        asyncio.run(_run())

    @pytest.mark.parametrize("stream_open", [True, False])
    def test_a_reply_naming_a_chat_session_joins_its_history_and_no_stream(
        self, monkeypatch: pytest.MonkeyPatch, stream_open: bool,
    ) -> None:
        """A reply that answers no message (what Claude said on its own) never ends a message's stream."""
        from unittest.mock import AsyncMock

        from loopx.db import chat

        save = AsyncMock(return_value=None)
        monkeypatch.setattr(chat, "save_message", save)

        async def _run() -> None:
            with tempfile.TemporaryDirectory() as tmp:
                bus = MessageBus(runtime_root=Path(tmp))
                adapter = WebChannelAdapter()
                await adapter.start(bus)
                correlation_id = (await adapter.handle_user_message(text="hi"))["correlation_id"] if stream_open \
                    else "finished-corr"
                reply = MessageEnvelope.create(
                    source=MANAGER_ADDRESS,
                    target=channel_address("web", "channel-web"),
                    message_type=MessageType.AGENT_REPLY,
                    payload={"text": "the background job is done", "autonomous": True, "chat_session_id": "chat-1"},
                    correlation_id=correlation_id,
                )
                receipt = await adapter.deliver(reply)
                assert receipt.success
                save.assert_awaited_once_with(session_id="chat-1", message_id=reply.message_id, role="assistant",
                                              content="the background job is done")
                if stream_open:
                    assert adapter._sse_queues[correlation_id].empty()
                await adapter.stop()
                await bus.shutdown()

        asyncio.run(_run())

    def test_envelope_to_sse_event_reply(self) -> None:
        env = MessageEnvelope.create(
            source=MANAGER_ADDRESS,
            target=channel_address("web"),
            message_type=MessageType.AGENT_REPLY,
            payload={"text": "hello user"},
        )
        event = WebChannelAdapter._envelope_to_sse_event(env)
        assert event["kind"] == "answer.final"
        assert event["payload"]["text"] == "hello user"

    def test_envelope_to_sse_event_status(self) -> None:
        env = MessageEnvelope.create(
            source=MANAGER_ADDRESS,
            target=channel_address("web"),
            message_type=MessageType.STATUS_UPDATE,
            payload={"text": "thinking..."},
        )
        event = WebChannelAdapter._envelope_to_sse_event(env)
        assert event["kind"] == "agent.phase"

    def test_envelope_to_sse_event_delta(self) -> None:
        env = MessageEnvelope.create(
            source=MANAGER_ADDRESS,
            target=channel_address("web"),
            message_type=MessageType.STATUS_UPDATE,
            payload={"text": "word", "delta": True},
        )
        event = WebChannelAdapter._envelope_to_sse_event(env)
        assert event["kind"] == "answer.delta"

    def test_healthcheck(self) -> None:
        async def _run() -> None:
            with tempfile.TemporaryDirectory() as tmp:
                bus = MessageBus(runtime_root=Path(tmp))
                adapter = WebChannelAdapter()
                await adapter.start(bus)

                health = adapter.healthcheck()
                assert health["healthy"]
                assert health["channel"] == "web"
                assert health["active_sse_streams"] == 0

                await adapter.stop()
                await bus.shutdown()

        asyncio.run(_run())

    def test_close_sse_stream(self) -> None:
        async def _run() -> None:
            with tempfile.TemporaryDirectory() as tmp:
                bus = MessageBus(runtime_root=Path(tmp))
                adapter = WebChannelAdapter()
                await adapter.start(bus)

                result = await adapter.handle_user_message(text="test")
                corr_id = result["correlation_id"]
                assert len(adapter._sse_queues) == 1

                adapter.close_sse_stream(corr_id)
                assert len(adapter._sse_queues) == 0

                await adapter.stop()
                await bus.shutdown()

        asyncio.run(_run())


class TestChannelRegistry:
    def test_register_and_list(self) -> None:
        async def _run() -> None:
            with tempfile.TemporaryDirectory() as tmp:
                bus = MessageBus(runtime_root=Path(tmp))
                registry = ChannelRegistry(bus=bus)
                adapter = WebChannelAdapter()
                await registry.register(adapter)

                channels = registry.list_channels()
                assert len(channels) == 1
                assert channels[0]["channel_type"] == "web"

                await registry.stop_all()
                await bus.shutdown()

        asyncio.run(_run())

    def test_unregister(self) -> None:
        async def _run() -> None:
            with tempfile.TemporaryDirectory() as tmp:
                bus = MessageBus(runtime_root=Path(tmp))
                registry = ChannelRegistry(bus=bus)
                adapter = WebChannelAdapter()
                await registry.register(adapter)

                await registry.unregister("channel-web")
                assert len(registry.list_channels()) == 0

                await bus.shutdown()

        asyncio.run(_run())


class TestTelegramAdapter:
    def test_split_message_short(self) -> None:
        from loopx.agents.channels.telegram import TelegramChannelAdapter
        chunks = TelegramChannelAdapter._split_message("hello", max_len=100)
        assert chunks == ["hello"]

    def test_split_message_long(self) -> None:
        from loopx.agents.channels.telegram import TelegramChannelAdapter
        text = "line one\nline two\nline three\nline four"
        chunks = TelegramChannelAdapter._split_message(text, max_len=20)
        assert len(chunks) >= 2
        assert "".join(chunks).replace("\n", "") == text.replace("\n", "")


class TestServeChannelWiring:
    """Telegram and Lark are configured from the Settings page (channel_configs);
    a channel without a usable sender allowlist does not start."""

    def test_telegram_adapter_reads_settings(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from loopx.agents.serve import _telegram_adapter_from_config
        monkeypatch.setenv("MY_TG_TOKEN", "token-from-env")
        adapter = _telegram_adapter_from_config(
            {"bot_token_env": "MY_TG_TOKEN", "allowed_user_ids": ["7", "8"]}
        )
        assert adapter is not None
        assert adapter._bot_token == "token-from-env"
        assert adapter._allowlist.ids == {"7", "8"}

    def test_telegram_keeps_reading_a_comma_separated_list(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # How the setting was stored before it became a list.
        from loopx.agents.serve import _telegram_adapter_from_config
        monkeypatch.setenv("LOOPX_TELEGRAM_BOT_TOKEN", "default-token")
        adapter = _telegram_adapter_from_config({"allowed_user_ids": "7, 8"})
        assert adapter is not None
        assert adapter._bot_token == "default-token"
        assert adapter._allowlist.ids == {"7", "8"}

    @pytest.mark.parametrize("config", [{}, {"allowed_user_ids": ""}, {"allowed_user_ids": []}])
    def test_telegram_without_an_allowlist_does_not_start(
        self, config: dict[str, Any], monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
    ) -> None:
        from loopx.agents.serve import _telegram_adapter_from_config
        # The environment fallback no longer opens the channel either.
        monkeypatch.setenv("LOOPX_TELEGRAM_ALLOWED_USERS", "7")
        with caplog.at_level("ERROR"):
            assert _telegram_adapter_from_config(config) is None
        assert "channel-telegram not started" in caplog.text and "allowlist is empty" in caplog.text

    def test_telegram_invalid_allowlist_disables_channel(self) -> None:
        from loopx.agents.serve import _telegram_adapter_from_config
        # Fail closed: a malformed allowlist must not fall back to "allow all".
        assert _telegram_adapter_from_config({"allowed_user_ids": "12,abc"}) is None

    def test_lark_adapter_reads_settings(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        from loopx.agents.serve import _lark_adapter_from_config
        monkeypatch.setenv("MY_LARK_APP", "cli_test")
        adapter = _lark_adapter_from_config({"app_id_env": "MY_LARK_APP", "allowed_open_ids": [OPEN_ID]}, tmp_path)
        assert adapter is not None
        assert adapter._app_id == "cli_test"
        assert adapter._allowlist.ids == {OPEN_ID}

    @pytest.mark.parametrize("config", [{}, {"allowed_open_ids": []}, {"allowed_open_ids": ["on_0123456789abcdef"]}])
    def test_lark_without_a_valid_allowlist_does_not_start(
        self, config: dict[str, Any], tmp_path: Path, caplog: pytest.LogCaptureFixture,
    ) -> None:
        from loopx.agents.serve import _lark_adapter_from_config
        with caplog.at_level("ERROR"):
            assert _lark_adapter_from_config(config, tmp_path) is None
        assert "channel-lark not started" in caplog.text

    def test_failed_optional_channel_does_not_stop_server(self) -> None:
        from loopx.agents.channels.base import ChannelAdapter, DeliveryReceipt
        from loopx.agents.serve import _register_optional_channel

        class _FailingAdapter(ChannelAdapter):
            stopped = False

            @property
            def channel_type(self) -> str:
                return "failing"

            @property
            def channel_id(self) -> str:
                return "channel-failing"

            async def start(self, bus: MessageBus) -> None:
                raise RuntimeError("invalid token")

            async def stop(self) -> None:
                type(self).stopped = True

            async def deliver(self, envelope: MessageEnvelope) -> DeliveryReceipt:
                raise AssertionError("not started")

        async def _run() -> None:
            with tempfile.TemporaryDirectory() as tmp:
                bus = MessageBus(runtime_root=Path(tmp))
                registry = ChannelRegistry(bus=bus)
                await _register_optional_channel(registry, _FailingAdapter())
                assert registry.list_channels() == []
                assert _FailingAdapter.stopped
                await bus.shutdown()

        asyncio.run(_run())


class TestLarkAdapter:
    def test_strip_mention(self) -> None:
        from loopx.agents.channels.lark import LarkChannelAdapter
        assert LarkChannelAdapter._strip_mention("@bot hello") == "hello"
        assert LarkChannelAdapter._strip_mention("hello") == "hello"

    def test_format_reply(self) -> None:
        from loopx.agents.channels.lark import LarkChannelAdapter
        env = MessageEnvelope.create(
            source=MANAGER_ADDRESS,
            target=channel_address("lark"),
            message_type=MessageType.AGENT_REPLY,
            payload={"text": "回复内容"},
        )
        assert LarkChannelAdapter._format_reply(env) == "回复内容"

    def test_format_reply_delta_skipped(self) -> None:
        from loopx.agents.channels.lark import LarkChannelAdapter
        env = MessageEnvelope.create(
            source=MANAGER_ADDRESS,
            target=channel_address("lark"),
            message_type=MessageType.STATUS_UPDATE,
            payload={"text": "chunk", "delta": True},
        )
        assert LarkChannelAdapter._format_reply(env) == ""


class TestEndToEndFlow:
    """Integration test: Web → Manager queue → Planner queue → Worker queue path."""

    def test_message_routing_through_bus(self) -> None:
        async def _run() -> None:
            with tempfile.TemporaryDirectory() as tmp:
                bus = MessageBus(runtime_root=Path(tmp))

                # Simulate: Web adapter publishes user message
                user_env = MessageEnvelope.create(
                    source=channel_address("web", "channel-web"),
                    target=MANAGER_ADDRESS,
                    message_type=MessageType.USER_MESSAGE,
                    payload={"text": "add tests"},
                    metadata={"origin_channel_id": "channel-web"},
                )
                await bus.publish(user_env)

                # Manager receives it
                mq = bus.get_queue("manager")
                msg = await mq.get()
                assert msg.message_type == MessageType.USER_MESSAGE

                # Manager forwards to Planner
                task_env = MessageEnvelope.create(
                    source=MANAGER_ADDRESS,
                    target=PLANNER_ADDRESS,
                    message_type=MessageType.TASK_REQUEST,
                    payload={
                        "goal_id": "g1",
                        "requirement": "add tests",
                        "work_dir": "/tmp",
                    },
                    correlation_id=msg.correlation_id,
                    metadata={"origin_channel_id": "channel-web"},
                )
                await bus.publish(task_env)

                # Planner receives it
                pq = bus.get_queue("planner")
                pmsg = await pq.get()
                assert pmsg.message_type == MessageType.TASK_REQUEST

                # Planner dispatches to Worker
                work_env = MessageEnvelope.create(
                    source=PLANNER_ADDRESS,
                    target=worker_address("worker-1"),
                    message_type=MessageType.WORK_ITEM,
                    payload={
                        "task_id": "step-1",
                        "plan_id": "plan-1",
                        "instruction": "add tests",
                    },
                    correlation_id=msg.correlation_id,
                )
                await bus.publish(work_env)

                # Worker receives it
                wq = bus.get_queue("worker-1")
                wmsg = await wq.get()
                assert wmsg.message_type == MessageType.WORK_ITEM

                # Worker result goes back to Planner
                result_env = MessageEnvelope.create(
                    source=worker_address("worker-1"),
                    target=PLANNER_ADDRESS,
                    message_type=MessageType.WORK_RESULT,
                    payload={
                        "task_id": "step-1",
                        "plan_id": "plan-1",
                        "status": "completed",
                    },
                    correlation_id=msg.correlation_id,
                )
                await bus.publish(result_env)

                # Planner receives result
                pmsg2 = await pq.get()
                assert pmsg2.message_type == MessageType.WORK_RESULT

                await bus.shutdown()

        asyncio.run(_run())

    def test_correlation_id_preserved(self) -> None:
        """Correlation ID flows through the entire pipeline."""

        async def _run() -> None:
            with tempfile.TemporaryDirectory() as tmp:
                bus = MessageBus(runtime_root=Path(tmp))

                corr = "test-corr-123"
                env1 = MessageEnvelope.create(
                    source=channel_address("web"),
                    target=MANAGER_ADDRESS,
                    message_type=MessageType.USER_MESSAGE,
                    payload={"text": "hi"},
                    correlation_id=corr,
                )
                await bus.publish(env1)

                env2 = MessageEnvelope.create(
                    source=MANAGER_ADDRESS,
                    target=PLANNER_ADDRESS,
                    message_type=MessageType.TASK_REQUEST,
                    payload={"requirement": "do stuff"},
                    correlation_id=corr,
                )
                await bus.publish(env2)

                env3 = MessageEnvelope.create(
                    source=PLANNER_ADDRESS,
                    target=MANAGER_ADDRESS,
                    message_type=MessageType.GOAL_STATUS,
                    payload={"status": "completed"},
                    correlation_id=corr,
                )
                await bus.publish(env3)

                mq = bus.get_queue("manager")
                msg1 = await mq.get()  # USER_MESSAGE
                msg2 = await mq.get()  # GOAL_STATUS
                assert msg1.correlation_id == corr
                assert msg2.correlation_id == corr

                await bus.shutdown()

        asyncio.run(_run())


class TestDeliverOnce:
    """Channel-bound messages travel one path: the channel's own queue."""

    def test_reply_reaches_the_sse_stream_once(self) -> None:
        async def _run() -> int:
            with tempfile.TemporaryDirectory() as tmp:
                bus = MessageBus(runtime_root=Path(tmp))
                registry = ChannelRegistry(bus=bus)
                adapter = WebChannelAdapter()
                await registry.register(adapter)
                sent = await adapter.handle_user_message(text="hi", session_id="s1")
                corr = sent["correlation_id"]
                for message_type, payload in (
                    (MessageType.STATUS_UPDATE, {"text": "A", "delta": True}),
                    (MessageType.AGENT_REPLY, {"text": "done"}),
                ):
                    await bus.publish(MessageEnvelope.create(
                        source=MANAGER_ADDRESS,
                        target=channel_address("web"),
                        message_type=message_type,
                        payload=payload,
                        correlation_id=corr,
                        metadata={"origin_channel_id": "channel-web"},
                    ))
                await asyncio.sleep(0.05)
                count = adapter._sse_queues[corr].qsize()
                await registry.stop_all()
                await bus.shutdown()
                return count

        assert asyncio.run(_run()) == 2  # one delta + one final, not four

    def test_manager_progress_is_addressed_to_the_channel(self, agent_db: Any) -> None:
        from loopx.agents.message import TASK_ROUTER_ADDRESS
        from loopx.agents.tiers.manager import ManagerAgent, ManagerConfig

        async def _run() -> tuple[int, int]:
            with tempfile.TemporaryDirectory() as tmp:
                bus = MessageBus(runtime_root=Path(tmp))
                manager = ManagerAgent(config=ManagerConfig(work_dir=Path(tmp)), runtime_root=Path(tmp))
                manager._bus = bus
                await manager._handle(MessageEnvelope.create(
                    source=TASK_ROUTER_ADDRESS,
                    target=MANAGER_ADDRESS,
                    message_type=MessageType.GOAL_STATUS,
                    payload={"task_id": "t1", "status": "executing", "summary": "进度更新: 0/1 完成"},
                    metadata={"origin_channel_id": "channel-web"},
                ))
                counts = (bus.pending_count("channel-web"), bus.pending_count("task-router"))
                await bus.shutdown()
                return counts

        assert asyncio.run(_run()) == (1, 0)


# --- Sender allowlists: only listed senders reach the Manager ---


class TestSenderIds:
    @pytest.mark.parametrize(("raw", "expected"), [
        (None, ()),
        ([], ()),
        (["123456789"], ("123456789",)),
        ([" 42 ", "", "7", "42"], ("42", "7")),  # trimmed, blanks and repeats dropped, order kept
        ("7, 8,,", ("7", "8")),  # one comma-separated string, as the setting was stored before
        ([123456789], ("123456789",)),  # a number from a JSON client
    ])
    def test_telegram_user_ids(self, raw: Any, expected: tuple[str, ...]) -> None:
        from loopx.agents.channels.allowlist import sender_ids
        assert sender_ids("telegram", raw) == expected

    @pytest.mark.parametrize("bad", ["abc", "-1001234567890", "0", "007", "1.5", "@someone", "12 34"])
    def test_telegram_refuses_what_is_not_a_user_id(self, bad: str) -> None:
        from loopx.agents.channels.allowlist import AllowlistError, sender_ids
        with pytest.raises(AllowlistError, match="is not a Telegram user ID"):
            sender_ids("telegram", ["7", bad])

    def test_lark_open_ids(self) -> None:
        from loopx.agents.channels.allowlist import sender_ids
        assert sender_ids("lark", [f" {OPEN_ID} ", OTHER_OPEN_ID, OPEN_ID]) == (OPEN_ID, OTHER_OPEN_ID)

    @pytest.mark.parametrize("bad", [
        "on_7dab8a3d3cdcc9da365777c7ad535d62",  # a union_id
        "5d9bdxxx",  # a user_id
        "ou_",
        "ou_7dab 8a3d3cdcc9da",
        "ou_7dab8a3d3cdcc9da365777c7ad535d62\nforged log line",
        "123456789",
    ])
    def test_lark_refuses_what_is_not_an_open_id(self, bad: str) -> None:
        from loopx.agents.channels.allowlist import AllowlistError, sender_ids
        with pytest.raises(AllowlistError, match="is not a Lark open_id; .*starts with ou_"):
            sender_ids("lark", [bad])

    @pytest.mark.parametrize("raw", [{"id": "7"}, 7, [True], [["7"]]])
    def test_a_list_holds_strings(self, raw: Any) -> None:
        from loopx.agents.channels.allowlist import AllowlistError, sender_ids
        with pytest.raises(AllowlistError):
            sender_ids("lark" if raw == [True] else "telegram", raw)

    def test_a_list_is_bounded(self) -> None:
        from loopx.agents.channels.allowlist import MAX_SENDERS, AllowlistError, sender_ids
        assert len(sender_ids("telegram", [str(n) for n in range(1, MAX_SENDERS + 1)])) == MAX_SENDERS
        with pytest.raises(AllowlistError, match=f"at most {MAX_SENDERS}"):
            sender_ids("telegram", [str(n) for n in range(1, MAX_SENDERS + 2)])

    @pytest.mark.parametrize(("channel_type", "config", "problem"), [
        ("telegram", {}, "allowlist is empty"),
        ("telegram", {"allowed_user_ids": ""}, "allowlist is empty"),
        ("telegram", {"allowed_user_ids": "7,abc"}, "allowlist is invalid: 'abc' is not a Telegram user ID"),
        ("telegram", {"allowed_user_ids": ["7"]}, None),
        ("lark", {"app_id_env": "LARK_APP_ID"}, "allowlist is empty"),
        ("lark", {"allowed_open_ids": [OPEN_ID]}, None),
        ("web", {}, None),  # the web channel has no allowlist: it is local-only
    ])
    def test_why_a_channel_does_not_start(self, channel_type: str, config: dict[str, Any], problem: str | None) -> None:
        from loopx.agents.channels.allowlist import start_problem
        found = start_problem(channel_type, config)
        assert found is None if problem is None else problem in (found or "")

    def test_a_stored_config_holds_the_validated_list(self) -> None:
        from loopx.agents.channels.allowlist import AllowlistError, normalized_config
        assert normalized_config("telegram", {"bot_token_env": "T", "allowed_user_ids": " 7, 8 "}) == {
            "bot_token_env": "T", "allowed_user_ids": ["7", "8"]}
        assert normalized_config("lark", {"app_id_env": "A"}) == {"app_id_env": "A"}
        with pytest.raises(AllowlistError, match="must be a JSON object"):
            normalized_config("lark", ["ou_x"])


def _allowlist(channel_id: str, *ids: str) -> Any:
    from loopx.agents.channels.allowlist import SenderAllowlist
    return SenderAllowlist(channel_id, frozenset(ids))


def _manager_messages(bus: MessageBus) -> list[MessageEnvelope]:
    queue = bus.get_queue("manager")
    messages = []
    while not queue.empty():
        messages.append(queue.get_nowait())
    return messages


def _tg_update(user_id: int | None, text: str = "hello", chat_id: int = 99) -> Any:
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    return SimpleNamespace(
        effective_user=None if user_id is None else SimpleNamespace(id=user_id, username="someone"),
        effective_chat=SimpleNamespace(id=chat_id),
        message=SimpleNamespace(text=text, reply_text=AsyncMock()),
    )


class TestTelegramAllowlist:
    """At the adapter's message handlers: only a listed sender reaches the Manager."""

    def _run(self, tmp_path: Path, handler: str, update: Any, *ids: str) -> list[MessageEnvelope]:
        from loopx.agents.channels.telegram import TelegramChannelAdapter

        async def _main() -> list[MessageEnvelope]:
            bus = MessageBus(runtime_root=tmp_path)
            adapter = TelegramChannelAdapter(bot_token="0:not-a-token", allowlist=_allowlist("channel-telegram", *ids))
            adapter._bus = bus  # what start() sets, without polling Telegram
            await getattr(adapter, handler)(update, None)
            messages = _manager_messages(bus)
            await bus.shutdown()
            return messages

        return asyncio.run(_main())

    def test_a_listed_sender_reaches_the_manager(self, tmp_path: Path) -> None:
        (message,) = self._run(tmp_path, "_on_message", _tg_update(7, "build it"), "7")
        assert message.message_type == MessageType.USER_MESSAGE
        assert message.payload["text"] == "build it"
        assert message.payload["channel_context"]["user_id"] == 7

    @pytest.mark.parametrize("handler", ["_on_message", "_cmd_start", "_cmd_status", "_cmd_help"])
    def test_an_unlisted_sender_is_dropped_without_a_reply(
        self, tmp_path: Path, handler: str, caplog: pytest.LogCaptureFixture,
    ) -> None:
        update = _tg_update(8, "rm -rf the secret plan")
        with caplog.at_level("WARNING"):
            assert self._run(tmp_path, handler, update, "7") == []
        update.message.reply_text.assert_not_awaited()
        (line,) = [r.getMessage() for r in caplog.records if "allowlist" in r.getMessage()]
        assert "channel-telegram" in line and "'8'" in line and "secret plan" not in line

    def test_a_message_without_a_sender_is_dropped(self, tmp_path: Path) -> None:
        assert self._run(tmp_path, "_on_message", _tg_update(None), "7") == []

    def test_a_listed_sender_still_gets_the_commands(self, tmp_path: Path) -> None:
        update = _tg_update(7)
        assert self._run(tmp_path, "_cmd_help", update, "7") == []
        update.message.reply_text.assert_awaited_once()
        (status,) = self._run(tmp_path, "_cmd_status", _tg_update(7), "7")
        assert status.message_type == MessageType.USER_MESSAGE

    def test_an_empty_allowlist_keeps_the_channel_from_starting(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture,
    ) -> None:
        from loopx.agents.channels.telegram import TelegramChannelAdapter

        async def _main() -> tuple[Any, dict[str, Any]]:
            bus = MessageBus(runtime_root=tmp_path)
            adapter = TelegramChannelAdapter(bot_token="0:not-a-token", allowlist=_allowlist("channel-telegram"))
            await adapter.start(bus)  # would poll Telegram if it started
            await bus.shutdown()
            return adapter._app, adapter.healthcheck()

        with caplog.at_level("ERROR"):
            app, health = asyncio.run(_main())
        assert app is None and not health["healthy"]
        assert "channel-telegram not started" in caplog.text and "allowlist is empty" in caplog.text


def _lark_event(open_id: str | None, text: str = "hello") -> dict[str, Any]:
    import json as _json
    sender_id = {"user_id": "5d9bdxxx", "union_id": "on_7dab8a3d3cdcc9da365777c7ad535d62"}
    if open_id is not None:
        sender_id["open_id"] = open_id
    return {
        "type": "im.message.receive_v1",
        "event": {
            "sender": {"sender_id": sender_id, "sender_type": "user"},
            "message": {"chat_id": "oc_1", "message_id": "om_1", "message_type": "text",
                        "content": _json.dumps({"text": text})},
        },
    }


class TestLarkAllowlist:
    """At the adapter's event handler: only a listed open_id reaches the Manager."""

    def _run(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, event: dict[str, Any], *ids: str) -> Any:
        from types import SimpleNamespace

        from loopx.agents.channels.lark import LarkChannelAdapter

        sent: list[tuple[str, str]] = []

        async def _send(self: Any, chat_id: str, text: str) -> None:
            sent.append((chat_id, text))

        monkeypatch.setattr(LarkChannelAdapter, "_send_lark_message", _send)

        async def _main() -> Any:
            bus = MessageBus(runtime_root=tmp_path)
            adapter = LarkChannelAdapter(app_id="cli_test", runtime_root=tmp_path,
                                         allowlist=_allowlist("channel-lark", *ids))
            adapter._bus = bus  # what start() sets, without running lark-cli
            await adapter._handle_lark_event(event)
            messages = _manager_messages(bus)
            await bus.shutdown()
            return SimpleNamespace(messages=messages, sent=sent, conversations=dict(adapter._correlation_chat))

        return asyncio.run(_main())

    def test_a_listed_sender_reaches_the_manager(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        seen = self._run(tmp_path, monkeypatch, _lark_event(OPEN_ID, "@bot build it"), OPEN_ID)
        (message,) = seen.messages
        assert message.payload["text"] == "build it"
        assert message.payload["channel_context"]["sender_id"] == OPEN_ID

    def test_an_unlisted_sender_is_dropped_without_a_reply(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
    ) -> None:
        with caplog.at_level("WARNING"):
            seen = self._run(tmp_path, monkeypatch, _lark_event(OTHER_OPEN_ID, "the secret plan"), OPEN_ID)
        assert seen.messages == [] and seen.sent == [] and seen.conversations == {}
        (line,) = [r.getMessage() for r in caplog.records if "allowlist" in r.getMessage()]
        assert "channel-lark" in line and OTHER_OPEN_ID in line and "secret plan" not in line

    def test_only_the_open_id_counts(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # The event's other ids name the same user, but the list holds open_ids.
        assert self._run(tmp_path, monkeypatch, _lark_event(None), OPEN_ID).messages == []
        assert self._run(tmp_path, monkeypatch, _lark_event(OTHER_OPEN_ID), "5d9bdxxx").messages == []

    def test_an_empty_allowlist_keeps_the_channel_from_starting(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
    ) -> None:
        from loopx.agents.channels import lark
        # lark-cli and an app id are there: only the empty list stops it.
        monkeypatch.setattr(lark, "_check_lark_cli_available", lambda: True)

        async def _main() -> tuple[Any, dict[str, Any]]:
            bus = MessageBus(runtime_root=tmp_path)
            adapter = lark.LarkChannelAdapter(app_id="cli_test", runtime_root=tmp_path,
                                              allowlist=_allowlist("channel-lark"))
            await adapter.start(bus)
            tasks = adapter._collector_task
            await adapter.stop()
            await bus.shutdown()
            return tasks, adapter.healthcheck()

        with caplog.at_level("ERROR"):
            collector, health = asyncio.run(_main())
        assert collector is None and not health["healthy"]
        assert "channel-lark not started" in caplog.text and "allowlist is empty" in caplog.text
