"""Ending an agent ends every process it started (``looper.agents.proc_tree``).

A stand-in agent starts what a real one leaves behind: a shell command in a
session and process group of its own, as Codex runs each one, with a
grandchild under it, and an ordinary child in the agent's own group. Each
path that stops an agent must leave none of them running.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Awaitable, Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from fake_claude import fake_claude
from looper.agents import proc_tree
from looper.agents.bus import MessageBus
from looper.agents.claude_cli import ResidentClaude
from looper.agents.runtimes import per_turn
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


# --- The sweep at the next start ---


@pytest.fixture
def runtime_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A runtime root to sweep; recording, which a sweep turns on, is off again afterwards."""
    monkeypatch.setattr(proc_tree, "_records", None)
    monkeypatch.setattr(proc_tree, "_server", None)
    monkeypatch.setattr(proc_tree, "_recorded", {})
    monkeypatch.setattr(proc_tree, "_ending", set())
    return tmp_path / "runtime"


def _exited() -> tuple[int, str]:
    """The pid and start time of a process that has exited since."""
    process = subprocess.Popen(["/bin/sleep", "0.2"])
    started = proc_tree._start_time(process.pid)
    process.wait()
    assert started is not None
    return process.pid, started


def _record(root: Path, pid: int, started: str, server: tuple[int, str]) -> Path:
    directory = root / proc_tree.RECORDS_DIR
    directory.mkdir(parents=True, exist_ok=True)
    return proc_tree._write_record(directory, pid, started, server)


def test_the_next_start_ends_the_tree_of_an_agent_whose_server_is_gone(
    tmp_path: Path, runtime_root: Path, trees: list[dict[str, int]], caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    script = tmp_path / "agent.py"
    script.write_text(_SPAWN_TREE + "sys.stdin.read()\n", encoding="utf-8")

    async def _run() -> tuple[dict[str, int], Path]:
        await proc_tree.sweep(runtime_root)  # this server records its agents from here on
        process = await proc_tree.spawn(sys.executable, str(script), cwd=tmp_path, stdin=asyncio.subprocess.PIPE)
        tree = await _started(tmp_path, trees)
        record = runtime_root / proc_tree.RECORDS_DIR / f"{process.pid}.json"
        # The server that started it crashed: the next one finds the agent still running.
        saved = json.loads(record.read_text())
        assert (saved["pid"], saved["started"]) == (process.pid, proc_tree._start_time(process.pid))
        _record(runtime_root, process.pid, saved["started"], _exited())
        monkeypatch.setattr(proc_tree, "_recorded", {})  # a new server has no in-memory records of the old one's agents
        await proc_tree.sweep(runtime_root)
        await asyncio.wait_for(process.wait(), 10)
        return tree, record

    caplog.set_level(logging.INFO, logger=proc_tree.__name__)
    tree, record = asyncio.run(_run())
    assert _running(list(tree.values())) == []
    assert not record.exists()
    assert _ended_line(caplog).endswith(": 4 found, 4 exited on SIGTERM, 0 killed")


@pytest.mark.parametrize("server", ["gone", "running"])
def test_the_sweep_leaves_a_reused_pid_and_another_running_servers_agent_alone(
    runtime_root: Path, server: str,
) -> None:
    # A live process recorded with another start time is not the agent the record names: its pid was reused.
    reused = subprocess.Popen(["/bin/sleep", "30"], start_new_session=True)
    other_server = subprocess.Popen(["/bin/sleep", "30"])
    agent = subprocess.Popen(["/bin/sleep", "30"], start_new_session=True)
    try:
        owner = _exited() if server == "gone" else (other_server.pid, proc_tree._start_time(other_server.pid))
        records = [_record(runtime_root, reused.pid, "Thu Jan 1 00:00:00 1970", owner),
                   _record(runtime_root, agent.pid, str(proc_tree._start_time(agent.pid)), owner)]
        asyncio.run(proc_tree.sweep(runtime_root))
        assert _running([reused.pid], settle=0) == [reused.pid]
        if server == "running":  # a server still running on this runtime root keeps its agents and records
            assert _running([agent.pid], settle=0) == [agent.pid]
            assert all(record.exists() for record in records)
        else:
            assert _running([agent.pid]) == []
            assert not any(record.exists() for record in records)
    finally:
        for process in (reused, other_server, agent):
            process.kill()
            process.wait()


def test_the_sweep_drops_records_of_agents_that_exited(runtime_root: Path) -> None:
    pid, started = _exited()
    record = _record(runtime_root, pid, started, _exited())
    asyncio.run(proc_tree.sweep(runtime_root))
    assert not record.exists()


def test_the_sweep_leaves_a_reused_pid_in_a_recorded_tree_alone(runtime_root: Path) -> None:
    reused = subprocess.Popen(["/bin/sleep", "30"], start_new_session=True)
    try:
        pid, started = _exited()
        record = _record(runtime_root, pid, started, _exited())
        saved = json.loads(record.read_text())
        saved["tree"] = [{"pid": reused.pid, "started": "Thu Jan 1 00:00:00 1970"}]
        record.write_text(json.dumps(saved))
        asyncio.run(proc_tree.sweep(runtime_root))
        assert _running([reused.pid], settle=0) == [reused.pid]
        assert not record.exists()
    finally:
        reused.kill()
        reused.wait()


def test_an_end_cut_short_leaves_the_rest_of_the_tree_for_the_next_start(
    tmp_path: Path, runtime_root: Path, trees: list[dict[str, int]], monkeypatch: pytest.MonkeyPatch,
) -> None:
    script = tmp_path / "agent.py"
    script.write_text(_SPAWN_TREE + "sys.stdin.read()\n", encoding="utf-8")

    async def _run() -> None:
        await proc_tree.sweep(runtime_root)
        process = await proc_tree.spawn(
            sys.executable, str(script), cwd=tmp_path, stdin=asyncio.subprocess.PIPE,
            env={**os.environ, "TRAP_TERM": "1"})
        tree = await _started(tmp_path, trees)
        record = runtime_root / proc_tree.RECORDS_DIR / f"{process.pid}.json"
        monkeypatch.setattr(proc_tree, "TERM_GRACE_SEC", 10)
        ending = asyncio.create_task(proc_tree.end(process, label="agent"))
        try:
            await asyncio.wait_for(process.wait(), 3)
            await asyncio.sleep(0.05)  # the reaper has had time to forget the agent
            assert record.exists()
            saved = json.loads(record.read_text())
            assert tree["child"] in {entry["pid"] for entry in saved["tree"]}
            saved["server_pid"], saved["server_started"] = _exited()
            record.write_text(json.dumps(saved))
            monkeypatch.setattr(proc_tree, "TERM_GRACE_SEC", 0.5)
            await proc_tree.sweep(runtime_root)
            assert _running([tree["child"]], settle=3) == []
            assert not record.exists()
        finally:
            ending.cancel()
            await asyncio.gather(ending, return_exceptions=True)

    asyncio.run(_run())


def test_a_graceful_stop_records_the_tree_before_the_agent_exits(
    tmp_path: Path, runtime_root: Path, trees: list[dict[str, int]], monkeypatch: pytest.MonkeyPatch,
) -> None:
    claude = fake_claude(tmp_path / "claude", _RESIDENT_AGENT)

    async def _run() -> None:
        await proc_tree.sweep(runtime_root)
        session = ResidentClaude(label="manager")
        await session.start([claude], cwd=str(tmp_path), signature=None, env={**os.environ, "TRAP_TERM": "1"})

        async def on_event(event: dict[str, Any]) -> None:
            pass

        await session.run_turn("hello", on_event)
        tree = await _started(tmp_path, trees)
        record = runtime_root / proc_tree.RECORDS_DIR / f"{session.pid}.json"
        monkeypatch.setattr(proc_tree, "TERM_GRACE_SEC", 10)
        stopping = asyncio.create_task(session.stop())
        try:
            for _ in range(300):
                if not _running([tree["agent"]], settle=0):
                    break
                await asyncio.sleep(0.01)
            assert _running([tree["agent"]], settle=0) == []
            await asyncio.sleep(0.05)  # the reaper has had time to forget the agent
            assert record.exists()
            saved = json.loads(record.read_text())
            assert tree["child"] in {entry["pid"] for entry in saved["tree"]}
            saved["server_pid"], saved["server_started"] = _exited()
            record.write_text(json.dumps(saved))
            monkeypatch.setattr(proc_tree, "TERM_GRACE_SEC", 0.5)
            await proc_tree.sweep(runtime_root)
            assert _running([tree["child"]], settle=3) == []
            assert not record.exists()
        finally:
            stopping.cancel()
            await asyncio.gather(stopping, return_exceptions=True)

    asyncio.run(_run())


def test_the_sweep_keeps_the_record_until_its_process_has_ended(runtime_root: Path) -> None:
    process = subprocess.Popen(["/bin/sh", "-c", 'trap "" TERM; exec sleep 30'], start_new_session=True)
    try:
        time.sleep(0.1)  # the shell has installed its trap
        started = proc_tree._start_time(process.pid)
        assert started is not None
        record = _record(runtime_root, process.pid, started, _exited())

        async def _run() -> None:
            sweeping = asyncio.create_task(proc_tree.sweep(runtime_root))
            try:
                await asyncio.sleep(0.2)
                assert not sweeping.done()
                assert record.exists()
                await sweeping
                assert not record.exists()
                assert _running([process.pid]) == []
            finally:
                sweeping.cancel()
                await asyncio.gather(sweeping, return_exceptions=True)

        asyncio.run(_run())
    finally:
        process.kill()
        process.wait()


def test_the_sweep_keeps_a_tree_when_its_start_times_cannot_be_read(
    runtime_root: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = subprocess.Popen(["/bin/sleep", "30"], start_new_session=True)
    try:
        started = proc_tree._start_time(process.pid)
        assert started is not None
        pid, agent_started = _exited()
        record = _record(runtime_root, pid, agent_started, _exited())
        saved = json.loads(record.read_text())
        saved["tree"] = [{"pid": process.pid, "started": started}]
        record.write_text(json.dumps(saved))
        monkeypatch.setattr(proc_tree, "_start_times", lambda pids: None)
        asyncio.run(proc_tree.sweep(runtime_root))
        assert record.exists()
        assert json.loads(record.read_text()) == saved
        assert _running([process.pid], settle=0) == [process.pid]
    finally:
        process.kill()
        process.wait()


def test_ending_an_orphan_rechecks_its_start_time() -> None:
    process = subprocess.Popen(["/bin/sleep", "30"], start_new_session=True)
    try:
        asyncio.run(proc_tree._end(
            proc_tree._Orphan(process.pid, started="Thu Jan 1 00:00:00 1970"), label="x", known={}))
        assert _running([process.pid], settle=0) == [process.pid]
    finally:
        process.kill()
        process.wait()


def test_a_record_lists_the_tree_only_while_an_end_is_ending_it(
    tmp_path: Path, runtime_root: Path, trees: list[dict[str, int]],
) -> None:
    script = tmp_path / "agent.py"
    script.write_text(_SPAWN_TREE + "sys.stdin.read()\n", encoding="utf-8")

    async def _run() -> None:
        await proc_tree.sweep(runtime_root)
        process = await proc_tree.spawn(sys.executable, str(script), cwd=tmp_path, stdin=asyncio.subprocess.PIPE)
        await _started(tmp_path, trees)
        record = runtime_root / proc_tree.RECORDS_DIR / f"{process.pid}.json"
        assert "tree" not in json.loads(record.read_text())
        await proc_tree.end(process, label="agent")
        assert not record.exists()

    asyncio.run(_run())


def test_a_reaped_agents_record_is_removed(tmp_path: Path, runtime_root: Path) -> None:
    async def _run() -> tuple[bool, bool]:
        await proc_tree.sweep(runtime_root)
        process = await proc_tree.spawn("/bin/sleep", "0.2")
        record = runtime_root / proc_tree.RECORDS_DIR / f"{process.pid}.json"
        recorded = record.exists()
        await process.wait()
        await asyncio.sleep(0.05)  # the record is removed once the process is reaped
        return recorded, record.exists()

    assert asyncio.run(_run()) == (True, False)


# --- TurnTree: a turn's tree snapshotted at spawn, refreshed, ended from the snapshot ---

_DETACHED_CHILD = (
    "import os, subprocess, sys\n"
    "g = subprocess.Popen(['/bin/sleep', '60'], start_new_session=True)\n"
    "open('grandchild.pid', 'w').write(str(g.pid))\n"
    "import time\nwhile not os.path.exists('release'):\n    time.sleep(0.01)\n"
)


async def _spawn_python(root: Path, code: str) -> asyncio.subprocess.Process:
    return await asyncio.create_subprocess_exec(
        sys.executable, "-c", code, cwd=root, stdin=asyncio.subprocess.PIPE, start_new_session=True)


async def _wait_file(path: Path) -> str:
    for _ in range(500):
        if path.exists() and path.read_text().strip():
            return path.read_text().strip()
        await asyncio.sleep(0.01)
    raise AssertionError(f"{path} was not written")


def test_turn_tree_ends_a_detached_grandchild_whose_parent_is_gone(tmp_path: Path) -> None:
    code = ("import subprocess, sys, time\n"
            "child = subprocess.Popen([sys.executable, '-c', " + repr(_DETACHED_CHILD) + "])\n"
            "child.wait()\nsys.stdin.read()\n")

    async def _run() -> tuple[int, bool, list[int]]:
        process = await _spawn_python(tmp_path, code)
        tree = proc_tree.TurnTree.track(process, label="turn")
        grandchild = int(await _wait_file(tmp_path / "grandchild.pid"))
        await tree.refresh(force=True)  # the middle child still runs
        seen = grandchild in tree.seen
        (tmp_path / "release").write_text("go")
        await asyncio.sleep(0.3)  # the middle child has exited; the grandchild is re-parented
        try:
            await tree.end()
        finally:
            for pid in _running([grandchild], settle=0):
                os.kill(pid, signal.SIGKILL)
        return grandchild, seen, _running([grandchild])

    grandchild, seen, left = asyncio.run(_run())
    assert seen
    assert left == []


def test_turn_tree_leaves_a_reused_pid_alone(tmp_path: Path) -> None:
    async def _run() -> list[int]:
        process = await _spawn_python(tmp_path, "import subprocess, time\n"
                                      "subprocess.Popen(['/bin/sleep', '60'], start_new_session=True)\nimport sys; sys.stdin.read()\n")
        tree = proc_tree.TurnTree.track(process, label="turn")
        for _ in range(100):
            await tree.refresh(force=True)
            if len(tree.seen) > 1:
                break
            await asyncio.sleep(0.05)
        (child,) = [pid for pid in tree.seen if pid != process.pid]
        tree.seen[child] = "Thu Jan 1 00:00:00 1970"
        try:
            await tree.end()
            return _running([child], settle=0)
        finally:
            for pid in _running([child], settle=0):
                os.kill(pid, signal.SIGKILL)

    assert len(asyncio.run(_run())) == 1


def test_turn_tree_end_new_leaves_the_agent_and_earlier_children(tmp_path: Path) -> None:
    code = ("import subprocess, sys\n"
            "old = subprocess.Popen(['/bin/sleep', '60'])\nprint('go', flush=True)\n"
            "sys.stdin.readline()\n"
            "new = subprocess.Popen(['/bin/sleep', '60'])\nopen('new.pid', 'w').write(str(new.pid))\n"
            "print(old.pid, flush=True)\nsys.stdin.read()\n")

    async def _run() -> tuple[list[int], list[int]]:
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-c", code, cwd=tmp_path, stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, start_new_session=True)
        assert process.stdin and process.stdout
        await process.stdout.readline()
        tree = proc_tree.TurnTree.track(process, label="turn")
        await tree.baseline()
        process.stdin.write(b"\n")
        await process.stdin.drain()
        old = int((await process.stdout.readline()).decode())
        new = int(await _wait_file(tmp_path / "new.pid"))
        try:
            await tree.end_new()
            return _running([new]), _running([process.pid, old], settle=0)
        finally:
            for pid in _running([process.pid, old, new], settle=0):
                os.kill(pid, signal.SIGKILL)
            await process.wait()

    new_left, survivors = asyncio.run(_run())
    assert new_left == []
    assert len(survivors) == 2


def test_turn_tree_ticker_picks_up_a_child_started_between_events(tmp_path: Path) -> None:
    code = ("import subprocess, sys\nsys.stdin.readline()\n"
            "c = subprocess.Popen(['/bin/sleep', '60'])\nopen('c.pid', 'w').write(str(c.pid))\nsys.stdin.read()\n")

    async def _run() -> bool:
        process = await _spawn_python(tmp_path, code)
        tree = proc_tree.TurnTree.track(process, label="turn")
        tree.start_ticker(interval=0.05)
        process.stdin.write(b"\n")  # type: ignore[union-attr]
        child = int(await _wait_file(tmp_path / "c.pid"))
        try:
            for _ in range(100):
                if child in tree.seen:
                    return True
                await asyncio.sleep(0.05)
            return False
        finally:
            await tree.stop_ticker()
            await tree.end()
            for pid in _running([child], settle=0):
                os.kill(pid, signal.SIGKILL)

    assert asyncio.run(_run())


def test_turn_tree_end_returns_within_a_bound_when_the_agent_is_never_reaped() -> None:
    sleeper = subprocess.Popen(["/bin/sleep", "60"], start_new_session=True)

    class _Stuck:
        pid = sleeper.pid
        returncode = None

        def send_signal(self, sig: int) -> None:
            sleeper.send_signal(sig)

        async def wait(self) -> int:
            await asyncio.Event().wait()
            return 0

    async def _run() -> float:
        tree = proc_tree.TurnTree.track(_Stuck(), label="turn")
        began = time.monotonic()
        await tree.end()
        return time.monotonic() - began

    try:
        took = asyncio.run(_run())
    finally:
        sleeper.kill()
        sleeper.wait()
    assert took < proc_tree.REAP_SEC + proc_tree.TERM_GRACE_SEC + 1


# A child of the agent's group, seen by a refresh, that then leaves the group (setsid) while its parent exits:
# nothing but the snapshot (its pid and start time) links it to the turn any more.
_LEAVES_THE_GROUP = (
    "import os, time\n"
    "open('child.pid', 'w').write(str(os.getpid()))\n"
    "while not os.path.exists('setsid'):\n    time.sleep(0.01)\n"
    "os.setsid()\nopen('moved', 'w').write('1')\ntime.sleep(60)\n"
)
_MIDDLE = (
    "import os, subprocess, sys, time\n"
    "subprocess.Popen([sys.executable, '-c', " + repr(_LEAVES_THE_GROUP) + "])\n"
    "while not os.path.exists('moved'):\n    time.sleep(0.01)\n"
)


async def _child_that_leaves_its_group(tmp_path: Path, tree: proc_tree.TurnTree) -> int:
    child = int(await _wait_file(tmp_path / "child.pid"))
    await tree.refresh(force=True)  # seen while still in the agent's group
    assert child in tree.seen and tree.seen[child] is not None
    (tmp_path / "setsid").write_text("go")
    await _wait_file(tmp_path / "moved")
    await asyncio.sleep(0.3)  # the middle process has exited: the child is re-parented, in a group of its own
    return child


def test_turn_tree_end_ends_a_seen_child_that_left_the_group(tmp_path: Path) -> None:
    code = ("import subprocess, sys\n"
            "subprocess.Popen([sys.executable, '-c', " + repr(_MIDDLE) + "]).wait()\nsys.stdin.read()\n")

    async def _run() -> list[int]:
        process = await _spawn_python(tmp_path, code)
        tree = proc_tree.TurnTree.track(process, label="turn")
        child = await _child_that_leaves_its_group(tmp_path, tree)
        try:
            await tree.end()
            return _running([child])
        finally:
            for pid in _running([child], settle=0):
                os.kill(pid, signal.SIGKILL)

    assert asyncio.run(_run()) == []


def test_turn_tree_end_new_ends_a_seen_child_that_left_the_group(tmp_path: Path) -> None:
    code = ("import subprocess, sys\nsys.stdin.readline()\n"
            "subprocess.Popen([sys.executable, '-c', " + repr(_MIDDLE) + "]).wait()\nsys.stdin.read()\n")

    async def _run() -> tuple[list[int], list[int]]:
        process = await _spawn_python(tmp_path, code)
        tree = proc_tree.TurnTree.track(process, label="turn")
        await tree.baseline()
        process.stdin.write(b"\n")  # type: ignore[union-attr]
        child = await _child_that_leaves_its_group(tmp_path, tree)
        try:
            await tree.end_new()
            return _running([child]), _running([process.pid], settle=0)
        finally:
            for pid in _running([child, process.pid], settle=0):
                os.kill(pid, signal.SIGKILL)
            await process.wait()

    child_left, agent = asyncio.run(_run())
    assert child_left == []
    assert len(agent) == 1  # the agent survives end_new


# A background command that leaves the agent's group at once (setsid) while its parent exits, before any refresh
# could see it: only the turn's ownership of what the agent starts (fork lineage on macOS, an inherited marker on
# Linux) still links it to the turn.
_LEAVES_AT_ONCE = (
    "import os, time\nos.setsid()\n"
    "open('child.pid', 'w').write(str(os.getpid()))\ntime.sleep(60)\n"
)
_QUICK_MIDDLE = "import subprocess, sys\nsubprocess.Popen([sys.executable, '-c', " + repr(_LEAVES_AT_ONCE) + "])\n"


async def _unseen_child_that_left_at_once(tmp_path: Path, tree: proc_tree.TurnTree) -> int:
    child = int(await _wait_file(tmp_path / "child.pid"))
    await asyncio.sleep(0.3)  # the middle process has exited: the child is re-parented, in a session of its own
    assert child not in tree.seen
    return child


@pytest.mark.skipif(not proc_tree.FOLLOWS_OWNERSHIP, reason="no ownership facility on this platform")
def test_turn_tree_end_ends_an_unseen_child_that_left_the_group_at_once(tmp_path: Path) -> None:
    code = ("import subprocess, sys\nsys.stdin.readline()\n"
            "subprocess.Popen([sys.executable, '-c', " + repr(_QUICK_MIDDLE) + "]).wait()\nsys.stdin.read()\n")

    async def _run() -> list[int]:
        process = await proc_tree.spawn(sys.executable, "-c", code, cwd=tmp_path, stdin=asyncio.subprocess.PIPE)
        tree = proc_tree.TurnTree.track(process, label="turn")
        process.stdin.write(b"\n")  # type: ignore[union-attr]
        child = await _unseen_child_that_left_at_once(tmp_path, tree)
        try:
            await tree.end()
            return _running([child])
        finally:
            for pid in _running([child], settle=0):
                os.kill(pid, signal.SIGKILL)

    assert asyncio.run(_run()) == []


@pytest.mark.skipif(not proc_tree.FOLLOWS_OWNERSHIP, reason="no ownership facility on this platform")
def test_turn_tree_end_new_ends_an_unseen_child_that_left_at_once_and_spares_an_earlier_one(tmp_path: Path) -> None:
    # The earlier turn's background command left the same way and is still running at the baseline: it is not
    # this turn's to end.
    code = ("import os, subprocess, sys, time\n"
            "subprocess.Popen([sys.executable, '-c', " + repr(_QUICK_MIDDLE) + "]).wait()\n"
            "while not os.path.exists('child.pid'):\n    time.sleep(0.01)\n"
            "os.rename('child.pid', 'earlier.pid')\nprint('go', flush=True)\nsys.stdin.readline()\n"
            "subprocess.Popen([sys.executable, '-c', " + repr(_QUICK_MIDDLE) + "]).wait()\nsys.stdin.read()\n")

    async def _run() -> tuple[list[int], list[int]]:
        process = await proc_tree.spawn(sys.executable, "-c", code, cwd=tmp_path,
                                        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE)
        tree = proc_tree.TurnTree.track(process, label="turn")
        assert process.stdin and process.stdout
        await process.stdout.readline()
        earlier = int((tmp_path / "earlier.pid").read_text())
        await asyncio.sleep(0.3)
        await tree.baseline()
        process.stdin.write(b"\n")
        await process.stdin.drain()
        child = await _unseen_child_that_left_at_once(tmp_path, tree)
        try:
            await tree.end_new()
            return _running([child]), _running([process.pid, earlier], settle=0)
        finally:
            for pid in _running([child, earlier], settle=0):
                os.kill(pid, signal.SIGKILL)
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)  # the agent's group
            await process.wait()

    child_left, survivors = asyncio.run(_run())
    assert child_left == []
    assert len(survivors) == 2


@pytest.mark.skipif(not proc_tree.FOLLOWS_OWNERSHIP, reason="no ownership facility on this platform")
def test_turn_tree_end_ends_an_unseen_child_two_quick_shells_down(tmp_path: Path) -> None:
    # As a tool runs `(cmd &)`: the tool's shell starts a subshell, which starts the command and exits at once.
    shells = "import subprocess, sys\nsubprocess.Popen([sys.executable, '-c', " + repr(_QUICK_MIDDLE) + "]).wait()\n"
    code = ("import subprocess, sys\nsys.stdin.readline()\n"
            "subprocess.Popen([sys.executable, '-c', " + repr(shells) + "]).wait()\nsys.stdin.read()\n")

    async def _run() -> list[int]:
        process = await proc_tree.spawn(sys.executable, "-c", code, cwd=tmp_path, stdin=asyncio.subprocess.PIPE)
        tree = proc_tree.TurnTree.track(process, label="turn")
        process.stdin.write(b"\n")  # type: ignore[union-attr]
        child = await _unseen_child_that_left_at_once(tmp_path, tree)
        try:
            await tree.end()
            return _running([child])
        finally:
            for pid in _running([child], settle=0):
                os.kill(pid, signal.SIGKILL)

    assert asyncio.run(_run()) == []


@pytest.mark.skipif(not proc_tree._MARKS, reason="no environment mark on this platform")
def test_turn_tree_end_ends_a_child_that_left_at_once_without_any_lineage_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The fork watch can lose a short-lived shell before its children are listed; the environment mark the agent
    # passes down does not depend on any ancestor still running.
    monkeypatch.setattr(proc_tree, "_LINEAGE", False)
    code = ("import subprocess, sys\nsys.stdin.readline()\n"
            "subprocess.Popen([sys.executable, '-c', " + repr(_QUICK_MIDDLE) + "]).wait()\nsys.stdin.read()\n")

    async def _run() -> list[int]:
        process = await proc_tree.spawn(sys.executable, "-c", code, cwd=tmp_path, stdin=asyncio.subprocess.PIPE)
        tree = proc_tree.TurnTree.track(process, label="turn")
        process.stdin.write(b"\n")  # type: ignore[union-attr]
        child = await _unseen_child_that_left_at_once(tmp_path, tree)
        try:
            await tree.end()
            return _running([child])
        finally:
            for pid in _running([child], settle=0):
                os.kill(pid, signal.SIGKILL)

    assert asyncio.run(_run()) == []


# Two quick forks, the second leaving the session (setsid) and running a system binary: the fork watch can lose both
# middle processes before their children are listed. The environment mark the agent passes down (Linux) depends on
# neither; on macOS ending such a daemon is best effort, as Ctrl-C in a terminal leaves one running.
_DETACHED_BEST_EFFORT = "macOS: ending a detached double-fork daemon is best effort (chat-stop-v2 RFC 6.6)"
_DOUBLE_FORK_EXEC = (
    "import os\n"
    "if os.fork() == 0:\n"
    "    if os.fork() == 0:\n"
    "        os.setsid()\n"
    "        open('child.pid', 'w').write(str(os.getpid()))\n"
    "        os.execv('/bin/sleep', ['/bin/sleep', '60'])\n"
    "    os._exit(0)\n"
    "os.wait()\n"
)


@pytest.mark.skipif(not proc_tree._MARKS, reason=_DETACHED_BEST_EFFORT)
def test_turn_tree_end_ends_a_double_forked_system_binary_without_any_lineage_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(proc_tree._Lineage, "_follow", lambda self: None)  # the fork watch lost every middle process
    code = "import sys\nsys.stdin.readline()\n" + _DOUBLE_FORK_EXEC + "sys.stdin.read()\n"

    async def _run() -> list[int]:
        process = await proc_tree.spawn(sys.executable, "-c", code, cwd=tmp_path, stdin=asyncio.subprocess.PIPE)
        tree = proc_tree.TurnTree.track(process, label="turn")
        process.stdin.write(b"\n")  # type: ignore[union-attr]
        child = await _unseen_child_that_left_at_once(tmp_path, tree)
        try:
            await tree.end()
            return _running([child])
        finally:
            for pid in _running([child], settle=0):
                os.kill(pid, signal.SIGKILL)

    assert asyncio.run(_run()) == []


@pytest.mark.skipif(not proc_tree.FOLLOWS_OWNERSHIP, reason="no ownership facility on this platform")
def test_turn_tree_end_ends_what_the_agent_starts_in_a_session_of_its_own_as_it_exits(tmp_path: Path) -> None:
    # Told to exit, the agent starts a background command that leaves its group at once: it did not run when the end
    # began, yet the end goes on looking for what the turn owns until it has finished. (Started ignoring SIGTERM, the
    # shell in between lives to start it.)
    code = ("import os, signal, subprocess, sys, time\n"
            "def on_term(*_):\n"
            "    signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            "    subprocess.Popen([sys.executable, '-c', " + repr(_QUICK_MIDDLE) + "]).wait()\n"
            "    while not os.path.exists('child.pid'):\n        time.sleep(0.01)\n"
            "    time.sleep(0.3)\n"
            "    sys.exit(0)\n"
            "signal.signal(signal.SIGTERM, on_term)\n"
            "print('ready', flush=True)\nsys.stdin.read()\n")

    async def _run() -> list[int]:
        process = await proc_tree.spawn(sys.executable, "-c", code, cwd=tmp_path,
                                        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE)
        tree = proc_tree.TurnTree.track(process, label="turn")
        assert process.stdout
        await process.stdout.readline()
        try:
            await tree.end()
            child = int((tmp_path / "child.pid").read_text())
            return _running([child])
        finally:
            if (tmp_path / "child.pid").exists():
                for pid in _running([int((tmp_path / "child.pid").read_text())], settle=0):
                    os.kill(pid, signal.SIGKILL)

    assert asyncio.run(_run()) == []


@pytest.mark.skipif(not proc_tree.FOLLOWS_OWNERSHIP, reason="no ownership facility on this platform")
def test_turn_tree_end_ends_what_the_agent_owns_that_started_after_the_ends_last_listing(tmp_path: Path) -> None:
    # Told to exit, the agent double-forks a command into a session of its own only once the end has listed every
    # process, then exits: the ownership listing that follows finds it, though the listing before did not.
    code = ("import os, signal, sys, time\n"
            "def on_term(*_):\n"
            "    signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            "    while not os.path.exists('go'):\n        time.sleep(0.01)\n"
            + "".join("    " + line + "\n" for line in _DOUBLE_FORK_EXEC.splitlines()[1:])  # (os is imported)
            + "    while not os.path.exists('child.pid'):\n        time.sleep(0.01)\n"
            "    os._exit(0)\n"
            "signal.signal(signal.SIGTERM, on_term)\n"
            "print('ready', flush=True)\nsys.stdin.read()\n")

    async def _run() -> list[int]:
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-c", code, cwd=tmp_path, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            start_new_session=True)
        tree = proc_tree.TurnTree.track(process, label="turn")
        assert process.stdout
        await process.stdout.readline()
        agent_ended = threading.Event()

        class _Ended(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                if f"ended the process tree of pid {process.pid}:" in record.getMessage():
                    agent_ended.set()

        live_owned, calls = tree._live_owned, []

        def listing_after_the_snapshot() -> tuple[set[int], bool]:
            # The first call comes before any signal; the next follows the end's first listing of every process.
            calls.append(None)
            if len(calls) == 2:
                (tmp_path / "go").write_text("1")
                deadline = time.monotonic() + 10
                while not ((tmp_path / "child.pid").exists() and agent_ended.is_set()) \
                        and time.monotonic() < deadline:
                    time.sleep(0.01)
            return live_owned()

        tree._live_owned = listing_after_the_snapshot  # type: ignore[method-assign]
        handler, logger = _Ended(), logging.getLogger(proc_tree.__name__)
        level = logger.level
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        try:
            await tree.end()
            child = int(await _wait_file(tmp_path / "child.pid"))
            return _running([child])
        finally:
            logger.removeHandler(handler)
            logger.setLevel(level)
            if (tmp_path / "child.pid").exists():
                for pid in _running([int((tmp_path / "child.pid").read_text())], settle=0):
                    os.kill(pid, signal.SIGKILL)

    assert asyncio.run(_run()) == []


_LEAVES_ONCE_TOLD = (
    "import os, signal, time\n"
    "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
    "open('child.pid', 'w').write(str(os.getpid()))\n"
    "while not os.path.exists('go'):\n    time.sleep(0.01)\n"
    "os.setsid()\n"
    "signal.signal(signal.SIGTERM, signal.SIG_DFL)\n"
    "open('left', 'w').write('1')\n"
    "time.sleep(60)\n"
)


def _ends_what_leaves_the_tree_during_a_slow_ownership_scan(
    tmp_path: Path, scan: float, end: Callable[[proc_tree.TurnTree], Awaitable[None]] = proc_tree.TurnTree.end,
) -> list[int]:
    # A child of the agent, still in the agent's group when the end lists every process, leaves for a session of its
    # own only then, while the ownership scan takes ``scan`` more seconds; the agent has exited, so the tree's end
    # finishes without it. ``end`` ends the tree. The child still running afterwards is returned.
    code = ("import os, subprocess, sys, time\n"
            "subprocess.Popen([sys.executable, '-c', " + repr(_LEAVES_ONCE_TOLD) + "])\n"
            "while not os.path.exists('child.pid'):\n    time.sleep(0.01)\n"
            "print('ready', flush=True)\nsys.stdin.read()\n")

    async def _run() -> list[int]:
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-c", code, cwd=tmp_path, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            start_new_session=True)
        tree = proc_tree.TurnTree.track(process, label="turn")
        assert process.stdout
        await process.stdout.readline()
        agent_ended = threading.Event()

        class _Ended(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                if f"ended the process tree of pid {process.pid}:" in record.getMessage():
                    agent_ended.set()

        live_owned, calls = tree._live_owned, []

        def listing_after_the_snapshot() -> tuple[set[int], bool]:
            # The first call comes before any signal; the next follows the end's first listing of every process.
            calls.append(None)
            if len(calls) == 2:
                (tmp_path / "go").write_text("1")
                deadline = time.monotonic() + 10
                while not ((tmp_path / "left").exists() and agent_ended.is_set()) and time.monotonic() < deadline:
                    time.sleep(0.01)
                time.sleep(scan)
            return live_owned()

        tree._live_owned = listing_after_the_snapshot  # type: ignore[method-assign]
        handler, logger = _Ended(), logging.getLogger(proc_tree.__name__)
        level = logger.level
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        try:
            await end(tree)
            assert agent_ended.is_set() and (tmp_path / "left").exists()
            return _running([int((tmp_path / "child.pid").read_text())])
        finally:
            logger.removeHandler(handler)
            logger.setLevel(level)
            for pid in _running([int((tmp_path / "child.pid").read_text())], settle=0):
                os.kill(pid, signal.SIGKILL)

    return asyncio.run(_run())


def test_turn_tree_end_ends_what_left_the_tree_after_the_ends_last_listing_reached_it(tmp_path: Path) -> None:
    # Once every end has finished, the end looks again for whatever still runs, rather than trusting the listing
    # that reached it.
    assert _ends_what_leaves_the_tree_during_a_slow_ownership_scan(tmp_path, 2 * proc_tree.TERM_GRACE_SEC) == []


def test_turn_tree_end_ends_what_left_the_tree_during_a_scan_that_outlasts_the_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The scan returns only after the looking has to stop: the last look still checks every seen process without
    # the old listing's reached filter, so the child that left meanwhile is killed, not left running.
    monkeypatch.setattr(proc_tree, "TERM_GRACE_SEC", 0.3)
    monkeypatch.setattr(proc_tree, "REAP_SEC", 0.3)
    assert _ends_what_leaves_the_tree_during_a_slow_ownership_scan(
        tmp_path, 2 * (proc_tree.TERM_GRACE_SEC + proc_tree.REAP_SEC)) == []


def test_a_stop_after_the_turn_ends_what_left_the_tree_during_a_scan_that_outlasts_the_stops_wait(
    tmp_path: Path,
) -> None:
    # The production grace and reap: the scan outlasts how long the stop waits for the end. The wait stops, the end
    # does not: its last look still finds the child that left the tree and kills it.
    async def stop(tree: proc_tree.TurnTree) -> None:
        session = CODEX.resident_session("planner-task-1")
        session._finished_tree = tree
        began = time.monotonic()
        await session.end_turn_processes()
        assert time.monotonic() - began < per_turn._end_bound() + 1
        await asyncio.gather(*(task for task in asyncio.all_tasks() if task is not asyncio.current_task()))

    assert _ends_what_leaves_the_tree_during_a_slow_ownership_scan(
        tmp_path, 2 * (proc_tree.TERM_GRACE_SEC + proc_tree.REAP_SEC), stop) == []


@pytest.mark.skipif(not proc_tree._MARKS, reason=_DETACHED_BEST_EFFORT)
def test_turn_tree_end_ends_what_the_agent_double_forks_as_it_exits_without_any_lineage_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Told to exit, the agent double-forks a system binary into a session of its own and exits at once: every
    # ancestor of the binary is gone before the end lists it, and the fork watch lost them all. The agent's
    # environment mark outlives the agent.
    monkeypatch.setattr(proc_tree._Lineage, "_follow", lambda self: None)
    code = ("import signal, sys\n"
            "def on_term(*_):\n"
            + "".join("    " + line + "\n" for line in _DOUBLE_FORK_EXEC.splitlines())
            + "    os._exit(0)\n"
            "signal.signal(signal.SIGTERM, on_term)\n"
            "print('ready', flush=True)\nsys.stdin.read()\n")

    async def _run() -> list[int]:
        process = await proc_tree.spawn(sys.executable, "-c", code, cwd=tmp_path,
                                        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE)
        tree = proc_tree.TurnTree.track(process, label="turn")
        assert process.stdout
        await process.stdout.readline()
        try:
            await tree.end()
            child = int(await _wait_file(tmp_path / "child.pid"))
            return _running([child])
        finally:
            if (tmp_path / "child.pid").exists():
                for pid in _running([int((tmp_path / "child.pid").read_text())], settle=0):
                    os.kill(pid, signal.SIGKILL)

    assert asyncio.run(_run()) == []


# The same double fork, the binary's streams its own: a turn that reads the agent's output to its end is not held.
_DOUBLE_FORK_EXEC_DETACHED = _DOUBLE_FORK_EXEC.replace(
    "        os.setsid()\n",
    "        os.setsid()\n"
    "        null = os.open(os.devnull, os.O_RDWR)\n"
    "        for fd in (0, 1, 2):\n"
    "            os.dup2(null, fd)\n",
)


def _late_fork_watch(monkeypatch: pytest.MonkeyPatch) -> None:
    """The fork watch resumes only once the processes between the agent and what it started have exited."""
    adopt = proc_tree._Lineage._adopt

    def late(self: Any, parent: int) -> None:
        time.sleep(0.5)
        adopt(self, parent)

    monkeypatch.setattr(proc_tree._Lineage, "_adopt", late)


def _left_running(child: int) -> list[int]:
    left = _running([child], settle=0)
    for pid in left:
        os.kill(pid, signal.SIGKILL)
    return left


# A resident Claude that answers its turn and, when its stdin closes, double-forks a system binary and exits.
_RESIDENT_THAT_DETACHES = (
    "import json, sys\n"
    "sys.stdin.readline()\n"
    "print(json.dumps({'type': 'system', 'subtype': 'init', 'session_id': 's'}), flush=True)\n"
    "print(json.dumps({'type': 'result', 'subtype': 'success', 'is_error': False, 'result': 'ok'}), flush=True)\n"
    "sys.stdin.read()\n"
    + _DOUBLE_FORK_EXEC_DETACHED
)


@pytest.mark.skipif(not proc_tree._MARKS, reason=_DETACHED_BEST_EFFORT)
def test_a_killed_resident_claude_that_double_forks_as_its_stdin_closes_leaves_nothing_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # ``stop(kill=True)`` closes Claude's stdin first: Claude double-forks, the binary leaves the session and execs,
    # and Claude exits, all before the end begins and before the fork watch resumes.
    _late_fork_watch(monkeypatch)
    claude = fake_claude(tmp_path / "claude", _RESIDENT_THAT_DETACHES)
    end = proc_tree.TurnTree.end

    async def end_once_the_agent_exited(self: proc_tree.TurnTree) -> None:
        await _wait_file(tmp_path / "child.pid")
        for _ in range(500):
            if self.process.returncode is not None:
                break
            await asyncio.sleep(0.01)
        await end(self)

    monkeypatch.setattr(proc_tree.TurnTree, "end", end_once_the_agent_exited)

    async def _run() -> int:
        session = ResidentClaude(label="manager")
        await session.start([claude], cwd=str(tmp_path), signature=None)

        async def on_event(event: dict[str, Any]) -> None:
            pass

        turn = await session.run_turn("hello", on_event)
        assert turn.result_event["result"] == "ok"
        await session.stop(kill=True)
        return int((tmp_path / "child.pid").read_text())

    assert _left_running(asyncio.run(_run())) == []


# A Codex turn that answers, then double-forks a system binary and exits: the turn returns with its result.
_CODEX_TURN_THAT_DETACHES = (
    "import json, sys\n"
    "sys.stdin.read()\n"
    "def emit(event):\n"
    "    print(json.dumps(event), flush=True)\n"
    "emit({'type': 'thread.started', 'thread_id': 'thread-1'})\n"
    "emit({'type': 'turn.started'})\n"
    "emit({'type': 'item.completed', 'item': {'id': 'item_0', 'type': 'agent_message', 'text': 'ok'}})\n"
    "emit({'type': 'turn.completed', 'usage': {'input_tokens': 1, 'cached_input_tokens': 0,\n"
    "                                          'cache_write_input_tokens': 0, 'output_tokens': 1,\n"
    "                                          'reasoning_output_tokens': 0}})\n"
    + _DOUBLE_FORK_EXEC_DETACHED
)


@pytest.mark.skipif(not proc_tree._MARKS, reason=_DETACHED_BEST_EFFORT)
def test_a_stop_after_a_codex_turn_returned_ends_what_it_double_forked_as_it_exited(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _late_fork_watch(monkeypatch)
    codex = fake_claude(tmp_path / "codex", _CODEX_TURN_THAT_DETACHES)

    async def _run() -> int:
        session = CODEX.resident_session("planner-task-1")
        command = _resident_command(tmp_path / "runtime", codex, "prompt")
        await session.start(command, cwd=str(tmp_path), signature="s", env=_launch().environment())

        async def on_event(event: dict[str, Any]) -> None:
            pass

        turn = await session.run_turn("hello", on_event)
        assert turn.result_event is not None
        child = int(await _wait_file(tmp_path / "child.pid"))
        await asyncio.sleep(1)  # the fork watch has resumed, too late: the child's ancestors have exited
        await session.end_turn_processes()  # the stop came once the turn had returned
        return child

    assert _left_running(asyncio.run(_run())) == []


def test_spawn_starts_the_agent_itself_and_nothing_beside_it() -> None:
    # No launcher, no keeper, on any platform: the process is the agent, alone in its group, and ``kill()`` ends it.
    async def _run() -> tuple[list[int], list[int], str, int]:
        process = await proc_tree.spawn("/bin/sleep", "60")
        try:
            table = proc_tree._snapshot()
            group = [pid for pid, (_, pgid) in table.items() if pgid == process.pid]
            children = [pid for pid, (ppid, _) in table.items() if ppid == process.pid]
            command = subprocess.run(["ps", "-o", "comm=", "-p", str(process.pid)], capture_output=True, text=True,
                                     check=False).stdout.strip()
            process.kill()
            return group, children, command, await asyncio.wait_for(process.wait(), 5)
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()

    group, children, command, code = asyncio.run(_run())
    assert children == []
    assert len(group) == 1
    assert command.endswith("sleep")
    assert code == -signal.SIGKILL


# A loop an earlier turn left running in the agent's group (its shell exited), starting a short-lived child again and
# again: the review's ``(while :; do sleep 1; done) &``.
_LOOP_THEN_TURN = (
    "import subprocess, sys\n"
    "subprocess.run(['/bin/sh', '-c', '(while :; do sleep 0.2; done) & echo $! > loop.pid'])\n"
    "print('go', flush=True)\n"
    "sys.stdin.readline()\n"
    "new = subprocess.Popen(['/bin/sleep', '60'])\nprint(new.pid, flush=True)\nsys.stdin.read()\n"
)


def test_turn_tree_end_new_returns_within_its_bound_while_a_baseline_loop_keeps_starting_children(
    tmp_path: Path,
) -> None:
    # What a baseline process starts since the baseline is the turn's: children that keep coming do not keep
    # ``end_new`` going past its bound, and the loop itself runs on.
    async def _run() -> tuple[float, list[int], list[int]]:
        process = await proc_tree.spawn(sys.executable, "-c", _LOOP_THEN_TURN, cwd=tmp_path,
                                        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE)
        assert process.stdin and process.stdout
        tree = proc_tree.TurnTree.track(process, label="turn")
        try:
            await process.stdout.readline()
            loop = int(await _wait_file(tmp_path / "loop.pid"))
            await asyncio.sleep(0.5)  # the loop has started children
            await tree.baseline()
            process.stdin.write(b"\n")
            await process.stdin.drain()
            new = int(await process.stdout.readline())
            await asyncio.sleep(0.5)  # and more since the baseline
            began = time.monotonic()
            await tree.end_new()
            took = time.monotonic() - began
            return took, _running([new]), _running([process.pid, loop], settle=0)
        finally:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)  # the agent's group: the loop and its children too
            await process.wait()

    took, new_left, survivors = asyncio.run(_run())
    assert took < proc_tree.TERM_GRACE_SEC + proc_tree.REAP_SEC + 1
    assert new_left == []
    assert len(survivors) == 2  # the agent and the loop run on


def test_turn_tree_end_gives_up_on_a_stray_that_survives_sigkill(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    # A seen child that left the group and that no signal ends (EPERM, uninterruptible sleep; here its signals are
    # dropped): the end logs it and returns within its bound instead of trying again forever.
    code = ("import subprocess, sys\n"
            "subprocess.Popen([sys.executable, '-c', " + repr(_MIDDLE) + "]).wait()\nsys.stdin.read()\n")
    signal_tree = proc_tree._signal
    stubborn: set[int] = set()

    def dropping(process: Any, tree: dict[int, int], sig: int) -> None:
        signal_tree(process, {pid: pgid for pid, pgid in tree.items() if pid not in stubborn}, sig)

    monkeypatch.setattr(proc_tree, "_signal", dropping)

    async def _run() -> tuple[float, list[int]]:
        process = await _spawn_python(tmp_path, code)
        tree = proc_tree.TurnTree.track(process, label="turn")
        child = await _child_that_leaves_its_group(tmp_path, tree)
        stubborn.add(child)
        try:
            began = time.monotonic()
            await tree.end()
            return time.monotonic() - began, _running([child], settle=0)
        finally:
            for pid in _running([child], settle=0):
                os.kill(pid, signal.SIGKILL)

    caplog.set_level(logging.WARNING, logger=proc_tree.__name__)
    took, left = asyncio.run(_run())
    assert took < proc_tree.TERM_GRACE_SEC + 2 * proc_tree.REAP_SEC + 1
    assert len(left) == 1  # still running: given up on, not looped over
    assert any("left running" in record.getMessage() for record in caplog.records)


# A resident MCP server the agent started in an earlier turn; this turn it runs a tool (a child of its own).
_SERVER = (
    "import os, subprocess, time\n"
    "while not os.path.exists('call'):\n    time.sleep(0.01)\n"
    "tool = subprocess.Popen(['/bin/sleep', '60'])\nopen('tool.pid', 'w').write(str(tool.pid))\ntime.sleep(60)\n"
)
_AGENT_WITH_SERVER = (
    "import subprocess, sys\n"
    "server = subprocess.Popen([sys.executable, '-c', " + repr(_SERVER) + "])\n"
    "open('server.pid', 'w').write(str(server.pid))\nsys.stdin.read()\n"
)


def test_turn_tree_end_new_ends_the_tool_a_baseline_server_ran_this_turn_and_leaves_the_server(
    tmp_path: Path,
) -> None:
    async def _run() -> tuple[list[int], list[int]]:
        process = await _spawn_python(tmp_path, _AGENT_WITH_SERVER)
        tree = proc_tree.TurnTree.track(process, label="turn")
        server = int(await _wait_file(tmp_path / "server.pid"))
        tool = 0
        try:
            await tree.baseline()
            assert server in tree.seen
            (tmp_path / "call").write_text("go")
            tool = int(await _wait_file(tmp_path / "tool.pid"))
            await tree.end_new()
            return _running([tool]), _running([process.pid, server], settle=0)
        finally:
            for pid in _running([tool, server] if tool else [server], settle=0):
                os.kill(pid, signal.SIGKILL)
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            await process.wait()

    tool_left, survivors = asyncio.run(_run())
    assert tool_left == []
    assert len(survivors) == 2  # the agent and its server run on


def test_turn_tree_end_looks_again_when_an_owned_process_exits_before_it_is_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Once every end has finished, the ownership listing finds a process that, before its start time is read,
    # forks a child into a session of its own (inheriting the ownership mark) and exits; the end does not finish
    # on that listing, as the next one finds the child.
    gone = subprocess.Popen(["/usr/bin/true"])
    gone.wait()  # the owned process: listed, but exited by the time its start time is read
    child: list[subprocess.Popen[bytes]] = []
    ended_scan = threading.Event()
    strays = proc_tree.TurnTree._strays

    async def noting_ended(self: proc_tree.TurnTree, exclude: frozenset[int], *, ended: bool = False) -> Any:
        if ended:
            ended_scan.set()
        return await strays(self, exclude, ended=ended)

    def owned(self: proc_tree.TurnTree) -> tuple[set[int], bool]:
        if not ended_scan.is_set():
            return set(), False
        if not child:
            # What the listed process left behind: in no tree the end knows, owned only by its inherited mark.
            child.append(subprocess.Popen(["/bin/sleep", "60"], start_new_session=True))
            return {gone.pid}, False
        return ({child[0].pid} if child[0].poll() is None else set()), False

    monkeypatch.setattr(proc_tree.TurnTree, "_strays", noting_ended)
    monkeypatch.setattr(proc_tree.TurnTree, "_live_owned", owned)

    async def _run() -> list[int]:
        process = await _spawn_python(tmp_path, "import sys\nsys.stdin.read()\n")
        tree = proc_tree.TurnTree.track(process, label="turn")
        try:
            await tree.end()
            assert child
            child[0].poll()  # reaped, so a zombie is not taken for a running process
            return _running([child[0].pid], settle=0)
        finally:
            for leftover in child:
                leftover.kill()
                leftover.wait()

    assert asyncio.run(_run()) == []


def test_turn_tree_end_looks_again_when_a_marked_process_exits_before_its_environment_is_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Linux marks, through a simulated ``/proc``: once every end has finished, the listing names a process that,
    # before its environment is read, forks a child into a session of its own (inheriting the mark) and exits; the
    # end does not finish on that listing, as the next one finds the child.
    proc = tmp_path / "proc"
    proc.mkdir()
    monkeypatch.setattr(proc_tree, "_PROC", str(proc))
    gone = subprocess.Popen(["/usr/bin/true"])
    gone.wait()  # the marked process: listed, but exited by the time its environment is read
    child: list[subprocess.Popen[bytes]] = []
    ended_scan = threading.Event()
    strays = proc_tree.TurnTree._strays
    live = proc_tree._Marked.live

    async def noting_ended(self: proc_tree.TurnTree, exclude: frozenset[int], *, ended: bool = False) -> Any:
        if ended:
            ended_scan.set()
        return await strays(self, exclude, ended=ended)

    def listing(self: proc_tree._Marked) -> tuple[set[int], bool]:
        if ended_scan.is_set():
            if not child:
                # Listed (its ``/proc`` entry is there), gone before its environment is read; the child it left
                # is in no tree the end knows and not in this listing.
                (proc / str(gone.pid)).mkdir()
                child.append(subprocess.Popen(["/bin/sleep", "60"], start_new_session=True))
            else:
                if (proc / str(gone.pid)).exists():
                    (proc / str(gone.pid)).rmdir()
                (proc / str(child[0].pid)).mkdir(exist_ok=True)
                (proc / str(child[0].pid) / "environ").write_bytes(b"A=1\0" + self._entry + b"\0")
        return live(self)

    monkeypatch.setattr(proc_tree.TurnTree, "_strays", noting_ended)
    monkeypatch.setattr(proc_tree._Marked, "live", listing)

    async def _run() -> list[int]:
        process = await _spawn_python(tmp_path, "import sys\nsys.stdin.read()\n")
        tree = proc_tree.TurnTree.track(process, label="turn")
        if tree._owned is not None:
            tree._owned.close()
        tree._owned = proc_tree._Marked(process.pid, "mark")
        try:
            await tree.end()
            assert child
            child[0].poll()  # reaped, so a zombie is not taken for a running process
            return _running([child[0].pid], settle=0)
        finally:
            for leftover in child:
                leftover.kill()
                leftover.wait()

    assert asyncio.run(_run()) == []


def test_turn_tree_end_looks_again_when_a_marked_process_is_a_zombie_as_its_environment_is_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # As above, but the marked process has exited and is not reaped yet as its environment is read: a zombie's
    # environment reads empty, and the end does not finish on that listing, as the next one finds the child.
    proc = tmp_path / "proc"
    proc.mkdir()
    monkeypatch.setattr(proc_tree, "_PROC", str(proc))
    gone = subprocess.Popen(["/usr/bin/true"])
    gone.wait()
    child: list[subprocess.Popen[bytes]] = []
    ended_scan = threading.Event()
    strays = proc_tree.TurnTree._strays
    live = proc_tree._Marked.live

    async def noting_ended(self: proc_tree.TurnTree, exclude: frozenset[int], *, ended: bool = False) -> Any:
        if ended:
            ended_scan.set()
        return await strays(self, exclude, ended=ended)

    def listing(self: proc_tree._Marked) -> tuple[set[int], bool]:
        zombie = proc / str(gone.pid)
        if ended_scan.is_set():
            if not child:
                # Listed and still there, but a zombie: its environment reads empty; the child it left is in
                # no tree the end knows and not in this listing.
                zombie.mkdir()
                (zombie / "environ").write_bytes(b"")
                (zombie / "stat").write_bytes(f"{gone.pid} (true) Z 1 {gone.pid} {gone.pid} 0 -1".encode())
                child.append(subprocess.Popen(["/bin/sleep", "60"], start_new_session=True))
            else:
                if zombie.exists():
                    shutil.rmtree(zombie)
                (proc / str(child[0].pid)).mkdir(exist_ok=True)
                (proc / str(child[0].pid) / "environ").write_bytes(b"A=1\0" + self._entry + b"\0")
        return live(self)

    monkeypatch.setattr(proc_tree.TurnTree, "_strays", noting_ended)
    monkeypatch.setattr(proc_tree._Marked, "live", listing)

    async def _run() -> list[int]:
        process = await _spawn_python(tmp_path, "import sys\nsys.stdin.read()\n")
        tree = proc_tree.TurnTree.track(process, label="turn")
        if tree._owned is not None:
            tree._owned.close()
        tree._owned = proc_tree._Marked(process.pid, "mark")
        try:
            await tree.end()
            assert child
            child[0].poll()  # reaped, so a zombie is not taken for a running process
            return _running([child[0].pid], settle=0)
        finally:
            for leftover in child:
                leftover.kill()
                leftover.wait()

    assert asyncio.run(_run()) == []


def test_marked_listing_counts_a_zombie_as_exited_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # A zombie no one reaps (another program's) calls for one more listing, not for one at every listing: the
    # listing after the one that found it has had what it forked.
    proc = tmp_path / "proc"
    (proc / "42").mkdir(parents=True)
    (proc / "42" / "environ").write_bytes(b"")
    (proc / "42" / "stat").write_bytes(b"42 (a (b) c) Z 1 42 42 0 -1")
    (proc / "43").mkdir()
    (proc / "43" / "environ").write_bytes(b"")
    (proc / "43" / "stat").write_bytes(b"43 (kthreadd) S 0 0 0 0 -1")
    monkeypatch.setattr(proc_tree, "_PROC", str(proc))
    marked = proc_tree._Marked(1, "mark")
    assert marked.live() == (set(), True)
    assert marked.live() == (set(), False)


def test_turn_tree_end_kills_the_strays_a_listing_past_the_deadline_found(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The first listing of strays returns only after the deadline (a slow ``ps``): what it found still gets SIGKILL.
    monkeypatch.setattr(proc_tree, "TERM_GRACE_SEC", 0.3)
    monkeypatch.setattr(proc_tree, "REAP_SEC", 0.3)
    strays = proc_tree.TurnTree._strays

    async def slow(self: proc_tree.TurnTree, *args: Any, **kwargs: Any) -> Any:
        found = await strays(self, *args, **kwargs)
        await asyncio.sleep(0.8)
        return found

    monkeypatch.setattr(proc_tree.TurnTree, "_strays", slow)
    code = ("import subprocess, sys\n"
            "subprocess.Popen([sys.executable, '-c', " + repr(_MIDDLE) + "]).wait()\nsys.stdin.read()\n")

    async def _run() -> list[int]:
        process = await _spawn_python(tmp_path, code)
        tree = proc_tree.TurnTree.track(process, label="turn")
        child = await _child_that_leaves_its_group(tmp_path, tree)
        try:
            await tree.end()
            return _running([child])
        finally:
            for pid in _running([child], settle=0):
                os.kill(pid, signal.SIGKILL)

    assert asyncio.run(_run()) == []
