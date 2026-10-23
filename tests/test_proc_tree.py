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
from looper.agents import claude_cli, proc_tree
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
    monkeypatch.setattr(claude_cli, "STOP_GRACE_SEC", 0.5)


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


@pytest.fixture
def _patient_end(monkeypatch: pytest.MonkeyPatch) -> None:
    """A turn waits only ``TERM_GRACE_SEC + REAP_SEC + 1`` s for its tree's end; on a loaded machine the ``ps`` calls
    before the first SIGTERM can use that up, and the loop closing then cancels the end unsent. The test is about
    the tree being ended, not how fast, so it gives the end room (it returns as soon as the tree is gone)."""
    monkeypatch.setattr(proc_tree, "REAP_SEC", 30.0)


@pytest.mark.usefixtures("_patient_end")
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
        record = runtime_root / proc_tree.RECORDS_DIR / f"{tree['agent']}.json"
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


@pytest.mark.parametrize("recordable", [True, False], ids=["recorded", "identity_unavailable"])
def test_a_reaped_agents_record_is_removed(
    tmp_path: Path, runtime_root: Path, monkeypatch: pytest.MonkeyPatch, recordable: bool,
) -> None:
    async def _run() -> tuple[bool, bool]:
        await proc_tree.sweep(runtime_root)
        if not recordable:
            # A process can exit before its start time is read. Its handle must still be returned and reaped.
            monkeypatch.setattr(proc_tree, "_start_time", lambda pid: None)
        process = await proc_tree.spawn("/bin/sleep", "0.2")
        record = runtime_root / proc_tree.RECORDS_DIR / f"{process.pid}.json"
        recorded = record.exists()
        await process.wait()
        assert process.returncode == 0
        await asyncio.sleep(0.05)  # the record is removed once the process is reaped
        return recorded, record.exists()

    assert asyncio.run(_run()) == (recordable, False)


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


@pytest.mark.parametrize("root_reused", [False, True], ids=["seen_leader", "root"])
def test_turn_tree_leaves_a_reused_leaders_new_tree_alone(
    monkeypatch: pytest.MonkeyPatch, root_reused: bool,
) -> None:
    class Agent:
        pid = 101
        returncode = 0

        async def wait(self) -> int:
            return 0

    table = {101: (1, 101), 102: (101, 102), 105: (102, 105)}
    starts = {101: "agent", 102: "child", 105: "survivor"}
    signalled: set[int] = set()
    monkeypatch.setattr(proc_tree, "_snapshot", lambda: dict(table))
    monkeypatch.setattr(proc_tree, "_start_times", lambda pids: {pid: starts[pid] for pid in pids if pid in starts})
    monkeypatch.setattr(proc_tree.os, "kill", lambda pid, sig: signalled.add(pid))
    monkeypatch.setattr(proc_tree.os, "killpg", lambda pgid, sig: signalled.update(
        pid for pid, (_, group) in table.items() if group == pgid))
    monkeypatch.setattr(proc_tree, "TERM_GRACE_SEC", 0)

    async def _run() -> None:
        tree = proc_tree.TurnTree.track(Agent(), label="turn")
        await tree.refresh(force=True)
        reused = 101 if root_reused else 102
        # The old leader exited; an unrelated leader now owns its pid and has a new child and group member.
        table.clear()
        table.update({reused: (1, reused), 103: (reused, 103), 104: (1, reused), 105: (1, 105)})
        starts.update({reused: "replacement", 103: "new child", 104: "new member"})
        await tree.end()

    asyncio.run(_run())
    assert signalled == {105}  # the original turn's surviving descendant is still ended


def test_turn_tree_forgets_groups_missing_from_a_refresh(monkeypatch: pytest.MonkeyPatch) -> None:
    process = proc_tree._Orphan(101)
    table = {101: (1, 101), 102: (101, 102)}
    monkeypatch.setattr(proc_tree, "_snapshot", lambda: dict(table))
    monkeypatch.setattr(proc_tree, "_start_times", lambda pids: {pid: "started" for pid in pids})

    async def _run() -> None:
        tree = proc_tree.TurnTree.track(process, label="turn")
        table.pop(102)
        await tree.refresh(force=True)
        table.update({102: (1, 102), 103: (102, 102)})
        await tree.refresh(force=True)
        assert 103 not in tree.seen

    asyncio.run(_run())


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
