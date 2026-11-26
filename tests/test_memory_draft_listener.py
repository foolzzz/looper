"""A failed side connection of the memory drafter never stops the service."""

import asyncio
import sys
import time

import asyncpg
import pytest

from evals.prompt_regression import runner
from looper.agents import memory_draft_service
from tests.db_support import DSN, fresh_database_without_recording


class Listener:
    def __init__(self):
        self.closed, self.channels = False, []

    async def add_listener(self, channel, callback):
        self.channels.append(channel)

    def is_closed(self):
        return self.closed

    async def close(self):
        self.closed = True


def drafter(monkeypatch, connect):
    monkeypatch.setattr(memory_draft_service.asyncpg, "connect", connect)
    monkeypatch.setattr(memory_draft_service, "CONNECT_RETRY_SECONDS", 0)
    monkeypatch.setattr(memory_draft_service, "TICK_SECONDS", .01)
    service = memory_draft_service.MemoryDraftService(None)

    async def idle():
        return False

    monkeypatch.setattr(service, "run_once", idle)
    return service


def test_a_failed_listener_connection_degrades_to_polling_and_recovers(monkeypatch, tmp_path):
    calls, listener = [], Listener()

    async def connect(url, **kwargs):
        calls.append(url)
        # The certificate probe's stale errno, then a real outage, then recovery.
        if len(calls) <= memory_draft_service.CONNECT_ATTEMPTS:
            raise OSError(22, "Invalid argument")
        return listener

    async def check():
        service = drafter(monkeypatch, connect)
        await service.start("postgresql://synthetic/db")  # must not raise
        assert service._task is not None and not service._task.done()
        assert service._listener is None and len(calls) == memory_draft_service.CONNECT_ATTEMPTS
        deadline = time.monotonic() + 5
        while service._listener is None:  # the loop reattaches on a later tick
            assert time.monotonic() < deadline
            await asyncio.sleep(.01)
        assert listener.channels == [memory_draft_service._WAKE]
        await service.stop()
        assert listener.closed

    asyncio.run(check())


def test_cancellation_during_connect_still_propagates(monkeypatch):
    async def connect(url, **kwargs):
        raise asyncio.CancelledError

    async def check():
        service = drafter(monkeypatch, connect)
        with pytest.raises(asyncio.CancelledError):
            await service.start("postgresql://synthetic/db")

    asyncio.run(check())


@pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")
def test_serve_stays_up_when_the_drafter_connection_fails(monkeypatch, tmp_path):
    # Only the drafter's own asyncpg.connect fails; the pool and the control-plane lock connect normally.
    monkeypatch.setattr(runner, "_SERVE", (
        "import sys, asyncpg\n"
        "from pathlib import Path\n"
        "real = asyncpg.connect\n"
        "async def connect(*a, **k):\n"
        "    if sys._getframe(1).f_globals['__name__'].endswith('memory_draft_service'):\n"
        "        raise OSError(22, 'Invalid argument')\n"
        "    return await real(*a, **k)\n"
        "asyncpg.connect = connect\n"
        "from looper.agents.serve import serve_agent\n"
        "serve_agent(host='127.0.0.1', port=int(sys.argv[1]), runtime_root=Path(sys.argv[2]), work_dir=Path(sys.argv[3]))\n"))
    runtime, home, binaries = (tmp_path / name for name in ("runtime", "home", "bin"))
    home.mkdir()
    binaries.mkdir()
    log = tmp_path / "serve.log"
    with fresh_database_without_recording(str(DSN)) as (url, _):
        with runner.running_server(database_url=url, runtime_root=runtime, bin_dir=binaries, home=home,
                                   notes_root=home / "notes", log_path=log) as api:
            deadline = time.monotonic() + 30
            while "agent system running" not in log.read_text(encoding="utf-8", errors="replace"):
                assert time.monotonic() < deadline, log.read_text()[-2000:]
                time.sleep(.1)
            time.sleep(.5)
            assert api.post("/api/v1/system/status", {})["result"]
        text = log.read_text(encoding="utf-8", errors="replace")
        assert "memory drafter listener unavailable" in text


def test_a_failed_main_pool_still_fails_startup_with_its_own_error(monkeypatch):
    from looper.db import connection

    async def refuse(*args, **kwargs):
        raise ConnectionRefusedError("pool")

    monkeypatch.setattr(connection.asyncpg, "create_pool", refuse)
    with pytest.raises(ConnectionRefusedError, match="pool"):
        asyncio.run(connection.init_pool("postgresql://127.0.0.1:1/none"))
