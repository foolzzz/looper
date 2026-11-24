"""Deletion from the offline CLI shows the same limits before asking for final confirmation."""

import asyncio
from datetime import datetime, timezone

import asyncpg
import pytest

from looper import cli
from looper.agents import source_service
from looper.db import message_stream as store
from tests import msgflow_support as sup
from tests.db_support import DSN, fresh_database_without_recording

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")


def test_cli_previews_cancels_and_then_deletes_one_message(tmp_path, monkeypatch, capsys):
    directory = tmp_path / "collector"
    sup.append(directory, "oc_synthetic", sup.message("synthetic-message", sent_at=datetime.now(timezone.utc)))

    async def seed(url):
        conn = await asyncpg.connect(url)
        try:
            source = await store.save_source("synthetic", {"ingest_mode": "bridge", "bridge_dir": str(directory), "bridge_start_from": "beginning"}, db=conn)
        finally:
            await conn.close()
        from looper.db.connection import init_pool, close_pool
        await init_pool(url, min_size=1, max_size=3)
        try:
            await source_service.run_bridge_pass(source)
        finally:
            await close_pool()

    async def count(url):
        conn = await asyncpg.connect(url)
        try:
            return await conn.fetchval("SELECT count(*) FROM source_events")
        finally:
            await conn.close()

    with fresh_database_without_recording(DSN) as (url, _):
        asyncio.run(seed(url))
        config = tmp_path / "server.toml"
        config.write_text(f'[database]\nurl = "{url}"\n')
        config.chmod(0o600)
        args = ["msgflow", "delete", "--message", "synthetic-message", "--runtime-root", str(tmp_path / "root"),
                "--config", str(config)]
        assert cli.main([*args[:3], " ", *args[4:]]) == 1
        assert asyncio.run(count(url)) == 1
        assert "error:" in capsys.readouterr().err
        monkeypatch.setattr("builtins.input", lambda _: "no")
        assert cli.main(args) == 1
        assert asyncio.run(count(url)) == 1
        output = capsys.readouterr().out
        assert "Processor" in output and "lark-chat" in output and "backups: 0" in output
        assert "daily message backups keep the newest 7 copies" in output and "older copies age out" in output
        monkeypatch.setattr("builtins.input", lambda _: "delete")
        assert cli.main(args) == 0
        assert asyncio.run(count(url)) == 0
        assert "Deleted 1" in capsys.readouterr().out
