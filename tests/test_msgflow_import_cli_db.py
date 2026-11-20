"""``looper msgflow import`` end to end: the ops file, the database it names and the files of a collector."""

import asyncio
from datetime import datetime, timedelta, timezone

import asyncpg
import pytest

from looper import cli
from looper.db import message_stream as store
from tests import msgflow_support as sup
from tests.db_support import DSN, fresh_database_without_recording

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")


def run_cli(tmp_path, url, *argv):
    config = tmp_path / "server.toml"
    config.write_text(f'[database]\nurl = "{url}"\n')
    config.chmod(0o600)
    return cli.main(["msgflow", "import", "--runtime-root", str(tmp_path / "root"), "--config", str(config), *argv])


async def query(url, sql):
    conn = await asyncpg.connect(url)
    try:
        return await conn.fetch(sql)
    finally:
        await conn.close()


async def add_source(url, directory):
    conn = await asyncpg.connect(url)
    try:
        await store.save_source("src", {"name": "T", "ingest_mode": "bridge", "bridge_dir": str(directory)}, db=conn)
    finally:
        await conn.close()


def test_import_reports_stores_and_is_safe_to_repeat(tmp_path, capsys):
    now = datetime.now(timezone.utc)
    sup.append(tmp_path / "collector", "oc_a", sup.message("recent", sent_at=now - timedelta(days=1)),
               sup.message("old", sent_at=now - timedelta(days=30)))
    with fresh_database_without_recording(DSN) as (url, _):
        assert run_cli(tmp_path, url, "--days", "3") == 1
        assert "no message source" in capsys.readouterr().err
        asyncio.run(add_source(url, tmp_path / "collector"))
        assert run_cli(tmp_path, url, "--days", "3", "--dry-run") == 0
        dry = capsys.readouterr().out
        assert "would store 1" in dry and "skipped 1" in dry and "every file adds up" in dry
        assert asyncio.run(query(url, "SELECT count(*) AS n FROM source_events"))[0]["n"] == 0
        assert run_cli(tmp_path, url, "--days", "3") == 0
        assert "stored 1" in capsys.readouterr().out
        assert [r["external_id"] for r in asyncio.run(query(url, "SELECT external_id FROM source_events"))] == ["recent"]
        assert run_cli(tmp_path, url, "--days", "3") == 0
        assert "already stored or repeated 1" in capsys.readouterr().out


def test_import_refuses_while_the_server_holds_the_source(tmp_path, capsys):
    sup.append(tmp_path / "collector", "oc_a", sup.message("m1", sent_at=datetime.now(timezone.utc)))

    async def hold(url):
        conn = await asyncpg.connect(url)
        await store.try_lock(conn, "src")
        return conn

    with fresh_database_without_recording(DSN) as (url, _):
        asyncio.run(add_source(url, tmp_path / "collector"))
        loop = asyncio.new_event_loop()
        holder = loop.run_until_complete(hold(url))
        try:
            assert run_cli(tmp_path, url, "--days", "3") == 1
            assert "another pass" in capsys.readouterr().err
        finally:
            loop.run_until_complete(holder.close())
            loop.close()


@pytest.mark.parametrize("missing", ["table", "retention_column", "deletion_count_column"])
@pytest.mark.parametrize("dry_run", [False, True])
def test_import_refuses_an_upgrade_pending_restart(tmp_path, capsys, missing, dry_run):
    sup.append(tmp_path / "collector", "oc_a", sup.message("m1", sent_at=datetime.now(timezone.utc)))
    with fresh_database_without_recording(DSN) as (url, _):
        asyncio.run(add_source(url, tmp_path / "collector"))
        ddl = {"table": "DROP TABLE source_deletions",
               "retention_column": "ALTER TABLE event_sources DROP COLUMN retention_last_at",
               "deletion_count_column": "ALTER TABLE source_runs DROP COLUMN skipped_by_deletion"}[missing]
        asyncio.run(query(url, ddl))
        assert run_cli(tmp_path, url, "--days", "3", *(["--dry-run"] if dry_run else [])) == 1
        error = capsys.readouterr().err
        assert "start `looper serve` once to upgrade" in error
        assert asyncio.run(query(url, "SELECT count(*) AS n FROM source_events"))[0]["n"] == 0


@pytest.mark.parametrize("dry_run", [False, True])
def test_import_reports_deleted_messages_separately(tmp_path, capsys, dry_run):
    now = datetime.now(timezone.utc)
    sup.append(tmp_path / "collector", "oc_a", sup.message("deleted", sent_at=now),
               sup.message("new", sent_at=now), sup.message("new", sent_at=now))
    with fresh_database_without_recording(DSN) as (url, _):
        asyncio.run(add_source(url, tmp_path / "collector"))
        asyncio.run(query(url, "INSERT INTO source_deletions (source_id, scope, message_id) "
                              "VALUES ('src', 'message', 'deleted')"))
        assert run_cli(tmp_path, url, "--days", "3", "--per-file", *(["--dry-run"] if dry_run else [])) == 0
        output = capsys.readouterr().out
        assert "already stored or repeated 1" in output
        assert output.count("skipped by deletion 1") == 2
        assert "every file adds up" in output
        assert asyncio.run(query(url, "SELECT count(*) AS n FROM source_events"))[0]["n"] == (0 if dry_run else 1)


@pytest.mark.parametrize("days", ["0", "99999"])
def test_import_refuses_days_out_of_range(tmp_path, capsys, days):
    assert run_cli(tmp_path, "postgresql://unused", "--days", days) == 2
