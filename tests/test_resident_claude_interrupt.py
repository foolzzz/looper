"""The races of a resident Claude's stopped turn that no cell of ``tests/test_stop_matrix.py`` has a stand-in for: an
event that lands as the interrupt is made, a result queued behind it, what a killed Claude writes as it dies, a stop
before the turn's prompt is written or after its result. Each stop window (silent, streaming, blocked, tool, done;
obeyed or deaf) is in the matrix."""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
from pathlib import Path
from typing import Any

import pytest

from fake_claude import fake_claude
from looper.agents import claude_cli, proc_tree
from looper.agents.claude_cli import ResidentClaude
from looper.agents.runtimes import StopMethod
from looper.agents.runtimes.cancel import TurnCancel

_OBEYS = """\
import json, os, sys, time
def out(event):
    print(json.dumps(event), flush=True)
turns = 0
for raw in sys.stdin:
    message = json.loads(raw)
    if message['type'] == 'control_request':
        out({'type': 'control_response', 'response': {'subtype': 'success', 'request_id': message['request_id']}})
        out({'type': 'result', 'subtype': 'error_during_execution', 'is_error': True, 'total_cost_usd': 0.02,
             'session_id': 's1', 'num_turns': 1})
        continue
    turns += 1
    if turns == 1:
        out({'type': 'system', 'subtype': 'init', 'session_id': 's1'})
        out({'type': 'stream_event', 'event': {'type': 'content_block_delta', 'delta': {'type': 'text_delta', 'text': 'hi'}}})
    else:
        out({'type': 'system', 'subtype': 'init', 'session_id': 's1'})
        out({'type': 'result', 'subtype': 'success', 'is_error': False, 'result': 'pid=%d' % os.getpid(),
             'total_cost_usd': 0.03, 'session_id': 's1', 'num_turns': 1})
"""

# Streams text while it listens: the interrupt arrives (and is answered) between two of its deltas.
_STREAMS = """\
import json, os, sys, threading, time
lock = threading.Lock()
def out(event):
    with lock:
        print(json.dumps(event), flush=True)
interrupted = threading.Event()
def listen():
    for raw in sys.stdin:
        message = json.loads(raw)
        if message['type'] == 'control_request':
            interrupted.set()
            time.sleep(0.15)
            out({'type': 'control_response', 'response': {'subtype': 'success', 'request_id': message['request_id']}})
            out({'type': 'result', 'subtype': 'error_during_execution', 'is_error': True, 'total_cost_usd': 0.02,
                 'session_id': 's1', 'num_turns': 1})
        elif message['type'] == 'user':
            inbox.append(message)
inbox = []
threading.Thread(target=listen, daemon=True).start()
turns = 0
while True:
    if not inbox:
        time.sleep(0.01)
        continue
    inbox.pop(0)
    turns += 1
    out({'type': 'system', 'subtype': 'init', 'session_id': 's1'})
    if turns == 1:
        while not interrupted.is_set():
            out({'type': 'stream_event', 'event': {'type': 'content_block_delta', 'delta': {'type': 'text_delta', 'text': 'hi'}}})
            time.sleep(0.05)
        for _ in range(20):
            out({'type': 'stream_event', 'event': {'type': 'content_block_delta', 'delta': {'type': 'text_delta', 'text': 'hi'}}})
            time.sleep(0.01)
    else:
        out({'type': 'result', 'subtype': 'success', 'is_error': False, 'result': 'pid=%d' % os.getpid(),
             'total_cost_usd': 0.03, 'session_id': 's1', 'num_turns': 1})
"""

# Deaf to the interrupt; ending (SIGTERM) it writes the stopped turn's answer and result before it exits.
_ANSWERS_AS_IT_DIES = """\
import json, signal, sys, time
def out(event):
    print(json.dumps(event), flush=True)
def die(*_):
    out({'type': 'assistant', 'message': {'role': 'assistant', 'content': [{'type': 'text', 'text': 'too late'}]}})
    out({'type': 'result', 'subtype': 'success', 'is_error': False, 'result': 'too late', 'total_cost_usd': 0.5,
         'session_id': 's1', 'num_turns': 1})
    sys.exit(0)
signal.signal(signal.SIGTERM, die)
for raw in sys.stdin:
    message = json.loads(raw)
    if message['type'] == 'user':
        out({'type': 'system', 'subtype': 'init', 'session_id': 's1'})
        time.sleep(60)
"""

async def _until(check: Any) -> None:
    for _ in range(500):
        if check():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("timed out")


async def _until_streaming(seen: list[dict[str, Any]]) -> None:
    for _ in range(500):
        if any(e.get("type") == "stream_event" or e.get("subtype") == "init" for e in seen):
            return
        await asyncio.sleep(0.01)
    raise AssertionError("the turn never started")


def test_interrupt_with_no_turn_does_nothing(tmp_path: Path) -> None:
    claude = fake_claude(tmp_path / "claude", _OBEYS)

    async def _run() -> None:
        session = ResidentClaude(label="manager")
        await session.start([claude], cwd=str(tmp_path), signature=None)
        try:
            assert await session.interrupt() is None
        finally:
            await session.stop(kill=True)

    asyncio.run(_run())


def test_an_event_during_the_interrupt_does_not_end_the_next_turn(tmp_path: Path) -> None:
    """The stop hook that fires on the next event must not also end the turn while the interrupt is under way:
    the process is kept, so its output after the turn must still reach the next one."""
    claude = fake_claude(tmp_path / "claude", _STREAMS)

    async def _run() -> None:
        session = ResidentClaude(label="manager")
        await session.start([claude], cwd=str(tmp_path), signature=None)
        seen: list[dict[str, Any]] = []
        asked = False

        async def on_event(event: dict[str, Any]) -> None:
            seen.append(event)

        def stop() -> claude_cli.CapStop | None:
            return claude_cli.CapStop(claude_cli.user_stop_result(), claude_cli.Usage()) if asked else None

        try:
            turn = asyncio.create_task(session.run_turn("work", on_event, stop=stop))
            await _until_streaming(seen)
            asked = True
            assert await session.interrupt() is StopMethod.CONTROL
            result = await asyncio.wait_for(turn, 5)
            assert result.stopped is StopMethod.CONTROL
            again = await asyncio.wait_for(session.run_turn("next", on_event), 5)
            assert again.result_event is not None and again.result_event["result"].startswith("pid=")
        finally:
            await session.stop(kill=True)

    asyncio.run(_run())


def test_a_stop_that_lands_on_an_event_before_the_interrupt_kills_and_says_so(tmp_path: Path) -> None:
    claude = fake_claude(tmp_path / "claude", _STREAMS)

    async def _run() -> None:
        session = ResidentClaude(label="manager")
        await session.start([claude], cwd=str(tmp_path), signature=None)
        seen: list[dict[str, Any]] = []

        async def on_event(event: dict[str, Any]) -> None:
            seen.append(event)

        def stop() -> claude_cli.CapStop | None:
            return claude_cli.CapStop(claude_cli.user_stop_result(), claude_cli.Usage()) if len(seen) > 3 else None

        try:
            result = await asyncio.wait_for(session.run_turn("work", on_event, stop=stop), 5)
            assert result.stopped is StopMethod.KILL
            assert session.started is False
        finally:
            await session.stop(kill=True)

    asyncio.run(_run())


def test_what_a_killed_claude_writes_as_it_dies_is_no_turn_of_its_own(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The stopped turn's output that is still read while Claude is killed belongs to no turn: it is neither a
    turn Claude ran on its own (whose answer the Manager would send to the chat) nor spend of the session."""
    monkeypatch.setattr(claude_cli, "INTERRUPT_GRACE_SEC", 0.2)
    claude = fake_claude(tmp_path / "claude", _ANSWERS_AS_IT_DIES)
    own: list[Any] = []

    async def autonomous(tag: Any) -> Any:
        own.append(tag)
        raise AssertionError("a killed turn's output began a turn of Claude's own")

    async def _run() -> None:
        session = ResidentClaude(label="manager")
        await session.start([claude], cwd=str(tmp_path), signature=None, autonomous=autonomous)
        seen: list[dict[str, Any]] = []

        async def on_event(event: dict[str, Any]) -> None:
            seen.append(event)

        try:
            turn = asyncio.create_task(session.run_turn("work", on_event))
            await _until_streaming(seen)
            assert await session.interrupt() is StopMethod.KILL
            result = await asyncio.wait_for(turn, 10)
            assert result.stopped is StopMethod.KILL
            await asyncio.sleep(0.3)  # the reader has had what Claude wrote as it died
            assert own == []
            assert session.totals.cost_usd in (None, 0)
            assert not any(e.get("type") == "result" for e in seen)
        finally:
            await session.stop(kill=True)

    asyncio.run(_run())


def test_a_killed_turn_returns_only_after_its_last_event_was_handled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An event of the stopped turn its owner was still handling when Claude was killed is handled before the turn
    returns: nothing of it reaches the owner after the turn (its record closed, the next turn begun)."""
    monkeypatch.setattr(claude_cli, "INTERRUPT_GRACE_SEC", 0.2)
    claude = fake_claude(tmp_path / "claude", _ANSWERS_AS_IT_DIES)

    async def _run() -> None:
        session = ResidentClaude(label="manager")
        await session.start([claude], cwd=str(tmp_path), signature=None)
        handling: list[str] = []

        async def on_event(event: dict[str, Any]) -> None:
            handling.append("in")
            await asyncio.sleep(1.5)  # a slow write of the event (its record, the chat)
            handling.append("out")

        async def run_turn() -> Any:
            result = await session.run_turn("work", on_event)
            handling.append("returned")
            return result

        try:
            turn = asyncio.create_task(run_turn())
            await _until(lambda: handling == ["in"])
            assert await session.interrupt() is StopMethod.KILL
            result = await asyncio.wait_for(turn, 10)
            assert result.stopped is StopMethod.KILL
            assert handling == ["in", "out", "returned"]
        finally:
            await session.stop(kill=True)

    asyncio.run(_run())


# --- the turn's cancel (``run_turn(cancel=)``) ---

# Logs each line it reads to ``inbox``; a user message starts a turn whose tool ``setsid``s a sleeper (its pid in
# ``sleeper.pid``) and goes on; ``obey`` is its ``OBEY`` env: it answers an interrupt, else it is deaf to it. After
# the interrupt it keeps writing text (``LATE``) and, with ``QUEUED``, a success result right behind its answer.
_TOOL = """\
import json, os, subprocess, sys, time
def out(event):
    print(json.dumps(event), flush=True)
for raw in sys.stdin:
    with open('inbox', 'a') as f:
        f.write(raw)
    message = json.loads(raw)
    if message['type'] == 'control_request':
        if os.environ.get('OBEY') != '1':
            continue
        out({'type': 'control_response', 'response': {'subtype': 'success', 'request_id': message['request_id']}})
        if os.environ.get('LATE'):
            for _ in range(5):
                out({'type': 'stream_event', 'event': {'type': 'content_block_delta',
                     'delta': {'type': 'text_delta', 'text': 'late text'}}})
                time.sleep(0.02)
        if os.environ.get('QUEUED'):
            out({'type': 'result', 'subtype': 'success', 'is_error': False, 'result': 'an answer', 'session_id': 's1',
                 'total_cost_usd': 0.02, 'num_turns': 1})
        else:
            out({'type': 'result', 'subtype': 'error_during_execution', 'is_error': True, 'session_id': 's1',
                 'total_cost_usd': 0.02, 'num_turns': 1})
        continue
    out({'type': 'system', 'subtype': 'init', 'session_id': 's1'})
    if message['message']['content'] == 'work':
        sleeper = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], start_new_session=True)
        with open('sleeper.pid', 'w') as f:
            f.write(str(sleeper.pid))
        out({'type': 'stream_event', 'event': {'type': 'content_block_delta',
             'delta': {'type': 'text_delta', 'text': 'working'}}})
        if os.environ.get('ANSWER'):  # answers, leaving its background tool running
            out({'type': 'result', 'subtype': 'success', 'is_error': False, 'result': 'done', 'session_id': 's1',
                 'total_cost_usd': 0.02, 'num_turns': 1})
        continue
    out({'type': 'result', 'subtype': 'success', 'is_error': False, 'result': 'pid=%d' % os.getpid(),
         'total_cost_usd': 0.03, 'session_id': 's1', 'num_turns': 1})
"""


_ENV: dict[str, str] = {}


def _gone(pid: int) -> bool:
    return pid not in proc_tree._snapshot()


def _signals(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Every signal ``proc_tree`` sends from now on (still sent)."""
    sent: list[int] = []
    real = proc_tree._signal

    def record(process: Any, tree: Any, sig: int) -> None:
        sent.append(sig)
        real(process, tree, sig)

    monkeypatch.setattr(proc_tree, "_signal", record)
    return sent


def _kill(pid: int) -> None:
    with contextlib.suppress(ProcessLookupError):
        os.kill(pid, signal.SIGKILL)


def test_a_cancel_before_run_turn_writes_nothing_and_leaves_the_process(tmp_path: Path) -> None:
    claude = fake_claude(tmp_path / "claude", _OBEYS)

    async def _run() -> None:
        session = ResidentClaude(label="manager")
        await session.start([claude], cwd=str(tmp_path), signature=None)
        pid = session.pid
        seen: list[dict[str, Any]] = []

        async def on_event(event: dict[str, Any]) -> None:
            seen.append(event)

        try:
            cancel = TurnCancel()
            cancel.request()
            turn = await asyncio.wait_for(session.run_turn("work", on_event, cancel=cancel), 5)
            assert turn.stopped is StopMethod.KILL and turn.prompt_sent is False
            assert turn.result_event is not None and turn.result_event[claude_cli.USER_STOP]
            assert seen == [] and session.started and session.pid == pid
            assert pid is not None
            os.kill(pid, 0)
            assert session._output is not None and session._output.turn is None
        finally:
            await session.stop(kill=True)

    asyncio.run(_run())


def _tool_session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **env: str) -> tuple[ResidentClaude, str]:
    """A session and a stand-in; the session's ``start`` gets ``_ENV`` (the process' environment is filtered)."""
    _ENV.clear()
    _ENV.update({**os.environ, **env})
    monkeypatch.setattr(claude_cli, "INTERRUPT_GRACE_SEC", 0.5)
    monkeypatch.setattr(claude_cli, "STOP_GRACE_SEC", 0.5)
    monkeypatch.setattr(proc_tree, "TERM_GRACE_SEC", 0.2)
    return ResidentClaude(label="manager"), fake_claude(tmp_path / "claude", _TOOL)


async def _until_sleeper(tmp_path: Path) -> int:
    await _until(lambda: (tmp_path / "sleeper.pid").exists() and (tmp_path / "sleeper.pid").read_text())
    return int((tmp_path / "sleeper.pid").read_text())


def test_a_result_queued_behind_the_control_request_is_not_an_answer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    session, claude = _tool_session(tmp_path, monkeypatch, OBEY="1", QUEUED="1")

    async def _run() -> None:
        await session.start([claude], cwd=str(tmp_path), signature=None, env=_ENV)
        seen: list[dict[str, Any]] = []

        async def on_event(event: dict[str, Any]) -> None:
            seen.append(event)

        try:
            cancel = TurnCancel()
            running = asyncio.create_task(session.run_turn("work", on_event, cancel=cancel))
            await _until_streaming(seen)
            cancel.request()
            turn = await asyncio.wait_for(running, 5)
            assert turn.stopped is StopMethod.CONTROL
            assert turn.result_event is not None and turn.result_event["result"] == "an answer"
            assert not any(e.get("type") == "result" for e in seen)
        finally:
            await session.stop(kill=True)

    asyncio.run(_run())


def test_a_stop_between_the_result_and_the_return_ends_the_turn_and_signals_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The result is dispatched (the output forgets the turn) and run_turn has not yet returned: the stop is the
    # turn's, which is over already; nothing is interrupted or signalled, its background tool runs on.
    session, claude = _tool_session(tmp_path, monkeypatch, OBEY="1", ANSWER="1")
    sent = _signals(monkeypatch)

    async def _run() -> None:
        await session.start([claude], cwd=str(tmp_path), signature=None, env=_ENV)
        pid = session.pid
        cancel = TurnCancel()

        async def on_event(event: dict[str, Any]) -> None:
            if event.get("type") == "result":
                asyncio.get_running_loop().call_soon(cancel.request)  # right after the dispatch clears the turn

        try:
            turn = await asyncio.wait_for(session.run_turn("work", on_event, cancel=cancel), 5)
            sleeper = await _until_sleeper(tmp_path)
            assert cancel.requested
            assert turn.stopped is StopMethod.CONTROL and turn.usage_complete  # a whole turn's result
            assert turn.result_event is not None and turn.result_event["result"] == "done"
            assert not _gone(sleeper) and sent == []
            assert session.started and session.pid == pid
            assert session._user_turn is None and session._interrupting is None
            inbox = (tmp_path / "inbox").read_text()
            assert "control_request" not in inbox  # nothing left to interrupt
        finally:
            await session.stop(kill=True)
            if (tmp_path / "sleeper.pid").exists():
                _kill(int((tmp_path / "sleeper.pid").read_text()))

    asyncio.run(_run())
