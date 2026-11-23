import asyncio
from unittest.mock import AsyncMock

import pytest

from looper.agents.channels.base import DeliveryReceipt
from looper.agents.channels.registry import ChannelRegistry
from looper.agents.bus import MessageBus


def test_registry_notify_passes_retry_key(tmp_path, monkeypatch):
    from looper.agents import turn_metrics

    observed = []
    monkeypatch.setattr(turn_metrics, "record_proactive", lambda ref, **fields: observed.append(fields))
    ref = turn_metrics.ProactiveRef(turn_metrics.ProactiveSource.SCHEDULE, "event-1")

    async def run():
        registry = ChannelRegistry(MessageBus(tmp_path))
        adapter = AsyncMock()
        adapter.notify.return_value = DeliveryReceipt('lark', True)
        registry._channels['channel-lark'] = adapter
        assert await registry.notify('channel-lark', 'oc_synthetic', 'hello',
                                     idempotency_key='event-1', proactive=ref) == (True, None)
        adapter.notify.assert_awaited_once_with('oc_synthetic', 'hello', idempotency_key='event-1')
        assert observed[0]["dedupe_key"] == "event-1"
        assert observed[0]["outcome"] == "sent"
    asyncio.run(run())


def test_lark_notify_uses_stable_scope(monkeypatch):
    from looper.agents.channels.lark import LarkChannelAdapter
    from looper.agents.channels.allowlist import SenderAllowlist
    adapter = LarkChannelAdapter(allowlist=SenderAllowlist('lark', frozenset({'ou_synthetic'})))
    scopes = []
    from contextlib import contextmanager
    @contextmanager
    def scope(key, **kwargs):
        scopes.append(key)
        yield
    monkeypatch.setattr(adapter._transport, 'message_scope', scope)
    monkeypatch.setattr(adapter, '_notify', AsyncMock(return_value=DeliveryReceipt('lark', True)))
    async def run():
        await adapter.notify('oc_synthetic', 'hello', idempotency_key='event-1')
        await adapter.notify('oc_synthetic', 'hello', idempotency_key='event-1')
        assert scopes == ['event-1', 'event-1']
    asyncio.run(run())


def test_lark_start_checks_unfinished_outbox_when_switch_is_off(monkeypatch, tmp_path):
    from types import SimpleNamespace
    from looper.agents import chat_settings
    from looper.agents.channels.allowlist import SenderAllowlist
    from looper.agents.channels.lark import LarkChannelAdapter
    from looper.agents.channels.lark_transport import LarkTransportKind

    async def run():
        transport = SimpleNamespace(available=lambda: True, kind=LarkTransportKind.BOT,
                                    aclose=AsyncMock())
        adapter = LarkChannelAdapter(channel_type='lark_bot', transport=transport,
                                     allowlist=SenderAllowlist('lark_bot', frozenset({'ou_synthetic'})))
        check = AsyncMock()
        monkeypatch.setattr(chat_settings, '_outbound_enabled', False)
        monkeypatch.setattr(adapter._channel_outbox, 'start_if_needed', check)
        monkeypatch.setattr(adapter, '_outbound_loop', AsyncMock())
        monkeypatch.setattr(adapter, '_event_collector_loop', AsyncMock())
        try:
            await adapter.start(MessageBus(tmp_path))
            check.assert_awaited_once_with()
        finally:
            await adapter.stop()
    asyncio.run(run())


@pytest.mark.parametrize('failure', [RuntimeError('pool unavailable'), OSError('database unavailable')])
def test_disabled_lark_starts_without_outbox_database(monkeypatch, tmp_path, failure):
    from types import SimpleNamespace
    from looper.agents import chat_settings
    from looper.agents.channels.lark import LarkChannelAdapter
    from looper.agents.channels.allowlist import SenderAllowlist
    from looper.db import channel_outbox
    from test_lark_transport import StubTransport

    async def run():
        monkeypatch.setattr(chat_settings, '_outbound_enabled', False)
        probe = AsyncMock(side_effect=failure)
        monkeypatch.setattr(channel_outbox, 'has_unfinished', probe)
        transport = StubTransport()
        adapter = LarkChannelAdapter(channel_id='channel-lark_bot', channel_type='lark_bot',
            transport=transport, runtime_root=tmp_path,
            allowlist=SenderAllowlist('channel-lark_bot', frozenset({'ou_synthetic'})))
        monkeypatch.setattr(adapter, '_outbound_loop', AsyncMock())
        monkeypatch.setattr(adapter, '_event_collector_loop', AsyncMock())
        try:
            await adapter.start(SimpleNamespace(runtime_root=tmp_path))
            assert adapter._running
            assert adapter._channel_outbox._task is None
            receipt = await adapter.notify('oc_synthetic', 'hello')
            assert receipt.success
            assert transport.calls == [('send', {'chat_id': 'oc_synthetic', 'text': 'hello'})]
            # Disabled startup schedules recovery without waiting for the database.
            await asyncio.wait_for(adapter._channel_outbox._recovery_task, 1)
            probe.assert_awaited_once_with('channel-lark_bot')
            assert adapter._channel_outbox._task is None
        finally:
            await adapter.stop()
        assert transport.closed

    asyncio.run(run())
