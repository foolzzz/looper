"""Server sleep diagnostics compare elapsed wall and monotonic time."""

import logging

import pytest

from looper.agents import serve


@pytest.mark.parametrize('excess', [-2000, 0, 1, 60, 2000])
def test_suspend_detector_logs_only_large_wall_clock_jumps(caplog, excess):
    caplog.set_level(logging.WARNING, logger=serve.__name__)
    now = [10000.0, 100.0]
    detector = serve._SuspendDetector(wall_clock=lambda: now[0], monotonic_clock=lambda: now[1])
    now[0] += 30 + excess
    now[1] += 30
    detector.check()
    now[0] += 30 + (excess if 0 < excess <= 60 else 0)
    now[1] += 30
    detector.check()  # the old jump must not be logged again
    assert [record.message for record in caplog.records] == (
        ['the system was suspended for about 2000 s (sleep); timers and runs paused'] if excess > 60 else [])
