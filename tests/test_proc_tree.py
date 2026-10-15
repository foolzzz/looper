"""Ending an agent ends every process it started (``looper.agents.proc_tree``).

A stand-in agent starts what a real one leaves behind: a shell command in a
session and process group of its own, as Codex runs each one, with a
grandchild under it, and an ordinary child in the agent's own group. Each
path that stops an agent must leave none of them running.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from fake_claude import fake_claude
from looper.agents import proc_tree
from looper.agents.bus import MessageBus
from looper.agents.claude_cli import ResidentClaude
from looper.agents.tiers.worker import WorkerConfig, WorkerPool
from tests.test_agent_planner_worker import _fake_claude, _start_warm, _work_item, _worker_row
from tests.test_codex_runtime import CODEX, _launch, _resident_command

# What the stand-in runs to start its tree; tree.json names each process once all of them run. With TRAP_TERM
# set, the ordinary child ignores SIGTERM. Like an agent's commands, none of them holds the agent's own output.
_SPAWN_TREE = """\
import json, os, subprocess, sys, time
def _wait_for(name):
    while not (os.path.exists(name) and open(name).read().strip()):
        time.sleep(0.01)
    return open(name).read().strip()
quiet = {'stdin': subprocess.DEVNULL, 'stdout': subprocess.DEVNULL, 'stderr': subprocess.DEVNULL}
command = subprocess.Popen(['/bin/sh', '-c', 'sleep 60 & echo $! > grandchild.pid; wait'], start_new_session=True,
                           **quiet)
if os.environ.get('TRAP_TERM'):
    child = subprocess.Popen(['/bin/sh', '-c', 'trap "" TERM; echo ready > trap.ready; exec sleep 60'], **quiet)
    _wait_for('trap.ready')
else:
    child = subprocess.Popen(['/bin/sleep', '60'], **quiet)
tree = {'agent': os.getpid(), 'command': command.pid, 'grandchild': int(_wait_for('grandchild.pid')),
        'child': child.pid}
json.dump(tree, open('tree.tmp', 'w'))
os.rename('tree.tmp', 'tree.json')
"""


def _running(pids: list[int], *, settle: float = 2.0) -> list[int]:
    """Those of ``pids`` still running (a zombie has exited), after up to ``settle`` s for the rest to go."""
    deadline = time.monotonic() + settle
    while True:
        listed = subprocess.run(["ps", "-A", "-o", "pid=,stat="], capture_output=True, text=True, check=True)
        live = {int(pid) for pid, stat in (line.split()[:2] for line in listed.stdout.splitlines()) if stat[0] != "Z"}
        running = [pid for pid in pids if pid in live]
        if not running or time.monotonic() >= deadline:
            return running
        time.sleep(0.05)


@pytest.fixture
def trees() -> Iterator[list[dict[str, int]]]:
    """The trees the stand-ins started; whatever a failing test leaves of them is killed afterwards."""
    started: list[dict[str, int]] = []
    yield started
    for pid in _running([pid for tree in started for pid in tree.values()], settle=0):
        os.kill(pid, signal.SIGKILL)


@pytest.fixture(autouse=True)
def _short_grace(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(proc_tree, "TERM_GRACE_SEC", 0.5)


async def _started(root: Path, trees: list[dict[str, int]]) -> dict[str, int]:
    """The stand-in's tree once it all runs, checked to be running."""
    for _ in range(500):
        if (root / "tree.json").exists():
            tree = json.loads((root / "tree.json").read_text())
            trees.append(tree)
            assert _running(list(tree.values()), settle=0) == list(tree.values())
            return tree
        await asyncio.sleep(0.01)
    raise AssertionError("the stand-in agent did not start its tree")


async def _agent(root: Path, *, trap_term: bool = False) -> asyncio.subprocess.Process:
    """The stand-in started as Looper starts an agent; it starts its tree and waits on stdin."""
    script = root / "agent.py"
    script.write_text(_SPAWN_TREE + "sys.stdin.read()\n", encoding="utf-8")
    return await asyncio.create_subprocess_exec(
        sys.executable, str(script), cwd=root, stdin=asyncio.subprocess.PIPE, start_new_session=True,
        env={**os.environ, **({"TRAP_TERM": "1"} if trap_term else {})})


def _ended_line(caplog: pytest.LogCaptureFixture) -> str:
    (line,) = [r.getMessage() for r in caplog.records if r.name == proc_tree.__name__ and "ended" in r.getMessage()]
    return line


def test_end_reaches_every_process_the_agent_started(
    tmp_path: Path, trees: list[dict[str, int]], caplog: pytest.LogCaptureFixture,
) -> None:
    async def _run() -> tuple[dict[str, int], int | None]:
        process = await _agent(tmp_path)
        tree = await _started(tmp_path, trees)
        await proc_tree.end(process, label="agent")
        return tree, process.returncode

    caplog.set_level(logging.INFO, logger=proc_tree.__name__)
    tree, returncode = asyncio.run(_run())
    assert _running(list(tree.values())) == []
    assert returncode == -signal.SIGTERM  # ended and reaped
    assert _ended_line(caplog).endswith(": 4 found, 4 exited on SIGTERM, 0 killed")


def test_what_ignores_sigterm_is_killed_after_the_grace_period(
    tmp_path: Path, trees: list[dict[str, int]], caplog: pytest.LogCaptureFixture,
) -> None:
    async def _run() -> tuple[dict[str, int], float]:
        process = await _agent(tmp_path, trap_term=True)
        tree = await _started(tmp_path, trees)
        began = time.monotonic()
        await proc_tree.end(process, label="agent")
        return tree, time.monotonic() - began

    caplog.set_level(logging.INFO, logger=proc_tree.__name__)
    tree, took = asyncio.run(_run())
    assert _running(list(tree.values())) == []
    assert took >= proc_tree.TERM_GRACE_SEC
    assert _ended_line(caplog).endswith(": 4 found, 3 exited on SIGTERM, 1 killed")


# What a Worker's Claude does here: starts its tree, reads the first turn, starts its session and waits.
_LISTENING_AGENT = (
    "if os.environ.get('WARM_UP'):\n    sys.exit()\n"
    + _SPAWN_TREE
    + "sys.stdin.readline()\n"
    "print(json.dumps({'type': 'system', 'subtype': 'init', 'session_id': 's'}), flush=True)\n"
    "time.sleep(60)\n"
)


def test_killing_a_worker_ends_its_tree(tmp_path: Path, agent_db: Any, trees: list[dict[str, int]]) -> None:
    async def _run() -> tuple[dict[str, int], bool]:
        bus = MessageBus(runtime_root=tmp_path)
        pool = WorkerPool(config=WorkerConfig(claude_bin=_fake_claude(tmp_path, _LISTENING_AGENT)),
                          runtime_root=tmp_path)
        await pool.start(bus)
        await asyncio.sleep(0)  # the pool subscribes
        await bus.publish(_work_item(tmp_path))
        tree = await _started(tmp_path, trees)
        killed = await pool.kill("w1")
        await pool.stop()
        await bus.shutdown()
        return tree, killed

    tree, killed = asyncio.run(_run())
    assert killed
    assert _running(list(tree.values())) == []


def test_a_worker_over_its_time_limit_ends_its_tree(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, agent_db: Any, trees: list[dict[str, int]],
) -> None:
    _worker_row(monkeypatch, {"timeout_seconds": 1, "max_turns": 0})
    claude = _fake_claude(tmp_path, _LISTENING_AGENT)
    _start_warm(monkeypatch, claude)

    async def _run() -> tuple[dict[str, int], dict[str, Any]]:
        bus = MessageBus(runtime_root=tmp_path)
        pool = WorkerPool(config=WorkerConfig(claude_bin=claude), runtime_root=tmp_path)
        pool._bus = bus
        run = asyncio.create_task(pool._run_worker(_work_item(tmp_path)))
        tree = await _started(tmp_path, trees)
        await run
        return tree, bus.get_queue("planner-t1").get_nowait().payload

    tree, reply = asyncio.run(_run())
    assert reply["error"] == "Claude ran longer than the 1 s limit (timeout_seconds)"
    assert _running(list(tree.values())) == []


# A resident Claude: answers its first turn once its tree runs, and exits when stdin closes, leaving the tree.
_RESIDENT_AGENT = (
    "import json, sys\n"
    "sys.stdin.readline()\n"
    + _SPAWN_TREE
    + "print(json.dumps({'type': 'system', 'subtype': 'init', 'session_id': 's'}), flush=True)\n"
    "print(json.dumps({'type': 'result', 'subtype': 'success', 'is_error': False, 'result': 'ok'}), flush=True)\n"
    "sys.stdin.read()\n"
)


@pytest.mark.parametrize("kill", [False, True])
def test_stopping_a_resident_claude_ends_its_tree(tmp_path: Path, trees: list[dict[str, int]], kill: bool) -> None:
    claude = fake_claude(tmp_path / "claude", _RESIDENT_AGENT)

    async def _run() -> tuple[dict[str, int], Any]:
        session = ResidentClaude(label="manager")
        await session.start([claude], cwd=str(tmp_path), signature=None)

        async def on_event(event: dict[str, Any]) -> None:
            pass

        turn = await session.run_turn("hello", on_event)
        tree = await _started(tmp_path, trees)
        await session.stop(kill=kill)  # without kill, Claude exits on its own when its stdin closes
        return tree, turn

    tree, turn = asyncio.run(_run())
    assert turn.result_event["result"] == "ok"
    assert _running(list(tree.values())) == []


# A Codex turn that starts its tree and runs on until it is stopped.
_PER_TURN_AGENT = _SPAWN_TREE + "sys.stdin.read()\ntime.sleep(60)\n"


@pytest.mark.parametrize("how", ["cancel", "stop"])
def test_stopping_a_per_turn_session_ends_the_running_turns_tree(
    tmp_path: Path, trees: list[dict[str, int]], how: str,
) -> None:
    codex = fake_claude(tmp_path / "codex", _PER_TURN_AGENT)

    async def _run() -> dict[str, int]:
        session = CODEX.resident_session("planner-task-1")
        command = _resident_command(tmp_path / "runtime", codex, "prompt")
        await session.start(command, cwd=str(tmp_path), signature="s", env=_launch().environment())

        async def on_event(event: dict[str, Any]) -> None:
            pass

        turn = asyncio.create_task(session.run_turn("hello", on_event))
        tree = await _started(tmp_path, trees)
        if how == "cancel":  # as a Planner stops a turn
            turn.cancel()
        else:
            await session.stop()
        await asyncio.gather(turn, return_exceptions=True)
        return tree

    tree = asyncio.run(_run())
    assert _running(list(tree.values())) == []
