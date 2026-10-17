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
    posts (with ``reply_to`` only, when ``replies``)."""

    def __init__(self) -> None:
        self.posts: list[dict[str, Any]] = []
        self.next_id = 100
        self.fail = 0
        self.replies = False

    async def __call__(self, channel: str, target: NotifyTarget, text: str, *, reply_to: str | None = None
                       ) -> PostReceipt:
        self.posts.append({"channel": channel, "target": target, "text": text, "reply_to": reply_to})
        if self.fail and (reply_to is not None or not self.replies):
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
                          state: str = "open", step_id: str = "step-g1") -> None:
    from datetime import datetime, timezone

    from looper.db import plans, tasks

    project = await _project(pool)
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
                group: bool = False, message_id: str = "om_user_1") -> MessageEnvelope:
    context: dict[str, Any] = {"chat_id": chat, "chat_type": "group" if group else "p2p", "sender_id": LARK_USER,
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
        await user_gates.add_user_message("task-a", "g1", {"kind": "message", "text": "PostgreSQL", "via": "lark",
                                                           "channel": "lark", "chat": "oc_dm",
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


def test_a_failed_post_is_noted_and_a_reply_to_a_gone_message_is_posted_on_its_own(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def test(pool: Any) -> None:
        from looper.agents.gate_courier import GateCourier

        await _task_with_gate(pool, origin="channel-telegram", chat=TELEGRAM_CHAT, kind="chat")
        poster = _Poster()
        poster.fail = 1
        courier = GateCourier(poster)
        await courier.run_once()  # the card fails
        (gate,) = (await _api("GET", "/api/tasks/task-a"))[1]["gates"]
        assert gate["channel_posts"] == {"channel": "telegram", "target": f"Telegram {TELEGRAM_CHAT}", "sent": 0,
                                         "failed": 1, "last_error": "the bot was removed from the chat"}
        activity = [r["summary"] for r in await pool.fetch("SELECT summary FROM activity WHERE kind = 'error'")]
        assert activity == [f"Gate g1: could not send to Telegram {TELEGRAM_CHAT}: the bot was removed from the chat"]

        # The reminder carries the card's questions; with no card it is a message of its own.
        await _say("task-a", "step-g1", "reminder", "Still waiting")
        await courier.run_once()
        reminder = poster.posts[-1]
        assert reminder["reply_to"] is None and "1. Which database?" in reminder["text"]
        assert reminder["text"].startswith("⏰ 还在等你 · 已等 0 小时 · settings file")

        # A reply under a message that is gone is posted on its own.
        poster.fail, poster.replies = 1, True
        await _say("task-a", "step-g1", "cancelled", "No answer needed", state="cancelled", resolution="s1 found it")
        await courier.run_once()
        assert [(p["reply_to"], p["text"]) for p in poster.posts[-2:]] == [("om_101", "✅ 不用回答了：s1 found it"),
                                                                         (None, "✅ 不用回答了：s1 found it")]
        states = [r["post_state"] for r in await pool.fetch("SELECT post_state FROM task_user_gate_messages "
                                                            "ORDER BY id")]
        assert states == ["failed", "sent", "sent"]

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
        status, cleared, _ = await _api("PUT", "/api/gate-settings", {"dashboard_target": None})
        assert status == 200 and cleared["dashboard_target"] is None

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
                message["author"]) == ("PostgreSQL", "lark", "lark", "oc_dm", "om_user_1", None)

        # A reply further down the thread is found by its root; in a group the discussion names who wrote.
        found = await routed(_lark_reply("16, please", reply_to="om_555", root_id="om_101", group=True,
                                         chat=LARK_GROUP, message_id="om_user_2"))
        assert found.kind is RouteKind.DELIVERED
        authors = [m["author"] for m in await user_gates.thread("step-g1", pool=pool) if m["role"] == "user"]
        assert authors == [None, "张三"]
        # A reply to the user's own message there is about the gate too.
        assert (await routed(_lark_reply("and 90 days", reply_to="om_user_2", message_id="om_user_3"))).kind \
            is RouteKind.DELIVERED

        # The task stopped: kept for its restart, and the user is told; too long a message is refused.
        await pool.execute("UPDATE tasks SET status = 'stopped'")
        kept = await routed(_lark_reply("SQLite after all", reply_to="om_101", message_id="om_user_4"), live=False)
        assert kept.kind is RouteKind.KEPT and kept.reply.startswith("任务已停止")
        refused = await routed(_lark_reply("x" * 8001, reply_to="om_101", message_id="om_user_5"), live=False)
        assert refused.kind is RouteKind.REFUSED and "8001" in refused.reply

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


def test_the_manager_relays_the_users_words_to_a_waiting_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    async def test(pool: Any) -> None:
        from looper.agents.tiers.manager import ManagerAgent
        from looper.db import user_gates

        await _task_with_gate(pool, origin="channel-lark", chat=LARK_GROUP, kind="chat", mention=LARK_USER)
        answer, bus = await _relay(_tool_turn("Use PostgreSQL", group=True),
                                   {"task_id": "task-a", "gate": "g1", "text": "Use PostgreSQL"}, {"task-a"})
        assert answer["status"] == "ok" and answer["result"]["delivered"] == "queued", answer
        (woken,) = bus.published
        assert woken.message_type is MessageType.GATE_MESSAGE
        (message,) = [m for m in await user_gates.thread("step-g1", pool=pool) if m["role"] == "user"]
        assert (message["via"], message["author"], message["channel"], message["chat"], message["external_id"]) == (
            "manager:lark", "张三", "lark", LARK_GROUP, "om_turn")

        # From the dashboard chat: no channel message; the user ends the discussion.
        answer, _ = await _relay(_tool_turn("go on", channel="web"),
                                 {"task_id": "task-a", "gate": "g1", "text": "", "end": True}, set())
        assert answer["status"] == "ok" and answer["result"]["delivered"] == "kept"
        last = (await user_gates.thread("step-g1", pool=pool))[-1]
        assert (last["kind"], last["via"], last["channel"], last["external_id"]) == ("end", "manager:web", None, None)

        # The Manager sees the gate before each message, and in the task.
        section = await ManagerAgent._open_gates_section(None)  # type: ignore[arg-type]
        assert section[0] == "Open gates (waiting for the user's answer; newest first):"
        assert section[1].startswith('  - task task-a (project calc), gate g1 "storage": open for 0 min, waiting for ')
        assert section[1].endswith("open questions: q1 Which database?")

        await pool.execute("UPDATE task_user_gates SET state = 'passed', summary = '{\"decisions\": [], "
                           "\"assumptions\": [], \"open\": []}'")
        answer, _ = await _relay(_tool_turn("one more thing"), {"task_id": "task-a", "gate": "g1", "text": "x"},
                                 {"task-a"})
        assert answer["status"] == "error" and answer["error"]["code"] == "conflict"
        assert "gate g1 is passed" in answer["error"]["message"]
        assert await ManagerAgent._open_gates_section(None) == []  # type: ignore[arg-type]

    _run(test, monkeypatch)
