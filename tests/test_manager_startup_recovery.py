"""Replacement cleanup must not consume a chat turn or duplicate its answer."""
from __future__ import annotations

import asyncio

import pytest

from fake_claude import fake_claude
from looper.agents.claude_cli import ResidentClaude
from looper.agents.tiers import manager as module
from looper.agents.tiers.turn_token import TurnToken
from tests.test_manager_new_session import _web
from tests.test_manager_stop import _manager, _replies, _Session


@pytest.fixture
def retry_delays(monkeypatch):
    delays = []
    sleep = asyncio.sleep

    async def fast_delay(seconds):
        if seconds == 5:
            delays.append(seconds)
            seconds = 0
        await sleep(seconds)

    monkeypatch.setattr(module.asyncio, "sleep", fast_delay)
    return delays


def test_replacement_turn_does_not_join_slow_descendant_cleanup(tmp_path, agent_db, monkeypatch):
    command = fake_claude(tmp_path / "claude", """
import json, sys
for line in sys.stdin:
    print(json.dumps({'type': 'result', 'subtype': 'success', 'result': 'ok'}), flush=True)
""")

    async def run():
        session = ResidentClaude(label="manager")
        manager, bus, queue = _manager(tmp_path, session)
        manager._config.claude_bin = command
        await session.start([command], cwd=str(tmp_path), signature="old")
        old = session._process
        tree = session._tree
        original_end = tree.end
        sweeping = asyncio.Event()
        finished = asyncio.Event()

        async def slow_end():
            sweeping.set()
            await asyncio.sleep(20)
            await original_end()
            finished.set()

        monkeypatch.setattr(tree, "end", slow_end)
        try:
            await asyncio.wait_for(manager._handle(_web("hello")), 5)
            assert old.returncode is not None
            assert not finished.is_set()
            await asyncio.wait_for(sweeping.wait(), 15)
            (reply,) = _replies(queue)
            assert reply["text"] == "ok" and reply["outcome"] == "answered"
            stopping = asyncio.create_task(session.stop(kill=True))
            await asyncio.sleep(0.05)
            assert not stopping.done()
            await asyncio.wait_for(stopping, 25)
            assert finished.is_set() and not session._ending
        finally:
            await session.stop(kill=True)
            await bus.shutdown()

    asyncio.run(run())


def test_unexited_main_process_blocks_replacement_and_retry(tmp_path, agent_db, monkeypatch, retry_delays):
    command = fake_claude(tmp_path / "claude", "import sys, time\nsys.stdin.read()\ntime.sleep(60)\n")
    keep = TurnToken.keep
    budgets = []

    async def bounded(self, name, aw, timeout, **kwargs):
        if name == "claude_stop":
            budgets.append(timeout)
            assert timeout == 30
            timeout = 0.05  # exercise the deadline without waiting two real 30-second budgets
        return await keep(self, name, aw, timeout, **kwargs)

    async def run():
        session = ResidentClaude(label="manager")
        manager, bus, queue = _manager(tmp_path, session)
        await session.start([command], cwd=str(tmp_path), signature="old")
        process, tree = session._process, session._tree
        kill, end = process.kill, tree.end
        release = asyncio.Event()

        async def blocked_end():
            await release.wait()
            await end()

        monkeypatch.setattr(process, "kill", lambda: None)  # simulate an uninterruptible main process
        monkeypatch.setattr(tree, "end", blocked_end)
        monkeypatch.setattr(TurnToken, "keep", bounded)
        try:
            await asyncio.wait_for(manager._handle(_web("hello")), 3)
            (reply,) = _replies(queue)
            assert reply["outcome"] == "failed"
            assert budgets == [30, 30] and retry_delays == [5]
            assert not session.started and process.returncode is None
        finally:
            kill()
            release.set()
            await session.stop(kill=True)
            await bus.shutdown()

    asyncio.run(run())


@pytest.mark.parametrize("step", ["claude_stop", "claude_start", "switch_session"])
@pytest.mark.parametrize("always_fail", [False, True])
def test_startup_failure_retries_once_before_one_reply(
    step, always_fail, tmp_path, agent_db, monkeypatch, retry_delays, caplog,
):
    async def run():
        session = _Session(hold=False)
        manager, bus, queue = _manager(tmp_path, session)
        calls = []
        if step == "claude_stop":
            session.started = True
            session.stale = lambda signature: session.started
        elif step == "switch_session":
            manager._session_loaded = True
            manager._session_id, manager._session_runtime = "old-session", "omp"
        original = {"claude_stop": session.stop, "claude_start": session.start,
                    "switch_session": manager._switch_session}[step]

        async def flaky(*args, **kwargs):
            calls.append(1)
            if len(calls) == 1 or always_fail:
                if step == "claude_start":
                    raise OSError("launch unavailable")
                raise TimeoutError("startup timed out")
            return await original(*args, **kwargs)

        owner = manager if step == "switch_session" else session
        attr = {"claude_stop": "stop", "claude_start": "start", "switch_session": "_switch_session"}[step]
        monkeypatch.setattr(owner, attr, flaky)
        try:
            await asyncio.wait_for(manager._handle(_web("hello")), 3)
            (reply,) = _replies(queue)
            assert reply["outcome"] == ("failed" if always_fail else "answered")
            assert len(session.turns) == (0 if always_fail else 1)
            assert len(calls) == 2 and retry_delays == [5]
            assert step in caplog.text and "retrying before message delivery" in caplog.text
        finally:
            await bus.shutdown()

    asyncio.run(run())


def test_timeout_after_delivery_is_never_retried(tmp_path, agent_db, retry_delays):
    async def fail():
        raise TimeoutError("response stalled")

    async def run():
        session = _Session(hold=False, on_turn=fail)
        manager, bus, queue = _manager(tmp_path, session)
        try:
            await asyncio.wait_for(manager._handle(_web("hello")), 3)
            (reply,) = _replies(queue)
            assert reply["outcome"] == "failed"
            assert len(session.turns) == 1 and retry_delays == []
        finally:
            await bus.shutdown()

    asyncio.run(run())


def test_retry_during_preparation_keeps_the_turn_lock_and_message(
    tmp_path, agent_db, monkeypatch, retry_delays,
):
    async def run():
        session = _Session(hold=False)
        manager, bus, queue = _manager(tmp_path, session)
        prepare = manager._prepare_turn
        calls = []

        async def fail():
            raise OSError("restore unavailable")

        async def restoring(token, envelope, **kwargs):
            prepared = await prepare(token, envelope, **kwargs)
            calls.append(prepared)
            if len(calls) == 1:
                await token.keep("claude_start", fail(), 10)
            return prepared

        monkeypatch.setattr(manager, "_prepare_turn", restoring)
        try:
            await asyncio.wait_for(manager._handle(_web("hello")), 3)
            (reply,) = _replies(queue)
            assert reply["outcome"] == "answered"
            assert len(calls) == 2 and calls[0].turn is calls[1].turn
            assert len(session.turns) == 1 and retry_delays == [5]
            assert not manager._turn_lock.locked()
        finally:
            await bus.shutdown()

    asyncio.run(run())
