"""chat_messages records the channel of each turn, and list_sessions/list_messages return it, so the
console can label every channel's conversation. Runs only with LOOPER_TEST_DATABASE_URL."""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
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


def test_a_channel_is_stored_and_returned() -> None:
    from looper.db import chat as db_chat
    from looper.db import connection

    async def _main() -> None:
        async with _fresh_schema() as pool:
            await connection.ensure_schema(pool)
            # A web turn defaults to 'web'; a telegram turn is tagged telegram under telegram:<chat>.
            await db_chat.save_message(session_id="session-web", message_id="m1", role="user",
                                       content="hi", pool=pool)
            await db_chat.save_message(session_id="telegram:42", message_id="t1", role="user",
                                       content="从 TG", channel="telegram", pool=pool)
            await db_chat.save_message(session_id="telegram:42", message_id="t1:reply", role="assistant",
                                       content="ok", channel="telegram", pool=pool)

            msgs = await db_chat.list_messages("telegram:42", pool=pool)
            assert [m["channel"] for m in msgs] == ["telegram", "telegram"]
            assert [m["role"] for m in msgs] == ["user", "assistant"]

            sessions = {s["session_id"]: s for s in await db_chat.list_sessions(pool=pool)}
            assert sessions["session-web"]["channel"] == "web"
            assert sessions["telegram:42"]["channel"] == "telegram"
            assert sessions["telegram:42"]["message_count"] == 2

    asyncio.run(_main())
