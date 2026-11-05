"""Request persistence, deduplication, readback and one-time schema admission."""

import asyncio
from datetime import datetime, timezone, timedelta
import hashlib
import json
from types import SimpleNamespace

import pytest

from looper.db import connection, llm_records, llm_requests
from tests.db_support import DSN, fresh_connection, run_db

pytestmark = pytest.mark.skipif(not DSN, reason="requires disposable LOOPER_TEST_DATABASE_URL")


def capture(*, seq=1, record_id=None, after_record_id=None, route_id="a" * 22):
    from looper.agents.llmrec.capture import split_request

    now = datetime.now(timezone.utc)
    request = {"model": "test", "system": [{"type": "text", "text": "system"}],
               "tools": [{"name": "read", "input_schema": {"type": "object"}}],
               "messages": [{"role": "user", "content": "hello"}], "stream": True}
    raw = json.dumps(request).encode()
    skeleton, parts = split_request(request, "/v1/messages")
    route = SimpleNamespace(id=route_id, upstream="https://user:never-store@model.invalid/base?key=never-store",
        protocol="anthropic-messages", runtime_type="claude-code", provider_id="test-provider", model="test",
        agent_id="worker-test", task_id="task-test", step_id="step-test", role="worker", created_at=now)
    return SimpleNamespace(route=route, instance="test-instance", seq=seq, record_id=record_id,
        after_record_id=after_record_id, started_at=now, method="POST", path="/v1/messages", kind="model",
        model="test", stream=True, status=200, outcome="completed", error=None, ttfb_ms=1, duration_ms=2,
        request_headers={"authorization": "<redacted>"}, response_headers={"content-type": "text/event-stream"},
        skeleton=skeleton, parts=parts, request_bytes=len(raw), request_sha256=hashlib.sha256(raw).hexdigest(),
        response_body={"type": "message", "content": []}, response_raw=None, response_bytes=12,
        input_tokens=3, output_tokens=4, cache_read_tokens=5, cache_write_tokens=6, usage={"input_tokens": 3},
        record_gap=None, original=request)


def test_upgrade_once_and_fresh_schema_parity():
    from tests.bridge_schema_support import structure, witness

    async def body():
        async with fresh_connection() as old, fresh_connection() as fresh:
            await connection.ensure_schema(old)
            await old.execute("DROP TABLE llm_requests, llm_request_parts, llm_request_routes")
            before, shape = await witness(old), await structure(old)
            assert await connection.schema_change_pending(old)
            assert await witness(old) == before and await structure(old) == shape
            await connection.ensure_schema(old)
            await connection.ensure_schema(fresh)
            assert await structure(old) == await structure(fresh)
            after, shape = await witness(old), await structure(old)
            assert not await connection.schema_change_pending(old)
            await connection.ensure_schema(old)
            assert await witness(old) == after and await structure(old) == shape
            assert await old.fetchval("SELECT count(*) FROM schema_migrations WHERE name='llm_requests'") == 1
            assert not await fresh.fetchval("SELECT EXISTS (SELECT 1 FROM schema_migrations WHERE name='llm_requests')")
            # A recorded migration is never replayed as a DDL repair.
            await old.execute("DROP TABLE llm_requests")
            assert not await connection.schema_change_pending(old)
    asyncio.run(asyncio.wait_for(body(), 30))


def test_batch_idempotence_parts_and_full_request_readback():
    async def body(pool):
        record_id = await llm_records.start_record(agent_id="worker-test", pool=pool)
        first = capture(record_id=record_id)
        second = capture(seq=2, record_id=record_id)
        second.started_at = first.started_at - timedelta(seconds=1)
        await llm_requests.write_batch([first], pool=pool)
        await llm_requests.write_batch([first, second], pool=pool)
        assert await pool.fetchval("SELECT count(*) FROM llm_requests") == 2
        assert await pool.fetchval("SELECT count(*) FROM llm_request_parts") == len(first.parts)
        route = await pool.fetchrow("SELECT * FROM llm_request_routes")
        assert route["upstream_host"] == "model.invalid" and route["upstream_path"] == "/base"
        assert route["last_request_at"] == first.started_at
        rows = await llm_requests.list_requests(record_id=record_id, pool=pool)
        assert len(rows) == 2
        assert all(not {"request_body", "response_body", "response_raw"} & row.keys() for row in rows)
        full = await llm_requests.get_request(rows[0]["id"], pool=pool)
        assert full["request_body"] == first.original
        assert full["usage"] == {"input_tokens": 3}
        assert full["record_id"] == record_id
        assert await llm_requests.get_request(-1, pool=pool) is None
    run_db(body, timeout=30)


def test_missing_record_and_between_turn_filters():
    async def body(pool):
        record_id = await llm_records.start_record(agent_id="worker-test", pool=pool)
        active = capture(record_id=record_id)
        between = capture(seq=2, after_record_id=record_id)
        missing = capture(seq=3, record_id=record_id + 1000, route_id="b" * 22)
        missing.route.agent_id = "worker-other"
        missing.route.task_id = "task-other"
        await llm_requests.write_batch([active, between, missing], pool=pool)
        rows = await llm_requests.list_requests(pool=pool)
        assert len(rows) == 3 and rows[0]["record_id"] is None
        assert len(await llm_requests.list_requests(record_id=record_id, pool=pool)) == 1
        extra = await llm_requests.list_requests(record_id=record_id, between_turns=True, pool=pool)
        assert len(extra) == 1 and extra[0]["proxy_seq"] == 2 and extra[0]["record_id"] is None
        assert len(await llm_requests.list_requests(agent_id="worker-test", pool=pool)) == 2
        assert len(await llm_requests.list_requests(task_id="task-test", pool=pool)) == 2
        assert len(await llm_requests.list_requests(route_id="b" * 22, pool=pool)) == 1
        assert len(await llm_requests.list_requests(before_id=rows[0]["id"], limit=1, pool=pool)) == 1
        with pytest.raises(ValueError, match="requires record_id"):
            await llm_requests.list_requests(between_turns=True, pool=pool)
    run_db(body, timeout=30)


def test_writer_retries_same_batch_and_accounts_for_inflight_bytes(monkeypatch):
    from looper.agents.llmrec.writer import DbSink

    async def body():
        sink = DbSink(object(), max_pending_bytes=10)
        item = SimpleNamespace(size=lambda: 6)
        attempts, entered, proceed = [], asyncio.Event(), asyncio.Event()

        async def write(batch, *, pool):
            attempts.append(batch)
            entered.set()
            await proceed.wait()
            if len(attempts) == 1:
                raise ConnectionError("unavailable")

        monkeypatch.setattr(llm_requests, "write_batch", write)
        assert sink.offer(item)
        task = asyncio.create_task(sink.run())
        try:
            await asyncio.wait_for(entered.wait(), 2)
            assert sink.pending == 1
            assert not sink.offer(item)
            proceed.set()
            await sink.close(timeout=4)
            assert attempts == [[item], [item]]
            assert sink.pending == 0 and sink.recorded == 1 and sink.write_errors == 1
            assert not sink.offer(item)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    asyncio.run(asyncio.wait_for(body(), 7))
