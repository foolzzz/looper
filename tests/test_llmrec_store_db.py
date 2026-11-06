"""Request persistence, deduplication, readback and one-time schema admission."""

import asyncio
from datetime import datetime, timezone, timedelta
import hashlib
import json
from types import SimpleNamespace

import asyncpg
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
        record_gap=None, translation=None, original=request)


@pytest.mark.parametrize("raw_response", [False, True])
def test_writer_stores_nul_bodies_with_original_sizes(raw_response):
    from looper.agents.llmrec.capture import build_capture, canonical
    from looper.agents.llmrec.writer import DbSink

    async def body(pool):
        request = {"model": "test", "messages": [{"role": "user", "content": "a\0b\\u0000"}],
                   "extra": {"text": "c\0d"}}
        response = {"content": [{"text": "e\0f"}]}
        raw_request = canonical(request)
        response_data = b"e\0f" if raw_response else canonical(response)
        item = build_capture(route=capture().route, instance="nul", seq=1, path="/v1/messages",
            request_body=raw_request, response_data=response_data, request_headers={},
            response_headers={"content-type": "application/json"}, response_bytes=len(response_data))
        sink = DbSink(pool)
        assert sink.offer(item)
        task = asyncio.create_task(sink.run())
        await asyncio.sleep(0)
        try:
            await sink.close(timeout=3)
            assert sink.pending == 0 and sink.recorded == int(not raw_response) and sink.write_errors == 0
            assert sink.gaps == int(raw_response)
            rows = await llm_requests.list_requests(pool=pool)
            assert len(rows) == 1 and rows[0]["body_sanitized"] is True
            full = await llm_requests.get_request(rows[0]["id"], pool=pool)
            assert full["request_body"]["messages"][0]["content"] == "a\\u0000b\\u0000"
            assert full["request_body"]["extra"]["text"] == "c\\u0000d"
            if raw_response:
                assert full["response_raw"] == "e\\u0000f"
                assert full["record_gap"] == "unparsable_stream"
            else:
                assert full["response_body"] == {"content": [{"text": "e\\u0000f"}]}
            assert full["request_bytes"] == len(raw_request)
            assert full["response_bytes"] == len(response_data)
            assert full["request_sha256"] == hashlib.sha256(raw_request).hexdigest()
            for sha, part in item.parts.items():
                assert await pool.fetchval("SELECT bytes FROM llm_request_parts WHERE sha256=$1", sha) == len(canonical(part))
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    run_db(body, timeout=10)


@pytest.mark.parametrize("sqlstate,error_class", [("22000", "DataError"),
    ("22P02", "InvalidTextRepresentationError"), ("22P05", "UntranslatableCharacterError"),
    ("22001", "StringDataRightTruncationError")])
def test_writer_isolates_rejected_row_and_processes_next_batch(monkeypatch, caplog, sqlstate, error_class):
    from looper.agents.llmrec.capture import Capture
    from looper.agents.llmrec.writer import DbSink

    async def body(pool):
        await pool.execute(f"""
            CREATE FUNCTION reject_body() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
                IF NEW.proxy_seq = 2 AND NEW.request_body IS NOT NULL THEN
                    RAISE EXCEPTION 'private body must not be logged' USING ERRCODE = '{sqlstate}';
                END IF;
                RETURN NEW;
            END $$;
            CREATE TRIGGER reject_body BEFORE INSERT ON llm_requests
                FOR EACH ROW EXECUTE FUNCTION reject_body();
        """)
        committed = asyncio.Event()
        write_batch = llm_requests.write_batch
        batches = []

        async def write(items, *, pool):
            await write_batch(items, pool=pool)
            batches.append([item.seq for item in items])
            committed.set()

        monkeypatch.setattr(llm_requests, "write_batch", write)
        sink = DbSink(pool)
        items = [Capture(**{key: value for key, value in vars(capture(seq=seq)).items() if key != "original"})
                 for seq in range(1, 5)]
        for item in items[:3]:
            assert sink.offer(item)
        task = asyncio.create_task(sink.run())
        try:
            await asyncio.wait_for(committed.wait(), 3)
            assert sink.offer(items[3])
            await sink.close(timeout=3)
            assert batches == [[1, 2, 3], [4]]
            assert sink.pending == 0 and sink.recorded == 3 and sink.gaps == 1
            assert sink.gap_reasons == {"rejected_by_database": 1}
            rows = await llm_requests.list_requests(pool=pool)
            assert {row["proxy_seq"] for row in rows} == {1, 2, 3, 4}
            rejected = next(row for row in rows if row["proxy_seq"] == 2)
            assert rejected["record_gap"] == "rejected_by_database" and rejected["error"] == error_class
            full = await llm_requests.get_request(rejected["id"], pool=pool)
            assert full["request_body"] is None and full["response_body"] is None and full["response_raw"] is None
            assert full["request_bytes"] == items[1].request_bytes
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    run_db(body, timeout=10)
    assert "private body must not be logged" not in caplog.text


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
        assert full["body_sanitized"] is False
        assert full["usage"] == {"input_tokens": 3}
        assert full["record_id"] == record_id
        assert await llm_requests.get_request(-1, pool=pool) is None
    run_db(body, timeout=30)


def test_request_readback_fetches_only_its_referenced_parts(monkeypatch):
    async def body(pool):
        first, later = capture(), capture(seq=2)
        later.skeleton, later.parts = {"messages": []}, {"f" * 64: {"text": "unrelated later turn"}}
        await llm_requests.write_batch([first, later], pool=pool)
        request_id = await pool.fetchval("SELECT id FROM llm_requests WHERE proxy_seq = 1")
        assert await pool.fetchval("SELECT count(*) FROM llm_request_parts") == len(first.parts) + 1

        fetched = []
        fetch = asyncpg.Connection.fetch

        async def observe(conn, query, *args, **kwargs):
            rows = await fetch(conn, query, *args, **kwargs)
            if "FROM llm_request_parts" in query:
                fetched.extend(row["sha256"] for row in rows)
            return rows

        monkeypatch.setattr(asyncpg.Connection, "fetch", observe)
        result = await llm_requests.get_request(request_id, pool=pool)
        assert result["request_body"] == first.original
        assert set(fetched) == set(first.parts)
        later_id = await pool.fetchval("SELECT id FROM llm_requests WHERE proxy_seq = 2")
        assert (await llm_requests.get_request(later_id, pool=pool))["request_body"] == {"messages": []}
        assert set(fetched) == set(first.parts)

    run_db(body, timeout=10)


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


@pytest.mark.parametrize("failure", [ConnectionError, asyncpg.ConnectionDoesNotExistError,
                                     asyncpg.SerializationError])
def test_writer_retries_same_batch_and_accounts_for_inflight_bytes(monkeypatch, failure):
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
                raise failure("unavailable")

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


def test_writer_does_not_retry_programming_errors(monkeypatch):
    from looper.agents.llmrec.capture import Capture
    from looper.agents.llmrec.writer import DbSink

    async def write(batch, *, pool):
        raise ValueError("invalid writer implementation")

    async def body():
        sink = DbSink(object())
        sink.offer(Capture())
        monkeypatch.setattr(llm_requests, "write_batch", write)
        with pytest.raises(ValueError, match="invalid writer implementation"):
            await sink.run()
        assert sink.write_errors == 1
    asyncio.run(asyncio.wait_for(body(), 2))


def test_translation_metadata_roundtrip():
    from looper.agents.llmrec.capture import Capture

    async def body(pool):
        item = Capture(**{key: value for key, value in vars(capture()).items() if key != "original"})
        item.translation = {"api": "openai-chat", "dialect": "deepseek", "upstream_model": "upstream",
                            "upstream_usage": {"prompt_cache_hit_tokens": 5}, "common_prefix_bytes": None,
                            "notes": {"dropped": {}, "degraded": {}, "clamped": {}}, "usage_missing": False}
        await llm_requests.write_batch([item], pool=pool)
        row = (await llm_requests.list_requests(pool=pool))[0]
        result = await llm_requests.get_request(row["id"], pool=pool)
        assert result["translation"] == item.translation == row["translation"]
        assert tuple(result[key] for key in ("input_tokens", "output_tokens", "cache_read_tokens",
                                             "cache_write_tokens")) == (3, 4, 5, 6)
    run_db(body, timeout=10)


def test_translation_column_upgrade_preserves_rows_and_runs_once():
    from tests.bridge_schema_support import structure, witness

    async def body():
        async with fresh_connection() as old, fresh_connection() as fresh:
            await connection.ensure_schema(old)
            await old.execute("ALTER TABLE llm_requests DROP COLUMN IF EXISTS translation")
            await old.execute("""
                INSERT INTO llm_request_routes (id, agent_id, runtime_type, upstream_host,
                                                 created_at, last_request_at)
                VALUES ('old-route', 'worker-test', 'claude-code', 'upstream.invalid', now(), now());
                INSERT INTO llm_requests (route_id, proxy_instance, proxy_seq, agent_id, started_at,
                                          method, path, kind, outcome, request_headers,
                                          request_bytes, request_sha256)
                VALUES ('old-route', 'previous', 1, 'worker-test', now(), 'POST', '/v1/messages',
                        'model', 'completed', '{}', 2, 'old-hash');
            """)
            previous = dict(await old.fetchrow("SELECT * FROM llm_requests"))
            before, shape = await witness(old), await structure(old)
            assert await connection.schema_change_pending(old)
            assert await witness(old) == before and await structure(old) == shape
            await connection.ensure_schema(old)
            await connection.ensure_schema(fresh)
            assert await structure(old) == await structure(fresh)
            assert dict(await old.fetchrow("SELECT * FROM llm_requests")) == previous | {"translation": None}
            assert await old.fetchval("SELECT count(*) FROM schema_migrations "
                                      "WHERE name='llm_requests_translation'") == 1
            after, shape = await witness(old), await structure(old)
            assert not await connection.schema_change_pending(old)
            await connection.ensure_schema(old)
            assert await witness(old) == after and await structure(old) == shape
            assert not await fresh.fetchval("SELECT EXISTS (SELECT 1 FROM schema_migrations "
                                           "WHERE name='llm_requests_translation')")
            await old.execute("ALTER TABLE llm_requests DROP COLUMN translation")
            assert not await connection.schema_change_pending(old)
    asyncio.run(asyncio.wait_for(body(), 30))


def test_recorded_bytes_count_inserted_utf8_and_deduplicated_parts():
    async def body(pool):
        first = capture(seq=1)
        first.response_raw = "答\0案"
        first.response_bytes = 2**30  # Wire sizes do not count toward retained content.
        await llm_requests.write_batch([first, first], pool=pool)
        row = await pool.fetchrow("SELECT * FROM llm_requests")
        expected = 0
        for name in llm_requests._COLUMNS:
            if name == "stored_bytes":
                continue
            value = row[name]
            if name in llm_requests._JSON_COLUMNS and value is not None:
                value = json.dumps(json.loads(value), ensure_ascii=False, separators=(",", ":"))
            if isinstance(value, str):
                expected += len(value.encode("utf-8"))
        assert row["stored_bytes"] == expected
        parts = await pool.fetch("SELECT stored_bytes,body FROM llm_request_parts")
        assert len(parts) == len(first.parts)
        for part in parts:
            assert part["stored_bytes"] == len(json.dumps(json.loads(part["body"]), ensure_ascii=False,
                                                        separators=(",", ":")).encode("utf-8"))
        async with pool.acquire() as conn:
            assert await llm_requests.logical_bytes(conn) == expected + sum(p["stored_bytes"] for p in parts)
        # The hourly query depends only on the byte columns; changing the body alone does
        # not change its result or force a row-to-text/TOAST scan.
        await pool.execute("UPDATE llm_requests SET response_raw = repeat('x', 100000)")
        async with pool.acquire() as conn:
            assert await llm_requests.logical_bytes(conn) == expected + sum(p["stored_bytes"] for p in parts)
    run_db(body, timeout=15)


def test_recorded_bytes_upgrade_backfills_once_and_preserves_bodies():
    from tests.bridge_schema_support import structure, witness

    async def body():
        async with fresh_connection() as old, fresh_connection() as fresh:
            await connection.ensure_schema(old)
            # A connection supplies acquire() only on pools, so seed through the actual
            # writer using a pool pinned to this disposable schema.
            schema = await old.fetchval("SELECT current_schema()")
            pool = await asyncpg.create_pool(DSN, min_size=1, max_size=1, server_settings={"search_path": schema})
            try:
                await llm_requests.write_batch([capture()], pool=pool)
            finally:
                await pool.close()
            await old.execute("ALTER TABLE llm_requests DROP COLUMN stored_bytes; "
                              "ALTER TABLE llm_request_parts DROP COLUMN stored_bytes")
            request = dict(await old.fetchrow("SELECT * FROM llm_requests"))
            parts = [dict(row) for row in await old.fetch("SELECT * FROM llm_request_parts ORDER BY sha256")]
            # Adding the nullable trailing byte column adds one comma to row::text.
            request_bytes = await old.fetchval("SELECT octet_length(row::text) + 1 FROM llm_requests row")
            part_bytes = dict(await old.fetch("SELECT sha256, octet_length(row::text) + 1 FROM llm_request_parts row"))
            before = await witness(old)
            assert await connection.schema_change_pending(old)
            assert await witness(old) == before
            await connection.ensure_schema(old)
            await connection.ensure_schema(fresh)
            assert await structure(old) == await structure(fresh)
            upgraded = dict(await old.fetchrow("SELECT * FROM llm_requests"))
            assert upgraded.pop("stored_bytes") == request_bytes and upgraded == request
            for upgraded, previous in zip(await old.fetch("SELECT * FROM llm_request_parts ORDER BY sha256"), parts):
                upgraded = dict(upgraded)
                assert upgraded.pop("stored_bytes") == part_bytes[previous["sha256"]] and upgraded == previous
            assert await old.fetchval("SELECT count(*) FROM schema_migrations "
                                      "WHERE name='llm_requests_stored_bytes'") == 1
            after = await witness(old)
            assert not await connection.schema_change_pending(old)
            await connection.ensure_schema(old)
            assert await witness(old) == after
            assert not await fresh.fetchval("SELECT EXISTS (SELECT 1 FROM schema_migrations "
                                           "WHERE name='llm_requests_stored_bytes')")
            await old.execute("ALTER TABLE llm_requests DROP COLUMN stored_bytes")
            assert not await connection.schema_change_pending(old)
    asyncio.run(asyncio.wait_for(body(), 30))
