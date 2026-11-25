import asyncio
from unittest.mock import AsyncMock

from looper.agents import failure_notices


def test_failure_notice_uses_saved_origin_and_deduplicates(monkeypatch):
    from looper.db import tasks, system_settings
    monkeypatch.setattr(tasks, "get_task", AsyncMock(return_value={"id": "task", "status": "waiting_user",
        "origin_channel_id": "channel-telegram", "chat_id": "synthetic-chat", "chat_kind": "chat",
        "origin_reply_route": {"chat_id": "synthetic-chat", "reply_to": "synthetic-thread"}}))
    monkeypatch.setattr(system_settings, "claim", AsyncMock(side_effect=[True, False]))
    from looper.agents.channels.base import PostReceipt
    post = AsyncMock(return_value=PostReceipt(True))
    event = {"event": "failure_hold_opened", "failure_id": 5, "step_id": "step", "kind": "environment", "cause": "login"}
    async def run():
        await failure_notices.notify("task", event, post=post)
        await failure_notices.notify("task", event, post=post)
    asyncio.run(run())
    post.assert_awaited_once()
    args, kwargs = post.await_args
    assert args[0] == "channel-telegram" and args[1].id == "synthetic-chat"
    assert kwargs["reply_to"] == "synthetic-thread"
    assert "failure_id=5" in args[2] and "Planner" in args[2]
