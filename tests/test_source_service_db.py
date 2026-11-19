"""SourceService: which sources run on a tick, and what a pass is given."""

from datetime import datetime, timedelta, timezone

import pytest

from looper.agents.drain import DrainScope, DrainState
from looper.agents.source_service import SourceService
from looper.db import message_stream as store
from tests import msgflow_support as sup
from tests.db_support import DSN, run_db

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")

T0 = datetime(2026, 11, 18, 12, 0, 30, tzinfo=timezone.utc)


async def sources(pool, directory):
    await store.save_source("bridged", {"name": "B", "ingest_mode": "bridge", "bridge_dir": str(directory),
                                        "cron": "*/2 * * * *", "timezone": "UTC", "bridge_start_from": "beginning"}, db=pool)
    await store.save_source("idle", {"name": "I"}, db=pool)


def test_a_tick_runs_a_bridged_source_once_per_fire_of_its_rule(monkeypatch, tmp_path):
    async def check(pool):
        sup.append(tmp_path, "oc_a", sup.message("m1"))
        service = SourceService()
        assert await service.run_once(clock=lambda: T0) == 1
        assert await service.run_once(clock=lambda: T0 + timedelta(seconds=40)) == 0
        sup.append(tmp_path, "oc_a", sup.message("m2", sent_at=T0))
        assert await service.run_once(clock=lambda: T0 + timedelta(minutes=2)) == 1
        # After a long sleep one pass catches up; it does not replay every missed fire.
        assert await service.run_once(clock=lambda: T0 + timedelta(hours=5)) == 1
        assert await pool.fetchval("SELECT count(*) FROM source_runs WHERE source_id = 'bridged'") == 3
        assert await pool.fetchval("SELECT count(*) FROM source_runs WHERE source_id = 'idle'") == 0
        assert await pool.fetchval("SELECT count(*) FROM source_events") == 2

    run_db(check, monkeypatch, setup=lambda pool: sources(pool, tmp_path), timeout=30)


def test_a_draining_server_starts_no_pass(monkeypatch, tmp_path):
    async def check(pool):
        drain = DrainState()
        drain.begin(DrainScope.RUNS)
        assert await SourceService(drain=drain).run_once(clock=lambda: T0) == 0
        assert await pool.fetchval("SELECT count(*) FROM source_runs") == 0

    run_db(check, monkeypatch, setup=lambda pool: sources(pool, tmp_path), timeout=30)


def test_the_pass_is_given_the_apps_of_the_bots_and_keeps_one_it_has_learnt(monkeypatch, tmp_path):
    async def check(pool):
        sup.append(tmp_path, "oc_a", sup.message("push", sender=sup.LOOPER_APP, sender_type="app"),
                   sup.message("kept", sender=sup.OTHER_APP, sender_type="app"))
        answers = [{sup.LOOPER_APP}, set()]

        async def own_apps():
            return answers.pop(0)

        service = SourceService(own_apps=own_apps)
        assert await service.known_own_apps() == {sup.LOOPER_APP}
        await service.run_once(clock=lambda: T0)
        assert [r[0] for r in await pool.fetch("SELECT external_id FROM source_events")] == ["kept"]
        # A later answer without the app does not forget it (a bot replaced since still wrote those messages).
        service._own_apps_at = -1e9
        assert await service.known_own_apps() == {sup.LOOPER_APP}

    run_db(check, monkeypatch, setup=lambda pool: sources(pool, tmp_path), timeout=30)


def test_every_configured_lark_channel_counts_as_the_bots_own_app_enabled_or_not(monkeypatch):
    from looper.agents.source_service import own_bot_app_ids

    async def check(pool):
        for channel_id, kind, app, enabled in (("c1", "lark_bot", "cli_on", True), ("c2", "lark_bot", "cli_off", False),
                                               ("c3", "telegram", "cli_tg", True)):
            await pool.execute("INSERT INTO channel_configs (channel_id, channel_type, enabled, config) "
                               "VALUES ($1, $2, $3, $4::jsonb)", channel_id, kind, enabled,
                               '{"app_id": "%s"}' % app)
        assert await own_bot_app_ids() == {"cli_on", "cli_off"}

    run_db(check, monkeypatch, timeout=30)
