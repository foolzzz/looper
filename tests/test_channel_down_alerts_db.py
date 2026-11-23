"""Channel outages use independent operator paths and actual delivery receipts."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from looper.agents.channels.base import DeliveryReceipt, PostReceipt
from looper.agents.channels.registry import ChannelRegistry
from looper.agents.http_server import AgentHTTPServer
from looper.db import channel_configs
from tests.db_support import run_with_channel_configs


def test_outage_banner_alternate_alert_and_recovery_survive_restart(monkeypatch):
    from looper.agents.channels.down_alerts import Monitor

    async def test(pool):
        clock = [datetime.now(timezone.utc)]
        await configure(pool, telegram=True)
        registry, telegram = running_registry()
        monitor = Monitor(registry, clock=lambda: clock[0])
        server = SimpleNamespace(_channels=registry, _self_checks=[], _worker_pool=None)
        await monitor.tick()
        clock[0] += timedelta(minutes=10)
        await monitor.tick()
        assert not telegram.notify.await_args_list
        clock[0] += timedelta(minutes=1)
        await monitor.tick()
        checks = await AgentHTTPServer._checks_now(server)
        assert len([c for c in checks if c['id'] == 'channel_down:channel-lark']) == 1
        assert telegram.notify.await_count == 1
        assert telegram.notify.call_args.args[0] == '1000001'
        assert 'channel-lark' in telegram.notify.call_args.args[1]
        await monitor.tick()
        monitor = Monitor(registry, clock=lambda: clock[0])
        await monitor.tick()
        assert telegram.notify.await_count == 1
        registry._channels['channel-lark'] = adapter('lark')
        await monitor.tick()
        await monitor.tick()
        assert await AgentHTTPServer._checks_now(server) == []
        assert telegram.notify.await_count == 2
        assert '恢复' in telegram.notify.call_args.args[1]
        assert not registry.get('channel-lark').notify.await_args_list
        # A new outage has its own allowance.
        registry._channels.pop('channel-lark')
        await monitor.tick()
        clock[0] += timedelta(minutes=11)
        await monitor.tick()
        assert telegram.notify.await_count == 3

    run_with_channel_configs(test, monkeypatch)


@pytest.mark.parametrize('telegram', [False, True])
def test_only_other_enabled_running_operator_channels_receive_alerts(monkeypatch, telegram):
    from looper.agents.channels.down_alerts import Monitor

    async def test(pool):
        clock = [datetime.now(timezone.utc)]
        await configure(pool, telegram=telegram)
        registry, other = running_registry()
        # Even an enabled/running bot without an operator cannot receive an alert.
        if telegram:
            await channel_configs.set_config('channel-telegram', {'config': {'allowed_user_ids': ['1000001']}}, pool=pool)
        monitor = Monitor(registry, clock=lambda: clock[0])
        await monitor.tick()
        clock[0] += timedelta(minutes=11)
        await monitor.tick()
        assert any(c['id'] == 'channel_down:channel-lark' for c in registry.start_checks())
        other.notify.assert_not_awaited()
        await channel_configs.set_config('channel-lark', {'enabled': False}, pool=pool)
        await monitor.tick()
        assert registry.start_checks() == []
        other.notify.assert_not_awaited()

    run_with_channel_configs(test, monkeypatch)


@pytest.mark.parametrize('source', ['notification', 'receipt'])
def test_all_failed_delivery_streak_resets_on_success(monkeypatch, source):
    from looper.agents.channels.down_alerts import Monitor

    async def test(pool):
        clock = [datetime.now(timezone.utc)]
        await configure(pool, telegram=True)
        registry, telegram = running_registry()
        lark = adapter('lark')
        registry._channels['channel-lark'] = lark
        monitor = Monitor(registry, clock=lambda: clock[0])
        monkeypatch.setattr(registry, '_delivery_clock', lambda: clock[0])

        async def outcome(success):
            if source == 'receipt':
                lark.notify.return_value = DeliveryReceipt('lark', success)
                await registry.notify('channel-lark', 'ou_synthetic00001', 'synthetic notice')
            else:
                # Real schedule_notifications rows, including the legacy channel-type spelling.
                await pool.execute("INSERT INTO schedule_notifications "
                    "(id, schedule_id, kind, channel, text, status, created_at) "
                    "VALUES ($1, 'schedule-test', 'run', 'lark', 'synthetic', $2, $3)",
                    str(clock[0].timestamp()), 'sent' if success else 'failed', clock[0])

        if source == 'notification':
            await pool.execute("INSERT INTO projects (id, name) VALUES ('project-test', 'Synthetic')")
            await pool.execute("INSERT INTO schedules (id, project_id, name, cron, timezone, requirement) "
                               "VALUES ('schedule-test', 'project-test', 'Synthetic', '*/5 * * * *', 'UTC', 'Synthetic')")
        await outcome(False)
        await monitor.tick()
        clock[0] += timedelta(minutes=11)
        await monitor.tick()
        assert telegram.notify.await_count == 1
        assert any(c['id'] == 'channel_down:channel-lark' for c in registry.start_checks())
        clock[0] += timedelta(seconds=1)
        await outcome(True)
        await monitor.tick()
        assert telegram.notify.await_count == 2
        assert registry.start_checks() == []
        clock[0] += timedelta(seconds=1)
        await outcome(False)
        await monitor.tick()
        assert registry.start_checks() == []
        clock[0] += timedelta(minutes=9)
        await outcome(True)
        clock[0] += timedelta(minutes=20)
        await monitor.tick()
        assert registry.start_checks() == []
        assert telegram.notify.await_count == 2

    run_with_channel_configs(test, monkeypatch)


def test_healthy_channels_unchanged_for_default_and_custom_threshold(monkeypatch):
    from looper.agents.channels import down_alert_settings
    from looper.agents.channels.down_alerts import Monitor

    async def test(pool):
        await configure(pool, telegram=True)
        registry, telegram = running_registry()
        registry._channels['channel-lark'] = adapter('lark')
        before = repr(registry.list_channels()), repr(registry.start_checks())
        for minutes in (10, 60):
            monkeypatch.setattr(down_alert_settings, 'minutes', lambda: minutes)
            await Monitor(registry).tick()
            assert (repr(registry.list_channels()), repr(registry.start_checks())) == before
        telegram.notify.assert_not_awaited()
        assert not await pool.fetchval("SELECT count(*) FROM system_settings WHERE key LIKE 'channels.down_state%'")

    run_with_channel_configs(test, monkeypatch)


async def configure(pool, *, telegram):
    await channel_configs.set_config('channel-lark', {'enabled': True,
        'config': {'allowed_open_ids': ['ou_synthetic00001'], 'operator_sender_id': 'ou_synthetic00001'}}, pool=pool)
    await channel_configs.set_config('channel-telegram', {'enabled': telegram,
        'config': {'allowed_user_ids': ['1000001'], 'operator_sender_id': '1000001'}}, pool=pool)


def adapter(kind):
    return SimpleNamespace(channel_id=f'channel-{kind}', channel_type=kind,
        healthcheck=lambda: {'channel': kind, 'healthy': True},
        notify=AsyncMock(return_value=DeliveryReceipt(kind, True)),
        post=AsyncMock(return_value=PostReceipt(True)))


def running_registry():
    registry = ChannelRegistry(SimpleNamespace())
    telegram = adapter('telegram')
    registry._channels['channel-telegram'] = telegram
    return registry, telegram
