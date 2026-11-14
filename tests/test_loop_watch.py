"""The server watchdog distinguishes a blocked loop from an unscheduled process."""

from __future__ import annotations

import asyncio
import logging
import sys
import threading
import time

from looper.agents.loop_watch import LoopWatch


def test_blocking_callback_reports_its_stack_and_recovery(caplog):
    caplog.set_level(logging.WARNING)

    def blocking_callback():
        time.sleep(1.5)

    async def run():
        watch = LoopWatch()
        watch.start()
        try:
            asyncio.get_running_loop().call_soon(blocking_callback)
            await asyncio.sleep(1.85)
            summary = watch.take_summary()
            assert summary.loop_stalls == 1
            assert summary.loop_max_stall_ms >= 1400
            assert watch.take_summary().loop_stalls == 0
        finally:
            await watch.stop()
        assert not watch._thread.is_alive()

    asyncio.run(run())
    stacks = [record.message for record in caplog.records if "loop thread stack:" in record.message]
    assert len(stacks) == 1
    assert "blocking_callback" in stacks[0]
    assert len([record for record in caplog.records if "event loop was blocked for" in record.message]) == 1


def test_awaiting_sleep_does_not_report_a_block(caplog):
    caplog.set_level(logging.WARNING)

    async def run():
        watch = LoopWatch()
        watch.start()
        try:
            for _ in range(6):
                await asyncio.sleep(0.25)
            assert watch.take_summary().loop_stalls == 0
        finally:
            await watch.stop()

    asyncio.run(run())
    assert not caplog.records


def test_watchdog_oversleep_suppresses_the_same_loop_interval(monkeypatch):
    now = [100.0]
    messages = []
    monkeypatch.setattr(threading.Thread, "start", lambda self: None)

    async def run():
        watch = LoopWatch(clock=lambda: now[0], log=lambda fmt, *args: messages.append(fmt % args))
        watch.start()
        try:
            now[0] += 3.0
            watch._check()
            now[0] += 0.25
            watch._check()
            watch._beat = now[0]
            now[0] += 0.25
            watch._check()
            summary = watch.take_summary()
            assert summary.process_stalls == 1
            assert summary.loop_stalls == 0
        finally:
            await watch.stop()

    asyncio.run(run())
    assert messages == [
        "the server process was not scheduled for 2.8 s (machine overloaded or swapping); not an event-loop block"
    ]


def test_escalations_only_log_changed_innermost_frames_and_reset_summary(monkeypatch):
    now = [100.0]
    messages = []
    frame = [sys._getframe()]
    monkeypatch.setattr(threading.Thread, "start", lambda self: None)

    def other_frame():
        return sys._getframe()

    async def run():
        watch = LoopWatch(clock=lambda: now[0], frames=lambda: {threading.get_ident(): frame[0]},
                          log=lambda fmt, *args: messages.append(fmt % args))
        watch.start()
        try:
            for tick in range(1, 66):
                now[0] = 100.0 + tick * 0.25
                if tick == 60:
                    frame[0] = other_frame()
                watch._check()
            assert len([message for message in messages if "loop thread stack:" in message]) == 2
            watch._beat = now[0]
            now[0] += 0.25
            watch._check()
            assert "event loop was blocked for 16.2 s" == messages[-1]
            summary = watch.take_summary()
            assert summary.loop_stalls == 1
            assert summary.loop_max_stall_ms == 16250
            assert summary.process_stalls == 0
            assert watch.take_summary().loop_max_stall_ms == 0
        finally:
            await watch.stop()

    asyncio.run(run())


def test_query_stall_reading_survives_heartbeat_and_summary_reset(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(threading.Thread, "start", lambda self: None)

    async def run():
        watch = LoopWatch(clock=lambda: now[0], log=lambda *args: None)
        watch.start()
        try:
            now[0] = 106.0
            # The lease callback can run before the watchdog resumes.
            assert watch.stall_seconds_between(100.0, 106.0) == 6.0
            watch._check()
            await asyncio.sleep(0)  # Update the heartbeat after resuming.
            watch.take_summary()
            assert watch.stall_seconds_between(100.0, 106.0) >= 5.0
            assert watch.stall_seconds_between(106.0, 106.5) == 0.0
        finally:
            await watch.stop()
    asyncio.run(run())
