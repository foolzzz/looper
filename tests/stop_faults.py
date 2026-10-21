"""Fault injection for the stop tests: block a turn at a named ``token.step``/``token.keep``."""

from __future__ import annotations

import asyncio

import pytest

from looper.agents.tiers import turn_token


class Hold:
    """A turn held at the step ``name``: ``reached`` is set when it gets there, ``release()`` lets it go on."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.reached = asyncio.Event()
        self._released = asyncio.Event()

    def release(self) -> None:
        self._released.set()

    async def __call__(self, name: str) -> None:
        if name == self.name and not self.reached.is_set():
            self.reached.set()
            await self._released.wait()


def hold_at(monkeypatch: pytest.MonkeyPatch, name: str) -> Hold:
    hold = Hold(name)
    monkeypatch.setattr(turn_token, "FAULTS", hold)
    return hold
