"""Lookback subscriptions on disposable PostgreSQL: windows, local dates and daily reservations."""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest

from looper.agents import processor
from looper.db import connection, subscriptions as db_subs
from tests.db_support import DSN
from tests.subscription_support import (SUBSCRIPTION, UPDATE, add, confirm, cursor, fire,
                                        latest, minutes_ago, scenario, source, started)

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")
LOOKBACK = {"window": "lookback", "max_age_minutes": 1440}
DAILY = {**LOOKBACK, "delivery_policy": "daily"}


@pytest.mark.parametrize("rules", [[], [{"name": "changes", "chat_kinds": ["group"]}]])
def test_lookback_ignores_cursor_selects_sent_time_bounds_and_caps_newest_in_oldest_first_order(
        monkeypatch, tmp_path, rules):
    async def check(env, poster, fake):
        await source(env.pool)
        await add(env.pool, "old", sent_at=minutes_ago(env, 1440.01))
        await add(env.pool, "boundary", sent_at=minutes_ago(env, 1440))
        await add(env.pool, "earlier", sent_at=minutes_ago(env, 12))
        await add(env.pool, "newer", sent_at=minutes_ago(env, 5))
        await add(env.pool, "future", sent_at=minutes_ago(env, -1))
        await add(env.pool, "uncommitted", sent_at=minutes_ago(env, 1), commit=False)
        sid = await started(env, subscription={**LOOKBACK, "max_events": 2, "rules": rules, "render": "lines_v2"})
        # A cursor beyond every event must not hide the lookback window or move when it ends.
        await env.pool.execute("UPDATE subscriptions SET cursor_seq = 999999 WHERE schedule_id = $1", sid)
        before = await db_subs.get(sid)
        await confirm(env, sid)
        await env.scheduler.run_now(sid)
        await env.scheduler.idle()
        run = await latest(env, sid)
        assert run["stream"]["events_selected"] == 2
        assert run["stream"]["events_over_cap"] == 1
        assert run["stream"]["events_in_window"] == 3
        assert run["stream"]["events_too_old"] == 0
        lines = fake.calls[0]["input_text"].splitlines()
        assert lines[0] == "消息(共 2 条):"
        assert lines[1].endswith(": earlier") and lines[2].endswith(": newer")
        after = await db_subs.get(sid)
        assert (after["cursor_seq"], after["cursor_at"]) == (before["cursor_seq"], before["cursor_at"])
        assert len(poster.sent) == 1

    scenario(check, monkeypatch, tmp_path)


def test_lookback_includes_age_boundary_and_equal_time_order_is_stable(monkeypatch, tmp_path):
    async def check(env, poster, fake):
        sid = await started(env, subscription=LOOKBACK)
        await add(env.pool, "boundary", sent_at=minutes_ago(env, 1440))
        await add(env.pool, "same time first", sent_at=minutes_ago(env, 5))
        await add(env.pool, "same time second", sent_at=minutes_ago(env, 5))
        await env.scheduler.run_now(sid)
        await env.scheduler.idle()
        lines = fake.calls[0]["input_text"].splitlines()
        assert [line.rsplit(": ", 1)[-1] for line in lines[1:]] == [
            "boundary", "same time first", "same time second"]

    scenario(check, monkeypatch, tmp_path)


@pytest.mark.parametrize("reply,failure", [("SKIP", None), ("digest", processor.ProcessorFailure.TIMEOUT)])
def test_lookback_empty_quiet_and_failure_never_advance_cursor(monkeypatch, tmp_path, reply, failure):
    async def check(env, poster, fake):
        sid = await started(env, subscription=LOOKBACK)
        before = await db_subs.get(sid)
        await add(env.pool, "too old", sent_at=minutes_ago(env, 1500))
        assert (await fire(env, sid))["status"] == "skipped_gate"
        assert not fake.calls
        await add(env.pool, "fresh", sent_at=minutes_ago(env, 5))
        fake.reply, fake.failure = reply, failure
        await fire(env, sid)
        after = await db_subs.get(sid)
        assert (after["cursor_seq"], after["cursor_at"]) == (before["cursor_seq"], before["cursor_at"])
        assert not poster.sent

    scenario(check, monkeypatch, tmp_path)


@pytest.mark.parametrize("zone,expected", [("Pacific/Kiritimati", "2026-01-16"),
                                            ("America/Los_Angeles", "2026-01-15")])
@pytest.mark.parametrize("rules", [[], [{"name": "all"}]])
def test_header_date_uses_firing_time_in_subscription_timezone_with_or_without_rules(
        monkeypatch, tmp_path, zone, expected, rules):
    async def check(env, poster, fake):
        header = "Daily {date}" + (" · {count}" if rules else "")
        sid = await started(env, timezone=zone, subscription={**DAILY, "output_header": header, "rules": rules})
        await confirm(env, sid)
        await add(env.pool, "change", sent_at=minutes_ago(env, 5))
        async def during():
            env.clock.now += timedelta(days=1)
        fake.during = during
        run = await fire(env, sid)
        assert run["stream"]["output_text"].startswith(f"Daily {expected}" + (" · 1" if rules else "") + "\n\n")
        assert len(poster.sent) == 1

    scenario(check, monkeypatch, tmp_path)


def test_daily_guard_covers_manual_overlapping_fires_and_catchup_then_allows_next_local_day(monkeypatch, tmp_path):
    async def check(env, poster, fake):
        sid = await started(env, subscription=DAILY, cron="0 9 * * *", catch_up="once", overlap="queue")
        await confirm(env, sid)
        await add(env.pool, "first report", sent_at=minutes_ago(env, 5))
        # Two manual requests race; a queued request must be checked when it actually starts.
        await asyncio.gather(env.scheduler.run_now(sid), env.scheduler.run_now(sid))
        await env.scheduler.idle()
        await env.scheduler.tick()
        await env.scheduler.idle()
        await env.scheduler.run_now(sid)
        await env.scheduler.idle()
        assert len(fake.calls) == len(poster.sent) == 1
        # A stale next fire after downtime is still today's one reservation.
        await env.pool.execute("UPDATE schedules SET next_fire_at = $2, last_fired_at = NULL WHERE id = $1",
                               sid, env.clock.now - timedelta(days=3))
        await env.scheduler.tick()
        await env.scheduler.idle()
        assert len(fake.calls) == len(poster.sent) == 1
        assert await env.pool.fetchval("SELECT count(*) FROM subscription_daily_claims WHERE schedule_id = $1", sid) == 1
        env.clock.now += timedelta(days=1)
        await add(env.pool, "next day", sent_at=minutes_ago(env, 1))
        await env.scheduler.run_now(sid)
        await env.scheduler.idle()
        assert len(fake.calls) == len(poster.sent) == 2
        assert await cursor(env, sid) == 0

    scenario(check, monkeypatch, tmp_path)


@pytest.mark.parametrize("outcome", ["quiet", "failed", "uncertain", "dashboard_only"])
def test_daily_reservation_survives_restart_and_every_attempt_outcome(monkeypatch, tmp_path, outcome):
    from tests.subscription_support import scheduler_with

    async def check(env, poster, fake):
        sid = await started(env, subscription=DAILY)
        if outcome != "dashboard_only":
            await confirm(env, sid)
        if outcome == "quiet":
            fake.reply = "SKIP"
        if outcome == "failed":
            fake.failure = processor.ProcessorFailure.TIMEOUT
        if outcome == "uncertain":
            poster.break_after = 1
        await add(env.pool, "report", sent_at=minutes_ago(env, 5))
        await fire(env, sid)
        first = await latest(env, sid)
        assert first["stream"]["outcome"] == ("failed" if outcome == "failed" else outcome)
        scheduler_with(env, poster=poster)
        await confirm(env, sid)
        await env.scheduler.run_now(sid)
        await env.scheduler.idle()
        assert len(fake.calls) == 1
        assert len(poster.sent) == (1 if outcome == "uncertain" else 0)

    scenario(check, monkeypatch, tmp_path)


def test_daily_keys_are_unique_in_database_and_independent_between_schedules(monkeypatch, tmp_path):
    import asyncpg

    async def check(env, poster, fake):
        first = await started(env, subscription=DAILY)
        second = await started(env, subscription=DAILY)
        await add(env.pool, "report", sent_at=minutes_ago(env, 5))
        await fire(env, first)
        await fire(env, second)
        assert len(fake.calls) == 2
        day = await env.pool.fetchval("SELECT local_date FROM subscription_daily_claims WHERE schedule_id = $1", first)
        with pytest.raises(asyncpg.UniqueViolationError):
            await env.pool.execute("INSERT INTO subscription_daily_claims (schedule_id, local_date) VALUES ($1, $2)",
                                   first, day)

    scenario(check, monkeypatch, tmp_path)


@pytest.mark.parametrize("patch", [{"window": "rolling"}, {"window": None}, {"delivery_policy": "weekly"},
                                  {"window": "lookback", "max_age_minutes": None},
                                  {"window": "lookback", "max_age_minutes": 10081},
                                  {"window": "lookback", "max_events": 2001}])
def test_create_and_update_reject_invalid_window_policy_and_limits(monkeypatch, tmp_path, patch):
    async def check(env, poster, fake):
        sid = await started(env, enable=False)
        status, _ = await env.call("POST", UPDATE, {"schedule_id": sid, "fields": {"subscription": patch}})
        assert status == 400
        status, _ = await env.call("POST", "/api/v1/schedule_admin/create", {"project_id": env.project_id, "fields": {
            "kind": "subscription", "name": "invalid", "cron": "0 9 * * *", "run_budget_usd": 0.5,
            "requirement": "Digest", "subscription": {**SUBSCRIPTION, **patch}}})
        assert status == 400
        assert (await db_subs.get(sid))["window"] == "cursor"

    scenario(check, monkeypatch, tmp_path)


def test_lookback_create_requires_age_and_partial_update_keeps_existing_age(monkeypatch, tmp_path):
    async def check(env, poster, fake):
        sid = await started(env, enable=False)
        fields = {key: val for key, val in {**SUBSCRIPTION, **LOOKBACK}.items() if key != "max_age_minutes"}
        status, _ = await env.call("POST", "/api/v1/schedule_admin/create", {"project_id": env.project_id, "fields": {
            "kind": "subscription", "name": "missing age", "cron": "0 9 * * *", "run_budget_usd": 0.5,
            "requirement": "Digest", "subscription": fields}})
        assert status == 400
        await env.ok("POST", UPDATE, {"schedule_id": sid, "fields": {"subscription": {"window": "lookback"}}})
        assert (await db_subs.get(sid))["max_age_minutes"] == SUBSCRIPTION["max_age_minutes"]

    scenario(check, monkeypatch, tmp_path)


def test_lookback_can_repeat_per_run_and_only_cursor_mode_initializes_or_resets_cursor(monkeypatch, tmp_path):
    async def check(env, poster, fake):
        sid = await started(env, subscription=LOOKBACK)
        await confirm(env, sid)
        seq = await add(env.pool, "rolling report", sent_at=minutes_ago(env, 1))
        await fire(env, sid)
        await fire(env, sid)
        assert len(poster.sent) == 2
        before = await db_subs.get(sid)
        assert before["cursor_seq"] == 0 and before["cursor_at"] is None
        status, _ = await env.call("POST", "/api/v1/schedule_admin/reset_cursor", {
            "schedule_id": sid, "after": minutes_ago(env, 20).isoformat()})
        assert status == 400
        await env.ok("POST", UPDATE, {"schedule_id": sid, "fields": {"subscription": {"window": "cursor"}}})
        assert await cursor(env, sid) == seq
        await fire(env, sid)
        assert len(fake.calls) == 2

    scenario(check, monkeypatch, tmp_path)


def test_a_running_window_cannot_switch_modes_or_delivery_policy(monkeypatch, tmp_path):
    async def check(env, poster, fake):
        sid = await started(env, subscription=LOOKBACK)
        await add(env.pool, "report", sent_at=minutes_ago(env, 1))
        async def during():
            for patch in ({"window": "cursor"}, {"delivery_policy": "daily"}):
                status, _ = await env.call("POST", UPDATE, {"schedule_id": sid, "fields": {"subscription": patch}})
                assert status == 409
        fake.during = during
        await fire(env, sid)
        assert await cursor(env, sid) == 0

    scenario(check, monkeypatch, tmp_path)


@pytest.mark.parametrize("patch", [{"window": "lookback"}, {"delivery_policy": "daily"}])
def test_changing_window_or_delivery_policy_clears_confirmation_and_can_be_confirmed_again(monkeypatch, tmp_path, patch):
    async def check(env, poster, fake):
        sid = await started(env)
        await confirm(env, sid)
        await env.ok("POST", UPDATE, {"schedule_id": sid, "fields": {"subscription": patch}})
        assert (await db_subs.get(sid))["target_confirmed_at"] is None
        await confirm(env, sid)
        await add(env.pool, "report", sent_at=minutes_ago(env, 1))
        await fire(env, sid)
        assert len(poster.sent) == 1

    scenario(check, monkeypatch, tmp_path)


def test_lookback_cursor_reset_is_refused_before_any_cursor_preview(monkeypatch, tmp_path):
    async def unexpected(*args, **kwargs):
        raise AssertionError("a lookback subscription must not preview a cursor")

    async def check(env, poster, fake):
        sid = await started(env, subscription=LOOKBACK)
        monkeypatch.setattr(db_subs, "cursor_after", unexpected)
        status, _ = await env.call("POST", "/api/v1/schedule_admin/reset_cursor", {
            "schedule_id": sid, "after": minutes_ago(env, 20).isoformat()})
        assert status == 400

    scenario(check, monkeypatch, tmp_path)


def test_daily_key_rolls_at_local_midnight_instead_of_utc_midnight(monkeypatch, tmp_path):
    async def check(env, poster, fake):
        sid = await started(env, subscription=DAILY, timezone="Pacific/Kiritimati")
        await confirm(env, sid)
        await add(env.pool, "report", sent_at=minutes_ago(env, 1))
        await env.scheduler.run_now(sid)
        await env.scheduler.idle()
        # UTC changes from Jan 15 to 16, but the schedule is still on local Jan 16.
        env.clock.now += timedelta(hours=5)
        await env.scheduler.run_now(sid)
        await env.scheduler.idle()
        assert len(poster.sent) == 1
        # Local midnight (10:00 UTC) starts Jan 17 while UTC remains Jan 16.
        env.clock.now += timedelta(hours=9)
        await env.scheduler.run_now(sid)
        await env.scheduler.idle()
        assert len(poster.sent) == 2

    scenario(check, monkeypatch, tmp_path)


@pytest.mark.parametrize("explicit", [False, True])
def test_default_and_explicit_cursor_gossip_keep_input_order_header_and_cursor_contract(monkeypatch, tmp_path, explicit):
    async def check(env, poster, fake):
        await source(env.pool)
        await add(env.pool, "before creation", sent_at=minutes_ago(env, 7))
        sid = await started(env, subscription={"window": "cursor"} if explicit else {})
        await confirm(env, sid)
        await add(env.pool, "earlier", sent_at=minutes_ago(env, 5))
        top = await add(env.pool, "later", sent_at=minutes_ago(env, 2))
        await fire(env, sid)
        assert fake.calls[0]["input_text"] == (
            "消息(共 2 条):\n[2026/1/15 11:58:00]《Ops room》 ou_alice: later\n"
            "[2026/1/15 11:55:00]《Ops room》 ou_alice: earlier")
        assert poster.sent[0]["text"] == SUBSCRIPTION["output_header"] + "\n\n" + fake.reply
        assert await cursor(env, sid) == top
        await fire(env, sid)
        assert len(fake.calls) == len(poster.sent) == 1

    scenario(check, monkeypatch, tmp_path)


def test_upgrade_adds_default_cursor_and_daily_guard_without_changing_existing_cursor(monkeypatch, tmp_path):
    async def check(env, poster, fake):
        sid = await started(env)
        await confirm(env, sid)
        await add(env.pool, "existing", sent_at=minutes_ago(env, 1))
        await fire(env, sid)
        before = await db_subs.get(sid)
        async with env.pool.acquire() as conn:
            await conn.execute('ALTER TABLE subscriptions DROP COLUMN "window", DROP COLUMN delivery_policy')
            await conn.execute("ALTER TABLE subscription_runs DROP COLUMN window_mode")
            await conn.execute("DROP TABLE subscription_daily_claims")
            await conn.execute("DELETE FROM schema_migrations WHERE name = 'subscription_lookback'")
            assert await connection.schema_change_pending(conn)
        await connection.ensure_schema(env.pool)
        await connection.ensure_schema(env.pool)
        after = await db_subs.get(sid)
        assert (after["window"], after["delivery_policy"]) == ("cursor", "per_run")
        assert {key: value for key, value in after.items() if key not in ("window", "delivery_policy")} == {
            key: value for key, value in before.items() if key not in ("window", "delivery_policy")}
        assert await env.pool.fetchval("SELECT to_regclass('subscription_daily_claims') IS NOT NULL")
        async with env.pool.acquire() as conn:
            assert not await connection.schema_change_pending(conn)

    scenario(check, monkeypatch, tmp_path)
