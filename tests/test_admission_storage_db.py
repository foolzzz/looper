"""Admission persistence, operator boundaries and restart-safe freeze notices."""

from datetime import datetime, timedelta, timezone

import asyncpg
import pytest

from looper.db import connection, profile_registry, provider_admission, system_settings
from looper.agents import admission, admission_notices, self_check
from tests.db_support import DSN, run_db, v1_api

pytestmark = pytest.mark.skipif(
    not DSN, reason="requires disposable LOOPER_TEST_DATABASE_URL"
)
PROVIDER = "prov-claude-subscription"
NOW = datetime.now(timezone.utc)


def test_upgrade_recreates_missing_objects_even_with_marker_and_keeps_fresh_baseline_only():
    async def check(pool):
        assert await pool.fetchval("SELECT count(*) FROM schema_migrations") == 1
        await pool.execute(
            "INSERT INTO schema_migrations(name) VALUES ('provider_admission')"
        )
        await pool.execute("DROP TABLE provider_admission_events")
        async with pool.acquire() as conn:
            assert await connection.schema_change_pending(conn)
        await connection.ensure_schema(pool)
        async with pool.acquire() as conn:
            assert not await connection.schema_change_pending(conn)
        assert await pool.fetchval(
            "SELECT to_regclass('provider_admission_events') IS NOT NULL"
        )

    run_db(check)


def test_event_and_current_state_round_trip_and_record_delete_sets_null():
    async def check(pool):
        record = await pool.fetchval(
            "INSERT INTO llm_records(agent_id) VALUES ('synthetic') RETURNING id"
        )
        event_id = await provider_admission.save(
            PROVIDER,
            "open",
            "frozen",
            reason="quota",
            until=NOW + timedelta(hours=1),
            until_known=True,
            backoff_step=0,
            record_id=record,
            since=NOW,
            actor="code",
            pool=pool,
        )
        row = (await provider_admission.read_all(pool=pool))[0]
        assert (row["state"], row["until"], row["since"]) == (
            "frozen",
            NOW + timedelta(hours=1),
            NOW,
        )
        event = await pool.fetchrow(
            "SELECT * FROM provider_admission_events WHERE id=$1", event_id
        )
        assert (event["from_state"], event["to_state"], event["actor"]) == (
            "open",
            "frozen",
            "code",
        )
        await pool.execute("DELETE FROM llm_records WHERE id=$1", record)
        assert (await provider_admission.read_all(pool=pool))[0]["record_id"] is None
        assert (
            await pool.fetchval(
                "SELECT record_id FROM provider_admission_events WHERE id=$1", event_id
            )
            is None
        )
        await provider_admission.save(
            PROVIDER,
            "frozen",
            "open",
            reason=None,
            until=None,
            until_known=True,
            backoff_step=0,
            record_id=None,
            since=NOW,
            actor="operator",
            pool=pool,
        )
        assert (
            await pool.fetchval(
                "SELECT actor FROM provider_admission_events ORDER BY id DESC LIMIT 1"
            )
            == "operator"
        )
        with pytest.raises(asyncpg.CheckViolationError):
            await provider_admission.save(
                PROVIDER,
                "open",
                "frozen",
                reason=None,
                until=None,
                until_known=True,
                backoff_step=0,
                record_id=None,
                since=NOW,
                actor="code",
                pool=pool,
            )
        assert (
            await pool.fetchval("SELECT count(*) FROM provider_admission_events") == 2
        )

    run_db(check)


def test_settings_refuse_typing_off_and_operator_clear_and_limits_read_back(
    tmp_path, monkeypatch
):
    async def check(pool):
        await admission.set_enabled(False)
        async with v1_api(tmp_path, pool) as call:
            status, answer = await call("admission_settings.get")
            assert status == 200 and answer["result"] == {"enabled": False}
            status, answer = await call("admission_settings.update", {"enabled": True})
            assert status == 409 and answer["error"]["code"] == "conflict"
            assert not admission.enabled()
            await system_settings.put_settings({"engine.failure_typing": "record"})
            status, answer = await call(
                "admission_settings.update", {"enabled": True}, client=True
            )
            assert status == 403
            status, answer = await call("admission_settings.update", {"enabled": True})
            assert status == 200 and answer["result"] == {"enabled": True}
            await admission.transition(
                PROVIDER, admission.QuotaFailure(NOW + timedelta(hours=1), None)
            )
            status, answer = await call("providers.limits", {"provider_id": PROVIDER})
            assert (
                status == 200
                and answer["result"]["providers"][0]["admission"]["state"] == "frozen"
            )
            status, answer = await call(
                "providers.limits_clear", {"provider_id": PROVIDER}
            )
            assert status == 200 and answer["result"]["state"] == "open"
            assert (
                await pool.fetchval(
                    "SELECT actor FROM provider_admission_events ORDER BY id DESC LIMIT 1"
                )
                == "operator"
            )
            status, answer = await call(
                "providers.limits_clear", {"provider_id": "missing"}
            )
            assert status == 404
            status, answer = await call("admission_settings.update", {"enabled": None})
            assert status == 200 and answer["result"] == {"enabled": False}
        await admission.set_enabled(False)

    run_db(check, monkeypatch)


def test_freeze_notice_claim_is_per_event_and_survives_reconfiguration(monkeypatch):
    sent = []

    async def send(*args, **kwargs):
        sent.append((args, kwargs))
        return True, None

    async def check(pool):
        from looper.agents import gate_service
        from looper.agents.gate_service import GateChat
        from looper.agents.task_notice import NotifyChannel, NotifyTarget, TargetKind

        async def target():
            return GateChat(
                NotifyChannel.TELEGRAM,
                NotifyTarget(TargetKind.CHAT, "synthetic-chat"),
                "channel-telegram",
            )

        monkeypatch.setattr(gate_service, "dashboard_target", target)
        admission_notices.configure(send)
        for _ in range(2):
            await admission_notices.freeze_alert(
                PROVIDER,
                11,
                until=NOW + timedelta(hours=1),
                until_known=False,
                waiting=2,
            )
            admission_notices.configure(send)
        assert len(sent) == 1 and "重置时间未知" in sent[0][0][2]
        await admission_notices.freeze_alert(
            PROVIDER, 12, until=NOW + timedelta(hours=1), until_known=True, waiting=3
        )
        assert len(sent) == 2

    run_db(check, monkeypatch)


def test_self_check_absent_when_off_and_warns_for_typing_or_overadmissions(monkeypatch):
    async def check(pool):
        assert await self_check.admission_checks(pool=pool) == []
        await system_settings.put_settings(
            {"engine.admission.enabled": "true"}, pool=pool
        )
        assert (await self_check.admission_checks(pool=pool))[
            0
        ].status == self_check.WARN
        await system_settings.put_settings(
            {"engine.failure_typing": "record"}, pool=pool
        )
        monkeypatch.setattr(admission, "daily_overadmissions", lambda: 3)
        assert (await self_check.admission_checks(pool=pool))[0].status == self_check.OK
        monkeypatch.setattr(admission, "daily_overadmissions", lambda: 4)
        assert (await self_check.admission_checks(pool=pool))[
            0
        ].status == self_check.WARN

    run_db(check, monkeypatch)


def test_hourly_pruning_retains_recent_events_and_current_freeze():
    async def check(pool):
        for age in (91, 89):
            await pool.execute(
                "INSERT INTO provider_admission_events(provider_id,from_state,to_state,actor,created_at) "
                "VALUES ($1,'open','frozen','code',now()-$2*interval '1 day')",
                PROVIDER,
                age,
            )
        await provider_admission.save(
            PROVIDER,
            "open",
            "frozen",
            reason="quota",
            until=NOW + timedelta(days=1),
            until_known=True,
            backoff_step=0,
            record_id=None,
            since=NOW,
            actor="code",
            pool=pool,
        )
        assert await provider_admission.prune(pool=pool) == 1
        assert (
            await pool.fetchval("SELECT count(*) FROM provider_admission_events") == 2
        )
        assert (await provider_admission.read_all(pool=pool))[0]["state"] == "frozen"

    run_db(check)


def test_limits_off_keeps_fixed_baseline_and_never_queries_admission(monkeypatch):
    # Recorded baseline: fc9cfc7f2, before account-admission implementation.
    from looper.agents import limit_signals

    async def check(pool):
        await admission.set_enabled(False)
        accounts = await profile_registry.list_providers()

        async def forbidden(*args, **kwargs):
            raise AssertionError("disabled admission must not read its state")

        monkeypatch.setattr(provider_admission, "read_all", forbidden)
        monkeypatch.setattr(admission, "snapshot", forbidden)
        actual = await limit_signals.limits()
        assert actual == {
            "enabled": False,
            "providers": [
                {
                    "provider_id": p["id"],
                    "name": p["name"],
                    "admission": None,
                    "cost_basis": "api_equivalent"
                    if p["auth"] == "oauth"
                    else "reported",
                    "windows": [],
                    "usage": [],
                }
                for p in accounts
            ],
        }

    run_db(check, monkeypatch)


def test_allowed_signal_compares_the_real_record_timestamp(monkeypatch):
    async def check(pool):
        await admission.set_enabled(True)
        try:
            await admission.transition(PROVIDER, admission.OperatorClear())
            await admission.transition(PROVIDER, admission.QuotaFailure(None, None))
            frozen_at = admission._controller.states[PROVIDER].since
            for offset, expected in ((-1, "frozen"), (1, "open")):
                record_id = await pool.fetchval(
                    "INSERT INTO llm_records(agent_id,created_at) VALUES ('synthetic-signal',$1) RETURNING id",
                    frozen_at + timedelta(seconds=offset),
                )
                await admission.observed(PROVIDER, [("seven_day", {"status": "allowed"})], record_id)
                assert (await admission.snapshot(PROVIDER))["state"] == expected
        finally:
            await admission.set_enabled(False)

    run_db(check, monkeypatch)
