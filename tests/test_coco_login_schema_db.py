"""Coco's provider migration and Settings view on disposable PostgreSQL schemas."""

import asyncio
import re
from pathlib import Path

import asyncpg
import pytest

from looper.agents import profile_registry, self_check
from looper.agents.runtimes import coco_login
from looper.db import connection
from tests.db_support import DSN, fresh_connection, fresh_schema

pytestmark = pytest.mark.skipif(not DSN, reason="requires disposable LOOPER_TEST_DATABASE_URL")


def test_existing_registry_migrates_coco_login_once():
    async def test():
        async with fresh_connection() as conn:
            previous = (Path(__file__).parent / "fixtures" / "llm_providers_before_coco_login.sql").read_text()
            sql, count = re.subn(r"CREATE TABLE IF NOT EXISTS llm_providers \(.*?\n\);",
                                 lambda _: previous, connection.SCHEMA_PATH.read_text(), count=1, flags=re.S)
            assert count == 1
            await conn.execute(sql)
            await conn.execute("INSERT INTO schema_migrations (name) VALUES ('post_bridge_baseline')")
            await conn.execute("INSERT INTO registry_schema_version (id, version) VALUES (TRUE, 4)")
            # The old schema admits Cursor but refuses the new login pairing.
            await conn.execute("INSERT INTO llm_providers (id, name, vendor_type, auth, oauth_runtime_type) "
                               "VALUES ('saved', 'Saved login', 'cursor-login', 'oauth', 'cursor-agent')")
            before = dict(await conn.fetchrow("SELECT * FROM llm_providers WHERE id='saved'"))
            with pytest.raises(asyncpg.CheckViolationError):
                await conn.execute("INSERT INTO llm_providers (id, name, vendor_type, auth, oauth_runtime_type) "
                                   "VALUES ('new', 'New login', 'coco-login', 'oauth', 'coco')")
            assert await connection.schema_change_pending(conn)
            await connection.ensure_schema(conn)
            row = await conn.fetchrow("SELECT * FROM llm_providers WHERE id='prov-coco-login'")
            assert row is not None
            assert (row['name'], row['vendor_type'], row['auth'], row['oauth_runtime_type'],
                    row['enabled'], row['builtin']) == ('coco login', 'coco-login', 'oauth', 'coco', True, True)
            assert await conn.fetchval("SELECT base_url FROM llm_provider_endpoints "
                                       "WHERE provider_id='prov-coco-login' AND protocol='openai-responses'") == ''
            marker = await conn.fetchrow("SELECT * FROM schema_migrations WHERE name='coco_login'")
            assert marker is not None
            await connection.ensure_schema(conn)
            assert await conn.fetchval("SELECT count(*) FROM llm_providers WHERE id='prov-coco-login'") == 1
            assert await conn.fetchrow("SELECT * FROM llm_providers WHERE id='prov-coco-login'") == row
            assert await conn.fetchrow("SELECT * FROM schema_migrations WHERE name='coco_login'") == marker
            assert dict(await conn.fetchrow("SELECT * FROM llm_providers WHERE id='saved'")) == before
            assert not await connection.schema_change_pending(conn)
    asyncio.run(asyncio.wait_for(test(), 30))


def test_coco_login_rejects_a_codex_pairing():
    async def test():
        async with fresh_connection() as conn:
            await connection.ensure_schema(conn)
            # Positive control: the seeded coco pairing is accepted.
            assert await conn.fetchval("SELECT oauth_runtime_type FROM llm_providers "
                                       "WHERE id='prov-coco-login'") == 'coco'
            with pytest.raises(asyncpg.CheckViolationError):
                await conn.execute("UPDATE llm_providers SET oauth_runtime_type='codex' WHERE id='prov-coco-login'")
    asyncio.run(asyncio.wait_for(test(), 30))


def test_provider_view_reports_and_clears_a_forked_coco_login(monkeypatch, tmp_path):
    async def test():
        async with fresh_schema() as conn:
            await connection.ensure_schema(conn)
            monkeypatch.setattr(connection, '_pool', conn)
            assert profile_registry._login_modules()['coco'] is coco_login
            monkeypatch.setattr(profile_registry, '_login_modules', lambda: {'coco': coco_login})
            monkeypatch.setenv('TRAE_HOME', str(tmp_path / 'operator-home'))
            home = tmp_path / '.local' / 'coco' / 'runs' / 'example'
            (home / 'cli').mkdir(parents=True)
            (home / 'traecli.toml').write_text(coco_login.CONFIG_MARKER + '\n')
            (home / 'cli' / 'auth.json').write_text('synthetic fork')
            assert await coco_login.inspect_at_start(tmp_path)
            listed = await profile_registry.list_providers()
            provider = next(p for p in listed['providers'] if p['provider_id'] == 'prov-coco-login')
            assert provider['login']['state'] == 'unavailable'
            cleared = await profile_registry.clear_login_problem('prov-coco-login')
            assert cleared['provider']['login']['state'] == 'checking'
            await asyncio.to_thread(coco_login.cached_probe)
            listed = await profile_registry.list_providers()
            provider = next(p for p in listed['providers'] if p['provider_id'] == 'prov-coco-login')
            assert provider['login']['state'] == 'not_logged_in'
            assert await coco_login.recorded_problem() is None
            check = await self_check.coco_login_check()
            assert check.status == self_check.SKIP
            assert check.detail == 'not checked: no Agent Profile uses the coco login'
            # Doctor reads its own connection, rather than relying on the server's global pool.
            connect = asyncpg.connect
            schema = await conn.fetchval('SELECT current_schema()')

            async def isolated_connect(dsn, **kwargs):
                return await connect(dsn, **kwargs, server_settings={'search_path': schema})

            monkeypatch.setattr(asyncpg, 'connect', isolated_connect)
            facts = await self_check._database_facts(str(DSN))
            assert facts.coco_login_profiles == 0 and facts.coco_login_problem is None
    try:
        asyncio.run(asyncio.wait_for(test(), 30))
    finally:
        coco_login.forget_probe()
