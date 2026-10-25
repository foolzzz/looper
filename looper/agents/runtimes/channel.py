"""How a Worker talks to one runtime process during a run."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from . import RuntimeAdapter


class RunChannel(Protocol):
    """A run's first turn, ordered output until EOF, further turns and idempotent input close.

    ``send`` returns once the runtime accepts the turn; a run that cannot accept it raises ``RuntimeError``.
    ``wait_input_ready`` waits for the startup handshake without writing a turn.
    Once ready, ``send_timeout`` reserves the full write and acceptance budget.
    ``start`` and ``send`` may raise ``BrokenPipeError`` or ``ConnectionResetError`` if input has closed.
    """

    @property
    def send_timeout(self) -> float: ...
    async def start(self, prompt: str) -> None: ...
    async def wait_input_ready(self) -> None: ...
    def events(self) -> AsyncIterator[dict[str, Any]]: ...
    async def send(self, text: str, *, uuid: str | None = None) -> None: ...
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

    async def send(self, text: str, *, uuid: str | None = None) -> None:
        assert self._process.stdin is not None
        async with asyncio.timeout(self.send_timeout):
            await self._runtime.write_turn(self._process.stdin, text, uuid=uuid)

    def end_input(self) -> None:
        assert self._process.stdin is not None
        if not self._process.stdin.is_closing():
            self._process.stdin.close()
