"""Reconnect timing uses fixed delays for lark-cli and bounded jitter for bots."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from looper.agents.channels import lark
from looper.agents.channels.allowlist import SenderAllowlist


def _delays(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, backoff: bool,
            streams: list[str], jitter: float) -> list[float]:
    now = [0.0]
    sleeps: list[float] = []

    class Transport:
        def __init__(self) -> None:
            self.streams = iter(streams)

        async def events(self) -> AsyncIterator[dict[str, Any]]:
            outcome = next(self.streams)
            if outcome == "long":
                now[0] += 6 * lark.LarkChannelAdapter._RESTART_SECONDS
            if outcome == "event_error":
                yield {}
            if outcome in {"error", "event_error"}:
                raise ConnectionError("connection lost")

    adapter = lark.LarkChannelAdapter(runtime_root=tmp_path, backoff=backoff, transport=Transport(),
                                      allowlist=SenderAllowlist("channel-lark", frozenset({"ou_allowed"})))

    async def handle(event: dict[str, Any]) -> None:
        pass

    async def sleep(delay: float) -> None:
        sleeps.append(delay)
        if len(sleeps) == len(streams):
            adapter._running = False

    def uniform(low: float, high: float) -> float:
        assert backoff, "legacy reconnects must not use jitter"
        assert (low, high) == (0.5, 1.0)
        return jitter

    monkeypatch.setattr(lark, "monotonic", lambda: now[0])
    monkeypatch.setattr(lark, "_reconnect_jitter", uniform)
    monkeypatch.setattr(lark.asyncio, "sleep", sleep)
    monkeypatch.setattr(adapter, "_handle_lark_event", handle)
    adapter._running = True
    asyncio.run(adapter._event_collector_loop())
    return sleeps


def test_legacy_reconnects_keep_the_exact_fixed_delay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    assert _delays(tmp_path, monkeypatch, backoff=False, streams=["empty", "error"] * 4, jitter=0.5) == [10.0] * 8
    assert caplog.text.count("event collector crashed, restarting in 10.0s") == 4
    assert caplog.text.count("event consumer stopped; starting it again in 10.0s") == 4


@pytest.mark.parametrize("jitter", [0.5, 0.75, 1.0])
def test_bot_reconnect_delays_grow_to_a_cap_with_jitter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, jitter: float,
) -> None:
    delays = _delays(tmp_path, monkeypatch, backoff=True, streams=["empty", "error"] * 4, jitter=jitter)
    assert delays == [delay * jitter for delay in [20, 40, 80, 160, 300, 300, 300, 300]]


def test_a_yielded_event_or_a_long_lived_stream_resets_bot_backoff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    delays = _delays(tmp_path, monkeypatch, backoff=True,
                     streams=["empty", "error", "event_error", "empty", "long", "empty"], jitter=1.0)
    assert delays == [20, 40, 10, 20, 10, 20]
