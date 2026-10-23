"""Failure gates and full-archive recovery on three disposable databases.

Put real PostgreSQL clients on PATH (or set LOOPER_RECOVERY_PG_DUMP and
LOOPER_RECOVERY_PG_RESTORE). LOOPER_TEST_V2_CHECKOUT can name an independent
old-layout patch checkout whose initializer supports registry version 2;
otherwise the pinned v2 source receives the minimal forward-guard backport.
No fake PostgreSQL tools or current initializer stand in for that release.
"""
from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import shutil
import subprocess
import sys
import tarfile
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import asyncpg
import pytest

from looper import ops_config
from looper.agents import db_backup
from looper.agents.serve import _back_up_before_schema_change
from looper.db import connection, registry_migration
from tests.test_db_backup import _backups, _install, _install_docker, _docker_calls, _serve
from tests.test_llm_registry_rehearsal_db import _dsn, _history, _snapshot_databases
from tests.test_registry_migration_report_db import state
from tests.test_profile_rename_migration_db import _structure as _raw_structure

DSN = os.environ.get("LOOPER_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not DSN, reason="requires a disposable LOOPER_TEST_DATABASE_URL")

_VERSION_TWO = """
CREATE TABLE registry_schema_version (
    id BOOLEAN PRIMARY KEY CHECK (id), version INTEGER NOT NULL CHECK (version > 0));
INSERT INTO registry_schema_version VALUES (TRUE, 2);
"""
_DENY_DDL = """
CREATE FUNCTION reject_recovery_ddl() RETURNS event_trigger LANGUAGE plpgsql AS $$
BEGIN RAISE EXCEPTION 'recovery DDL tripwire'; END $$;
CREATE EVENT TRIGGER recovery_ddl_tripwire ON ddl_command_start
EXECUTE FUNCTION reject_recovery_ddl();
"""

# This driver imports the old patch in a separate interpreter, including its
# initializer, profile reader, adapter bindings and historical cost reader.
# Captures stay in mode-0600 files; process output contains no registry rows.
_OLD_DRIVER = r'''
import asyncio, json, os, sys
from dataclasses import asdict, replace
from pathlib import Path
checkout, dsn, output = sys.argv[1:]
sys.path.insert(0, checkout)
from looper.db import connection, profile_registry, llm_records
from looper.agents.profile_registry import AgentProfile
from looper.agents.runtimes import ADAPTERS
from looper.agents.runtimes.pricing import model_price, pricer
from looper.agents.run_usage import Usage
assert Path(connection.__file__).resolve().is_relative_to(Path(checkout).resolve())
assert connection.REGISTRY_SCHEMA_VERSION == 2
async def run():
    import asyncpg
    conn = await asyncpg.connect(dsn, command_timeout=15)
    try:
        await connection.ensure_schema(conn)
        assert await conn.fetchval('SELECT version FROM registry_schema_version') == 2
        assert await conn.fetchval("SELECT to_regclass('llm_models')") is None
        profiles = await profile_registry.list_profiles(pool=conn)
        assert len(profiles) == 20
        fixtures = []
        usage = Usage(input_tokens=1000, output_tokens=100, cache_read_tokens=200, cache_creation_tokens=50)
        for row in sorted(profiles, key=lambda r: r['id']):
            profile = AgentProfile.from_row(row)
            provider = replace(profile.provider, api_key=await profile_registry.get_api_key(
                profile.provider.id, pool=conn))
            adapter = ADAPTERS[profile.runtime.type]
            binding = adapter.bind_provider(replace(provider, api_key=provider.api_key or 'recovery-missing'),
                                            profile.model, profile.effort or '')
            price = pricer(provider)
            fixtures.append([row, asdict(binding), adapter.cost_source(provider),
                             [[name, asdict(model_price(provider, name)), price(name, usage)]
                              for name in sorted({profile.model, 'seed-unknown', *provider.models})]])
        tasks = [r['id'] for r in await conn.fetch('SELECT id FROM tasks ORDER BY id')]
        history = [await llm_records.task_costs(tasks, pool=conn),
                   await llm_records.spend_since(None, pool=conn)]
        assert len(history[0]) == 24 and history[1]['runs'] == 144
        data = json.dumps({'profiles': fixtures, 'history': history}, default=lambda v: sorted(v), sort_keys=True)
        fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as out:
            out.write(data)
    finally:
        await conn.close()
asyncio.run(asyncio.wait_for(run(), 45))
'''


@pytest.fixture(scope="module")
def old_patch(tmp_path_factory):
    value = os.environ.get("LOOPER_TEST_V2_CHECKOUT")
    if value:
        path = Path(value).resolve()
    else:
        # The released v2.12.2 lacks the forward guard. Rehearse the exact
        # backport on the independently pinned old implementation, never by
        # changing the current initializer's supported-version constant.
        path = tmp_path_factory.mktemp("guarded-v2")
        revision = "252e736725c427d4b19e3a427e9f044077781ef8"
        archived = subprocess.run(["git", "archive", revision, "looper"], cwd=Path(__file__).resolve().parents[1],
                                  capture_output=True, timeout=15, check=True)
        with tarfile.open(fileobj=io.BytesIO(archived.stdout)) as archive:
            archive.extractall(path, filter="data")
        source = path / "looper/db/connection.py"
        text = source.read_text()
        guard = '''
REGISTRY_SCHEMA_VERSION = 2

class RegistrySchemaVersionError(RuntimeError):
    pass

async def check_registry_schema_version(conn):
    if await conn.fetchval("SELECT to_regclass('registry_schema_version') IS NOT NULL"):
        version = await conn.fetchval("SELECT version FROM registry_schema_version WHERE id = TRUE")
        if version is None:
            raise RegistrySchemaVersionError("registry schema version is missing; initialization refused")
        if version > REGISTRY_SCHEMA_VERSION:
            raise RegistrySchemaVersionError(
                f"registry schema version {version} exceeds supported version 2; startup refused")

'''
        anchor = "async def ensure_schema("
        assert text.count(anchor) == 1
        text = text.replace(anchor, guard + anchor, 1)
        anchor = '        await conn.execute(f"SELECT pg_advisory_xact_lock({_SCHEMA_LOCK});\\n{sql}")'
        assert text.count(anchor) == 1
        text = text.replace(anchor, '''        await conn.execute(f"SELECT pg_advisory_xact_lock({_SCHEMA_LOCK})")
        await check_registry_schema_version(conn)
        await conn.execute(sql)
        await conn.execute("CREATE TABLE IF NOT EXISTS registry_schema_version ("
                           "id BOOLEAN PRIMARY KEY CHECK (id), version INTEGER NOT NULL CHECK (version > 0)); "
                           "INSERT INTO registry_schema_version VALUES (TRUE, 2) ON CONFLICT (id) DO NOTHING")''')
        source.write_text(text)
    assert (path / "looper/db/connection.py").is_file(), "old patch checkout is missing"
    assert path != Path(__file__).resolve().parents[1], "the current checkout is not the old patch"
    return path


@pytest.fixture
def pg_clients():
    dump = os.environ.get("LOOPER_RECOVERY_PG_DUMP") or shutil.which("pg_dump")
    restore = os.environ.get("LOOPER_RECOVERY_PG_RESTORE") or shutil.which("pg_restore")
    if not dump or not restore:
        pytest.skip("requires real pg_dump and pg_restore for full database recovery")
    for program in (dump, restore):
        result = subprocess.run([program, "--version"], capture_output=True, text=True, timeout=15, check=True)
        assert "(PostgreSQL)" in result.stdout
    return dump, restore


async def _old_start(checkout, dsn, tmp_path, label):
    driver = tmp_path / "old_driver.py"
    driver.write_text(_OLD_DRIVER)
    output = tmp_path / f"{label}.json"
    env = {**os.environ, "HOME": str(tmp_path / "home")}
    result = await asyncio.to_thread(
        subprocess.run, [sys.executable, "-I", str(driver), str(checkout), dsn, str(output)],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    return json.loads(output.read_text())


@asynccontextmanager
async def _connect(dsn):
    conn = await asyncpg.connect(dsn, command_timeout=15)
    try:
        yield conn
    finally:
        await conn.close()


async def _block_ddl(conn):
    await conn.execute(_DENY_DDL)
    # Positive control: a silently inactive event trigger is not evidence.
    with pytest.raises(asyncpg.RaiseError, match="recovery DDL tripwire"):
        await conn.execute("CREATE TABLE recovery_tripwire_probe (id integer)")


async def _structure(conn):
    structure = await _raw_structure(conn)
    # pg_restore reparses CHECK expressions and can flatten nested AND nodes.
    # PostgreSQL's pretty deparser removes only redundant parentheses while
    # retaining precedence; raw expression-tree formatting is not a contract.
    structure["constraints"] = [tuple(r) for r in await conn.fetch(
        "SELECT conrelid::regclass::text, conname, pg_get_constraintdef(oid, true) FROM pg_constraint "
        "WHERE connamespace = current_schema()::regnamespace ORDER BY 1, 2")]
    structure["sequences"] = [tuple(r) for r in await conn.fetch(
        "SELECT sequencename, last_value FROM pg_sequences WHERE schemaname = current_schema() ORDER BY 1")]
    return structure


@pytest.mark.parametrize("failure", ["dump", "verification", "skip-backup"])
def test_registry_backup_failure_refuses_ready_before_any_ddl(tmp_path, failure):
    async def run():
        async with _snapshot_databases() as (_, target, target_dsn):
            await target.execute(_VERSION_TWO)
            before = await state(target)
            await _block_ddl(target)
            _install(tmp_path / "bin", mode="exit" if failure == "dump" else "dump",
                     restore_ok=failure != "verification")
            flags = ("--skip-backup",) if failure == "skip-backup" else ()
            code, log = await asyncio.to_thread(_serve, tmp_path, target_dsn, *flags)
            assert code == 1
            assert "recovery DDL tripwire" not in log
            assert "schema" in log.lower()
            assert await state(target) == before
            assert _backups(tmp_path) == []
    asyncio.run(asyncio.wait_for(run(), 100))


def test_registry_upgrade_with_a_docker_backup_passes_the_gate_without_local_pg_restore(tmp_path):
    """The documented Docker setup has no local pg_restore: the dump is listed inside the container, with a stand-in
    ``docker`` on PATH (the real one is never called)."""
    calls = _install_docker(tmp_path / "bin")

    async def run():
        async with _snapshot_databases() as (_, target, target_dsn):
            await target.execute(_VERSION_TWO)
            code, log = await asyncio.to_thread(
                _serve, tmp_path, target_dsn,
                LOOPER_BACKUP_CMD="docker exec -e A=b pgc pg_dump --format=custom -U {user} {dbname}")
            assert code is None, log
            assert "phase=backup_verified" in log and "pg_restore is required" not in log
            assert await target.fetchval("SELECT version FROM registry_schema_version") == 3
    asyncio.run(asyncio.wait_for(run(), 150))
    assert [call["argv"][:5] for call in _docker_calls(calls)][-1] == ["exec", "-i", "pgc", "pg_restore", "--list"]
    assert len(_backups(tmp_path)) == 1


def test_failure_after_conversion_rolls_back_every_ddl_row_and_marker(monkeypatch):
    async def run():
        async with _snapshot_databases() as (source, target, target_dsn):
            await source.execute(_VERSION_TWO)
            await target.execute(_VERSION_TWO)
            before = await state(source)
            structure = await _structure(source)
            assert await state(target) == before
            original = registry_migration.apply
            reached = False

            async def fail_after_writes(conn):
                nonlocal reached
                await original(conn)
                assert await conn.fetchval("SELECT count(*) FROM llm_models") == 27
                assert await conn.fetchval("SELECT count(*) FROM agent_profiles WHERE model_id IS NOT NULL") == 20
                reached = True
                raise RuntimeError("injected conversion failure")

            monkeypatch.setattr(registry_migration, "apply", fail_after_writes)
            with pytest.raises(RuntimeError, match="injected conversion failure"):
                await connection.ensure_schema(target)
            assert reached and not target.is_in_transaction()
            # Read through a new connection: no uncommitted or cached view can
            # conceal a partial migration, fingerprint or version update.
            async with _connect(target_dsn) as readback:
                assert await state(readback) == before
                assert await _structure(readback) == structure
                assert await readback.fetchval("SELECT version FROM registry_schema_version") == 2
                assert await readback.fetchval("SELECT to_regclass('llm_models')") is None
                assert await readback.fetchval("SELECT to_regclass('llm_provider_endpoints')") is None
                assert await readback.fetchval("SELECT to_regclass('llm_model_migration_map')") is None
                assert await connection.schema_change_pending(readback)
            assert await state(source) == before
    asyncio.run(asyncio.wait_for(run(), 90))


def test_full_backup_restores_old_patch_in_isolation_and_keeps_new_writes(tmp_path, old_patch, pg_clients, caplog):
    caplog.set_level(logging.INFO, logger="looper.agents.serve")
    async def run():
        async with _snapshot_databases() as (source, target, target_dsn):
            await source.execute(_VERSION_TWO)
            await target.execute(_VERSION_TWO)
            source_before = await state(source)
            expected = await _old_start(old_patch, target_dsn, tmp_path, "before")
            before = await state(target)
            structure = await _structure(target)
            history = await _history(target)
            assert len(history[0]) == 24 and history[1]["runs"] == 144
            assert await connection.schema_change_pending(target)
            dump, restore = pg_clients
            ops = ops_config.load(ops_config.Flags(runtime_root=tmp_path),
                                  environ={"LOOPER_DATABASE_URL": target_dsn, "LOOPER_PG_DUMP": dump},
                                  dotenv=tmp_path / "absent.env")
            pool = await asyncpg.create_pool(target_dsn, min_size=1, max_size=2, command_timeout=15)
            try:
                await _back_up_before_schema_change(pool, ops, tmp_path, skip_backup=False)
            finally:
                await pool.close()
            [archive] = (tmp_path / "backups").glob("*.dump")
            assert archive.stat().st_mode & 0o777 == 0o600
            messages = [r.getMessage() for r in caplog.records if r.name == "looper.agents.serve"]
            assert len(messages) == 2
            assert "phase=backup " in messages[0] and "phase=backup_verified " in messages[1]
            for message in messages:
                assert "version=2" in message and "rollback=not_needed" in message
                assert not any(value in message for value in (target_dsn, str(archive), "sk-synthetic", "seed-"))
            await connection.ensure_schema(target)
            assert await target.fetchval("SELECT version FROM registry_schema_version") == 3
            assert await _history(target) == history
            # An accepted write after commit must survive the entire recovery.
            await target.execute("UPDATE projects SET description = 'accepted after migration' WHERE id = 'proj-seed-1'")
            assert await target.fetchval("SELECT count(*) FROM projects WHERE description = 'accepted after migration'") == 1
            migrated = await state(target)
            name = f"looper_recovery_{uuid.uuid4().hex}"
            admin = await asyncpg.connect(DSN, command_timeout=15)
            try:
                await admin.execute(f'CREATE DATABASE "{name}"')
                recovery_dsn = _dsn(name)
                bare, password, _, _ = db_backup._connection(recovery_dsn)
                result = await asyncio.to_thread(
                    subprocess.run, [restore, "--exit-on-error", "--no-owner", "--no-password", f"--dbname={bare}", str(archive)],
                    env={**os.environ, **({"PGPASSWORD": password} if password else {})},
                    capture_output=True, timeout=60)
                assert result.returncode == 0, "real pg_restore failed"
                async with _connect(recovery_dsn) as recovered:
                    assert await state(recovered) == before
                    restored_structure = await _structure(recovered)
                    for kind, definitions in structure.items():
                        assert set(restored_structure[kind]) == set(definitions), (
                            kind, set(definitions) - set(restored_structure[kind]),
                            set(restored_structure[kind]) - set(definitions))
                    assert await _history(recovered) == history
                    actual = await _old_start(old_patch, recovery_dsn, tmp_path, "recovered")
                    assert actual == expected
                    assert await recovered.fetchval("SELECT version FROM registry_schema_version") == 2
                    assert await recovered.fetchval("SELECT to_regclass('llm_models')") is None
                assert await state(target) == migrated
                assert await state(source) == source_before
            finally:
                await admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
                await admin.close()
    asyncio.run(asyncio.wait_for(run(), 180))
