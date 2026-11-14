"""The controller fingerprints code once without stalling health checks."""

import asyncio
from collections import Counter
from pathlib import Path
import threading
import time
from unittest.mock import AsyncMock

import httpx
import pytest

from looper.agents.llmrec import ROUTE_FORMAT
from looper.agents.llmrec import control
from looper.db import system_settings


@pytest.fixture
def healthy_proxy(monkeypatch):
    code = control.code_sha256()
    state = {'pid': 1234, 'instance': 'owned', 'start_time': 'owned-start', 'port': 12345,
             'code_sha256': code, 'config_sha256': control.config_sha256(code, '')}
    health = {**state, 'route_format': ROUTE_FORMAT, 'features': {'translate': 1}}
    monkeypatch.setattr(control, '_code_hash', None, raising=False)
    monkeypatch.setattr(control.ProxyControl, '_state', lambda self: state)
    monkeypatch.setattr(control.ProxyControl, '_running', AsyncMock(return_value=True))
    monkeypatch.setattr(system_settings, 'get_settings', AsyncMock(return_value={}))
    client = httpx.AsyncClient
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kwargs: client(**kwargs,
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=health))))
    return health


def test_startup_and_repeated_health_checks_read_code_once_per_process(tmp_path, monkeypatch, healthy_proxy):
    reads = Counter()
    read_bytes = Path.read_bytes

    def counted_read(path):
        reads[path] += 1
        return read_bytes(path)

    monkeypatch.setattr(Path, 'read_bytes', counted_read)

    async def run():
        owners = [control.ProxyControl(tmp_path, '', enabled=True) for _ in range(2)]
        await asyncio.gather(*(owner.at_start([]) for owner in owners))
        startup_reads = reads.copy()
        for owner in owners:
            for _ in range(3):
                assert await owner._health() == healthy_proxy
                assert not owner._outdated
        assert reads == startup_reads

    asyncio.run(run())
    root = Path(control.__file__).resolve().parents[2]
    files = set((root / 'agents' / 'llmrec').glob('*.py')) | {
        root / 'db' / 'llm_requests.py', root / 'db' / 'gateway.py'}
    assert reads == Counter({path: 1 for path in files})


def test_slow_code_reads_leave_loop_ticking_during_startup_and_health(tmp_path, monkeypatch, healthy_proxy):
    read_bytes = Path.read_bytes
    read_threads = set()

    def slow_read(path):
        read_threads.add(threading.get_ident())
        time.sleep(.5)
        return read_bytes(path)

    monkeypatch.setattr(Path, 'read_bytes', slow_read)

    async def run():
        ticks = []
        done = asyncio.Event()

        async def ticker():
            while not done.is_set():
                ticks.append(time.monotonic())
                await asyncio.sleep(.05)
            ticks.append(time.monotonic())

        task = asyncio.create_task(ticker())
        await asyncio.sleep(0)
        try:
            owner = control.ProxyControl(tmp_path, '', enabled=True)
            await owner.at_start([])
            for _ in range(3):
                assert await owner._health() == healthy_proxy
        finally:
            done.set()
            await task
        assert len(ticks) >= 5
        assert max(b - a for a, b in zip(ticks, ticks[1:])) < .25
        assert read_threads and threading.get_ident() not in read_threads

    asyncio.run(asyncio.wait_for(run(), 30))


def test_different_proxy_hash_is_outdated_and_replaced(tmp_path, monkeypatch, healthy_proxy):
    async def run():
        owner = control.ProxyControl(tmp_path, '', enabled=True)
        await owner.at_start([])
        healthy_proxy['code_sha256'] = 'different-code'
        assert await owner._health() == healthy_proxy
        assert owner._outdated
        stopped = AsyncMock()
        monkeypatch.setattr(owner, '_stop', stopped)
        monkeypatch.setattr(asyncio, 'create_subprocess_exec', AsyncMock(side_effect=OSError('no spawn')))
        with pytest.raises(OSError, match='no spawn'):
            await owner._start()
        stopped.assert_awaited_once_with(owner._state())

    asyncio.run(run())
