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


def _capture(seq, *, age=0, route="a" * 22, agent="worker-test", task="task-test"):
    now = datetime.now(timezone.utc)
    owner = RouteFile(id=route, upstream="https://model.invalid", agent_id=agent, task_id=task,
                      created_at=now.isoformat())
    body = {"system": "shared system", "tools": [{"name": "read"}],
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


def test_recent_empty_route_has_grace_then_purge_reclaims_it():
    async def check(pool):
        item, _ = _capture(1)
        await llm_requests.write_batch([item], pool=pool)
        async with pool.acquire() as conn, conn.transaction():
            assert await llm_requests.prune_oldest(conn) == 1
            assert await llm_requests.prune_empty_routes(conn) == 0
            assert await llm_requests.purge(conn, task_id="task-test") == {"requests": 0, "routes": 1}
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


def test_physical_size_cap_bounds_rounds_without_faking_size_query(monkeypatch, caplog):
    async def check(pool):
        items = [_capture(seq)[0] for seq in range(1, 4)]
        await llm_requests.write_batch(items, pool=pool)
        async with pool.acquire() as conn:
            size = await llm_requests.stored_bytes(conn)
            expected = await conn.fetchval("SELECT sum(pg_total_relation_size(name::regclass))::bigint "
                "FROM unnest(ARRAY['llm_requests','llm_request_parts','llm_request_routes']) name")
            assert size == expected and size > 0
        # An intentionally tiny unit makes the cap smaller than even PostgreSQL's empty indexes.
        # DELETE does not shrink those relations: the real query must hit the bounded warning path.
        monkeypatch.setattr(housekeeping, "MIB", 1)
        original = llm_requests.prune_oldest
        calls, deleted = [], []
        async def prune(conn):
            calls.append(1)
            oldest = await conn.fetchval("SELECT id FROM llm_requests ORDER BY id LIMIT 1")
            count = await original(conn, batch=1)
            if count:
                deleted.append(oldest)
            return count
        monkeypatch.setattr(llm_requests, "prune_oldest", prune)
        report = housekeeping.Report()
        await housekeeping._prune_llm_requests(report, datetime.now(timezone.utc))
        assert len(calls) == 50 and deleted == sorted(deleted) and len(deleted) == 3
        assert report.llm_requests == 3
        assert "after 50 rounds" in caplog.text
        assert "allocated PostgreSQL pages" in caplog.text
    run_db(check, monkeypatch, timeout=15)


def test_size_cap_reclaims_space_to_ninety_percent_before_eviction(monkeypatch):
    async def check(pool):
        # Make physically sparse pages without dead versions that another snapshot could pin.
        # Repacking those pages must reclaim real relation space before deleting retained rows.
        await pool.execute("ALTER TABLE llm_requests SET (fillfactor=10)")
        items = [_capture(seq)[0] for seq in range(1, 21)]
        for item in items:
            item.response_body = {"content": "x" * 600}
        await llm_requests.write_batch(items, pool=pool)
        async with pool.acquire() as conn:
            size = await llm_requests.stored_bytes(conn)
        await pool.execute("ALTER TABLE llm_requests SET (fillfactor=100)")
        monkeypatch.setattr(housekeeping, "MIB", size * 0.95 / 2048)
        report = housekeeping.Report()
        await housekeeping._prune_llm_requests(report, datetime.now(timezone.utc))
        async with pool.acquire() as conn:
            assert await llm_requests.stored_bytes(conn) <= 0.9 * 2048 * housekeeping.MIB
        assert report.llm_requests == 0
        assert await pool.fetchval("SELECT count(*) FROM llm_requests") == len(items)
        identifier = await pool.fetchval("SELECT id FROM llm_requests ORDER BY id DESC LIMIT 1")
        assert (await llm_requests.get_request(identifier, pool=pool))["request_body"]["messages"][0]["content"] == "message 20"
    run_db(check, monkeypatch, timeout=20)


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


def test_compaction_failure_keeps_eviction_and_later_cleanup_running(monkeypatch, tmp_path, caplog):
    async def check(pool):
        await llm_requests.write_batch([_capture(1)[0]], pool=pool)
        monkeypatch.setattr(housekeeping, "MIB", 1)
        monkeypatch.setattr(housekeeping, "_compact_llm_requests", AsyncMock(side_effect=TimeoutError("lock busy")))
        old_runner = tmp_path / ".local/command-runner/runner-old.py"
        old_runner.parent.mkdir(parents=True)
        old_runner.write_text("pass")
        now = datetime.now(timezone.utc)
        os.utime(old_runner, ((now - timedelta(days=40)).timestamp(),) * 2)
        report = await housekeeping.run_once(tmp_path, now, current_runner=None, commands=None)
        assert report.failed_steps == {"llm request compaction": "TimeoutError: lock busy"}
        assert "lock busy" in caplog.text and "after 50 rounds" in caplog.text
        assert report.runner_files == 1 and not old_runner.exists()
        assert report.llm_requests == 1
        assert await pool.fetchval("SELECT count(*) FROM llm_requests") == 0
        assert await pool.fetchval("SELECT count(*) FROM llm_request_parts") == 0
    run_db(check, monkeypatch, timeout=15)


@pytest.mark.parametrize('failed_call', [1, 2])
def test_compaction_timeout_still_evicts_oldest_to_physical_target(monkeypatch, failed_call, caplog):
    async def check(pool):
        # Sparse pages let recovery reclaim physical space without depending on
        # immediate reclamation of deleted tuple versions on a shared cluster.
        await pool.execute("ALTER TABLE llm_requests SET (fillfactor=10)")
        items = [_capture(seq)[0] for seq in range(1, 25)]
        for item in items:
            item.response_body = {"content": "x" * 600}
        await llm_requests.write_batch(items, pool=pool)
        async with pool.acquire() as conn:
            size = await llm_requests.stored_bytes(conn)
        monkeypatch.setattr(housekeeping, "MIB", size * 0.95 / 2048)
        compact = housekeeping._compact_llm_requests
        calls = 0
        async def sometimes_compact():
            nonlocal calls
            calls += 1
            if calls == failed_call:
                await pool.execute("ALTER TABLE llm_requests SET (fillfactor=100)")
                raise TimeoutError("lock busy")
            await compact()
        monkeypatch.setattr(housekeeping, "_compact_llm_requests", sometimes_compact)
        prune = llm_requests.prune_oldest
        async def prune_one(conn):
            return await prune(conn, batch=1)
        monkeypatch.setattr(llm_requests, "prune_oldest", prune_one)
        report = housekeeping.Report()
        await housekeeping._prune_llm_requests(report, datetime.now(timezone.utc))
        assert calls > failed_call and "lock busy" in caplog.text
        assert report.failed_steps == {"llm request compaction": "TimeoutError: lock busy"}
        async with pool.acquire() as conn:
            assert await llm_requests.stored_bytes(conn) <= 0.9 * 2048 * housekeeping.MIB
        remaining = await pool.fetch("SELECT id, proxy_seq FROM llm_requests ORDER BY id")
        assert 0 < report.llm_requests < len(items)
        assert [row['proxy_seq'] for row in remaining] == list(range(report.llm_requests + 1, len(items) + 1))
        for row in remaining:
            request = await llm_requests.get_request(row['id'], pool=pool)
            assert request['response_body'] == items[row['proxy_seq'] - 1].response_body
    run_db(check, monkeypatch, timeout=20)


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
