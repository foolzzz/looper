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
sends SIGKILL to what is left, waits up to ``REAP_SEC`` for those processes
to exit and reaps the agent. A stop that first lets
the agent exit on its own records the tree before it asks (``begin_end``) and
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
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Any, Protocol

from .. import child_env

logger = logging.getLogger(__name__)

TERM_GRACE_SEC = 3.0  # how long the tree has to exit after SIGTERM before what is left is killed
REAP_SEC = 2.0  # how long the agent has to be reaped after SIGKILL before ``end`` gives up waiting
_REFRESH_MIN_SEC = 0.1
_POLL_SEC = 0.05
_PS_TIMEOUT_SEC = 5.0
RECORDS_DIR = Path(".local") / "agent-processes"  # under the runtime root: one ``<pid>.json`` per running agent

# Set by ``sweep``: where this server records its agents, and the server (pid, start time) each record names.
_records: Path | None = None
_server: tuple[int, str] | None = None
_recorded: dict[int, tuple[Path, str]] = {}
_ending: set[int] = set()
_forgetting: set[asyncio.Task[None]] = set()

# pid -> (ppid, pgid) of every live process; a zombie has exited already and is left out.
Table = dict[int, tuple[int, int]]
# pid -> pgid of the processes of one agent's tree.
Tree = dict[int, int]


def _snapshot() -> Table:
    """Every live process, from one ``ps``; empty when ``ps`` cannot run."""
    try:
        listed = subprocess.run(["ps", "-A", "-o", "pid=,ppid=,pgid=,stat="], capture_output=True, text=True,
                                timeout=_PS_TIMEOUT_SEC, check=False, env=child_env.inherited())
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
    """A previous server's process, signalled by pid and never reaped. A start-time check just before SIGTERM
    and SIGKILL limits the pid-reuse window to one ``ps`` call."""

    returncode = None

    def __init__(self, pid: int, started: str | None = None) -> None:
        self.pid = pid
        self.started = started

    def send_signal(self, sig: int) -> None:
        os.kill(self.pid, sig)


def _tree(process: _Agent, table: Table, known: Tree, *, skip: frozenset[int] = frozenset()) -> Tree:
    """The processes of ``process``'s tree that ``table`` lists: the agent, every process whose parent is in the
    tree, every member of a group the agent or one of them leads, and each of ``known`` still in its group.
    A pid in ``skip`` contributes neither its children nor its group."""
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

    if root not in skip:
        add_group(root)  # the agent leads its group, whose members stay its own once it has exited
    while pending:
        pid = pending.pop()
        if pid in skip or pid in tree or pid not in table or pid == own_pid:
            continue
        tree[pid] = pgid = table[pid][1]
        pending.extend(children[pid])
        if pgid == pid:
            add_group(pid)
    if not table and process.returncode is None and root not in skip:
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


async def begin_end(process: asyncio.subprocess.Process) -> Tree:
    """Take and record the tree before asking the agent to exit; keep its record until ``end`` finishes."""
    tree = tree_of(process)
    if process.pid in _recorded:
        _ending.add(process.pid)
        await _record_tree(process.pid, tree)
    return tree


async def _record_tree(pid: int, tree: Tree) -> None:
    path, started = _recorded[pid]
    starts = await asyncio.to_thread(_start_times, tree)
    if starts is None:
        logger.warning("cannot read start times for agent pid %s; keeping its recorded tree", pid)
        return
    assert _server is not None
    _write_record(path.parent, pid, started, _server, tree=starts)


async def end(process: asyncio.subprocess.Process, *, label: str, known: Tree | None = None) -> None:
    """End ``process`` and its tree: SIGTERM, then SIGKILL for what is still running after ``TERM_GRACE_SEC``;
    ``process`` is reaped. ``known`` is its tree as ``begin_end`` took it earlier. Processes that appear during the
    grace period get SIGTERM too, and a cancelled wait kills the rest at once."""
    try:
        await _end(process, label=label, known=known or {})
    finally:
        # A cancelled end sends SIGKILL too; reap it before the cancellation escapes.
        await _reap(process, label)


async def _reap(process: Any, label: str) -> None:
    """Wait for the killed agent, for at most ``REAP_SEC``: a turn never hangs on a process that will not be reaped."""
    try:
        await asyncio.wait_for(process.wait(), REAP_SEC)
    except asyncio.TimeoutError:
        logger.warning("%s: pid %s was not reaped within %.1f s of SIGKILL", label, process.pid, REAP_SEC)


async def _end(process: _Agent, *, label: str, known: Tree, exclude: frozenset[int] = frozenset()) -> None:
    """End the tree, recording it so the next start can finish an end cut short by the server's exit. Processes
    in ``exclude`` are never signalled."""
    recorded = _recorded.get(process.pid)
    if recorded is not None:
        _ending.add(process.pid)

    def members(table: Table, known: Tree) -> Tree:
        return _tree(process, table, known, skip=exclude)

    seen = dict(known)
    alive = dict(seen)
    try:
        # Bulk stops must not serialize blocking ps calls on the server's event loop.
        seen = members(await asyncio.to_thread(_snapshot), known)
        alive = dict(seen)
        if recorded is not None:
            path, started = recorded
            await _record_tree(process.pid, seen)
        if alive:
            if isinstance(process, _Orphan) and process.started is not None:
                if await asyncio.to_thread(_start_time, process.pid) != process.started:
                    logger.warning("%s: pid %s was reused; leaving it alone", label, process.pid)
                    alive = {}
                    return
            _signal(process, alive, signal.SIGTERM)
        deadline = time.monotonic() + TERM_GRACE_SEC
        while alive and time.monotonic() < deadline:
            await asyncio.sleep(_POLL_SEC)
            alive = members(await asyncio.to_thread(_snapshot), seen)
            if fresh := {pid: pgid for pid, pgid in alive.items() if pid not in seen}:
                seen.update(fresh)
                if recorded is not None:
                    await _record_tree(process.pid, seen)
                _signal(process, fresh, signal.SIGTERM)
    except BaseException:
        alive = members(_snapshot(), seen)
        raise
    finally:
        if alive:
            if (isinstance(process, _Orphan) and process.started is not None
                    and await asyncio.to_thread(_start_time, process.pid) != process.started):
                logger.warning("%s: pid %s was reused; leaving it alone", label, process.pid)
            else:
                _signal(process, alive, signal.SIGKILL)
                # SIGKILL being sent (or the agent being reaped) does not mean its descendants have exited.
                # Wait for the processes we signalled, without expanding the tree or signalling it again.
                pending = dict(alive)
                deadline = time.monotonic() + REAP_SEC
                while pending and time.monotonic() < deadline:
                    table = await asyncio.to_thread(_snapshot)
                    pending = {pid: pgid for pid, pgid in pending.items()
                               if pid in table and table[pid][1] == pgid}
                    if pending:
                        await asyncio.sleep(min(_POLL_SEC, max(0, deadline - time.monotonic())))
                if pending:
                    logger.warning("%s: processes still running %.1f s after SIGKILL: %s",
                                   label, REAP_SEC, sorted(pending))
        logger.info("%s: ended the process tree of pid %s: %d found, %d exited on SIGTERM, %d killed",
                    label, process.pid, len(seen), len(seen) - len(alive), len(alive))
        _ending.discard(process.pid)
        if recorded is not None and process.returncode is not None:
            try:
                if json.loads(path.read_text(encoding="utf-8")).get("started") == started:
                    path.unlink()
            except (OSError, ValueError):
                pass
            _recorded.pop(process.pid, None)


# --- Recording agents for the next server's sweep ---


def _start_time(pid: int) -> str | None:
    """When ``pid`` started, as ``ps -o lstart=`` prints it in UTC; None when no such process runs."""
    try:
        listed = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)], capture_output=True, text=True,
                                timeout=_PS_TIMEOUT_SEC, check=False, env={**child_env.inherited(), "LC_ALL": "C", "TZ": "UTC"})
    except (OSError, subprocess.SubprocessError):
        return None
    return " ".join(listed.stdout.split()) or None


def _start_times(pids: Iterable[int]) -> dict[int, str] | None:
    """The start times ``ps`` lists for ``pids``, in UTC; None when ``ps`` cannot run."""
    selected = ",".join(map(str, pids))
    if not selected:
        return {}
    try:
        listed = subprocess.run(["ps", "-o", "pid=,lstart=", "-p", selected], capture_output=True, text=True,
                                timeout=_PS_TIMEOUT_SEC, check=False, env={**child_env.inherited(), "LC_ALL": "C", "TZ": "UTC"})
    except (OSError, subprocess.SubprocessError):
        return None
    started: dict[int, str] = {}
    for line in listed.stdout.splitlines():
        fields = line.split(maxsplit=1)
        if len(fields) != 2:
            continue
        try:
            started[int(fields[0])] = " ".join(fields[1].split())
        except ValueError:
            continue
    return started


def _write_record(
    directory: Path, pid: int, started: str, server: tuple[int, str], tree: dict[int, str] | None = None,
) -> Path:
    """Record agent ``pid``, started at ``started``, as started by ``server`` (its pid and start time)."""
    path = directory / f"{pid}.json"
    record: dict[str, Any] = {"pid": pid, "started": started, "server_pid": server[0], "server_started": server[1]}
    if tree is not None:
        record["tree"] = [{"pid": p, "started": s} for p, s in tree.items()]
    path.write_text(json.dumps(record) + "\n", encoding="utf-8")
    return path


async def spawn(*argv: str, env: Mapping[str, str] | None = None, **kwargs: Any) -> asyncio.subprocess.Process:
    """Start an agent process in a session of its own, so its children share its process group, and record it
    until it is reaped (once ``sweep`` has run). Without ``env``, start with ``child_env.inherited()``."""
    process = await asyncio.create_subprocess_exec(
        *argv, env=child_env.inherited() if env is None else env, start_new_session=True, **kwargs)
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
        _recorded[process.pid] = (path, started)
        forget = asyncio.get_running_loop().create_task(_forget(process, path, started))
        _forgetting.add(forget)
        forget.add_done_callback(_forgetting.discard)
    return process


async def _forget(process: asyncio.subprocess.Process, path: Path, started: str) -> None:
    await process.wait()
    if process.pid in _ending:
        return
    try:
        if json.loads(path.read_text(encoding="utf-8")).get("started") == started:  # not a later agent's pid
            path.unlink()
    except (OSError, ValueError):
        pass
    _recorded.pop(process.pid, None)


async def sweep(runtime_root: Path) -> None:
    """End what agents of a previous server on ``runtime_root`` left running, then record this server's agents.

    A record names an agent's pid and start time and the server that started
    it. One whose server still runs (another server on this runtime root) is
    left alone. Otherwise its agent's tree is ended before the record is
    removed, when its pid still runs with the same start time: the same process,
    orphaned by a server that crashed or was killed. A pid that no longer
    runs, or that now belongs to another process, is left alone. A record also
    lists the tree an end was ending, so a server that exited in the middle
    of an end has the rest ended at the next start.
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
    ends: dict[Path, list[tuple[_Orphan, str]]] = {}
    keep: set[Path] = set()
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
        agents: list[tuple[_Orphan, str]] = []
        if _start_time(pid) == started:
            orphans.append(pid)
            agents.append((_Orphan(pid, started), f"orphaned agent {pid}"))
        try:
            tree = {int(entry["pid"]): str(entry["started"]) for entry in record.get("tree", [])}
        except (ValueError, KeyError, TypeError):
            tree = {}
        current = await asyncio.to_thread(_start_times, tree)
        if current is None:
            logger.warning("cannot read start times for agent record %s; keeping it for the next sweep", path.name)
            keep.add(path)
        else:
            agents.extend((_Orphan(p, start), f"orphaned process {p} of agent {pid}")
                          for p, start in tree.items() if current.get(p) == start
                          and not (p == pid and pid in orphans))
        if agents:
            ends[path] = agents
        elif path not in keep:
            path.unlink(missing_ok=True)
    if orphans:
        logger.warning("ending %d agent(s) a previous server left running: pid %s", len(orphans),
                       ", ".join(map(str, orphans)))

    async def end_record(path: Path, agents: list[tuple[_Orphan, str]]) -> None:
        await asyncio.gather(*(_end(agent, label=label, known={}) for agent, label in agents))
        if path not in keep:
            path.unlink(missing_ok=True)

    await asyncio.gather(*(end_record(path, agents) for path, agents in ends.items()))


# --- One turn's tree, followed while the turn runs ---


class TurnTree:
    """The tree of one turn's agent process, snapshotted when the turn starts and refreshed while it runs, so a
    stop can end every process the turn ever started, even those whose parent has exited since.

    ``seen`` maps each observed pid to its start time (None if it could not be read). Every refresh checks
    these identities before expanding the tree; a reused pid stays excluded for the rest of the turn."""

    def __init__(self, process: Any, label: str) -> None:
        self.process = process
        self.label = label
        self.seen: dict[int, str | None] = {}
        self._groups: Tree = {}
        self._excluded: set[int] = set()
        self._refreshed = float("-inf")
        self._lock = asyncio.Lock()
        self._ticker: asyncio.Task[None] | None = None

    @classmethod
    def track(cls, process: Any, *, label: str) -> TurnTree:
        tree = cls(process, label)
        tree._merge(_tree(process, _snapshot(), {}))
        tree.seen.update(_start_times(tree.seen) or {})
        return tree

    def _merge(self, tree: Tree) -> list[int]:
        fresh = [pid for pid in tree if pid not in self.seen]
        self._groups.update(tree)
        for pid in fresh:
            self.seen[pid] = None
        return fresh

    async def refresh(self, *, force: bool = False, skip: frozenset[int] = frozenset()) -> None:
        """Merge the current tree into ``seen``; at most once per ``_REFRESH_MIN_SEC`` unless ``force``."""
        async with self._lock:
            if not force and time.monotonic() - self._refreshed < _REFRESH_MIN_SEC:
                return
            table = await asyncio.to_thread(_snapshot)
            skip = skip | await self._reused(table)
            # A leader's exit does not invalidate an observed member's own identity.
            groups = {pid: pgid for pid, pgid in self._groups.items() if pid in table and pid not in skip}
            observed = _tree(self.process, table, groups, skip=skip)
            unread = [pid for pid in self.seen.keys() | observed.keys()
                      if self.seen.get(pid) is None and pid not in skip]
            starts = (await asyncio.to_thread(_start_times, unread) or {}) if unread else {}
            # A stopped ticker can be cancelled at either lookup. Publish new pids with their identities
            # together, so a cancelled refresh cannot leave them permanently excluded as unknown.
            self._groups = groups
            self._merge(observed)
            for pid in unread:
                if pid in starts:
                    self.seen[pid] = starts[pid]
            self._refreshed = time.monotonic()

    def start_ticker(self, interval: float = 0.5) -> None:
        if self._ticker is None or self._ticker.done():
            self._ticker = asyncio.get_running_loop().create_task(self._tick(interval))

    async def _tick(self, interval: float) -> None:
        while True:
            try:
                await self.refresh()
            except Exception:
                logger.warning("%s: cannot refresh the process tree", self.label, exc_info=True)
            await asyncio.sleep(interval)

    async def stop_ticker(self) -> None:
        ticker, self._ticker = self._ticker, None
        if ticker is not None:
            ticker.cancel()
            await asyncio.gather(ticker, return_exceptions=True)

    async def _reused(self, table: Table | None = None) -> frozenset[int]:
        """Reject reused identities durably, and unread identities for this observation. Never replace a
        missing initial identity with a later process's start time just because its pid is still present."""
        known = {pid: started for pid, started in self.seen.items() if started is not None}
        current = await asyncio.to_thread(_start_times, known)
        if current is None:
            return frozenset(self.seen) | self._excluded | {self.process.pid}
        self._excluded.update(pid for pid, started in known.items() if pid in current and current[pid] != started)
        missing = self.seen.keys() - current.keys()
        root = self.process.pid
        if root in known and self.process.returncode is not None and root not in current:
            if table is None:
                table = await asyncio.to_thread(_snapshot)
            if root not in table:
                # A reaped root still owns its surviving group; an unread live pid does not prove ownership.
                missing.discard(root)
        return frozenset(self._excluded | missing
                         | ({self.process.pid} if self.seen.get(self.process.pid) is None else set()))

    async def begin_end(self) -> None:
        """Refresh and record the tree before the agent is asked to exit on its own: its record (what the next
        start's ``sweep`` ends) stays until ``end`` finishes, though the agent exits first."""
        await self.refresh(force=True)
        if self.process.pid in _recorded:
            _ending.add(self.process.pid)
            await _record_tree(self.process.pid, dict(self._groups))

    async def _strays(self, exclude: frozenset[int]) -> list[_Orphan]:
        """The seen processes the tree no longer reaches (one that left its group, setsid, once its parent exited):
        each still running with the start time the snapshot read, so a reused pid is never among them."""
        reached = _tree(self.process, await asyncio.to_thread(_snapshot), self._groups, skip=exclude)
        known = {pid: started for pid, started in self.seen.items()
                 if started is not None and pid not in reached and pid not in exclude and pid != self.process.pid}
        current = await asyncio.to_thread(_start_times, known) or {}
        return [_Orphan(pid, started) for pid, started in known.items() if current.get(pid) == started]

    async def _end_all(self, exclude: frozenset[int]) -> None:
        """End the tree, and beside it (not delaying its first signal) the seen processes it no longer reaches."""
        async def end_agent() -> None:
            # With no identity, only the live subprocess handle is safe: never infer a group from its pid.
            if (self.process.pid in exclude and self.process.pid not in self._excluded
                    and self.process.returncode is None):
                try:
                    self.process.send_signal(signal.SIGTERM)
                    await asyncio.wait_for(self.process.wait(), TERM_GRACE_SEC)
                except (asyncio.TimeoutError, ProcessLookupError, PermissionError):
                    pass
                finally:
                    if self.process.returncode is None:
                        try:
                            self.process.send_signal(signal.SIGKILL)
                        except (ProcessLookupError, PermissionError):
                            pass

        async def end_strays() -> None:
            strays = await self._strays(exclude)
            await asyncio.gather(*(_end(stray, label=f"{self.label} (left the tree)", known={}, exclude=exclude)
                                   for stray in strays))

        ending = [asyncio.create_task(coro) for coro in (
            _end(self.process, label=self.label, known=dict(self._groups), exclude=exclude),
            end_strays(), end_agent())]
        try:
            await asyncio.gather(*ending)
        finally:
            # gather propagates cancellation before every child's asynchronous cleanup has finished.
            await asyncio.gather(*ending, return_exceptions=True)

    async def end(self) -> None:
        """End every process of the tree seen so far and of the tree now, then reap the agent."""
        try:
            try:
                await self.stop_ticker()
                await self.refresh(force=True)
                exclude = await self._reused()
            except asyncio.CancelledError:
                # begin_end may already own the exited agent's record. Finish cleanup even when preparation
                # is cancelled, checking identities again before signalling anything from the saved tree.
                await self._end_all(await self._reused())
                raise
            await self._end_all(exclude)
        finally:
            await _reap(self.process, self.label)


def track(process: Any, *, label: str = "agent") -> TurnTree:
    """Snapshot ``process``'s tree now, to follow while its turn runs and to end from."""
    return TurnTree.track(process, label=label)
