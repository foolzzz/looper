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

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("", set()),
            ("  ", set()),
            ("42", {42}),
            (" 1, 2,,3 ", {1, 2, 3}),
            ("7,7", {7}),
        ],
    )
    def test_parse_allowed_user_ids(self, raw: str, expected: set[int]) -> None:
        from loopx.agents.channels.telegram import parse_allowed_user_ids
        assert parse_allowed_user_ids(raw) == expected

    @pytest.mark.parametrize("raw", ["abc", "1,x", "1;2", "@user"])
    def test_parse_allowed_user_ids_rejects_non_integer(self, raw: str) -> None:
        from loopx.agents.channels.telegram import parse_allowed_user_ids
        with pytest.raises(ValueError):
            parse_allowed_user_ids(raw)


class TestServeChannelWiring:
    """Telegram and Lark are configured from the Settings page (channel_configs)."""

    def test_telegram_adapter_reads_settings(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from loopx.agents.serve import _telegram_adapter_from_config
        monkeypatch.setenv("MY_TG_TOKEN", "token-from-env")
        monkeypatch.delenv("LOOPX_TELEGRAM_ALLOWED_USERS", raising=False)
        adapter = _telegram_adapter_from_config(
            {"bot_token_env": "MY_TG_TOKEN", "allowed_user_ids": "7, 8"}
        )
        assert adapter is not None
        assert adapter._bot_token == "token-from-env"
        assert adapter._allowed_user_ids == {7, 8}

    def test_telegram_adapter_defaults(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from loopx.agents.serve import _telegram_adapter_from_config
        monkeypatch.setenv("LOOPX_TELEGRAM_BOT_TOKEN", "default-token")
        monkeypatch.delenv("LOOPX_TELEGRAM_ALLOWED_USERS", raising=False)
        adapter = _telegram_adapter_from_config({})
        assert adapter is not None
        assert adapter._bot_token == "default-token"
        assert adapter._allowed_user_ids == set()

    def test_telegram_invalid_allowlist_disables_channel(self) -> None:
        from loopx.agents.serve import _telegram_adapter_from_config
        # Fail closed: a malformed allowlist must not fall back to "allow all".
        assert _telegram_adapter_from_config({"allowed_user_ids": "12,abc"}) is None

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
                        "project_root": "/tmp",
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
