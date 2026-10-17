"""User gates in Lark and Telegram, on a real PostgreSQL: the courier posts a gate's discussion to the chat its task
came from (the card, then replies under it, the Planner's reply under the user's message) and notes what each post
did; a reply to any of those messages goes to the gate before the Manager's model; a web task's gates go to the
chat the system setting names.

The channels are stand-ins (``_Poster`` answers ``ChannelRegistry.post``). Runs only when LOOPER_TEST_DATABASE_URL
points at a disposable database.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from looper.agents.channels.base import PostReceipt
from looper.agents.message import MANAGER_ADDRESS, MessageEnvelope, MessageType, channel_address
from looper.agents.task_notice import NotifyTarget, TargetKind
from tests.test_ops_tasks_db import _project
from tests.test_user_gates_db import _api, _gate, _gate_step
from tests.test_usage_api_db import DSN, _run

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")

LARK_USER = "ou_requester"
LARK_GROUP = "oc_team"
TELEGRAM_CHAT = "4242"


class _Poster:
    """``ChannelRegistry.post`` as the courier calls it: each post gets the next message id; ``fail`` fails the next
    posts, ``gone`` the next replies as replies to a message that is gone."""

    def __init__(self) -> None:
        self.posts: list[dict[str, Any]] = []
        self.next_id = 100
        self.fail = 0
        self.gone = 0

    async def __call__(self, channel: str, target: NotifyTarget, text: str, *, reply_to: str | None = None,
                       idempotency_key: str | None = None) -> PostReceipt:
        self.posts.append({"channel": channel, "target": target, "text": text, "reply_to": reply_to,
                           "key": idempotency_key})
        if self.gone and reply_to is not None:
            self.gone -= 1
            return PostReceipt(False, error="lark-cli exited 2: the message was recalled", parent_gone=True)
        if self.fail:
            self.fail -= 1
            return PostReceipt(False, error="the bot was removed from the chat")
        self.next_id += 1
        return PostReceipt(True, external_ids=(f"om_{self.next_id}",))


class _Bus:
    def __init__(self) -> None:
        self.published: list[MessageEnvelope] = []

    async def publish(self, envelope: MessageEnvelope) -> None:
        self.published.append(envelope)


async def _task_with_gate(pool: Any, task_id: str = "task-a", *, origin: str = "", chat: str | None = None,
                          kind: str | None = None, mention: str | None = None, status: str = "waiting_user",
                          state: str = "open", step_id: str = "step-g1", project_name: str = "calc") -> None:
    from datetime import datetime, timezone

    from looper.db import plans, tasks

    project = await _project(pool, project_name)
    await tasks.create_task(task_id=task_id, requirement="write the settings file", project_id=project["id"],
                            work_dir="/w", status=status, origin_channel_id=origin, chat_id=chat, chat_kind=kind,
                            chat_mention=mention, title="settings file", pool=pool)
    await plans.save(task_id, plan={"plan_id": f"plan-{task_id}", "status": "executing"},
                     steps=[{**_gate_step(step_id), "status": "waiting_user"}],
                     gates=[_gate(step_id, state=state, opened_at=datetime.now(timezone.utc))],
                     gate_messages=[{"step_id": step_id, "role": "looper", "kind": "opened",
                                     "text": "Gate opened: storage"}], pool=pool)


async def _say(task_id: str, step_id: str, kind: str, text: str, role: str = "looper", **gate: Any) -> None:
    """A message of the Planner's or Looper's in the gate, with the gate as it is then."""
    from looper.db import plans

    await plans.save(task_id, gates=[_gate(step_id, **gate)] if gate else [],
                     gate_messages=[{"step_id": step_id, "role": role, "kind": kind, "text": text}])


def _lark_reply(text: str, *, reply_to: str | None = None, root_id: str | None = None, chat: str = "oc_dm",
                group: bool = False, message_id: str = "om_user_1", sender: str = LARK_USER) -> MessageEnvelope:
    context: dict[str, Any] = {"chat_id": chat, "chat_type": "group" if group else "p2p", "sender_id": sender,
                               "message_id": message_id}
    if reply_to:
        context["reply_to"] = reply_to
    if root_id:
        context["root_id"] = root_id
    if group:
        context["sender_name"] = "张三"
    return MessageEnvelope.create(source=channel_address("lark", "channel-lark"), target=MANAGER_ADDRESS,
                                  message_type=MessageType.USER_MESSAGE,
                                  payload={"text": text, "channel_context": context})


def test_a_lark_tasks_gate_posts_its_card_and_the_rest_under_it(monkeypatch: pytest.MonkeyPatch) -> None:
    async def test(pool: Any) -> None:
        from looper.agents.gate_courier import GROUP_LINE, GateCourier
        from looper.db import user_gates

        await _task_with_gate(pool, origin="channel-lark", chat=LARK_USER, kind="user")
        poster = _Poster()
        courier = GateCourier(poster, dashboard_url="http://looper.test")
        assert await courier.run_once() == 1
        (card,) = poster.posts
        assert (card["channel"], card["target"], card["reply_to"]) == ("lark", NotifyTarget(TargetKind.USER,
                                                                                              LARK_USER), None)
        assert card["text"].splitlines()[:3] == ["⏸ 需要你决定 · settings file", "storage",
                                                 "1. Which database?（选项：PostgreSQL / SQLite）"]
        assert "直接回复这条消息作答。" in card["text"] and GROUP_LINE not in card["text"]
        assert "[http://looper.test/#tasks/task-a](http://looper.test/#tasks/task-a)" in card["text"]
        assert await courier.run_once() == 0  # handled once

        # The user answers in Lark (under the card), the Planner replies: under the user's message.
        # As routing records a direct message: in the chat its sender's open_id names (``origin_chat``).
        await user_gates.add_user_message("task-a", "g1", {"kind": "message", "text": "PostgreSQL", "via": "lark",
                                                           "channel": "lark", "chat": LARK_USER,
                                                           "external_id": "om_user_1"})
        assert await user_gates.unposted() == []  # the user's own messages are never posted
        await _say("task-a", "step-g1", "reply", "Which version?", role="planner", state="discussing")
        await _say("task-a", "step-g1", "updated", "the service runs 15", state="discussing")
        await courier.run_once()
        reply, update = poster.posts[1:]
        assert (reply["text"], reply["reply_to"]) == ("Which version?", "om_user_1")
        assert update["reply_to"] == "om_101" and update["text"].startswith("✏️ 规划者更新了问题：the service runs 15")

        thread = await user_gates.thread("step-g1", pool=pool)
        assert [(m["role"], m["kind"], m["post_state"]) for m in thread] == [
            ("looper", "opened", "sent"), ("user", "message", "none"), ("planner", "reply", "sent"),
            ("looper", "updated", "sent")]
        posts = await pool.fetch("SELECT channel, chat, external_id FROM task_user_gate_posts ORDER BY id")
        assert [tuple(p) for p in posts] == [("lark", LARK_USER, "om_101"), ("lark", LARK_USER, "om_102"),
                                             ("lark", LARK_USER, "om_103")]
        # Each post is keyed by its message, so Lark sends it once however often it is tried.
        assert [p["key"] for p in poster.posts] == [f"gate-{m['id']}" for m in thread if m["post_state"] == "sent"]
        (gate,) = (await _api("GET", "/api/tasks/task-a"))[1]["gates"]
        assert gate["channel_posts"] == {"channel": "lark", "target": f"Lark {LARK_USER}", "sent": 3, "failed": 0,
                                         "last_error": None}

    _run(test, monkeypatch)


def test_a_lark_groups_messages_that_wait_for_an_answer_say_to_mention_the_bot(monkeypatch: pytest.MonkeyPatch,
                                                                               ) -> None:
    async def test(pool: Any) -> None:
        from looper.agents.gate_courier import GROUP_LINE, GateCourier

        await _task_with_gate(pool, origin="channel-lark", chat=LARK_GROUP, kind="chat", mention=LARK_USER)
        poster = _Poster()
        courier = GateCourier(poster)
        await courier.run_once()
        summary = {"decisions": ["PostgreSQL"], "assumptions": ["version 16"], "open": []}
        await _say("task-a", "step-g1", "closed", "Settled", state="passed", summary=summary,
                   plan_change={"added": ["s2"], "dropped": [], "stopped": []})
        await courier.run_once()
        card, closed = poster.posts
        assert card["target"] == NotifyTarget(TargetKind.CHAT, LARK_GROUP, LARK_USER) and GROUP_LINE in card["text"]
        assert closed["text"] == ("✅ 讨论结束 · storage\n决定：\n- PostgreSQL\n假设（没有经你确认）：\n- version 16\n"
                                  "计划：加了 s2\n[/#tasks/task-a](/#tasks/task-a)")
        assert GROUP_LINE not in closed["text"] and closed["reply_to"] == "om_101"

    _run(test, monkeypatch)


def test_a_failed_post_is_tried_again_and_the_gates_later_messages_wait_behind_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def test(pool: Any) -> None:
        from looper.agents.gate_courier import POST_ATTEMPTS, POST_RETRY_SEC, GateCourier

        await _task_with_gate(pool, origin="channel-telegram", chat=TELEGRAM_CHAT, kind="chat")
        poster = _Poster()
        courier = GateCourier(poster)

        async def due() -> None:  # the next try of every post that waits for one is due now
            await pool.execute("UPDATE task_user_gate_messages SET post_retry_at = now() - interval '1 second' "
                               "WHERE post_retry_at IS NOT NULL AND post_state IS NULL")

        async def posts() -> list[tuple[Any, ...]]:
            return [tuple(r) for r in await pool.fetch(
                "SELECT kind, post_state, post_attempts, post_retry_at IS NOT NULL FROM task_user_gate_messages "
                "ORDER BY id")]

        poster.fail = 1
        await courier.run_once()  # the card fails once: it is tried again in POST_RETRY_SEC[0]
        retry_in = await pool.fetchval("SELECT extract(epoch FROM post_retry_at - now()) FROM "
                                       "task_user_gate_messages WHERE kind = 'opened'")
        assert POST_RETRY_SEC[0] - 5 < retry_in <= POST_RETRY_SEC[0]
        assert await posts() == [("opened", None, 1, True)]
        (gate,) = (await _api("GET", "/api/tasks/task-a"))[1]["gates"]
        assert gate["channel_posts"]["failed"] == 0  # not failed yet
        # The reminder written meanwhile waits behind the card, so the chat gets them in order.
        await _say("task-a", "step-g1", "reminder", "Still waiting")
        await courier.run_once()
        assert len(poster.posts) == 1
        await due()
        await courier.run_once()
        card, reminder = poster.posts[1:]
        assert card["key"] == poster.posts[0]["key"] and card["text"].startswith("⏸ 需要你决定")
        assert reminder["reply_to"] == "om_101" and reminder["text"].startswith("⏰ 还在等你 · 已等 0 小时")
        assert await posts() == [("opened", "sent", 2, False), ("reminder", "sent", 1, False)]

        # POST_ATTEMPTS tries in all, then it failed: the task page and the activity say so.
        poster.fail = POST_ATTEMPTS
        await _say("task-a", "step-g1", "updated", "the service runs 15", state="open")
        for _ in range(POST_ATTEMPTS):
            await courier.run_once()
            await due()
        assert await posts() == [("opened", "sent", 2, False), ("reminder", "sent", 1, False),
                                 ("updated", "failed", POST_ATTEMPTS, False)]
        (gate,) = (await _api("GET", "/api/tasks/task-a"))[1]["gates"]
        assert gate["channel_posts"] == {"channel": "telegram", "target": f"Telegram {TELEGRAM_CHAT}", "sent": 2,
                                         "failed": 1, "last_error": "the bot was removed from the chat"}
        activity = [r["summary"] for r in await pool.fetch("SELECT summary FROM activity WHERE kind = 'error'")]
        assert activity == [f"Gate g1: could not send to Telegram {TELEGRAM_CHAT}: the bot was removed from the chat"]

        # A reply under a message that is gone is posted on its own, under a key of its own; a reply that failed
        # otherwise was tried again under its message (above), as it may have arrived.
        poster.gone = 1
        await _say("task-a", "step-g1", "cancelled", "No answer needed", state="cancelled", resolution="s1 found it")
        await courier.run_once()
        (under, alone) = poster.posts[-2:]
        assert (under["reply_to"], alone["reply_to"], alone["text"]) == ("om_101", None, "✅ 不用回答了：s1 found it")
        assert alone["key"] == f"{under['key']}-own"
        assert (await posts())[-1] == ("cancelled", "sent", 1, False)

    _run(test, monkeypatch)


def test_what_asks_for_an_answer_is_not_posted_once_the_gate_stopped_waiting(monkeypatch: pytest.MonkeyPatch,
                                                                             ) -> None:
    async def test(pool: Any) -> None:
        from looper.agents.gate_courier import GateCourier

        await _task_with_gate(pool, origin="channel-telegram", chat=TELEGRAM_CHAT, kind="chat")
        # A backlog: the gate opened, was reminded, updated and closed before any of it was posted.
        await _say("task-a", "step-g1", "reminder", "Still waiting")
        await _say("task-a", "step-g1", "updated", "the service runs 15")
        await _say("task-a", "step-g1", "resumed", "")
        await _say("task-a", "step-g1", "reply", "PostgreSQL then.", role="planner")
        await _say("task-a", "step-g1", "closed", "Settled", state="passed",
                   summary={"decisions": ["PostgreSQL"], "assumptions": [], "open": []})
        poster = _Poster()
        await GateCourier(poster).run_once()
        assert [p["text"].splitlines()[0] for p in poster.posts] == ["PostgreSQL then.", "✅ 讨论结束 · storage"]
        states = [tuple(r) for r in await pool.fetch("SELECT kind, post_state FROM task_user_gate_messages ORDER BY id")]
        assert states == [("opened", "none"), ("reminder", "none"), ("updated", "none"), ("resumed", "none"),
                          ("reply", "sent"), ("closed", "sent")]

    _run(test, monkeypatch)


def test_a_web_tasks_gates_go_to_the_chat_the_setting_names(monkeypatch: pytest.MonkeyPatch) -> None:
    async def test(pool: Any) -> None:
        from looper.agents.gate_courier import GateCourier

        await _task_with_gate(pool)
        poster = _Poster()
        courier = GateCourier(poster)
        await courier.run_once()
        assert poster.posts == []  # no setting: the task page only
        state = await pool.fetchval("SELECT post_state FROM task_user_gate_messages")
        assert state == "none"
        (gate,) = (await _api("GET", "/api/tasks/task-a"))[1]["gates"]
        assert gate["channel_posts"] is None

        # Only a chat a channel's allowlist lets in may be named.
        await pool.execute("INSERT INTO channel_configs (channel_id, channel_type, enabled, config) VALUES "
                           "('channel-telegram', 'telegram', true, $1::jsonb) ON CONFLICT (channel_id) DO UPDATE SET "
                           "config = EXCLUDED.config, enabled = true",
                           json.dumps({"allowed_user_ids": [TELEGRAM_CHAT]}))
        status, settings, _ = await _api("GET", "/api/gate-settings")
        assert status == 200 and settings["dashboard_target"] is None
        assert {"channel": "telegram", "chat": TELEGRAM_CHAT} in [
            {k: t[k] for k in ("channel", "chat")} for t in settings["targets"]]
        status, refused, _ = await _api("PUT", "/api/gate-settings",
                                        {"dashboard_target": {"channel": "telegram", "chat": "999"}})
        assert status == 400 and "allowlist" in refused["error"]
        status, refused, _ = await _api("PUT", "/api/gate-settings", {"target": None})
        assert status == 400
        status, saved, _ = await _api("PUT", "/api/gate-settings",
                                      {"dashboard_target": {"channel": "telegram", "chat": TELEGRAM_CHAT}})
        assert status == 200 and saved["dashboard_target"] == {"channel": "telegram", "chat": TELEGRAM_CHAT}

        await _say("task-a", "step-g1", "reminder", "Still waiting")
        await courier.run_once()
        (reminder,) = poster.posts
        assert reminder["target"] == NotifyTarget(TargetKind.CHAT, TELEGRAM_CHAT)

        # The chat leaves the allowlist: its replies would be dropped, so the gates go to the task page only.
        await pool.execute("UPDATE channel_configs SET config = $1::jsonb WHERE channel_id = 'channel-telegram'",
                           json.dumps({"allowed_user_ids": ["5151"]}))
        await _say("task-a", "step-g1", "reminder", "Still waiting")
        await courier.run_once()
        assert len(poster.posts) == 1
        assert await pool.fetchval("SELECT post_state FROM task_user_gate_messages ORDER BY id DESC LIMIT 1") == "none"
        status, settings, _ = await _api("GET", "/api/gate-settings")
        assert settings["dashboard_target"] == {"channel": "telegram", "chat": TELEGRAM_CHAT}
        assert settings["dashboard_target_problem"] == (
            f"Telegram {TELEGRAM_CHAT} is no longer on the Telegram channel's sender allowlist: the gates of dashboard "
            "tasks go to the task page only")
        # A chat on a channel that is off cannot be chosen.
        await pool.execute("UPDATE channel_configs SET enabled = false WHERE channel_id = 'channel-telegram'")
        status, refused, _ = await _api("PUT", "/api/gate-settings",
                                        {"dashboard_target": {"channel": "telegram", "chat": "5151"}})
        assert status == 400 and refused["error"] == ("the Telegram channel is off: turn it on in Settings → Channels "
                                                      "first")
        status, cleared, _ = await _api("PUT", "/api/gate-settings", {"dashboard_target": None})
        assert status == 200 and cleared["dashboard_target"] is None and cleared["dashboard_target_problem"] is None

    _run(test, monkeypatch)


def test_a_reply_to_a_gates_message_goes_to_the_gate_before_the_manager(monkeypatch: pytest.MonkeyPatch) -> None:
    async def test(pool: Any) -> None:
        from looper.agents.gate_courier import GateCourier
        from looper.agents.gate_routing import RouteKind, route
        from looper.db import user_gates

        await _task_with_gate(pool, origin="channel-lark", chat=LARK_USER, kind="user")
        await GateCourier(_Poster()).run_once()  # the card is om_101
        bus = _Bus()

        def routed(envelope: MessageEnvelope, live: bool = True) -> Any:
            return route(envelope, has_planner=lambda task_id: live, bus=bus, source=MANAGER_ADDRESS)

        assert (await routed(_lark_reply("hello"))).kind is RouteKind.NONE  # replies to nothing
        assert (await routed(_lark_reply("hello", reply_to="om_999"))).kind is RouteKind.NONE  # to no gate's message
        found = await routed(_lark_reply("PostgreSQL", reply_to="om_101"))
        assert found.kind is RouteKind.DELIVERED
        (woken,) = bus.published
        assert woken.message_type is MessageType.GATE_MESSAGE and woken.target.agent_id == "planner-task-a"
        (message,) = [m for m in await user_gates.thread("step-g1", pool=pool) if m["role"] == "user"]
        assert (message["text"], message["via"], message["channel"], message["chat"], message["external_id"],
                message["author"]) == ("PostgreSQL", "lark", "lark", LARK_USER, "om_user_1", None)

        # A message in the card's thread that names only its root is found by it; in a group the discussion names
        # who wrote, and the chat is the group.
        found = await routed(_lark_reply("16, please", root_id="om_101", group=True, chat=LARK_GROUP,
                                         message_id="om_user_2"))
        assert found.kind is RouteKind.DELIVERED
        users = [m for m in await user_gates.thread("step-g1", pool=pool) if m["role"] == "user"]
        assert [(m["author"], m["chat"]) for m in users] == [(None, LARK_USER), ("张三", LARK_GROUP)]
        # A reply to a message in the thread that is no gate's (the Manager's own answer there, such as its notice
        # that a reply was kept) is the Manager's, whatever its root.
        assert (await routed(_lark_reply("continue the task", reply_to="om_kept_notice", root_id="om_101",
                                         message_id="om_user_x"))).kind is RouteKind.NONE
        # A reply to the user's own message there is about the gate too.
        assert (await routed(_lark_reply("and 90 days", reply_to="om_user_2", message_id="om_user_3"))).kind \
            is RouteKind.DELIVERED

        # The task stopped: kept for its restart, and the user is told; too long a message is refused.
        await pool.execute("UPDATE tasks SET status = 'stopped'")
        kept = await routed(_lark_reply("SQLite after all", reply_to="om_101", message_id="om_user_4"), live=False)
        assert kept.kind is RouteKind.KEPT and kept.reply.startswith("任务已停止")
        refused = await routed(_lark_reply("x" * 8001, reply_to="om_101", message_id="om_user_5"), live=False)
        assert (refused.kind, refused.reply) == (RouteKind.REFUSED, "这条回答太长（最多 8000 个字），没能交给关卡：请分几条回复。")
        empty = await routed(_lark_reply("  ", reply_to="om_101", message_id="om_user_7"), live=False)
        assert (empty.kind, empty.reply) == (RouteKind.REFUSED, "这条回复没有文字，没能交给关卡：请写下你的回答再回复一次。")

        # A gate that no longer waits: the Manager hears the message, with a note.
        await pool.execute("UPDATE tasks SET status = 'waiting_user'")
        await pool.execute("UPDATE task_user_gates SET state = 'cancelled'")
        closed = await routed(_lark_reply("one more thing", reply_to="om_101", message_id="om_user_6"))
        assert closed.kind is RouteKind.CLOSED and "gate g1 of task task-a" in closed.note
        assert "looper_task_instruct" in closed.note

    _run(test, monkeypatch)


def test_a_telegram_reply_is_matched_within_its_chat_only(monkeypatch: pytest.MonkeyPatch) -> None:
    async def test(pool: Any) -> None:
        from looper.agents.gate_courier import GateCourier
        from looper.agents.gate_routing import RouteKind, route
        from looper.db import user_gates

        await _task_with_gate(pool, origin="channel-telegram", chat=TELEGRAM_CHAT, kind="chat")
        poster = _Poster()
        await GateCourier(poster).run_once()  # the card is om_101 in the stand-in
        bus = _Bus()

        def telegram(chat: str, reply_to: str) -> MessageEnvelope:
            return MessageEnvelope.create(
                source=channel_address("telegram", "channel-telegram"), target=MANAGER_ADDRESS,
                message_type=MessageType.USER_MESSAGE,
                payload={"text": "PostgreSQL", "channel_context": {"chat_id": int(chat), "user_id": 7,
                                                                   "message_id": "9", "reply_to_message_id": reply_to}})

        other = await route(telegram("5151", "om_101"), has_planner=lambda _: True, bus=bus,
                            source=MANAGER_ADDRESS)
        assert other.kind is RouteKind.NONE and bus.published == []
        same = await route(telegram(TELEGRAM_CHAT, "om_101"), has_planner=lambda _: True, bus=bus,
                           source=MANAGER_ADDRESS)
        assert same.kind is RouteKind.DELIVERED and len(bus.published) == 1

        # The Planner's reply goes under the user's message 9 of the task's chat; after a message the user sent the
        # Manager from another chat (its id means nothing in this one), under the card.
        await _say("task-a", "step-g1", "reply", "Which version?", role="planner", state="discussing")
        await GateCourier(poster).run_once()
        await user_gates.add_user_message("task-a", "g1", {"kind": "message", "text": "15", "via": "telegram",
                                                           "channel": "telegram", "chat": "5151", "external_id": "3"})
        await _say("task-a", "step-g1", "reply", "15 then.", role="planner", state="discussing")
        await GateCourier(poster).run_once()
        assert [(p["target"].id, p["text"], p["reply_to"]) for p in poster.posts[1:]] == [
            (TELEGRAM_CHAT, "Which version?", "9"), (TELEGRAM_CHAT, "15 then.", "om_101")]

    _run(test, monkeypatch)


def test_the_discussions_from_before_gates_reached_channels_are_not_posted(monkeypatch: pytest.MonkeyPatch) -> None:
    async def test(pool: Any) -> None:
        from looper.agents.gate_courier import GateCourier
        from looper.db import connection

        await _task_with_gate(pool, origin="channel-lark", chat=LARK_USER, kind="user")
        # As the migration found them: written before the column was there.
        await pool.execute("UPDATE task_user_gate_messages SET post_state = NULL")
        await pool.execute("DELETE FROM schema_migrations WHERE name = "
                           "'task_user_gate_messages_posted_before_channels'")
        await connection.ensure_schema(pool)
        poster = _Poster()
        assert await GateCourier(poster).run_once() == 0 and poster.posts == []

    _run(test, monkeypatch)


def _tool_turn(text: str, *, channel: str = "lark", group: bool = False) -> Any:
    """A Manager turn answering ``text`` in a Lark chat (the user's message om_turn), or the dashboard chat."""
    from looper.agents.ops import Turn

    if channel == "web":
        envelope = MessageEnvelope.create(source=channel_address("web"), target=MANAGER_ADDRESS,
                                          message_type=MessageType.USER_MESSAGE,
                                          payload={"text": text, "channel_context": {"session_id": "chat-1"}})
        return Turn(channel="web", chat="chat-1", message_id=envelope.message_id, envelope=envelope)
    envelope = _lark_reply(text, chat=LARK_GROUP if group else "oc_dm", group=group, message_id="om_turn")
    return Turn(channel="lark", chat=LARK_GROUP if group else "oc_dm", message_id=envelope.message_id,
                envelope=envelope, sender=LARK_USER, group=group)


async def _relay(turn: Any, arguments: dict[str, Any], planners: set[str]) -> tuple[dict[str, Any], _Bus]:
    from types import SimpleNamespace

    from looper.agents.ops.service import ManagerOps, Platform

    bus = _Bus()
    router = SimpleNamespace(has_planner=lambda task_id: task_id in planners)
    ops = ManagerOps(Platform(bus=bus, task_router=router), url="http://127.0.0.1:9", token="tok")
    ops.begin_turn(turn)
    try:
        return await ops.call("looper_task_gate_reply", arguments), bus
    finally:
        ops.end_turn(turn)


def test_the_manager_relays_the_users_own_message_to_a_waiting_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    async def test(pool: Any) -> None:
        from looper.agents.gate_courier import GateCourier
        from looper.db import user_gates

        await _task_with_gate(pool, origin="channel-lark", chat=LARK_GROUP, kind="chat", mention=LARK_USER)
        poster = _Poster()
        await GateCourier(poster).run_once()  # the card in the group is om_101
        # The user's message itself goes, whatever the model would say.
        answer, bus = await _relay(_tool_turn("Use PostgreSQL", group=True), {"task_id": "task-a", "gate": "g1"},
                                   {"task-a"})
        assert answer["status"] == "ok" and answer["result"]["delivered"] == "queued", answer
        (woken,) = bus.published
        assert woken.message_type is MessageType.GATE_MESSAGE
        (message,) = [m for m in await user_gates.thread("step-g1", pool=pool) if m["role"] == "user"]
        assert (message["text"], message["via"], message["author"], message["channel"], message["chat"],
                message["external_id"]) == ("Use PostgreSQL", "manager:lark", "张三", "lark", LARK_GROUP, "om_turn")

        # Only part of it, copied word for word; an excerpt that is not in the message is refused.
        turn = _tool_turn("Thanks! On the storage question: PostgreSQL 16. Also, how much did today cost?")
        answer, _ = await _relay(turn, {"task_id": "task-a", "gate": "g1", "excerpt": "PostgreSQL   16."}, {"task-a"})
        assert answer["status"] == "ok"
        assert [m["text"] for m in await user_gates.thread("step-g1", pool=pool) if m["role"] == "user"][-1] == \
            "PostgreSQL   16."
        answer, _ = await _relay(turn, {"task_id": "task-a", "gate": "g1", "excerpt": "PostgreSQL 15"}, {"task-a"})
        assert answer["status"] == "error" and "word for word" in answer["error"]["message"]

        # Answered from the user's direct chat with the bot: the Planner's reply goes to the gate's group, under
        # the card, not under a message the group cannot see.
        await _say("task-a", "step-g1", "reply", "16 it is.", role="planner", state="discussing")
        await GateCourier(poster).run_once()
        assert (poster.posts[-1]["target"].id, poster.posts[-1]["reply_to"]) == (LARK_GROUP, "om_101")

        # From the dashboard chat: no channel message; the user ends the discussion with their message.
        answer, _ = await _relay(_tool_turn("go on", channel="web"), {"task_id": "task-a", "gate": "g1", "end": True},
                                 set())
        assert answer["status"] == "ok" and answer["result"]["delivered"] == "kept"
        last = (await user_gates.thread("step-g1", pool=pool))[-1]
        assert (last["kind"], last["text"], last["via"], last["channel"], last["external_id"]) == (
            "end", "go on", "manager:web", None, None)

        await pool.execute("UPDATE task_user_gates SET state = 'passed', summary = '{\"decisions\": [], "
                           "\"assumptions\": [], \"open\": []}'")
        answer, _ = await _relay(_tool_turn("one more thing"), {"task_id": "task-a", "gate": "g1"}, {"task-a"})
        assert answer["status"] == "error" and answer["error"]["code"] == "conflict"
        assert "gate g1 is passed" in answer["error"]["message"]

    _run(test, monkeypatch)


def test_the_manager_sees_the_open_gates_of_the_chat_it_answers(monkeypatch: pytest.MonkeyPatch) -> None:
    async def test(pool: Any) -> None:
        from looper.agents.tiers.manager import ManagerAgent

        await _task_with_gate(pool, origin="channel-lark", chat=LARK_GROUP, kind="chat", mention=LARK_USER)
        # Another user's task, from their direct chat with the bot.
        await _task_with_gate(pool, "task-b", origin="channel-lark", chat="ou_other", kind="user", step_id="step-b1",
                              project_name="notes")

        async def section(envelope: MessageEnvelope | None) -> list[str]:
            return await ManagerAgent._open_gates_section(None, envelope)  # type: ignore[arg-type]

        web = await section(_tool_turn("hi", channel="web").envelope)
        assert web[0] == "Open gates (waiting for the user's answer; newest first):"
        assert sorted(line.split(",")[0] for line in web[1:3]) == ["  - task task-a (project calc)",
                                                                   "  - task task-b (project notes)"]
        assert any(f"waiting for the user; chat: Lark group {LARK_GROUP}; open questions: q1 Which database?" in line
                   for line in web)
        assert any("chat: Lark ou_other;" in line for line in web)

        group = await section(_lark_reply("hi", chat=LARK_GROUP, group=True))
        assert group[0] == "Open gates of this chat (waiting for the user's answer; newest first):"
        assert len(group) == 3 and group[1].startswith('  - task task-a (project calc), gate g1 "storage": open for ')
        assert "chat:" not in group[1] and group[1].endswith("open questions: q1 Which database?")
        # The other user's direct chat sees only its own gate; this user's direct chat none of these.
        other = await section(_lark_reply("hi", chat="oc_other_dm", sender="ou_other"))
        assert len(other) == 3 and other[1].startswith("  - task task-b")
        assert await section(_lark_reply("hi")) == []

        await pool.execute("UPDATE task_user_gates SET state = 'passed', summary = '{\"decisions\": [], "
                           "\"assumptions\": [], \"open\": []}'")
        assert await section(None) == []

    _run(test, monkeypatch)
