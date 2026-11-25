"""Process identities and bounded sampling on the real process-tree path."""

import asyncio
import ctypes
import errno
import os
import subprocess
import sys
import time
from contextlib import asynccontextmanager

import pytest

from looper.agents import proc_tree


@pytest.mark.skipif(sys.platform != "darwin", reason="libproc is macOS-only")
def test_native_start_times_match_persisted_ps_identities(monkeypatch):
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True)
    try:
        pids = [os.getpid(), child.pid]
        expected = {}
        for pid in pids:
            result = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)], capture_output=True,
                                    text=True, check=True, env={**os.environ, "LC_ALL": "C", "TZ": "UTC"})
            expected[pid] = " ".join(result.stdout.split())
        assert all(expected.values())
        assert proc_tree._native_start_times(pids) == expected

        def no_subprocess(*args, **kwargs):
            pytest.fail("live identities must not spawn ps")

        monkeypatch.setattr(proc_tree.subprocess, "run", no_subprocess)
        assert proc_tree._start_times(iter(pids)) == expected
        assert {pid: proc_tree._start_time(pid) for pid in pids} == expected
    finally:
        child.kill()
        child.wait()


@pytest.mark.parametrize("native", [None, {}])
def test_native_failure_falls_back_but_missing_process_does_not(monkeypatch, native):
    monkeypatch.setattr(proc_tree, "_native_start_times", lambda pids: native)
    calls = []

    def ps(args, **kwargs):
        calls.append(args)
        output = "101 Sat Oct 31 12:00:00 2026" if "pid=,lstart=" in args else "Sat Oct 31 12:00:00 2026"
        return subprocess.CompletedProcess(args, 0, stdout=output, stderr="")

    monkeypatch.setattr(proc_tree.subprocess, "run", ps)
    assert proc_tree._start_times(iter([101])) == ({101: "Sat Oct 31 12:00:00 2026"} if native is None else {})
    assert proc_tree._start_time(101) == ("Sat Oct 31 12:00:00 2026" if native is None else None)
    assert len(calls) == (2 if native is None else 0)


@pytest.mark.parametrize("failure", ["library", "symbol", "permission", "short", "gone"])
def test_native_lookup_failures_and_exited_pids(monkeypatch, failure):
    monkeypatch.setattr(proc_tree.sys, "platform", "darwin")

    def read(*args):
        ctypes.set_errno(errno.ESRCH if failure == "gone" else errno.EPERM)
        return 1 if failure == "short" else 0

    def load():
        if failure == "library":
            raise OSError("libproc unavailable")
        if failure == "symbol":
            raise AttributeError("proc_pidinfo unavailable")
        return read

    monkeypatch.setattr(proc_tree, "_proc_pidinfo", load)
    assert proc_tree._native_start_times([101]) == ({} if failure == "gone" else None)


def test_native_identity_uses_c_names_and_normalized_single_digit_day(monkeypatch):
    monkeypatch.setattr(proc_tree.sys, "platform", "darwin")

    def read(pid, flavor, arg, buffer, size):
        info = ctypes.cast(buffer, ctypes.POINTER(proc_tree._ProcBsdInfo)).contents
        info.pbi_start_tvsec = 1772582399  # Tue Mar 3 23:59:59 2026 UTC
        info.pbi_start_tvusec = 999999
        return size

    monkeypatch.setattr(proc_tree, "_proc_pidinfo", lambda: read)
    assert proc_tree._native_start_times([101]) == {101: "Tue Mar 3 23:59:59 2026"}


def test_other_platforms_keep_ps(monkeypatch):
    monkeypatch.setattr(proc_tree.sys, "platform", "linux")
    assert proc_tree._native_start_times([os.getpid()]) is None


def test_idle_resident_bounds_ps_calls_and_checkpoint_restores_fast_sampling(monkeypatch):
    class Agent:
        pid = 101
        returncode = None

    clock = [0.0]
    calls = []
    delays = []
    monkeypatch.setattr(proc_tree.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(proc_tree, "_native_start_times", lambda pids: {pid: "original" for pid in pids})

    def ps(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, stdout="101 1 101 S\n", stderr="")

    monkeypatch.setattr(proc_tree.subprocess, "run", ps)

    async def run():
        tree = proc_tree.track(Agent())
        real_timeout = asyncio.timeout

        @asynccontextmanager
        async def advance(timeout):
            tree._wake.set()
            yield
            delays.append(timeout)
            clock[0] += timeout
            if clock[0] >= 60:
                raise asyncio.CancelledError
            raise asyncio.TimeoutError

        monkeypatch.setattr(proc_tree.asyncio, "timeout", advance)
        with pytest.raises(asyncio.CancelledError):
            await tree._tick(0.5)
        assert len(calls) <= 25  # Includes the initial snapshot, over a simulated minute.
        assert delays[-1] == 5.0
        await tree.checkpoint()
        delays.clear()
        clock[0] = 60
        with pytest.raises(asyncio.CancelledError):
            await tree._tick(0.5)
        assert delays == [0.5]
        monkeypatch.setattr(proc_tree.asyncio, "timeout", real_timeout)

    asyncio.run(run())


def test_process_change_resets_backoff_and_reused_pid_is_rejected(monkeypatch):
    class Agent:
        pid = 101
        returncode = None

    table = {101: (1, 101)}
    starts = {101: "original"}
    monkeypatch.setattr(proc_tree, "_snapshot", lambda: dict(table))
    monkeypatch.setattr(proc_tree, "_start_times", lambda pids: {pid: starts[pid] for pid in pids})

    async def run():
        tree = proc_tree.track(Agent())
        tree._changed = time.monotonic() - 60
        table[102] = (101, 102)
        starts[102] = "child"
        await tree.refresh()
        assert time.monotonic() - tree._changed < 1
        starts[102] = "replacement"
        table[103] = (102, 103)
        starts[103] = "unrelated"
        await tree.refresh(force=True)
        assert 102 in tree._excluded
        assert 103 not in tree.seen

    asyncio.run(run())


def test_new_turn_wakes_an_idle_ticker_immediately(monkeypatch):
    class Agent:
        pid = 101
        returncode = None

    monkeypatch.setattr(proc_tree, "_snapshot", lambda: {101: (1, 101)})
    monkeypatch.setattr(proc_tree, "_start_times", lambda pids: {pid: "original" for pid in pids})

    async def run():
        tree = proc_tree.track(Agent())
        tree._changed = time.monotonic() - 60
        refreshed = asyncio.Event()
        refresh = tree.refresh

        async def observe(**kwargs):
            await refresh(**kwargs)
            refreshed.set()

        monkeypatch.setattr(tree, "refresh", observe)
        tree.start_ticker()
        try:
            await asyncio.wait_for(refreshed.wait(), 1)
            await asyncio.sleep(0.02)  # The ticker has entered its five-second wait.
            refreshed.clear()
            tree.wake()
            await asyncio.wait_for(refreshed.wait(), 0.5)
            assert time.monotonic() - tree._changed < 0.5
        finally:
            await tree.stop_ticker()

    asyncio.run(run())


def test_end_forces_refresh_while_ticker_is_backed_off():
    async def run():
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-c", "import subprocess, sys\nsys.stdin.readline()\n"
            "child = subprocess.Popen(['/bin/sleep', '60'])\nprint(child.pid, flush=True)\n"
            "sys.stdin.read()\n", start_new_session=True,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE)
        child = None
        tree = proc_tree.track(process)
        try:
            tree._changed = time.monotonic() - 60
            tree.start_ticker()
            await asyncio.sleep(0.1)
            # A new child appears after the last periodic observation.
            process.stdin.write(b"\n")
            child = int(await asyncio.wait_for(process.stdout.readline(), 5))
            assert child not in tree.seen
            tree._refreshed = time.monotonic()  # A throttled refresh would miss the new child.
            await tree.end()
            assert child in tree.seen
            assert await asyncio.wait_for(process.wait(), 5) is not None
            assert child not in (proc_tree._snapshot() or {})
        finally:
            await tree.stop_ticker()
            if process.returncode is None:
                process.kill()
            await process.wait()
            if child is not None and child in (proc_tree._snapshot() or {}):
                os.kill(child, 9)

    asyncio.run(run())
