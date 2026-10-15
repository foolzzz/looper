"""The read-only Conversations endpoint: GET /api/chat/sessions lists every channel's chat session,
newest first, each labelled with its channel, and GET /api/chat/messages returns each turn's channel.

Runs only when LOOPER_TEST_DATABASE_URL points at a disposable database. Each test works in a new
schema of its own, dropped afterwards.
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
    from looper.db import connection

    async def _main() -> None:
        async with _fresh_schema() as pool:
            await connection.ensure_schema(pool)
            monkeypatch.setattr(connection, "_pool", pool)
            await test(pool)

    asyncio.run(_main())


class _Writer:
    def __init__(self) -> None:
        self.data = b""

    def write(self, data: bytes) -> None:
        self.data += data

    async def drain(self) -> None:
        pass


async def _call(method: str, path: str, query: dict[str, str] | None = None) -> tuple[int, Any]:
    from looper.agents.http_server import AgentHTTPServer
    from looper.agents.tiers.task import TaskRouter

    server = AgentHTTPServer(web_adapter=SimpleNamespace(), supervisor=SimpleNamespace(),
                             task_router=TaskRouter(runtime_root=Path(tempfile.mkdtemp())))
    writer = _Writer()
    await server._route_api(method, path, query or {}, b"", writer)
    head, _, payload = writer.data.partition(b"\r\n\r\n")
    return int(head.split()[1]), json.loads(payload)


def test_sessions_list_every_channel_newest_first_with_channel_and_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _test(pool: Any) -> None:
        from looper.db import chat as db_chat

        await db_chat.save_message(session_id="session-web", message_id="w1", role="user",
                                   content="hi", pool=pool)
        await db_chat.save_message(session_id="telegram:42", message_id="t1", role="user",
                                   content="from tg", channel="telegram", pool=pool)
        await db_chat.save_message(session_id="telegram:42", message_id="t1:reply", role="assistant",
                                   content="ok", channel="telegram", pool=pool)

        status, body = await _call("GET", "/api/chat/sessions")
        assert status == 200
        sessions = body["sessions"]
        assert [s["session_id"] for s in sessions] == ["telegram:42", "session-web"]
        by_id = {s["session_id"]: s for s in sessions}
        assert by_id["telegram:42"]["channel"] == "telegram"
        assert by_id["telegram:42"]["message_count"] == 2
        assert by_id["session-web"]["channel"] == "web"
        assert by_id["session-web"]["message_count"] == 1

        status, msgs = await _call("GET", "/api/chat/messages", {"session_id": "telegram:42"})
        assert status == 200
        assert [m["channel"] for m in msgs["messages"]] == ["telegram", "telegram"]
        assert [m["role"] for m in msgs["messages"]] == ["user", "assistant"]

    _run(_test, monkeypatch)


def test_sessions_empty_when_no_messages(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _test(pool: Any) -> None:
        status, body = await _call("GET", "/api/chat/sessions")
        assert status == 200
        assert body["sessions"] == []

    _run(_test, monkeypatch)
