"""Host-owned runner processes and bounded, generation-bound session leases."""
from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
import errno
import logging
import os
from pathlib import Path
import re
import shutil
import time
import uuid

from ... import proc_tree
from ...log_redaction import add_secret, redact
from ..per_turn import session_dir_name
from .bridge import CloseState, ConnectionLost, RpcFailure, RunnerBridge
from .protocol import ErrorCode, MAX_FRAME_BYTES, ProtocolError

logger = logging.getLogger(__name__)
BACKOFF_BASE = 1.0
BACKOFF_MAX = 60.0
FAILURE_WINDOW = 600.0
MAX_FAILURES = 5
SHUTDOWN_TIMEOUT = 5.0
DIAGNOSTIC_BYTES = 8192
STDERR_LINES_PER_SECOND = 10
STDERR_DRAIN_TIMEOUT = 1.0
_CONFIGURATION_ERROR = "runner configuration or protocol version is invalid"
MAX_RUNNERS_PER_KIND = 4
IDLE_STOP_SECONDS = 300.0
_KIND = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_-]{0,63}")
_ENV = frozenset({"PATH", "LANG", "LANGUAGE", "TZ", "LC_ALL", "LC_CTYPE", "LC_NUMERIC",
    "LC_TIME", "LC_COLLATE", "LC_MONETARY", "LC_MESSAGES", "LC_PAPER", "LC_NAME",
    "LC_ADDRESS", "LC_TELEPHONE", "LC_MEASUREMENT", "LC_IDENTIFICATION"})


class RunnerUnavailable(ConnectionLost):
    """The configured runner cannot currently accept a new session."""


def _private(path: Path) -> Path:
    path.mkdir(mode=0o700, exist_ok=True)
    if path.is_symlink() or not path.is_dir():
        raise ValueError("runner directory must be a private directory")
    path.chmod(0o700)
    return path


def _exit_signal(process: asyncio.subprocess.Process) -> asyncio.Event:
    # Process.wait() can wait for inherited pipes after the parent has exited.
    # asyncio exposes the raw exit only through its subprocess protocol callback.
    event = asyncio.Event()
    protocol = process._transport.get_protocol()
    previous = protocol.process_exited

    def exited():
        protocol.process_exited = previous
        try:
            previous()
        finally:
            event.set()

    protocol.process_exited = exited
    if process.returncode is not None:
        protocol.process_exited = previous
        event.set()
    return event


async def _joined(task):
    # Cancellation of a caller never abandons a spawn, close or admitted write.
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        await asyncio.shield(asyncio.gather(task, return_exceptions=True))
        raise


@dataclass(eq=False)
class RunnerLease:
    bridge: RunnerBridge
    runner_id: str
    session_key: str
    directory: Path
    home: Path
    state_dir: Path
    agent_dir: Path
    _owner: RunnerSupervisor = field(repr=False)
    _generation: _Generation = field(repr=False)
    session_id: str | None = field(default=None, repr=False)
    valid: bool = True
    _open_task: asyncio.Task | None = field(default=None, repr=False)
    _release_task: asyncio.Task | None = field(default=None, repr=False)

    async def open_session(self, params: Mapping) -> str:
        return await self._open_session(params, self.bridge.open_session)

    async def load_session(self, params: Mapping) -> str:
        return await self._open_session(params, self.bridge.load_session)

    async def _open_session(self, params: Mapping, method: Callable) -> str:
        """Inject host paths; cwd/add_dirs remain the caller's task worktree."""
        if not self.valid:
            raise RunnerUnavailable("runner lease is no longer available; resume was lost")
        if self._open_task is not None:
            raise RuntimeError("runner lease may open only one session")
        key = params.get("provider", {}).get("api_key")
        if isinstance(key, str):
            add_secret(key)
        try:
            self._open_task = self._owner._own(method({**params, "session_key": self.session_key,
                "home": str(self.home), "state_dir": str(self.state_dir)}))
            self.session_id = await self._open_task
            if not self.valid:
                raise RunnerUnavailable("runner generation was lost while opening session")
            return self.session_id
        except BaseException:
            await self.release()
            raise

    async def release(self) -> None:
        if self._release_task is None or self._release_task.done() and self in self._generation.leases:
            self.valid = False
            self._release_task = self._owner._own(self._owner._release(self))
        await _joined(self._release_task)


@dataclass
class _Kind:
    command: tuple[str, ...]
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    members: list[_Generation] = field(default_factory=list)
    number: int = 0
    failures: deque = field(default_factory=deque)
    last_start_failure: str | None = None
    fatal: str | None = None
    restart: asyncio.Task | None = None


@dataclass(eq=False)
class _Generation:
    kind: str
    state: _Kind
    directory: Path
    process: asyncio.subprocess.Process
    tree: proc_tree.TurnTree | None = None
    exited: asyncio.Event = field(default_factory=asyncio.Event)
    bridge: RunnerBridge | None = None
    cap: int = 1
    leases: set[RunnerLease] = field(default_factory=set)
    stderr: asyncio.Task | None = None
    waiter: asyncio.Task | None = None
    failure: asyncio.Task | None = None
    failed: bool = False
    stopping: bool = False
    idle: asyncio.Task | None = None
    diagnostics: deque[bytes] = field(default_factory=deque, repr=False)


class RunnerSupervisor:
    def __init__(self, runtime_root: Path, commands: Mapping[str, Sequence[str | Path]], *,
                 session_cap: int = 4,
                 env: Mapping[str, Mapping[str, str]] | None = None,
                 startup_error: Callable[[str], str] | None = None,
                 connect: Callable[[asyncio.subprocess.Process, str], Awaitable[RunnerBridge]]):
        if type(session_cap) is not int or session_cap <= 0:
            raise ValueError("runner session cap must be positive")
        self.root = Path(runtime_root).absolute() / "runners"
        self.session_cap, self.connect = session_cap, connect
        self._startup_error = startup_error
        self._kinds: dict[str, _Kind] = {}
        for kind, argv in commands.items():
            if not isinstance(kind, str) or not _KIND.fullmatch(kind):
                raise ValueError("runner kind must be a safe identifier")
            if isinstance(argv, (str, bytes)) or not argv or not all(isinstance(a, (str, Path)) and a for a in argv):
                raise ValueError("runner command must be an explicit argv")
            if not Path(argv[0]).is_absolute():
                raise ValueError("runner executable must be absolute")
            # Path arguments explicitly declare files relative to the launch cwd; strings are opaque argv.
            command = tuple(str(a.absolute()) if isinstance(a, Path) else a for a in argv)
            self._kinds[kind] = _Kind(command)
        self._env: dict[str, dict[str, str]] = {}
        if env is not None and not isinstance(env, Mapping):
            raise ValueError("runner environment must be a per-kind mapping")
        for kind, values in (env or {}).items():
            if kind not in self._kinds:
                raise ValueError("runner environment kind is not configured")
            if not isinstance(values, Mapping):
                raise ValueError("runner environment must be a mapping")
            for name, value in values.items():
                if (not isinstance(name, str) or not re.fullmatch(r"[A-Z_][A-Z0-9_]*", name)
                        or name in {"PATH", "HOME", "TMPDIR", "TZ"} or name.startswith(("LANG", "LC_"))):
                    raise ValueError("runner environment name is invalid or reserved")
                if not isinstance(value, str) or "\0" in value:
                    raise ValueError("runner environment value must be a string without NUL")
            self._env[kind] = dict(values)
        self._stopped = False
        self._shutdown = asyncio.Event()
        self._stop_task: asyncio.Task | None = None
        self._tasks: set[asyncio.Task] = set()
        self._starts: set[asyncio.Task] = set()

    def _own(self, coroutine) -> asyncio.Task:
        task = asyncio.create_task(coroutine)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    @staticmethod
    def _trim(state: _Kind) -> None:
        while state.failures and state.failures[0] <= time.monotonic() - FAILURE_WINDOW:
            state.failures.popleft()

    async def acquire(self, kind: str, *, owner: str | None = None,
                      state_dir: Path | None = None) -> RunnerLease:
        if owner is not None and state_dir is not None:
            raise ValueError("runner owner and state_dir are mutually exclusive")
        if state_dir is not None and not Path(state_dir).is_absolute():
            raise ValueError("runner state_dir must be absolute")
        if self._stopped:
            raise RunnerUnavailable("runner supervisor is stopped")
        if kind not in self._kinds:
            raise ValueError("runner kind is not configured")
        owner_name = session_dir_name(owner) if owner is not None else None
        if owner is not None and (not owner or not owner_name or owner_name.startswith(".")):
            raise ValueError("runner owner must name a safe state directory")
        state = self._kinds[kind]
        while True:
            backoff = None
            async with state.lock:
                if self._stopped:
                    raise RunnerUnavailable("runner supervisor is stopped")
                # Reap dead members before counting the pool or selecting free slots.
                for member in list(state.members):
                    if member.process.returncode is not None or member.bridge._error:
                        self._failed(member)
                    if member.failed:
                        await self._retire(member)
                generation = max(state.members, key=lambda m: m.cap - len(m.leases), default=None)
                if generation is None or len(generation.leases) >= generation.cap:
                    if len(state.members) >= MAX_RUNNERS_PER_KIND:
                        total = sum(member.cap for member in state.members)
                        raise ProtocolError(ErrorCode.CAPACITY, f"CAPACITY: runner pool has {total} session slots; all are in use")
                    self._trim(state)
                    if state.fatal:
                        raise RunnerUnavailable(state.fatal)
                    if len(state.failures) >= MAX_FAILURES:
                        detail = f"; last start failure: {state.last_start_failure}" if state.last_start_failure else ""
                        raise RunnerUnavailable(f"runner unavailable: five failures within ten minutes{detail}")
                    if state.restart is not None and not state.restart.done():
                        backoff = state.restart
                    else:
                        # The kind lock covers the handshake and first reservation,
                        # so concurrent acquisitions cannot over-expand the pool.
                        starting = self._own(self._start(kind, state))
                        self._starts.add(starting)
                        starting.add_done_callback(self._starts.discard)
                        try:
                            generation = await asyncio.shield(starting)
                        except asyncio.CancelledError:
                            # The supervisor owns cleanup after a turn stops waiting,
                            # including abandon() cancelling the caller a second time.
                            if not starting.done():
                                if not starting.cancelling():
                                    starting.cancel()
                            elif not starting.cancelled() and starting.exception() is None:
                                # Cancellation can race with a successful handshake.
                                # Retire that unused member without counting a crash.
                                unused = starting.result()
                                state.members.remove(unused)
                                await _joined(self._own(self._dispose(unused)))
                            await asyncio.shield(asyncio.gather(starting, return_exceptions=True))
                            raise
                if backoff is None:
                    if self._stopped or generation.failed or generation.bridge._error:
                        raise RunnerUnavailable("runner generation is unavailable; resume was lost")
                    key = uuid.uuid4().hex
                    directory = _private(generation.directory / "sessions" / key)
                    try:
                        if state_dir is not None:
                            state_dir = Path(state_dir)
                            state_dir.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                            state_dir = _private(state_dir)
                        elif owner_name is not None:
                            state_dir = _private(_private(_private(self.root / "state") / kind) / owner_name)
                        else:
                            state_dir = _private(directory / "native")
                        lease = RunnerLease(generation.bridge, generation.directory.name, key, directory,
                            _private(directory / "home"), state_dir, _private(directory / "agent"),
                            self, generation)
                    except BaseException:
                        shutil.rmtree(directory)
                        raise
                    generation.leases.add(lease)  # Opening counts, before releasing the kind lock.
                    if generation.idle is not None:
                        generation.idle.cancel()
                        generation.idle = None
                    return lease
            # Wait without the kind lock, then recheck members and free slots:
            # a healthy member may have freed capacity during the backoff.
            await asyncio.shield(backoff)

    async def _start(self, kind: str, state: _Kind) -> _Generation:
        self.root.parent.mkdir(parents=True, exist_ok=True)
        _private(self.root)
        numbers = [int(m[1]) for path in self.root.iterdir()
                   if (m := re.fullmatch(re.escape(kind) + r"-([0-9]+)", path.name))]
        state.number = max([state.number, *numbers])
        while True:
            state.number += 1
            directory = self.root / f"{kind}-{state.number}"
            try:
                directory.mkdir(mode=0o700)
                break
            except FileExistsError:
                continue
        generation = None
        try:
            _private(directory / "sessions")
            tmp = _private(directory / "tmp")
            environment = {k: v for k, v in os.environ.items() if k in _ENV}
            environment.update(self._env.get(kind, {}))
            environment.update(HOME=str(directory), TMPDIR=str(tmp))
            spawning = self._own(proc_tree.spawn(*state.command, env=environment, cwd=directory,
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE, limit=MAX_FRAME_BYTES + 1))
            cancelled = None
            try:
                process = await asyncio.shield(spawning)
            except asyncio.CancelledError as error:
                # A spawn can create its process before returning the handle. Keep
                # ownership until it returns, then dispose instead of initializing.
                process = await asyncio.shield(spawning)
                cancelled = error
            generation = _Generation(kind, state, directory, process)
            generation.exited = _exit_signal(process)
            generation.tree = proc_tree.track(process, label=f"runner {kind}")
            generation.tree.start_ticker()
            generation.stderr = self._own(self._stderr(generation))
            if cancelled is not None:
                raise cancelled
            generation.bridge = await self.connect(process, kind)
            # Initialization can start children after the launch snapshot. Own
            # them before publishing a runner that may immediately be killed.
            await generation.tree.refresh(force=True)
            generation.bridge.on_failure = lambda cause: self._failed(generation)
            caps = generation.bridge.capabilities
            maximum = caps.get("max_sessions", 1)
            if type(maximum) is not int or maximum < 1:
                raise ValueError("runner max_sessions must be positive")
            generation.cap = min(self.session_cap, maximum) if caps.get("per_session_isolation") is True else 1
            if generation.bridge._error or process.returncode is not None:
                raise RunnerUnavailable("runner exited during initialization")
            state.members.append(generation)
            generation.waiter = self._own(self._wait(generation))
            self._idle(generation)
            logger.info("runner %s member %s started: sessions=%d capacity=%d pool_sessions=%d",
                        kind, directory.name.rsplit("-", 1)[1], len(generation.leases), generation.cap,
                        sum(len(member.leases) for member in state.members))
            return generation
        except asyncio.CancelledError:
            if generation is not None:
                await self._dispose(generation)
            raise
        except BaseException as error:
            # Timeouts and transient I/O failures use the same bounded restart path as a crash.
            fatal = (isinstance(error, (ValueError, ProtocolError)) or
                generation is None and isinstance(error, OSError) and error.errno in {
                    errno.ENOENT, errno.EACCES, errno.ENOEXEC} or
                isinstance(error, RpcFailure) and error.code in {
                    ErrorCode.VERSION_MISMATCH, ErrorCode.INVALID_PARAMS, ErrorCode.UNSUPPORTED,
                    ErrorCode.ISOLATION_REFUSED})
            if fatal:
                cause = _CONFIGURATION_ERROR
                if isinstance(error, RpcFailure) or generation is None and isinstance(error, OSError):
                    cause = redact(str(error)).replace("\r", " ").replace("\n", " ")[:512]
                state.fatal = _CONFIGURATION_ERROR
                if self._startup_error:
                    state.fatal = self._startup_error(cause)
                elif cause != _CONFIGURATION_ERROR:
                    state.fatal = f"{_CONFIGURATION_ERROR}: {cause}"
            if generation is not None:
                generation.stopping = True
                await self._dispose(generation)
            state.failures.append(time.monotonic())
            self._restart(kind, state)
            message = state.fatal or "runner failed to initialize; restart is bounded"
            if not state.fatal and self._startup_error:
                cause = str(error) or ("runner initialization timed out" if isinstance(error, TimeoutError)
                                       else type(error).__name__)
                cause = redact(cause).replace("\r", " ").replace("\n", " ")[:512]
                message = f"{self._startup_error(cause)}; restart is bounded"
            state.last_start_failure = message
            raise RunnerUnavailable(message) from None

    async def _wait(self, generation: _Generation) -> None:
        await generation.exited.wait()
        self._failed(generation)

    def _failed(self, generation: _Generation) -> None:
        if generation.failed:
            return
        generation.failed = True
        for lease in generation.leases:
            lease.valid = False
        if generation.bridge is not None:
            # Poison synchronously before reaping or allowing any replacement generation.
            generation.bridge._fail(ConnectionLost("runner-exited: generation lost"))
        if not generation.stopping:
            generation.failure = self._own(self._recover(generation))

    async def invalidate(self, lease: RunnerLease, cause: BaseException) -> None:
        generation = lease._generation
        state = generation.state
        async with state.lock:
            if generation not in state.members:
                return
            if (isinstance(cause, ValueError) and not isinstance(cause, ProtocolError)) or (
                    isinstance(cause, (ProtocolError, RpcFailure)) and cause.code == ErrorCode.VERSION_MISMATCH):
                state.fatal = _CONFIGURATION_ERROR
            self._failed(generation)
        if generation.failure:
            await _joined(generation.failure)

    async def _retire(self, generation: _Generation) -> None:
        # Caller holds the kind lock; a queued recovery may find it already reaped.
        state = generation.state
        if generation not in state.members:
            return
        await self._dispose(generation)
        state.members.remove(generation)
        state.failures.append(time.monotonic())
        self._restart(generation.kind, state)

    async def _recover(self, generation: _Generation) -> None:
        async with generation.state.lock:
            await self._retire(generation)

    def _restart(self, kind: str, state: _Kind) -> None:
        self._trim(state)
        if (not self._stopped and not state.fatal and len(state.failures) < MAX_FAILURES
                and (state.restart is None or state.restart.done())):
            state.restart = self._own(self._restart_loop(kind, state))

    async def _restart_loop(self, kind: str, state: _Kind) -> None:
        # Back off after failure, but replace a member only on demand.
        delay = min(BACKOFF_MAX, BACKOFF_BASE * 2 ** max(0, len(state.failures) - 1))
        try:
            await asyncio.wait_for(self._shutdown.wait(), delay)
        except TimeoutError:
            pass

    def _idle(self, generation: _Generation) -> None:
        if not self._stopped and not generation.stopping and not generation.leases and generation.idle is None:
            generation.idle = self._own(self._stop_idle(generation))

    async def _stop_idle(self, generation: _Generation) -> None:
        try:
            await asyncio.sleep(IDLE_STOP_SECONDS)
            state = generation.state
            async with state.lock:
                if (not self._stopped and not generation.failed and not generation.leases
                        and sum(not member.failed for member in state.members) > 1):
                    await self._dispose(generation, graceful=True)
                    state.members.remove(generation)
        finally:
            if generation.idle is asyncio.current_task():
                generation.idle = None

    async def _release(self, lease: RunnerLease) -> None:
        generation, bridge = lease._generation, lease.bridge
        closed = lease.session_id is None and lease._open_task is None
        try:
            if lease._open_task is not None:
                result, = await asyncio.gather(lease._open_task, return_exceptions=True)
                if isinstance(result, str):
                    lease.session_id = result
            # Also support a session opened directly through the lease's Bridge.
            sid = lease.session_id or next((s for s, e in bridge.sessions.items()
                                            if e.session_key == lease.session_key), None)
            closed = sid is None
            if sid is not None and not generation.failed:
                try:
                    entry = bridge.sessions.get(sid)
                    try:
                        closed = await bridge.close_session(sid) == CloseState.CLOSED
                    except (RpcFailure, ProtocolError, TimeoutError) as error:
                        logger.warning("runner %s session close failed (%s); retaining lease for retry",
                                       generation.kind, type(error).__name__)
                    finally:
                        if entry is not None and entry.turn is not None:
                            await entry.turn.drain_tools()
                except ConnectionLost:
                    self._failed(generation)
            if generation.failed:
                await bridge.close()
        finally:
            if closed or generation.failed:
                try:
                    shutil.rmtree(lease.directory)
                finally:
                    generation.leases.discard(lease)
                    self._idle(generation)

    async def _dispose(self, generation: _Generation, *, graceful: bool = False) -> None:
        generation.stopping = True
        if generation.idle is not None and generation.idle is not asyncio.current_task():
            generation.idle.cancel()
            generation.idle = None
        session_count = len(generation.leases)
        process = generation.process
        known = {}
        if graceful and process.returncode is None:
            if generation.tree is not None:
                await generation.tree.begin_end()
            else:
                known = await proc_tree.begin_end(process)
        releases = []
        if graceful:
            releases = [self._own(lease.release()) for lease in list(generation.leases)]
            deadline = asyncio.get_running_loop().time() + SHUTDOWN_TIMEOUT
            if releases:
                await asyncio.wait(releases, timeout=SHUTDOWN_TIMEOUT)
            process.stdin.close()  # stdin EOF is the normal process shutdown mechanism.
            try:
                async with asyncio.timeout_at(deadline):
                    await process.wait()
            except TimeoutError:
                pass
        # Even after the parent exits, its group or tracked children can remain.
        self._failed(generation)
        if generation.tree is not None:
            await generation.tree.end()
        else:
            await proc_tree.end(process, label=f"runner {generation.kind}", known=known)
        if generation.bridge is not None:
            await generation.bridge.close()
        else:
            process.stdin.close()
        if generation.stderr is not None:
            await asyncio.wait([generation.stderr], timeout=STDERR_DRAIN_TIMEOUT)
            if not generation.stderr.done():
                logger.warning("runner %s stderr did not close within %.1f s", generation.kind, STDERR_DRAIN_TIMEOUT)
                generation.stderr.cancel()
                process._transport.get_pipe_transport(2).close()
        results = await asyncio.gather(*(t for t in (generation.stderr, generation.waiter, *releases)
                                         if t is not None), return_exceptions=True)
        results.extend(await asyncio.gather(*(lease.release() for lease in list(generation.leases)),
                                            return_exceptions=True))
        for error in results:
            if isinstance(error, BaseException) and not isinstance(error, asyncio.CancelledError):
                logger.error("runner %s cleanup failed (%s)", generation.kind, type(error).__name__)

        logger.info("runner %s member %s stopped: sessions=%d capacity=%d pool_sessions=%d",
                    generation.kind, generation.directory.name.rsplit("-", 1)[1], session_count, generation.cap,
                    sum(len(member.leases) for member in generation.state.members if member is not generation))

    async def stop(self) -> None:
        self._stopped = True
        self._shutdown.set()
        if self._stop_task is None:
            self._stop_task = asyncio.create_task(self._stop())
        await _joined(self._stop_task)

    async def _stop(self) -> None:
        # Initialization holds the kind lock. Interrupt it before waiting for that
        # lock, and keep every spawn/cleanup owned until its process has been reaped.
        starts = list(self._starts)
        for starting in starts:
            if not starting.done() and not starting.cancelling():
                starting.cancel()
        if starts:
            await asyncio.gather(*starts, return_exceptions=True)

        async def close_kind(state):
            async with state.lock:
                for generation in list(state.members):
                    await self._dispose(generation, graceful=True)
                    state.members.remove(generation)
        results = await asyncio.gather(*(close_kind(state) for state in self._kinds.values()),
                                       return_exceptions=True)
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        errors = [error for error in results if isinstance(error, Exception)]
        if errors:
            raise ExceptionGroup("runner shutdown failed", errors)

    async def _stderr(self, generation: _Generation) -> None:
        line = bytearray()
        oversized = False
        window, emitted = time.monotonic(), 0
        retained_bytes = 0

        def retain():
            nonlocal window, emitted, retained_bytes
            text = "[oversized stderr line discarded]" if oversized else redact(line.decode("utf-8", "replace"))
            safe = text.encode("utf-8")[:DIAGNOSTIC_BYTES]
            generation.diagnostics.append(safe)
            retained_bytes += len(safe) + 1
            while retained_bytes > DIAGNOSTIC_BYTES:
                retained_bytes -= len(generation.diagnostics.popleft()) + 1
            if time.monotonic() - window >= 1:
                window, emitted = time.monotonic(), 0
            if emitted < STDERR_LINES_PER_SECOND:
                logger.warning("runner %s stderr: %s", generation.kind, safe.decode("utf-8", "replace"))
                emitted += 1

        while chunk := await generation.process.stderr.read(4096):
            parts = chunk.split(b"\n")
            for index, part in enumerate(parts):
                if len(line) + len(part) > DIAGNOSTIC_BYTES:
                    oversized = True
                if not oversized:
                    line.extend(part)
                if index < len(parts) - 1:
                    retain()
                    line.clear()
                    oversized = False
        if line or oversized:
            retain()
