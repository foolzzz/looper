"""Collection scheduling does not wait on account I/O or change bridge ticks."""

import asyncio
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

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


@pytest.mark.parametrize("initially_enabled", [False, True])
def test_alert_polling_tracks_runtime_switch_without_reads_while_off(monkeypatch, initially_enabled):
    from looper.agents.message_stream_alerts import SourceAlerts
    from looper.agents.v1_domains import message_collection as api
    from looper.db import message_stream_alerts

    async def check(pool):
        await source(pool)
        await pool.execute("UPDATE source_collection_settings SET alerts_enabled=$1", initially_enabled)
        configured = AsyncMock(wraps=message_stream_alerts.configured)
        monkeypatch.setattr(message_stream_alerts, "configured", configured)
        ticks = asyncio.Queue()

        async def list_sources():
            ticks.put_nowait(None)
            return []

        monkeypatch.setattr(store, "list_sources", list_sources)
        alerts = SourceAlerts()
        monkeypatch.setattr(alerts, "evaluate", AsyncMock())
        monkeypatch.setattr(alerts, "flush", AsyncMock())
        service = SourceService(interval=0.001, alerts=alerts)

        async def tick(count=1):
            for _ in range(count):
                await asyncio.wait_for(ticks.get(), 2)
                await asyncio.sleep(0)

        async def update(enabled):
            await api.run(SimpleNamespace(_source_service=service),
                          SimpleNamespace(operation_id="message_collection.update"),
                          {"source_id": "synthetic", "fields": {"alerts_enabled": enabled}}, None, {})
            while not ticks.empty():
                ticks.get_nowait()

        try:
            await service.start()
            await tick(5)
            assert bool(alerts.evaluate.await_count) is initially_enabled
            assert alerts.flush.await_count == alerts.evaluate.await_count
            assert configured.await_count == 1
            await update(False)
            before = (alerts.evaluate.await_count, alerts.flush.await_count, configured.await_count)
            await tick(5)
            assert (alerts.evaluate.await_count, alerts.flush.await_count, configured.await_count) == before
            await update(True)
            before = alerts.evaluate.await_count
            await tick()
            assert alerts.evaluate.await_count > before
            await update(False)
            before = (alerts.evaluate.await_count, alerts.flush.await_count, configured.await_count)
            await tick(5)
            assert (alerts.evaluate.await_count, alerts.flush.await_count, configured.await_count) == before
        finally:
            await service.stop()

    run_db(check, monkeypatch, timeout=30)


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
        await pool.execute("UPDATE source_collection_settings SET alerts_enabled=true")
        await service.refresh_alerts()
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
