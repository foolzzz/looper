"""A real keeper owns its child's lifetime and durable byte streams without a server."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import stat
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import ExitStack
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from looper.agents import keeper, proc_tree
from looper.agents.keeper.__main__ import main
from looper.agents.keeper.process import Keeper
from tests.test_keeper_protocol import short_directory


ROOT = Path(__file__).resolve().parents[1]
SLEEP = "import time; time.sleep(60)"
TREE = """\
import json, os, subprocess, sys, time
grandchild = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], start_new_session=True)
with open('tree.json', 'w') as output:
    json.dump({'child': os.getpid(), 'grandchild': grandchild.pid}, output)
while True:
    os.write(1, b'tick\\n')
    time.sleep(0.05)
"""


def _wait_for(predicate: Callable[[], Any], timeout: float = 12) -> Any:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if result := predicate():
            return result
        time.sleep(0.03)
    raise AssertionError("keeper did not reach the expected state before the timeout")


def _live(pids: list[int]) -> set[int]:
    if not pids:
        return set()
    result = subprocess.run(
        ["ps", "-o", "pid=,stat=", "-p", ",".join(map(str, pids))],
        capture_output=True, text=True, timeout=5, check=False,
    )
    assert result.returncode in (0, 1), result.stderr
    return {int(fields[0]) for line in result.stdout.splitlines()
            if len(fields := line.split()) == 2 and not fields[1].startswith("Z")}


@dataclass
class Run:
    directory: Path
    runtime_root: Path
    cwd: Path
    pid: int
    process: subprocess.Popen[bytes] | None
    parent: subprocess.Popen[bytes] | None = None
    identities: dict[int, str] = field(default_factory=dict)

    def state(self) -> dict:
        return keeper.read_json(self.directory / keeper.STATE) or {}

    def running(self) -> dict:
        state = _wait_for(lambda: (value if (value := self.state()).get("state") == "running" else None))
        self.identities[state["child_pid"]] = state["child_started"]
        return state

    def wait(self, expected: int = 0) -> dict:
        if self.process is not None:
            assert self.process.wait(timeout=15) == expected, (self.directory / keeper.LOG).read_text()
        else:
            _wait_for(lambda: not _live([self.pid]), timeout=15)
        return self.state()

    def tree(self) -> dict[str, int]:
        tree = _wait_for(lambda: keeper.read_json(self.cwd / "tree.json"))
        for pid in tree.values():
            started = proc_tree._start_time(pid)
            assert started is not None
            self.identities[pid] = started
        return tree

    def stop(self) -> None:
        os.kill(self.pid, signal.SIGTERM)


@pytest.fixture
def launch(tmp_path: Path) -> Iterator[Callable[..., Run]]:
    runs: list[Run] = []
    directories = ExitStack()

    def start(
        script: str = SLEEP, *, manifest: dict | str | None = None,
        env: dict[str, str] | None = None, argv: list[str] | None = None,
        via_parent: bool = False, directory_mode: int = 0o700, keeper_setup: str = "",
    ) -> Run:
        runtime_root = tmp_path / f"runtime-{len(runs)}"
        directory = directories.enter_context(short_directory())
        runtime_root.mkdir(parents=True)
        directory.chmod(directory_mode)
        cwd = runtime_root / "work"
        cwd.mkdir()
        if isinstance(manifest, dict):
            keeper.write_json(directory / keeper.MANIFEST, manifest)
        elif isinstance(manifest, str):
            (directory / keeper.MANIFEST).write_text(manifest, encoding="utf-8")
        command = keeper.boot_argv(sys.executable, ROOT, directory, runtime_root, str(cwd),
                                   argv if argv is not None else [sys.executable, "-c", script])
        if keeper_setup:
            command[3] = command[3].replace("; sys.exit(", "\n" + keeper_setup + "\nsys.exit(")
        log_fd = os.open(directory / keeper.LOG, os.O_WRONLY | os.O_CREAT, 0o600)
        try:
            if via_parent:
                parent_script = (
                    "import json, subprocess, sys, time; "
                    "child = subprocess.Popen(json.loads(sys.argv[1]), start_new_session=True); "
                    "open(sys.argv[2], 'w').write(str(child.pid)); time.sleep(60)"
                )
                parent = subprocess.Popen(
                    [sys.executable, "-c", parent_script, json.dumps(command), str(directory / "keeper.pid")],
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=log_fd,
                    start_new_session=True, env=env,
                )
                run = Run(directory, runtime_root, cwd, parent.pid, None, parent)
                runs.append(run)
                run.pid = int(_wait_for(lambda: (directory / "keeper.pid").read_text()
                                       if (directory / "keeper.pid").exists() else None))
            else:
                process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                           stderr=log_fd, start_new_session=True, env=env)
                run = Run(directory, runtime_root, cwd, process.pid, process)
                runs.append(run)
            if started := proc_tree._start_time(run.pid):
                run.identities[run.pid] = started
            return run
        finally:
            os.close(log_fd)

    yield start

    for run in reversed(runs):
        # A failure before RUNNING can still leave an agent record, so read those too.
        if run.parent is not None and (run.directory / "keeper.pid").exists():
            run.pid = int((run.directory / "keeper.pid").read_text())
            if run.pid not in run.identities and (started := proc_tree._start_time(run.pid)):
                run.identities[run.pid] = started
        state = run.state()
        if state.get("child_pid") and state.get("child_started"):
            run.identities[state["child_pid"]] = state["child_started"]
        for path in (run.runtime_root / proc_tree.RECORDS_DIR).glob("*.json"):
            record = keeper.read_json(path) or {}
            if record.get("pid") and record.get("started"):
                run.identities[record["pid"]] = record["started"]
        for pid in (keeper.read_json(run.cwd / "tree.json") or {}).values():
            if pid not in run.identities and (started := proc_tree._start_time(pid)):
                run.identities[pid] = started
        if run.pid in _live([run.pid]) and proc_tree._start_time(run.pid) == run.identities.get(run.pid):
            run.stop()
        if run.process is not None:
            try:
                run.process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                run.process.kill()
                run.process.wait(timeout=5)
        if run.parent is not None:
            if run.parent.poll() is None:
                run.parent.kill()
            run.parent.wait(timeout=5)
        for pid in _live(list(run.identities)):
            if proc_tree._start_time(pid) == run.identities[pid]:
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        _wait_for(lambda: not _live(list(run.identities)), timeout=5)
    directories.close()


def test_every_child_byte_is_logged_in_order(launch: Callable[..., Run]) -> None:
    stdout = bytes(range(256)) * (3 * 1024 * 1024 // 256)
    stderr = b"err\x00\xff" * 2048
    run = launch("""\
import random, sys
data = bytes(range(256)) * (3 * 1024 * 1024 // 256)
randomizer = random.Random(7)
offset = 0
while offset < len(data):
    block = data[offset:offset + randomizer.randint(1, 100000)]
    sys.stdout.buffer.write(block)
    sys.stdout.buffer.flush()
    offset += len(block)
sys.stderr.buffer.write(b'err\\x00\\xff' * 2048)
sys.exit(7)
""")
    state = run.wait()
    assert (run.directory / keeper.STDOUT).read_bytes() == stdout
    assert (run.directory / keeper.STDERR).read_bytes() == stderr
    assert (run.directory / keeper.STDIN).read_bytes() == b""
    assert state["state"] == "exited"
    assert state["exit_code"] == 7
    assert state["signal"] is None
    assert state["end_reason"] == "exited"
    assert state["format"] == 1
    assert state["protocol_versions"] == ["1.0"]
    assert state["controller_epoch"] is None
    assert state["socket"] == str(run.directory / keeper.SOCKET)
    assert not (run.directory / keeper.SOCKET).exists()
    assert state["keeper_pid"] == run.pid and state["keeper_started"]
    assert state["ended_at"]
    assert stat.S_IMODE(run.directory.stat().st_mode) == 0o700
    for name in (keeper.STDOUT, keeper.STDERR, keeper.STDIN, keeper.STATE, keeper.LOG):
        assert stat.S_IMODE((run.directory / name).stat().st_mode) == 0o600


def test_the_child_keeps_the_callers_umask(launch: Callable[..., Run]) -> None:
    run = launch("""\
import os
previous = os.umask(0)
os.umask(previous)
print(oct(previous))
with open('created.txt', 'w') as output:
    output.write('child file')
""", keeper_setup="import os; os.umask(0o022)")
    assert run.wait()["exit_code"] == 0
    assert (run.directory / keeper.STDOUT).read_text() == "0o22\n"
    assert stat.S_IMODE((run.cwd / "created.txt").stat().st_mode) == 0o644


def test_the_keeper_outlives_its_parent_and_its_group(launch: Callable[..., Run]) -> None:
    run = launch(TREE, via_parent=True)
    run.running()
    tree = run.tree()
    os.kill(run.pid, signal.SIGINT)
    os.kill(run.pid, signal.SIGHUP)
    assert run.parent is not None
    os.killpg(run.parent.pid, signal.SIGKILL)
    assert run.parent.wait(timeout=5) == -signal.SIGKILL
    before = (run.directory / keeper.STDOUT).stat().st_size
    _wait_for(lambda: (run.directory / keeper.STDOUT).stat().st_size > before)
    assert _live([run.pid, *tree.values()]) == {run.pid, *tree.values()}
    run.stop()
    state = run.wait()
    assert state["end_reason"] == "ended"
    assert not _live(list(tree.values()))


def test_a_server_sweep_leaves_a_live_keepers_child_and_ends_a_dead_keepers(
    launch: Callable[..., Run], monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(proc_tree, "_records", None)
    monkeypatch.setattr(proc_tree, "_server", None)
    monkeypatch.setattr(proc_tree, "_recorded", {})
    monkeypatch.setattr(proc_tree, "_ending", set())
    monkeypatch.setattr(proc_tree, "_forgetting", set())
    run = launch()
    state = run.running()
    child = state["child_pid"]
    record = run.runtime_root / proc_tree.RECORDS_DIR / f"{child}.json"
    assert keeper.read_json(record)["server_pid"] == run.pid
    asyncio.run(proc_tree.sweep(run.runtime_root))
    assert _live([child]) == {child}
    assert record.exists()
    assert run.process is not None
    run.process.kill()
    assert run.process.wait(timeout=5) == -signal.SIGKILL
    asyncio.run(proc_tree.sweep(run.runtime_root))
    assert not _live([child])
    assert not record.exists()


def test_no_controller_means_the_orphan_timeout_ends_the_child(launch: Callable[..., Run]) -> None:
    run = launch(manifest={"orphan_timeout_s": 1})
    child = run.running()["child_pid"]
    assert run.wait()["end_reason"] == "orphaned"
    assert not _live([child])
    assert f"keeper orphaned run={run.directory.name}" in (run.directory / keeper.LOG).read_text()


def test_stdout_past_the_cap_keeps_the_child_running(launch: Callable[..., Run]) -> None:
    run = launch("import os, time; os.write(1, b'x' * 10000); time.sleep(60)",
                 manifest={"stdout_limit_bytes": 1000})
    state = run.running()
    _wait_for(lambda: run.state().get("stdout_truncated"))
    assert (run.directory / keeper.STDOUT).read_bytes() == b"x" * 1000
    assert run.state()["stdout_truncated_at"] == 1000
    assert _live([state["child_pid"]]) == {state["child_pid"]}
    run.stop()
    assert run.wait()["end_reason"] == "ended"


def test_a_child_that_cannot_start_is_recorded(launch: Callable[..., Run]) -> None:
    run = launch(argv=["/does-not-exist/keeper-test-child"])
    state = run.wait(expected=3)
    assert state["state"] == "exited" and state["end_reason"] == "start_failed"
    assert state["error"] and len(state["error"]) <= 300
    assert state["child_pid"] is None


def test_the_child_gets_the_keepers_environment_and_cwd(launch: Callable[..., Run]) -> None:
    environment = {**os.environ, "KEEPER_TEST_SENTINEL": "inherited exactly", "PYTHONPATH": "/unused/path"}
    run = launch("import json, os; print(json.dumps({'env': dict(os.environ), 'cwd': os.getcwd()}))",
                 env=environment)
    run.wait()
    observed = json.loads((run.directory / keeper.STDOUT).read_text())
    assert observed == {"env": environment, "cwd": str(run.cwd)}


def test_an_insecure_run_directory_is_rejected_without_writing(launch: Callable[..., Run]) -> None:
    run = launch(directory_mode=0o755)
    run.wait(expected=2)
    assert {path.name for path in run.directory.iterdir()} == {keeper.LOG}
    assert (run.directory / keeper.LOG).read_text()


@pytest.mark.parametrize("manifest", ["not json", {"orphan_timeout_s": True, "stdout_limit_bytes": -1}])
def test_an_invalid_manifest_uses_defaults_and_logs_a_warning(
    launch: Callable[..., Run], manifest: dict | str,
) -> None:
    run = launch("import os; os.write(1, b'x' * 2000)", manifest=manifest)
    assert run.wait()["end_reason"] == "exited"
    assert (run.directory / keeper.STDOUT).read_bytes() == b"x" * 2000
    assert "warning" in (run.directory / keeper.LOG).read_text().lower()


def test_stdout_eof_does_not_end_a_running_child(launch: Callable[..., Run]) -> None:
    run = launch("import os, time; os.close(1); os.write(2, b'closed'); time.sleep(60)")
    child = run.running()["child_pid"]
    _wait_for(lambda: (run.directory / keeper.STDERR).read_bytes() == b"closed")
    assert run.state()["state"] == "running"
    assert _live([child]) == {child}
    run.stop()
    assert run.wait()["end_reason"] == "ended"


def test_a_grandchild_holding_the_pipe_is_ended_after_the_child_exits(launch: Callable[..., Run]) -> None:
    started = time.monotonic()
    run = launch("""\
import json, subprocess, sys
grandchild = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
with open('tree.json', 'w') as output:
    json.dump({'grandchild': grandchild.pid}, output)
print('child finished', flush=True)
""")
    state = run.wait()
    tree = keeper.read_json(run.cwd / "tree.json")
    assert tree
    assert time.monotonic() - started < 12
    assert state["exit_code"] == 0 and state["end_reason"] == "exited"
    assert (run.directory / keeper.STDOUT).read_bytes() == b"child finished\n"
    assert not _live(list(tree.values()))


def test_a_grandchild_with_detached_stdio_is_ended_after_the_child_exits(launch: Callable[..., Run]) -> None:
    run = launch("""\
import json, subprocess, sys
grandchild = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'],
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
with open('tree.json', 'w') as output:
    json.dump({'grandchild': grandchild.pid}, output)
""")
    state = run.wait()
    tree = keeper.read_json(run.cwd / "tree.json")
    assert tree
    assert state["exit_code"] == 0 and state["end_reason"] == "exited"
    assert not _live(list(tree.values()))


def test_a_setsid_descendant_holding_stdout_does_not_block_keeper_exit(launch: Callable[..., Run]) -> None:
    run = launch("""\
import json, subprocess, sys
grandchild = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], start_new_session=True)
with open('tree.json', 'w') as output:
    json.dump({'grandchild': grandchild.pid}, output)
print('child finished', flush=True)
""")
    tree = run.tree()
    assert run.process is not None
    bound = 2 * proc_tree.REAP_SEC + 2
    started = time.monotonic()
    assert run.process.wait(timeout=bound) == 0
    assert time.monotonic() - started < bound
    state = run.state()
    assert state["state"] == "exited" and state["end_reason"] == "exited" and state["exit_code"] == 0
    assert (run.directory / keeper.STDOUT).read_bytes() == b"child finished\n"
    # Still holding stdout outside the original tree: the bounded drain had to close its reader.
    assert _live(list(tree.values())) == set(tree.values())


def test_a_descendants_output_during_sigterm_is_logged(launch: Callable[..., Run]) -> None:
    descendant = """\
import os, signal, time
def stop(sig, frame):
    os.write(1, b'descendant ended\\n')
    raise SystemExit(0)
signal.signal(signal.SIGTERM, stop)
open('ready', 'w').close()
time.sleep(60)
"""
    run = launch(f"""\
import json, pathlib, subprocess, sys, time
grandchild = subprocess.Popen([sys.executable, '-c', {descendant!r}])
with open('tree.json', 'w') as output:
    json.dump({{'grandchild': grandchild.pid}}, output)
while not pathlib.Path('ready').exists():
    time.sleep(0.01)
""")
    state = run.wait()
    tree = keeper.read_json(run.cwd / "tree.json")
    assert tree
    assert state["exit_code"] == 0 and state["end_reason"] == "exited"
    assert not _live(list(tree.values()))
    assert (run.directory / keeper.STDOUT).read_bytes() == b"descendant ended\n"


@pytest.mark.parametrize("size", [999, 1000, 10000])
def test_stderr_cap_is_recorded_without_ending_the_run(launch: Callable[..., Run], size: int) -> None:
    run = launch(f"import os; os.write(2, b'e' * {size}); os.write(1, b'finished')",
                 keeper_setup="from looper.agents.keeper import process; process.STDERR_LIMIT_BYTES = 1000")
    state = run.wait()
    assert state["end_reason"] == "exited"
    assert state["stderr_truncated"] is (size >= 1000)
    assert (run.directory / keeper.STDERR).read_bytes() == b"e" * min(size, 1000)
    assert (run.directory / keeper.STDOUT).read_bytes() == b"finished"


def test_repeated_sigterm_during_starting_ends_the_child_once(launch: Callable[..., Run]) -> None:
    run = launch(keeper_setup="""\
import asyncio, os, signal
from looper.agents import proc_tree
original_spawn, original_end = proc_tree.spawn, proc_tree.end
async def delayed_spawn(*argv, **kwargs):
    for _ in range(2):
        os.kill(os.getpid(), signal.SIGTERM)
        await asyncio.sleep(0.05)
    return await original_spawn(*argv, **kwargs)
async def counted_end(*args, **kwargs):
    os.write(2, b'end called\\n')
    await original_end(*args, **kwargs)
proc_tree.spawn, proc_tree.end = delayed_spawn, counted_end
""")
    state = run.wait()
    assert state["end_reason"] == "ended" and state["signal"] == signal.SIGTERM
    assert not _live([state["child_pid"]])
    assert (run.directory / keeper.LOG).read_text().count("end called\n") == 1


def test_an_unreadable_owner_start_time_fails_before_spawning(launch: Callable[..., Run]) -> None:
    run = launch(keeper_setup="from looper.agents import proc_tree; proc_tree._start_time = lambda pid: None")
    state = run.wait(expected=3)
    assert state["end_reason"] == "start_failed" and state["child_pid"] is None
    assert "start time" in state["error"]


def test_a_log_write_error_ends_the_child_tree_and_records_the_error(launch: Callable[..., Run]) -> None:
    run = launch(TREE, keeper_setup="""\
from looper.agents.keeper import process
def failed_write(fd, data):
    raise OSError('injected log write failure')
process._write_all = failed_write
""")
    state = run.wait(expected=1)
    tree = keeper.read_json(run.cwd / "tree.json")
    assert tree
    assert state["state"] == "exited" and state["end_reason"] == "keeper_error"
    assert state["error"] == "injected log write failure"
    assert not _live(list(tree.values()))


def test_a_log_sync_error_still_records_the_error_state(launch: Callable[..., Run]) -> None:
    run = launch("print('finished')", keeper_setup="""\
import os, stat
original_sync = os.fsync
def failed_log_sync(fd):
    info = os.fstat(fd)
    if stat.S_ISREG(info.st_mode) and os.lseek(fd, 0, os.SEEK_CUR) == len(b'finished\\n'):
        raise OSError('injected log sync failure')
    original_sync(fd)
os.fsync = failed_log_sync
""")
    state = run.wait(expected=1)
    assert state["state"] == "exited" and state["end_reason"] == "keeper_error"
    assert state["error"] == "injected log sync failure"


def test_attaching_cancels_the_orphan_timer_and_detaching_restarts_it(tmp_path: Path) -> None:
    async def exercise():
        held = Keeper(tmp_path, tmp_path, str(tmp_path), [])
        held.orphan_timeout_s = 0.05
        held.controller_detached()
        held.controller_attached()
        await asyncio.sleep(0.1)
        assert held.end_reason is None
        held.controller_detached()
        await asyncio.wait_for(held._orphan_task, 1)
        assert held.end_reason == keeper.EndReason.ORPHANED

    asyncio.run(exercise())


@pytest.mark.parametrize("arguments", [[], ["--dir", "unused"],
                                      ["--dir", "unused", "--runtime-root", "unused", "--cwd", "unused", "--"]])
def test_incomplete_keeper_arguments_return_a_usage_error(arguments: list[str]) -> None:
    assert main(arguments) == 2
