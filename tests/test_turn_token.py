"""Typed turn phases, the registry, and the one cancellation primitive (``TurnCancel``)."""

from __future__ import annotations

import asyncio
import itertools
from datetime import datetime, timezone
from typing import Any

import pytest

from looper.agents.message import AgentAddress, MessageEnvelope, MessageType
from looper.agents.runtimes.cancel import TurnCancel, TurnCancelled
from looper.agents.tiers import turn_token
from looper.agents.tiers.manager import StopRequest as ManagerStopRequest, TurnStopped as ManagerTurnStopped
from looper.agents.tiers.turn_token import TurnPhase, TurnRegistry, TurnStopped, TurnToken

P = TurnPhase
ALLOWED = {(P.RECEIVED, P.PREPARING), (P.PREPARING, P.STARTING), (P.STARTING, P.RUNNING)}


def _token(phase: TurnPhase = P.RECEIVED, mid: str = "m1") -> TurnToken:
    token = TurnToken(message_id=mid, client_message_id=None, channel="web", chat="c",
                      received_at=datetime.now(timezone.utc))
    token.phase = phase
    return token


def test_reexports() -> None:
    assert ManagerStopRequest is turn_token.StopRequest
    assert ManagerTurnStopped is TurnStopped


@pytest.mark.parametrize("src,dst", list(itertools.product(P, P)))
def test_transition_table(src: TurnPhase, dst: TurnPhase) -> None:
    token = _token(src)
    if src is P.STOPPING:
        with pytest.raises(TurnStopped):
            token.advance(dst)
        assert token.phase is P.STOPPING
    elif (src, dst) in ALLOWED:
        token.advance(dst)
        assert token.phase is dst
    else:
        with pytest.raises(AssertionError):
            token.advance(dst)
        assert token.phase is src


def test_restart() -> None:
    token = _token(P.RUNNING)
    token.restart()
    assert token.phase is P.STARTING
    with pytest.raises(AssertionError):
        token.restart()
    stopping = _token(P.STOPPING)
    with pytest.raises(TurnStopped):
        stopping.restart()


def test_request_stop() -> None:
    token = _token(P.PREPARING)
    assert token.request_stop("web") is True
    assert token.request_stop("web") is False
    assert token.phase is P.STOPPING and token.cancel.requested
    assert token.stop is not None and token.stop.phase is P.PREPARING and token.stop.via == "web"
    with pytest.raises(TurnStopped):
        token.check()
    token.settle("stopped")
    assert token.phase is P.ENDED and not token.live
    assert token.request_stop("web") is False
    token.settle("answered")
    assert token.outcome == "stopped"


def test_settle_from_any_live_phase() -> None:
    for phase in (P.RECEIVED, P.PREPARING, P.STARTING, P.RUNNING, P.STOPPING):
        token = _token(phase)
        token.settle("x")
        assert token.phase is P.ENDED


def test_sent_advances_only_starting() -> None:
    token = _token(P.STARTING)
    token.cancel.sent()
    assert token.phase is P.RUNNING and token.cancel.prompt_sent
    stopping = _token(P.STARTING)
    stopping.request_stop("web")
    stopping.cancel.sent()
    assert stopping.phase is P.STOPPING


def test_cancel_callbacks() -> None:
    cancel = TurnCancel()
    calls: list[str] = []
    cancel.on_request(lambda: calls.append("a"))
    off = cancel.on_request(lambda: calls.append("b"))
    off()
    off()

    def boom() -> None:
        raise RuntimeError("x")

    cancel.on_request(boom)
    cancel.on_request(lambda: calls.append("c"))
    assert cancel.request() is True
    assert cancel.request() is False
    assert calls == ["a", "c"]
    cancel.on_request(lambda: calls.append("late"))
    assert calls == ["a", "c", "late"]
    assert cancel.requested_at is not None
    assert TurnCancel.never() is not TurnCancel.never()


def test_on_sent_listeners() -> None:
    cancel = TurnCancel()
    calls: list[int] = []
    cancel.on_sent(lambda: calls.append(1))
    cancel.sent()
    cancel.sent()
    cancel.on_sent(lambda: calls.append(2))
    assert calls == [1, 2]


def test_race() -> None:
    async def run() -> None:
        cancel = TurnCancel()

        async def value() -> int:
            await asyncio.sleep(0)
            return 7

        assert await cancel.race(value()) == 7

        inner: list[asyncio.Task[Any]] = []

        async def hang() -> None:
            inner.append(asyncio.current_task())  # type: ignore[arg-type]
            await asyncio.Event().wait()

        waiter = asyncio.ensure_future(cancel.race(hang()))
        await asyncio.sleep(0.01)
        cancel.request()
        with pytest.raises(TurnCancelled):
            await waiter
        assert inner[0].cancelled()

        # after the request: raises at once, and a coroutine is closed without a warning
        import warnings
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            with pytest.raises(TurnCancelled):
                await cancel.race(value())

        fresh = TurnCancel()
        tasks: list[asyncio.Task[Any]] = []

        async def slow() -> None:
            tasks.append(asyncio.current_task())  # type: ignore[arg-type]
            await asyncio.Event().wait()

        with pytest.raises(TimeoutError):
            await fresh.race(slow(), timeout=0.01)
        assert tasks[0].cancelled()

    asyncio.run(run())


def test_step_and_keep() -> None:
    async def run() -> None:
        token = _token(P.PREPARING)

        async def hang() -> None:
            await asyncio.Event().wait()

        waiter = asyncio.ensure_future(token.step("hold", hang()))
        await asyncio.sleep(0.01)
        token.request_stop("web")
        with pytest.raises(TurnStopped):
            await asyncio.wait_for(waiter, 1)

        other = _token(P.PREPARING)
        done: list[str] = []

        async def work() -> str:
            await asyncio.sleep(0.05)
            done.append("done")
            return "ok"

        kept = asyncio.ensure_future(other.keep("work", work(), 1))
        await asyncio.sleep(0.01)
        other.request_stop("web")
        with pytest.raises(TurnStopped):
            await kept
        assert done == ["done"]

        fine = _token(P.PREPARING)
        assert await fine.keep("work", work(), 1) == "ok"
        with pytest.raises(TimeoutError):
            await fine.keep("slow", hang(), 0.01)

    asyncio.run(run())


def test_keep_owns_its_step_a_cancel_or_timeout_ends_it_and_undoes_it() -> None:
    async def run() -> None:
        events: list[str] = []

        async def start() -> str:
            try:
                await asyncio.sleep(0.2)
            except asyncio.CancelledError:
                events.append("start cancelled")
                raise
            events.append("started")
            return "process"

        async def undo() -> None:
            events.append("undone")

        token = _token(P.PREPARING)
        with pytest.raises(TimeoutError):
            await token.keep("claude_start", start(), 0.05, undo=undo)
        assert events == ["start cancelled", "undone"]

        events.clear()
        outer = asyncio.ensure_future(_token(P.PREPARING).keep("claude_start", start(), 5, undo=undo))
        await asyncio.sleep(0.05)
        outer.cancel()
        with pytest.raises(asyncio.CancelledError):
            await outer
        assert events == ["start cancelled", "undone"]  # nothing of the step runs on after the turn's task ended
        await asyncio.sleep(0.3)
        assert events == ["start cancelled", "undone"]

        events.clear()
        assert await _token(P.PREPARING).keep("claude_start", start(), 5, undo=undo) == "process"
        assert events == ["started"]  # undone only when the turn gave it up

    asyncio.run(run())


def test_faults_hook_gets_the_step_name(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []

    async def faults(name: str) -> None:
        seen.append(name)

    monkeypatch.setattr(turn_token, "FAULTS", faults)

    async def one() -> int:
        return 1

    async def run() -> None:
        token = _token(P.PREPARING)
        assert await token.step("a", one()) == 1
        assert await token.keep("b", one(), 1) == 1

    asyncio.run(run())
    assert seen == ["a", "b"]


def _envelope(mid: str) -> MessageEnvelope:
    addr = AgentAddress("manager", "m")
    return MessageEnvelope(message_id=mid, correlation_id=mid, timestamp="t", source=addr, target=addr,
                           message_type=MessageType.USER_MESSAGE, payload={})


def test_registry() -> None:
    reg = TurnRegistry()
    a = reg.receive(_envelope("a"), channel="web", chat="c", client_message_id="ca")
    b = reg.receive(_envelope("b"), channel="web", chat="c", client_message_id=None)
    other = reg.receive(_envelope("o"), channel="tg", chat="c", client_message_id=None)
    assert reg.find("a") is a and reg.find("ca") is a and reg.find("zz") is None
    assert reg.live_for("web", "c") == [a, b]
    assert reg.oldest_for("web", "c") is a and reg.oldest_for("web", "none") is None
    a.settle("answered")
    assert reg.find("a") is None and reg.find("ca") is None
    assert reg.live_for("web", "c") == [b]
    reg.drop(b)
    assert reg.find("b") is None
    reg.clear()
    assert reg.find("o") is None and other.live
