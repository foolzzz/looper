"""The user's stop of a Manager turn in each phase of the turn, over each runtime's stand-in process.

A real ``ManagerAgent`` (on ``agent_db`` and a bus) runs the stand-ins of ``tests/stop_matrix_support.py`` (real
pipes, real process trees) under the runtimes' own sessions. The turn is stopped in one phase:

- ``received``: the message waits behind another turn;
- ``preparing``: held at the first step of the preparation;
- ``starting``: held at the runtime's start, and (Claude Code) during the handoff to a new session;
- ``running``: streaming, and a tool in progress (a process in a session of its own, and a write call that comes
  after the stop);
- ``ended``: the turn has been answered.

Each cell checks what the page and the next turn see: ``POST /api/v1/operator_chat/turn`` while the turn is held, the record's
``user_stop.phase``, the reply's outcome, the note the next turn is told (the user stopped it, or withdrew it), that
the next turn is answered in the same session, and which processes the stop ended: none when the runtime ended the
turn on its interrupt (a tool it left running is its own), the agent with its tree when it ignored it. The runtimes
run per turn (Codex, OMP, pi, DSH) have no interrupt: their stop is refused and the reply runs on. A phase that does not
reach the runtime (received, preparing, ended) runs over one runtime, and so does that refusal. The runtimes' own windows
are in ``tests/test_stop_matrix.py``.
"""

from __future__ import annotations

import asyncio
import json
import urllib.request
from collections.abc import Awaitable, Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from fake_claude import fake_claude
from looper.agents import chat_settings, claude_cli, proc_tree
from looper.agents.agent_settings import AgentSettings
from looper.agents.bus import MessageBus
from looper.agents.message import MessageEnvelope, MessageType
from looper.agents.ops import Call, Risk, Tool
from looper.agents.ops.service import ManagerOps, Platform
from looper.agents.runtimes import grok_acp
from looper.agents.tiers import manager as manager_module
from looper.agents.tiers.manager import PREPARATION_STEPS, ManagerAgent, ManagerConfig, StopResult
from looper.agents.tiers.turn_token import TurnPhase
from tests.stop_faults import hold_at
from tests.stop_matrix_support import BOUND, _CLAUDE, _alive, _per_turn_parts, _until
from tests.test_manager_new_session import _web
from tests.test_manager_reply import WEB

STOPPABLE = ["claude-obeys", "claude-deaf", "grok-acp"]  # the runtimes with an interrupt (``can_interrupt``)
# The phases that do not reach the runtime (received, preparing, ended) and the refusal of a runtime without an
# interrupt (the same ``PerTurnSession`` for Codex, OMP, pi and DSH) run over one runtime each: they would run the same
# code over the others.
SHARED, PER_TURN = "claude-obeys", "codex"
# How each runtime's stand-in is told what to do: Grok's by the message's text, the others' by a ``mode`` file.
GROK_TEXT = {"streaming": "HANG", "tool": "SLEEPER", "answer": "work"}
PAD = "整理一下今天的待办事项并告诉我结果，不用着急，也不必写得太长。" * 4  # (the note quotes the first 80 characters)
STOPPED_NOTE = "[Looper] The user stopped your previous reply"
WITHDREW_NOTE = "[Looper] The user withdrew"
METHOD = {"claude-obeys": "control", "claude-deaf": "kill", "grok-acp": "cancel"}


@pytest.fixture(autouse=True)
def _fast(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stopping is on; the graces are fractions of a second (the stand-ins answer or are deaf at once)."""
    monkeypatch.setattr(chat_settings, "_stop_enabled", True)
    monkeypatch.setattr(claude_cli, "INTERRUPT_GRACE_SEC", 0.5)
    monkeypatch.setattr(claude_cli, "STOP_GRACE_SEC", 0.5)
    monkeypatch.setattr(grok_acp, "CANCEL_GRACE_SEC", 0.5)
    monkeypatch.setattr(proc_tree, "TERM_GRACE_SEC", 0.2)


class Rig:
    """A started Manager over one runtime's stand-in, and what a cell needs to drive and read it."""

    def __init__(self, runtime: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, agent_db: Any) -> None:
        self.runtime, self.agent_db, self.monkeypatch = runtime, agent_db, monkeypatch
        self.root = tmp_path / "runtime"
        self.root.mkdir()
        self.directory = tmp_path / "matrix"
        self.directory.mkdir(exist_ok=True)
        self.replies: list[dict[str, Any]] = []
        self.pending = "answer"
        self.sent = 0
        self.settings = self._settings(tmp_path)
        self.bus = MessageBus(runtime_root=self.root)
        self.queue = self.bus.get_queue(WEB.agent_id)
        self.manager = ManagerAgent(config=ManagerConfig(claude_bin=self.claude, work_dir=self.root),
                                    runtime_root=self.root)
        self._load_real = manager_module.load_agent_settings
        monkeypatch.setattr(manager_module, "load_agent_settings", AsyncMock(side_effect=self._load_settings))

    # --- The runtime ---

    def _settings(self, tmp_path: Path) -> Any:
        self.claude = "claude"
        env = (("STOP_MATRIX_DIR", str(self.directory)),)
        if self.runtime.startswith("claude"):
            self.claude = fake_claude(tmp_path / "claude", _CLAUDE)
            return None, env
        if self.runtime == "grok-acp":
            from test_grok_acp import _fake
            from test_grok_runtime import _launch, _operator

            _operator(tmp_path, self.monkeypatch)
            return _launch(executable=_fake(tmp_path)).settings, env
        return _per_turn_parts(tmp_path, self.runtime)[4].settings, env

    async def _load_settings(self, *_: Any, **__: Any) -> AgentSettings:
        settings, env = self.settings
        if settings is None:  # Claude Code's: the built-in profile the database would give
            settings = await self._load_real(*_, **__)
        from dataclasses import replace

        return replace(settings, env=(*settings.env, *env))

    def mode(self, name: str) -> None:
        """What the next turn's stand-in does (Grok's is told by the message)."""
        if self.runtime != "grok-acp":
            suffix = " obey" if self.runtime == "claude-obeys" else ""
            (self.directory / "mode").write_text(name + (suffix if self.runtime.startswith("claude") else ""))
        self.pending = name

    def text(self, marker: str = "") -> str:
        trigger = GROK_TEXT[self.pending] if self.runtime == "grok-acp" else ""
        return f"{PAD} {marker} {trigger}".strip()

    @property
    def session(self) -> Any:
        return self.manager._claude

    @property
    def resident_pids(self) -> set[int]:
        """The processes a stopped turn leaves: the runtime's own one (and its MCP bridge), when it keeps one."""
        pid = self.session.pid
        return {pid} if pid else set()

    def processes(self) -> list[int]:
        """Every process the stand-ins started, and the tool's in a session of its own."""
        found: list[int] = []
        pids = self.directory / "pids"
        if pids.exists():
            found += [int(line) for line in pids.read_text().split()]
        for sleeper in self.root.glob(".local/grok/sleeper.pid"):
            found.append(int(sleeper.read_text()))
        argv = self.root / ".local" / "grok" / "acp-argv.jsonl"
        if argv.exists():
            found += [json.loads(line)["pid"] for line in argv.read_text().splitlines()]
        return found

    def left(self) -> list[int]:
        """The processes of the turns so far that are still there, but for the runtime's own."""
        return [pid for pid in self.processes() if pid not in self.resident_pids and _alive(pid)]

    def reached(self) -> bool:
        if self.runtime == "grok-acp":
            return bool(self.session.pid) and self.session._prompt_id is not None
        return (self.directory / "reached").exists()

    # --- The turns ---

    async def start(self) -> None:
        await self.manager.start(self.bus)

    async def send(self, marker: str = "") -> MessageEnvelope:
        envelope = _web(self.text(marker), self.sent)
        self.sent += 1
        await self.bus.publish(envelope)
        return envelope

    async def replied(self, count: int) -> list[dict[str, Any]]:
        """The replies so far, once there are ``count`` of them."""
        async def collect() -> None:
            while True:
                while not self.queue.empty():
                    envelope = self.queue.get_nowait()
                    if envelope.message_type == MessageType.AGENT_REPLY:
                        self.replies.append(envelope.payload)
                if len(self.replies) >= count:
                    return
                await asyncio.sleep(0.02)

        await asyncio.wait_for(collect(), BOUND)
        return self.replies

    async def answer(self, marker: str = "") -> MessageEnvelope:
        """A turn the stand-in answers."""
        self.mode("answer")
        envelope = await self.send(marker)
        await self.replied(self.sent)
        assert self.replies[-1]["outcome"] == "answered", self.replies[-1]
        return envelope

    def token(self, envelope: MessageEnvelope) -> Any:
        token = self.manager.turn_for(envelope.message_id)
        assert token is not None
        return token

    async def api_turn(self) -> dict[str, Any] | None:
        """``POST /api/v1/operator_chat/turn`` of the chat the messages are in."""
        from looper.agents.http_server import AgentHTTPServer

        class Writer:
            data = b""

            def write(self, data: bytes) -> None:
                self.data += data

            async def drain(self) -> None:
                pass

        server = AgentHTTPServer(web_adapter=SimpleNamespace(), supervisor=SimpleNamespace(),
                                 task_router=SimpleNamespace(), manager=self.manager)
        writer = Writer()
        from looper.agents.api_v1 import OPERATOR
        await server._handle_v1("POST", "/api/v1/operator_chat/turn", {}, {},
                                b'{"session_id":"session-a"}', writer, OPERATOR)
        return json.loads(writer.data.partition(b"\r\n\r\n")[2])["result"]["turn"]

    def user_stops(self) -> list[dict[str, Any]]:
        return [call.args[1]["user_stop"] for call in self.agent_db.add_metadata.await_args_list
                if "user_stop" in call.args[1]]

    def records(self) -> list[str]:
        """The input of each run the Manager recorded."""
        return [call.kwargs.get("input_text", "") for call in self.agent_db.start_record.await_args_list]

    async def finish(self) -> None:
        await self.manager.stop()
        await self.bus.shutdown()


async def _next_turn(rig: Rig, session_id: str | None, note: str | None, *, notes: int) -> None:
    """After a stop: the notes the next turn is to be told, and that it tells them and is answered in the session."""
    assert len(rig.manager._stopped_notes) == notes, rig.manager._stopped_notes
    if note is not None:
        assert rig.manager._stopped_notes[-1].startswith(note), rig.manager._stopped_notes
    records = len(rig.records())
    await rig.answer("NEXT")
    assert rig.manager._stopped_notes == []  # told once
    told = rig.records()[records:]
    if note is not None:
        assert len(told) == 1 and note in told[0], told
    assert rig.manager._session_id == session_id


def _case(runtime: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, agent_db: Any) -> Rig:
    return Rig(runtime, tmp_path, monkeypatch, agent_db)


def _run(body: Callable[[], Awaitable[None]]) -> None:
    asyncio.run(asyncio.wait_for(body(), 90))


# --- received ---


def test_a_message_stopped_while_it_waits_is_never_started(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, agent_db: Any,
) -> None:
    async def run() -> None:
        rig = _case(SHARED, tmp_path, monkeypatch, agent_db)
        await rig.start()
        try:
            await rig.answer("WARM")
            session_id = rig.manager._session_id
            rig.mode("streaming")
            ahead = await rig.send("AHEAD")
            await _until(rig.reached)
            waiting = await rig.send("WAITING")
            token = rig.token(waiting)
            assert token.phase is TurnPhase.RECEIVED
            shown = await rig.api_turn()
            assert shown is not None and shown["message_id"] == ahead.message_id and shown["state"] == "running"

            assert await rig.manager.stop_turn(waiting.message_id, via="web") is StopResult.STOPPING
            assert token.stop is not None and token.stop.phase is TurnPhase.RECEIVED
            assert await rig.manager.stop_turn(ahead.message_id, via="web") is StopResult.STOPPING
            replies = await rig.replied(rig.sent)
            assert [r["outcome"] for r in replies[-2:]] == ["stopped", "stopped"]
            assert not any("WAITING" in record for record in rig.records())  # the model never had it
            assert [stop["phase"] for stop in rig.user_stops()] == ["running"]  # (no record for the waiting one)
            assert rig.left() == []
            await _next_turn(rig, session_id, WITHDREW_NOTE, notes=2)
        finally:
            await rig.finish()

    _run(run)


# --- preparing ---


def test_a_message_stopped_while_it_is_prepared_never_reaches_the_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, agent_db: Any,
) -> None:
    async def run() -> None:
        rig = _case(SHARED, tmp_path, monkeypatch, agent_db)
        await rig.start()
        try:
            await rig.answer("WARM")
            session_id, records = rig.manager._session_id, len(rig.records())
            finished = len(agent_db.finish_record.await_args_list)
            hold = hold_at(monkeypatch, PREPARATION_STEPS[0])
            envelope = await rig.send("PREPARING")
            await asyncio.wait_for(hold.reached.wait(), BOUND)
            shown = await rig.api_turn()
            assert shown is not None and shown["state"] == "preparing" and shown["stopping"] is False
            assert await rig.manager.stop_turn(envelope.message_id, via="web") is StopResult.STOPPING
            shown = await rig.api_turn()
            assert shown is not None and shown["state"] == "stopping" and shown["stopping"] is True
            hold.release()
            replies = await rig.replied(rig.sent)
            assert replies[-1]["outcome"] == "stopped" and "error" not in replies[-1]
            assert len(rig.records()) == records and rig.user_stops() == []  # nothing ran: no record
            assert len(agent_db.finish_record.await_args_list) == finished
            assert replies[-1]["text"] == "已停止，这次没有回复。"
            assert rig.manager.turn_for(envelope.message_id) is None and await rig.api_turn() is None
            monkeypatch.setattr("looper.agents.tiers.turn_token.FAULTS", None)
            await _next_turn(rig, session_id, WITHDREW_NOTE, notes=1)
        finally:
            await rig.finish()

    _run(run)


# --- starting ---


@pytest.mark.parametrize("runtime", STOPPABLE)
def test_a_message_stopped_while_its_runtime_starts_is_not_sent(
    runtime: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, agent_db: Any,
) -> None:
    async def run() -> None:
        rig = _case(runtime, tmp_path, monkeypatch, agent_db)
        await rig.start()
        try:
            await rig.answer("WARM")
            session_id = rig.manager._session_id
            await rig.session.stop()  # the next turn starts the runtime again, resuming the session
            hold = hold_at(monkeypatch, "claude_start")
            envelope = await rig.send("STARTING")
            await asyncio.wait_for(hold.reached.wait(), BOUND)
            shown = await rig.api_turn()
            assert shown is not None and shown["state"] == "starting"
            assert await rig.manager.stop_turn(envelope.message_id, via="web") is StopResult.STOPPING
            hold.release()
            replies = await rig.replied(rig.sent)
            assert replies[-1]["outcome"] == "stopped" and "error" not in replies[-1]
            assert [stop["phase"] for stop in rig.user_stops()] == ["starting"]
            assert rig.user_stops()[0]["method"] is None  # (the prompt was not written)
            assert replies[-1]["text"] == "已停止，这次没有回复。"
            finish = agent_db.finish_record.await_args_list[-1].kwargs
            assert finish["status"] == "interrupted" and finish["cost_usd"] is None
            assert rig.left() == []
            monkeypatch.setattr("looper.agents.tiers.turn_token.FAULTS", None)
            await _next_turn(rig, session_id, WITHDREW_NOTE, notes=1)
        finally:
            await rig.finish()

    _run(run)


@pytest.mark.parametrize("runtime", ["claude-obeys", "claude-deaf"])
def test_a_message_stopped_during_the_handoff_keeps_the_old_session(
    runtime: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, agent_db: Any,
) -> None:
    async def run() -> None:
        rig = _case(runtime, tmp_path, monkeypatch, agent_db)
        await rig.start()
        try:
            await rig.answer("WARM")
            session_id, process = rig.manager._session_id, rig.session.pid
            rig.manager._session_runtime = "omp"  # the session was an omp's: the next one is a new one, after a handoff
            rig.mode("streaming")  # the handoff turn streams and waits
            envelope = await rig.send("HANDMSG")
            await _until(rig.reached)
            token = rig.token(envelope)
            assert token.phase in (TurnPhase.STARTING, TurnPhase.RUNNING)
            assert await rig.manager.stop_turn(envelope.message_id, via="web") is StopResult.STOPPING
            replies = await rig.replied(rig.sent)
            assert replies[-1]["outcome"] == "stopped" and "error" not in replies[-1]
            assert [stop["phase"] for stop in rig.user_stops()] == ["starting"]
            assert rig.manager._session_id == session_id  # the old session is not dropped
            assert not any("HANDMSG" in record for record in rig.records())  # (the handoff's record is the only one)
            assert rig.manager._stopped_notes[-1].startswith(WITHDREW_NOTE)  # the user's message was not sent
            assert rig.left() == []
            # The old session stays: its process too, when it obeyed the interrupt (else it was ended, and the next
            # turn starts it again, resuming the session).
            assert (rig.session.pid == process) is (runtime == "claude-obeys")
            # The next turn switches again: the handoff, then the answer.
            rig.manager._session_runtime = "omp"
            (rig.directory / "mode").unlink()
            await rig.answer("NEXT")
            assert rig.manager._session_runtime == "claude-code"
        finally:
            await rig.finish()

    _run(run)


# --- running ---


@pytest.mark.parametrize("runtime", STOPPABLE)
def test_a_reply_stopped_while_it_streams(
    runtime: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, agent_db: Any,
) -> None:
    async def run() -> None:
        rig = _case(runtime, tmp_path, monkeypatch, agent_db)
        await rig.start()
        try:
            await rig.answer("WARM")
            session_id = rig.manager._session_id
            rig.mode("streaming")
            envelope = await rig.send("STREAMING")
            await _until(rig.reached)
            await asyncio.sleep(0.3)
            shown = await rig.api_turn()
            assert shown is not None and shown["state"] == "running"
            runtime_cancel = rig.session._turn_cancel if runtime == "grok-acp" else rig.session._cancel
            assert runtime_cancel is rig.token(envelope).cancel
            assert await rig.manager.stop_turn(envelope.message_id, via="web") is StopResult.STOPPING
            assert runtime_cancel.requested
            replies = await rig.replied(rig.sent)
            assert replies[-1]["outcome"] == "stopped" and "error" not in replies[-1]
            (stop,) = rig.user_stops()
            assert stop["phase"] == "running" and stop["method"] == METHOD[runtime], stop
            assert rig.left() == []
            await _next_turn(rig, session_id, STOPPED_NOTE, notes=1)
        finally:
            await rig.finish()

    _run(run)


class Endpoint:
    """The agent endpoint over one write tool that records what runs: ``post`` calls it as a runtime's MCP bridge does
    (with the binding it read from the turn file)."""

    def __init__(self, rig: Rig) -> None:
        self.rig, self.ran, self.actions = rig, [], []
        self.turn_file = rig.root / ".local" / "manager-turn.json"

    async def __aenter__(self) -> Endpoint:
        from looper.agents.channels.web import WebChannelAdapter
        from looper.agents.http_server import AgentHTTPServer
        from tests.manager_mcp_stand_in import _free_port

        rig = self.rig

        async def write(call: Call) -> dict[str, Any]:
            self.ran.append(dict(call.arguments))
            return {"ok": True}

        async def record(**row: Any) -> int:
            self.actions.append(row)
            return len(self.actions)

        rig.monkeypatch.setattr("looper.agents.ops.service.db_actions.record", record)
        schema = {"type": "object", "properties": {"n": {"type": "integer"}}}
        port = _free_port()
        self.ops = ManagerOps(Platform(bus=rig.bus, task_router=None, manager=rig.manager),
                              url=f"http://127.0.0.1:{port}", tools=(Tool("t_write", "writes", schema, Risk.WRITE, write),),
                              turn_file=self.turn_file)
        self.ops._working_at = self.ops._clock()  # the MCP entry was probed
        rig.manager.ops = self.ops
        self.server = AgentHTTPServer(host="127.0.0.1", port=port, web_adapter=WebChannelAdapter(),
                                      task_router=SimpleNamespace(), supervisor=SimpleNamespace(bus=rig.bus),
                                      manager=rig.manager, runtime_root=rig.root, manager_ops=self.ops)
        await self.server.start()
        self.port = port
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.server.stop()

    def binding(self) -> str | None:
        return json.loads(self.turn_file.read_text())["turn"]

    async def post(self, binding: str | None, n: int = 1) -> dict[str, Any]:
        def request() -> dict[str, Any]:
            headers = {"Authorization": f"Bearer {self.ops._token}", "Content-Type": "application/json"}
            if binding:
                headers["X-Looper-Turn"] = binding
            call = urllib.request.Request(f"http://127.0.0.1:{self.port}/api/agent/v1/ops/t_write",
                                          json.dumps({"n": n}).encode(), headers, method="POST")
            with urllib.request.urlopen(call, timeout=10) as response:
                return json.loads(response.read())

        return await asyncio.to_thread(request)


@pytest.mark.parametrize("runtime", STOPPABLE)
def test_a_reply_stopped_in_a_tool_ends_only_an_agent_that_ignored_it_and_refuses_the_late_write(
    runtime: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, agent_db: Any,
) -> None:
    async def run() -> None:
        rig = _case(runtime, tmp_path, monkeypatch, agent_db)
        async with Endpoint(rig) as endpoint:
            await rig.start()
            try:
                await rig.answer("WARM")
                session_id = rig.manager._session_id
                rig.mode("tool")
                envelope = await rig.send("TOOL")
                if runtime == "grok-acp":
                    await _until(lambda: any(rig.root.glob(".local/grok/sleeper.pid")))
                else:
                    await _until(rig.reached)
                await asyncio.sleep(0.8)  # the tool's process is seen by the next refresh
                binding = endpoint.binding()
                assert binding is not None  # what the runtime's bridge reads for the call it is making
                shown = await rig.api_turn()
                assert shown is not None and shown["state"] == "running"
                assert await rig.manager.stop_turn(envelope.message_id, via="web") is StopResult.STOPPING
                late = await endpoint.post(binding)  # in flight when the user stopped
                assert late["error"]["code"] == "stopped", late
                replies = await rig.replied(rig.sent)
                assert replies[-1]["outcome"] == "stopped" and "error" not in replies[-1]
                assert endpoint.ran == []
                assert [(row["status"], row["error_code"]) for row in endpoint.actions] == [("error", "stopped")]
                (stop,) = rig.user_stops()
                assert stop["phase"] == "running" and stop["method"] == METHOD[runtime], stop
                if METHOD[runtime] == "kill":  # it ignored the interrupt: the agent and its tree are ended
                    assert rig.left() == []  # (the tool's process in a session of its own too)
                else:  # it ended the turn itself: nothing was signalled, the tool it left running is its own
                    assert rig.left(), rig.processes()
                assert endpoint.binding() is None
                again = await endpoint.post(binding, 2)  # later still: the turn is over
                assert again["error"]["code"] == "stopped" and endpoint.ran == []
                await _next_turn(rig, session_id, STOPPED_NOTE, notes=1)
            finally:
                await rig.finish()

    _run(run)


# --- a runtime without an interrupt ---


def test_a_grok_fallback_on_the_first_turn_runs_its_write_without_a_streamed_tool_use(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, agent_db: Any,
) -> None:
    from test_grok_runtime import _FAKE_GROK, _launch

    async def run() -> None:
        rig = _case("grok-acp", tmp_path, monkeypatch, agent_db)
        async with Endpoint(rig) as endpoint:
            # ACP initialize fails; the per-turn process calls before it streams even its init event.
            body = '''
import json, os, sys, urllib.request
if 'agent' in sys.argv[1:]:
    message = json.loads(sys.stdin.readline())
    print(json.dumps({'jsonrpc': '2.0', 'id': message['id'],
                      'error': {'code': -32601, 'message': 'initialize unavailable'}}), flush=True)
    sys.stdin.read()
else:
    binding = json.load(open(os.environ['TURN_FILE']))['turn']
    request = urllib.request.Request(os.environ['WRITE_URL'], json.dumps({'n': 1}).encode(),
        {'Authorization': 'Bearer ' + os.environ['OPS_TOKEN'], 'Content-Type': 'application/json',
         'X-Looper-Turn': binding}, method='POST')
    with urllib.request.urlopen(request, timeout=10) as response:
        open(os.environ['WRITE_RESULT'], 'w').write(response.read().decode())
    exec(compile(HEADLESS, 'headless', 'exec'))
'''.replace("HEADLESS", repr(_FAKE_GROK))
            result_file = tmp_path / "write-result.json"
            env = (("TURN_FILE", str(endpoint.turn_file)), ("OPS_TOKEN", endpoint.ops._token),
                   ("WRITE_URL", f"http://127.0.0.1:{endpoint.port}/api/agent/v1/ops/t_write"),
                   ("WRITE_RESULT", str(result_file)))
            rig.settings = _launch(executable=fake_claude(tmp_path / "fallback-grok", body), env=env).settings, ()
            await rig.start()
            try:
                await rig.answer()
                assert rig.session.transport == "per-turn"
                assert json.loads(result_file.read_text())["status"] == "ok"
                assert endpoint.ran == [{"n": 1}]
            finally:
                await rig.finish()

    _run(run)


def test_a_per_turn_runtime_refuses_the_stop_and_its_reply_runs_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, agent_db: Any,
) -> None:
    signalled: list[int] = []
    real_signal = proc_tree._signal

    def signal_(process: Any, tree: Any, sig: int) -> None:
        signalled.append(sig)
        real_signal(process, tree, sig)

    monkeypatch.setattr(proc_tree, "_signal", signal_)

    async def run() -> None:
        rig = _case(PER_TURN, tmp_path, monkeypatch, agent_db)
        await rig.start()
        try:
            await rig.answer("WARM")
            rig.mode("slow")
            envelope = await rig.send("SLOW")
            await _until(rig.reached)
            waiting = await rig.send("WAITING")
            shown = await rig.api_turn()
            assert shown is not None and shown["state"] == "running" and shown["can_interrupt"] is False
            running = rig.processes()[-1:]  # the slow turn's process (the warm turn's has exited)
            for message in (envelope, waiting):
                assert await rig.manager.stop_turn(message.message_id, via="web") is StopResult.CANNOT_INTERRUPT
                token = rig.token(message)
                assert token.stop is None and not token.cancel.requested
            await asyncio.sleep(0.3)
            assert signalled == [] and running and all(map(_alive, running)), (signalled, running)  # nothing signalled
            (rig.directory / "go").touch()
            replies = await rig.replied(rig.sent)
            assert [r["outcome"] for r in replies[-2:]] == ["answered", "answered"], replies
            assert rig.user_stops() == [] and rig.manager._stopped_notes == []
        finally:
            await rig.finish()

    _run(run)


# --- ended ---


def test_a_stop_after_the_turn_ended_changes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, agent_db: Any,
) -> None:
    async def run() -> None:
        rig = _case(SHARED, tmp_path, monkeypatch, agent_db)
        await rig.start()
        try:
            envelope = await rig.answer("DONE")
            await _until(lambda: rig.manager.turn_for(envelope.message_id) is None)
            session_id, replies, records = rig.manager._session_id, len(rig.replies), len(rig.records())
            assert await rig.manager.stop_turn(envelope.message_id, via="web") is StopResult.NOT_RUNNING
            assert await rig.api_turn() is None
            await asyncio.sleep(0.2)
            assert len(await rig.replied(replies)) == replies and len(rig.records()) == records
            assert rig.user_stops() == [] and rig.manager._stopped_notes == []
            assert rig.manager._session_id == session_id
            statuses = {call.kwargs["status"] for call in agent_db.finish_record.await_args_list}
            assert statuses == {"completed"}, statuses
            await rig.answer("NEXT")
        finally:
            await rig.finish()

    _run(run)
