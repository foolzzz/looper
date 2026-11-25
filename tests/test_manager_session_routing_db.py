"""Session routing on (``manager.session_routing.enabled``): the default Manager's groups and other senders' direct
chats run in sessions of their own, in parallel, at most ``manager.sessions.max_resident`` of them resident.

Each session's runtime is a stand-in (``_Resident``) put in place before its first turn; the senders are the Lark
stand-in's (``OPEN_ID`` is the bot's operator, ``OTHER`` another allowed sender). Runs only when
LOOPER_TEST_DATABASE_URL points at a disposable database.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from looper.agents import operator_identity, session_routing
from looper.agents.bus import MessageBus
from looper.agents.claude_cli import ResidentTurn
from looper.agents.message import MANAGER_ADDRESS, MessageEnvelope, MessageType, channel_address
from looper.agents.run_usage import Usage
from looper.agents.session_routing import SessionKey, SessionKind
from looper.agents.tiers.manager import PROFILE_NOT_HERE_REPLY, ManagerAgent, ManagerConfig
from looper.db import agent_sessions, manager_sessions
from tests.db_support import DSN, run_db, until
from tests.lark_cli_stand_in import OPEN_ID, OTHER

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")

LARK = channel_address("lark", "channel-lark")
WEB = channel_address("web")
GROUP_A, GROUP_B = "oc_synthetic_group_a", "oc_synthetic_group_b"


class _Resident:
    """A resident runtime stand-in: each turn answers at once, or (``hold``) once ``release`` is set; it reports its
    own session id, and remembers each command it was started with."""

    pid = None
    shares_bridge = True
    can_interrupt = True

    def __init__(self, session_id: str, *, hold: bool = False) -> None:
        self.label, self.session_id, self.hold = session_id, session_id, hold
        self.started, self.command, self.starts = False, [], []
        self.totals = Usage()
        self.turns: list[str] = []
        self.running, self.release = asyncio.Event(), asyncio.Event()
        self.on_turn: Any = None
        self.reply_text: str | None = None  # the turn's answer; by default its session id and number

    def stale(self, signature: Any) -> bool:
        return False

    async def start(self, command: list[str], **kwargs: Any) -> None:
        self.started, self.command = True, command
        self.starts.append(command)

    async def stop(self, **kwargs: Any) -> None:
        self.started = False

    async def try_stop_if_idle(self) -> bool:
        self.started = False
        return True

    async def run_turn(self, text: str, on_event: Any, **kwargs: Any) -> ResidentTurn:
        self.turns.append(text)
        await on_event({"type": "system", "subtype": "init", "session_id": self.session_id})
        if self.on_turn is not None:
            await self.on_turn(on_event)
        if self.hold:
            self.running.set()
            await self.release.wait()
        text = self.reply_text or f"{self.session_id} 回复 {len(self.turns)}"
        return ResidentTurn({"type": "result", "subtype": "success", "is_error": False, "result": text,
                             "session_id": self.session_id})


def _lark(text: str, chat: str, sender: str, *, group: bool) -> MessageEnvelope:
    return MessageEnvelope.create(
        source=LARK, target=MANAGER_ADDRESS, message_type=MessageType.USER_MESSAGE,
        payload={"text": text, "channel_context": {"chat_id": chat, "chat_type": "group" if group else "p2p",
                                                   "sender_id": sender, "message_id": f"om_{text}"}},
        metadata={"origin_channel_id": LARK.agent_id})


def _web(text: str) -> MessageEnvelope:
    return MessageEnvelope.create(source=WEB, target=MANAGER_ADDRESS, message_type=MessageType.USER_MESSAGE,
                                  payload={"text": text, "channel_context": {"session_id": "web-chat-1"}})


def _replies(bus: MessageBus, address: Any) -> list[dict[str, Any]]:
    queue = bus.get_queue(address.agent_id)
    items = []
    while not queue.empty():
        envelope = queue.get_nowait()
        if envelope.message_type == MessageType.AGENT_REPLY:
            items.append(envelope)
    return items


class _Rig:
    def __init__(self, root: Path) -> None:
        self.bus = MessageBus(runtime_root=root)
        self.manager = ManagerAgent(config=ManagerConfig(claude_bin="claude", work_dir=root), runtime_root=root)
        self.main = _Resident("sess-main")
        self.manager._sessions["main"].claude = self.main
        self.replies: dict[str, list[MessageEnvelope]] = {}

    def group(self, chat: str, resident: _Resident) -> _Resident:
        self.manager._router.session(SessionKey(SessionKind.GROUP, LARK.agent_id, chat)).claude = resident
        return resident

    def session(self, chat: str, kind: SessionKind = SessionKind.GROUP) -> Any:
        return self.manager._sessions[SessionKey(kind, LARK.agent_id, chat).text]

    async def start(self) -> None:
        await self.bus.start()
        await self.manager.start(self.bus)

    def collect(self) -> None:
        for address in (LARK, WEB):
            for envelope in _replies(self.bus, address):
                self.replies.setdefault(envelope.reply_to or "", []).append(envelope)

    async def reply(self, message: MessageEnvelope, timeout: float = 20) -> str:
        def answered() -> bool:
            self.collect()
            return message.message_id in self.replies

        await until(answered, timeout=timeout)
        return self.replies[message.message_id][-1].payload["text"]

    async def close(self) -> None:
        await self.manager.stop()
        await self.bus.shutdown()


@pytest.fixture
def routed(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Routing on, a cap of 2, and ``OPEN_ID`` as the Lark bot's operator."""
    async def operator(channel_id: str) -> str | None:
        return OPEN_ID if channel_id == LARK.agent_id else None

    monkeypatch.setattr(session_routing, "_routing_enabled", True)
    monkeypatch.setattr(session_routing, "_max_resident", 2)
    monkeypatch.setattr(operator_identity, "operator_sender_id", operator)
    return monkeypatch


def _run(test: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    async def body(pool: Any) -> None:
        rig = _Rig(tmp_path)
        await rig.start()
        try:
            await test(rig, pool)
        finally:
            await rig.close()

    run_db(body, monkeypatch, timeout=90)


def test_a_group_is_answered_while_main_is_mid_turn(routed: Any, tmp_path: Path) -> None:
    async def test(rig: _Rig, pool: Any) -> None:
        rig.main.hold = True
        group = rig.group(GROUP_A, _Resident("sess-a"))
        asked = _web("写一篇很长的文章")
        await rig.bus.publish(asked)
        await asyncio.wait_for(rig.main.running.wait(), 20)
        question = _lark("你好", GROUP_A, OTHER, group=True)
        await rig.bus.publish(question)
        assert await rig.reply(question) == "sess-a 回复 1"
        assert asked.message_id not in rig.replies and len(group.turns) == 1
        rig.main.release.set()
        assert await rig.reply(asked) == "sess-main 回复 1"
        assert (await manager_sessions.get("manager", f"group:{LARK.agent_id}:{GROUP_A}"))["session_id"] == "sess-a"
        assert (await agent_sessions.get_session("manager"))["session_id"] == "sess-main"

    _run(test, routed, tmp_path)


def test_two_messages_of_one_group_run_in_order(routed: Any, tmp_path: Path) -> None:
    async def test(rig: _Rig, pool: Any) -> None:
        group = rig.group(GROUP_A, _Resident("sess-a", hold=True))
        first, second = _lark("第一条", GROUP_A, OTHER, group=True), _lark("第二条", GROUP_A, OPEN_ID, group=True)
        await rig.bus.publish(first)
        await rig.bus.publish(second)
        await asyncio.wait_for(group.running.wait(), 20)
        await asyncio.sleep(0.2)
        assert len(group.turns) == 1 and "第一条" in group.turns[0]
        group.release.set()
        assert await rig.reply(second) == "sess-a 回复 2"
        assert "第二条" in group.turns[1] and rig.replies[first.message_id][-1].payload["text"] == "sess-a 回复 1"

    _run(test, routed, tmp_path)


def test_a_routed_turn_cannot_call_an_unlisted_tool_with_or_without_its_binding(routed: Any,
                                                                                  tmp_path: Path) -> None:
    from looper.agents.ops import Risk, Tool
    from looper.agents.ops.service import ManagerOps, Platform

    async def test(rig: _Rig, pool: Any) -> None:
        chats: list[str | None] = []

        async def run(call: Any) -> dict[str, Any]:
            chats.append(call.turn.chat)
            return {}

        schema = {"type": "object", "properties": {}, "additionalProperties": False}
        ops = ManagerOps(Platform(bus=None, task_router=None), url="http://127.0.0.1:9/", token="tok",
                         tools=(Tool("t_write", "writes", schema, Risk.WRITE, run),))
        ops._working_at = ops._clock()  # the MCP entry was probed
        rig.manager.ops = ops
        rig.main.hold = True
        group = rig.group(GROUP_A, _Resident("sess-a"))
        answers: list[dict[str, Any]] = []

        async def calls(on_event: Any) -> None:
            binding = rig.manager._running_token.binding  # this session's turn (the worker's context)
            await on_event({"type": "assistant", "message": {"content": [
                {"type": "tool_use", "id": "tu-1", "name": "mcp__looper__t_write", "input": {}}]}})
            answers.append(await ops.call("t_write", {}, binding=binding, call_id="tu-1"))
            answers.append(await ops.call("t_write", {}))

        group.on_turn = calls
        await rig.bus.publish(_web("写一篇很长的文章"))
        await asyncio.wait_for(rig.main.running.wait(), 20)
        question = _lark("改一下", GROUP_A, OTHER, group=True)
        await rig.bus.publish(question)
        await rig.reply(question)
        assert chats == []  # neither the group's binding nor no binding grants a tool
        assert all(answer["status"] == "error" and answer["error"]["code"] == "forbidden" for answer in answers)
        rig.main.release.set()

    _run(test, routed, tmp_path)


def test_at_the_cap_an_idle_group_is_stopped_and_a_busy_one_is_waited_for(routed: Any, tmp_path: Path) -> None:
    async def test(rig: _Rig, pool: Any) -> None:
        await rig.reply(await _published(rig, _web("你好")))
        a = rig.group(GROUP_A, _Resident("sess-a"))
        b = rig.group(GROUP_B, _Resident("sess-b"))
        await rig.reply(await _published(rig, _lark("一", GROUP_A, OTHER, group=True)))
        assert rig.main.started and a.started
        await rig.reply(await _published(rig, _lark("二", GROUP_B, OTHER, group=True)))
        assert b.started and not a.started and rig.main.started  # main never goes for another
        assert (await manager_sessions.get("manager", f"group:{LARK.agent_id}:{GROUP_A}"))["session_id"] == "sess-a"
        # B busy and resident: A's next message waits for B's turn to end, then B is stopped for it.
        b.hold = True
        held = await _published(rig, _lark("三", GROUP_B, OTHER, group=True))
        await asyncio.wait_for(b.running.wait(), 20)
        waiting = await _published(rig, _lark("四", GROUP_A, OTHER, group=True))
        await asyncio.sleep(0.5)
        assert len(a.turns) == 1 and waiting.message_id not in rig.replies
        b.release.set()
        await rig.reply(held)
        assert await rig.reply(waiting) == "sess-a 回复 2"
        assert a.started and not b.started
        # The stopped session resumed its saved session.
        assert a.starts[-1][a.starts[-1].index("--resume") + 1] == "sess-a"

    _run(test, routed, tmp_path)


async def _published(rig: _Rig, message: MessageEnvelope) -> MessageEnvelope:
    await rig.bus.publish(message)
    return message


def test_new_in_a_group_resets_only_that_group(routed: Any, tmp_path: Path) -> None:
    async def test(rig: _Rig, pool: Any) -> None:
        await rig.reply(await _published(rig, _web("你好")))
        rig.group(GROUP_A, _Resident("sess-a"))
        await rig.reply(await _published(rig, _lark("一", GROUP_A, OTHER, group=True)))
        await rig.reply(await _published(rig, _lark("/new", GROUP_A, OTHER, group=True)))
        row = await manager_sessions.get("manager", f"group:{LARK.agent_id}:{GROUP_A}")
        assert row["session_id"] is None and row["clean_start"] is True
        assert rig.session(GROUP_A).session_id is None
        assert (await agent_sessions.get_session("manager"))["session_id"] == "sess-main"
        assert rig.manager._sessions["main"].session_id == "sess-main"

    _run(test, routed, tmp_path)


def test_a_group_cannot_choose_a_profile(routed: Any, tmp_path: Path) -> None:
    async def test(rig: _Rig, pool: Any) -> None:
        rig.group(GROUP_A, _Resident("sess-a"))
        asked = await _published(rig, _lark("/profile", GROUP_A, OPEN_ID, group=True))
        assert await rig.reply(asked) == PROFILE_NOT_HERE_REPLY
        assert (await agent_sessions.get_session("manager") or {}).get("selected_profile_id") is None

    _run(test, routed, tmp_path)


def test_another_senders_direct_chat_runs_apart_from_the_operators(routed: Any, tmp_path: Path) -> None:
    async def test(rig: _Rig, pool: Any) -> None:
        other = _Resident("sess-dm")
        rig.manager._router.session(SessionKey(SessionKind.DM, LARK.agent_id, "oc_synthetic_dm_other")).claude = other
        mine = await _published(rig, _lark("我的", "oc_synthetic_dm_mine", OPEN_ID, group=False))
        assert await rig.reply(mine) == "sess-main 回复 1"
        theirs = await _published(rig, _lark("他的", "oc_synthetic_dm_other", OTHER, group=False))
        assert await rig.reply(theirs) == "sess-dm 回复 1"
        assert len(rig.main.turns) == 1 and len(other.turns) == 1

    _run(test, routed, tmp_path)


def test_a_message_cut_off_in_its_session_is_replayed_at_restart(routed: Any, tmp_path: Path) -> None:
    """A session's worker settles a message only once it is handled: stopped mid-turn, the journal keeps it."""
    async def body(pool: Any) -> None:
        rig = _Rig(tmp_path)
        await rig.start()
        group = rig.group(GROUP_A, _Resident("sess-a", hold=True))
        question = await _published(rig, _lark("你好", GROUP_A, OTHER, group=True))
        await asyncio.wait_for(group.running.wait(), 20)
        await rig.manager.stop()
        await rig.bus.shutdown()
        restarted = MessageBus(runtime_root=tmp_path)
        await restarted.start()
        assert [e.message_id for e in await restarted.drain_queue("manager")] == [question.message_id]
        await restarted.shutdown()

    run_db(body, routed, timeout=90)


@pytest.mark.parametrize("end", ("started", "failed", "stopped"))
def test_a_start_reserves_capacity_until_it_starts_fails_or_is_stopped(routed: Any, tmp_path: Path,
                                                                     end: str) -> None:
    class StartingResident(_Resident):
        def __init__(self) -> None:
            super().__init__("sess-a")
            self.starting, self.allow_start = asyncio.Event(), asyncio.Event()

        async def start(self, command: list[str], **kwargs: Any) -> None:
            self.starting.set()
            await self.allow_start.wait()
            if end == "failed":
                raise OSError("synthetic start failure")
            await super().start(command, **kwargs)

    async def test(rig: _Rig, pool: Any) -> None:
        await rig.reply(await _published(rig, _web("hello")))
        starting = rig.group(GROUP_A, StartingResident())
        other = rig.group(GROUP_B, _Resident("sess-b"))
        first = await _published(rig, _lark("first", GROUP_A, OTHER, group=True))
        await asyncio.wait_for(starting.starting.wait(), 20)
        second = await _published(rig, _lark("second", GROUP_B, OTHER, group=True))
        await asyncio.sleep(0.2)
        assert not other.starts, "the resident cap includes the first group's start in progress"
        if end == "stopped":
            await rig.manager.stop_turn(first.message_id, via="synthetic-test")
        starting.allow_start.set()
        await rig.reply(first)
        assert await rig.reply(second) == "sess-b 回复 1"
        assert not rig.manager._router._reservations
        assert rig.manager._router.resident_count() <= 2

    _run(test, routed, tmp_path)
