"""The Chat page's conversation list: GET /api/chat/sessions lists the Manager's chat sessions on every channel,
newest first, each labelled with its channel and its last message (Planner threads left out, a failed read a 500),
and GET /api/chat/messages returns a session's newest messages, oldest first, each with its channel.

Runs only when LOOPER_TEST_DATABASE_URL points at a disposable database. Each test works in a new
schema of its own, dropped afterwards.
"""

from __future__ import annotations

import asyncio
import json
import os
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


async def _call(method: str, path: str, root: Path, query: dict[str, str] | None = None) -> tuple[int, Any]:
    from looper.agents.http_server import AgentHTTPServer
    from looper.agents.tiers.task import TaskRouter

    server = AgentHTTPServer(web_adapter=SimpleNamespace(), supervisor=SimpleNamespace(),
                             task_router=TaskRouter(runtime_root=root))
    writer = _Writer()
    await server._route_api(method, path, query or {}, b"", writer)
    head, _, payload = writer.data.partition(b"\r\n\r\n")
    return int(head.split()[1]), json.loads(payload)


def test_sessions_list_every_channel_newest_first_with_channel_and_count(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    async def _test(pool: Any) -> None:
        from looper.db import chat as db_chat

        await db_chat.save_message(session_id="session-web", message_id="w1", role="user",
                                   content="hi", pool=pool)
        await db_chat.save_message(session_id="telegram:42", message_id="t1", role="user",
                                   content="from tg", channel="telegram", pool=pool)
        await db_chat.save_message(session_id="telegram:42", message_id="t1:reply", role="assistant",
                                   content="ok", channel="telegram", pool=pool)

        status, body = await _call("GET", "/api/chat/sessions", tmp_path)
        assert status == 200
        sessions = body["sessions"]
        assert [s["session_id"] for s in sessions] == ["telegram:42", "session-web"]
        by_id = {s["session_id"]: s for s in sessions}
        assert by_id["telegram:42"]["channel"] == "telegram"
        assert by_id["telegram:42"]["message_count"] == 2
        assert by_id["session-web"]["channel"] == "web"
        assert by_id["session-web"]["message_count"] == 1

        status, msgs = await _call("GET", "/api/chat/messages", tmp_path, {"session_id": "telegram:42"})
        assert status == 200
        assert [m["channel"] for m in msgs["messages"]] == ["telegram", "telegram"]
        assert [m["role"] for m in msgs["messages"]] == ["user", "assistant"]

    _run(_test, monkeypatch)


def test_sessions_empty_when_no_messages(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    async def _test(pool: Any) -> None:
        status, body = await _call("GET", "/api/chat/sessions", tmp_path)
        assert status == 200
        assert body["sessions"] == []

    _run(_test, monkeypatch)


def test_sessions_leave_out_planner_threads_and_show_each_last_message(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    async def _test(pool: Any) -> None:
        from looper.db import chat as db_chat

        await db_chat.save_message(session_id="lark:ou_1", message_id="l1", role="user", content="deploy status?",
                                   channel="lark", pool=pool)
        await db_chat.save_message(session_id="lark:ou_1", message_id="l1:reply", role="assistant",
                                   content="All green. " + "x" * 300, channel="lark", pool=pool)
        await db_chat.save_message(session_id="planner-task-1", message_id="p1", role="user", content="faster",
                                   pool=pool)

        status, body = await _call("GET", "/api/chat/sessions", tmp_path)
        assert status == 200
        (session,) = body["sessions"]  # the Planner thread is not a conversation with the Manager
        assert (session["session_id"], session["last_role"]) == ("lark:ou_1", "assistant")
        assert session["last_content"] == ("All green. " + "x" * 300)[:200]

    _run(_test, monkeypatch)


def test_a_long_session_returns_its_newest_messages_oldest_first(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    async def _test(pool: Any) -> None:
        from looper.db import chat as db_chat

        for n in range(250):
            await db_chat.save_message(session_id="telegram:7", message_id=f"m{n}", role="user", content=f"#{n}",
                                       channel="telegram", pool=pool)

        status, body = await _call("GET", "/api/chat/messages", tmp_path, {"session_id": "telegram:7"})
        assert status == 200
        messages = body["messages"]
        assert len(messages) == 200
        assert [m["content"] for m in messages] == [f"#{n}" for n in range(50, 250)]  # the newest, ascending

    _run(_test, monkeypatch)


def test_a_failed_read_of_the_sessions_is_a_500(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    async def _test(pool: Any) -> None:
        from looper.db import chat as db_chat

        async def broken(**_: Any) -> list[dict[str, Any]]:
            raise ConnectionError("the database went away")

        monkeypatch.setattr(db_chat, "list_sessions", broken)
        status, body = await _call("GET", "/api/chat/sessions", tmp_path)
        assert status == 500 and "ConnectionError" in body["error"]

    _run(_test, monkeypatch)

