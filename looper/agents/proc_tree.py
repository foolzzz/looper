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
import secrets
import signal
import subprocess
import sys
import threading
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
    await _end(process, label=label, known=known or {})
    await _reap(process, label)


async def _reap(process: Any, label: str) -> None:
    """Wait for the killed agent, for at most ``REAP_SEC``: a turn never hangs on a process that will not be reaped."""
    try:
        await asyncio.wait_for(process.wait(), REAP_SEC)
    except asyncio.TimeoutError:
        logger.warning("%s: pid %s was not reaped within %.1f s of SIGKILL", label, process.pid, REAP_SEC)


async def _end(process: _Agent, *, label: str, known: Tree, exclude: frozenset[int] = frozenset(),
               spare: frozenset[int] = frozenset(), grace: float | None = None) -> None:
    """End the tree, recording it so the next start can finish an end cut short by the server's exit. Processes
    in ``exclude`` are never signalled, nor those one of ``spare`` started (``_started_by``); ``grace`` replaces
    ``TERM_GRACE_SEC``."""
    recorded = _recorded.get(process.pid)
    if recorded is not None:
        _ending.add(process.pid)

    def members(table: Table, known: Tree) -> Tree:
        left = exclude | _started_by(table, spare)
        return {pid: pgid for pid, pgid in _tree(process, table, known).items() if pid not in left}

    seen = members(_snapshot(), known)
    alive = dict(seen)
    try:
        if recorded is not None:
            path, started = recorded
            await _record_tree(process.pid, seen)
        if alive:
            if (gone := await _gone(process, label)) is not None:
                alive.pop(process.pid, None)  # exited: what it left is still signalled; reused: nothing is
                if gone == "reused" or not alive:
                    alive = {}
                    return
            _signal(process, alive, signal.SIGTERM)
        deadline = time.monotonic() + (TERM_GRACE_SEC if grace is None else grace)
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
        if alive and (gone := await _gone(process, label)) is not None:
            alive = {} if gone == "reused" else {pid: pgid for pid, pgid in alive.items() if pid != process.pid}
        if alive:
            _signal(process, alive, signal.SIGKILL)
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


async def _gone(process: _Agent, label: str) -> str | None:
    """Whether ``process``, signalled by pid (``_Orphan``), no longer runs as the process its start time names:
    "exited", or "reused" (its pid now another process's); None while it does. Checked just before each signal."""
    if not isinstance(process, _Orphan) or process.started is None:
        return None
    current = await asyncio.to_thread(_start_time, process.pid)
    if current is None:
        logger.info("%s: pid %s exited", label, process.pid)
        return "exited"
    if current != process.started:
        logger.warning("%s: pid %s was reused; leaving it alone", label, process.pid)
        return "reused"
    return None


def _started_by(table: Table, spare: Iterable[int]) -> set[int]:
    """The processes of ``table`` that one of ``spare`` started, or one they started, and so on (by parent link):
    what a process an earlier turn left running starts is not this turn's."""
    spare = set(spare)
    if not spare:
        return set()
    children: defaultdict[int, list[int]] = defaultdict(list)
    for pid, (ppid, _) in table.items():
        children[ppid].append(pid)
    found: set[int] = set()
    pending = [child for parent in spare for child in children[parent]]
    while pending:
        pid = pending.pop()
        if pid in found or pid in spare:
            continue
        found.add(pid)
        pending.extend(children[pid])
    return found


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
    """The start times ``ps`` lists for ``pids``, in UTC; None when ``ps`` cannot run. More than one pid is read
    from a listing of every process: macOS ``ps -p`` with several pids takes about 0.3 s, ``ps -A`` 0.03 s."""
    wanted = set(pids)
    if not wanted:
        return {}
    selected = ["-p", str(next(iter(wanted)))] if len(wanted) == 1 else ["-A"]
    try:
        listed = subprocess.run(["ps", "-o", "pid=,lstart=", *selected], capture_output=True, text=True,
                                timeout=_PS_TIMEOUT_SEC, check=False, env={**child_env.inherited(), "LC_ALL": "C", "TZ": "UTC"})
    except (OSError, subprocess.SubprocessError):
        return None
    started: dict[int, str] = {}
    for line in listed.stdout.splitlines():
        fields = line.split(maxsplit=1)
        if len(fields) != 2:
            continue
        try:
            pid = int(fields[0])
        except ValueError:
            continue
        if pid in wanted:
            started[pid] = " ".join(fields[1].split())
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
    until it is reaped (once ``sweep`` has run). Without ``env``, start with ``child_env.inherited()``. On Linux the
    process gets a mark of its own (``OWNER_ENV``) that everything it starts inherits."""
    environment = dict(child_env.inherited() if env is None else env)
    owner = None
    if _MARKS:
        owner = environment[OWNER_ENV] = secrets.token_hex(12)
    process = await asyncio.create_subprocess_exec(*argv, env=environment, start_new_session=True, **kwargs)
    if owner is not None:
        _owners[process.pid] = owner
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


# --- What an agent owns: every process it started, whatever group or session that joined since ---
#
# The tree (``_tree``) reaches a process through its parent or its group; a background command that leaves the
# group at once (``setsid``) while its parent exits has neither once the parent is gone, and a refresh that comes
# after that never sees it. So a turn's agent also owns its descendants through what the platform keeps for each
# process:
#
# - Linux: an environment variable the agent gets at spawn (``OWNER_ENV``), inherited by everything it starts,
#   whether or not its ancestors still run, read from ``/proc/<pid>/environ``. This is the guarantee: a detached
#   daemon (double fork, setsid, its ancestors gone) is ended with the turn.
# - macOS, best effort: the kernel gives every process an id version that is never reused, and keeps the version of
#   the process that created it (``p_orig_ppidversion``) through re-parenting and ``setsid``. A kqueue watch on each
#   descendant (``NOTE_FORK``) records every new process's version as its parent creates it, so a command that
#   exits at once still links the processes it started to the agent. That watch is a race: a short-lived shell can
#   exit before its children are listed, and the version that links its children is lost. Such a daemon then runs
#   on, as one started from a terminal survives its Ctrl-C (``docs/architecture/rfcs/chat-stop-v2.zh-CN.md`` 6.6).
#   No process is added to any spawn for this.
#
# Elsewhere, or for a process ``spawn`` did not start (Linux), the tree alone decides.

OWNER_ENV = "LOOPER_PROC_OWNER"
_MARKS = sys.platform.startswith("linux") and Path("/proc/self/environ").exists()
_owners: dict[int, str] = {}  # agent pid -> its mark, set by ``spawn`` (Linux)


def _libproc() -> Any:
    if sys.platform != "darwin":
        return None
    try:
        import ctypes

        # Each call is a short system call: made holding the GIL, so a busy server thread does not stretch a
        # listing of every process (one call each) past the life of a shell that starts something and exits.
        lib = ctypes.PyDLL("/usr/lib/libproc.dylib")
        lib.proc_pidinfo.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint64, ctypes.c_void_p, ctypes.c_int]
        lib.proc_listallpids.argtypes = [ctypes.c_void_p, ctypes.c_int]
        lib.proc_listchildpids.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_int]
        return lib
    except (OSError, AttributeError):
        return None


_LIBPROC = _libproc()
_PIDUNIQIDENTIFIERINFO = 17  # proc_pidinfo flavor: struct proc_uniqidentifierinfo (56 bytes)
_UNIQ_SIZE = 56


def _identity(pid: int) -> tuple[int, int, int] | None:
    """macOS: ``pid``'s (unique id, id version, its creator's id version); None when it does not run or cannot be
    read. The unique id stays through ``exec``; the version changes."""
    import ctypes

    buffer = ctypes.create_string_buffer(_UNIQ_SIZE)
    if _LIBPROC.proc_pidinfo(pid, _PIDUNIQIDENTIFIERINFO, 0, buffer, _UNIQ_SIZE) != _UNIQ_SIZE:
        return None
    raw = buffer.raw
    return (int.from_bytes(raw[16:24], sys.byteorder), int.from_bytes(raw[32:36], sys.byteorder),
            int.from_bytes(raw[36:40], sys.byteorder))


def _versions(pid: int) -> tuple[int, int] | None:
    """macOS: ``pid``'s (id version, its creator's id version); None when it does not run or cannot be read."""
    identity = _identity(pid)
    return identity[1:] if identity is not None else None


def _listed(call: Callable[[Any, int], int]) -> list[int]:
    import ctypes

    size = 4096
    while True:
        pids = (ctypes.c_int * size)()
        count = call(pids, ctypes.sizeof(pids))
        if count < 0:
            return []
        if count < size:
            return [pids[i] for i in range(count) if pids[i] > 0]
        size *= 2


def _all_pids() -> list[int]:
    return _listed(_LIBPROC.proc_listallpids)


def _children(pid: int) -> list[int]:
    return _listed(lambda buffer, size: _LIBPROC.proc_listchildpids(pid, buffer, size))


def _readable_versions() -> bool:
    if _LIBPROC is None:
        return False
    try:
        own = _versions(os.getpid())
    except Exception:
        return False
    return own is not None and own[0] != 0 and own[1] != 0


class _Owned(Protocol):
    def live(self) -> set[int]: ...

    def close(self) -> None: ...


class _Lineage:
    """macOS: the agent's descendants by fork lineage. A thread waits on a kqueue for any watched process to
    fork; the forking process's children are then recorded and watched at once (and theirs, for a fork that came
    before its parent was watched), and one listing of every process (at most once per ``_POLL_SEC``) records each
    whose creator's version is recorded. Versions are never reused, so a recorded process that exited still links
    what it started. Without a fork, nothing is listed."""

    _POLL_SEC = 0.25

    def __init__(self, pid: int, label: str) -> None:
        import select

        self.label = label
        self._root = pid
        self._root_id: int | None = None
        self._versions: set[int] = set()
        self._known: dict[int, int] = {}  # pid -> version of each recorded process seen running
        self._watched: set[int] = set()
        self._scanned = float("-inf")
        self._lock = threading.Lock()
        self._closed = threading.Event()
        self._kq = select.kqueue()
        identity = _identity(pid)
        if identity is None:
            self._kq.close()
            self._closed.set()
            return
        self._root_id = identity[0]
        self._versions.add(identity[1])
        self._known[pid] = identity[1]
        self._watch(pid)
        self._scan()
        if pid not in self._watched:  # it exited already: nothing more to follow
            self.close()
            return
        self._thread = threading.Thread(target=self._follow, name=f"proc-lineage-{pid}", daemon=True)
        self._thread.start()

    def _watch(self, pid: int) -> None:
        import select

        if pid in self._watched or self._closed.is_set():
            return
        try:
            self._kq.control([select.kevent(pid, select.KQ_FILTER_PROC, select.KQ_EV_ADD,
                                            select.KQ_NOTE_FORK | select.KQ_NOTE_EXIT)], 0, 0)
        except OSError:
            return  # it exited already; what it started is still found by its version
        self._watched.add(pid)

    def _adopt(self, parent: int) -> None:
        """Record ``parent``'s version now (what it forked names it, since its last ``exec``), then record and watch
        its children, then theirs (forked before they were watched)."""
        with self._lock:
            if (versions := _versions(parent)) is not None and parent in self._known:
                self._versions.add(versions[0])
            pending = [parent]
            while pending:
                for child in _children(pending.pop()):
                    versions = _versions(child)
                    if versions is None or self._known.get(child) == versions[0]:
                        continue
                    self._versions.add(versions[0])
                    self._known[child] = versions[0]
                    self._watch(child)
                    pending.append(child)

    def _scan(self) -> set[int]:
        """Record every running process created by a recorded one, until none is left; return the running ones."""
        with self._lock:
            self._scanned = time.monotonic()
            table: dict[int, tuple[int, int]] = {}
            for pid in _all_pids():
                if (versions := _versions(pid)) is not None:
                    table[pid] = versions
            owned: set[int] = set()
            root = _identity(self._root)
            if root is not None and root[0] == self._root_id:  # the agent runs: its version since its last exec
                self._versions.add(root[1])
            grew = True
            while grew:
                grew = False
                for pid, (version, creator) in table.items():
                    if pid not in owned and (version in self._versions or creator in self._versions):
                        owned.add(pid)
                        if version not in self._versions:
                            self._versions.add(version)
                            grew = True
            for pid in owned:
                self._known[pid] = table[pid][0]
                self._watch(pid)
            self._watched &= owned
            self._known = {pid: version for pid, version in self._known.items() if pid in owned}
            return owned

    def _follow(self) -> None:
        import select

        pending = False  # a fork came since the last listing
        try:
            while not self._closed.is_set():
                try:
                    events = self._kq.control(None, 64, self._POLL_SEC)
                except OSError:
                    return
                forked = [event.ident for event in events if event.fflags & select.KQ_NOTE_FORK]
                for parent in forked:
                    self._adopt(parent)
                pending = pending or bool(forked)
                exited = any(event.ident == self._root and event.fflags & select.KQ_NOTE_EXIT for event in events)
                if exited or (pending and time.monotonic() - self._scanned >= self._POLL_SEC):
                    self._scan()
                    pending = False
                if exited:
                    return  # once the agent has exited nothing new is followed; what it left stays owned
        except Exception:
            logger.warning("%s: cannot follow the agent's processes", self.label, exc_info=True)
        finally:
            with self._lock:
                self._closed.set()
                self._kq.close()

    def live(self) -> set[int]:
        """The running processes the agent started, itself excluded."""
        return self._scan() - {self._root}

    def close(self) -> None:
        self._closed.set()


class _Marked:
    """Linux: the processes whose environment (``/proc/<pid>/environ``) carries the agent's mark."""

    def __init__(self, pid: int, mark: str) -> None:
        self._root = pid
        self._entry = f"{OWNER_ENV}={mark}".encode()

    def live(self) -> set[int]:
        owned: set[int] = set()
        for entry in os.scandir("/proc"):
            if not entry.name.isdigit() or int(entry.name) == self._root:
                continue
            try:
                with open(f"/proc/{entry.name}/environ", "rb") as environ:
                    if self._entry in environ.read().split(b"\0"):
                        owned.add(int(entry.name))
            except OSError:
                continue
        return owned

    def close(self) -> None:
        pass


_LINEAGE = _readable_versions()
FOLLOWS_OWNERSHIP = _LINEAGE or _MARKS  # whether this platform has a facility ``_owned`` can use


def _owned(process: Any, label: str) -> _Owned | None:
    """How ``process``'s descendants are followed here, or None where only its tree is."""
    if _MARKS and (mark := _owners.pop(process.pid, None)) is not None:
        return _Marked(process.pid, mark)
    if _LINEAGE:
        try:
            return _Lineage(process.pid, label)
        except Exception:
            logger.warning("%s: cannot follow the agent's processes", label, exc_info=True)
    return None


# --- One turn's tree, followed while the turn runs ---


class TurnTree:
    """The tree of one turn's agent process, snapshotted when the turn starts and refreshed while it runs, so a
    stop can end every process the turn ever started, even those whose parent has exited since. Where the
    platform allows, the agent's descendants are also followed from their creation (``_owned``), so one that left
    the tree before any refresh is ended too (on macOS, best effort).

    ``seen`` maps each pid of the tree to its start time (None until a refresh has read it); ``end`` leaves a
    pid whose start time has changed alone, since the pid was reused."""

    def __init__(self, process: Any, label: str) -> None:
        self.process = process
        self.label = label
        self.seen: dict[int, str | None] = {}
        self._groups: Tree = {}
        self._baseline: set[int] = set()
        self._refreshed = float("-inf")
        self._listed: set[int] = set()  # the pids the last refresh's listing had
        self._lock = asyncio.Lock()
        self._ticker: asyncio.Task[None] | None = None
        self._owned: _Owned | None = None

    @classmethod
    def track(cls, process: Any, *, label: str) -> TurnTree:
        tree = cls(process, label)
        tree._owned = _owned(process, label)
        tree._merge(_tree(process, _snapshot(), {}))
        return tree

    def _live_owned(self) -> set[int]:
        if self._owned is None:
            return set()
        try:
            return self._owned.live()
        except Exception:
            logger.warning("%s: cannot list the agent's processes", self.label, exc_info=True)
            return set()

    def _merge(self, tree: Tree) -> list[int]:
        fresh = [pid for pid in tree if pid not in self.seen]
        self._groups.update(tree)
        for pid in fresh:
            self.seen[pid] = None
        return fresh

    async def refresh(self, *, force: bool = False) -> None:
        """Merge the current tree into ``seen``; at most once per ``_REFRESH_MIN_SEC`` unless ``force``."""
        async with self._lock:
            if not force and time.monotonic() - self._refreshed < _REFRESH_MIN_SEC:
                return
            table = await asyncio.to_thread(_snapshot)
            self._merge(_tree(self.process, table, self._groups))
            self._listed = set(table)
            # One that exited before its start time was read can never be told from a reused pid: forgotten, not
            # asked for again at every refresh (``ps -p`` with many pids takes long).
            for pid in [pid for pid, started in self.seen.items() if started is None and pid not in table]:
                del self.seen[pid]
            unread = [pid for pid, started in self.seen.items() if started is None]
            if unread:
                starts = await asyncio.to_thread(_start_times, unread) or {}
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

    async def baseline(self) -> None:
        """Remember the current members, and what the agent started earlier that still runs; ``end_new`` leaves
        them, and what they start, running. (The listings run off the event loop.)"""
        table = await asyncio.to_thread(_snapshot)
        self._merge(_tree(self.process, table, self._groups))
        self._baseline = set(self.seen) | await asyncio.to_thread(self._live_owned)

    async def _reused(self, exclude: frozenset[int] = frozenset()) -> frozenset[int]:
        """The seen pids now another process's: of those the last refresh listed (one that exited is no one's)."""
        known = {pid: started for pid, started in self.seen.items()
                 if started is not None and pid not in exclude and pid in self._listed}
        current = await asyncio.to_thread(_start_times, known)
        if current is None:
            return frozenset()
        return frozenset(pid for pid, started in known.items() if pid in current and current[pid] != started)

    async def begin_end(self) -> None:
        """Refresh and record the tree before the agent is asked to exit on its own: its record (what the next
        start's ``sweep`` ends) stays until ``end`` finishes, though the agent exits first."""
        await self.refresh(force=True)
        if self.process.pid in _recorded:
            _ending.add(self.process.pid)
            await _record_tree(self.process.pid, dict(self._groups))

    async def _strays(self, exclude: frozenset[int], spare: frozenset[int], *, ended: bool = False) -> list[_Orphan]:
        """The processes of the turn the tree no longer reaches (one that left its group, setsid, once its parent
        exited): each seen one still running with the start time the snapshot read, so a reused pid is never among
        them, and each the agent owns (``_owned``) that runs now, whether or not the snapshot lists it. None in
        ``exclude``, nor one that a process of ``spare`` started. Once every end has finished (``ended``), each
        still running is one: none is left to the tree's end, and one the snapshot reached may have left the tree
        since."""
        table = await asyncio.to_thread(_snapshot)
        reached = {} if ended else _tree(self.process, table, self._groups)
        skip = exclude | _started_by(table, spare) | {self.process.pid}
        known = {pid: started for pid, started in self.seen.items()
                 if started is not None and pid in table and pid not in reached and pid not in skip}
        current = await asyncio.to_thread(_start_times, known) or {}
        strays = [_Orphan(pid, started) for pid, started in known.items() if current.get(pid) == started]
        owned = await asyncio.to_thread(self._live_owned)
        # Every one it owns, not only those the snapshot lists: one started since (a setsid command a shell starts
        # as it is told to exit) would otherwise be missed, and the end finish while it runs.
        unseen = [pid for pid in owned if pid not in reached and pid not in skip and pid not in known]
        if unseen:
            starts = await asyncio.to_thread(_start_times, unseen) or {}
            strays += [_Orphan(pid, starts[pid]) for pid in unseen if pid in starts]
        return strays

    async def _end_all(self, exclude: frozenset[int]) -> None:
        """End the tree, and beside it (not delaying its first signal) the processes it no longer reaches: those
        running now and each that appears meanwhile (a command a tool's shell starts in a session of its own as it
        is told to exit). Once every end has finished, each still running gets one more end, as it may have left
        the tree since a listing reached it. What one of ``exclude`` (the agent aside) starts is left alone too.

        Bounded: every stray end's SIGKILL comes by ``TERM_GRACE_SEC`` after the first signals, and the looking
        stops ``TERM_GRACE_SEC + REAP_SEC`` after them, or once only processes ended twice already are left (one
        SIGKILL does not end: EPERM, uninterruptible sleep); what still runs then is logged and left."""
        spare = exclude - {self.process.pid}
        if self.process.pid not in exclude:
            # Record ownership before a root that can exit is signalled. A resident agent stays alive: signal its
            # descendants first, without delaying them for an ownership scan of every process.
            await asyncio.to_thread(self._live_owned)
        began = time.monotonic()
        killed_by, deadline = began + TERM_GRACE_SEC, began + TERM_GRACE_SEC + REAP_SEC
        ends = [asyncio.ensure_future(_end(self.process, label=self.label, known=dict(self._groups),
                                           exclude=exclude, spare=spare))]
        tries: dict[int, int] = {}  # pid -> how many stray ends it was given
        try:
            while True:
                ended = all(end.done() for end in ends)
                strays = await self._strays(exclude, spare, ended=ended)
                now = time.monotonic()
                if now >= deadline:
                    if strays:
                        logger.warning("%s: %d process(es) still running %.1f s after the stop began, left running: "
                                       "pid %s", self.label, len(strays), now - began,
                                       ", ".join(str(stray.pid) for stray in strays))
                    break
                fresh = [stray for stray in strays if tries.get(stray.pid, 0) < (2 if ended else 1)]
                if ended and not fresh:
                    if strays:
                        logger.warning("%s: %d process(es) survived SIGKILL, left running: pid %s", self.label,
                                       len(strays), ", ".join(str(stray.pid) for stray in strays))
                    break
                for stray in fresh:
                    tries[stray.pid] = tries.get(stray.pid, 0) + 1
                ends += [asyncio.ensure_future(_end(stray, label=f"{self.label} (left the tree)", known={},
                                                    exclude=exclude, spare=spare, grace=max(0.0, killed_by - now)))
                         for stray in fresh]
                await asyncio.sleep(_POLL_SEC)
        except BaseException:
            for end in ends:
                end.cancel()  # each kills what is left of its tree at once
            await asyncio.gather(*ends, return_exceptions=True)
            raise
        await asyncio.gather(*ends)

    async def end(self) -> None:
        """End every process of the tree seen so far and of the tree now, and every one the agent owns, then reap
        the agent."""
        await self.stop_ticker()
        await self.refresh(force=True)
        try:
            await self._end_all(await self._reused())
        finally:
            if self._owned is not None:
                self._owned.close()
        await _reap(self.process, self.label)

    async def end_new(self) -> None:
        """End the members that are not in the baseline; the agent, the baseline members and what they start
        survive."""
        await self.refresh(force=True)
        exclude = frozenset(self._baseline | {self.process.pid})
        await self._end_all(exclude | await self._reused(exclude))


def track(process: Any, *, label: str = "agent") -> TurnTree:
    """Snapshot ``process``'s tree now, to follow while its turn runs and to end from."""
    return TurnTree.track(process, label=label)
