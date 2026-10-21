"""Interrupting a resident Claude's running turn with a control request, and killing it after a grace."""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path
from typing import Any

import pytest

from fake_claude import fake_claude
from looper.agents import claude_cli
from looper.agents.claude_cli import ResidentClaude
from looper.agents.runtimes import StopMethod

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
            assert again.result_event is not None and again.result_event["result"] == f"pid={pid}"
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
    real_end = claude_cli.proc_tree.end
    ended: list[bool] = []

    async def slow_end(*args: Any, **kwargs: Any) -> Any:
        await asyncio.sleep(0.5)
        try:
            return await real_end(*args, **kwargs)
        finally:
            ended.append(True)

    monkeypatch.setattr(claude_cli.proc_tree, "end", slow_end)

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
