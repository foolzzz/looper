"""The user's stop of a Manager turn, for every runtime path and every window it can land in.

Each runtime runs a stand-in process (real pipes, real process trees). Codex covers every window of the shared
``PerTurnSession`` path; OMP, pi and DSH retain their own streaming/tool windows, and Grok's fallback its
before-spawn/tool windows. Restore a runtime's full cells if it stops using that shared path.
A turn is stopped in one window:

- ``before_spawn``: while the turn prepares, before its process exists (per-turn: the command; Grok: the session is
  readied, ``initialize`` unanswered);
- ``spawning``: while the process is being spawned (the spawn is held);
- ``silent``: after the process started, before it wrote anything;
- ``streaming``: while it streams text;
- ``blocked``: while writing the message to it is blocked (it reads nothing: the pipe fills);
- ``tool``: while a tool call it started runs (a child process, in a session of its own: ``setsid``);
- ``done``: after it wrote the turn's result, before the turn is cleaned up (the process lingers);
- ``tail``: the process has exited and its stderr is read (per-turn runtimes, and the Grok fallback).

Every cell checks the same things: the stop is not lost (the turn ends stopped, never as a success); it ends within a
bound; a runtime that ended the turn on its interrupt (``CONTROL``, ``CANCEL``) had no process signalled, the tool the
turn started included, and one that did not (or a per-turn process) was ended with its group and tree; nothing of it
reaches the next turn; the next turn runs and resumes the same session. The Manager's own phases (received, preparing, starting, running, ended) over the same
stand-ins are in ``tests/test_manager_stop_matrix.py``; the table of cells is in ``chat-stop-v2.zh-CN.md`` 8.2.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
from pathlib import Path
from typing import Any

import pytest

from fake_claude import fake_claude
from looper.agents import claude_cli, proc_tree
from looper.agents.claude_cli import USER_STOP, ResidentClaude
from looper.agents.runtimes import StopMethod
from looper.agents.runtimes import grok_acp, per_turn
from looper.agents.runtimes.cancel import TurnCancel
from tests.stop_matrix_support import (
    BIG, BOUND, Seen, Stop, _answered, _alive, _CLAUDE, _per_turn, _pids, _stopped, _until,
)


PER_TURN = ["codex", "omp", "pi", "dsh"]
WINDOWS = ["before_spawn", "spawning", "silent", "streaming", "blocked", "tool", "done"]
# Codex, OMP, pi and DSH all run per turn through ``PerTurnSession._run_turn`` (spawn, write, read, tail, end the
# tree): Codex runs every window of that shared path. Each of the others runs the two windows that read its own
# stream, which is what differs: the text it streams, and the tool call it starts. A runtime that stops running
# through ``PerTurnSession`` gets its cells back.
PER_TURN_CELLS = [("codex", window) for window in WINDOWS] + [
    (runtime, window) for runtime in PER_TURN[1:] for window in ("streaming", "tool")]


@pytest.fixture
def sent_signals(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    sent: list[int] = []
    real_signal = proc_tree._signal

    def record(process: Any, tree: Any, sig: int) -> None:
        sent.append(sig)
        real_signal(process, tree, sig)

    monkeypatch.setattr(proc_tree, "_signal", record)
    return sent


class SlowSpawn:
    """``proc_tree.spawn`` held on its first call until ``release``: the turn is stopped while its process is spawned."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._monkeypatch, self._real = monkeypatch, proc_tree.spawn
        self.reached, self.release, self._held = asyncio.Event(), asyncio.Event(), False
        monkeypatch.setattr(proc_tree, "spawn", self)

    async def __call__(self, *argv: str, **kwargs: Any) -> Any:
        if not self._held:
            self._held = True
            self.reached.set()
            await self.release.wait()
        return await self._real(*argv, **kwargs)

    def restore(self) -> None:
        self.release.set()
        self._monkeypatch.setattr(proc_tree, "spawn", self._real)


@pytest.mark.parametrize("runtime,window", PER_TURN_CELLS)
def test_per_turn_stop_in_every_window(
    tmp_path: Path, runtime: str, window: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(proc_tree, "TERM_GRACE_SEC", 0.2)
    session, command, env, directory = _per_turn(tmp_path, runtime)

    async def run() -> tuple[Any, ...]:
        await session.start(command, cwd=str(tmp_path), signature="s", env=env)
        (directory / "mode").write_text("answer")
        first = await session.run_turn("hello", Seen())
        assert _answered(first) and first.session_started, first.result_event
        sid = json.loads((directory / "turns.jsonl").read_text().splitlines()[-1])["sid"]
        (directory / "pids").unlink()

        before_usage = session.totals
        (directory / "mode").write_text("blocked" if window == "before_spawn" else
                                        "answer" if window == "spawning" else window)
        seen, stop = Seen(), Stop()
        via_interrupt = runtime == "codex" and window == "tool"  # one cell stops through ``interrupt()``
        if not via_interrupt:
            seen.stop = stop  # (the interrupt is a task of its own: what the turn reads before it runs is not late)
        ended: list[bool] = []
        if via_interrupt:
            real_end = proc_tree.TurnTree.end

            async def slow_end(tree: Any) -> Any:  # the tree's cleanup takes a while: the turn waits for it
                await asyncio.sleep(0.5)
                try:
                    return await real_end(tree)
                finally:
                    ended.append(True)

            monkeypatch.setattr(proc_tree.TurnTree, "end", slow_end)
        release = asyncio.Event()
        slow = SlowSpawn(monkeypatch) if window == "spawning" else None
        if window == "before_spawn":
            loop, real_argv = asyncio.get_running_loop(), session._turn_argv
            reached = asyncio.Event()

            def slow_argv() -> list[str]:
                loop.call_soon_threadsafe(reached.set)
                asyncio.run_coroutine_threadsafe(release.wait(), loop).result()
                return real_argv()

            monkeypatch.setattr(session, "_turn_argv", slow_argv)
        cancel = TurnCancel()
        running = asyncio.create_task(
            session.run_turn(BIG if window == "blocked" else "STOPPED_TURN_PAYLOAD", seen, stop=stop, cancel=cancel))
        try:
            if window == "before_spawn":
                await asyncio.wait_for(reached.wait(), BOUND)
            elif slow is not None:
                await asyncio.wait_for(slow.reached.wait(), BOUND)
            else:
                await _until(lambda: (directory / "reached").exists())
                if window == "blocked":
                    await _until(lambda: session._process is not None
                                 and session._process.stdin.transport.get_write_buffer_size() > 0)
                else:  # what it wrote reaches the turn; the tool's process is seen by the next refresh
                    await asyncio.sleep(0.8 if window == "tool" else 0.3)
            if window == "tool":
                await _until(lambda: len(_pids(directory)) == 3)
            pids = _pids(directory)
            if window == "tool":
                assert len(pids) == 3 and all(_alive(pid) for pid in pids)
            stop.asked = True
            if via_interrupt:
                method = await asyncio.wait_for(session.interrupt(), BOUND)
            else:
                cancel.request()
                method = StopMethod.KILL
            release.set()
            if slow is not None:
                slow.release.set()
            turn = await asyncio.wait_for(running, BOUND)
            if via_interrupt:
                assert ended == [True]  # a stopped turn returns only after its tree's cleanup
            if slow is not None:
                pids = _pids(directory)  # the process the held spawn went on to start
        finally:
            release.set()
            if slow is not None:
                slow.restore()
            running.cancel()
            await asyncio.gather(running, return_exceptions=True)
        if window == "tool":
            assert turn.session_started
            if runtime in ("omp", "pi"):
                assert session.totals.input_tokens == before_usage.input_tokens + turn.usage.input_tokens
                assert session.totals.cache_read_tokens == before_usage.cache_read_tokens + turn.usage.cache_read_tokens
        seen.closed = True
        monkeypatch.undo()
        alive = [pid for pid in pids if _alive(pid)]
        stale = session.stale("s")

        (directory / "mode").write_text("answer")
        nxt = Seen()
        after = await asyncio.wait_for(session.run_turn("next", nxt), BOUND)
        record = json.loads((directory / "turns.jsonl").read_text().splitlines()[-1])
        resumed = record["sid"]
        flag = {"codex": "resume", "omp": "--resume", "pi": "--session", "dsh": "--session-id"}[runtime]
        args = record["args"]
        assert flag in args
        value = args[args.index(flag) + 1]
        if runtime == "pi":
            assert json.loads(Path(value).read_text().splitlines()[0])["id"] == sid
        else:
            assert value == sid
        await session.stop()
        return method, turn, seen, alive, stale, after, nxt, sid, resumed

    method, turn, seen, alive, stale, after, nxt, sid, resumed = asyncio.run(asyncio.wait_for(run(), 60))
    assert method is StopMethod.KILL
    assert _stopped(turn) and turn.result_event[USER_STOP], turn.result_event
    assert not seen.late and not seen.after_stop
    assert window == "done" or not any(e.get("type") == "result" and not e.get("is_error") for e in seen.events)
    assert alive == []  # (the tool's process in a session of its own too)
    if window == "tool":  # what the stopped turn spent: counted as far as the runtime reported it, else unknown
        if runtime == "codex":
            assert turn.usage_complete is False and not turn.usage.input_tokens
        elif runtime in ("omp", "pi"):
            assert turn.usage_complete
            assert (turn.usage.input_tokens, turn.usage.output_tokens,
                    turn.usage.cache_read_tokens, turn.usage.cache_creation_tokens) == (10, 1, 4, 2)
    assert not stale
    assert _answered(after), after.result_event
    assert "STOPPED_TURN_PAYLOAD" not in json.dumps(nxt.events) and "half an answer" not in json.dumps(nxt.events)
    assert resumed == sid


def test_per_turn_stop_after_the_process_exited_before_the_turn_returned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The last step of a turn whose process has exited (its stderr read): a stop that lands there is not lost.
    (The step is ``PerTurnSession``'s: Codex stands for every per-turn runtime.)"""
    session, command, env, directory = _per_turn(tmp_path, "codex")
    real_tail = per_turn.stderr_tail
    holding, release = asyncio.Event(), asyncio.Event()

    async def held_tail(stream: Any, *args: Any) -> str:
        text = await real_tail(stream, *args)
        holding.set()
        await release.wait()
        return text

    async def run() -> tuple[Any, ...]:
        await session.start(command, cwd=str(tmp_path), signature="s", env=env)
        assert await session.interrupt() is None  # no turn runs: nothing to stop
        (directory / "mode").write_text("answer")
        await session.run_turn("hello", Seen())
        monkeypatch.setattr(per_turn, "stderr_tail", held_tail)
        running = asyncio.create_task(session.run_turn("work", Seen()))
        try:
            await asyncio.wait_for(holding.wait(), BOUND)
            stopping = asyncio.create_task(session.interrupt())  # (the cancel of a turn run without one)
            await asyncio.sleep(0.1)
            release.set()
            method, turn = await asyncio.wait_for(stopping, BOUND), await asyncio.wait_for(running, BOUND)
        finally:
            release.set()
            running.cancel()
            await asyncio.gather(running, return_exceptions=True)
        monkeypatch.setattr(per_turn, "stderr_tail", real_tail)
        after = await asyncio.wait_for(session.run_turn("next", Seen()), BOUND)
        await session.stop()
        return method, turn, after

    method, turn, after = asyncio.run(asyncio.wait_for(run(), 60))
    assert method is StopMethod.KILL
    assert _stopped(turn) and turn.result_event[USER_STOP], turn.result_event
    assert _answered(after), after.result_event


# --- Claude Code: one resident stream-json process (``claude_cli.ResidentClaude``) ---


CLAUDE_WINDOWS = ["silent", "streaming", "blocked", "tool", "done"]


@pytest.mark.parametrize("obey", [False, True], ids=["deaf", "obeys"])
@pytest.mark.parametrize("window", CLAUDE_WINDOWS)
def test_resident_claude_stop_in_every_window(
    tmp_path: Path, window: str, obey: bool, monkeypatch: pytest.MonkeyPatch, sent_signals: list[int],
) -> None:
    """Claude's process is spawned by ``start``, before the turn: a stop before it is the Manager's (its window
    ``before_spawn`` is ``test_manager_stop_matrix.test_a_message_stopped_while_its_runtime_starts_is_not_sent``)."""
    monkeypatch.setattr(claude_cli, "INTERRUPT_GRACE_SEC", 0.5)
    monkeypatch.setattr(claude_cli, "STOP_GRACE_SEC", 0.5)
    monkeypatch.setattr(proc_tree, "TERM_GRACE_SEC", 0.2)
    directory = tmp_path / "matrix"
    directory.mkdir()
    env = {**os.environ, "STOP_MATRIX_DIR": str(directory)}
    claude = fake_claude(tmp_path / "claude", _CLAUDE)
    ended: list[bool] = []
    if window == "tool" and not obey:
        real_end = proc_tree.TurnTree.end

        async def slow_end(tree: Any) -> Any:  # the tree's cleanup takes a while: the killed turn waits for it
            await asyncio.sleep(0.5)
            try:
                return await real_end(tree)
            finally:
                ended.append(True)

        monkeypatch.setattr(proc_tree.TurnTree, "end", slow_end)

    async def run() -> tuple[Any, ...]:
        session = ResidentClaude(label="manager")
        await session.start([claude], cwd=str(tmp_path), signature="s", env=env)
        if window == "blocked":
            (directory / "stall").touch()  # after the first message, it reads no more
        first = await session.run_turn("hello", Seen())
        assert _answered(first), first.result_event
        process = session.pid
        seen, stop, cancel = Seen(hold_at="result" if window == "done" else None), Stop(), TurnCancel()
        text = BIG if window == "blocked" else f"mode:{window}{' obey' if obey else ''}"
        running = asyncio.create_task(session.run_turn(text, seen, stop=stop, cancel=cancel))
        via_interrupt = window == "tool" and obey  # the one cell stopped through ``interrupt()``
        if not via_interrupt:
            seen.stop = stop
        try:
            if window == "done":
                await asyncio.wait_for(seen.holding.wait(), BOUND)
                pids = [int(p) for p in (directory / "pids").read_text().split()]
                stop.asked = True
                cancel.request()
                await asyncio.sleep(0.2)
                seen.release.set()
                turn = await asyncio.wait_for(running, BOUND)
                method = turn.stopped
            else:
                await _until(lambda: (directory / "reached").exists())
                if window == "blocked":
                    await _until(lambda: session._process is not None
                                 and session._process.stdin.transport.get_write_buffer_size() > 0)
                else:
                    await asyncio.sleep(0.2)
                pids = [int(p) for p in (directory / "pids").read_text().split()]
                stop.asked = True
                if via_interrupt:
                    method = await asyncio.wait_for(session.interrupt(), BOUND)
                else:
                    cancel.request()
                turn = await asyncio.wait_for(running, BOUND)
                method = turn.stopped
            if window == "tool" and not obey:
                assert ended == [True]  # a killed turn returns only after its tree's cleanup
        finally:
            seen.release.set()
            running.cancel()
            await asyncio.gather(running, return_exceptions=True)
        if method is StopMethod.CONTROL:
            assert sent_signals == []
        seen.closed = True
        kept = session.started and session.pid == process
        alive = [pid for pid in pids if _alive(pid)]
        (directory / "stall").unlink(missing_ok=True)
        if not session.started or session.stale("s"):
            await session.stop()
            await session.start([claude], cwd=str(tmp_path), signature="s", env=env)  # resumes the session
        nxt = Seen()
        after = await asyncio.wait_for(session.run_turn("mode:answer", nxt), BOUND)
        await session.stop(kill=True)
        return method, turn, seen, kept, pids, alive, after, nxt, process

    method, turn, seen, kept, pids, alive, after, nxt, process = asyncio.run(asyncio.wait_for(run(), 60))
    if window == "done" or (obey and window != "blocked"):
        assert method is StopMethod.CONTROL and kept  # the process and its session stay for the next turn
        assert process in alive and alive == pids  # nothing was signalled: a tool the turn started is Claude's
        if window != "done":  # Claude's interrupted result does not carry what the aborted request spent (2.1.x)
            assert turn.usage_complete is False
    else:
        assert method is StopMethod.KILL and alive == []
    assert _stopped(turn) and turn.stopped is method, turn.result_event
    assert not seen.late and not seen.after_stop
    assert _answered(after) and after.result_event["result"] == "answered answer", after.result_event
    assert "half an answer" not in json.dumps(nxt.events)


# --- Grok over ACP (``grok_acp.AcpGrok``) ---

GROK_WINDOWS = ["before_spawn", "spawning", "silent", "streaming", "blocked", "tool", "done"]


@pytest.mark.parametrize("window", GROK_WINDOWS)
def test_grok_acp_stop_in_every_window(
    tmp_path: Path, window: str, monkeypatch: pytest.MonkeyPatch, sent_signals: list[int],
) -> None:
    from test_grok_acp import _command, _fake
    from test_grok_runtime import GROK, _launch, _operator

    _operator(tmp_path, monkeypatch)
    monkeypatch.setattr(grok_acp, "CANCEL_GRACE_SEC", 0.5)
    monkeypatch.setattr(proc_tree, "TERM_GRACE_SEC", 0.2)
    root, grok = tmp_path / "runtime", _fake(tmp_path)
    stall = root / ".local" / "grok" / "stall"
    sleeper_file = root / ".local" / "grok" / "sleeper.pid"  # the tool's process, in a session of its own

    async def run() -> tuple[Any, ...]:
        session = grok_acp.AcpGrok(label="manager")
        env = _launch().environment()
        await session.start(_command(root, grok), cwd=str(tmp_path), signature="s", env=env)
        first = await session.run_turn("hello", Seen())
        assert _answered(first), first.result_event
        sid = session._session
        if window in ("before_spawn", "spawning", "blocked"):
            # A new process loads the session for the stopped turn: deaf to ``initialize``, or to the prompt.
            await session._end_process()
            if window == "before_spawn":
                session._env = {**env, "FAKE_ACP_SILENT": "1"}
            else:
                stall.touch()
        sent_signals.clear()  # the previous process may have been ended to hold initialization
        seen, stop, cancel = Seen(hold_at="result" if window == "done" else None), Stop(), TurnCancel()
        text = {"before_spawn": "work", "spawning": "work", "silent": "QUIET", "streaming": "HANG", "blocked": BIG,
                "tool": "SLEEPER", "done": "work"}[window]
        slow = SlowSpawn(monkeypatch) if window == "spawning" else None
        tool_pids: list[int] = []
        running = asyncio.create_task(session.run_turn(text, seen, stop=stop, cancel=cancel))
        try:
            if window == "done":
                await asyncio.wait_for(seen.holding.wait(), BOUND)
                pid = session.pid
                stop.asked = True
                cancel.request()
                await asyncio.sleep(0.2)
                seen.release.set()
                turn = await asyncio.wait_for(running, BOUND)
                method = turn.stopped
            else:
                if window == "before_spawn":
                    await _until(lambda: session._process is not None)
                    await asyncio.sleep(0.3)  # ``initialize`` is waiting
                elif slow is not None:
                    await asyncio.wait_for(slow.reached.wait(), BOUND)
                elif window == "blocked":
                    await _until(lambda: session._process is not None and session._prompt_id is not None
                                 and session._process.stdin.transport.get_write_buffer_size() > 0)
                elif window == "tool":
                    await _until(sleeper_file.exists)
                    await _until(lambda: any(e.get("type") == "assistant" for e in seen.events))
                    await asyncio.sleep(0.3)
                else:
                    await _until(lambda: session._prompt_id is not None and bool(seen.events))
                    await asyncio.sleep(0.3)
                pid = session.pid
                tool_pids = [int(sleeper_file.read_text())] if window == "tool" else []
                stop.asked = True
                cancel.request()
                if slow is not None:
                    slow.release.set()
                turn = await asyncio.wait_for(running, BOUND)
                method = turn.stopped
        finally:
            if slow is not None:
                slow.restore()
            seen.release.set()
            running.cancel()
            await asyncio.gather(running, return_exceptions=True)
        if method is StopMethod.CANCEL:
            assert sent_signals == []
            assert session.pid == pid and pid is not None and _alive(pid)
            assert turn.prompt_sent and turn.result_event["stop_reason"] == "cancelled"
            assert seen.events[-1]["type"] == "result"
        seen.closed = True
        session._env = env
        stall.unlink(missing_ok=True)
        gone = pid is None or not _alive(pid) or session.pid == pid
        tools = [p for p in tool_pids if _alive(p)]  # (a tool's process in a session of its own)
        transport = session.transport
        if session.stale("s"):  # the owner starts over, resuming the saved session
            await session.stop()
            await session.start(_command(root, grok) + GROK.resume_args(sid), cwd=str(tmp_path), signature="s",
                                env=env)
        nxt = Seen()
        after = await asyncio.wait_for(session.run_turn("next", nxt), BOUND)
        if method is StopMethod.CANCEL:
            assert session.pid == pid  # the following turn uses the same resident process
        resumed = session._session
        await session.stop()
        for tool in tools:
            with contextlib.suppress(ProcessLookupError):
                os.kill(tool, signal.SIGKILL)
        return method, turn, seen, gone, tools, transport, after, nxt, sid, resumed

    method, turn, seen, gone, tools, transport, after, nxt, sid, resumed = asyncio.run(asyncio.wait_for(run(), 60))
    assert method is (StopMethod.KILL if window in ("before_spawn", "spawning", "blocked") else StopMethod.CANCEL)
    if window == "tool":
        assert tools  # Grok ended the turn on its cancel: nothing was signalled, its tool is its own
    assert _stopped(turn) and turn.stopped is method, turn.result_event
    assert turn.result_event.get("is_error"), turn.result_event  # never a success
    assert not seen.late and gone
    assert transport == "acp"  # a stop never sends the session per turn
    assert _answered(after) and "| next |" in after.result_event["result"], after.result_event
    assert resumed == sid
    assert "thinking" not in json.dumps(nxt.events)


# ACP unavailable: the session runs per turn (``grok.ResidentGrok``), stopped by the same cancel. Its turns are
# ``PerTurnSession``'s, whose windows are run under Codex above: what is Grok's is the fall back itself (a stop
# asked while the session falls back) and the tool it starts.

GROK_FALLBACK_WINDOWS = ["before_spawn", "tool"]


@pytest.mark.parametrize("window", GROK_FALLBACK_WINDOWS)
def test_grok_fallback_stop_in_every_window(tmp_path: Path, window: str, monkeypatch: pytest.MonkeyPatch) -> None:
    from test_grok_acp import _command, _fake_with_headless
    from test_grok_runtime import _launch, _operator

    _operator(tmp_path, monkeypatch)
    monkeypatch.setattr(proc_tree, "TERM_GRACE_SEC", 0.2)
    root, grok = tmp_path / "runtime", _fake_with_headless(tmp_path)
    sleeper_file = root / ".local" / "grok" / "sleeper.pid"
    cancel = TurnCancel()
    spawned: list[str] = []
    if window == "before_spawn":  # asked while the session falls back, before the per-turn process exists
        fall_back = grok_acp.AcpGrok._fall_back

        async def requesting(self: Any, reason: str) -> None:
            cancel.request()
            await fall_back(self, reason)
            spawned.clear()  # (the ACP process of the readying is behind us)

        monkeypatch.setattr(grok_acp.AcpGrok, "_fall_back", requesting)
    spawn = proc_tree.spawn

    async def counting_spawn(*argv: str, **kwargs: Any) -> Any:
        spawned.append(argv[0])
        return await spawn(*argv, **kwargs)

    monkeypatch.setattr(proc_tree, "spawn", counting_spawn)

    async def run() -> tuple[Any, ...]:
        session = grok_acp.AcpGrok(label="manager")
        env = {**_launch().environment(), "FAKE_ACP_DEAD": "1"}
        await session.start(_command(root, grok), cwd=str(tmp_path), signature="s", env=env)
        seen = Seen()
        if window == "before_spawn":
            turn = await asyncio.wait_for(session.run_turn("work", seen, cancel=cancel), BOUND)
            started = list(spawned)
            pids: list[int] = []
        else:
            first = await session.run_turn("hello", Seen())  # falls back
            assert _answered(first), first.result_event
            assert session.transport == "per-turn"
            running = asyncio.create_task(session.run_turn("SLEEPER", seen, cancel=cancel))
            try:
                await _until(sleeper_file.exists)
                await _until(lambda: bool(seen.events))
                await asyncio.sleep(0.2)
                pids = [int(sleeper_file.read_text())]
                cancel.request()
                turn = await asyncio.wait_for(running, BOUND)
            finally:
                running.cancel()
                await asyncio.gather(running, return_exceptions=True)
            started = []
        seen.closed = True
        alive = [pid for pid in pids if _alive(pid)]
        nxt = Seen()
        after = await asyncio.wait_for(session.run_turn("next", nxt), BOUND)
        transport = session.transport
        await session.stop()
        return turn, seen, alive, transport, started, after, nxt

    turn, seen, alive, transport, started, after, nxt = asyncio.run(asyncio.wait_for(run(), 60))
    assert _stopped(turn) and turn.stopped is StopMethod.KILL, turn.result_event
    assert turn.result_event.get("is_error") and not seen.late
    assert alive == [] and transport == "per-turn"
    assert _answered(after) and "| next |" in after.result_event["result"], after.result_event
    assert "half an answer" not in json.dumps(nxt.events)
    if window == "before_spawn":
        assert started == [] and not any(e.get("type") == "assistant" for e in seen.events)

