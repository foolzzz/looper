"""Schema setup and teardown in ``run_db`` do not spend the test body's deadline.

The slow phases below are an awaited event that stands in for that many seconds.
Nothing in this file sleeps for the simulated duration.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

import pytest

from looper.db import connection
from tests import db_support
from tests.db_support import run_db


class _Stall:
    """One phase's simulated delay. The deadline helper releases it, or the budget cancels it."""

    def __init__(self) -> None:
        self.seconds = 0.0
        self.waiting: asyncio.Future[None] | None = None

    async def for_seconds(self, seconds: float) -> None:
        future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self.seconds = seconds
        self.waiting = future
        try:
            await future
        finally:
            if self.waiting is future:
                self.waiting = None

    def release(self) -> None:
        waiting = self.waiting
        self.waiting = None
        if waiting is not None and not waiting.done():
            waiting.set_result(None)


def _install_clock(monkeypatch: pytest.MonkeyPatch, stall: _Stall) -> None:
    async def deadline(timeout: float) -> None:
        for _ in range(200):
            if stall.waiting is not None:
                break
            await asyncio.sleep(0)
        else:
            await asyncio.Event().wait()
            return
        if stall.seconds > timeout:
            return
        stall.release()
        await asyncio.Event().wait()

    monkeypatch.setattr(db_support, "_phase_deadline", deadline)


def _run(monkeypatch: pytest.MonkeyPatch, *, setup_seconds: float, body_seconds: float,
         teardown_seconds: float, events: list[str]) -> None:
    stall = _Stall()
    _install_clock(monkeypatch, stall)
    monkeypatch.setattr(db_support, "DSN", "postgresql://postgres@example.invalid/budget")

    @asynccontextmanager
    async def schema(*, max_size: int = 3):
        events.append(f"open:{max_size}")
        yield object()
        events.append("teardown")
        if teardown_seconds:
            await stall.for_seconds(teardown_seconds)

    async def ensure(_pool) -> None:
        events.append("ensure")
        if setup_seconds:
            await stall.for_seconds(setup_seconds)

    async def body(_pool) -> None:
        events.append("body")
        if body_seconds:
            await stall.for_seconds(body_seconds)

    monkeypatch.setattr(db_support, "fresh_schema", schema)
    monkeypatch.setattr(connection, "ensure_schema", ensure)
    run_db(body, timeout=5)


@pytest.mark.parametrize(
    ("setup_seconds", "body_seconds", "teardown_seconds", "expected"),
    [
        pytest.param(6, 0, 0, None, id="schema-setup-6s"),
        pytest.param(0, 6, 0, "test body exceeded 5s", id="body-6s"),
        pytest.param(31, 0, 0, "schema setup exceeded 30s", id="schema-setup-31s"),
        pytest.param(0, 0, 6, None, id="teardown-6s"),
        pytest.param(0, 0, 31, "schema teardown exceeded 30s", id="teardown-31s"),
    ],
)
def test_schema_phases_keep_their_own_budget(monkeypatch, setup_seconds, body_seconds, teardown_seconds,
                                             expected) -> None:
    """6s of schema work finishes under the 30s schema budget; a body over 5s still fails as the test."""
    events: list[str] = []
    if expected is None:
        _run(monkeypatch, setup_seconds=setup_seconds, body_seconds=body_seconds,
             teardown_seconds=teardown_seconds, events=events)
    else:
        with pytest.raises(TimeoutError, match="^" + expected + "$"):
            _run(monkeypatch, setup_seconds=setup_seconds, body_seconds=body_seconds,
                 teardown_seconds=teardown_seconds, events=events)
    assert events[0] == "open:3"
    assert "ensure" in events
    if expected == "schema setup exceeded 30s":
        assert "body" not in events
    else:
        assert "body" in events
    assert "teardown" in events


def test_a_timeout_from_the_body_keeps_its_own_message(monkeypatch) -> None:
    """A TimeoutError the test raises is the test's, not a schema-setup failure."""
    stall = _Stall()
    _install_clock(monkeypatch, stall)
    monkeypatch.setattr(db_support, "DSN", "postgresql://postgres@example.invalid/budget")

    @asynccontextmanager
    async def schema(*, max_size: int = 3):
        yield object()

    async def ensure(_pool) -> None:
        return None

    async def body(_pool) -> None:
        raise TimeoutError("outbound delivery is still pending")

    monkeypatch.setattr(db_support, "fresh_schema", schema)
    monkeypatch.setattr(connection, "ensure_schema", ensure)
    with pytest.raises(TimeoutError, match="^outbound delivery is still pending$"):
        run_db(body, timeout=5)
