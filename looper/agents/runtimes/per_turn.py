"""Resident sessions that run one process per turn and resume the session (multi-runtime RFC P2).

A runtime whose CLI runs one turn and exits (Codex's ``exec``, OMP's print
mode, Grok's headless mode) still serves the Manager and a Planner: each turn is a new process that
resumes the session the previous turn left. ``PerTurnSession`` is what the
tiers drive for them (``runtimes.ResidentSession``), in place of the one
long-lived process Claude Code keeps (``claude_cli.ResidentClaude``):

- ``start`` keeps the command (the runtime's ``resident_command``, plus its
  ``resume_args`` for a saved session) and starts nothing; it fails only when
  the executable cannot be found;
- ``run_turn`` runs one process with the message on stdin and hands each of its
  internal events to the owner. The session the turn names is the one the next
  turn resumes. A turn that ends without a result marks the session ``stale``,
  so the owner starts over; a process that names no session at all (the
  session to resume is gone) is a turn that never started, as the tiers read a
  session Claude Code no longer has;
- ``stop`` forgets the command and the session; a running turn is killed.

The runtime says how a turn's command is laid out (``_split``,
``_turn_argv``), how its output is read (``_stream``) and what a turn spent
(``turn_usage``). A runtime runs no turn of its own between turns here, so the
``autonomous`` handler is never called.
"""

from __future__ import annotations

import asyncio
import logging
import re
import shutil
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import TYPE_CHECKING, Any, ClassVar, Protocol

from .. import proc_tree
from ..run_usage import Usage
from .cancel import TurnCancel

if TYPE_CHECKING:
    from ..claude_cli import CapStop, ResidentTurn
    from . import StopMethod
    from ..run_usage import Price

logger = logging.getLogger(__name__)

STDERR_DRAIN_SEC = 1.0  # how long a turn waits for stderr to close after its process exited
# How long a turn waits for its tree to be ended: the grace, the reap and a second.
# How much of a turn's stderr is kept for the log when it ends without a result.
_STDERR_TAIL = 4_000


async def stderr_tail(stream: asyncio.StreamReader | None, timeout: float | None = None) -> str:
    """The last ``_STDERR_TAIL`` bytes ``stream`` gives before it ends, or before ``timeout`` seconds are up."""
    if stream is None:
        return ""
    kept = b""
    loop = asyncio.get_running_loop()
    deadline = None if timeout is None else loop.time() + timeout
    while True:
        try:
            chunk = await asyncio.wait_for(stream.read(65_536), None if deadline is None else max(0.0, deadline - loop.time()))
        except asyncio.TimeoutError:
            break
        if not chunk:
            break
        kept = (kept + chunk)[-_STDERR_TAIL:]
    return kept.decode("utf-8", errors="replace")


async def _exited(process: asyncio.subprocess.Process) -> None:
    """Return once ``process`` has exited. ``Process.wait()`` also waits for every pipe to close, so a grandchild that
    holds stderr open would keep a turn waiting for the grandchild when the exit was reported after the output ended;
    the exit status is set as soon as the child is reaped, whatever the pipes do."""
    while process.returncode is None:
        await asyncio.sleep(0.01)


def _end_bound() -> float:
    """How long a turn waits for the cleanup of its process tree: the grace, the reap and a second."""
    return proc_tree.TERM_GRACE_SEC + proc_tree.REAP_SEC + 1


def session_dir_name(label: str) -> str:
    """A resident owner's ``label`` (``manager``, ``planner-<task>``) as the name of its session directory."""
    return re.sub(r"[^A-Za-z0-9._-]+", "_", label)[:120] or "session"


class TurnStream(Protocol):
    """One turn's output as internal events; ``session_id`` once the output named the session."""

    session_id: str | None

    def events(self, stdout: asyncio.StreamReader) -> AsyncIterator[dict[str, Any]]: ...

    # A stream that reports usage per request also has ``usage_so_far() -> Usage``: what the turn spent so far,
    # for a turn the user stopped. One without it (Codex reports usage only at a turn's end) gives ``Usage()``,
    # and the stopped turn's ``usage_complete`` is False.


class PerTurnSession(ABC):
    """The Manager's or a Planner's session on a runtime that runs one process per turn (the module's notes)."""

    runtime_label: ClassVar[str]  # names the runtime in the log

    def __init__(self, *, label: str) -> None:
        self._label = label
        self._command: list[str] = []  # as started: the base command and the session it resumes
        self._base: list[str] = []  # the command without the session
        self._signature: Any = None
        self._cwd: str = ""
        self._env: dict[str, str] | None = None
        self._session: str | None = None  # the session the next turn resumes
        self._process: asyncio.subprocess.Process | None = None  # the turn running now, if any
        self._ended = False  # the last turn ended without a result: the owner starts over
        self._active = False  # a turn is between run_turn's start and its end, its process spawned or not
        self._cancel: TurnCancel | None = None  # the running turn's cancel (what ``interrupt`` requests)
        self._done: asyncio.Future[None] | None = None  # set when the running turn has ended
        self._tree: proc_tree.TurnTree | None = None  # the running turn's process tree, once spawned
        self._end_task: asyncio.Future[None] | None = None  # the cleanup of that tree, once started
        self._finished_tree: proc_tree.TurnTree | None = None  # the last turn's tree, until the next turn starts
        self._finished_end: asyncio.Future[None] | None = None  # the end of that tree, once a stop started it
        self._totals = Usage()
        self._price: Price | None = None  # prices the totals at the provider's prices

    @property
    def label(self) -> str:
        return self._label

    @property
    def started(self) -> bool:
        return bool(self._command)

    @property
    def command(self) -> list[str]:
        return self._command

    @property
    def shares_bridge(self) -> bool:
        return False

    @property
    def pid(self) -> int | None:
        return self._process.pid if self._process is not None else None

    @property
    def totals(self) -> Usage:
        return self._totals

    def stale(self, signature: Any) -> bool:
        """True when it was started with another ``signature``, or its last turn ended without a result."""
        return self.started and (self._ended or self._signature != signature)

    # --- What the runtime says ---

    @abstractmethod
    def _split(self, command: list[str]) -> tuple[list[str], str | None, str]:
        """``command`` as the base command, the session it resumes (None for a new one) and the executable it runs;
        raises ``ValueError`` for a command that is not the runtime's."""

    @abstractmethod
    def _turn_argv(self) -> list[str]:
        """The next turn's command, from ``_base`` and ``_session``; it may write files, so it runs in a thread."""

    @abstractmethod
    def _stream(self) -> TurnStream:
        """What reads the next turn's output."""

    @abstractmethod
    def turn_usage(self, result_event: dict[str, Any] | None) -> Usage:
        """A turn's own tokens and cost from its result, moving the session's totals on by them."""

    def _turn_ended(self, *, new_session: bool) -> None:
        """Called after a turn with a result; ``new_session`` when it started the session it named."""

    # --- The session ---

    async def start(
        self, command: list[str], *, cwd: str, signature: Any, env: dict[str, str] | None = None,
        totals: Usage | None = None, autonomous: Callable[[Any], Awaitable[Any]] | None = None,
        price: Price | None = None,
    ) -> None:
        """Keep the command for the turns; raises ``OSError`` when its executable cannot be found. ``price``
        prices the session's totals at the provider's prices (``Launch.price``)."""
        base, session, executable = self._split(list(command))
        if shutil.which(executable) is None:
            raise OSError(f"{executable!r} was not found")
        self._command, self._signature, self._cwd, self._env = list(command), signature, cwd, env
        self._base, self._session, self._ended = base, session, False
        self._totals, self._price = totals or Usage(), price

    async def run_turn(
        self, text: str, on_event: Callable[[dict[str, Any]], Awaitable[None]], *, tag: Any = None,
        stop: Callable[[], CapStop | None] | None = None, cancel: TurnCancel | None = None,
    ) -> ResidentTurn:
        """Run ``text`` as one turn and hand each of its events to ``on_event``. A turn given up (an exception,
        cancellation included) kills its process before it propagates.

        ``stop`` is asked after each event whether the turn went over its soft
        cap (``ClaudeRunRecorder.cap_stop``); a turn over it ends there, with
        the cap's result, its process killed, the session's totals moved on by
        what it spent.

        ``cancel`` is the one way to stop the turn: once it is requested, in any phase, the turn's process tree is
        ended, no event reaches ``on_event`` (the output is read only for what the turn spent) and the turn
        returns ``user_stop_result``. A request that came before the process exists starts none.
        """
        assert self.started
        cancel = cancel or TurnCancel.never()
        self._active, self._cancel, self._finished_tree = True, cancel, None
        self._done = asyncio.get_running_loop().create_future()
        unregister = cancel.on_request(self._on_stop)
        try:
            return await self._run_turn(text, on_event, tag=tag, stop=stop, cancel=cancel)
        finally:
            unregister()
            if self._end_task is None:  # (an ended tree has nothing left to end)
                self._finished_tree = self._tree
            self._active, self._cancel, self._tree, self._end_task = False, None, None, None
            if not self._done.done():
                self._done.set_result(None)

    def _on_stop(self) -> None:
        """The turn's cancel was requested: end its tree once the process exists (otherwise ``_run_turn`` sees the
        request when it has spawned)."""
        if self._tree is not None and self._end_task is None:
            self._end_task = asyncio.ensure_future(self._tree.end())

    def _stopped_turn(self, *, session_started: bool, usage: Usage, complete: bool, sent: bool) -> ResidentTurn:
        from ..claude_cli import ResidentTurn, user_stop_result
        from . import StopMethod

        return ResidentTurn(user_stop_result(), session_started=session_started, usage=usage,
                            stopped=StopMethod.KILL, usage_complete=complete, prompt_sent=sent)

    async def _end_tree(self, process: asyncio.subprocess.Process, tree: proc_tree.TurnTree) -> None:
        """End the turn's tree, through the stop's own end when it is under way; bounded."""
        if self._end_task is None:
            self._end_task = asyncio.ensure_future(tree.end())
        try:
            await asyncio.wait_for(asyncio.shield(self._end_task), _end_bound())
        except asyncio.TimeoutError:
            logger.warning("%s: the turn's process tree was not ended within %.1f s", self._label, _end_bound())

    async def _run_turn(
        self, text: str, on_event: Callable[[dict[str, Any]], Awaitable[None]], *, tag: Any = None,
        stop: Callable[[], CapStop | None] | None = None, cancel: TurnCancel,
    ) -> ResidentTurn:
        from ..claude_cli import ResidentTurn

        if cancel.requested:  # a stop that came before the turn began: nothing is started
            return self._stopped_turn(session_started=False, usage=Usage(), complete=True, sent=False)
        argv = await asyncio.to_thread(self._turn_argv)
        if cancel.requested:
            return self._stopped_turn(session_started=False, usage=Usage(), complete=True, sent=False)
        resuming = self._session
        process = await proc_tree.spawn(
            *argv, cwd=self._cwd, env=self._env,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        self._process = process
        self._tree = tree = proc_tree.track(process, label=self._label)
        if cancel.requested:  # the stop came while the process was being spawned: end it with the turn
            self._on_stop()
        else:
            tree.start_ticker()
        logger.info("%s started a %s turn (pid=%s, %s)", self._label, self.runtime_label, process.pid,
                    f"resuming {resuming}" if resuming else "new session")
        stderr = asyncio.create_task(stderr_tail(process.stderr))
        stream = self._stream()
        if callable(getattr(stream, "cancel", None)):
            cancel.on_request(stream.cancel)
        result: dict[str, Any] | None = None
        session_started = False
        capped: CapStop | None = None
        failure: Exception | None = None
        sent = False
        try:
            assert process.stdin is not None and process.stdout is not None
            try:
                if not cancel.requested:
                    process.stdin.write(text.encode("utf-8"))
                    await process.stdin.drain()
                    process.stdin.close()
                    sent = True
                    cancel.sent()
            except (BrokenPipeError, ConnectionResetError):
                logger.warning("%s: %s closed stdin before the message was sent", self._label, self.runtime_label)
            async for event in stream.events(process.stdout):
                if event.get("type") == "system" and event.get("subtype") == "init":
                    session_started = True
                    self._session = stream.session_id or self._session
                if cancel.requested:  # read on only for what the turn spent
                    await tree.refresh()
                    continue
                await on_event(event)
                await tree.refresh()
                if cancel.requested:
                    continue
                if stop is not None and (capped := stop()) is not None:
                    await self._end_tree(process, tree)
                    break
                if event.get("type") == "result":
                    result = event
            await _exited(process)
        except BaseException as exc:
            await self._end_tree(process, tree)
            if not (cancel.requested and isinstance(exc, Exception)):  # a stopped turn's closed pipe is not a failure
                stderr.cancel()
                self._process, self._ended = None, True
                raise
            failure = exc
        finally:
            await tree.stop_ticker()
        # Read before the stop is looked at: a stop that lands while it is read still ends the turn.
        errors = ""
        if failure is None:
            done, _ = await asyncio.wait({stderr}, timeout=STDERR_DRAIN_SEC)  # a grandchild may hold it open
            if done and not stderr.cancelled():
                errors = stderr.result()
        stderr.cancel()
        if self._end_task is not None:  # the turn returns only once the stop has cleaned its process tree up
            await self._end_tree(process, tree)
        self._process = None
        if cancel.requested:
            usage = getattr(stream, "usage_so_far", Usage)()
            self._totals = self._totals.plus(usage)
            return self._stopped_turn(session_started=session_started, usage=usage,
                                      complete=hasattr(stream, "usage_so_far"), sent=sent)
        if failure is not None:
            raise failure
        if capped is not None:
            self._totals = self._totals.plus(capped.usage)
            return ResidentTurn(capped.result_event, session_started=session_started, usage=capped.usage,
                                prompt_sent=sent)
        if result is None:
            self._ended = True
            logger.warning("%s: the %s turn ended without a result (exit code %s): %s", self._label,
                           self.runtime_label, process.returncode, errors.strip()[-500:])
        else:
            self._turn_ended(new_session=resuming is None and session_started)
        return ResidentTurn(result, None if result is not None else process.returncode,
                            session_started=session_started, usage=self.turn_usage(result), prompt_sent=sent)

    async def end_turn_processes(self) -> None:
        """End every process the last turn started, a background command that outlived its process included."""
        tree, self._finished_tree = self._finished_tree, None
        if tree is None:
            return
        # Shielded: the bound stops the wait, not the end, whose last look and SIGKILL of strays still run.
        self._finished_end = end = asyncio.ensure_future(tree.end())
        try:
            await asyncio.wait_for(asyncio.shield(end), _end_bound())
        except asyncio.TimeoutError:
            logger.warning("%s: the stopped turn's processes were not ended within %.1f s", self._label, _end_bound())
        except Exception:
            logger.warning("%s: could not end the stopped turn's processes", self._label, exc_info=True)

    async def interrupt(self) -> StopMethod | None:
        """Request the running turn's cancel and wait (bounded) for the turn to end its process tree; the turn
        returns ``user_stop_result`` and the session is kept for the next turn. None when no turn runs."""
        from . import StopMethod

        cancel, done = self._cancel, self._done
        if not self._active or cancel is None or done is None:
            return None
        cancel.request()
        try:
            await asyncio.wait_for(asyncio.shield(done), _end_bound() + STDERR_DRAIN_SEC)
        except asyncio.TimeoutError:
            logger.warning("%s: the stopped turn did not end in time", self._label)
        return StopMethod.KILL

    async def stop(self, *, kill: bool = False) -> None:
        """Forget the session's command and the session; a turn still running is killed."""
        process, self._process = self._process, None
        self._command, self._signature, self._session, self._ended = [], None, None, False
        if process is not None:
            await proc_tree.end(process, label=self._label)
        logger.info("%s stopped its %s session", self._label, self.runtime_label)
