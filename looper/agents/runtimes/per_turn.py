"""Resident sessions that run one process per turn and resume the session (multi-runtime RFC P2).

A runtime whose CLI runs one turn and exits (Codex's ``exec``, OMP's print
mode) still serves the Manager and a Planner: each turn is a new process that
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

from ..run_usage import Usage

if TYPE_CHECKING:
    from ..claude_cli import CapStop, ResidentTurn
    from ..run_usage import Price

logger = logging.getLogger(__name__)

# How much of a turn's stderr is kept for the log when it ends without a result.
_STDERR_TAIL = 4_000


async def stderr_tail(stream: asyncio.StreamReader | None) -> str:
    """The last ``_STDERR_TAIL`` bytes ``stream`` gives before it ends."""
    if stream is None:
        return ""
    kept = b""
    while chunk := await stream.read(65_536):
        kept = (kept + chunk)[-_STDERR_TAIL:]
    return kept.decode("utf-8", errors="replace")


def session_dir_name(label: str) -> str:
    """A resident owner's ``label`` (``manager``, ``planner-<task>``) as the name of its session directory."""
    return re.sub(r"[^A-Za-z0-9._-]+", "_", label)[:120] or "session"


class TurnStream(Protocol):
    """One turn's output as internal events; ``session_id`` once the output named the session."""

    session_id: str | None

    def events(self, stdout: asyncio.StreamReader) -> AsyncIterator[dict[str, Any]]: ...


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
        stop: Callable[[], CapStop | None] | None = None,
    ) -> ResidentTurn:
        """Run ``text`` as one turn and hand each of its events to ``on_event``. A turn given up (an exception,
        cancellation included) kills its process before it propagates.

        ``stop`` is asked after each event whether the turn went over its soft
        cap (``ClaudeRunRecorder.cap_stop``); a turn over it ends there, with
        the cap's result, its process killed, the session's totals moved on by
        what it spent.
        """
        from ..claude_cli import ResidentTurn

        assert self.started
        argv = await asyncio.to_thread(self._turn_argv)
        resuming = self._session
        process = await asyncio.create_subprocess_exec(
            *argv, cwd=self._cwd, env=self._env,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        self._process = process
        logger.info("%s started a %s turn (pid=%s, %s)", self._label, self.runtime_label, process.pid,
                    f"resuming {resuming}" if resuming else "new session")
        stderr = asyncio.create_task(stderr_tail(process.stderr))
        stream = self._stream()
        result: dict[str, Any] | None = None
        session_started = False
        capped: CapStop | None = None
        try:
            assert process.stdin is not None and process.stdout is not None
            try:
                process.stdin.write(text.encode("utf-8"))
                await process.stdin.drain()
                process.stdin.close()
            except (BrokenPipeError, ConnectionResetError):
                logger.warning("%s: %s closed stdin before the message was sent", self._label, self.runtime_label)
            async for event in stream.events(process.stdout):
                if event.get("type") == "system" and event.get("subtype") == "init":
                    session_started = True
                    self._session = stream.session_id or self._session
                await on_event(event)
                if stop is not None and (capped := stop()) is not None:
                    process.kill()
                    break
                if event.get("type") == "result":
                    result = event
            await process.wait()
        except BaseException:
            if process.returncode is None:
                process.kill()
            await process.wait()
            stderr.cancel()
            self._process, self._ended = None, True
            raise
        self._process = None
        errors = await stderr
        if capped is not None:
            self._totals = self._totals.plus(capped.usage)
            return ResidentTurn(capped.result_event, session_started=session_started, usage=capped.usage)
        if result is None:
            self._ended = True
            logger.warning("%s: the %s turn ended without a result (exit code %s): %s", self._label,
                           self.runtime_label, process.returncode, errors.strip()[-500:])
        else:
            self._turn_ended(new_session=resuming is None and session_started)
        return ResidentTurn(result, None if result is not None else process.returncode,
                            session_started=session_started, usage=self.turn_usage(result))

    async def stop(self, *, kill: bool = False) -> None:
        """Forget the session's command and the session; a turn still running is killed."""
        process, self._process = self._process, None
        self._command, self._signature, self._session, self._ended = [], None, None, False
        if process is not None and process.returncode is None:
            process.kill()
            await process.wait()
        logger.info("%s stopped its %s session", self._label, self.runtime_label)
