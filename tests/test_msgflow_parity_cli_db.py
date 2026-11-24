"""The parity command reads frozen database facts, including through the real CLI parser."""
import asyncio
import json
import os
import subprocess
import sys
from datetime import timedelta

import asyncpg
import pytest

from looper import cli
from looper.agents import message_stream_parity as parity
from looper.agents.source_service import run_bridge_pass
from looper.db import message_collection as collection, message_stream as store
from tests import msgflow_support as sup
from tests.db_support import DSN, fresh_database_without_recording

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")


def test_parity_cli_reports_same_frozen_facts_without_any_writes(tmp_path, capsys):
    directory = tmp_path / "collector"
    sup.append(directory, "oc_a", sup.message("m1"))

    async def prepare(url):
        from looper.db.connection import init_pool, close_pool
        pool = await init_pool(url, min_size=1, max_size=3)
        try:
            await store.save_source("src", {"ingest_mode": "bridge", "bridge_dir": str(directory),
                "bridge_start_from": "beginning", "lark_profile": "synthetic", "user_open_id": sup.ME}, db=pool)
            await run_bridge_pass(await store.get_source("src", db=pool), db=pool, now=sup.NOW)
            await pool.execute("SELECT setval(pg_get_serial_sequence('source_parity_sessions','id'),9007199254740992)")
            result = await collection.switch_mode("src", "collect", expected_generation=0, bridge_stopped=True, now=sup.NOW, db=pool)
            return result["parity_session_id"]
        finally:
            await close_pool()

    async def finish(url, session, *, drift=False):
        conn = await asyncpg.connect(url)
        try:
            async with conn.transaction():
                rows = [{"external_id": "m2" if drift else "m1", "chat_id": "oc_a",
                         "sent_at": sup.NOW - timedelta(minutes=5), "thread_id": None, "root_id": None, "parent_id": None}]
                await parity.observe_collect(conn, "src", "oc_a", rows,
                    [{"container_kind": "chat", "container_id": "oc_a", "complete": True}],
                    sup.NOW - timedelta(days=1), sup.NOW)
                await parity.finalize_session(conn, int(session))
        finally:
            await conn.close()

    async def snapshot(url):
        conn = await asyncpg.connect(url)
        try:
            tables = ("event_sources", "source_chats", "source_runs", "source_events", "source_collection_settings",
                      "source_parity_sessions", "source_parity_members", "source_parity_coverage", "source_stream_alerts")
            return {table: [dict(r) for r in await conn.fetch(f"SELECT * FROM {table} ORDER BY 1,2")] for table in tables}
        finally:
            await conn.close()

    with fresh_database_without_recording(DSN) as (url, _):
        session = asyncio.run(prepare(url))
        config = tmp_path / "ops.toml"
        config.write_text(f'[server]\nhost="127.0.0.1"\nport=28761\n[database]\nurl="{url}"\n')
        config.chmod(0o600)
        args = ["msgflow", "parity", "--config", str(config), "--runtime-root", str(tmp_path / "root")]
        assert cli.main([*args, "--json"]) == 3
        pending = json.loads(capsys.readouterr().out)
        assert pending["session"] == session == "9007199254740993"
        asyncio.run(finish(url, session))
        before = asyncio.run(snapshot(url))
        assert cli.main([*args, "--json"]) == 0
        report = json.loads(capsys.readouterr().out)
        assert report["state"] == "matched" and report["totals"]["bridge"] == 1
        assert cli.main(args) == 0
        assert "matched" in capsys.readouterr().out
        env = {**os.environ, "PATH": "/usr/bin:/bin"}
        process = subprocess.run([sys.executable, "-c", "from looper.cli import main; raise SystemExit(main())", *args, "--json"], capture_output=True, text=True, env=env, timeout=30)
        assert process.returncode == 0, process.stderr
        assert json.loads(process.stdout) == report
        for extra in ([], ["--dry-run"]):
            import_args = ["msgflow", "import", "--days", "1", *args[2:], *extra]
            assert cli.main(import_args) == 1
            error = capsys.readouterr().err
            assert "collect mode" in error and "Traceback" not in error
            rejected = subprocess.run([sys.executable, "-c", "from looper.cli import main; raise SystemExit(main())",
                *import_args], capture_output=True, text=True, env=env, timeout=30)
            assert rejected.returncode == 1 and "collect mode" in rejected.stderr and "Traceback" not in rejected.stderr
        assert asyncio.run(snapshot(url)) == before
        assert cli.main([*args, "--session", "not-an-id"]) == 2
        assert cli.main([*args, "--cursor", "bad!cursor"]) == 2
        assert cli.main([*args, "--limit", "201"]) == 2
        capsys.readouterr()
        asyncio.run(finish(url, session, drift=True))
        assert cli.main([*args, "--json"]) == 1
        assert json.loads(capsys.readouterr().out)["totals"]["bridge_only"] == 1


def test_parity_cli_missing_reference_sources_and_migration_have_distinct_exit_codes(tmp_path, capsys):
    async def execute(url, sql):
        conn = await asyncpg.connect(url)
        try:
            await conn.execute(sql)
        finally:
            await conn.close()

    with fresh_database_without_recording(DSN) as (url, _):
        config = tmp_path / "ops.toml"
        config.write_text(f'[server]\nhost="127.0.0.1"\nport=28762\n[database]\nurl="{url}"\n')
        config.chmod(0o600)
        args = ["msgflow", "parity", "--config", str(config), "--runtime-root", str(tmp_path / "root")]
        assert cli.main(args) == 2
        asyncio.run(execute(url, "INSERT INTO event_sources(id,kind,name) VALUES('first','lark_user','Synthetic')"))
        assert cli.main([*args, "--json"]) == 3
        assert json.loads(capsys.readouterr().out)["session"] is None
        assert cli.main([*args, "--session", "9223372036854775808"]) == 2
        assert cli.main([*args, "--source", "missing"]) == 2
        assert cli.main([*args, "--session", "123"]) == 2
        asyncio.run(execute(url, "INSERT INTO event_sources(id,kind,name) VALUES('second','lark_user','Synthetic')"))
        assert cli.main(args) == 2
        assert cli.main([*args, "--source", "first"]) == 3
        capsys.readouterr()
        asyncio.run(execute(url, "DROP TABLE source_parity_coverage"))
        assert cli.main([*args, "--source", "first"]) == 6
        assert "migration" in capsys.readouterr().err
