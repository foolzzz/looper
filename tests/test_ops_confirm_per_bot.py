"""A confirmation waits in the chat it was asked in, and two bots of one Manager are two chats (RFC §9.1)."""

from __future__ import annotations

import asyncio
from typing import Any

from looper.agents.ops import Call, Risk, Tool, Turn
from looper.agents.ops.service import ManagerOps, Platform


def _ops(ran: list[Any]) -> ManagerOps:
    async def delete(call: Call) -> dict[str, Any]:
        ran.append(call.turn.channel_id)
        return {"deleted": True}

    async def escalate(call: Call) -> str:
        return "删除任务 t-1"

    schema = {"type": "object", "properties": {}}
    ops = ManagerOps(Platform(bus=None, task_router=None), url="http://127.0.0.1:9/", token="tok",
                     tools=(Tool("t_delete", "deletes", schema, Risk.WRITE, delete, escalate=escalate),))
    ops._working_at = ops._clock()
    return ops


async def _ask_then_confirm(ops: ManagerOps, asked_via: str | None, confirmed_via: str | None) -> dict[str, Any]:
    asking = Turn(channel="telegram", chat="42", message_id="m1", sender="42", channel_id=asked_via)
    ops.begin_turn(asking)
    asked = await ops.call("t_delete", {})
    assert asked["status"] == "needs_confirmation"
    ops.end_turn(asking)
    confirming = Turn(channel="telegram", chat="42", message_id="m2", sender="42", channel_id=confirmed_via)
    told = ops.begin_turn(confirming)
    assert bool(told) is (asked_via == confirmed_via)
    answer = await ops.call("looper_confirm", {"confirmation_id": asked["confirmation_id"]})
    ops.end_turn(confirming)
    return answer


def test_another_bot_of_the_manager_cannot_confirm_the_operation(agent_db: Any) -> None:
    ran: list[Any] = []
    answer = asyncio.run(_ask_then_confirm(_ops(ran), "channel-telegram-2", "channel-telegram-3"))
    assert answer["status"] == "error" and answer["error"]["code"] == "not_found" and ran == []


def test_the_bot_that_asked_confirms_it(agent_db: Any) -> None:
    ran: list[Any] = []
    answer = asyncio.run(_ask_then_confirm(_ops(ran), "channel-telegram-2", "channel-telegram-2"))
    assert answer["status"] == "ok" and ran == ["channel-telegram-2"]


def test_a_turn_naming_no_bot_is_keyed_by_its_channel(agent_db: Any) -> None:
    ran: list[Any] = []
    answer = asyncio.run(_ask_then_confirm(_ops(ran), None, None))
    assert answer["status"] == "ok" and ran == [None]
