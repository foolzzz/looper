"""Bound recorded exchanges while preserving every surviving request's full body."""

import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import logging
import os
from unittest.mock import AsyncMock

import pytest

from looper.agents import housekeeping
from looper.agents.llmrec import ROUTES_DIR, RouteFile
from looper.agents.llmrec.capture import build_capture, canonical
from looper.db import llm_requests, system_settings
from tests.db_support import run_db


def _capture(seq, *, age=0, route="a" * 22, agent="worker-test", task="task-test", system="shared system"):
    now = datetime.now(timezone.utc)
    owner = RouteFile(id=route, upstream="https://model.invalid", agent_id=agent, task_id=task,
                      created_at=now.isoformat())
    body = {"system": system, "tools": [{"name": "read"}],
            "messages": [{"role": "user", "content": f"message {seq}"}]}
    capture = build_capture(route=owner, instance="retention", seq=seq, path="/v1/messages",
        started_at=now - timedelta(days=age), request_body=canonical(body),
        response_headers={"content-type": "application/json"}, response_data=b'{"content":[]}')
    return capture, body


def test_age_prunes_unused_parts_but_rebuilds_surviving_live_route():
    async def check(pool):
        old, _ = _capture(1, age=10)
        new, expected = _capture(2)
        expired, _ = _capture(3, age=10, route="b" * 22)
        await llm_requests.write_batch([old, new, expired], pool=pool)
        assert await pool.fetchval("SELECT count(*) FROM llm_requests") == 3
        assert await pool.fetchval("SELECT count(*) FROM llm_request_parts") == 7
        async with pool.acquire() as conn, conn.transaction():
            before = datetime.now(timezone.utc) - timedelta(days=7)
            assert await llm_requests.prune_older(conn, before, batch=1) == 1
            assert await llm_requests.prune_older(conn, before) == 1
            assert await llm_requests.prune_older(conn, before) == 0
            assert await llm_requests.prune_empty_routes(conn) == 1
        assert await pool.fetchval("SELECT count(*) FROM llm_request_routes") == 1
        assert await pool.fetchval("SELECT count(*) FROM llm_request_parts") == len(new.parts)
        identifier = await pool.fetchval("SELECT id FROM llm_requests")
        assert (await llm_requests.get_request(identifier, pool=pool))["request_body"] == expected
    run_db(check, timeout=15)


def test_recent_empty_route_has_grace_and_scoped_purge_leaves_it():
    async def check(pool):
        item, _ = _capture(1)
        await llm_requests.write_batch([item], pool=pool)
        async with pool.acquire() as conn, conn.transaction():
            assert await llm_requests.prune_oldest(conn) == 1
            assert await llm_requests.prune_empty_routes(conn) == 0
            assert await llm_requests.purge(conn, task_id="task-test") == {"requests": 0, "routes": 0}
        assert await pool.fetchval("SELECT count(*) FROM llm_request_parts") == 0
    run_db(check, timeout=15)


@pytest.mark.parametrize("scope", [{"task_id": "task-test"}, {"agent_id": "worker-test"}, {"everything": True}])
def test_purge_scope_and_later_capture(scope):
    async def check(pool):
        target, _ = _capture(1)
        neighbor, neighbor_body = _capture(2, route="b" * 22, agent="neighbor", task="neighbor")
        await llm_requests.write_batch([target, neighbor], pool=pool)
        async with pool.acquire() as conn, conn.transaction():
            assert await llm_requests.purge(conn, **scope) == {
                "requests": 2 if scope.get("everything") else 1, "routes": 2 if scope.get("everything") else 1}
        if not scope.get("everything"):
            identifier = await pool.fetchval("SELECT id FROM llm_requests")
            assert (await llm_requests.get_request(identifier, pool=pool))["request_body"] == neighbor_body
        later, expected = _capture(3)
        await llm_requests.write_batch([later], pool=pool)
        identifier = await pool.fetchval("SELECT id FROM llm_requests WHERE proxy_seq=3")
        assert (await llm_requests.get_request(identifier, pool=pool))["request_body"] == expected
    run_db(check, timeout=15)


def test_oldest_pruning_uses_id_and_does_not_break_shared_parts():
    async def check(pool):
        first, _ = _capture(1)
        later, expected = _capture(2, age=1)
        await llm_requests.write_batch([first, later], pool=pool)
        async with pool.acquire() as conn, conn.transaction():
            assert await llm_requests.prune_oldest(conn, batch=1) == 1
        assert await pool.fetchval("SELECT proxy_seq FROM llm_requests") == 2
        identifier = await pool.fetchval("SELECT id FROM llm_requests")
        assert (await llm_requests.get_request(identifier, pool=pool))["request_body"] == expected
    run_db(check, timeout=15)


def test_logical_size_counts_live_toasted_content_once_and_excludes_dead_pages():
    async def check(pool):
        async with pool.acquire() as conn:
            assert await llm_requests.logical_bytes(conn) == 0
        items = [_capture(seq, system="s" * 32000)[0] for seq in (1, 2)]
        for item in items:
            item.response_raw = "答" * 16000
            item.response_bytes = 2**29  # Original wire sizes are not retained content sizes.
        await llm_requests.write_batch(items, pool=pool)
        async with pool.acquire() as conn:
            size = await llm_requests.logical_bytes(conn)
            # Two UTF-8 replies plus one shared system part, including compressed TOAST.
            assert 128000 < size < 160000
        async with pool.acquire() as conn, conn.transaction():
            assert await llm_requests.prune_oldest(conn, batch=1) == 1
        async with pool.acquire() as conn:
            remaining_size = await llm_requests.logical_bytes(conn)
            assert 80000 < remaining_size < size - 48000
        identifier = await pool.fetchval("SELECT id FROM llm_requests")
        assert (await llm_requests.get_request(identifier, pool=pool))["request_body"] == _capture(2, system="s" * 32000)[1]
        async with pool.acquire() as conn, conn.transaction():
            assert await llm_requests.prune_oldest(conn) == 1
            assert await llm_requests.prune_empty_routes(conn, grace=False) == 1
        async with pool.acquire() as conn:
            assert await llm_requests.logical_bytes(conn) == 0
            assert await llm_requests.stored_bytes(conn) > 0
    run_db(check, timeout=15)


def test_housekeeping_expires_when_recording_disabled_and_isolates_failure(monkeypatch, tmp_path, caplog):
    async def check(pool):
        old, _ = _capture(1, age=10)
        await llm_requests.write_batch([old], pool=pool)
        await system_settings.put_settings({"llmrec.enabled": "false"}, pool=pool)
        caplog.set_level(logging.INFO, logger=housekeeping.__name__)
        report = await housekeeping.run_once(tmp_path, datetime.now(timezone.utc), current_runner=None, commands=None)
        assert not report.failed_steps
        assert report.llm_requests == report.llm_routes == 1
        assert "llm_requests=1 llm_routes=1 llm_route_files=0" in caplog.text
        fresh, _ = _capture(2)
        await llm_requests.write_batch([fresh], pool=pool)
        monkeypatch.setattr(llm_requests, "prune_older", AsyncMock(side_effect=OSError("batch failed")))
        report = await housekeeping.run_once(tmp_path, datetime.now(timezone.utc), current_runner=None, commands=None)
        assert report.failed_steps == {"llm requests": "OSError: batch failed"}
        assert await pool.fetchval("SELECT count(*) FROM llm_requests") == 1
    run_db(check, monkeypatch, timeout=15)


def test_physical_overhead_does_not_evict_live_requests(monkeypatch):
    async def check(pool):
        items = [_capture(seq)[0] for seq in range(1, 4)]
        await llm_requests.write_batch(items, pool=pool)
        async with pool.acquire() as conn:
            size = await llm_requests.stored_bytes(conn)
            expected = await conn.fetchval("SELECT sum(pg_total_relation_size(name::regclass))::bigint "
                "FROM unnest(ARRAY['llm_requests','llm_request_parts','llm_request_routes']) name")
            assert size == expected and size > 0
        # Live contents fit, but even the compacted indexes exceed this cap.
        monkeypatch.setattr(housekeeping, "MIB", 16 * 1024 / 2048)
        report = housekeeping.Report()
        with monkeypatch.context() as patch:
            patch.setattr(llm_requests, "stored_bytes", AsyncMock(side_effect=AssertionError("physical size read")))
            await housekeeping._prune_llm_requests(report, datetime.now(timezone.utc))
        assert report.llm_requests == 0 and not report.failed_steps
        assert await pool.fetchval("SELECT count(*) FROM llm_requests") == len(items)
        async with pool.acquire() as conn:
            assert await llm_requests.stored_bytes(conn) > 2048 * housekeeping.MIB
        for row in await pool.fetch("SELECT id, proxy_seq FROM llm_requests"):
            assert (await llm_requests.get_request(row["id"], pool=pool))["request_body"] == _capture(row["proxy_seq"])[1]
    run_db(check, monkeypatch, timeout=15)


def test_route_file_gc_ended_stale_fresh_unreadable_and_replaced(tmp_path, monkeypatch, caplog):
    now = datetime.now(timezone.utc)
    directory = tmp_path / ROUTES_DIR
    paths = {}
    for name, ended, age in (("ended", now - timedelta(days=2), 0), ("stale", None, 8),
                              ("stale_ended", now - timedelta(hours=1), 8),
                              ("fresh", None, 0), ("recent_end", now - timedelta(hours=1), 0)):
        route = replace(_capture(1)[0].route, ended_at=ended.isoformat() if ended else None)
        path = directory / f"{name}.json"
        route.write(path)
        stamp = (now - timedelta(days=age)).timestamp()
        os.utime(path, (stamp, stamp))
        paths[name] = path
    broken = directory / "unreadable.json"
    broken.write_text("{")
    os.utime(broken, ((now - timedelta(days=8)).timestamp(),) * 2)
    assert housekeeping.prune_route_files(directory, now) == 2
    assert paths["stale"].exists()
    assert not paths["ended"].exists() and not paths["stale_ended"].exists()
    assert paths["fresh"].exists() and paths["recent_end"].exists() and broken.exists()
    assert "skipped unreadable" in caplog.text
    stale = paths["recent_end"]
    os.utime(stale, ((now - timedelta(days=8)).timestamp(),) * 2)
    read = RouteFile.read
    def concurrent_write(path):
        route = read(path)
        if path == stale:
            route.write(path)
        return route
    monkeypatch.setattr(RouteFile, "read", concurrent_write)
    assert housekeeping.prune_route_files(directory, now) == 0 and stale.exists()


def test_concurrent_writer_and_prune_preserve_all_new_references(monkeypatch):
    async def check(pool):
        old, _ = _capture(1, age=10)
        new, expected = _capture(2)
        await llm_requests.write_batch([old], pool=pool)
        # Pause a real write after its route upsert/parts insert. GC must wait for that
        # route transaction before checking which parts the new request references.
        async with pool.acquire() as conn:
            await conn.execute("""CREATE FUNCTION pause_capture() RETURNS trigger LANGUAGE plpgsql AS $$
                BEGIN
                    IF NEW.proxy_seq = 2 THEN PERFORM pg_advisory_xact_lock(310501); END IF;
                    RETURN NEW;
                END $$;
                CREATE TRIGGER pause_capture BEFORE INSERT ON llm_requests
                    FOR EACH ROW EXECUTE FUNCTION pause_capture()""")
            await conn.execute("SELECT pg_advisory_lock(310501)")
            writer = asyncio.create_task(llm_requests.write_batch([new], pool=pool))
            entered = asyncio.Event()
            original = llm_requests._lock_request_routes
            async def observed(conn, rows):
                entered.set()
                return await original(conn, rows)
            monkeypatch.setattr(llm_requests, "_lock_request_routes", observed)
            async def prune():
                async with pool.acquire() as gc, gc.transaction():
                    return await llm_requests.prune_older(gc, datetime.now(timezone.utc) - timedelta(days=7))
            cleaner = None
            try:
                # A positive database observation proves the writer is inside the trigger,
                # rather than relying on sleeps to guess that it has locked the route.
                async def blocked_writer():
                    for _ in range(100):
                        if await conn.fetchval("SELECT EXISTS (SELECT 1 FROM pg_locks WHERE locktype='advisory' "
                                               "AND objid=310501 AND NOT granted)"):
                            return
                        await asyncio.sleep(0.01)
                    raise AssertionError("writer did not reach its request insert")
                await asyncio.wait_for(blocked_writer(), 3)
                cleaner = asyncio.create_task(prune())
                await asyncio.wait_for(entered.wait(), 2)
                assert not cleaner.done()
                await conn.execute("SELECT pg_advisory_unlock(310501)")
                assert await asyncio.wait_for(cleaner, 3) == 1
                await asyncio.wait_for(writer, 3)
            finally:
                await conn.execute("SELECT pg_advisory_unlock_all()")
                for task in (writer, cleaner):
                    if task is not None and not task.done():
                        task.cancel()
                await asyncio.gather(*(task for task in (writer, cleaner) if task is not None), return_exceptions=True)
        identifier = await pool.fetchval("SELECT id FROM llm_requests")
        assert (await llm_requests.get_request(identifier, pool=pool))["request_body"] == expected
    run_db(check, monkeypatch, timeout=15)


def test_size_cap_evicts_only_the_oldest_byte_budget(monkeypatch):
    async def check(pool):
        await system_settings.put_settings({"llmrec.max_total_mb": "256"}, pool=pool)
        # One MiB of parts per route, highly compressible on disk: use recorded bytes.
        for seq in range(1, 271):
            item, _ = _capture(seq, route=f"{seq:022d}", system="s" * housekeeping.MIB)
            await llm_requests.write_batch([item], pool=pool)
        async with pool.acquire() as conn:
            assert 270 * housekeeping.MIB < await llm_requests.logical_bytes(conn) < 271 * housekeeping.MIB
        report = housekeeping.Report()
        await housekeeping._prune_llm_requests(report, datetime.now(timezone.utc))
        assert report.llm_requests == report.llm_routes == 40
        assert await pool.fetchval("SELECT min(proxy_seq) FROM llm_requests") == 41
        assert await pool.fetchval("SELECT count(*) FROM llm_requests") == 230
        async with pool.acquire() as conn:
            assert 229 * housekeeping.MIB < await llm_requests.logical_bytes(conn) <= 0.9 * 256 * housekeeping.MIB
        identifier = await pool.fetchval("SELECT id FROM llm_requests ORDER BY id LIMIT 1")
        assert (await llm_requests.get_request(identifier, pool=pool))["request_body"]["system"] == "s" * housekeeping.MIB
    run_db(check, monkeypatch, timeout=60)


def test_size_cap_backfills_rows_from_an_older_writer(monkeypatch):
    async def check(pool):
        await system_settings.put_settings({"llmrec.max_total_mb": "256"}, pool=pool)
        # Scale the cap down so a small fixture can exercise the real eviction path.
        monkeypatch.setattr(housekeeping, "MIB", 128)
        fill = llm_requests.fill_missing_stored_bytes
        monkeypatch.setattr(llm_requests, "fill_missing_stored_bytes", lambda conn: fill(conn, batch=1))
        old, _ = _capture(1, system="s" * 32000)
        survivor, expected = _capture(2, route="b" * 22)
        await llm_requests.write_batch([old, survivor], pool=pool)
        await pool.execute("UPDATE llm_requests SET stored_bytes = 0")
        await pool.execute("UPDATE llm_request_parts SET stored_bytes = 0")
        async with pool.acquire() as conn:
            assert await llm_requests.logical_bytes(conn) == 0
        report = housekeeping.Report()
        await housekeeping._prune_llm_requests(report, datetime.now(timezone.utc))
        assert report.llm_requests == report.llm_routes == 1
        assert await pool.fetchval("SELECT proxy_seq FROM llm_requests") == 2
        assert await pool.fetchval("SELECT min(stored_bytes) FROM llm_requests") > 0
        assert await pool.fetchval("SELECT min(stored_bytes) FROM llm_request_parts") > 0
        identifier = await pool.fetchval("SELECT id FROM llm_requests")
        assert (await llm_requests.get_request(identifier, pool=pool))["request_body"] == expected
    run_db(check, monkeypatch, timeout=15)


def test_byte_budget_counts_parts_only_when_the_route_is_emptied():
    async def check(pool):
        old, _ = _capture(1, system="s" * 32000)
        neighbor, _ = _capture(2, route="b" * 22, system="n" * 32000)
        shared, expected = _capture(3, system="s" * 32000)
        await llm_requests.write_batch([old, neighbor, shared], pool=pool)
        async with pool.acquire() as conn, conn.transaction():
            # The first request alone cannot free the shared system part. The second can
            # free its route's 32 KiB, so stop there instead of deleting the newer owner.
            assert await llm_requests.prune_oldest(conn, byte_budget=16000) == 2
            assert await llm_requests.prune_empty_routes(conn, grace=False) == 1
        assert await pool.fetchval("SELECT proxy_seq FROM llm_requests") == 3
        assert await pool.fetchval("SELECT count(*) FROM llm_request_parts") == len(shared.parts)
        identifier = await pool.fetchval("SELECT id FROM llm_requests")
        assert (await llm_requests.get_request(identifier, pool=pool))["request_body"] == expected
    run_db(check, timeout=15)


def test_byte_budget_counts_exclusive_parts_on_a_surviving_route():
    async def check(pool):
        first, _ = _capture(1, system="a" * 32000)
        second, expected = _capture(2, system="b" * 32000)
        await llm_requests.write_batch([first, second], pool=pool)
        async with pool.acquire() as conn, conn.transaction():
            assert await llm_requests.prune_oldest(conn, byte_budget=16000) == 1
            assert await llm_requests.prune_empty_routes(conn, grace=False) == 0
        assert await pool.fetchval("SELECT proxy_seq FROM llm_requests") == 2
        assert await pool.fetchval("SELECT count(*) FROM llm_request_parts") == len(second.parts)
        identifier = await pool.fetchval("SELECT id FROM llm_requests")
        assert (await llm_requests.get_request(identifier, pool=pool))["request_body"] == expected
    run_db(check, timeout=15)


def test_byte_budget_bounds_each_batch_to_two_thousand():
    async def check(pool):
        item, _ = _capture(1)
        await llm_requests.write_batch([item], pool=pool)
        await pool.execute("""INSERT INTO llm_requests
            (route_id,proxy_instance,proxy_seq,agent_id,started_at,method,path,kind,outcome,
             request_headers,request_bytes,request_sha256,stored_bytes)
            SELECT $1,'bulk',seq,'worker-test',now(),'POST','/v1/messages','model','completed','{}',0,'',1
            FROM generate_series(2,2002) seq""", item.route.id)
        async with pool.acquire() as conn, conn.transaction():
            assert await llm_requests.prune_oldest(conn, batch=5000, byte_budget=10**9) == 2000
        assert await pool.fetchval("SELECT count(*) FROM llm_requests") == 2
    run_db(check, timeout=15)


def test_housekeeping_stops_after_fifty_rounds(monkeypatch):
    async def check(pool):
        # Simulate concurrent writes maintaining the size: enforce the work bound even
        # when successful deletions do not reduce the observed recorded bytes.
        size = AsyncMock(return_value=300 * housekeeping.MIB)
        prune = AsyncMock(return_value=1)
        monkeypatch.setattr(llm_requests, "logical_bytes", size)
        monkeypatch.setattr(llm_requests, "prune_oldest", prune)
        await system_settings.put_settings({"llmrec.max_total_mb": "256"}, pool=pool)
        report = housekeeping.Report()
        await housekeeping._prune_llm_requests(report, datetime.now(timezone.utc))
        assert report.llm_requests == prune.await_count == 50
        assert all(call.kwargs["byte_budget"] == 300 * housekeeping.MIB - 256 * housekeeping.MIB * 9 // 10
                   for call in prune.await_args_list)
    run_db(check, monkeypatch, timeout=15)


def test_housekeeping_stops_when_no_requests_can_be_deleted(monkeypatch):
    async def check(pool):
        monkeypatch.setattr(llm_requests, "logical_bytes", AsyncMock(return_value=300 * housekeeping.MIB))
        prune = AsyncMock(return_value=0)
        monkeypatch.setattr(llm_requests, "prune_oldest", prune)
        await system_settings.put_settings({"llmrec.max_total_mb": "256"}, pool=pool)
        report = housekeeping.Report()
        await housekeeping._prune_llm_requests(report, datetime.now(timezone.utc))
        assert report.llm_requests == 0 and prune.await_count == 1
    run_db(check, monkeypatch, timeout=15)


@pytest.mark.parametrize("scope", [{"task_id": "task-test"}, {"agent_id": "worker-test"}])
def test_scoped_purge_sweeps_only_routes_it_emptied(scope):
    async def check(pool):
        target, _ = _capture(1)
        untouched, _ = _capture(2, route="b" * 22)
        neighbor, expected = _capture(3, route="c" * 22, agent="neighbor", task="neighbor")
        await llm_requests.write_batch([target, untouched, neighbor], pool=pool)
        await pool.execute("DELETE FROM llm_requests WHERE proxy_seq=2")
        async with pool.acquire() as conn, conn.transaction():
            assert await llm_requests.purge(conn, **scope) == {"requests": 1, "routes": 1}
        assert await pool.fetchval("SELECT count(*) FROM llm_request_routes WHERE id=$1", untouched.route.id) == 1
        identifier = await pool.fetchval("SELECT id FROM llm_requests")
        assert (await llm_requests.get_request(identifier, pool=pool))["request_body"] == expected
    run_db(check, timeout=15)


def test_manager_cleanup_counts_and_removes_owned_routes_and_parts():
    from looper.db import manager_cleanup
    async def check(pool):
        await pool.execute("INSERT INTO managers (id, name, system_prompt, enabled, deleted_at) "
                           "VALUES ('manager-retired', 'Retired', 'Help.', false, now() - interval '31 days')")
        owner, _ = _capture(1, agent="manager-retired")
        between, _ = _capture(2, agent="manager-retired")
        neighbor, expected = _capture(3, route="b" * 22, agent="manager-neighbor")
        await llm_requests.write_batch([owner, between, neighbor], pool=pool)
        preview = await manager_cleanup.preview("manager-retired", pool=pool)
        assert preview["eligible"] and preview["counts"]["llm_requests"] == 2
        result = await manager_cleanup.purge("manager-retired", pool=pool)
        assert result["purged"] and result["counts"]["llm_requests"] == 2
        assert await pool.fetchval("SELECT count(*) FROM llm_request_routes") == 1
        assert await pool.fetchval("SELECT count(*) FROM llm_request_parts") == len(neighbor.parts)
        identifier = await pool.fetchval("SELECT id FROM llm_requests")
        assert (await llm_requests.get_request(identifier, pool=pool))["request_body"] == expected
    run_db(check, timeout=15)
