"""The Lark (lark-cli) channel's settings against a real PostgreSQL, with the stand-in lark-cli: the old app keys
dropped at startup, the allowlist bound to an app on save, the channel's status with names and refused senders,
and the people search.

Runs only when LOOPER_TEST_DATABASE_URL points at a disposable database. Each test works in a new schema.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from lark_cli_stand_in import OPEN_ID, OTHER

DSN = os.environ.get("LOOPER_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")


class _Writer:
    def __init__(self) -> None:
        self.data = b""

    def write(self, data: bytes) -> None:
        self.data += data

    async def drain(self) -> None:
        pass


async def _call(method: str, path: str, body: Any = None, query: dict[str, str] | None = None) -> tuple[int, Any]:
    from looper.agents.http_server import AgentHTTPServer
    from looper.agents.tiers.task import TaskRouter

    server = AgentHTTPServer(web_adapter=SimpleNamespace(), supervisor=SimpleNamespace(),
                             task_router=TaskRouter(runtime_root=Path(tempfile.mkdtemp())))
    writer = _Writer()
    await server._route_api(method, path, query or {}, b"" if body is None else json.dumps(body).encode(), writer)
    head, _, payload = writer.data.partition(b"\r\n\r\n")
    return int(head.split()[1]), json.loads(payload)


def _run(test: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncpg

    from looper.db import connection

    async def _main() -> None:
        schema = f"looper_test_{uuid.uuid4().hex[:12]}"
        admin = await asyncpg.connect(DSN)
        await admin.execute(f'CREATE SCHEMA "{schema}"')
        pool = await asyncpg.create_pool(DSN, min_size=1, max_size=3, server_settings={"search_path": schema})
        try:
            await connection.ensure_schema(pool)
            monkeypatch.setattr(connection, "_pool", pool)
            await test(pool)
        finally:
            await pool.close()
            await admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
            await admin.close()

    asyncio.run(_main())


def test_a_lark_row_from_before_loses_its_app_keys_and_old_name(monkeypatch: pytest.MonkeyPatch,
                                                                caplog: pytest.LogCaptureFixture) -> None:
    from looper.agents.serve import seed_channel_configs

    async def test(pool: Any) -> None:
        await pool.execute(
            "INSERT INTO channel_configs (channel_id, channel_type, display_name, enabled, config) VALUES "
            "('channel-lark', 'lark', 'Lark (飞书) Bot', true, $1::jsonb)",
            json.dumps({"app_id_env": "LARK_APP_ID", "app_secret_env": "LARK_APP_SECRET",
                        "allowed_open_ids": [OPEN_ID], "description": "mine"}))
        with caplog.at_level("INFO"):
            await seed_channel_configs(pool)
            await seed_channel_configs(pool)  # once: nothing more to drop
        row = await pool.fetchrow("SELECT display_name, enabled, config FROM channel_configs "
                                  "WHERE channel_id = 'channel-lark'")
        assert (row["display_name"], row["enabled"]) == ("Lark (lark-cli)", True)
        # It gets the empty profile a new row has, so Settings → Channels shows the field.
        assert json.loads(row["config"]) == {"profile": "", "allowed_open_ids": [OPEN_ID], "description": "mine"}
        assert caplog.text.count("dropped app_id_env and app_secret_env") == 1

        # A profile chosen since is kept at the next startup.
        await pool.execute("UPDATE channel_configs SET config = config || '{\"profile\": \"work\"}'::jsonb "
                           "WHERE channel_id = 'channel-lark'")
        await seed_channel_configs(pool)
        assert json.loads(await pool.fetchval("SELECT config FROM channel_configs WHERE channel_id = 'channel-lark'")
                          )["profile"] == "work"

    _run(test, monkeypatch)


def test_saving_binds_the_allowlist_and_the_status_names_people(monkeypatch: pytest.MonkeyPatch,
                                                                lark_cli_stand_in: Path) -> None:
    from looper.agents.serve import seed_channel_configs
    from looper.db.channel_configs import record_rejected

    async def test(pool: Any) -> None:
        await seed_channel_configs(pool)
        status, view = await _call("PUT", "/api/channel-configs/channel-lark", {
            "enabled": True, "config": {"profile": "", "allowed_open_ids": [OPEN_ID], "app_id_env": "X"}})
        assert status == 200, view
        assert view["config"]["allowlist_app_id"] == "cli_main" and "app_id_env" not in view["config"]

        await record_rejected("channel-lark", OTHER)
        await record_rejected("channel-lark", OTHER, chat_id="oc_team")  # an @-mention of the bot in a group
        await record_rejected("channel-lark", OPEN_ID)  # on the allowlist meanwhile: not shown
        status, lark = await _call("GET", "/api/channels/lark/status")
        assert status == 200
        assert lark["bot"] == {"name": "HelperBot", "app_id": "cli_main", "open_id": "ou_bot0000001",
                               "profile": "cli_main"}
        assert lark["problem"] is None
        assert lark["names"] == {OPEN_ID: "Ada Lovelace", OTHER: "Alan Turing"}
        [rejected] = lark["rejected"]
        assert (rejected["open_id"], rejected["name"], rejected["count"], rejected["chat_id"]) == (
            OTHER, "Alan Turing", 2, "oc_team")
        assert set(rejected) == {"open_id", "name", "count", "last_at", "chat_id"}  # never what they wrote

        # Another app's profile, the allowlist unchanged: it does not start, and says why.
        status, view = await _call("PUT", "/api/channel-configs/channel-lark", {
            "config": {**view["config"], "profile": "other"}})
        assert status == 200 and view["config"]["allowlist_app_id"] == "cli_main"
        status, lark = await _call("GET", "/api/channels/lark/status")
        assert lark["problem"].startswith("the sender allowlist was made for app cli_main; profile other is app "
                                          "cli_other")

        status, found = await _call("GET", "/api/channels/lark/people", query={"q": "alan"})
        assert status == 200 and [p["open_id"] for p in found["people"]] == [OTHER]
        assert (await _call("GET", "/api/channels/lark/people", query={"q": ""}))[0] == 400
        status, payload = await _call("PUT", "/api/channel-configs/channel-lark", {
            "config": {"profile": "bad profile", "allowed_open_ids": [OPEN_ID]}})
        assert status == 400 and "not a lark-cli profile name" in payload["error"]

    _run(test, monkeypatch)


def test_only_the_newest_refused_senders_are_kept(monkeypatch: pytest.MonkeyPatch) -> None:
    from looper.db.channel_configs import REJECTED_KEPT, record_rejected, rejected_senders

    async def test(pool: Any) -> None:
        for n in range(REJECTED_KEPT + 5):
            await record_rejected("channel-lark", f"ou_sender{n:06d}")
        kept = await rejected_senders("channel-lark")
        assert len(kept) == REJECTED_KEPT and kept[0]["sender_id"] == f"ou_sender{REJECTED_KEPT + 4:06d}"

    _run(test, monkeypatch)


def test_a_table_from_before_loses_what_refused_senders_wrote(monkeypatch: pytest.MonkeyPatch) -> None:
    from looper.db import connection
    from looper.db.channel_configs import rejected_senders

    async def test(pool: Any) -> None:
        await pool.execute("ALTER TABLE channel_rejected_senders ADD COLUMN snippet TEXT NOT NULL DEFAULT ''")
        await pool.execute("INSERT INTO channel_rejected_senders (channel_id, sender_id, snippet) "
                           "VALUES ('channel-lark', $1, 'what they wrote')", OTHER)
        await connection.ensure_schema(pool)
        assert await pool.fetchval("SELECT count(*) FROM information_schema.columns WHERE table_schema = "
                                   "current_schema() AND table_name = 'channel_rejected_senders' "
                                   "AND column_name = 'snippet'") == 0
        assert [r["sender_id"] for r in await rejected_senders("channel-lark")] == [OTHER]

    _run(test, monkeypatch)
