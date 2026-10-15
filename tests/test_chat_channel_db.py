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


def test_a_telegram_resume_brief_excludes_the_current_message() -> None:
    """The message a turn answers must not also appear as history, for Telegram/Lark too
    (they have no session_id; the exclusion keys on <channel>:<chat_id>)."""
    import tempfile
    from pathlib import Path

    from looper.agents.message import MANAGER_ADDRESS, MessageEnvelope, MessageType, channel_address
    from looper.agents.tiers.manager import ManagerAgent, ManagerConfig
    from looper.db import chat as db_chat
    from looper.db import connection

    async def _main() -> None:
        async with _fresh_schema() as pool:
            await connection.ensure_schema(pool)
            connection._pool = pool  # the Manager reads through the default pool
            try:
                for mid, role, content in [("a", "user", "老问题"), ("b", "assistant", "老回答"),
                                           ("c", "user", "现在的消息")]:
                    await db_chat.save_message(session_id="telegram:42", message_id=mid, role=role,
                                               content=content, channel="telegram", pool=pool)
                env = MessageEnvelope.create(
                    source=channel_address("telegram", "channel-telegram"), target=MANAGER_ADDRESS,
                    message_type=MessageType.USER_MESSAGE,
                    payload={"text": "现在的消息", "channel_context": {"chat_id": "42"}}, correlation_id="c1")
                with tempfile.TemporaryDirectory() as tmp:
                    mgr = ManagerAgent(config=ManagerConfig(work_dir=Path(tmp)), runtime_root=Path(tmp))
                    blocks = await mgr._recent_conversations(env, "现在的消息")
                joined = "\n".join(blocks)
                assert "老问题" in joined  # history is shown
                assert "现在的消息" not in joined  # the current message is not duplicated
            finally:
                connection._pool = None

    asyncio.run(_main())
