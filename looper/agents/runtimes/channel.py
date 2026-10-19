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
    ``start`` and ``send`` may raise ``BrokenPipeError`` or ``ConnectionResetError`` if input has closed.
    """

    async def start(self, prompt: str) -> None: ...
    def events(self) -> AsyncIterator[dict[str, Any]]: ...
    async def send(self, text: str, *, uuid: str | None = None) -> None: ...
    def end_input(self) -> None: ...


class StreamChannel:
    """The runtime's existing stdin turns and stdout events, without a protocol of its own."""

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

    async def send(self, text: str, *, uuid: str | None = None) -> None:
        assert self._process.stdin is not None
        await self._runtime.write_turn(self._process.stdin, text, uuid=uuid)

    def end_input(self) -> None:
        assert self._process.stdin is not None
        if not self._process.stdin.is_closing():
            self._process.stdin.close()
