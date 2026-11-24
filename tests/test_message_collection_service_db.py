"""Collection scheduling does not wait on account I/O or change bridge ticks."""

import asyncio
from datetime import timedelta

import pytest

from looper.agents.drain import DrainScope, DrainState
from looper.agents.message_collect import AuthStatus, CollectorError
from looper.agents.source_service import SourceService
from looper.db import message_stream as store
from tests import msgflow_support as sup
from tests.message_collect_support import Backend
from tests.db_support import DSN, run_db

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")


async def source(pool, source_id="synthetic"):
    await store.save_source(
        source_id,
        {"name": "Synthetic", "ingest_mode": "collect", "lark_profile": "synthetic", "user_open_id": sup.ME},
        db=pool,
    )
    await pool.execute(
        "INSERT INTO source_collection_settings(source_id,collect_enabled_at) VALUES($1,$2)", source_id, sup.NOW
    )


def test_collect_does_not_block_bridge_ticks_alerts_or_other_sources(monkeypatch, tmp_path):
    async def check(pool):
        await source(pool)
        await source(pool, "second")
        await store.save_source(
            "bridge",
            {"name": "Bridge", "ingest_mode": "bridge", "bridge_dir": str(tmp_path), "bridge_start_from": "beginning"},
            db=pool,
        )
        sup.append(tmp_path, "oc_bridge", sup.message("bridge_m"))
        gate = asyncio.Event()

        class Blocked(Backend):
            async def auth_status(self, profile):
                await gate.wait()
                return AuthStatus("ready", sup.ME)

        class Alerts:
            ticks = 0

            async def evaluate(self, *args, **kwargs):
                self.ticks += 1

            async def flush(self):
                pass

            async def stop(self):
                pass

        alerts = Alerts()
        service = SourceService(collector_backend=Blocked(), alerts=alerts)
        try:
            assert await asyncio.wait_for(service.run_once(clock=lambda: sup.NOW), 2) == 3
            assert len(service._collect_tasks) == 2
            assert await pool.fetchval("SELECT count(*) FROM source_events WHERE source_id='bridge'") == 1
            assert alerts.ticks >= 1
            with pytest.raises(CollectorError):
                await service.run_now("synthetic")
            await service.run_once(clock=lambda: sup.NOW + timedelta(seconds=15))
            assert await pool.fetchval("SELECT count(*) FROM source_runs WHERE status='skipped_busy'") == 0
        finally:
            await service.stop()
        assert not service._collect_tasks

    run_db(check, monkeypatch, timeout=30)


def test_due_slot_sleep_drain_and_stop_have_bounded_work(monkeypatch):
    async def check(pool):
        await source(pool)
        gate = asyncio.Event()

        class Blocked(Backend):
            async def auth_status(self, profile):
                await gate.wait()
                return AuthStatus("missing", None)

        service = SourceService(collector_backend=Blocked())
        other = SourceService(collector_backend=Blocked())
        try:
            assert await service.run_once(clock=lambda: sup.NOW) == 1
            assert await other.run_once(clock=lambda: sup.NOW) == 0
            assert await service.run_once(clock=lambda: sup.NOW + timedelta(hours=5)) == 0
            assert await service.run_once(clock=lambda: sup.NOW + timedelta(hours=5, seconds=15)) == 0
            assert await pool.fetchval("SELECT count(*) FROM source_runs WHERE status='skipped_busy'") == 1
            drain = DrainState()
            drain.begin(DrainScope.RUNS)
            service._drain = drain
            assert await service.run_once(clock=lambda: sup.NOW + timedelta(hours=6)) == 0
        finally:
            await service.stop()
            await other.stop()
        assert not service._collect_tasks

    run_db(check, monkeypatch, timeout=30)


def test_daily_metadata_retention_survives_rollback_and_keeps_unresolved_evidence(monkeypatch):
    from looper.db import message_parity as parity

    async def check(pool):
        await source(pool)
        await store.save_source("synthetic", {"ingest_mode": "off"}, db=pool)
        ids = {}
        async with pool.acquire() as conn:
            for state in ("matched", "drift", "incomplete"):
                ids[state] = await parity.create_session(
                    conn,
                    "synthetic",
                    0,
                    sup.NOW - timedelta(days=1),
                    sup.NOW,
                    0,
                    policy_digest="synthetic",
                    reference_digest="synthetic",
                )
                await conn.execute(
                    "UPDATE source_parity_sessions SET state=$2,finished_at=$3 WHERE id=$1",
                    ids[state],
                    state,
                    None if state == "incomplete" else sup.NOW - timedelta(days=40),
                )
            old = await store.start_run(conn, "synthetic", "collect", "scheduled", now=sup.NOW - timedelta(days=41))
            await store.finish_run(conn, old, "ok", now=sup.NOW - timedelta(days=40))
        service = SourceService()
        assert await service.run_once(clock=lambda: sup.NOW) == 0
        assert {row[0] for row in await pool.fetch("SELECT id FROM source_parity_sessions")} == {
            ids["drift"],
            ids["incomplete"],
        }
        assert await pool.fetchval("SELECT count(*) FROM source_runs WHERE id=$1", old) == 0
        assert await pool.fetchval("SELECT ingest_mode FROM event_sources WHERE id='synthetic'") == "off"

    run_db(check, monkeypatch, timeout=30)
