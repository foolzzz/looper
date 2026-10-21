"""/gates, /gate N <answer>, /gate N end and the reply "end" under a gate's card: answered by code in the Manager,
never reaching its model. The database and the gate service are stubbed (the real ones are checked in
``test_gate_channels_db``)."""

from __future__ import annotations

import asyncio
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from looper.agents import gate_commands, gate_routing
from looper.agents.bus import MessageBus
from looper.agents.channels.base import DeliveryReceipt
from looper.agents.channels.chat_history import CHAT_RECORD, record_reply
from looper.agents.gate_routing import RouteKind
from looper.agents.message import MANAGER_ADDRESS, MessageEnvelope, MessageType, channel_address
from looper.agents.projects import RequestError
from looper.agents.tiers import manager as manager_module
from looper.agents.tiers.manager import ManagerAgent, ManagerConfig

LARK = channel_address("lark", "channel-lark")
TELEGRAM = channel_address("telegram", "channel-telegram")


def _gate(task_id: str, label: str, name: str, *, topic: str = "选哪种存储？", chat_id: str = "",
          origin: str | None = None, hours: int = 3, options: list[str] | None = None) -> dict[str, Any]:
    return {"task_id": task_id, "step_key": label, "step_id": f"{task_id}-{label}", "topic": topic,
            "task_title": name, "task_goal_id": None, "task_requirement": "r", "origin_channel_id": origin,
            "chat_id": chat_id, "chat_kind": "chat", "chat_mention": None, "schedule_id": None,
            "opened_at": datetime.now(timezone.utc) - timedelta(hours=hours),
            "agenda": [{"id": "q1", "question": "用哪个库？", "options": options or [], "status": "open"},
                       {"id": "q2", "question": "done", "options": [], "status": "closed"}]}


class _World:
    """The gates that wait, what was posted to them, and what reached the model."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.gates: list[dict[str, Any]] = []
        self.listed_with: list[Any] = []
        self.posts: list[dict[str, Any]] = []
        self.refuse: RequestError | None = None
        self.delivered = "queued"
        self.sent_ok = True
        self.model: list[str] = []
        self.stored: list[str] = []

        async def open_gates(limit: int, *, chat: Any = None, dashboard: bool = False, pool: Any = None) -> Any:
            self.listed_with.append(chat)
            if chat is None:
                return list(self.gates)
            return [g for g in self.gates if g["chat_id"] == chat[1]]

        async def no_dashboard() -> None:
            return None

        async def post_message(task_id: str, label: str, data: Any, **kwargs: Any) -> dict[str, Any]:
            if self.refuse is not None:
                raise self.refuse
            self.posts.append({"task_id": task_id, "label": label, "data": dict(data), **kwargs})
            return {"delivered": self.delivered}

        async def task_name(task_id: str) -> str:
            return "登录重构"

        async def invoke(manager: Any, message: str, envelope: Any, *, turn: Any, note: str = "") -> str:
            self.model.append(message)
            return "model answer"

        async def persist(manager: Any, envelope: Any, role: str, content: str, message_id: str) -> None:
            self.stored.append(content)

        monkeypatch.setattr(gate_commands.db_user_gates, "open_gates", open_gates)
        monkeypatch.setattr(gate_commands, "dashboard_target", no_dashboard)
        monkeypatch.setattr(gate_commands, "post_message", post_message)
        monkeypatch.setattr(gate_routing, "post_message", post_message)
        monkeypatch.setattr(gate_routing, "task_name", task_name)
        monkeypatch.setattr(ManagerAgent, "_invoke_claude", invoke)
        monkeypatch.setattr(ManagerAgent, "_persist_chat", persist)
        gate_commands._snapshots.clear()

    def send(self, text: str, *, source: Any = LARK, **context: Any) -> list[tuple[str, bool]]:
        """One message to a Manager; the texts it published back, with whether the channel is to store each."""
        sent: list[tuple[str, bool]] = []

        async def run() -> None:
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                bus = MessageBus(runtime_root=root)
                manager = ManagerAgent(config=ManagerConfig(claude_bin="claude", work_dir=root), runtime_root=root)
                manager._bus = bus

                async def tools_problem() -> None:
                    return None

                manager.ops = SimpleNamespace(platform=SimpleNamespace(task_router=SimpleNamespace(
                    has_planner=lambda task_id: True)), tools_problem=tools_problem)
                base = {"chat_id": "ou_me", "chat_type": "p2p", "sender_id": "ou_me", "message_id": "om_9"}
                await manager._handle(MessageEnvelope.create(
                    source=source, target=MANAGER_ADDRESS, message_type=MessageType.USER_MESSAGE,
                    payload={"text": text, "channel_context": {**base, **context}}, correlation_id="c-1"))
                queue = bus.get_queue(source.agent_id)
                while not queue.empty():
                    envelope = queue.get_nowait()
                    if envelope.message_type is MessageType.AGENT_REPLY:
                        sent.append((envelope.payload.get("text"), CHAT_RECORD in envelope.metadata))
                        # What the channel's outbound loop does once it has tried to send the answer.
                        await record_reply("lark", envelope, DeliveryReceipt(channel="lark", success=self.sent_ok))
                await bus.shutdown()

        asyncio.run(run())
        return sent


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch) -> _World:
    return _World(monkeypatch)


@pytest.mark.usefixtures("agent_db")
class TestList:
    def test_a_private_chat_lists_every_gate_in_every_chat(self, world: _World) -> None:
        world.gates = [_gate("t1", "g1", "登录重构", chat_id="ou_me", options=["Redis", "DB"]),
                       _gate("t2", "g2", "报表", chat_id="oc_team", hours=1), _gate("t3", "g1", "网页任务")]
        [(text, stored)] = world.send("/gates")
        assert world.listed_with == [None]
        assert "1. 【登录重构】g1 选哪种存储？" in text and "2. 【报表】g2" in text and "3. 【网页任务】g1" in text
        assert "用哪个库？（选项：Redis / DB）" in text and "done" not in text
        assert "3 小时" in text and "/gate N" in text and "end" in text
        assert not stored and world.model == [] and world.stored == []

    def test_a_group_lists_only_its_own_gates(self, world: _World) -> None:
        world.gates = [_gate("t1", "g1", "登录重构", chat_id="ou_me"),
                       _gate("t2", "g2", "报表", chat_id="oc_team", origin="channel-lark")]
        [(text, _)] = world.send("/gates", chat_id="oc_team", chat_type="group")
        assert world.listed_with == [("lark", "oc_team")]
        assert "报表" in text and "登录重构" not in text and world.model == []

    def test_nothing_waiting(self, world: _World) -> None:
        assert world.send("/gates") == [(gate_commands.NONE_WAITING, False)]

    def test_telegram_form_with_the_bot_name(self, world: _World) -> None:
        world.gates = [_gate("t1", "g1", "A")]
        [(text, _)] = world.send("/gates@loop_bot", source=TELEGRAM, chat_id="42", sender_id="42")
        assert "1. 【A】g1" in text


@pytest.mark.usefixtures("agent_db")
class TestAnswer:
    def _listed(self, world: _World) -> None:
        world.gates = [_gate("t1", "g1", "登录重构"), _gate("t2", "g2", "报表")]
        world.send("/gates")

    def test_an_answer_goes_to_the_gate_the_number_stood_for(self, world: _World) -> None:
        self._listed(world)
        [(text, stored)] = world.send("/gate 2 用 Redis")
        post = world.posts[0]
        assert (post["task_id"], post["label"], post["data"], post["end"]) == ("t2", "g2", {"text": "用 Redis"}, False)
        assert post["via"] == "command:lark" and post["origin"]["channel"] == "lark"
        assert post["origin"]["external_id"] == "om_9"
        assert text == "已交给 报表 的 g2" and not stored and world.model == []

    def test_the_number_is_the_snapshot_not_the_live_order(self, world: _World) -> None:
        self._listed(world)
        world.gates.insert(0, _gate("t9", "g1", "新来的"))  # a newer gate; the snapshot is unchanged
        world.send("/gate 1 好")
        assert world.posts[0]["task_id"] == "t1"

    def test_end(self, world: _World) -> None:
        self._listed(world)
        [(text, _)] = world.send("/gate 1 end")
        assert world.posts[0]["end"] is True and world.posts[0]["data"] == {"text": ""}
        assert world.posts[0]["via"] == "command:lark"
        assert text == "已请规划者按现在的结论结束 登录重构 的 g1"

    def test_a_stopped_task_keeps_the_answer(self, world: _World) -> None:
        self._listed(world)
        world.delivered = "kept"
        [(text, _)] = world.send("/gate 1 好")
        assert text.startswith("任务已停止") and "登录重构 g1" in text and "已交给" not in text

    def test_ending_a_gate_of_a_stopped_task_is_kept_too(self, world: _World) -> None:
        self._listed(world)
        world.delivered = "kept"
        [(text, _)] = world.send("/gate 1 end")
        assert world.posts[0]["end"] is True
        assert text.startswith("任务已停止") and "登录重构 g1" in text and "已请规划者" not in text

    def test_without_a_list_the_user_is_sent_to_the_list(self, world: _World) -> None:
        world.gates = [_gate("t1", "g1", "登录重构")]
        [(text, _)] = world.send("/gate 1 好")
        assert world.posts == [] and "/gates" in text and "1. 【登录重构】" in text

    def test_an_unknown_number_gets_the_fresh_list(self, world: _World) -> None:
        self._listed(world)
        [(text, _)] = world.send("/gate 7 好")
        assert world.posts == [] and "没有第 7 个" in text and "2. 【报表】" in text

    def test_a_stale_snapshot_is_not_used(self, world: _World, monkeypatch: pytest.MonkeyPatch) -> None:
        self._listed(world)
        key = next(iter(gate_commands._snapshots))
        when, entries = gate_commands._snapshots[key]
        gate_commands._snapshots[key] = (when - gate_commands.SNAPSHOT_TTL_SEC - 1, entries)
        [(text, _)] = world.send("/gate 1 好")
        assert world.posts == [] and "/gates" in text

    def test_each_chat_has_its_own_numbers(self, world: _World) -> None:
        self._listed(world)
        [(text, _)] = world.send("/gate 1 好", chat_id="ou_other", sender_id="ou_other")
        assert world.posts == [] and "/gates" in text

    def test_a_gate_that_stopped_waiting_says_so(self, world: _World) -> None:
        self._listed(world)
        world.refuse = RequestError("409 Conflict", "gate g1 is closed: it does not wait for the user")
        [(text, _)] = world.send("/gate 1 好")
        assert "已经不在等你了" in text and world.model == []

    def test_a_list_that_was_not_delivered_numbers_nothing(self, world: _World) -> None:
        self._listed(world)
        world.gates = [_gate("t3", "g1", "新的任务"), _gate("t2", "g2", "报表")]
        world.sent_ok = False
        world.send("/gates")
        world.sent_ok = True
        [(text, _)] = world.send("/gate 1 end")
        assert world.posts == [] and "/gates" in text

    def test_a_list_is_the_snapshot_only_once_it_is_delivered(self, world: _World) -> None:
        world.gates = [_gate("t1", "g1", "登录重构")]
        envelope = MessageEnvelope.create(
            source=LARK, target=MANAGER_ADDRESS, message_type=MessageType.USER_MESSAGE,
            payload={"text": "/gates", "channel_context": {"chat_id": "ou_me", "sender_id": "ou_me"}})

        async def listed() -> None:
            await gate_commands.handle(envelope, "/gates", has_planner=None, bus=None, source=MANAGER_ADDRESS)

        asyncio.run(listed())
        assert gate_commands._snapshots == {}
        gate_commands.settle(envelope.message_id, True)
        assert len(gate_commands._snapshots) == 1

    def test_a_missing_answer_gets_the_usage(self, world: _World) -> None:
        self._listed(world)
        assert world.send("/gate 1") == [(gate_commands.USAGE, False)]
        assert world.posts == []

    def test_other_text_reaches_the_model(self, world: _World) -> None:
        world.send("/gatesx hello")
        world.send("gate 1 hello")
        assert world.model == ["/gatesx hello", "gate 1 hello"]


@pytest.mark.usefixtures("agent_db")
class TestReplyUnderACard:
    def _reply(self, world: _World, text: str, monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, bool]]:
        async def find(channel: str, chat: str, ids: Any) -> dict[str, Any]:
            return {"task_id": "t1", "step_key": "g1", "state": "open"}

        monkeypatch.setattr(gate_routing.db_user_gates, "find_reference", find)
        monkeypatch.setattr(gate_routing.GateState, "waiting", property(lambda self: True))
        return world.send(text, reply_to="om_1")

    def test_a_reply_gets_a_receipt(self, world: _World, monkeypatch: pytest.MonkeyPatch) -> None:
        [(text, stored)] = self._reply(world, "PostgreSQL", monkeypatch)
        assert text == "已交给 登录重构 的 g1" and not stored and world.model == []
        assert world.posts[0]["end"] is False and world.posts[0]["via"] == "lark"

    @pytest.mark.parametrize("word", ["end", "/end", " End "])
    def test_the_reply_end_ends_the_gate(self, world: _World, monkeypatch: pytest.MonkeyPatch, word: str) -> None:
        [(text, _)] = self._reply(world, word, monkeypatch)
        assert world.posts[0]["end"] is True and world.posts[0]["data"] == {"text": ""}
        assert text == "已请规划者按现在的结论结束 登录重构 的 g1" and world.model == []

    def test_end_inside_a_sentence_is_a_message(self, world: _World, monkeypatch: pytest.MonkeyPatch) -> None:
        self._reply(world, "end it with Redis", monkeypatch)
        assert world.posts[0]["end"] is False and world.posts[0]["data"] == {"text": "end it with Redis"}

    def test_a_closed_gate_reaches_the_model_with_a_note(self, world: _World, monkeypatch: pytest.MonkeyPatch) -> None:
        async def refuse(*args: Any, **kwargs: Any) -> Any:
            raise RequestError("409 Conflict", "gate g1 is closed")

        async def find(channel: str, chat: str, ids: Any) -> dict[str, Any]:
            return {"task_id": "t1", "step_key": "g1", "state": "open"}

        monkeypatch.setattr(gate_routing.db_user_gates, "find_reference", find)
        monkeypatch.setattr(gate_routing.GateState, "waiting", property(lambda self: True))
        monkeypatch.setattr(gate_routing, "post_message", refuse)
        world.send("late", reply_to="om_1")
        assert world.model == ["late"]
        assert gate_routing._closed("t1", "g1", "x").kind is RouteKind.CLOSED
