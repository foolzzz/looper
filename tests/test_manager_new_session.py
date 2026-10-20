"""/new: the user starts a new Manager session from any chat (web, Lark, Telegram).

Looper answers a message that is exactly ``/new`` itself, never Claude: the
Manager stops its resident Claude and drops the saved session, so the next
message starts a new session without ``--resume`` and without the resume
brief a new session otherwise gets. The reset is recorded as a run and
confirmed in the chat it came from. Claude and DSH use real subprocess
stand-ins; DSH reads one turn per process and owns its opaque session state.
The database is ``agent_db``'s in-memory one.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from collections.abc import Callable
from dataclasses import replace
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


def _dsh_manager(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, agent_db: Any, *, fail_second: bool = False,
                 opening_before_failure: bool = True) -> SimpleNamespace:
    """A real headless process per turn, persisting only its own opaque Session state."""
    from unittest.mock import AsyncMock

    from fake_claude import fake_claude
    from looper.agents.tiers import manager as module
    from tests.test_dsh_runtime import _dsh_step, _run_launch

    observed = tmp_path / "dsh-turns.jsonl"
    executable = fake_claude(tmp_path / "manager-dsh", f'''
import json, os, pathlib, sys, uuid
args = sys.argv[1:]
prompt = sys.stdin.read()
home = pathlib.Path(os.environ['DSH_HOME'])
state_file = home / 'stand-in-state.json'
state = json.loads(state_file.read_text()) if state_file.exists() else {{}}
saved = args[args.index('--session-id') + 1] if '--session-id' in args else None
session = saved or uuid.uuid4().hex
missing = saved is not None and saved not in state
turn = state.get(session, 0) + 1
patch = pathlib.Path(args[args.index('--patch') + 1])
with open({str(observed)!r}, 'a') as output:
    output.write(json.dumps({{'argv': args, 'prompt': prompt, 'home': str(home),
                             'session': session, 'patch': str(patch), 'executed': not missing,
                             'bootstrap': (patch.parent / 'looper-bootstrap.mjs').read_text(),
                             'token': os.environ.get('LOOPER_DSH_MCP_SECRET_0_ENV_0')}}) + '\\n')
if missing:
    print(json.dumps({{'type': 'error', 'message': 'Session not found'}}), flush=True)
    sys.exit(4)
state[session] = turn
state_file.write_text(json.dumps(state))
if not ({fail_second!r} and turn == 2) or {opening_before_failure!r}:
    print(json.dumps({{'type': 'session', 'sessionId': session}}), flush=True)
if {fail_second!r} and turn == 2:
    print(json.dumps({{'type': 'tool_call', 'callId': 'write-once', 'tool': 'write',
                      'input': {{'path': 'side-effect'}}}}), flush=True)
    sys.exit(7)
for record in [{_dsh_step()!r},
               {{'type': 'status', 'phase': 'turn_end', 'reason': {{'kind': 'completed'}}}},
               {{'type': 'final', 'text': f'dsh turn {{turn}}: ' + prompt.rpartition('\\n\\n')[2]}}]:
    print(json.dumps(record), flush=True)
''')
    launch = _run_launch(tmp_path, role="manager")
    profile = replace(launch.profile, runtime=replace(launch.profile.runtime, executable=executable))
    state = SimpleNamespace(settings=replace(launch.settings, profile=profile), library=launch.library,
                            observed=observed)
    # The generic legacy store predates provider persistence; retain it for restart compatibility checks.
    save_session = agent_db.save_session.side_effect

    async def save_with_provider(*args: Any, provider_id: str | None = None, **kwargs: Any) -> None:
        await save_session(*args, **kwargs)
        agent_db.store.sessions[args[0]]["provider_id"] = provider_id

    agent_db.save_session.side_effect = save_with_provider
    monkeypatch.setattr(module, "load_agent_settings", AsyncMock(side_effect=lambda *_: state.settings))
    monkeypatch.setattr(module, "load_installed", AsyncMock(side_effect=lambda *_: state.library))
    return state


def _dsh_turns(state: SimpleNamespace) -> list[dict[str, Any]]:
    return [json.loads(line) for line in state.observed.read_text().splitlines()]


def test_dsh_manager_persists_opening_session_and_resumes_after_restart_and_new(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, agent_db: Any,
) -> None:
    state = _dsh_manager(tmp_path, monkeypatch, agent_db)

    async def run() -> None:
        bus = MessageBus(runtime_root=tmp_path)
        queue = bus.get_queue(WEB.agent_id)
        manager = ManagerAgent(config=ManagerConfig(work_dir=tmp_path), runtime_root=tmp_path)
        manager._bus = bus
        try:
            await manager._handle(_web("first", 0))
            opening = _dsh_turns(state)[0]
            assert agent_db.store.sessions["manager"]["session_id"] == opening["session"]
            await manager.stop()
            manager = ManagerAgent(config=ManagerConfig(work_dir=tmp_path), runtime_root=tmp_path)
            manager._bus = bus
            await manager._handle(_web("second", 1))
            await manager._handle(_web("/new", 2))
            await manager._handle(_web("fresh", 3))
            replies = [e.payload["text"] for e in _drain(queue) if e.message_type == MessageType.AGENT_REPLY]
            assert replies == ["dsh turn 1: first", "dsh turn 2: second", NEW_SESSION_REPLY, "dsh turn 1: fresh"]
        finally:
            await manager.stop()
            await bus.shutdown()

    asyncio.run(asyncio.wait_for(run(), 15))
    first, second, fresh = _dsh_turns(state)
    assert _flag(first["argv"], "--session-id") is None
    assert _flag(second["argv"], "--session-id") == first["session"]
    assert _flag(fresh["argv"], "--session-id") is None
    assert fresh["session"] != first["session"]
    assert first["home"] == second["home"] == fresh["home"]
    records = [call.kwargs for call in agent_db.finish_record.await_args_list if call.kwargs.get("input_tokens")]
    assert len(records) == 3
    assert [record["input_tokens"] for record in records] == [10, 10, 10]
    assert [record["output_tokens"] for record in records] == [4, 4, 4]


def test_dsh_manager_reconfiguration_resumes_same_owner_with_new_overlay_and_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, agent_db: Any,
) -> None:
    from looper.agents.library import AgentLibrary
    from looper.agents.ops.service import server_config

    state = _dsh_manager(tmp_path, monkeypatch, agent_db)

    async def run() -> None:
        bus = MessageBus(runtime_root=tmp_path)
        queue = bus.get_queue(WEB.agent_id)
        manager = ManagerAgent(config=ManagerConfig(work_dir=tmp_path), runtime_root=tmp_path)
        manager._bus = bus
        try:
            await manager._handle(_web("before", 0))
            state.settings = replace(state.settings, profile=replace(
                state.settings.profile, model="deepseek-v4-pro", custom_prompt="NEW {{literal}} PROMPT"))
            config = tmp_path / "rotated-mcp.json"
            config.write_text(json.dumps({"mcpServers": {
                "looper": server_config("http://127.0.0.1:19999", "rotated-test-token"),
            }}))
            state.library = AgentLibrary(mcp_config=str(config), role="manager")
            await manager._handle(_web("after", 1))
            replies = [e.payload["text"] for e in _drain(queue) if e.message_type == MessageType.AGENT_REPLY]
            assert replies == ["dsh turn 1: before", "dsh turn 2: after"]
        finally:
            await manager.stop()
            await bus.shutdown()

    asyncio.run(asyncio.wait_for(run(), 15))
    first, second = _dsh_turns(state)
    assert _flag(second["argv"], "--session-id") == first["session"]
    assert second["home"] == first["home"]
    assert second["patch"] != first["patch"]
    assert "NEW {{literal}} PROMPT" in second["bootstrap"]
    assert "deepseek-v4-pro" in Path(second["patch"]).read_text()
    assert (first["token"], second["token"]) == ("test-run-token", "rotated-test-token")
    assert agent_db.store.sessions["manager"]["session_id"] == first["session"]


@pytest.mark.parametrize("opening_before_failure", [True, False], ids=["opening-seen", "tool-without-opening"])
def test_dsh_manager_does_not_replay_turn_after_tool_call_without_final(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, agent_db: Any, opening_before_failure: bool,
) -> None:
    state = _dsh_manager(tmp_path, monkeypatch, agent_db, fail_second=True,
                         opening_before_failure=opening_before_failure)

    async def run() -> None:
        bus = MessageBus(runtime_root=tmp_path)
        manager = ManagerAgent(config=ManagerConfig(work_dir=tmp_path), runtime_root=tmp_path)
        manager._bus = bus
        try:
            await manager._handle(_web("first", 0))
            # A restored owner takes the tier's saved-session recovery path, where replay could duplicate tools.
            await manager.stop()
            manager = ManagerAgent(config=ManagerConfig(work_dir=tmp_path), runtime_root=tmp_path)
            manager._bus = bus
            await manager._handle(_web("write exactly once", 1))
            assert len(_dsh_turns(state)) == 2
            await manager._handle(_web("recover", 2))
        finally:
            await manager.stop()
            await bus.shutdown()

    asyncio.run(asyncio.wait_for(run(), 15))
    turns = _dsh_turns(state)
    assert len(turns) == 3
    assert sum("write exactly once" in turn["prompt"] for turn in turns) == 1
    assert _flag(turns[2]["argv"], "--session-id") == turns[0]["session"]


@pytest.mark.parametrize(("runtime", "provider"), [("dsh", "previous-provider"),
                                                    ("claude-code", "prov-claude-subscription")])
def test_dsh_manager_replaces_saved_session_from_another_provider_or_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, agent_db: Any, runtime: str, provider: str,
) -> None:
    state = _dsh_manager(tmp_path, monkeypatch, agent_db)
    agent_db.store.sessions["manager"] = {
        "session_id": "previous-session", "session_totals": {"input_tokens": 999},
        "runtime_type": runtime, "profile_id": "previous-profile", "provider_id": provider,
    }

    async def run() -> None:
        bus = MessageBus(runtime_root=tmp_path)
        manager = ManagerAgent(config=ManagerConfig(work_dir=tmp_path), runtime_root=tmp_path)
        manager._bus = bus
        try:
            await manager._handle(_web("continue", 0))
        finally:
            await manager.stop()
            await bus.shutdown()

    asyncio.run(asyncio.wait_for(run(), 15))
    (turn,) = _dsh_turns(state)
    assert _flag(turn["argv"], "--session-id") is None
    assert "[RESUME] This is a new session (agent changed" in turn["prompt"]
    assert agent_db.store.sessions["manager"]["session_id"] == turn["session"]
    assert agent_db.store.sessions["manager"]["session_totals"]["input_tokens"] == 10


def test_dsh_manager_replaces_missing_session_with_fresh_session_and_resume_brief(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, agent_db: Any,
) -> None:
    state = _dsh_manager(tmp_path, monkeypatch, agent_db)
    agent_db.store.sessions["manager"] = {
        "session_id": "missing-session", "session_totals": {"input_tokens": 999},
        "runtime_type": "dsh", "profile_id": state.settings.profile.id,
        "provider_id": state.settings.profile.provider.id,
    }

    async def run() -> None:
        bus = MessageBus(runtime_root=tmp_path)
        queue = bus.get_queue(WEB.agent_id)
        manager = ManagerAgent(config=ManagerConfig(work_dir=tmp_path), runtime_root=tmp_path)
        manager._bus = bus
        try:
            await manager._handle(_web("continue once", 0))
            replies = [e.payload["text"] for e in _drain(queue) if e.message_type == MessageType.AGENT_REPLY]
            assert replies == ["dsh turn 1: continue once"]
        finally:
            await manager.stop()
            await bus.shutdown()

    asyncio.run(asyncio.wait_for(run(), 15))
    attempt, fresh = _dsh_turns(state)
    assert _flag(attempt["argv"], "--session-id") == "missing-session"
    assert _flag(fresh["argv"], "--session-id") is None
    assert (attempt["executed"], fresh["executed"]) == (False, True)
    assert "[RESUME] This is a new session (session lost:" in fresh["prompt"]
    assert fresh["prompt"].endswith("continue once")
    assert attempt["home"] == fresh["home"]
    saved = agent_db.store.sessions["manager"]
    assert saved["session_id"] == fresh["session"] != "missing-session"
    assert saved["session_totals"]["input_tokens"] == 10


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
                                                      "group": False, "previous_session_id": "sess-0"}
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
            await adapter._reactions.stop()  # what the channel's stop() does with a reaction call still running
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
                                                      "group": False, "previous_session_id": "sess-0"}
