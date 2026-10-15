"""/new: the user starts a new Manager session from any chat (web, Lark, Telegram).

Looper answers a message that is exactly ``/new`` itself, never Claude: the
Manager stops its resident Claude and drops the saved session, so the next
message starts a new session without ``--resume`` and without the resume
brief a new session otherwise gets. The reset is recorded as a run and
confirmed in the chat it came from. Claude is a stand-in throughout
(``_resident``, which here also logs each user turn it reads), and the
database is ``agent_db``'s in-memory one.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from looper.agents.bus import MessageBus
from looper.agents.message import MANAGER_ADDRESS, MessageEnvelope, MessageType
from looper.agents.tiers.manager import NEW_SESSION_REPLY, ManagerAgent, ManagerConfig
from tests.test_agent_channels import OPEN_ID, OTHER_OPEN_ID, _allowlist, _lark_event, _tg_update
from tests.test_manager_reply import WEB, _fake_claude, _flag, _resident
from tests.test_task_capture import CHATS

# Appends each user turn the stand-in reads to turns.jsonl in its working directory.
_LOG_TURNS = (
    "class _Logged:\n"
    "    def __init__(self, stream):\n"
    "        self.stream = stream\n"
    "    def __iter__(self):\n"
    "        for line in self.stream:\n"
    "            open('turns.jsonl', 'a').write(line)\n"
    "            yield line\n"
    "sys.stdin = _Logged(sys.stdin)\n"
)
# Holds the stand-in's first turn (after writing busy) until a file go exists.
_HOLD_FIRST = (
    "class _Held:\n"
    "    def __init__(self, stream):\n"
    "        self.stream = stream\n"
    "    def __iter__(self):\n"
    "        for n, line in enumerate(self.stream):\n"
    "            if n == 0:\n"
    "                open('busy', 'w').write('')\n"
    "                while not os.path.exists('go'):\n"
    "                    time.sleep(0.02)\n"
    "            yield line\n"
    "sys.stdin = _Held(sys.stdin)\n"
)


def _web(text: str, index: int = 0) -> MessageEnvelope:
    """A message from the web chat ``session-a``."""
    return MessageEnvelope.create(
        source=WEB, target=MANAGER_ADDRESS, message_type=MessageType.USER_MESSAGE,
        payload={"text": text, "channel_context": {"session_id": "session-a"}}, correlation_id=f"corr-{index}",
    )


def _drain(queue: asyncio.Queue[MessageEnvelope]) -> list[MessageEnvelope]:
    envelopes = []
    while not queue.empty():
        envelopes.append(queue.get_nowait())
    return envelopes


def _saved(agent_db: Any, session_id: str = "sess-0") -> None:
    """The Manager's session as an earlier run of the server saved it."""
    agent_db.store.sessions["manager"] = {"session_id": session_id, "session_totals": {"cost_usd": 0.5},
                                          "runtime_type": "claude-code", "profile_id": None}


def _resets(agent_db: Any) -> list[dict[str, Any]]:
    """The ``start_record`` keywords of the resets recorded, in order."""
    return [call.kwargs for call in agent_db.start_record.await_args_list
            if "session_reset" in (call.kwargs["metadata"] or {})]


def _chat(
    monkeypatch: pytest.MonkeyPatch, agent_db: Any, messages: list[str], *, restart_before: int | None = None,
) -> SimpleNamespace:
    """Send ``messages`` one at a time from the web chat to a Manager backed by the logging stand-in.

    With ``restart_before`` the Manager is stopped before that message and a
    new one, as after a server restart, takes the rest. Returns the final
    ``replies``, each started Claude's ``argv``, the ``turns`` the stand-ins
    read (each message, after the context sections), the ``runs`` Claude was
    given (the ``input_text`` of every run but a reset) and, after each
    message, whether each started Claude was still ``running``.
    """
    spawned: list[tuple[list[str], Any]] = []
    real_exec = asyncio.create_subprocess_exec

    async def _exec(*args: Any, **kwargs: Any) -> Any:
        process = await real_exec(*args, **kwargs)
        spawned.append(([str(a) for a in args[1:]], process))
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _exec)

    async def _run() -> tuple[list[str], list[str], list[list[bool]]]:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            claude = _fake_claude(root, _LOG_TURNS + _resident())
            bus = MessageBus(runtime_root=root)
            queue = bus.get_queue(WEB.agent_id)
            manager: ManagerAgent | None = None
            replies: list[str] = []
            running: list[list[bool]] = []
            for index, text in enumerate(messages):
                if manager is None or index == restart_before:
                    if manager is not None:
                        await manager.stop()
                    manager = ManagerAgent(config=ManagerConfig(claude_bin=claude, work_dir=root), runtime_root=root)
                    manager._bus = bus
                await manager._handle(_web(text, index))
                replies += [e.payload["text"] for e in _drain(queue) if e.message_type == MessageType.AGENT_REPLY]
                running.append([process.returncode is None for _, process in spawned])
            assert manager is not None
            await manager.stop()
            await bus.shutdown()
            log = root / "turns.jsonl"
            turns = [json.loads(line)["message"]["content"].rpartition("\n\n")[2]
                     for line in (log.read_text().splitlines() if log.exists() else [])]
            return replies, turns, running

    replies, turns, running = asyncio.run(_run())
    runs = [call.kwargs["input_text"] for call in agent_db.start_record.await_args_list
            if "session_reset" not in (call.kwargs["metadata"] or {})]
    return SimpleNamespace(replies=replies, argvs=[argv for argv, _ in spawned], turns=turns, runs=runs,
                           running=running)


async def _until(check: Callable[[], bool], timeout: float = 10.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not check():
        assert loop.time() < deadline, "timed out"
        await asyncio.sleep(0.02)


@pytest.mark.usefixtures("agent_db")
class TestNewSession:
    @pytest.mark.parametrize("restart", [False, True], ids=["same-server", "after-a-restart"])
    def test_the_message_after_new_starts_a_clean_session(
        self, monkeypatch: pytest.MonkeyPatch, agent_db: Any, restart: bool,
    ) -> None:
        agent_db.recent_conversations.return_value = CHATS  # what a new session's resume brief would carry
        _saved(agent_db)
        chat = _chat(monkeypatch, agent_db, ["the codeword is 42", "/new", "what is the codeword?"],
                     restart_before=2 if restart else None)
        assert chat.replies == ["turn 1: the codeword is 42", NEW_SESSION_REPLY, "turn 1: what is the codeword?"]
        first, second = chat.argvs
        assert _flag(first, "--resume") == "sess-0"
        assert chat.running[1] == [False]  # /new stopped the resident Claude
        assert _flag(second, "--resume") is None
        # No resume brief: the new session's first run is given the message alone.
        assert chat.runs == ["the codeword is 42", "what is the codeword?"]
        saved = agent_db.store.sessions["manager"]
        assert (saved["session_id"], saved["clean_start"]) == ("sess-1", False)  # the new session, now saved

    @pytest.mark.parametrize(("text", "command"), [
        ("/new", True), ("  /NEW\n", True), ("/New", True),
        ("/new foo", False), ("/news", False), ("new", False), ("/ new", False), ("start a /new session", False),
    ])
    def test_only_new_itself_is_answered_without_claude(
        self, monkeypatch: pytest.MonkeyPatch, agent_db: Any, text: str, command: bool,
    ) -> None:
        chat = _chat(monkeypatch, agent_db, ["hello", text])
        if command:
            assert chat.turns == ["hello"]
            assert chat.replies == ["turn 1: hello", NEW_SESSION_REPLY]
        else:  # an ordinary message, in the same session
            assert chat.turns == ["hello", text]
            assert chat.replies == ["turn 1: hello", f"turn 2: {text}"]
            assert _resets(agent_db) == []

    def test_a_turn_under_way_finishes_before_the_reset(self, agent_db: Any) -> None:
        async def _run() -> list[str]:
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                claude = _fake_claude(root, _HOLD_FIRST + _resident())
                bus = MessageBus(runtime_root=root)
                manager = ManagerAgent(config=ManagerConfig(claude_bin=claude, work_dir=root), runtime_root=root)
                await manager.start(bus)  # its loop takes the bus's messages, as the server's does
                await bus.publish(_web("hello", 0))
                await _until((root / "busy").exists)
                await bus.publish(_web("/new", 1))
                await asyncio.sleep(0.3)  # long enough for /new to act, had it not waited
                (root / "go").write_text("")
                queue = bus.get_queue(WEB.agent_id)
                replies: list[str] = []
                while len(replies) < 2:
                    envelope = await asyncio.wait_for(queue.get(), 10)
                    if envelope.message_type == MessageType.AGENT_REPLY:
                        replies.append(envelope.payload["text"])
                await manager.stop()
                await bus.shutdown()
                return replies

        assert asyncio.run(_run()) == ["turn 1: hello", NEW_SESSION_REPLY]
        turn = agent_db.finish_record.await_args_list[0].kwargs
        assert (turn["status"], turn["error"], turn["output_text"]) == ("completed", None, "turn 1: hello")
        # The turn saved its session; the reset dropped it after.
        assert [call.args[1] for call in agent_db.save_session.await_args_list][-2:] == ["sess-1", None]

    def test_the_reset_is_recorded_as_a_run_of_the_manager(
        self, monkeypatch: pytest.MonkeyPatch, agent_db: Any,
    ) -> None:
        _saved(agent_db)
        chat = _chat(monkeypatch, agent_db, ["/new"])
        assert chat.argvs == []  # no Claude ran
        (reset,) = _resets(agent_db)
        assert (reset["agent_id"], reset["session_id"], reset["input_text"]) == ("manager", "sess-0", "/new")
        assert reset["metadata"]["session_reset"] == {"channel": "web", "sender": None, "chat": "session-a",
                                                      "previous_session_id": "sess-0"}
        (finish,) = agent_db.finish_record.await_args_list
        assert (finish.kwargs["status"], finish.kwargs["output_text"]) == ("completed", NEW_SESSION_REPLY)

    def test_task_planner_sessions_are_left_alone(self, monkeypatch: pytest.MonkeyPatch, agent_db: Any) -> None:
        _saved(agent_db)
        planner = {"session_id": "sess-p", "session_totals": {"cost_usd": 1.0}, "runtime_type": "claude-code",
                   "profile_id": None}
        agent_db.store.sessions["planner-t1"] = dict(planner)
        _chat(monkeypatch, agent_db, ["/new"])
        assert agent_db.store.sessions["planner-t1"] == planner
        assert {call.args[0] for call in agent_db.save_session.await_args_list} == {"manager"}


@pytest.mark.usefixtures("agent_db")
class TestNewFromLarkAndTelegram:
    """/new from Lark or Telegram passes the channel's sender allowlist like any message, and the confirmation goes
    back to the chat it came from."""

    @pytest.mark.parametrize("listed", [True, False], ids=["listed", "unlisted"])
    @pytest.mark.parametrize("channel", ["lark", "telegram"])
    def test_only_a_listed_sender_starts_a_new_session(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, agent_db: Any, channel: str, listed: bool,
    ) -> None:
        from looper.agents.channels.lark import LarkChannelAdapter
        from looper.agents.channels.telegram import TelegramChannelAdapter

        _saved(agent_db)
        sent: list[tuple[Any, str]] = []

        async def _send_lark(self: Any, chat_id: str, text: str) -> None:
            sent.append((chat_id, text))

        async def _send_telegram(*, chat_id: int, text: str, **_: Any) -> Any:
            sent.append((chat_id, text))
            return SimpleNamespace(message_id=1)

        monkeypatch.setattr(LarkChannelAdapter, "_send_lark_message", _send_lark)

        async def _main() -> None:
            bus = MessageBus(runtime_root=tmp_path)
            adapter: Any
            if channel == "lark":
                adapter = LarkChannelAdapter(runtime_root=tmp_path, allowlist=_allowlist("channel-lark", OPEN_ID))
                adapter._bus = bus  # what start() sets, without running lark-cli
                await adapter._handle_lark_event(_lark_event(OPEN_ID if listed else OTHER_OPEN_ID, "/new"))
            else:
                adapter = TelegramChannelAdapter(bot_token="0:not-a-token",
                                                 allowlist=_allowlist("channel-telegram", "7"))
                adapter._bus = bus  # what start() sets, without polling Telegram
                adapter._app = SimpleNamespace(bot=SimpleNamespace(send_message=_send_telegram))
                await adapter._cmd_new(_tg_update(7 if listed else 8, "/new"), SimpleNamespace(args=[]))
            manager = ManagerAgent(config=ManagerConfig(work_dir=tmp_path), runtime_root=tmp_path)
            manager._bus = bus
            for envelope in _drain(bus.get_queue("manager")):
                await manager._handle(envelope)
            for envelope in _drain(bus.get_queue(adapter.channel_id)):
                await adapter.deliver(envelope)
            await manager.stop()
            await bus.shutdown()

        asyncio.run(_main())
        saved = agent_db.store.sessions["manager"]
        if not listed:
            assert saved["session_id"] == "sess-0" and _resets(agent_db) == [] and sent == []
            return
        assert (saved["session_id"], saved["clean_start"]) == (None, True)
        chat, sender = ("oc_1", OPEN_ID) if channel == "lark" else (99, "7")
        assert sent == [(chat, NEW_SESSION_REPLY)]
        (reset,) = _resets(agent_db)
        assert reset["metadata"]["session_reset"] == {"channel": channel, "sender": sender, "chat": str(chat),
                                                      "previous_session_id": "sess-0"}
