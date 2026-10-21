"""The user's stop of a Manager turn, for every runtime path and every window it can land in.

Each runtime runs a stand-in process (real pipes, real process trees). A turn is stopped in one window:

- ``before_spawn``: while the turn prepares, before its process exists (per-turn: the command; Grok: the session is
  readied, ``initialize`` unanswered);
- ``silent``: after the process started, before it wrote anything;
- ``streaming``: while it streams text;
- ``blocked``: while writing the message to it is blocked (it reads nothing: the pipe fills);
- ``tool``: while a tool call it started runs (a child process);
- ``done``: after it wrote the turn's result, before the turn is cleaned up (the process lingers).

Every cell checks the same things: the stop is not lost (the turn ends stopped, never as a success); it ends within a
bound with every process the stopped turn started gone; nothing of it reaches the next turn; the next turn runs and
resumes the same session. The Manager's own windows (before ``run_turn``, after it returns, a confirmation asked as
the stop lands) are in ``tests/test_manager_stop.py``; the table of cells and tests is in the chat composer RFC.
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
from looper.agents.claude_cli import USER_STOP, CapStop, ResidentClaude, user_stop_result
from looper.agents.run_usage import Usage
from looper.agents.runtimes import StopMethod
from looper.agents.runtimes import grok_acp
from looper.agents.runtimes.cancel import TurnCancel

BOUND = 8.0  # every stopped turn ends within this (the graces are cut to fractions of a second)
BIG = "x" * (4 << 20)  # a message no pipe takes whole: its write blocks while the process reads nothing


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return pid in proc_tree._snapshot()


async def _until(check: Any, timeout: float = BOUND) -> None:
    async def poll() -> None:
        while not check():
            await asyncio.sleep(0.02)

    await asyncio.wait_for(poll(), timeout)


class Seen:
    """``on_event`` of one turn: what it got, and what came after its turn returned (``late``). With ``hold_at``,
    it holds the first event of that type until ``release`` is set."""

    def __init__(self, hold_at: str | None = None) -> None:
        self.events: list[dict[str, Any]] = []
        self.closed = False
        self.late: list[dict[str, Any]] = []
        self.hold_at = hold_at
        self.holding, self.release = asyncio.Event(), asyncio.Event()

    async def __call__(self, event: dict[str, Any]) -> None:
        (self.late if self.closed else self.events).append(event)
        if self.hold_at is not None and event.get("type") == self.hold_at and not self.holding.is_set():
            self.holding.set()
            await self.release.wait()


class Stop:
    """The Manager's ``stop=`` hook: once the user asked, the turn's result is ``user_stop_result``."""

    def __init__(self) -> None:
        self.asked = False

    def __call__(self) -> CapStop | None:
        return CapStop(user_stop_result(), Usage()) if self.asked else None


def _stopped(turn: Any) -> bool:
    """The turn ended as the user's stop, as the Manager reads it (``ManagerAgent._run_turn``)."""
    return turn.stopped is not None or bool((turn.result_event or {}).get(USER_STOP))


def _answered(turn: Any) -> bool:
    return not _stopped(turn) and turn.result_event is not None and not turn.result_event.get("is_error")


async def _interrupt_held(session: Any, seen: Seen, stop: Stop, running: asyncio.Task[Any]) -> tuple[Any, Any]:
    """The stop lands while the turn's owner holds an event: the interrupt runs while it is let go."""
    stop.asked = True
    stopping = asyncio.create_task(session.interrupt())
    await asyncio.sleep(0.2)
    seen.release.set()
    return await asyncio.wait_for(stopping, BOUND), await asyncio.wait_for(running, BOUND)


# --- One process per turn: Codex, OMP, pi, DSH (``per_turn.PerTurnSession``) ---

_PRELUDE = r'''
import json, os, subprocess, sys, time
DIR = os.environ['STOP_MATRIX_DIR']
args = sys.argv[1:]
def put(name, text, how='a'):
    with open(os.path.join(DIR, name), how) as f:
        f.write(text)
mode = open(os.path.join(DIR, 'mode')).read().strip()
put('pids', '%d\n' % os.getpid())
def child():
    c = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
    put('pids', '%d\n' % c.pid)
def wait():
    child()
    put('reached', mode, 'w')
    time.sleep(60)
    sys.exit(0)
def emit(event):
    print(json.dumps(event), flush=True)
'''

_BODY = r'''
if mode == 'blocked':
    wait()  # its message is never read
text = sys.stdin.read()
put('turns.jsonl', json.dumps({'args': args, 'sid': SID, 'size': len(text)}) + '\n')
if mode == 'silent':
    wait()
opening()
if mode == 'streaming':
    say('half an answer')
    wait()
if mode == 'tool':
    tool()
    wait()
answer('answered: ' + text[:20])
if mode == 'done':
    wait()
'''

_CODEX = r'''
SID = args[args.index('resume') + 1] if 'resume' in args else 'thread-1'
def opening():
    emit({'type': 'thread.started', 'thread_id': SID})
    emit({'type': 'turn.started'})
def say(t):
    emit({'type': 'item.completed', 'item': {'id': 'i1', 'type': 'agent_message', 'text': t}})
def tool():
    emit({'type': 'item.started', 'item': {'id': 'i2', 'type': 'command_execution', 'command': 'sleep 60',
          'aggregated_output': '', 'status': 'in_progress'}})
def answer(t):
    say(t)
    emit({'type': 'turn.completed', 'usage': {'input_tokens': 10, 'cached_input_tokens': 0,
          'cache_write_input_tokens': 0, 'output_tokens': 1, 'reasoning_output_tokens': 0}})
'''

_OMP = r'''
SID = args[args.index('--resume') + 1] if '--resume' in args else 'omp-1'
USAGE = {'input': 10, 'output': 1, 'cacheRead': 0, 'cacheWrite': 0}
def opening():
    emit({'type': 'session', 'version': 3, 'id': SID, 'cwd': os.getcwd()})
def message(stop, content):
    emit({'type': 'message_end', 'message': {'role': 'assistant', 'stopReason': stop, 'model': 'm', 'usage': USAGE,
          'content': content}})
def say(t):
    message('toolUse', [{'type': 'text', 'text': t}])
def tool():
    message('toolUse', [{'type': 'toolCall', 'id': 'c1', 'name': 'bash', 'arguments': {'command': 'sleep 60'}}])
def answer(t):
    message('stop', [{'type': 'text', 'text': t}])
    emit({'type': 'agent_end', 'messages': [], 'isTerminal': True, 'yielded': True})
'''

_PI = r'''
directory = args[args.index('--session-dir') + 1]
if '--session' in args:
    SID = json.loads(open(args[args.index('--session') + 1]).readline())['id']
else:
    SID = 'pi-1'
    with open(os.path.join(directory, SID + '.jsonl'), 'w') as f:
        f.write(json.dumps({'type': 'session', 'id': SID}) + '\n')
USAGE = {'input': 10, 'output': 1, 'cacheRead': 0, 'cacheWrite': 0}
def opening():
    emit({'type': 'session', 'id': SID, 'cwd': os.getcwd()})
def message(stop, content):
    emit({'type': 'message_end', 'message': {'role': 'assistant', 'stopReason': stop, 'model': 'm', 'usage': USAGE,
          'content': content}})
def say(t):
    message('toolUse', [{'type': 'text', 'text': t}])
def tool():
    message('toolUse', [{'type': 'toolCall', 'id': 'c1', 'name': 'bash', 'arguments': {'command': 'sleep 60'}}])
def answer(t):
    message('stop', [{'type': 'text', 'text': t}])
    emit({'type': 'agent_settled', 'aborted': False})
'''

_DSH = r'''
SID = args[args.index('--session-id') + 1] if '--session-id' in args else 'dsh-1'
def opening():
    emit({'type': 'session', 'sessionId': SID, 'cwd': os.getcwd()})
def say(t):
    emit({'type': 'text', 'text': t})
def tool():
    emit({'type': 'tool_call', 'callId': 'c1', 'tool': 'bash', 'input': {'command': 'sleep 60'}})
def answer(t):
    emit({'type': 'status', 'phase': 'step_end', 'turn': 1, 'step': 1,
          'usage': {'inputTokens': 10, 'outputTokens': 1, 'cacheReadTokens': 0, 'cacheWriteTokens': 0}})
    emit({'type': 'status', 'phase': 'turn_end', 'reason': {'kind': 'completed'}})
    emit({'type': 'final', 'text': t})
'''


def _per_turn(tmp_path: Path, runtime: str) -> tuple[Any, list[str], dict[str, str], Path]:
    """The runtime's resident session, its command, its environment, and the stand-in's directory."""
    directory = tmp_path / "matrix"
    directory.mkdir()
    body = {"codex": _CODEX, "omp": _OMP, "pi": _PI, "dsh": _DSH}[runtime]
    executable = fake_claude(tmp_path / runtime, _PRELUDE + body + _BODY)
    if runtime == "codex":
        from test_codex_runtime import CODEX, _launch, _resident_command

        command, env, session = _resident_command(tmp_path / "runtime", executable, "prompt"), \
            _launch().environment(), CODEX.resident_session("manager")
    elif runtime == "omp":
        from test_omp_runtime import OMP, _launch, _resident_command

        command, env, session = _resident_command(tmp_path / "runtime", executable, "prompt"), \
            _launch().environment(), OMP.resident_session("manager")
    elif runtime == "pi":
        from test_pi_runtime import PI, _launch, _provider

        prompt = tmp_path / ".local" / "prompts" / "prompt.md"
        prompt.parent.mkdir(parents=True, exist_ok=True)
        prompt.write_text("Looper prompt")
        launch = _launch(_provider())
        command, env = PI.resident_command(executable, cwd=tmp_path, prompt_file=prompt, launch=launch), \
            launch.environment()
        session = PI.resident_session("manager")
    else:
        from test_dsh_runtime import _run_launch, _run_prompt_file, runtimes

        dsh = runtimes.adapter("dsh")
        launch = _run_launch(tmp_path, role="manager")
        command = dsh.resident_command(executable, cwd=tmp_path, prompt_file=_run_prompt_file(tmp_path), launch=launch)
        env, session = launch.environment(), dsh.resident_session("manager")
    return session, command, {**env, "STOP_MATRIX_DIR": str(directory)}, directory


def _pids(directory: Path) -> list[int]:
    path = directory / "pids"
    return [int(line) for line in path.read_text().split()] if path.exists() else []


PER_TURN = ["codex", "omp", "pi", "dsh"]
WINDOWS = ["before_spawn", "silent", "streaming", "blocked", "tool", "done"]


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

        (directory / "mode").write_text("blocked" if window == "before_spawn" else window)
        seen, stop = Seen(), Stop()
        release = asyncio.Event()
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
            else:
                await _until(lambda: (directory / "reached").exists())
                if window == "blocked":
                    await _until(lambda: session._process is not None
                                 and session._process.stdin.transport.get_write_buffer_size() > 0)
                else:
                    await asyncio.sleep(0.3)  # what it wrote reaches the turn
            pids = _pids(directory)
            stop.asked = True
            if runtime == "codex" and window == "tool":  # one cell stops through ``interrupt()``
                method = await asyncio.wait_for(session.interrupt(), BOUND)
            else:
                cancel.request()
                method = StopMethod.KILL
            release.set()
            turn = await asyncio.wait_for(running, BOUND)
        finally:
            release.set()
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
    assert alive == []
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

# What the stand-in does with each user message is told by the message: ``mode:<window>``. ``obey`` in it makes it
# answer an interrupt of a running turn (a control response, then the turn's error result; the tool it started,
# a ``setsid`` sleeper, is left to the stop to end); else it is deaf to the interrupt. A ``stall`` file
# makes it stop reading its input after the message it is answering.
_CLAUDE = r"""
import json, os, subprocess, sys, threading, time
DIR = os.environ['STOP_MATRIX_DIR']
def put(name, text, how='a'):
    with open(os.path.join(DIR, name), how) as f:
        f.write(text)
def out(event):
    print(json.dumps(event), flush=True)
put('pids', '%d\n' % os.getpid())
state = {'running': False, 'tool': None, 'obey': False}
def child():
    c = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], start_new_session=True)  # setsid
    put('pids', '%d\n' % c.pid)
    return c
def result(text, ok=True):
    state['running'] = False
    out({'type': 'result', 'subtype': 'success' if ok else 'error_during_execution', 'is_error': not ok,
         'result': text, 'total_cost_usd': 0.01, 'session_id': 's1', 'num_turns': 1})
def run(text):
    mode = text.partition('mode:')[2].split()[0] if 'mode:' in text else 'answer'
    state['obey'], state['running'] = 'obey' in text, True
    put('turns.jsonl', json.dumps({'mode': mode, 'size': len(text)}) + '\n')
    if mode == 'silent':
        put('reached', mode, 'w')
        return
    out({'type': 'system', 'subtype': 'init', 'session_id': 's1'})
    if mode == 'streaming':
        out({'type': 'stream_event', 'event': {'type': 'content_block_delta',
             'delta': {'type': 'text_delta', 'text': 'half an answer'}}})
        put('reached', mode, 'w')
        return
    if mode == 'tool':
        out({'type': 'assistant', 'message': {'role': 'assistant', 'content': [
             {'type': 'tool_use', 'id': 't1', 'name': 'Bash', 'input': {'command': 'sleep 60'}}]}})
        state['tool'] = child()
        put('reached', mode, 'w')
        return
    out({'type': 'assistant', 'message': {'role': 'assistant', 'content': [{'type': 'text', 'text': 'answered'}]}})
    result('answered ' + mode)
for raw in sys.stdin:
    message = json.loads(raw)
    if message['type'] == 'control_request':
        if state['obey']:
            out({'type': 'control_response', 'response': {'subtype': 'success',
                 'request_id': message['request_id']}})
            if state['running']:
                result('', ok=False)
        continue
    run(message['message']['content'])
    if os.path.exists(os.path.join(DIR, 'stall')):
        put('reached', 'blocked', 'w')
        time.sleep(60)
"""

CLAUDE_WINDOWS = ["silent", "streaming", "blocked", "tool", "done"]


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

GROK_WINDOWS = ["before_spawn", "silent", "streaming", "blocked", "tool", "done"]


@pytest.mark.parametrize("window", GROK_WINDOWS)
def test_grok_acp_stop_in_every_window(tmp_path: Path, window: str, monkeypatch: pytest.MonkeyPatch) -> None:
    from test_grok_acp import _command, _fake
    from test_grok_runtime import GROK, _launch, _operator

    _operator(tmp_path, monkeypatch)
    # The stand-in reads the cancel only once its tool calls are written (about 2 s): a real Grok reads it at once.
    monkeypatch.setattr(grok_acp, "CANCEL_GRACE_SEC", 3.0 if window == "tool" else 0.5)
    monkeypatch.setattr(proc_tree, "TERM_GRACE_SEC", 0.2)
    root, grok = tmp_path / "runtime", _fake(tmp_path)
    stall = root / ".local" / "grok" / "stall"

    async def run() -> tuple[Any, ...]:
        session = grok_acp.AcpGrok(label="manager")
        env = _launch().environment()
        await session.start(_command(root, grok), cwd=str(tmp_path), signature="s", env=env)
        first = await session.run_turn("hello", Seen())
        assert _answered(first), first.result_event
        sid = session._session
        if window in ("before_spawn", "blocked"):
            # A new process loads the session for the stopped turn: deaf to ``initialize``, or to the prompt.
            await session._end_process()
            if window == "before_spawn":
                session._env = {**env, "FAKE_ACP_SILENT": "1"}
            else:
                stall.touch()
        seen, stop = Seen(hold_at="result" if window == "done" else None), Stop()
        text = {"before_spawn": "work", "silent": "QUIET", "streaming": "HANG", "blocked": BIG, "tool": "LOOP",
                "done": "work"}[window]
        running = asyncio.create_task(session.run_turn(text, seen, stop=stop))
        try:
            if window == "done":
                await asyncio.wait_for(seen.holding.wait(), BOUND)
                pid = session.pid
                method, turn = await _interrupt_held(session, seen, stop, running)
            else:
                if window == "before_spawn":
                    await _until(lambda: session._process is not None)
                    await asyncio.sleep(0.3)  # ``initialize`` is waiting
                elif window == "blocked":
                    await _until(lambda: session._process is not None and session._prompt_id is not None
                                 and session._process.stdin.transport.get_write_buffer_size() > 0)
                elif window == "tool":
                    await _until(lambda: any(e.get("type") == "assistant" for e in seen.events))
                else:
                    await _until(lambda: session._prompt_id is not None and bool(seen.events))
                    await asyncio.sleep(0.3)
                pid = session.pid
                stop.asked = True
                method = await asyncio.wait_for(session.interrupt(), BOUND)
                turn = await asyncio.wait_for(running, BOUND)
        finally:
            seen.release.set()
            running.cancel()
            await asyncio.gather(running, return_exceptions=True)
        seen.closed = True
        session._env = env
        stall.unlink(missing_ok=True)
        gone = pid is None or not _alive(pid) or session.pid == pid
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
    assert method is (StopMethod.KILL if window in ("before_spawn", "blocked") else StopMethod.CANCEL)
    assert _stopped(turn) and turn.stopped is method, turn.result_event
    assert turn.result_event.get("is_error"), turn.result_event  # never a success
    assert not seen.late and gone
    assert transport == "acp"  # a stop never sends the session per turn
    assert _answered(after) and "| next |" in after.result_event["result"], after.result_event
    assert resumed == sid
    assert "thinking" not in json.dumps(nxt.events)
