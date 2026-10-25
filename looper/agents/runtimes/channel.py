"""How a Worker talks to one runtime process during a run."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from . import RuntimeAdapter


class DeliveryResult(Enum):
    """Runtime acceptance; the bus keeps its successful/refused bools and uses null for uncertainty."""

    ACCEPTED = True
    REJECTED = False
    UNKNOWN = None

    def __bool__(self) -> bool:
        # Keep callers that only check successful delivery compatible. Recovery must compare REJECTED explicitly.
        return self is DeliveryResult.ACCEPTED


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
