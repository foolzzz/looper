"""Latest per-account windows, sparse updates and seven UTC days of recorded usage."""
from datetime import datetime, timedelta, timezone

import pytest

from looper.db import connection, provider_limits
from looper.agents import self_check
from tests.db_support import DSN, dropped_tables, run_db

pytestmark = pytest.mark.skipif(not DSN, reason="requires disposable LOOPER_TEST_DATABASE_URL")
NOW = datetime.now(timezone.utc)
PROVIDER = "prov-claude-subscription"


def test_sparse_updates_preserve_omitted_fields_clear_null_and_ignore_old_observations():
    async def check(pool):
        provider = "prov-signals-test"
        await pool.execute("INSERT INTO llm_providers(id,name,vendor_type,auth) "
                           "VALUES ($1,'Synthetic signals','custom','api_key')", provider)
        base = dict(status="warning", utilization=.95, resets_at=NOW + timedelta(hours=1),
                    window_minutes=300, source="codex_app_server", observed_at=NOW)
        await provider_limits.upsert(provider, "primary", base, pool=pool)
        await provider_limits.upsert(provider, "secondary", {**base, "utilization": .1}, pool=pool)
        await provider_limits.upsert(provider, "primary", dict(status="allowed", source="codex_app_server",
            observed_at=NOW + timedelta(seconds=1), resets_at=None), pool=pool)
        row = (await provider_limits.windows(provider, pool=pool))[0]
        assert row["status"] == "allowed" and row["utilization"] == .95
        assert row["resets_at"] is None and row["window_minutes"] == 300
        await provider_limits.upsert(provider, "primary", base, pool=pool)
        assert (await provider_limits.windows(provider, pool=pool))[0] == row
        assert len(await provider_limits.windows(pool=pool)) == 2
        await pool.execute("DELETE FROM llm_providers WHERE id=$1", provider)
        assert await provider_limits.windows(pool=pool) == []
    run_db(check)


def test_signal_record_reference_survives_record_deletion_as_null():
    async def check(pool):
        record = await pool.fetchval("INSERT INTO llm_records(agent_id) VALUES ('synthetic') RETURNING id")
        await provider_limits.upsert(PROVIDER, "seven_day", dict(status="allowed", source="claude_stream",
            observed_at=NOW, record_id=record), pool=pool)
        await pool.execute("DELETE FROM llm_records WHERE id=$1", record)
        assert (await provider_limits.windows(PROVIDER, pool=pool))[0]["record_id"] is None
    run_db(check)


def test_upgrade_matches_fresh_schema_and_records_its_migration_once():
    async def check(pool):
        async with dropped_tables(pool, "provider_limit_windows"):
            await pool.execute("DROP INDEX idx_llm_records_provider_day")
            async with pool.acquire() as conn:
                assert await connection.schema_change_pending(conn)
            await connection.ensure_schema(pool)
        marker = await pool.fetchrow("SELECT * FROM schema_migrations WHERE name='provider_limit_windows'")
        assert marker is not None
        assert await pool.fetchval("SELECT indisvalid FROM pg_index WHERE indexrelid="
                                   "'idx_llm_records_provider_day'::regclass")
        await connection.ensure_schema(pool)
        assert await pool.fetchrow("SELECT * FROM schema_migrations WHERE name='provider_limit_windows'") == marker
        async with pool.acquire() as conn:
            assert not await connection.schema_change_pending(conn)
    run_db(check)


def test_usage_groups_accounts_and_utc_days_without_inventing_missing_quota_counts():
    async def check(pool):
        await pool.execute("""INSERT INTO llm_records(agent_id,created_at,input_tokens,output_tokens,
            cache_read_tokens,cache_creation_tokens,cost_usd,metadata) VALUES
            ('synthetic',now(),10,3,5,2,.25,'{"profile":{"provider":{"id":"prov-claude-subscription"}}}'),
            ('synthetic',now(),NULL,NULL,NULL,NULL,NULL,'{"profile":{"provider":{"id":"prov-claude-subscription"}}}'),
            ('synthetic',now()-interval '8 days',99,99,99,99,99,'{"profile":{"provider":{"id":"prov-claude-subscription"}}}'),
            ('synthetic',now(),7,1,0,0,.1,'{"profile":{"provider":{"id":"prov-codex-login"}}}')""")
        rows = await provider_limits.usage(PROVIDER, pool=pool)
        assert len(rows) == 1
        row = rows[0]
        assert (row["runs"], row["input_tokens"], row["output_tokens"], row["cache_read_input_tokens"],
                row["cache_creation_input_tokens"], row["cost_usd"], row["quota_failures"]) == (2,10,3,5,2,.25,None)
        assert len(await provider_limits.usage(pool=pool)) == 2
        await pool.execute("ALTER TABLE llm_records ADD COLUMN IF NOT EXISTS failure_class TEXT, "
                           "ADD COLUMN IF NOT EXISTS failure_cause TEXT")
        await pool.execute("UPDATE llm_records SET failure_class='quota', failure_cause='usage_limit' "
                           "WHERE input_tokens=10")
        for mode, expected in ((None, None), ("off", None), ("invalid", None), ("record", 1), ("act", 1)):
            await pool.execute("DELETE FROM system_settings WHERE key='engine.failure_typing'")
            if mode is not None:
                await pool.execute("INSERT INTO system_settings(key,value) VALUES ('engine.failure_typing',$1)", mode)
            assert (await provider_limits.usage(PROVIDER, pool=pool))[0]["quota_failures"] == expected
            assert (await provider_limits.usage("prov-codex-login", pool=pool))[0]["quota_failures"] == (
                0 if mode in ("record", "act") else None)
        await pool.execute("ALTER TABLE llm_records DROP COLUMN failure_class CASCADE")
        assert (await provider_limits.usage(PROVIDER, pool=pool))[0]["quota_failures"] is None
    run_db(check)


def test_self_check_is_absent_when_disabled_and_warns_for_high_or_unknown_usage():
    async def check(pool):
        assert await self_check.provider_limit_checks(pool=pool) == []
        await pool.execute("INSERT INTO system_settings(key,value) VALUES ('engine.limit_signals.enabled','true')")
        assert all(row.status == self_check.WARN for row in await self_check.provider_limit_checks(pool=pool))
        await provider_limits.upsert(PROVIDER, "seven_day", dict(status="allowed", utilization=.9,
            resets_at=NOW+timedelta(days=1), source="claude_stream", observed_at=NOW), pool=pool)
        checks = await self_check.provider_limit_checks(pool=pool)
        assert next(row for row in checks if row.id == 'provider_limits:'+PROVIDER).status == self_check.WARN
        await provider_limits.upsert(PROVIDER, "seven_day", dict(status="allowed", utilization=.2,
            source="claude_stream", observed_at=NOW+timedelta(seconds=1)), pool=pool)
        checks = await self_check.provider_limit_checks(pool=pool)
        assert next(row for row in checks if row.id == 'provider_limits:'+PROVIDER).status == self_check.OK
    run_db(check)


@pytest.mark.parametrize('field,value', [('status','unsupported'),('utilization',1.01),
                                         ('window_minutes',0),('source','unsupported')])
def test_database_rejects_invalid_window_fields(field,value):
    import asyncpg

    async def check(pool):
        observation = dict(status='allowed',source='claude_stream',observed_at=NOW)
        observation[field] = value
        with pytest.raises(asyncpg.CheckViolationError):
            await provider_limits.upsert(PROVIDER,'seven_day',observation,pool=pool)
        assert await provider_limits.windows(PROVIDER,pool=pool) == []
    run_db(check)


@pytest.mark.parametrize('age,reset', [(timedelta(days=2),timedelta(days=1)),
                                      (timedelta(),-timedelta(seconds=1))])
def test_self_check_warns_on_stale_and_expired_observations(age,reset):
    async def check(pool):
        await pool.execute("INSERT INTO system_settings(key,value) VALUES ('engine.limit_signals.enabled','true')")
        await provider_limits.upsert(PROVIDER,'seven_day',dict(status='allowed',utilization=.1,
            source='claude_stream',observed_at=NOW-age,resets_at=NOW+reset),pool=pool)
        checks = await self_check.provider_limit_checks(pool=pool)
        assert next(row for row in checks if row.id == 'provider_limits:'+PROVIDER).status == self_check.WARN
    run_db(check)


def test_operator_http_limits_readback_matches_dto_and_client_is_forbidden(tmp_path,monkeypatch):
    from looper.agents import api_v1
    from looper.agents.v1_domains.provider_limits import LIMITS
    from tests.db_support import v1_api

    async def check(pool):
        await pool.execute("INSERT INTO system_settings(key,value) VALUES ('engine.limit_signals.enabled','true')")
        await provider_limits.upsert(PROVIDER,'seven_day',dict(status='warning',utilization=.91,
            source='claude_stream',observed_at=NOW,resets_at=NOW+timedelta(days=1)),pool=pool)
        await pool.execute("""INSERT INTO llm_records(agent_id,input_tokens,output_tokens,cost_usd,metadata)
            VALUES ('synthetic',12,4,.05,'{"profile":{"provider":{"id":"prov-claude-subscription"}}}')""")
        async with v1_api(tmp_path,pool) as call:
            status,answer = await call('providers.limits',{'provider_id':PROVIDER})
            assert status == 200,answer
            api_v1.validate_arguments(LIMITS,answer['result'])
            row = answer['result']['providers'][0]
            assert row['windows'][0]['utilization'] == .91
            assert row['usage'][0]['runs'] == 1 and row['usage'][0]['input_tokens'] == 12
            status,answer = await call('providers.limits',{'provider_id':PROVIDER},client=True)
            assert status == 403 and 'result' not in answer
    run_db(check,monkeypatch)


def test_operator_http_setting_update_uses_operator_confirmation_and_reads_back(tmp_path,monkeypatch):
    from tests.db_support import v1_api

    async def check(pool):
        async with v1_api(tmp_path,pool) as call:
            status,answer = await call('limit_signals_settings.get')
            assert status == 200 and answer['result'] == {'enabled':False}
            status,answer = await call('limit_signals_settings.update',{'enabled':True})
            # The authenticated operator's direct request is already its confirmation.
            assert status == 200 and answer['result'] == {'enabled':True}
            status,answer = await call('limit_signals_settings.get')
            assert status == 200 and answer['result'] == {'enabled':True}
            assert await pool.fetchval("SELECT value FROM system_settings WHERE key='engine.limit_signals.enabled'") == 'true'
            status,answer = await call('limit_signals_settings.update',{'enabled':False},client=True)
            assert status == 403
            status,answer = await call('limit_signals_settings.update',{'enabled':None})
            assert status == 200 and answer['result'] == {'enabled':False}
            assert not await pool.fetchval("SELECT EXISTS(SELECT 1 FROM system_settings WHERE key='engine.limit_signals.enabled')")
    run_db(check,monkeypatch)
