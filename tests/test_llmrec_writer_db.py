"""Writer failures must be visible to the proxy controller."""

import asyncio
from datetime import datetime, timezone

import asyncpg
import httpx
import pytest

from looper.agents.llmrec import RouteFile
from looper.agents.llmrec.capture import Capture
from looper.agents.llmrec.proxy import Proxy
from looper.agents.llmrec.writer import DbSink
from tests.db_support import DSN, run_db

pytestmark = pytest.mark.skipif(not DSN, reason="requires disposable LOOPER_TEST_DATABASE_URL")


def test_health_reports_writer_stopped_by_database_error(tmp_path):
    async def body(pool):
        # Constraint failures are not transient connection errors and stop the writer.
        await pool.execute("""
            ALTER TABLE llm_requests ADD CONSTRAINT reject_capture CHECK (proxy_seq <> 1)
        """)
        item = Capture(route=RouteFile(id="a" * 22, upstream="https://model.invalid",
            agent_id="worker-test", created_at=datetime.now(timezone.utc).isoformat()),
            instance="writer-health", seq=1, path="/v1/messages")
        sink = DbSink(pool)
        server = Proxy(tmp_path, sink=sink)
        await server.start()
        task = asyncio.create_task(sink.run())
        try:
            async with httpx.AsyncClient(trust_env=False, timeout=2) as client:
                health_url = f"http://127.0.0.1:{server.port}/llmrec/health"
                assert (await client.get(health_url)).status_code == 200
                assert sink.offer(item)
                with pytest.raises(asyncpg.CheckViolationError):
                    await asyncio.wait_for(task, 3)
                assert sink.write_errors == 1
                assert await pool.fetchval("SELECT count(*) FROM llm_requests") == 0
                failed = await client.get(health_url)
                assert failed.status_code == 503
                assert failed.json()["writer_healthy"] is False
                # Captures cannot accumulate behind a writer that has stopped.
                assert sink.offer(item) is False
                assert sink.pending == 1
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await sink.close(timeout=1)
            await server.close(grace_s=1)

    run_db(body, timeout=10)
