"""Tests must not leave keepers or launchd jobs behind, and must not hang on a stopped child."""
import os
from pathlib import Path
import signal
import subprocess
import sys
import textwrap
import time
from types import SimpleNamespace

import pytest

from tests import leak_guard

ROOT = Path(__file__).resolve().parents[1]
ENV = {**os.environ, "PYTHONPATH": str(ROOT)}


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout.strip() not in ("", "Z")


def _gone(*pids: int, timeout: float = 10) -> bool:
    deadline = time.monotonic() + timeout
    while any(_alive(pid) for pid in pids) and time.monotonic() < deadline:
        time.sleep(0.05)
    return not any(_alive(pid) for pid in pids)


def _run(script: str, *, timeout: float = 60) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-c", textwrap.dedent(script)], cwd=ROOT, env=ENV,
                          capture_output=True, text=True, timeout=timeout)


def test_a_stopped_child_does_not_block_the_event_loop():
    # asyncio's own subprocess blocks the loop here on macOS; a bounded run turns a hang into a failure.
    done = _run("""
        import asyncio, signal, sys
        from tests.leak_guard import PollingProcess, reap_group
        async def main():
            p = PollingProcess([sys.executable, "-c", "import time; time.sleep(60)"])
            await asyncio.sleep(0.3)
            p.send_signal(signal.SIGSTOP)
            await asyncio.sleep(0.3)
            assert p.returncode is None
            reap_group(p.pid)
            assert await asyncio.wait_for(p.wait(), 5) == -signal.SIGKILL
            print("ok")
        asyncio.run(main())
    """, timeout=30)
    assert done.stdout.strip() == "ok", done.stderr


def test_the_keeper_fixture_reaps_the_keeper_tree_after_a_failing_test(tmp_path):
    marker = tmp_path / "pids"
    inner = tmp_path / "test_inner.py"
    inner.write_text(textwrap.dedent(f"""
        import asyncio
        from pathlib import Path
        from tests.test_kept_run_dirs import running_keeper, tmp_path as short_path  # noqa: F401

        def test_fails_while_a_keeper_runs(short_path):
            async def run():
                async with running_keeper(short_path) as (directory, process, state):
                    Path({str(marker)!r}).write_text(f"{{process.pid}} {{state['child_pid']}}")
                    assert False, "forced failure"
            asyncio.run(run())
    """))
    result = subprocess.run([sys.executable, "-m", "pytest", str(inner), "-q", "-p", "no:cacheprovider"],
                            cwd=tmp_path, env=ENV, capture_output=True, text=True, timeout=90)
    assert "forced failure" in result.stdout and "1 failed" in result.stdout, result.stdout
    keeper_pid, child_pid = map(int, marker.read_text().split())
    assert _gone(keeper_pid, child_pid), "the keeper tree survived a failing test"


def test_an_aborted_session_is_swept_by_the_next_one():
    # The owner dies by SIGKILL, so no finalizer runs; the child keeps running until the next session sweeps it.
    done = _run("""
        import os, signal, sys
        from tests.leak_guard import PollingProcess
        p = PollingProcess([sys.executable, "-c", "import time; time.sleep(60)"])
        print(p.pid, flush=True)
        os.kill(os.getpid(), signal.SIGKILL)
    """, timeout=30)
    pid = int(done.stdout.split()[0])
    try:
        assert _alive(pid)
        assert f"pid {pid}" in leak_guard.sweep()
        assert _gone(pid), "the sweep left the orphan running"
    finally:
        if _alive(pid):
            os.kill(pid, signal.SIGKILL)


def test_launchd_sweep_unloads_only_jobs_whose_plist_is_gone(monkeypatch, tmp_path):
    kept = tmp_path / "kept.plist"
    kept.write_text("")
    labels = {"com.looper.test-upgrade-aaa": Path("/tmp/lu.gone/missing.plist"),
              "com.looper.test-upgrade-bbb": kept,
              "com.looper.test-upgrade-ccc": Path("/Users/someone/Library/LaunchAgents/missing.plist")}
    booted = []

    def run(argv, **_):
        if argv[:2] == ["launchctl", "list"]:
            listing = "\n".join(f"-\t0\t{label}" for label in (*labels, "com.looper.service"))
            return SimpleNamespace(stdout=listing, returncode=0)
        if argv[:2] == ["launchctl", "print"]:
            return SimpleNamespace(stdout=f"\tpath = {labels[argv[2].rsplit('/', 1)[1]]}\n", returncode=0)
        booted.append(argv[2].rsplit("/", 1)[1])
        return SimpleNamespace(stdout="", returncode=0)

    monkeypatch.setattr(leak_guard.subprocess, "run", run)
    assert leak_guard.sweep_launchd() == ["com.looper.test-upgrade-aaa"] == booted


def test_a_recycled_pid_is_never_signalled(tmp_path):
    # Another process now holds the recorded pid: same pid, but a different start time or command.
    victim = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True)
    try:
        leak_guard.track(victim.pid)
        record = {"pid": victim.pid, "started": leak_guard.proc_tree._start_time(victim.pid),
                  "command": leak_guard._command(victim.pid)}
        assert leak_guard._is_recorded_process(record)
        assert not leak_guard._is_recorded_process({**record, "started": "Mon Jan  1 00:00:00 2001"})
        assert not leak_guard._is_recorded_process({**record, "command": "some other command"})
        assert not leak_guard._is_recorded_process({**{k: v for k, v in record.items() if k != "command"}})
        # A dead owner with a mismatching record: the sweep removes the record and leaves the process.
        leak_guard._write("0-recycled.json", {**record, "command": "some other command"})
        # The helper stamps this session as owner; make the owner a pid that does not exist.
        path = leak_guard._RECORDS / "0-recycled.json"
        path.write_text(path.read_text().replace(f'"owner": {os.getpid()}', '"owner": 4194305'))
        leak_guard.sweep()
        assert _alive(victim.pid)
    finally:
        leak_guard.untrack(victim.pid)
        (leak_guard._RECORDS / "0-recycled.json").unlink(missing_ok=True)
        victim.kill()
        victim.wait()
