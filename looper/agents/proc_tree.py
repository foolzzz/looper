"""Ending an agent process together with every process it started.

Looper starts each agent process (Claude Code, Codex, OMP) in a session of
its own (``start_new_session=True``), so the children it starts share its
process group. Some start a group of their own: Codex runs each shell
command in one. Once a process dies its children are re-parented, and
nothing links them to the agent any more. So ``end`` reads the agent's tree
from one ``ps`` snapshot before it sends any signal: the agent, every
process whose parent is in the tree, and every member of a group the agent
or one of them leads. It sends SIGTERM to those groups (and to the tree's
processes outside them), waits up to ``TERM_GRACE_SEC`` for them to exit,
sends SIGKILL to what is left and reaps the agent. A stop that first lets
the agent exit on its own takes the tree before it asks (``tree_of``) and
hands it to ``end``.

Every stop, kill, timeout and spend-cap path of the Workers, the Reviewers
and the resident Manager and Planner sessions ends its process this way, as
does server shutdown.

Agents no longer share the server's process group, so nothing ends them when
the server itself dies (SIGKILL, a crash). Each agent process is started
through ``spawn``, which records its pid and start time, and the server that
started it, under the runtime root (``RECORDS_DIR``) until it is reaped. At
the next start ``sweep`` ends the tree of every recorded agent still running
with the same start time, so a reused pid is never touched; a record whose
server still runs is left alone.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import subprocess
import time
from collections import defaultdict
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

logger = logging.getLogger(__name__)

TERM_GRACE_SEC = 3.0  # how long the tree has to exit after SIGTERM before what is left is killed
_POLL_SEC = 0.05
_PS_TIMEOUT_SEC = 5.0
RECORDS_DIR = Path(".local") / "agent-processes"  # under the runtime root: one ``<pid>.json`` per running agent

# Set by ``sweep``: where this server records its agents, and the server (pid, start time) each record names.
_records: Path | None = None
_server: tuple[int, str] | None = None
_forgetting: set[asyncio.Task[None]] = set()

# pid -> (ppid, pgid) of every live process; a zombie has exited already and is left out.
Table = dict[int, tuple[int, int]]
# pid -> pgid of the processes of one agent's tree.
Tree = dict[int, int]


def _snapshot() -> Table:
    """Every live process, from one ``ps``; empty when ``ps`` cannot run."""
    try:
        listed = subprocess.run(["ps", "-A", "-o", "pid=,ppid=,pgid=,stat="], capture_output=True, text=True,
                                timeout=_PS_TIMEOUT_SEC, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("cannot list processes, so only the agent's own group is ended: %s", exc)
        return {}
    table: Table = {}
    for line in listed.stdout.splitlines():
        fields = line.split()
        if len(fields) < 4 or fields[3].startswith("Z"):
            continue
        try:
            table[int(fields[0])] = (int(fields[1]), int(fields[2]))
        except ValueError:
            continue
    if not table:
        logger.warning("ps listed no processes (exit code %s), so only the agent's own group is ended",
                       listed.returncode)
    return table


class _Agent(Protocol):
    pid: int

    @property
    def returncode(self) -> int | None: ...

    def send_signal(self, sig: int) -> None: ...


class _Orphan:
    """An agent a previous server started: not this server's child, so it is signalled by pid and never reaped."""

    returncode = None

    def __init__(self, pid: int) -> None:
        self.pid = pid

    def send_signal(self, sig: int) -> None:
        os.kill(self.pid, sig)


def _tree(process: _Agent, table: Table, known: Tree) -> Tree:
    """The processes of ``process``'s tree that ``table`` lists: the agent, every process whose parent is in the
    tree, every member of a group the agent or one of them leads, and each of ``known`` still in its group."""
    root = process.pid
    children: defaultdict[int, list[int]] = defaultdict(list)
    members: defaultdict[int, list[int]] = defaultdict(list)
    for pid, (ppid, pgid) in table.items():
        children[ppid].append(pid)
        members[pgid].append(pid)
    own_pid, own_group = os.getpid(), os.getpgrp()
    tree: Tree = {}
    groups: set[int] = set()
    pending = [root, *(pid for pid, pgid in known.items() if pid in table and table[pid][1] == pgid)]

    def add_group(pgid: int) -> None:
        if pgid != own_group and pgid not in groups:
            groups.add(pgid)
            pending.extend(members[pgid])

    add_group(root)  # the agent leads its group, whose members stay its own once it has exited
    while pending:
        pid = pending.pop()
        if pid in tree or pid not in table or pid == own_pid:
            continue
        tree[pid] = pgid = table[pid][1]
        pending.extend(children[pid])
        if pgid == pid:
            add_group(pid)
    if not table and process.returncode is None:
        try:  # without a listing, the agent and its own group are all that can be reached
            tree[root] = os.getpgid(root)
        except ProcessLookupError:
            pass
    return tree


def _signal(process: _Agent, tree: Tree, sig: int) -> None:
    """Send ``sig`` to every group a process of ``tree`` leads and to the tree's other processes."""
    leaders = {pgid for pid, pgid in tree.items() if pid == pgid} - {os.getpgrp()}
    for pgid in leaders:
        _send(os.killpg, pgid, sig)
    for pid, pgid in tree.items():
        if pgid not in leaders and pid != process.pid:
            _send(os.kill, pid, sig)
    # The agent itself through asyncio, which knows whether it was reaped, unless its group got the signal.
    if process.pid in tree and tree[process.pid] not in leaders and process.returncode is None:
        try:
            process.send_signal(sig)
        except (ProcessLookupError, PermissionError):
            pass


def _send(send: Callable[[int, int], None], target: int, sig: int) -> None:
    try:
        send(target, sig)
    except (ProcessLookupError, PermissionError):
        pass  # it exited already


def tree_of(process: asyncio.subprocess.Process) -> Tree:
    """``process``'s tree now, for ``end`` once the process may have exited and its children been re-parented."""
    return _tree(process, _snapshot(), {})


async def end(process: asyncio.subprocess.Process, *, label: str, known: Tree | None = None) -> None:
    """End ``process`` and its tree: SIGTERM, then SIGKILL for what is still running after ``TERM_GRACE_SEC``;
    ``process`` is reaped. ``known`` is its tree as ``tree_of`` took it earlier. Processes that appear during the
    grace period get SIGTERM too, and a cancelled wait kills the rest at once."""
    await _end(process, label=label, known=known or {})
    await process.wait()


async def _end(process: _Agent, *, label: str, known: Tree) -> None:
    seen = _tree(process, _snapshot(), known)
    alive = dict(seen)
    if alive:
        _signal(process, alive, signal.SIGTERM)
    try:
        deadline = time.monotonic() + TERM_GRACE_SEC
        while alive and time.monotonic() < deadline:
            await asyncio.sleep(_POLL_SEC)
            alive = _tree(process, await asyncio.to_thread(_snapshot), seen)
            if fresh := {pid: pgid for pid, pgid in alive.items() if pid not in seen}:
                seen.update(fresh)
                _signal(process, fresh, signal.SIGTERM)
    except BaseException:
        alive = _tree(process, _snapshot(), seen)
        raise
    finally:
        if alive:
            _signal(process, alive, signal.SIGKILL)
        logger.info("%s: ended the process tree of pid %s: %d found, %d exited on SIGTERM, %d killed",
                    label, process.pid, len(seen), len(seen) - len(alive), len(alive))


# --- Recording agents for the next server's sweep ---


def _start_time(pid: int) -> str | None:
    """When ``pid`` started, as ``ps -o lstart=`` prints it in UTC; None when no such process runs."""
    try:
        listed = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)], capture_output=True, text=True,
                                timeout=_PS_TIMEOUT_SEC, check=False, env={**os.environ, "LC_ALL": "C", "TZ": "UTC"})
    except (OSError, subprocess.SubprocessError):
        return None
    return " ".join(listed.stdout.split()) or None


def _write_record(directory: Path, pid: int, started: str, server: tuple[int, str]) -> Path:
    """Record agent ``pid``, started at ``started``, as started by ``server`` (its pid and start time)."""
    path = directory / f"{pid}.json"
    path.write_text(json.dumps({"pid": pid, "started": started, "server_pid": server[0],
                                "server_started": server[1]}) + "\n", encoding="utf-8")
    return path


async def spawn(*argv: str, **kwargs: Any) -> asyncio.subprocess.Process:
    """Start an agent process in a session of its own, so its children share its process group, and record it
    until it is reaped (once ``sweep`` has run)."""
    process = await asyncio.create_subprocess_exec(*argv, start_new_session=True, **kwargs)
    if _records is not None and _server is not None:
        if (started := _start_time(process.pid)) is None:
            logger.warning("cannot read the start time of agent pid %s; it is not recorded for the next sweep",
                           process.pid)
            return process
        try:
            path = _write_record(_records, process.pid, started, _server)
        except OSError as exc:
            logger.warning("cannot record agent pid %s for the next sweep: %s", process.pid, exc)
            return process
        forget = asyncio.get_running_loop().create_task(_forget(process, path, started))
        _forgetting.add(forget)
        forget.add_done_callback(_forgetting.discard)
    return process


async def _forget(process: asyncio.subprocess.Process, path: Path, started: str) -> None:
    await process.wait()
    try:
        if json.loads(path.read_text(encoding="utf-8")).get("started") == started:  # not a later agent's pid
            path.unlink()
    except (OSError, ValueError):
        pass


async def sweep(runtime_root: Path) -> None:
    """End what agents of a previous server on ``runtime_root`` left running, then record this server's agents.

    A record names an agent's pid and start time and the server that started
    it. One whose server still runs (another server on this runtime root) is
    left alone. Otherwise the record is removed, and its agent's tree is
    ended when its pid still runs with the same start time: the same process,
    orphaned by a server that crashed or was killed. A pid that no longer
    runs, or that now belongs to another process, is left alone. Children of an
    agent that has already exited are not found: nothing links them to it.
    """
    global _records, _server
    directory = runtime_root / RECORDS_DIR
    directory.mkdir(parents=True, exist_ok=True)
    own_start = _start_time(os.getpid())
    _records, _server = (directory, (os.getpid(), own_start)) if own_start else (None, None)
    if own_start is None:
        logger.warning("cannot read this server's start time; its agents are not recorded for the next sweep")
    servers: dict[tuple[int, str], bool] = {}  # whether each (pid, start time) still runs
    orphans: list[int] = []
    for path in sorted(directory.glob("*.json")):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
            pid, started = int(record["pid"]), str(record["started"])
            server = (int(record["server_pid"]), str(record["server_started"]))
        except (OSError, ValueError, KeyError, TypeError):
            logger.warning("removing the unreadable agent record %s", path.name)
            path.unlink(missing_ok=True)
            continue
        if server != _server:
            if server not in servers:
                servers[server] = _start_time(server[0]) == server[1]
            if servers[server]:
                continue  # its server runs: the agent is that server's to end
        if _start_time(pid) == started:
            orphans.append(pid)
        path.unlink(missing_ok=True)
    if orphans:
        logger.warning("ending %d agent(s) a previous server left running: pid %s", len(orphans),
                       ", ".join(map(str, orphans)))
        await asyncio.gather(*(_end(_Orphan(pid), label=f"orphaned agent {pid}", known={}) for pid in orphans))
