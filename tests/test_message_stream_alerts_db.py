"""Durable source alert episodes, independent of channel catch-up and model turns."""

from datetime import datetime, timedelta, timezone

import pytest

from looper.agents import chat_settings, message_stream_alerts as alerts, morning_brief, proactive_delivery
from looper.agents.channels.base import PostOutcome, PostReceipt
from looper.agents import message_stream_parity as parity
from looper.db import message_parity as evidence
from looper.db import message_stream_alerts as store
from tests import subscription_support as stream
from tests.db_support import DSN, run_db

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")
NOW = datetime(2026, 11, 22, 12, tzinfo=timezone.utc)


async def seed(pool, *, target=True):
    await stream.source(pool)
    await stream.lark_targets(pool)
    await pool.execute("UPDATE channel_configs SET enabled = true, config = config || "
                       "jsonb_build_object('operator_sender_id', $1::text) WHERE channel_id = 'channel-lark'", stream.DM)
    await pool.execute("UPDATE event_sources SET ingest_mode = 'collect', auth_state = 'ready', "
                       "alert_channel = $1, alert_chat = $2 WHERE id = $3",
                       "channel-lark" if target else None, stream.DM if target else None, stream.SOURCE)
    await pool.execute("INSERT INTO source_collection_settings "
                       "(source_id, alerts_enabled, collect_enabled_at) VALUES ($1, true, $2)", stream.SOURCE, NOW)


def test_alert_episode_and_recovery_are_durable_and_sent_once(monkeypatch):
    async def check(pool):
        await seed(pool)
        sent = []

        async def post(channel, target, text, **kwargs):
            sent.append((channel, target, text, kwargs))
            return PostReceipt(True)

        service = alerts.SourceAlerts(post)
        await service.evaluate(now=NOW + timedelta(minutes=59, seconds=59))
        assert not await pool.fetchval("SELECT count(*) FROM source_stream_alerts")
        await service.evaluate(now=NOW + timedelta(hours=1))
        await service.flush(now=NOW + timedelta(hours=1))
        await alerts.SourceAlerts(post).evaluate(now=NOW + timedelta(hours=2))
        await alerts.SourceAlerts(post).flush(now=NOW + timedelta(hours=2))
        assert len(sent) == 1 and sent[0][1].group is False
        assert sent[0][3]["idempotency_key"].endswith(":open")
        await pool.execute("UPDATE source_collection_settings SET last_collect_ok_at = $1 WHERE source_id = $2",
                           NOW + timedelta(hours=2), stream.SOURCE)
        await service.evaluate(now=NOW + timedelta(hours=2))
        await service.flush(now=NOW + timedelta(hours=2))
        await service.flush(now=NOW + timedelta(hours=2))
        assert len(sent) == 2 and sent[1][3]["idempotency_key"].endswith(":recovered")
        row = await pool.fetchrow("SELECT * FROM source_stream_alerts")
        assert row["resolved_at"] and row["open_state"] == row["recovery_state"] == "sent"

    run_db(check, monkeypatch, timeout=30)


@pytest.mark.parametrize("outcome", [PostOutcome.REJECTED, PostOutcome.UNKNOWN])
def test_receipt_and_delivery_policy_preserve_rejected_unknown_and_deferred(monkeypatch, outcome):
    async def check(pool):
        await seed(pool)
        calls = []

        async def post(*args, **kwargs):
            calls.append(kwargs)
            return PostReceipt(False, outcome=outcome)

        service = alerts.SourceAlerts(post)
        await pool.execute("UPDATE event_sources SET auth_state = 'missing' WHERE id = $1", stream.SOURCE)
        await service.evaluate(now=NOW)
        await service.flush(now=NOW)
        assert await pool.fetchval("SELECT open_state FROM source_stream_alerts") == outcome.value
        await service.flush(now=NOW)
        assert len(calls) == 1
        if outcome is PostOutcome.UNKNOWN:
            await alerts.SourceAlerts(post).flush(now=NOW + timedelta(days=1))
            assert len(calls) == 1

    run_db(check, monkeypatch, timeout=30)


def test_sending_after_restart_becomes_unknown_and_disabled_sources_write_nothing(monkeypatch):
    async def check(pool):
        await stream.source(pool)
        service = alerts.SourceAlerts()
        await service.evaluate(now=NOW)
        assert not await pool.fetchval("SELECT count(*) FROM source_stream_alerts")
        await seed(pool)
        async with pool.acquire() as conn:
            row = await store.open_episode(conn, stream.SOURCE, "auth", now=NOW)
            claim = await store.claim_notice(conn, row["id"], "open", now=NOW)
            assert claim and not await store.claim_notice(conn, row["id"], "open", now=NOW)
        await service.flush(now=NOW)
        assert await pool.fetchval("SELECT open_state FROM source_stream_alerts") == "unknown"

    run_db(check, monkeypatch, timeout=30)


def test_auth_expiry_and_partial_thresholds_are_exact(monkeypatch):
    async def check(pool):
        await seed(pool, target=False)
        service = alerts.SourceAlerts()
        await pool.execute("UPDATE event_sources SET refresh_expires_at = $1 WHERE id = $2",
                           NOW + timedelta(hours=48, microseconds=1), stream.SOURCE)
        await service.evaluate(now=NOW)
        assert not await pool.fetchval("SELECT count(*) FROM source_stream_alerts")
        await pool.execute("UPDATE event_sources SET refresh_expires_at = $1 WHERE id = $2",
                           NOW + timedelta(hours=48), stream.SOURCE)
        for n in range(3):
            await pool.execute("INSERT INTO source_runs (source_id, mode, trigger, status, started_at, finished_at) "
                               "VALUES ($1, 'collect', 'manual', 'partial', $2, $2)",
                               stream.SOURCE, NOW + timedelta(seconds=n))
            await service.evaluate(now=NOW)
            partials = await pool.fetchval("SELECT count(*) FROM source_stream_alerts WHERE kind = 'partial'")
            assert partials == int(n == 2)
        assert {row["kind"] for row in await pool.fetch("SELECT kind FROM source_stream_alerts")} == {"auth_expiring", "partial"}
        await service.flush(now=NOW)
        assert {row["open_state"] for row in await pool.fetch("SELECT open_state FROM source_stream_alerts")} == {"dashboard"}

    run_db(check, monkeypatch, timeout=30)


def test_deferred_source_alert_uses_existing_morning_brief_consumption(monkeypatch):
    from looper.db import morning_briefs

    async def check(pool):
        await seed(pool)
        calls = []

        async def post(*args, **kwargs):
            calls.append(kwargs)
            return PostReceipt(True)

        monkeypatch.setattr(chat_settings, "_delivery_enabled", True)
        policy = proactive_delivery.DeliveryPolicy(timezone.utc,
            quiet_hours=proactive_delivery.DailyWindow.parse("11:00-13:00"))
        monkeypatch.setattr(proactive_delivery, "load_policy", lambda: policy)
        await pool.execute("UPDATE event_sources SET auth_state='missing' WHERE id=$1", stream.SOURCE)
        service = alerts.SourceAlerts(post)
        await service.evaluate(now=NOW)
        await service.flush(now=NOW)
        assert calls == []
        assert await pool.fetchval("SELECT open_state FROM source_stream_alerts") == "deferred"
        row = await pool.fetchrow("SELECT * FROM proactive_deliveries")
        assert row["source"] == "message_stream_alert" and row["decision"] == "digest"
        async with pool.acquire() as conn:
            found = await morning_briefs.digests(conn, channel_id="channel-lark", chat=stream.DM,
                                                until=NOW, dates=[row["digest_date"]])
            assert len(found) == 1 and "消息流告警" in morning_brief._digest_line(found[0], timezone.utc)
            await morning_briefs._record_read(conn, "channel-lark", stream.DM, [found[0]["id"]])
            assert await morning_briefs.digests(conn, channel_id="channel-lark", chat=stream.DM,
                                               until=NOW, dates=[row["digest_date"]]) == []

    run_db(check, monkeypatch, timeout=30)


def test_receipt_database_failure_does_not_resend_and_rejected_has_finite_retries(monkeypatch):
    async def check(pool):
        await seed(pool)
        sent = []

        async def post(*args, **kwargs):
            sent.append(kwargs)
            return PostReceipt(False, outcome=PostOutcome.REJECTED)

        service = alerts.SourceAlerts(post)
        await pool.execute("UPDATE event_sources SET auth_state='missing' WHERE id=$1", stream.SOURCE)
        await service.evaluate(now=NOW)
        original = store.settle_notice
        fails = 2

        async def flaky(*args, **kwargs):
            nonlocal fails
            if fails:
                fails -= 1
                raise RuntimeError("synthetic database interruption")
            return await original(*args, **kwargs)

        monkeypatch.setattr(store, "settle_notice", flaky)
        await service.flush(now=NOW)
        assert len(sent) == 1
        for n in range(1, 8):
            await service.flush(now=NOW + timedelta(minutes=n))
        assert len(sent) == 5
        assert await pool.fetchval("SELECT open_attempts FROM source_stream_alerts") == 5

    run_db(check, monkeypatch, timeout=30)


def test_parity_drift_recovers_only_when_its_same_frozen_window_matches(monkeypatch):
    async def check(pool):
        await seed(pool, target=False)
        await stream.chat(pool, "oc_synthetic")
        service = alerts.SourceAlerts()
        start = NOW - timedelta(hours=24)
        member = {"external_id": "om_synthetic", "chat_id": "oc_synthetic", "sent_at": NOW - timedelta(hours=1),
                  "thread_id": None, "root_id": None, "parent_id": None}
        async with pool.acquire() as conn, conn.transaction():
            original = await evidence.create_session(conn, stream.SOURCE, 1, start, NOW, 0,
                                                      policy_digest="synthetic", reference_digest="synthetic")
            await evidence.put_members(conn, original, "bridge", [member])
            await evidence.set_coverage(conn, original, "oc_synthetic", "chat", "oc_synthetic", start, NOW,
                                        state="complete")
            assert (await parity.finalize_session(conn, original))["state"] == "drift"
        await service.evaluate(now=NOW)
        alert = await pool.fetchrow("SELECT * FROM source_stream_alerts")
        assert alert["parity_session_id"] == original
        assert store.view(alert)["metadata"]["bridge_only"] == 1
        async with pool.acquire() as conn:
            unrelated = await evidence.create_session(conn, stream.SOURCE, 2, NOW, NOW + timedelta(hours=24), 0,
                                                        policy_digest="synthetic", reference_digest="synthetic")
            assert (await parity.finalize_session(conn, unrelated))["state"] == "matched"
        await service.evaluate(now=NOW)
        assert await pool.fetchval("SELECT resolved_at FROM source_stream_alerts") is None
        await pool.execute("UPDATE source_parity_sessions SET state='incomplete',reason='coverage_missing' WHERE id=$1", original)
        await service.evaluate(now=NOW)
        assert await pool.fetchval("SELECT resolved_at FROM source_stream_alerts") is None
        async with pool.acquire() as conn, conn.transaction():
            await evidence.put_members(conn, original, "collect", [member])
            assert (await parity.finalize_session(conn, original))["state"] == "matched"
        await service.evaluate(now=NOW)
        assert await pool.fetchval("SELECT resolved_at FROM source_stream_alerts") == NOW

    run_db(check, monkeypatch, timeout=30)


def test_auth_failure_does_not_duplicate_collector_failure_and_ok_recovers_both(monkeypatch):
    async def check(pool):
        await seed(pool, target=False)
        service = alerts.SourceAlerts()
        for status, error in (("search_failed", "transient"), ("auth_failed", "auth_missing")):
            await pool.execute("INSERT INTO source_runs(source_id,mode,trigger,status,error,finished_at) "
                               "VALUES($1,'collect','manual',$2,$3,$4)", stream.SOURCE, status, error, NOW)
            await service.evaluate(now=NOW)
            await service.evaluate(now=NOW)
        rows = await pool.fetch("SELECT kind, resolved_at FROM source_stream_alerts")
        assert {row["kind"] for row in rows} == {"auth", "collector_failure"}
        assert all(row["resolved_at"] is None for row in rows)
        await pool.execute("UPDATE event_sources SET auth_state='needs_refresh',auth_checked_at=$1 WHERE id=$2",
                           NOW + timedelta(seconds=1), stream.SOURCE)
        await service.evaluate(now=NOW + timedelta(seconds=1))
        assert await pool.fetchval("SELECT resolved_at FROM source_stream_alerts WHERE kind='auth'") is not None
        assert await pool.fetchval("SELECT resolved_at FROM source_stream_alerts WHERE kind='collector_failure'") is None
        await pool.execute("INSERT INTO source_runs(source_id,mode,trigger,status,finished_at) "
                           "VALUES($1,'collect','manual','ok',$2)", stream.SOURCE, NOW)
        await service.evaluate(now=NOW)
        assert await pool.fetchval("SELECT count(*) FROM source_stream_alerts WHERE resolved_at IS NULL") == 0

    run_db(check, monkeypatch, timeout=30)


def test_changed_target_is_dashboard_only_and_channel_alias_is_normalized(monkeypatch):
    async def check(pool):
        await seed(pool)
        await pool.execute("UPDATE event_sources SET alert_channel='lark',auth_state='missing' WHERE id=$1", stream.SOURCE)
        service = alerts.SourceAlerts(lambda *args, **kwargs: pytest.fail("invalidated targets must never send"))
        await service.evaluate(now=NOW)
        assert await pool.fetchval("SELECT target_channel FROM source_stream_alerts") == "channel-lark"
        await pool.execute("UPDATE event_sources SET alert_channel=NULL,alert_chat=NULL WHERE id=$1", stream.SOURCE)
        await service.flush(now=NOW)
        assert await pool.fetchval("SELECT open_state FROM source_stream_alerts") == "dashboard"

    run_db(check, monkeypatch, timeout=30)


def test_failed_delivery_decision_never_calls_platform_and_can_retry(monkeypatch):
    async def check(pool):
        await seed(pool)
        called = []

        async def post(*args, **kwargs):
            called.append(kwargs)
            return PostReceipt(True)

        async def unavailable(*args, **kwargs):
            raise RuntimeError("synthetic policy read failure")

        monkeypatch.setattr(chat_settings, "_delivery_enabled", True)
        monkeypatch.setattr(proactive_delivery, "decide", unavailable)
        await pool.execute("UPDATE event_sources SET auth_state='missing' WHERE id=$1", stream.SOURCE)
        service = alerts.SourceAlerts(post)
        await service.evaluate(now=NOW)
        await service.flush(now=NOW)
        assert called == [] and await pool.fetchval("SELECT open_state FROM source_stream_alerts") == "rejected"

        async def send(*args, **kwargs):
            return proactive_delivery.Decision(proactive_delivery.Outcome.SEND, proactive_delivery.Reason.WITHIN_LIMITS)

        monkeypatch.setattr(proactive_delivery, "decide", send)
        await service.flush(now=NOW + timedelta(minutes=1))
        assert len(called) == 1 and await pool.fetchval("SELECT open_state FROM source_stream_alerts") == "sent"

    run_db(check, monkeypatch, timeout=30)
