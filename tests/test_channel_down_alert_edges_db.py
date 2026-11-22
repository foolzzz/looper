"""Outage intervals follow recoveries across delivery sources and channel enable cycles."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from looper.agents.channels.base import DeliveryReceipt
from looper.agents.channels.down_alerts import Monitor
from looper.agents.channels.registry import ChannelRegistry
from looper.db import channel_configs
from tests.db_support import run_with_channel_configs


async def configured(pool):
    for kind, operator, allowlist in (('lark', 'ou_synthetic00001', 'allowed_open_ids'),
                                      ('telegram', '1000001', 'allowed_user_ids')):
        await channel_configs.set_config(f'channel-{kind}', {'enabled': True,
            'config': {allowlist: [operator], 'operator_sender_id': operator}}, pool=pool)
    registry = ChannelRegistry(SimpleNamespace())
    for kind in ('lark', 'telegram'):
        registry._channels[f'channel-{kind}'] = SimpleNamespace(
            healthcheck=lambda: {'healthy': True}, notify=AsyncMock(return_value=DeliveryReceipt(kind, True)))
    return registry


def test_success_between_checks_restarts_failure_deadline(monkeypatch):
    async def test(pool):
        registry = await configured(pool)
        clock = [datetime.now(timezone.utc)]
        monkeypatch.setattr(registry, '_delivery_clock', lambda: clock[0])
        monitor = Monitor(registry, clock=lambda: clock[0])
        registry.record_delivery('channel-lark', False)
        await monitor.tick()
        clock[0] += timedelta(minutes=5)
        registry.record_delivery('channel-lark', True)
        clock[0] += timedelta(minutes=1)
        registry.record_delivery('channel-lark', False)
        clock[0] += timedelta(minutes=5)
        await monitor.tick()
        assert registry.down_checks == []
        registry.get('channel-telegram').notify.assert_not_awaited()
        clock[0] += timedelta(minutes=6)
        await monitor.tick()
        assert len(registry.down_checks) == 1
        assert registry.get('channel-telegram').notify.await_count == 1

    run_with_channel_configs(test, monkeypatch)


def test_disable_then_enable_starts_a_new_outage(monkeypatch):
    async def test(pool):
        registry = await configured(pool)
        registry._channels.pop('channel-lark')
        clock = [datetime.now(timezone.utc)]
        monitor = Monitor(registry, clock=lambda: clock[0])
        await monitor.tick()
        clock[0] += timedelta(minutes=11)
        await monitor.tick()
        assert registry.get('channel-telegram').notify.await_count == 1
        await channel_configs.set_config('channel-lark', {'enabled': False}, pool=pool)
        await monitor.tick()
        assert registry.down_checks == []
        clock[0] += timedelta(hours=1)
        await channel_configs.set_config('channel-lark', {'enabled': True}, pool=pool)
        await monitor.tick()
        assert registry.down_checks == []
        assert registry.get('channel-telegram').notify.await_count == 1
        clock[0] += timedelta(minutes=11)
        await monitor.tick()
        assert len(registry.down_checks) == 1
        assert registry.get('channel-telegram').notify.await_count == 2

    run_with_channel_configs(test, monkeypatch)


def test_receipt_failure_after_database_success_is_not_lost(monkeypatch):
    async def test(pool):
        registry = await configured(pool)
        clock = [datetime.now(timezone.utc)]
        monkeypatch.setattr(registry, '_delivery_clock', lambda: clock[0])
        monitor = Monitor(registry, clock=lambda: clock[0])
        registry.record_delivery('channel-lark', False)
        await monitor.tick()
        await pool.execute("INSERT INTO projects (id, name) VALUES ('edge-project', 'Synthetic')")
        await pool.execute("INSERT INTO schedules (id, project_id, name, cron, timezone, requirement) "
                           "VALUES ('edge-schedule', 'edge-project', 'Synthetic', '*/5 * * * *', 'UTC', 'Synthetic')")
        clock[0] += timedelta(minutes=1)
        await pool.execute("INSERT INTO schedule_notifications "
            "(id, schedule_id, kind, channel, text, status, created_at) "
            "VALUES ('edge-notice', 'edge-schedule', 'run', 'lark', 'synthetic', 'sent', $1)", clock[0])
        clock[0] += timedelta(minutes=1)
        registry.record_delivery('channel-lark', False)
        await monitor.tick()
        assert registry.down_checks == []
        clock[0] += timedelta(minutes=11)
        await monitor.tick()
        assert len(registry.down_checks) == 1
        assert registry.get('channel-telegram').notify.await_count == 1

    run_with_channel_configs(test, monkeypatch)


@pytest.mark.parametrize('raises', [False, True])
def test_alternate_failure_does_not_block_remaining_operator_paths(monkeypatch, raises):
    async def test(pool):
        registry = await configured(pool)
        registry._channels.pop('channel-lark')
        await channel_configs.set_config('channel-lark_bot', {'enabled': True,
            'config': {'app_id': 'cli_synthetic', 'allowed_open_ids': ['ou_synthetic00002'],
                       'operator_sender_id': 'ou_synthetic00002'}}, pool=pool)
        third = SimpleNamespace(healthcheck=lambda: {'healthy': True},
                                notify=AsyncMock(return_value=DeliveryReceipt('lark_bot', True)))
        registry._channels['channel-lark_bot'] = third
        telegram = registry.get('channel-telegram')
        telegram.notify.side_effect = RuntimeError('synthetic transport failure') if raises else None
        telegram.notify.return_value = DeliveryReceipt('telegram', False, error='synthetic failure')
        clock = [datetime.now(timezone.utc)]
        monkeypatch.setattr(registry, '_delivery_clock', lambda: clock[0])
        monitor = Monitor(registry, clock=lambda: clock[0])
        await monitor.tick()
        clock[0] += timedelta(minutes=11)
        await monitor.tick()
        assert third.notify.await_count == 1
        await monitor.tick()
        assert third.notify.await_count == 1
        assert telegram.notify.await_count == 1

    run_with_channel_configs(test, monkeypatch)


def test_receipt_success_before_first_check_keeps_old_database_failure_cleared(monkeypatch):
    async def test(pool):
        registry = await configured(pool)
        clock = [datetime.now(timezone.utc)]
        monkeypatch.setattr(registry, '_delivery_clock', lambda: clock[0])
        await pool.execute("INSERT INTO projects (id, name) VALUES ('edge-project', 'Synthetic')")
        await pool.execute("INSERT INTO schedules (id, project_id, name, cron, timezone, requirement) "
                           "VALUES ('edge-schedule', 'edge-project', 'Synthetic', '*/5 * * * *', 'UTC', 'Synthetic')")
        await pool.execute("INSERT INTO schedule_notifications "
            "(id, schedule_id, kind, channel, text, status, created_at) "
            "VALUES ('edge-notice', 'edge-schedule', 'run', 'lark', 'synthetic', 'failed', $1)", clock[0])
        clock[0] += timedelta(minutes=1)
        registry.record_delivery('channel-lark', True)
        monitor = Monitor(registry, clock=lambda: clock[0])
        await monitor.tick()
        clock[0] += timedelta(minutes=11)
        await monitor.tick()
        assert registry.down_checks == []
        registry.get('channel-telegram').notify.assert_not_awaited()

    run_with_channel_configs(test, monkeypatch)
