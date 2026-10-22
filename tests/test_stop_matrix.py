"""The user's stop of a Manager turn, for every runtime path and every window it can land in.

Each runtime runs a stand-in process (real pipes, real process trees). A turn is stopped in one window:

- ``before_spawn``: while the turn prepares, before its process exists (per-turn: the command; Grok: the session is
  readied, ``initialize`` unanswered);
- ``spawning``: while the process is being spawned (the spawn is held);
- ``silent``: after the process started, before it wrote anything;
- ``streaming``: while it streams text;
- ``blocked``: while writing the message to it is blocked (it reads nothing: the pipe fills);
- ``tool``: while a tool call it started runs (a child process, in a session of its own: ``setsid``);
- ``detached`` (Claude): the tool's shell started a ``setsid`` sleeper and exited before any refresh saw either;
- ``done``: after it wrote the turn's result, before the turn is cleaned up (the process lingers);
- ``tail``: the process has exited and its stderr is read (per-turn runtimes, and the Grok fallback).

Every cell checks the same things: the stop is not lost (the turn ends stopped, never as a success); it ends within a
bound with every process the stopped turn started gone; nothing of it reaches the next turn; the next turn runs and
resumes the same session. The Manager's own phases (received, preparing, starting, running, ended) over the same
stand-ins are in ``tests/test_manager_stop_matrix.py``; the table of cells is in ``chat-stop-v2.zh-CN.md`` 8.2.
"""

from __future__ import annotations

import asyncio
import json
import os
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


@pytest.mark.parametrize("window", WINDOWS)
@pytest.mark.parametrize("runtime", PER_TURN)
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

        (directory / "mode").write_text("blocked" if window == "before_spawn" else
                                        "answer" if window == "spawning" else window)
        seen, stop = Seen(), Stop()
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
            session.run_turn(BIG if window == "blocked" else "work", seen, stop=stop, cancel=cancel))
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
            pids = _pids(directory)
            stop.asked = True
            if runtime == "codex" and window == "tool":  # one cell stops through ``interrupt()``
                method = await asyncio.wait_for(session.interrupt(), BOUND)
            else:
                cancel.request()
                method = StopMethod.KILL
            release.set()
            if slow is not None:
                slow.release.set()
            turn = await asyncio.wait_for(running, BOUND)
            if slow is not None:
                pids = _pids(directory)  # the process the held spawn went on to start
        finally:
            release.set()
            if slow is not None:
                slow.restore()
            running.cancel()
            await asyncio.gather(running, return_exceptions=True)
        seen.closed = True
        monkeypatch.undo()
        alive = [pid for pid in pids if _alive(pid)]
        stale = session.stale("s")

        (directory / "mode").write_text("answer")
        nxt = Seen()
        after = await asyncio.wait_for(session.run_turn("next", nxt), BOUND)
        resumed = json.loads((directory / "turns.jsonl").read_text().splitlines()[-1])["sid"]
        await session.stop()
        return method, turn, seen, alive, stale, after, nxt, sid, resumed

    method, turn, seen, alive, stale, after, nxt, sid, resumed = asyncio.run(asyncio.wait_for(run(), 60))
    assert method is StopMethod.KILL
    assert _stopped(turn) and turn.result_event[USER_STOP], turn.result_event
    assert not seen.late
    assert window == "done" or not any(e.get("type") == "result" and not e.get("is_error") for e in seen.events)
    assert alive == []  # (the tool's process in a session of its own too)
    assert not stale
    assert _answered(after), after.result_event
    assert "work" not in json.dumps(nxt.events) and "half an answer" not in json.dumps(nxt.events)
    assert resumed == sid


@pytest.mark.parametrize("runtime", PER_TURN)
def test_per_turn_stop_after_the_process_exited_before_the_turn_returned(
    tmp_path: Path, runtime: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The last step of a turn whose process has exited (its stderr read): a stop that lands there is not lost."""
    from looper.agents.runtimes import per_turn

    session, command, env, directory = _per_turn(tmp_path, runtime)
    real_tail = per_turn.stderr_tail
    holding, release = asyncio.Event(), asyncio.Event()

    async def held_tail(stream: Any, *args: Any) -> str:
        text = await real_tail(stream, *args)
        holding.set()
        await release.wait()
        return text

    async def run() -> tuple[Any, ...]:
        await session.start(command, cwd=str(tmp_path), signature="s", env=env)
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


CLAUDE_WINDOWS = ["silent", "streaming", "blocked", "tool", "detached", "done"]


@pytest.mark.parametrize("obey", [False, True], ids=["deaf", "obeys"])
@pytest.mark.parametrize("window", CLAUDE_WINDOWS)
def test_resident_claude_stop_in_every_window(
    tmp_path: Path, window: str, obey: bool, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Claude's process is spawned by ``start``, before the turn: a stop before it is the Manager's (its window
    ``before_spawn`` is ``test_manager_stop.test_stop_before_the_process_starts``)."""
    monkeypatch.setattr(claude_cli, "INTERRUPT_GRACE_SEC", 0.5)
    monkeypatch.setattr(claude_cli, "STOP_GRACE_SEC", 0.5)
    monkeypatch.setattr(proc_tree, "TERM_GRACE_SEC", 0.2)
    directory = tmp_path / "matrix"
    directory.mkdir()
    env = {**os.environ, "STOP_MATRIX_DIR": str(directory)}
    claude = fake_claude(tmp_path / "claude", _CLAUDE)

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
        finally:
            seen.release.set()
            running.cancel()
            await asyncio.gather(running, return_exceptions=True)
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
        return method, turn, seen, kept, alive, after, nxt, process

    method, turn, seen, kept, alive, after, nxt, process = asyncio.run(asyncio.wait_for(run(), 60))
    if window == "done" or (obey and window != "blocked"):
        assert method is StopMethod.CONTROL and kept  # the process and its session stay for the next turn
        assert alive == [process]  # a tool the turn started is gone
    else:
        assert method is StopMethod.KILL and alive == []
    assert _stopped(turn) and turn.stopped is method, turn.result_event
    assert not seen.late
    assert _answered(after) and after.result_event["result"] == "answered answer", after.result_event
    assert "half an answer" not in json.dumps(nxt.events)


# --- Grok over ACP (``grok_acp.AcpGrok``) ---

GROK_WINDOWS = ["before_spawn", "spawning", "silent", "streaming", "blocked", "tool", "done"]


@pytest.mark.parametrize("window", GROK_WINDOWS)
def test_grok_acp_stop_in_every_window(tmp_path: Path, window: str, monkeypatch: pytest.MonkeyPatch) -> None:
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
        seen.closed = True
        session._env = env
        stall.unlink(missing_ok=True)
        gone = pid is None or not _alive(pid) or session.pid == pid
        gone = gone and not any(_alive(p) for p in tool_pids)  # (a tool's process in a session of its own too)
        transport = session.transport
        if session.stale("s"):  # the owner starts over, resuming the saved session
            await session.stop()
            await session.start(_command(root, grok) + GROK.resume_args(sid), cwd=str(tmp_path), signature="s",
                                env=env)
        nxt = Seen()
        after = await asyncio.wait_for(session.run_turn("next", nxt), BOUND)
        resumed = session._session
        await session.stop()
        return method, turn, seen, gone, transport, after, nxt, sid, resumed

    method, turn, seen, gone, transport, after, nxt, sid, resumed = asyncio.run(asyncio.wait_for(run(), 60))
    assert method is (StopMethod.KILL if window in ("before_spawn", "spawning", "blocked") else StopMethod.CANCEL)
    assert _stopped(turn) and turn.stopped is method, turn.result_event
    assert turn.result_event.get("is_error"), turn.result_event  # never a success
    assert not seen.late and gone
    assert transport == "acp"  # a stop never sends the session per turn
    assert _answered(after) and "| next |" in after.result_event["result"], after.result_event
    assert resumed == sid
    assert "thinking" not in json.dumps(nxt.events)


# The same windows with ACP unavailable: the session runs per turn (``grok.ResidentGrok``), stopped by the same cancel.

GROK_FALLBACK_WINDOWS = ["before_spawn", "spawning", "silent", "streaming", "tool", "tail"]


@pytest.mark.parametrize("window", GROK_FALLBACK_WINDOWS)
def test_grok_fallback_stop_in_every_window(tmp_path: Path, window: str, monkeypatch: pytest.MonkeyPatch) -> None:
    from test_grok_acp import _command, _fake_with_headless
    from test_grok_runtime import GROK, _launch, _operator

    _operator(tmp_path, monkeypatch)
    monkeypatch.setattr(proc_tree, "TERM_GRACE_SEC", 0.2)
    root, grok = tmp_path / "runtime", _fake_with_headless(tmp_path)
    sleeper_file = root / ".local" / "grok" / "sleeper.pid"
    cancel = TurnCancel()
    spawned: list[str] = []
    holding, release = asyncio.Event(), asyncio.Event()
    real_tail = per_turn.stderr_tail

    async def held_tail(stream: Any, *args: Any) -> str:  # the process has exited; its stderr was read
        text = await real_tail(stream, *args)
        holding.set()
        await release.wait()
        return text

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
        # In the tail window the turn's first event is held until the stop lands: the process has exited and its stderr
        # is read, yet the turn has not consumed its output, so the result in the pipe is what the stop must drop. Without
        # the hold, whether the result was consumed before the stop depended on the runner's speed.
        seen = Seen("system" if window == "tail" else None)
        if window == "before_spawn":
            turn = await asyncio.wait_for(session.run_turn("work", seen, cancel=cancel), BOUND)
            started = list(spawned)
            pids: list[int] = []
        else:
            first = await session.run_turn("hello", Seen())  # falls back
            assert _answered(first), first.result_event
            assert session.transport == "per-turn"
            text = {"spawning": "work", "silent": "HANG", "streaming": "STREAM", "tool": "SLEEPER",
                    "tail": "work"}[window]
            slow = SlowSpawn(monkeypatch) if window == "spawning" else None
            if window == "tail":
                monkeypatch.setattr(per_turn, "stderr_tail", held_tail)
            running = asyncio.create_task(session.run_turn(text, seen, cancel=cancel))
            try:
                if slow is not None:
                    await asyncio.wait_for(slow.reached.wait(), BOUND)
                elif window == "tail":
                    await asyncio.wait_for(holding.wait(), BOUND)  # the process has exited
                    await asyncio.wait_for(seen.holding.wait(), BOUND)  # and the turn has read nothing past its first event
                else:
                    if window == "tool":
                        await _until(sleeper_file.exists)
                    await _until(lambda: bool(seen.events))
                    await asyncio.sleep(0.2)
                pids = [int(sleeper_file.read_text())] if window == "tool" else []
                cancel.request()
                if slow is not None:
                    slow.release.set()
                release.set()
                seen.release.set()
                turn = await asyncio.wait_for(running, BOUND)
            finally:
                release.set()
                seen.release.set()
                if slow is not None:
                    slow.restore()
                running.cancel()
                await asyncio.gather(running, return_exceptions=True)
            started = []
        seen.closed = True
        alive = [pid for pid in pids if _alive(pid)]
        monkeypatch.setattr(per_turn, "stderr_tail", real_tail)
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
    if window == "tail":
        assert not any(e.get("type") == "result" and not e.get("is_error") for e in seen.events)



def test_grok_fallback_end_turn_processes_ends_the_per_turn_sessions_tree(monkeypatch: pytest.MonkeyPatch) -> None:
    # A stop while the events are handled, after run_turn has returned an answer: the Manager has the turn's processes
    # ended. A session that fell back keeps the turn's tree on its per-turn session (it has none of its own).
    session = grok_acp.AcpGrok(label="manager")
    session._fallback = True
    ended: list[bool] = []

    async def end_turn_processes() -> None:
        ended.append(True)

    monkeypatch.setattr(session._legacy, "end_turn_processes", end_turn_processes)
    asyncio.run(session.end_turn_processes())
    assert ended == [True]
