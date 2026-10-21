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
