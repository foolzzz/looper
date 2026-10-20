"""Safety boundaries of the isolated, installed-wheel Pi smoke harness."""
from __future__ import annotations

import importlib.util
import os
import select
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "examples/pi/isolated-smoke.py"
spec = importlib.util.spec_from_file_location("pi_smoke", SCRIPT)
smoke = importlib.util.module_from_spec(spec)
spec.loader.exec_module(smoke)


@pytest.mark.parametrize("unsafe", ["port", "root", "database", "symlink"])
def test_refuses_unsafe_targets_before_any_process_starts(tmp_path: Path, monkeypatch, unsafe: str) -> None:
    root = tmp_path / "runtime"
    args = ["--runtime-root", str(root), "--database-url",
            "postgresql://localhost/looper_smoke_test", "--disposable-db"]
    if unsafe == "port":
        args += ["--port", "8767"]
    elif unsafe == "root":
        args[1] = str(SCRIPT.parent / "runtime")
    elif unsafe == "database":
        args.remove("--disposable-db")
    else:
        root.symlink_to(SCRIPT.parent, target_is_directory=True)

    def forbidden(*_args, **_kwargs):
        pytest.fail("validation must happen before starting a child")

    monkeypatch.setattr(subprocess, "Popen", forbidden)
    with pytest.raises(SystemExit) as error:
        smoke.main(args)
    assert error.value.code == 2


@pytest.mark.parametrize("failure", [False, True])
def test_reaps_process_and_stubborn_descendant_on_exit(tmp_path: Path, failure: bool) -> None:
    pid_file = tmp_path / "child.pid"
    read_fd, write_fd = os.pipe()
    # Both processes ignore TERM; cleanup must escalate for the whole group.
    child = "import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); time.sleep(60)"
    parent = ("import pathlib,signal,subprocess,sys,time; "
              "signal.signal(signal.SIGTERM,signal.SIG_IGN); "
              f"p=subprocess.Popen([sys.executable,'-c',{child!r}],pass_fds=({write_fd},)); "
              f"pathlib.Path({str(pid_file)!r}).write_text(str(p.pid)); time.sleep(60)")
    process = None
    try:
        with smoke.managed_process([sys.executable, "-c", parent], cwd=tmp_path, env=dict(os.environ),
                                   grace=0.1, pass_fds=(write_fd,)) as process:
            os.close(write_fd)
            write_fd = -1
            deadline = time.monotonic() + 5
            while not pid_file.exists():
                assert time.monotonic() < deadline
                time.sleep(0.01)
            assert not select.select([read_fd], [], [], 0)[0], "live descendant must hold the pipe open"
            if failure:
                raise RuntimeError("smoke failed")
    except RuntimeError:
        assert failure
    finally:
        if process is not None and process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)
        if write_fd != -1:
            os.close(write_fd)
        try:
            assert process.returncode is not None
            assert select.select([read_fd], [], [], 5)[0], "descendant survived smoke cleanup"
            assert os.read(read_fd, 1) == b""
        finally:
            os.close(read_fd)
