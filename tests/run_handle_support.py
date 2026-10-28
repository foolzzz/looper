"""A hosted run/channel with deterministic events and lifecycle controls."""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

from looper.agents.runtimes.channel import DeliveryResult, RunStartError


class ScriptedRun:
    send_timeout = 0.01
    command = ["scripted-run", "session"]
    pid = None

    def __init__(self, events: list[dict[str, Any]], *, hold: bool = False,
                 launch_error: RunStartError | None = None, dropped_input: list[str] | None = None,
                 start_error: Exception | None = None) -> None:
        self._events, self.hold = events, hold
        self.launch_error, self.start_error = launch_error, start_error
        self._dropped = dropped_input or []
        self.launch_calls = self.end_calls = self.wait_calls = 0
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.prompt = ""
        self.inputs: list[str] = []

    @property
    def channel(self) -> ScriptedRun:
        return self

    async def launch(self) -> None:
        self.launch_calls += 1
        if self.launch_error is not None:
            raise self.launch_error

    async def end(self) -> None:
        self.end_calls += 1
        self.release.set()

    async def wait(self) -> int:
        self.wait_calls += 1
        return 0

    async def error_output(self) -> str:
        return ""

    def dropped_input(self) -> list[str]:
        return list(self._dropped)

    async def start(self, prompt: str) -> None:
        self.prompt = prompt
        self.started.set()
        if self.start_error is not None:
            raise self.start_error

    async def wait_input_ready(self) -> None:
        await self.started.wait()

    async def events(self) -> AsyncIterator[dict[str, Any]]:
        for event in self._events:
            if self.hold and event.get("type") == "result":
                await self.release.wait()
            yield event
        if self.hold:
            await self.release.wait()

    async def send(self, text: str, *, uuid: str | None = None) -> DeliveryResult:
        self.inputs.append(text)
        return DeliveryResult.ACCEPTED

    def end_input(self) -> None:
        pass
