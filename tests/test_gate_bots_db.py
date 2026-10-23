"""A gate's chat is matched by bot (channel id), not by channel type, on a real PostgreSQL: two bots of one type share
chat ids and Telegram's message ids, so posting, finding the gate a reply is about, "Open gates of this chat" and
``/gates`` numbering all key on ``(channel id, chat)``. Runs only when LOOPER_TEST_DATABASE_URL points at a
disposable database."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from looper.agents.message import MANAGER_ADDRESS, MessageEnvelope, MessageType, channel_address
from looper.agents.task_notice import NotifyTarget, TargetKind
from tests.db_support import DSN, run_db
from tests.test_gate_channels_db import _Bus, _Poster, _task_with_gate

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")


async def _post(message_pool: Any, step_id: str, channel: str, channel_id: str, chat: str, ids: list[str]) -> None:
    from looper.db import user_gates

    message_id = await message_pool.fetchval(
        "SELECT id FROM task_user_gate_messages WHERE step_id = $1 ORDER BY id LIMIT 1", step_id)
    task_id = await message_pool.fetchval("SELECT task_id FROM task_user_gate_messages WHERE id = $1", message_id)
    await user_gates.record_post(message_id, task_id, "sent", channel=channel, channel_id=channel_id, chat=chat,
                                 external_ids=ids, pool=message_pool)


def _telegram(text: str, bot: str, *, chat: str = "-100", sender: str = "7", message_id: str = "m1",
              reply_to: str | None = None) -> MessageEnvelope:
    context: dict[str, Any] = {"chat_id": chat, "sender_id": sender, "message_id": message_id}
    if reply_to:
        context["reply_to_message_id"] = reply_to
    return MessageEnvelope.create(source=channel_address("telegram", bot), target=MANAGER_ADDRESS,
                                  message_type=MessageType.USER_MESSAGE,
                                  payload={"text": text, "channel_context": context})


def _lark_group(text: str, bot: str, *, chat: str = "oc_team") -> MessageEnvelope:
    context = {"chat_id": chat, "chat_type": "group", "sender_id": "ou_me", "message_id": "om_x"}
    return MessageEnvelope.create(source=channel_address("lark", bot), target=MANAGER_ADDRESS,
                                  message_type=MessageType.USER_MESSAGE,
                                  payload={"text": text, "channel_context": context})


async def _two_telegram_tasks(pool: Any, chats: tuple[str, str] = ("42", "42")) -> None:
    await _task_with_gate(pool, "task-a", origin="channel-telegram", chat=chats[0], kind="chat")
    await _task_with_gate(pool, "task-b", origin="channel-telegram-2", chat=chats[1], kind="chat", step_id="step-b1",
                          project_name="notes")


def test_open_gates_of_a_chat_are_those_of_its_bot(monkeypatch: pytest.MonkeyPatch) -> None:
    async def test(pool: Any) -> None:
        from looper.db import user_gates

        await _two_telegram_tasks(pool)

        def ids(gates: list[dict[str, Any]]) -> list[str]:
            return [gate["task_id"] for gate in gates]

        assert ids(await user_gates.open_gates(10, chat=("channel-telegram", "42"))) == ["task-a"]
        assert ids(await user_gates.open_gates(10, chat=("channel-telegram-2", "42"))) == ["task-b"]
        assert sorted(ids(await user_gates.open_gates(10))) == ["task-a", "task-b"]
        # A task from either bot is no dashboard task; one from no bot is.
        await _task_with_gate(pool, "task-c", step_id="step-c1", project_name="docs")
        assert ids(await user_gates.open_gates(10, chat=("channel-telegram-2", "42"), dashboard=True)) \
            == ["task-c", "task-b"]

    run_db(test, monkeypatch)


def test_a_reply_finds_the_gate_posted_through_its_own_bot(monkeypatch: pytest.MonkeyPatch) -> None:
    async def test(pool: Any) -> None:
        from looper.db import user_gates

        await _two_telegram_tasks(pool, ("42", "43"))
        await _post(pool, "step-g1", "telegram", "channel-telegram", "42", ["7"])
        await _post(pool, "step-b1", "telegram", "channel-telegram-2", "43", ["7"])

        first = await user_gates.find_reference("telegram", "42", ["7"], channel_id="channel-telegram")
        assert first is not None and first["task_id"] == "task-a"
        assert await user_gates.find_reference("telegram", "42", ["7"], channel_id="channel-telegram-2") is None
        second = await user_gates.find_reference("telegram", "43", ["7"], channel_id="channel-telegram-2")
        assert second is not None and second["task_id"] == "task-b"
        assert await user_gates.find_reference("telegram", "43", ["7"], channel_id="channel-telegram") is None
        assert await user_gates.first_post("step-b1", "channel-telegram-2", "43") == "7"
        assert await user_gates.first_post("step-b1", "channel-telegram", "43") is None

    run_db(test, monkeypatch)


@pytest.mark.parametrize("first", ["channel-telegram-2", "channel-telegram"])
def test_two_bots_posting_the_same_message_id_to_the_same_chat_keep_both(
        monkeypatch: pytest.MonkeyPatch, first: str) -> None:
    # A Telegram private chat's id is the user's in every bot, and each bot counts its message ids from 1.
    async def test(pool: Any) -> None:
        from looper.db import user_gates

        await _two_telegram_tasks(pool)
        posts = {"channel-telegram": "step-g1", "channel-telegram-2": "step-b1"}
        for bot in sorted(posts, key=lambda bot: bot != first):
            await _post(pool, posts[bot], "telegram", bot, "42", ["7"])
        assert await pool.fetchval("SELECT count(*) FROM task_user_gate_posts") == 2
        for bot, task in (("channel-telegram", "task-a"), ("channel-telegram-2", "task-b")):
            found = await user_gates.find_reference("telegram", "42", ["7"], channel_id=bot)
            assert found is not None and found["task_id"] == task, bot
            assert await user_gates.first_post(posts[bot], bot, "42") == "7"
        # An older binary's insert (it knows only the built-in bots) still finds the key its ON CONFLICT names.
        message_id = await pool.fetchval("SELECT message_id FROM task_user_gate_posts WHERE channel_id = "
                                         "'channel-telegram'")
        await pool.execute("INSERT INTO task_user_gate_posts (message_id, task_id, channel, chat, external_id) "
                           "VALUES ($1, 'task-a', 'telegram', '42', '7') ON CONFLICT (channel, chat, external_id) "
                           "DO NOTHING", message_id)
        assert await pool.fetchval("SELECT count(*) FROM task_user_gate_posts") == 2

    run_db(test, monkeypatch)


def test_a_lark_message_id_is_found_in_any_chat_but_only_through_its_bot(monkeypatch: pytest.MonkeyPatch) -> None:
    async def test(pool: Any) -> None:
        from looper.db import user_gates

        await _task_with_gate(pool, origin="channel-lark-5", chat="ou_me", kind="user")
        await _post(pool, "step-g1", "lark", "channel-lark-5", "ou_me", ["om_1"])
        found = await user_gates.find_reference("lark", "oc_elsewhere", ["om_1"], channel_id="channel-lark-5")
        assert found is not None and found["task_id"] == "task-a"
        assert await user_gates.find_reference("lark", "oc_elsewhere", ["om_1"], channel_id="channel-lark") is None

    run_db(test, monkeypatch)


def test_the_courier_posts_through_the_gates_bot(monkeypatch: pytest.MonkeyPatch) -> None:
    async def test(pool: Any) -> None:
        from looper.agents.gate_courier import GateCourier
        from looper.agents.gate_service import _dashboard_chat
        from looper.db import user_gates

        await _task_with_gate(pool, "task-a", origin="channel-lark-5", chat="ou_me", kind="user")
        await _task_with_gate(pool, "task-b", chat=None, step_id="step-b1", project_name="notes")
        dashboard = _dashboard_chat(json.dumps({"channel": "telegram", "chat": "42"}))
        poster = _Poster()
        courier = GateCourier(poster)
        for row in await user_gates.unposted(pool=pool):
            assert await courier._handle(row, dashboard)

        assert sorted((post["channel"], post["target"]) for post in poster.posts) == [
            ("channel-lark-5", NotifyTarget(TargetKind.USER, "ou_me")),
            ("channel-telegram", NotifyTarget(TargetKind.CHAT, "42"))]
        rows = await pool.fetch("SELECT task_id, channel, channel_id, chat FROM task_user_gate_posts ORDER BY task_id")
        # A Manager's own bot's posts keep channel NULL (schema.sql, task_user_gate_posts_bot_key).
        assert [tuple(row.values()) for row in rows] == [("task-a", None, "channel-lark-5", "ou_me"),
                                                         ("task-b", "telegram", "channel-telegram", "42")]

    run_db(test, monkeypatch)


def test_a_reply_through_a_bot_reaches_that_bots_gate_and_records_the_bot(monkeypatch: pytest.MonkeyPatch) -> None:
    async def test(pool: Any) -> None:
        from looper.agents.gate_courier import GateCourier
        from looper.agents.gate_routing import RouteKind, route
        from looper.db import user_gates

        await _task_with_gate(pool, origin="channel-telegram-2", chat="42", kind="chat")
        await GateCourier(_Poster()).run_once()  # the card is om_101, posted through channel-telegram-2
        bus = _Bus()

        def routed(envelope: MessageEnvelope) -> Any:
            return route(envelope, has_planner=lambda task_id: True, bus=bus, source=MANAGER_ADDRESS)

        other = _telegram("Redis", "channel-telegram", chat="42", reply_to="om_101")
        assert (await routed(other)).kind is RouteKind.NONE  # the default bot's chat 42 has no such message
        mine = _telegram("Redis", "channel-telegram-2", chat="42", reply_to="om_101", message_id="u1")
        assert (await routed(mine)).kind is RouteKind.DELIVERED
        (message,) = [m for m in await user_gates.thread("step-g1", pool=pool) if m["role"] == "user"]
        assert (message["channel"], message["channel_id"], message["chat"]) == (
            "telegram", "channel-telegram-2", "42")
        # The user's message is found only through the bot it came through.
        assert (await routed(_telegram("and 90 days", "channel-telegram", chat="42", reply_to="u1"))).kind \
            is RouteKind.NONE
        assert (await routed(_telegram("and 90 days", "channel-telegram-2", chat="42", reply_to="u1",
                                       message_id="u2"))).kind is RouteKind.DELIVERED

    run_db(test, monkeypatch)


def test_gate_lists_are_numbered_per_bot(monkeypatch: pytest.MonkeyPatch) -> None:
    async def test(pool: Any) -> None:
        from looper.agents import gate_commands
        from looper.agents.gate_commands import handle, settle

        await _two_telegram_tasks(pool, ("-100", "-100"))
        bus = _Bus()

        async def command(text: str, bot: str, message_id: str) -> str | None:
            envelope = _telegram(text, bot, message_id=message_id)
            reply = await handle(envelope, text, has_planner=lambda task_id: True, bus=bus, source=MANAGER_ADDRESS)
            settle(envelope.message_id, True)
            return reply

        first = await command("/gates", "channel-telegram", "c1")
        second = await command("/gates", "channel-telegram-2", "c2")
        assert first is not None and "【" in first and "1." in first
        assert second is not None and "1." in second
        assert {key[1]: [e.task_id for e in entries] for key, (_, entries) in gate_commands._snapshots.items()} == {
            "channel-telegram": ["task-a"], "channel-telegram-2": ["task-b"]}

        # /gate 1 answers the gate of the bot it is typed to.
        reply = await command("/gate 1 Redis", "channel-telegram-2", "c3")
        assert reply is not None and "已交给" in reply
        (message,) = await pool.fetch("SELECT task_id, channel_id FROM task_user_gate_messages WHERE role = 'user'")
        assert (message["task_id"], message["channel_id"]) == ("task-b", "channel-telegram-2")

        # A snapshot of one bot answers nothing in a bot that never listed.
        gate_commands._snapshots.clear()
        await command("/gates", "channel-telegram", "c4")
        again = await command("/gate 1 Redis", "channel-telegram-2", "c5")
        assert again is not None and again.startswith("请先用 /gates")

    run_db(test, monkeypatch)


def test_the_manager_sees_only_the_open_gates_of_the_bots_chat(monkeypatch: pytest.MonkeyPatch) -> None:
    async def test(pool: Any) -> None:
        from looper.agents.tiers.manager import ManagerAgent

        await _task_with_gate(pool, "task-a", origin="channel-lark", chat="oc_team", kind="chat")
        await _task_with_gate(pool, "task-b", origin="channel-lark-5", chat="oc_team", kind="chat", step_id="step-b1",
                              project_name="notes")
        manager = SimpleNamespace(agent_id="manager", _log="manager")

        async def tasks_in(bot: str) -> list[str]:
            lines = await ManagerAgent._open_gates_section(manager, _lark_group("hi", bot))  # type: ignore[arg-type]
            return [line.split(" ")[4] for line in lines[1:-1]]

        assert await tasks_in("channel-lark-5") == ["task-b"]
        assert await tasks_in("channel-lark") == ["task-a"]
        assert await tasks_in("channel-lark-6") == []

    run_db(test, monkeypatch)
