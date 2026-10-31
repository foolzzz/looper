"""Exercise the standalone runner through its isolated Python entry point."""

import ast
import hashlib
import json
import os
from pathlib import Path
import shlex
import signal
import subprocess
import sys
import time

import pytest


RUNNER = Path(__file__).resolve().parents[1] / "looper/agents/command_runner.py"


def python_command(source):
    return f"{shlex.quote(sys.executable)} -I -c {shlex.quote(source)}"


def group_alive(pgid):
    table = subprocess.run(
        ["ps", "-A", "-o", "pid=,pgid=,stat="], capture_output=True, text=True,
        check=True, timeout=2,
    )
    return any(int(fields[1]) == pgid and not fields[2].startswith("Z")
               for line in table.stdout.splitlines() if len(fields := line.split()) == 3)


def wait_for_file(path, seconds=3):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if path.exists():
            return json.loads(path.read_text())
        time.sleep(0.02)
    pytest.fail(f"runner did not write {path.name}")


@pytest.fixture
def run_command(tmp_path):
    runs = []

    def prepare(command="printf hello", **overrides):
        directory = tmp_path / f"run-{len(runs)}"
        directory.mkdir(mode=0o700)
        spec = {
            "format": 1, "run_id": "srun-test", "schedule_id": "sched-test",
            "command": command, "work_dir": str(tmp_path), "env": {}, "env_file": None,
            "timeout_seconds": None, "kill_grace_seconds": 1, "leftover_grace_seconds": 1,
            "log_path": str(directory / "logs/output.log"), "log_max_bytes": 50 * 1048576,
            "log_tail_ring_bytes": 1048576, "schedule_env": {},
        }
        spec.update(overrides)
        (directory / "spec.json").write_text(json.dumps(spec))
        runs.append((directory, spec))
        return directory, spec

    def run(command="printf hello", **overrides):
        directory, spec = prepare(command, **overrides)
        with (directory / "runner.log").open("wb") as diagnostic:
            result = subprocess.run(
                [sys.executable, "-I", str(RUNNER), str(directory)], timeout=20,
                stdout=diagnostic, stderr=subprocess.STDOUT,
            )
        return result, json.loads((directory / "exit.json").read_text()), directory, spec

    run.prepare = prepare
    yield run
    # Also clean up when an assertion or subprocess timeout interrupts a test.
    for directory, _ in runs:
        path = directory / "runner.json"
        if path.is_file():
            runner = json.loads(path.read_text())
            pgid = runner["child_pid"]
            assert pgid != os.getpgrp()
            if group_alive(pgid):
                os.killpg(pgid, signal.SIGKILL)


def assert_group_ended(directory):
    runner = json.loads((directory / "runner.json").read_text())
    assert runner["child_pid"] != os.getpgrp()
    assert not group_alive(runner["child_pid"])


def test_output_and_private_files(run_command):
    result, ended, directory, spec = run_command()
    assert result.returncode == 0
    assert ended["end"] == "exited"
    assert ended["exit_code"] == 0
    assert ended["signal"] is None
    assert Path(spec["log_path"]).read_bytes() == b"hello"
    assert ended["tail"] == "hello"
    assert ended["log_bytes"] == 5
    assert ended["log_error"] is None
    for path in (directory / "runner.json", directory / "exit.json", Path(spec["log_path"])):
        assert path.stat().st_mode & 0o777 == 0o600
    assert Path(spec["log_path"]).parent.stat().st_mode & 0o777 == 0o700
    assert not list(directory.glob(".*.tmp"))
    assert_group_ended(directory)


@pytest.mark.parametrize(("command", "code", "sig"), [
    ("exit 3", 3, None), ("kill -TERM $$", None, 15),
])
def test_command_returncode(run_command, command, code, sig):
    result, ended, _, _ = run_command(command)
    assert result.returncode == 0
    assert ended["end"] == "exited"
    assert (ended["exit_code"], ended["signal"]) == (code, sig)


def test_binary_output_is_unchanged(run_command, tmp_path):
    original = os.urandom(1048576)
    source = tmp_path / "binary"
    source.write_bytes(original)
    result, ended, _, spec = run_command(python_command(
        f"import pathlib, sys; sys.stdout.buffer.write(pathlib.Path({str(source)!r}).read_bytes())"))
    assert result.returncode == 0
    assert hashlib.sha256(Path(spec["log_path"]).read_bytes()).digest() == hashlib.sha256(original).digest()
    assert ended["log_truncated"] is False
    assert ended["log_bytes"] == len(original)
    assert len(ended["tail"]) <= 4000


@pytest.mark.parametrize(("size", "ring_bytes"), [
    (1000, 100), (5000, 100), (131089, 100), (5000, 0),
])
def test_log_limit_keeps_prefix_and_tail(run_command, size, ring_bytes):
    output = (bytes(range(250)) * (size // 250 + 1))[:size]
    result, ended, _, spec = run_command(
        python_command(f"import sys; sys.stdout.buffer.write((bytes(range(250)) * {size // 250 + 1})[:{size}])"),
        log_max_bytes=1000, log_tail_ring_bytes=ring_bytes,
    )
    truncated = size > 1000
    dropped = max(0, size - 1000 - ring_bytes)
    marker = (
        f"\n[looper] log truncated at 0 MiB: the run wrote {size} bytes; "
        f"{dropped} bytes in the middle were dropped; the last {ring_bytes} bytes follow\n"
    ).encode("ascii")
    expected = output[:1000]
    if truncated:
        expected += marker + (output[-ring_bytes:] if ring_bytes else b"")
    assert result.returncode == 0
    assert Path(spec["log_path"]).read_bytes() == expected
    assert ended["log_truncated"] is truncated
    assert ended["log_bytes"] == size
    assert ended["log_dropped_bytes"] == dropped


@pytest.mark.parametrize("command", ["sleep 30", "trap '' TERM; sleep 30"])
def test_timeout_ends_entire_process_group(run_command, command):
    started = time.monotonic()
    result, ended, directory, _ = run_command(command, timeout_seconds=1)
    assert time.monotonic() - started < 3
    assert result.returncode == 0
    assert ended["end"] == "timed_out"
    assert_group_ended(directory)


@pytest.mark.parametrize("command", ["sleep 30 & exit 0", "trap '' TERM; sleep 30 & exit 0"])
def test_shell_exit_cleans_up_background_process(run_command, command):
    started = time.monotonic()
    result, ended, directory, _ = run_command(command)
    assert time.monotonic() - started < 3
    assert result.returncode == 0
    assert ended["end"] == "exited"
    assert ended["exit_code"] == 0
    assert_group_ended(directory)


def test_closed_pipe_does_not_finish_running_command(run_command):
    result, ended, _, _ = run_command("exec 1>&- 2>&-; sleep 30", timeout_seconds=0.3)
    assert result.returncode == 0
    assert ended["end"] == "timed_out"


def test_escaped_process_holding_pipe_has_bounded_drain(run_command, tmp_path):
    pid_path = tmp_path / "escaped-pid"
    command = python_command(
        "import pathlib, subprocess, sys; "
        "child = subprocess.Popen([sys.executable, '-I', '-c', 'import time; time.sleep(30)'], "
        "start_new_session=True); "
        f"pathlib.Path({str(pid_path)!r}).write_text(str(child.pid))"
    )
    started = time.monotonic()
    try:
        result, ended, directory, _ = run_command(command)
        assert 2 <= time.monotonic() - started < 4
        assert result.returncode == 0
        assert ended["end"] == "exited"
        assert_group_ended(directory)
        assert group_alive(int(pid_path.read_text()))
    finally:
        if pid_path.exists():
            pgid = int(pid_path.read_text())
            os.killpg(pgid, signal.SIGKILL)
            deadline = time.monotonic() + 1
            while group_alive(pgid) and time.monotonic() < deadline:
                time.sleep(0.02)
            assert not group_alive(pgid)


def test_environment_precedence(run_command, tmp_path):
    env_file = tmp_path / "command.env"
    env_file.write_text("FOO=file\nBAR=file\n")
    result, ended, _, spec = run_command(
        'printf "FOO=%s BAR=%s LOOPER_SCHEDULE_ID=%s" "$FOO" "$BAR" "$LOOPER_SCHEDULE_ID"',
        env_file=str(env_file), env={"BAR": "env", "LOOPER_SCHEDULE_ID": "fake"},
        schedule_env={"LOOPER_SCHEDULE_ID": "real"},
    )
    assert result.returncode == 0
    assert ended["exit_code"] == 0
    assert Path(spec["log_path"]).read_text() == "FOO=file BAR=env LOOPER_SCHEDULE_ID=real"


@pytest.mark.parametrize(("content", "expected", "bad_line"), [
    ('export A=1\r\nB="two words"\nC=\'q\'\nD=a=b\n # comment\n\n',
     {"A": "1", "B": "two words", "C": "q", "D": "a=b"}, None),
    ("A=1\n# comment\noops\n", {}, 3),
    ("1BAD=secret\n", {}, 1),
])
def test_env_file_parser_in_isolated_process(tmp_path, content, expected, bad_line):
    env_file = tmp_path / "command.env"
    env_file.write_text(content)
    code = (
        "import json, runpy, sys; "
        "runner = runpy.run_path(sys.argv[1]); "
        "print(json.dumps(runner['parse_env_file'](sys.argv[2])))"
    )
    result = subprocess.run([sys.executable, "-I", "-c", code, str(RUNNER), str(env_file)],
                            capture_output=True, text=True, timeout=4, check=True)
    values, error = json.loads(result.stdout)
    assert values == expected
    if bad_line is None:
        assert error is None
    else:
        assert error == f"env_file {env_file}: line {bad_line} is not KEY=VALUE"
        assert "oops" not in error
        assert "secret" not in error


def test_env_file_secret_is_only_in_raw_log(run_command, tmp_path):
    env_file = tmp_path / "secret.env"
    env_file.write_text("SECRET=s3cr3t-value\nLONG=s3cr3t-value-extra\n")
    result, ended, directory, spec = run_command('echo "$LONG $SECRET"', env_file=str(env_file))
    assert result.returncode == 0
    assert Path(spec["log_path"]).read_text() == "s3cr3t-value-extra s3cr3t-value\n"
    assert ended["tail"] == "[redacted] [redacted]\n"
    assert "s3cr3t-value" not in (directory / "exit.json").read_text()
    assert "s3cr3t-value" not in (directory / "runner.log").read_text()


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGINT, signal.SIGHUP])
def test_runner_forwards_termination(run_command, sig):
    directory, _ = run_command.prepare("sleep 30")
    with (directory / "runner.log").open("wb") as diagnostic:
        process = subprocess.Popen([sys.executable, "-I", str(RUNNER), str(directory)],
                                   stdout=diagnostic, stderr=subprocess.STDOUT)
        try:
            runner = wait_for_file(directory / "runner.json")
            os.kill(runner["runner_pid"], sig)
            assert process.wait(timeout=4) == 0
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=2)
    ended = json.loads((directory / "exit.json").read_text())
    assert ended["end"] == "runner_terminated"
    assert_group_ended(directory)


@pytest.mark.parametrize("failure", ["work_dir", "env_file", "invalid_env_file", "log_path"])
def test_start_failure(run_command, tmp_path, failure):
    overrides = {}
    if failure in ("work_dir", "env_file"):
        overrides[failure] = str(tmp_path / "missing")
    elif failure == "invalid_env_file":
        path = tmp_path / "invalid.env"
        path.write_text("SECRET=hidden-value\noops\n")
        overrides["env_file"] = str(path)
    else:
        path = tmp_path / "regular-file"
        path.touch()
        overrides["log_path"] = str(path / "output.log")
    result, ended, _, _ = run_command(**overrides)
    assert result.returncode == 0
    assert ended["end"] == "start_failed"
    assert ended["exit_code"] is None
    assert ended["signal"] is None
    assert ended["started_at"] is None
    assert ended["error"]
    if failure == "work_dir":
        assert "is not a directory" in ended["error"]
    assert "hidden-value" not in ended["error"]
    assert "oops" not in ended["error"]


def test_runner_survives_parent_exit(run_command):
    directory, _ = run_command.prepare("sleep 0.2; printf survived")
    source = (
        "import subprocess, sys; "
        "log = open(sys.argv[3], 'wb'); "
        "subprocess.Popen([sys.executable, '-I', sys.argv[1], sys.argv[2]], "
        "start_new_session=True, stdin=subprocess.DEVNULL, stdout=log, stderr=log, close_fds=True)"
    )
    subprocess.run([sys.executable, "-I", "-c", source, str(RUNNER), str(directory),
                    str(directory / "runner.log")], check=True, timeout=4)
    ended = wait_for_file(directory / "exit.json")
    assert ended["end"] == "exited"
    assert ended["exit_code"] == 0
    assert ended["tail"] == "survived"
    assert_group_ended(directory)


@pytest.mark.parametrize("content", [None, "{invalid json"])
def test_unreadable_spec_only_writes_diagnostic(run_command, content):
    directory, _ = run_command.prepare()
    path = directory / "spec.json"
    if content is None:
        path.unlink()
    else:
        path.write_text(content)
    result = subprocess.run([sys.executable, "-I", str(RUNNER), str(directory)],
                            capture_output=True, text=True, timeout=4)
    assert result.returncode == 2
    assert len(result.stderr.splitlines()) == 1
    assert not (directory / "exit.json").exists()


def test_unknown_format_is_runner_error(run_command):
    result, ended, _, _ = run_command(format=2)
    assert result.returncode == 1
    assert ended["end"] == "runner_error"
    assert "format" in ended["error"]


def test_metadata_failure_ends_started_process_group(run_command):
    directory, _ = run_command.prepare("sleep 30")
    (directory / "runner.json").mkdir()
    result = subprocess.run([sys.executable, "-I", str(RUNNER), str(directory)],
                            capture_output=True, text=True, timeout=4)
    assert result.returncode == 1
    ended = json.loads((directory / "exit.json").read_text())
    assert ended["end"] == "runner_error"
    runner = json.loads((directory / ".runner.json.tmp").read_text())
    assert not group_alive(runner["child_pid"])


def test_log_write_failure_preserves_command_outcome(run_command):
    directory, spec = run_command.prepare(python_command("import sys; sys.stdout.write('x' * 32768)"))
    # A real filesystem write limit exercises partial writes followed by EFBIG.
    source = (
        "import resource, runpy, signal, sys; "
        "resource.setrlimit(resource.RLIMIT_FSIZE, (16384, 16384)); "
        "signal.signal(signal.SIGXFSZ, signal.SIG_IGN); "
        "sys.argv = sys.argv[1:]; runpy.run_path(sys.argv[0], run_name='__main__')"
    )
    result = subprocess.run([sys.executable, "-I", "-c", source, str(RUNNER), str(directory)],
                            capture_output=True, text=True, timeout=4)
    assert result.returncode == 0
    ended = json.loads((directory / "exit.json").read_text())
    assert ended["end"] == "exited"
    assert ended["exit_code"] == 0
    assert ended["log_bytes"] == 32768
    assert ended["log_error"]
    assert Path(spec["log_path"]).stat().st_size == 16384
    assert_group_ended(directory)


def test_missing_ps_still_kills_timed_out_process_group(run_command):
    directory, _ = run_command.prepare("sleep 30", timeout_seconds=0.1, kill_grace_seconds=0.1)
    source = (
        "import runpy, sys; runner = runpy.run_path(sys.argv[1]); "
        "runner['_PS_ENV']['PATH'] = '/nonexistent'; "
        "sys.exit(runner['main']([sys.argv[2]]))"
    )
    result = subprocess.run([sys.executable, "-I", "-c", source, str(RUNNER), str(directory)],
                            capture_output=True, text=True, timeout=4)
    assert result.returncode == 1
    runner = json.loads((directory / "runner.json").read_text())
    assert runner["runner_started"] is None
    assert runner["child_started"] is None
    ended = json.loads((directory / "exit.json").read_text())
    assert ended["end"] == "runner_error"
    assert "after SIGKILL" in ended["error"]
    assert_group_ended(directory)


def test_imports_are_top_level_and_standard_library():
    tree = ast.parse(RUNNER.read_text())
    top_level = {id(node) for node in tree.body}
    imports = [node for node in ast.walk(tree) if isinstance(node, (ast.Import, ast.ImportFrom))]
    assert imports
    for node in imports:
        assert id(node) in top_level
        names = [alias.name for alias in node.names] if isinstance(node, ast.Import) else [node.module]
        if isinstance(node, ast.ImportFrom):
            assert node.level == 0
        assert all(name.split(".")[0] in sys.stdlib_module_names for name in names)
