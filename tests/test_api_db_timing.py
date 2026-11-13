"""Query timings follow the requesting context and survive pooled connection reuse."""

import asyncio
import logging
import re
from unittest.mock import AsyncMock
from types import SimpleNamespace

import pytest

from looper.db import connection
from looper.db.timing import ApiTiming, current_api_timing, record_query
from tests.db_support import DSN


def test_query_callback_uses_the_context_copied_when_scheduled():
    async def run():
        loop = asyncio.get_running_loop()
        first, second = ApiTiming(), ApiTiming()
        token = current_api_timing.set(first)
        loop.call_soon(record_query, SimpleNamespace(elapsed=0.125))
        current_api_timing.set(second)
        loop.call_soon(record_query, SimpleNamespace(elapsed=0.025))
        current_api_timing.reset(token)
        loop.call_soon(record_query, SimpleNamespace(elapsed=0.500))
        await asyncio.sleep(0)
        assert (first.db_ms, first.db_queries) == (125, 1)
        assert (second.db_ms, second.db_queries) == (25, 1)
        assert current_api_timing.get() is None

    asyncio.run(run())


@pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")
def test_pool_query_logger_is_registered_once_and_attributes_real_queries(monkeypatch):
    async def run():
        # Only this pool is owned by the test; avoid replacing another fixture's pool.
        monkeypatch.setattr(connection, "_pool", None)
        pool = await connection.init_pool(DSN, min_size=1, max_size=2)
        try:
            async def request():
                timing = ApiTiming()
                token = current_api_timing.set(timing)
                try:
                    async with pool.acquire() as conn:
                        await conn.execute("SELECT pg_sleep(0.1)")
                        await asyncio.sleep(0)
                        assert timing.db_queries == 1
                    await asyncio.sleep(0)
                    return timing
                finally:
                    current_api_timing.reset(token)

            # Concurrent requests establish every pool connection through init=.
            first, second = await asyncio.gather(request(), request())
            assert all(timing.db_ms >= 100 and timing.db_queries == 2 for timing in (first, second))
            # Reusing connections must not add another logger or retain the old context.
            again = await request()
            assert again.db_ms >= 100 and again.db_queries == 2
            assert first.db_queries == second.db_queries == 2
            assert current_api_timing.get() is None
        finally:
            await connection.close_pool()

    asyncio.run(asyncio.wait_for(run(), timeout=10))


@pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")
def test_slow_handler_logs_elapsed_postgres_query_time(monkeypatch, caplog):
    from looper.agents import api_v1, http_server

    async def run():
        monkeypatch.setattr(connection, "_pool", None)
        pool = await connection.init_pool(DSN, min_size=1, max_size=1)
        server = http_server.AgentHTTPServer(web_adapter=None, task_router=None, supervisor=None)
        writer = SimpleNamespace(write=lambda _: None, drain=AsyncMock())

        async def answer(*args):
            async with pool.acquire() as conn:
                await conn.execute("SELECT pg_sleep(0.1)")
            return api_v1.OK, {"status": "ok", "result": {}}

        monkeypatch.setattr(server, "_answer_v1", answer)
        monkeypatch.setattr(http_server, "API_SLOW_SECONDS", 0.05)
        try:
            await server._handle_v1("GET", "/api/v1/whoami", {}, {}, b"", writer, api_v1.OPERATOR)
            assert current_api_timing.get() is None
        finally:
            await server.stop()
            await connection.close_pool()

    with caplog.at_level(logging.INFO):
        asyncio.run(asyncio.wait_for(run(), timeout=10))
    slow = [record.getMessage() for record in caplog.records if "api slow " in record.getMessage()]
    assert len(slow) == 1
    fields = re.search(r"db_ms=(\d+) db_queries=(\d+)", slow[0])
    assert fields and int(fields[1]) >= 100 and int(fields[2]) >= 1
