"""Interrupting a resident Claude's running turn with a control request, and killing it after a grace."""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import time
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
        out({'type': 'system', 'subtype': 'init', 'session_id': 's1', 'pid': os.getpid()})
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

_DEAF = """\
import json, sys, time
for raw in sys.stdin:
    message = json.loads(raw)
    if message['type'] == 'user':
        print(json.dumps({'type': 'system', 'subtype': 'init', 'session_id': 's1'}), flush=True)
        time.sleep(60)
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

# Never writes a thing, and deaf to the interrupt.
_SILENT = """\
import sys, time
for raw in sys.stdin:
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


@pytest.mark.parametrize("label", ["planner", "manager"])
@pytest.mark.parametrize("stage", ["baseline", "result"])
@pytest.mark.parametrize("ending", ["timeout", "cancel"])
def test_an_abandoned_exchange_kills_and_forgets_the_resident(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, label: str, stage: str, ending: str,
) -> None:
    claude = fake_claude(tmp_path / "claude", _DEAF)

    async def run() -> None:
        session = ResidentClaude(label=label)
        await session.start([claude], cwd=str(tmp_path), signature=None)
        process = session._process
        assert process is not None
        entered = asyncio.Event()
        stopped: list[bool] = []
        stop = session.stop

        async def stopping(*, kill: bool = False) -> None:
            stopped.append(kill)
            await stop(kill=kill)

        async def baseline(self: Any) -> None:
            entered.set()
            await asyncio.Event().wait()

        async def event(event: dict[str, Any]) -> None:
            if event.get("subtype") == "init":
                entered.set()

        monkeypatch.setattr(session, "stop", stopping)
        if stage == "baseline":
            monkeypatch.setattr(proc_tree.TurnTree, "baseline", baseline)
        turn = asyncio.create_task(session.run_turn("work", event))
        try:
            await asyncio.wait_for(entered.wait(), 5)
            if ending == "timeout":
                with pytest.raises(TimeoutError):
                    await asyncio.wait_for(turn, 0.1)
            else:
                turn.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await turn
            assert stopped == [True]
            assert session.started is False
            assert process.returncode is not None
        finally:
            await stop(kill=True)

    asyncio.run(run())


def test_an_obeyed_interrupt_ends_the_turn_and_keeps_the_process(tmp_path: Path) -> None:
    claude = fake_claude(tmp_path / "claude", _OBEYS)

    async def _run() -> None:
        session = ResidentClaude(label="manager")
        await session.start([claude], cwd=str(tmp_path), signature=None)
        seen: list[dict[str, Any]] = []

        async def on_event(event: dict[str, Any]) -> None:
            seen.append(event)

        pid = session.pid
        try:
            turn = asyncio.create_task(session.run_turn("work", on_event))
            await _until_streaming(seen)
            (agent_pid,) = [event["pid"] for event in seen if event.get("subtype") == "init"]
            started = time.monotonic()
            assert await session.interrupt() is StopMethod.CONTROL
            assert time.monotonic() - started < 1
            result = await turn
            assert result.stopped is StopMethod.CONTROL
            # Claude's interrupted result does not carry what the aborted request spent (measured with 2.1.x).
            assert result.usage_complete is False
            assert result.result_event is not None and result.result_event["is_error"] is True
            assert not any(e.get("type") == "control_response" for e in seen)
            assert session.totals.cost_usd == pytest.approx(0.02)
            assert result.usage.cost_usd == pytest.approx(0.02)
            again = await session.run_turn("next", on_event)
            assert again.stopped is None
            assert again.result_event is not None and again.result_event["result"] == f"pid={agent_pid}"
            assert session.pid == pid
        finally:
            await session.stop(kill=True)

    asyncio.run(_run())


def test_a_deaf_claude_is_killed_after_the_grace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(claude_cli, "INTERRUPT_GRACE_SEC", 0.3)
    claude = fake_claude(tmp_path / "claude", _DEAF)

    async def _run() -> None:
        session = ResidentClaude(label="manager")
        await session.start([claude], cwd=str(tmp_path), signature=None)
        pid = session.pid
        seen: list[dict[str, Any]] = []

        async def on_event(event: dict[str, Any]) -> None:
            seen.append(event)

        try:
            turn = asyncio.create_task(session.run_turn("work", on_event))
            await _until_streaming(seen)
            assert await session.interrupt() is StopMethod.KILL
            result = await turn
            assert result.stopped is StopMethod.KILL
            assert session.started is False
            assert pid is not None
            with pytest.raises(ProcessLookupError):
                os.kill(pid, 0)
        finally:
            await session.stop(kill=True)

    asyncio.run(_run())


def test_a_stop_before_claudes_first_event_is_not_lost(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(claude_cli, "INTERRUPT_GRACE_SEC", 0.3)
    claude = fake_claude(tmp_path / "claude", _SILENT)

    async def _run() -> None:
        session = ResidentClaude(label="manager")
        await session.start([claude], cwd=str(tmp_path), signature=None)
        pid = session.pid

        async def on_event(event: dict[str, Any]) -> None:
            pass

        try:
            turn = asyncio.create_task(session.run_turn("work", on_event))
            await _until(lambda: session._output is not None and session._output.turn is not None)
            assert await asyncio.wait_for(session.interrupt(), 5) is StopMethod.KILL
            result = await asyncio.wait_for(turn, 5)
            assert result.stopped is StopMethod.KILL
            assert session.started is False
            assert pid is not None
            with pytest.raises(ProcessLookupError):
                os.kill(pid, 0)
        finally:
            await session.stop(kill=True)

    asyncio.run(_run())


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


def test_a_killed_turn_returns_only_after_its_process_tree_is_gone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(claude_cli, "INTERRUPT_GRACE_SEC", 0.2)
    claude = fake_claude(tmp_path / "claude", _DEAF)
    real_end = proc_tree.TurnTree.end
    ended: list[bool] = []

    async def slow_end(*args: Any, **kwargs: Any) -> Any:
        await asyncio.sleep(0.5)
        try:
            return await real_end(*args, **kwargs)
        finally:
            ended.append(True)

    monkeypatch.setattr(proc_tree.TurnTree, "end", slow_end)

    async def _run() -> None:
        session = ResidentClaude(label="manager")
        await session.start([claude], cwd=str(tmp_path), signature=None)
        seen: list[dict[str, Any]] = []

        async def on_event(event: dict[str, Any]) -> None:
            seen.append(event)

        try:
            turn = asyncio.create_task(session.run_turn("work", on_event))
            await _until_streaming(seen)
            stopper = asyncio.create_task(session.interrupt())
            result = await turn
            assert result.stopped is StopMethod.KILL
            assert ended == [True]
            await stopper
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


def test_an_interrupt_blocked_by_stdin_backpressure_still_kills_after_the_grace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(claude_cli, "INTERRUPT_GRACE_SEC", 0.3)
    claude = fake_claude(tmp_path / "claude", _DEAF)

    async def _run() -> None:
        session = ResidentClaude(label="manager")
        await session.start([claude], cwd=str(tmp_path), signature=None)
        pid = session.pid

        async def on_event(event: dict[str, Any]) -> None:
            pass

        try:
            turn = asyncio.create_task(session.run_turn("work", on_event))
            await _until(lambda: session._output is not None and session._output.turn is not None)
            assert session._process is not None and session._process.stdin is not None

            async def never_drains() -> None:
                await asyncio.Event().wait()  # a full pipe: the write never completes

            session._process.stdin.drain = never_drains  # type: ignore[method-assign]
            assert await asyncio.wait_for(session.interrupt(), 5) is StopMethod.KILL
            result = await asyncio.wait_for(turn, 5)
            assert result.stopped is StopMethod.KILL
            assert pid is not None
            with pytest.raises(ProcessLookupError):
                os.kill(pid, 0)
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


async def _next_turn_answers(session: ResidentClaude, on_event: Any) -> Any:
    return await asyncio.wait_for(session.run_turn("next", on_event), 5)


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


def test_an_obeyed_cancel_ends_the_tools_and_keeps_the_process(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    session, claude = _tool_session(tmp_path, monkeypatch, OBEY="1")

    async def _run() -> None:
        await session.start([claude], cwd=str(tmp_path), signature=None, env=_ENV)
        pid = session.pid
        seen: list[dict[str, Any]] = []

        async def on_event(event: dict[str, Any]) -> None:
            seen.append(event)

        try:
            cancel = TurnCancel()
            running = asyncio.create_task(session.run_turn("work", on_event, cancel=cancel))
            sleeper = await _until_sleeper(tmp_path)
            await _until_streaming(seen)
            assert not _gone(sleeper)
            cancel.request()
            turn = await asyncio.wait_for(running, 5)
            assert turn.stopped is StopMethod.CONTROL and turn.prompt_sent is True
            assert session.started and session.pid == pid
            assert _gone(sleeper)
            _ENV["OBEY"] = "0"
            again = await _next_turn_answers(session, on_event)
            assert again.stopped is None and session.pid == pid
        finally:
            await session.stop(kill=True)

    asyncio.run(_run())


def test_a_deaf_claude_is_killed_with_its_whole_tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    session, claude = _tool_session(tmp_path, monkeypatch)

    async def _run() -> None:
        await session.start([claude], cwd=str(tmp_path), signature=None, env=_ENV)
        pid = session.pid
        seen: list[dict[str, Any]] = []

        async def on_event(event: dict[str, Any]) -> None:
            seen.append(event)

        try:
            cancel = TurnCancel()
            running = asyncio.create_task(session.run_turn("work", on_event, cancel=cancel))
            sleeper = await _until_sleeper(tmp_path)
            await _until_streaming(seen)
            started = time.monotonic()
            cancel.request()
            turn = await asyncio.wait_for(running, 5)
            assert time.monotonic() - started < 3
            assert turn.stopped is StopMethod.KILL and not session.started
            assert pid is not None and _gone(pid) and _gone(sleeper)
            await session.start([claude], cwd=str(tmp_path), signature=None, env=_ENV)  # resumes the session
            again = await _next_turn_answers(session, on_event)
            assert again.stopped is None
        finally:
            await session.stop(kill=True)

    asyncio.run(_run())


def test_nothing_of_a_stopped_turn_reaches_on_event_after_the_stop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    session, claude = _tool_session(tmp_path, monkeypatch, OBEY="1", LATE="1")

    async def _run() -> None:
        await session.start([claude], cwd=str(tmp_path), signature=None, env=_ENV)
        seen: list[dict[str, Any]] = []

        async def on_event(event: dict[str, Any]) -> None:
            seen.append(event)

        try:
            cancel = TurnCancel()
            running = asyncio.create_task(session.run_turn("work", on_event, cancel=cancel))
            await _until_streaming(seen)
            await asyncio.sleep(0.1)
            before = list(seen)
            cancel.request()
            turn = await asyncio.wait_for(running, 5)
            assert turn.stopped is StopMethod.CONTROL
            await asyncio.sleep(0.2)
            assert seen == before
        finally:
            await session.stop(kill=True)

    asyncio.run(_run())


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


def test_a_stop_between_the_result_and_the_return_still_ends_the_turns_tools(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The result is dispatched (the output forgets the turn) and run_turn has not yet returned: the stop is the
    # turn's, its background tool is ended, and the process stays.
    session, claude = _tool_session(tmp_path, monkeypatch, OBEY="1", ANSWER="1")

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
            assert _gone(sleeper)
            assert session.started and session.pid == pid
            assert session._user_turn is None and session._interrupting is None
            inbox = (tmp_path / "inbox").read_text()
            assert "control_request" not in inbox  # nothing left to interrupt: only the turn's tools are ended
        finally:
            await session.stop(kill=True)

    asyncio.run(_run())


# Starts a sleeper in a session of its own (its pid in ``sleeper.pid``), and runs on after its stdin closes.
_IGNORES_EOF = """\
import subprocess, sys, time
sleeper = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], start_new_session=True)
with open('sleeper.pid', 'w') as f:
    f.write(str(sleeper.pid))
for raw in sys.stdin:
    pass
time.sleep(60)
"""


def test_a_stop_whose_keep_budget_runs_out_in_a_slow_ps_still_ends_the_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The manager's ``keep("claude_stop", stop(), 10)`` with Claude using all of ``STOP_GRACE_SEC`` (5 s) and the
    end's ``ps`` all of ``_PS_TIMEOUT_SEC`` (5 s), scaled down: the budget runs out inside the tree's end, whose
    signals and kill of strays still come."""
    from datetime import datetime, timezone

    from looper.agents.tiers.turn_token import TurnToken

    monkeypatch.setattr(claude_cli, "STOP_GRACE_SEC", 0.5)
    monkeypatch.setattr(proc_tree, "TERM_GRACE_SEC", 0.2)
    monkeypatch.setattr(proc_tree, "REAP_SEC", 0.5)
    claude = fake_claude(tmp_path / "claude", _IGNORES_EOF)
    snapshot = proc_tree._snapshot
    closed: list[Any] = []

    def slow_once_stdin_closes() -> proc_tree.Table:
        if closed and closed[0].stdin.is_closing():
            time.sleep(proc_tree._PS_TIMEOUT_SEC)
        return snapshot()

    monkeypatch.setattr(proc_tree, "_snapshot", slow_once_stdin_closes)

    async def _run() -> None:
        session = ResidentClaude(label="manager")
        await session.start([claude], cwd=str(tmp_path), signature=None)
        assert session._process is not None
        process = session._process
        sleeper = await _until_sleeper(tmp_path)
        assert session._tree is not None
        await session._tree.refresh(force=True)
        starts = dict(session._tree.seen)
        assert starts.get(sleeper) is not None  # the visible child's identity is known before the ps fault
        monkeypatch.setattr(proc_tree, "_start_times", lambda pids: {pid: starts[pid] for pid in pids})
        monkeypatch.setattr(proc_tree, "_PS_TIMEOUT_SEC", 0.5)
        try:
            closed.append(process)
            token = TurnToken(message_id="m1", client_message_id=None, channel="web", chat="c",
                              received_at=datetime.now(timezone.utc))
            # Past the grace, inside the end's first ps.
            budget = claude_cli.STOP_GRACE_SEC + proc_tree._PS_TIMEOUT_SEC / 2
            with pytest.raises(TimeoutError):
                await token.keep("claude_stop", session.stop(), budget)
            assert process.pid not in snapshot() and sleeper not in snapshot()
        finally:
            for pid in (process.pid, sleeper):
                with contextlib.suppress(ProcessLookupError):
                    os.kill(pid, signal.SIGKILL)
            with contextlib.suppress(Exception):
                await process.wait()

    asyncio.run(_run())


@pytest.mark.parametrize("stage", ["reader", "autonomous"])
def test_output_cleanup_is_bounded_even_if_a_handler_suppresses_cancellation(
    monkeypatch: pytest.MonkeyPatch, stage: str,
) -> None:
    monkeypatch.setattr(claude_cli, "_DRAIN_SEC", 0.05)

    async def run() -> None:
        output = claude_cli._Output(ResidentClaude(label="manager"), asyncio.StreamReader(), None)
        entered, release = asyncio.Event(), asyncio.Event()
        tasks: list[asyncio.Task[None]] = []

        async def stuck(*args: Any) -> None:
            task = asyncio.current_task()
            assert task is not None
            tasks.append(task)
            entered.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                await release.wait()

        if stage == "reader":
            output._task = asyncio.create_task(stuck())
            await entered.wait()
        else:
            monkeypatch.setattr(output, "close_own", stuck)
        try:
            await asyncio.wait_for(output.finish(), 1)
            assert entered.is_set()
            assert any(not task.done() for task in tasks)
        finally:
            release.set()
            await asyncio.wait_for(asyncio.gather(*tasks), 1)

    asyncio.run(run())
