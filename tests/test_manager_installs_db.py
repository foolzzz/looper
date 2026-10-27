"""Manager capability ownership and authenticated API contracts on disposable PostgreSQL."""

import asyncio
from types import SimpleNamespace

import pytest

from looper.agents import api_v1, catalog, manager_installs as service
from looper.agents.v1_domains import catalog as catalog_domain
from looper.db import manager_installs as db, managers
from tests.db_support import DSN, run_db
from tests.test_api_v1_catalog_installs_db import _api

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")


async def _seed(pool):
    await pool.execute("INSERT INTO managers (id,name,system_prompt,enabled) VALUES "
                       "('manager-alpha','Alpha','Help.',true),('manager-beta','Beta','Help.',false)")
    for kind in ('plugin', 'skill', 'mcp'):
        await pool.execute("INSERT INTO library_items (id,kind,name,sha256,size_bytes,file_count,origin) "
                           "VALUES ($1,$1,$1,'synthetic',0,0,'ui')", kind)


def test_explicit_ownership_validation_inheritance_and_cleanup(monkeypatch):
    async def check(pool):
        await _seed(pool)
        for owner, item in [('manager-alpha', 'plugin'), ('manager-beta', 'skill'), ('manager', 'skill')]:
            view = await service.put_install(owner, item)
            assert [(r['item_id'], r['inherited']) for r in view['installs']] == [(item, False)]
        assert not (await service.list_installs('manager-alpha'))['installs'][0]['inherited']
        with pytest.raises(catalog.CatalogError):
            await service.put_install('manager-alpha', 'mcp')
        await pool.execute("INSERT INTO projects (id,name) VALUES ('proj-own','Own')")
        await pool.execute("UPDATE library_items SET origin='project',owner_project_id='proj-own' WHERE id='skill'")
        with pytest.raises(catalog.CatalogError):
            await service.put_install('manager-alpha', 'skill')
        await pool.execute("UPDATE library_items SET origin='ui',owner_project_id=NULL WHERE id='skill'")
        await catalog.install({'item_id': 'skill', 'scope': 'global', 'roles': ['manager']})
        with pytest.raises(catalog.CatalogConflict, match='inherited'):
            await service.put_install('manager-alpha', 'skill', False)
        with pytest.raises(catalog.CatalogConflict, match='inherited'):
            await service.delete_install('manager-alpha', 'skill')
        await service.put_install('manager-alpha', 'skill')
        await service.put_install('manager-alpha', 'skill', False)
        rows = (await service.list_installs('manager-alpha'))['installs']
        assert [(r['inherited'], r['enabled']) for r in rows if r['item_id'] == 'skill'] == [(True, True), (False, False)]
        await service.delete_install('manager-alpha', 'skill')
        assert await managers.delete_manager('manager-alpha') == 'deleted'
        assert not await pool.fetchval("SELECT EXISTS (SELECT 1 FROM manager_library_installs WHERE manager_id='manager-alpha')")
        for action in (lambda: service.list_installs('manager-alpha'), lambda: service.put_install('manager-alpha', 'skill'),
                       lambda: service.delete_install('manager-alpha', 'skill'), lambda: service.put_install('manager-missing', 'skill')):
            with pytest.raises(catalog.CatalogNotFound):
                await action()
    run_db(check, monkeypatch, timeout=20)


def test_delete_race_serializes_and_catalog_counts_cascade(monkeypatch):
    async def check(pool):
        await _seed(pool)
        # The delete waits behind an install transaction's Manager lock, then removes that install atomically.
        async with pool.acquire() as conn, conn.transaction():
            await db.put_install('manager-alpha', 'plugin', conn=conn)
            deleting = asyncio.create_task(managers.delete_manager('manager-alpha'))
            await asyncio.sleep(0)
            assert not deleting.done()
        assert await asyncio.wait_for(deleting, 5) == 'deleted'
        await pool.execute("INSERT INTO managers (id,name,system_prompt) VALUES ('manager-race','Race','Help.')")
        locked, release = asyncio.Event(), asyncio.Event()
        async def deleting_first():
            locked.set()
            await release.wait()
        deleting = asyncio.create_task(managers.delete_manager('manager-race', before_delete=deleting_first))
        await asyncio.wait_for(locked.wait(), 5)
        async with pool.acquire() as conn:
            pid = await conn.fetchval('SELECT pg_backend_pid()')
            async def install():
                async with conn.transaction():
                    await db.put_install('manager-race', 'plugin', conn=conn)
            inserting = asyncio.create_task(install())
            async def wait_for_install_lock():
                while not await pool.fetchval(
                        "SELECT wait_event_type = 'Lock' FROM pg_stat_activity WHERE pid = $1", pid):
                    assert not inserting.done(), 'install completed during the drain'
                    await asyncio.sleep(0.01)
            try:
                # Observe an actual database lock wait before allowing the drain to finish.
                await asyncio.wait_for(wait_for_install_lock(), 5)
                assert not inserting.done()
            finally:
                release.set()
                await asyncio.wait_for(asyncio.gather(deleting, inserting, return_exceptions=True), 5)
            assert deleting.result() == 'deleted'
            with pytest.raises(catalog.CatalogNotFound):
                inserting.result()
        assert await db.list_installs('manager-beta') == []
        await service.put_install('manager-beta', 'skill')
        await service.put_install('manager', 'skill')
        assert (await catalog.catalog_view())['installed_count'] == 1
        operation = SimpleNamespace(operation_id='catalog_items.delete')
        impact = await catalog_domain.impact(None, operation, {'item_id': 'skill'}, None, None)
        assert 'all 2 installations' in impact and '2 Manager installations' in impact
        await catalog.delete_item('skill')
        assert await pool.fetchval('SELECT count(*) FROM manager_library_installs') == 0
        assert (await catalog.catalog_view())['installed_count'] == 0
    run_db(check, monkeypatch, timeout=20)


def test_operator_only_closed_api_audit_and_confirm_boundary(tmp_path, monkeypatch):
    async def check(pool):
        await _seed(pool)
        async with _api(tmp_path, pool) as call:
            for action in ('list', 'put', 'delete'):
                body = {'manager_id': 'manager-alpha', **({'item_id': 'plugin'} if action != 'list' else {})}
                status, refused = await call('manager_installs.' + action, body, client=True)
                assert status == 403 and 'confirmation_id' not in refused
                operation = api_v1.find('POST', '/api/v1/manager_installs/' + action)
                assert operation.operator_only and operation.tool is None and not operation.hand
            status, _ = await call('/api/v1/confirm', {'confirmation_id': 'manager_installs.put'}, client=True)
            assert status == 404
            for bad in ({'manager_id': ''}, {'manager_id': 'manager-alpha', 'scope': 'manager'}):
                status, _ = await call('manager_installs.list', bad)
                assert status == 400
            status, installed = await call('manager_installs.put', {'manager_id': 'manager-alpha', 'item_id': 'plugin'})
            assert status == 200 and installed['result']['installs'][0]['item_id'] == 'plugin'
            assert await pool.fetchval("SELECT count(*) FROM manager_actions WHERE tool='manager_installs.put' AND status='ok'") == 1
            status, _ = await call('installs.create', {'item_id': 'skill', 'scope': 'manager'}, client=True)
            assert status == 400
            status, view = await call('manager_installs.delete', {'manager_id': 'manager-alpha', 'item_id': 'plugin'})
            assert status == 200 and view['result']['installs'] == []
    run_db(check, monkeypatch, timeout=20)
