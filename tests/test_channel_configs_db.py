"""The channel configuration API and its sender allowlists against a real PostgreSQL.

Runs only when LOOPER_TEST_DATABASE_URL points at a disposable database. Each
test works in a new schema of its own, dropped afterwards, seeded the way
``looper serve`` seeds it.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

DSN = os.environ.get("LOOPER_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")

OPEN_ID = "ou_7dab8a3d3cdcc9da365777c7ad535d62"


@asynccontextmanager
async def _fresh_schema() -> AsyncIterator[Any]:
    import asyncpg

    schema = f"looper_test_{uuid.uuid4().hex[:12]}"
    admin = await asyncpg.connect(DSN)
    await admin.execute(f'CREATE SCHEMA "{schema}"')
    pool = await asyncpg.create_pool(DSN, min_size=1, max_size=3, server_settings={"search_path": schema})
    try:
        yield pool
    finally:
        await pool.close()
        await admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
        await admin.close()


def _run(test: Callable[[Any], Awaitable[None]], monkeypatch: pytest.MonkeyPatch) -> None:
    """Run ``test(pool)`` on a new, seeded schema that is also the server's pool."""
    from looper.agents.serve import seed_channel_configs
    from looper.db import connection

    async def _main() -> None:
        async with _fresh_schema() as pool:
            await connection.ensure_schema(pool)
            monkeypatch.setattr(connection, "_pool", pool)
            await seed_channel_configs(pool)
            await test(pool)

    asyncio.run(_main())


class _Writer:
    def __init__(self) -> None:
        self.data = b""

    def write(self, data: bytes) -> None:
        self.data += data

    async def drain(self) -> None:
        pass


async def _call(method: str, path: str, body: Any = None) -> tuple[int, Any]:
    from looper.agents.http_server import AgentHTTPServer
    from looper.agents.tiers.task import TaskRouter

    server = AgentHTTPServer(web_adapter=SimpleNamespace(), supervisor=SimpleNamespace(),
                             task_router=TaskRouter(runtime_root=Path(tempfile.mkdtemp())))
    raw = body if isinstance(body, bytes) else (b"" if body is None else json.dumps(body).encode())
    writer = _Writer()
    await server._route_api(method, path, {}, raw, writer)
    head, _, payload = writer.data.partition(b"\r\n\r\n")
    return int(head.split()[1]), json.loads(payload)


def test_the_seeded_channels_have_empty_allowlists_and_say_why_they_do_not_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _test(pool: Any) -> None:
        status, body = await _call("GET", "/api/channel-configs")
        assert status == 200
        channels = {c["channel_id"]: c for c in body["configs"]}
        assert channels["channel-telegram"]["config"]["allowed_user_ids"] == []
        assert channels["channel-lark"]["config"]["allowed_open_ids"] == []
        for channel in channels.values():
            assert channel["enabled"] is False
            assert "allowlist is empty" in channel["start_problem"]

    _run(_test, monkeypatch)


def test_an_allowlist_round_trips_validated(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _test(pool: Any) -> None:
        status, lark = await _call("PUT", "/api/channel-configs/channel-lark", {
            "enabled": True,
            "config": {"app_id_env": "LARK_APP_ID", "allowed_open_ids": [f" {OPEN_ID} ", OPEN_ID, ""]},
        })
        assert status == 200
        assert lark["enabled"] is True
        assert lark["config"]["allowed_open_ids"] == [OPEN_ID]
        assert lark["start_problem"] is None
        # A Telegram list written the way it was stored before comes back as a list.
        status, telegram = await _call("PUT", "/api/channel-configs/channel-telegram", {
            "config": {"bot_token_env": "LOOPER_TELEGRAM_BOT_TOKEN", "allowed_user_ids": "7, 8"},
        })
        assert status == 200 and telegram["config"]["allowed_user_ids"] == ["7", "8"]

        status, again = await _call("GET", "/api/channel-configs/channel-lark")
        assert status == 200 and again["config"]["allowed_open_ids"] == [OPEN_ID] and again["start_problem"] is None
        stored = await pool.fetchval("SELECT config FROM channel_configs WHERE channel_id = 'channel-telegram'")
        assert json.loads(stored)["allowed_user_ids"] == ["7", "8"]

    _run(_test, monkeypatch)


def test_a_bad_id_is_refused_and_nothing_changes(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _test(pool: Any) -> None:
        await _call("PUT", "/api/channel-configs/channel-telegram", {"config": {"allowed_user_ids": ["7"]}})
        for config in ({"allowed_user_ids": ["7", "abc"]}, {"allowed_user_ids": ["-1001234567890"]}, ["7"]):
            status, body = await _call("PUT", "/api/channel-configs/channel-telegram",
                                       {"enabled": True, "config": config})
            assert status == 400, config
            assert body["error"]
        status, body = await _call("PUT", "/api/channel-configs/channel-lark",
                                   {"config": {"allowed_open_ids": ["on_7dab8a3d3cdcc9da365777c7ad535d62"]}})
        assert status == 400 and "is not a Lark open_id" in body["error"]
        _, telegram = await _call("GET", "/api/channel-configs/channel-telegram")
        assert telegram["enabled"] is False and telegram["config"] == {"allowed_user_ids": ["7"]}

    _run(_test, monkeypatch)


def test_an_enabled_channel_with_an_empty_list_is_saved_but_says_it_will_not_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _test(pool: Any) -> None:
        status, lark = await _call("PUT", "/api/channel-configs/channel-lark",
                                   {"enabled": True, "config": {"app_id_env": "LARK_APP_ID", "allowed_open_ids": []}})
        assert status == 200 and lark["enabled"] is True
        assert "allowlist is empty" in lark["start_problem"]
        status, _ = await _call("PUT", "/api/channel-configs/channel-none", {"enabled": True})
        assert status == 404

    _run(_test, monkeypatch)


def test_bot_token_is_stored_masked_and_never_returned(monkeypatch: pytest.MonkeyPatch) -> None:
    """The token goes to channel_secrets, not config: the API shows only has_bot_token, and
    ``***`` keeps it while ``""`` clears it. Mirrors the LLM-provider api_key contract."""
    from looper.db.channel_configs import get_bot_token

    async def _test(pool: Any) -> None:
        # No token to start with.
        status, body = await _call("GET", "/api/channel-configs/channel-telegram")
        assert status == 200 and body.get("has_bot_token") is False
        assert "bot_token" not in body["config"]

        # Store a token.
        status, body = await _call("PUT", "/api/channel-configs/channel-telegram", {"bot_token": "123:secret"})
        assert status == 200 and body["has_bot_token"] is True
        # The token value never leaves the server, in config or anywhere in the view.
        assert "123:secret" not in json.dumps(body)
        assert await get_bot_token("channel-telegram") == "123:secret"
        # It is kept out of channel_configs.config entirely.
        stored = await pool.fetchval("SELECT config FROM channel_configs WHERE channel_id = 'channel-telegram'")
        assert "bot_token" not in json.loads(stored)

        # The mask keeps the stored token; other fields can change alongside it.
        status, body = await _call("PUT", "/api/channel-configs/channel-telegram",
                                   {"bot_token": "***", "config": {"allowed_user_ids": ["7"]}})
        assert status == 200 and body["has_bot_token"] is True
        assert await get_bot_token("channel-telegram") == "123:secret"
        assert body["config"]["allowed_user_ids"] == ["7"]

        # An empty string removes it.
        status, body = await _call("PUT", "/api/channel-configs/channel-telegram", {"bot_token": ""})
        assert status == 200 and body["has_bot_token"] is False
        assert await get_bot_token("channel-telegram") is None

        # A non-string token is refused.
        status, _ = await _call("PUT", "/api/channel-configs/channel-telegram", {"bot_token": 123})
        assert status == 400

    _run(_test, monkeypatch)
