"""``WebChannelAdapter.handle_user_message`` with the API client a message comes from (no database)."""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from typing import Any

from looper.agents.bus import MessageBus
from looper.agents.channels.web import WebChannelAdapter


async def _sent(**kwargs: Any) -> tuple[Any, WebChannelAdapter]:
    with tempfile.TemporaryDirectory() as tmp:
        bus = MessageBus(runtime_root=Path(tmp))
        adapter = WebChannelAdapter()
        await adapter.start(bus)
        await adapter.handle_user_message(text="hi", session_id="cli-1", message_id="m1", **kwargs)
        envelope = await bus.get_queue("manager").get()
        await adapter.stop()
        await bus.shutdown()
        return envelope, adapter


def test_the_envelope_names_the_client_and_keeps_the_web_source() -> None:
    envelope, adapter = asyncio.run(_sent(client={"id": "client-1", "name": "laptop", "x": "y"}))
    assert envelope.payload["channel_context"]["client"] == {"id": "client-1", "name": "laptop"}
    assert envelope.source == adapter._address and envelope.metadata["origin_channel_id"] == adapter.channel_id


def test_without_a_client_the_context_has_none() -> None:
    envelope, _ = asyncio.run(_sent())
    assert "client" not in envelope.payload["channel_context"]
