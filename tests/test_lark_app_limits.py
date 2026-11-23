"""Lark CLI and Bot channels spend one application's write allowance."""
from __future__ import annotations

import asyncio

import pytest

from looper.agents import chat_settings
from looper.agents.channels import outbound
from looper.agents.channels.allowlist import SenderAllowlist
from looper.agents.channels.lark import LarkChannelAdapter


def _channels(app_id: str = 'cli_shared_test'):
    return [LarkChannelAdapter(
        channel_type=kind, bot_app_id=app_id,
        allowlist=SenderAllowlist(f'channel-{kind}', frozenset({'ou_synthetic1234'})),
    ) for kind in ('lark', 'lark_bot')]


def test_cli_and_bot_of_same_app_share_in_flight_cap(monkeypatch):
    monkeypatch.setattr(chat_settings, 'outbound_limits', lambda: dict(
        send_qps=100, write_qps=100, edit_interval_seconds=1, max_in_flight=4))

    async def run():
        channels = _channels()
        active = peak = 0

        async def write():
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(.01)
            active -= 1

        await asyncio.gather(*(channels[i % 2]._gateway.write('send', write) for i in range(20)))
        assert peak <= 4
        assert peak > 1

    asyncio.run(run())


@pytest.mark.parametrize(('kind', 'send_qps', 'write_qps'), [('send', 2, 100), ('react', 100, 2)])
def test_cli_and_bot_of_same_app_share_token_buckets(monkeypatch, kind, send_qps, write_qps):
    monkeypatch.setattr(chat_settings, 'outbound_limits', lambda: dict(
        send_qps=send_qps, write_qps=write_qps, edit_interval_seconds=1, max_in_flight=4))
    bucket_type = outbound.TokenBucket
    now = 0.0

    async def sleep(seconds):
        nonlocal now
        now += seconds
        await asyncio.sleep(0)

    def bucket(rate, capacity, *args, **kwargs):
        return bucket_type(rate, capacity, lambda: now, sleep=sleep)

    monkeypatch.setattr(outbound, 'TokenBucket', bucket)

    async def run():
        channels = _channels(f'cli_bucket_{kind}_test')

        async def write():
            return True

        for i in range(4):
            await channels[i % 2]._gateway.write(kind, write)
        assert now == pytest.approx(1)

    asyncio.run(run())


def test_different_apps_have_independent_allowances(monkeypatch):
    monkeypatch.setattr(chat_settings, 'outbound_limits', lambda: dict(
        send_qps=100, write_qps=100, edit_interval_seconds=1, max_in_flight=1))

    async def run():
        gateways = [_channels(app)[0]._gateway for app in ('cli_first_test', 'cli_second_test')]
        active = peak = 0

        async def write():
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(.01)
            active -= 1

        await asyncio.gather(*(gateway.write('send', write) for gateway in gateways))
        assert peak == 2

    asyncio.run(run())


@pytest.mark.parametrize('reuse_gateway', [False, True])
def test_app_allowances_are_local_to_each_event_loop(reuse_gateway):
    def limits():
        return dict(send_qps=100, write_qps=100, edit_interval_seconds=0, max_in_flight=1)

    # Construction may happen before a loop starts; keep old gateways alive deliberately.
    original = outbound.Gateway('cli', settings=limits, app_id='cli_loop_test')
    retained = [original]

    async def run():
        gateway = original if reuse_gateway else outbound.Gateway(
            'cli', settings=limits, app_id='cli_loop_test')
        sibling = outbound.Gateway('bot', settings=limits, app_id='cli_loop_test')
        retained.extend((gateway, sibling))
        entered, release, queued = asyncio.Event(), asyncio.Event(), asyncio.Event()
        calls = []

        async def hold():
            calls.append('first')
            entered.set()
            await release.wait()

        async def follow():
            calls.append('second')

        async def waiting_write():
            queued.set()
            await sibling.write('send', follow)

        first = asyncio.create_task(gateway.write('send', hold))
        second = None
        try:
            await asyncio.wait_for(entered.wait(), 1)
            second = asyncio.create_task(waiting_write())
            await asyncio.wait_for(queued.wait(), 1)
            assert calls == ['first']
            release.set()
            await asyncio.wait_for(asyncio.gather(first, second), 1)
            assert calls == ['first', 'second']
        finally:
            release.set()
            for task in (first, second):
                if task is not None:
                    task.cancel()
            await asyncio.gather(*(task for task in (first, second) if task is not None), return_exceptions=True)

    asyncio.run(run())
    asyncio.run(run())
