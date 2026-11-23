"""Triage's setting is operator-only; the foundation cannot run or send anything."""

from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from looper.agents import api_v1, triage_settings
from looper.agents.action_levels import ActionLevel
from tests.db_support import DSN, run_db, v1_api

pytestmark = pytest.mark.skipif(not DSN, reason="requires disposable LOOPER_TEST_DATABASE_URL")


def test_triage_setting_defaults_off_and_only_the_operator_can_store_or_reset_it(monkeypatch, tmp_path):
    async def check(pool):
        async with v1_api(tmp_path, pool) as call:
            for action, arguments in (("get", {}), ("update", {"enabled": True})):
                operation = api_v1.find("POST", f"/api/v1/triage_settings/{action}")
                assert operation.operator_only and operation.tool is None
                assert operation.level is ActionLevel.INSIDE
                status, answer = await call(f"triage_settings.{action}", arguments, client=True)
                assert status == 403 and answer["error"]["code"] == "forbidden"
            assert await triage_settings.view() == {"enabled": False}
            for arguments, expected in (({}, False), ({"enabled": True}, True),
                                        ({"enabled": False}, False), ({"enabled": None}, False)):
                action = "update" if arguments else "get"
                status, answer = await call(f"triage_settings.{action}", arguments)
                assert status == 200 and answer["result"] == {"enabled": expected}, answer
                assert await triage_settings.enabled() is expected
            assert await pool.fetchval("SELECT value FROM system_settings WHERE key = $1", triage_settings.KEY) is None
            for value in ("true", 1):
                status, _ = await call("triage_settings.update", {"enabled": value})
                assert status == 400
            assert await triage_settings.view() == {"enabled": False}
            assert await pool.fetchval("SELECT count(*) FROM manager_events") == 0
    run_db(check, monkeypatch)


def test_triage_firing_stays_inert_while_the_switch_is_off(monkeypatch, tmp_path):
    from looper.agents import extraction
    from looper.agents.scheduler import Scheduler
    from looper.db import schedules
    from tests.subscription_support import SOURCE, source

    async def check(pool):
        await source(pool)
        await triage_settings.update(False)
        await pool.execute("INSERT INTO projects (id, name) VALUES ('p', 'Synthetic project')")
        await pool.execute("INSERT INTO schedules (id, project_id, name, cron, timezone, kind, requirement) "
                           "VALUES ('s', 'p', 'Triage', '0 * * * *', 'UTC', 'subscription', 'Synthetic triage')")
        await pool.execute("INSERT INTO subscriptions (schedule_id, source_id, chat_kinds, sender_kinds, "
                           "max_age_minutes, max_events, text_max_chars, output_kind, cursor_seq) "
                           "VALUES ('s', $1, ARRAY['p2p'], ARRAY['user'], 60, 500, 200, 'triage', 42)", SOURCE)
        processor = AsyncMock(side_effect=AssertionError("a foundation must not launch extraction"))
        own_apps = AsyncMock(side_effect=AssertionError("an inert triage must not read a source"))
        monkeypatch.setattr(extraction, "plan", processor)
        poster = AsyncMock()
        now = datetime.now(timezone.utc)
        runner = Scheduler(bus=AsyncMock(), runtime_root=tmp_path, poster=poster, own_apps=own_apps,
                           clock=lambda: now)
        async with pool.acquire() as conn, conn.transaction():
            result = await runner._start_subscription(
                conn, await schedules.get_schedule('s', pool=conn), now, scheduled_for=now, run_id=None,
                common={"schedule_id": "s", "scheduled_for": now, "trigger": "manual"}, timeout={})
        run = await schedules.get_run(result.run_id, pool=pool)
        assert run["status"] == "skipped_gate"
        assert run["error"] == triage_settings.TURNED_OFF
        assert await pool.fetchval("SELECT cursor_seq FROM subscriptions WHERE schedule_id = 's'") == 42
        assert await pool.fetchval("SELECT count(*) FROM subscription_runs") == 0
        assert await pool.fetchval("SELECT count(*) FROM manager_events") == 0
        processor.assert_not_awaited()
        own_apps.assert_not_awaited()
        poster.assert_not_awaited()
    run_db(check, monkeypatch)
