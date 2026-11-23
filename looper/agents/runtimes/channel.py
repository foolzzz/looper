"""A run's lifecycle and how a Worker talks to it."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from . import RuntimeAdapter


class RunStartError(Exception):
    """A run could not start; the message is the reason (no process, no free runner session)."""


class RunStartTimeout(RunStartError):
    """The run's process host did not come up in time (a busy machine): a transient failure."""


@dataclass(frozen=True)
class Fork:
    """A consult's source session and directory, and its own directory for the copy it removes."""

    session_id: str
    session_dir: Path | None
    fork_dir: Path


class RunHandle(Protocol):
    """One Worker, Reviewer or consult run: its own process, or a shared runner's session."""

    @property
    def command(self) -> list[str]: ...  # record-safe argv or runner session descriptor
    @property
    def pid(self) -> int | None: ...  # hosted sessions never enter process-tree cleanup
    @property
    def channel(self) -> RunChannel: ...  # valid after launch()
    async def launch(self) -> None:
        """Spawn or open the session; raises RunStartError with the reason on failure."""
        ...
    async def end(self) -> None:
        """Stop now and release everything; idempotent and never raises."""
        ...
    async def wait(self) -> int:
        """After events ended: the process exit code, or close the session and return zero."""
        ...
    async def error_output(self) -> str: ...  # stderr for a failed process; empty for a session
    def dropped_input(self) -> list[str]: ...  # accepted input dropped before reaching the model


class DeliveryResult(Enum):
    """Runtime acceptance; the bus keeps its successful/refused bools and uses null for uncertainty."""

    ACCEPTED = True
    REJECTED = False
    UNKNOWN = None

    def __bool__(self) -> bool:
        # Keep callers that only check successful delivery compatible. Recovery must compare REJECTED explicitly.
        return self is DeliveryResult.ACCEPTED


class RoomSteerWindow:
    """Room input needs an outstanding tool at the protocol write boundary.

    Track producer-side events: a Worker's event consumer can lag behind the
    protocol reader while recording activity. Other input keeps its own rules.
    """

    def __init__(self) -> None:
        self._tools: set[str] = set()

    def tool_started(self, tool_id: str) -> None:
        if tool_id:
            self._tools.add(tool_id)

    def tool_finished(self, tool_id: str) -> None:
        self._tools.discard(tool_id)

    def observe(self, event: dict[str, Any]) -> None:
        if event.get("type") not in ("assistant", "user") or event.get("parent_tool_use_id"):
            return
        message = event.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            return
        for block in content:
            if not isinstance(block, dict):
                continue
            if event.get("type") == "assistant" and block.get("type") == "tool_use":
                self.tool_started(str(block.get("id") or ""))
            elif event.get("type") == "user" and block.get("type") == "tool_result":
                self.tool_finished(str(block.get("tool_use_id") or ""))

    def check(self, uuid: str | None) -> None:
        if uuid is not None and uuid.startswith("looper-reminder-") and not self._tools:
            raise RuntimeError("room delivery requires a running tool")


@dataclass
class DeliveryAttempt:
    writing: bool = False
    result: DeliveryResult = DeliveryResult.ACCEPTED


@asynccontextmanager
async def delivery_attempt(timeout: float) -> AsyncIterator[DeliveryAttempt]:
    """A bounded send: cancelling drain or an ACK waiter cannot retract bytes already queued."""
    attempt = DeliveryAttempt()
    try:
        async with asyncio.timeout(timeout):
            yield attempt
    except (TimeoutError, BrokenPipeError, ConnectionResetError):
        attempt.result = DeliveryResult.UNKNOWN if attempt.writing else DeliveryResult.REJECTED


class RunChannel(Protocol):
    """A run's first turn, ordered output until EOF, further turns and idempotent input close.

    ``send`` reports accepted, rejected before writing, or unknown acceptance. Explicit runtime refusals may
    raise ``RuntimeError``. Unknown input must not be replayed automatically.
    ``uuid`` names a room delivery; it is never reported by ``dropped_input``.
    ``wait_input_ready`` waits for the startup handshake without writing a turn.
    Once ready, ``send_timeout`` reserves the full write and acceptance budget.
    ``start`` may raise ``BrokenPipeError`` or ``ConnectionResetError`` if input has closed.
    """

    @property
    def send_timeout(self) -> float: ...
    async def start(self, prompt: str) -> None: ...
    async def wait_input_ready(self) -> None: ...
    def events(self) -> AsyncIterator[dict[str, Any]]: ...
    async def send(self, text: str, *, uuid: str | None = None) -> DeliveryResult: ...
    def end_input(self) -> None: ...


class StreamChannel:
    """The runtime's existing stdin turns and stdout events, without a protocol of its own."""

    send_timeout = 4.0

    def __init__(self, runtime: RuntimeAdapter, process: asyncio.subprocess.Process, *, model: str | None) -> None:
        self._runtime = runtime
        self._process = process
        self._model = model

    async def start(self, prompt: str) -> None:
        assert self._process.stdin is not None
        if self._process.stdin.is_closing():
            raise BrokenPipeError("the runtime closed stdin before the first prompt")
        await self._runtime.write_turn(self._process.stdin, prompt)

    def events(self) -> AsyncIterator[dict[str, Any]]:
        assert self._process.stdout is not None
        return self._runtime.parse(self._process.stdout, model=self._model)

    async def wait_input_ready(self) -> None:
        # Stream input has no handshake beyond start() writing the first turn.
        pass

    async def send(self, text: str, *, uuid: str | None = None) -> DeliveryResult:
        assert self._process.stdin is not None
        async with delivery_attempt(self.send_timeout) as attempt:
            attempt.writing = True
            await self._runtime.write_turn(self._process.stdin, text, uuid=uuid)
        return attempt.result

    def end_input(self) -> None:
        assert self._process.stdin is not None
        if not self._process.stdin.is_closing():
            self._process.stdin.close()
