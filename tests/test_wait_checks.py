"""The wait check executor: a fixed ``gh pr view`` and a project-gated shell command.

Neither path calls the real ``gh`` or a model. A stand-in records its argv and prints
the bytes the test gives it. Output is the tail of the combined stream; logs stay
free of that output and of the command text.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shlex
import signal
import subprocess
import sys
import time
import tracemalloc
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from looper.agents import proc_tree, wait_service
from looper.agents.waits import (
    OUTPUT_MAX_BYTES, CheckResult, CommandCheck, GithubPrCheck, ReadyWhen,
)
from looper.agents.wait_service import run_command, run_github_pr

REPO = "owner/name"
NUMBER = 12
JSON_FIELDS = "headRefOid,state,comments,reviews,statusCheckRollup"
SECRET = "super-secret-value"
SECRET_ENV = "SOME_SECRET"
OUTPUT_MARKER = "SECRET_OUTPUT_MARKER_9f3a"
COMMAND_MARKER = "SECRET_COMMAND_MARKER_9f3a"


def _pr(**changes: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "headRefOid": "1a2b3c4", "state": "OPEN", "comments": [], "reviews": [], "statusCheckRollup": [],
    }
    body.update(changes)
    return body


def _check(ready_when: ReadyWhen) -> GithubPrCheck:
    return GithubPrCheck(REPO, NUMBER, ready_when)


def _write_gh(directory: Path, payload: bytes, *, exit_code: int = 0, sleep: float = 0,
              pid_file: Path | None = None, env_file: Path | None = None) -> tuple[Path, Path]:
    """A ``gh`` that records argv, optionally the environment and its pid, then prints ``payload``."""
    argv_file = directory / "gh-argv.json"
    script = directory / "gh"
    script.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys, time, pathlib\n"
        f"pathlib.Path({str(argv_file)!r}).write_text(json.dumps(sys.argv))\n"
        + (f"pathlib.Path({str(env_file)!r}).write_text(json.dumps(dict(os.environ)))\n" if env_file else "")
        + (f"pathlib.Path({str(pid_file)!r}).write_text(str(os.getpid()))\n" if pid_file else "")
        + (f"time.sleep({sleep!r})\n" if sleep else "")
        + f"sys.stdout.buffer.write({payload!r})\n"
        f"raise SystemExit({exit_code})\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    return script, argv_file


def _running(pids: list[int], *, settle: float = 2.0) -> list[int]:
    """Those of ``pids`` still running (a zombie has exited), after up to ``settle`` s."""
    deadline = time.monotonic() + settle
    while True:
        listed = subprocess.run(["ps", "-A", "-o", "pid=,stat="], capture_output=True, text=True, check=True)
        live = {int(pid) for pid, stat in (line.split()[:2] for line in listed.stdout.splitlines()) if stat[0] != "Z"}
        running = [pid for pid in pids if pid in live]
        if not running or time.monotonic() >= deadline:
            return running
        time.sleep(0.05)


def _github(tmp_path: Path, body: dict[str, Any] | bytes, baseline: dict[str, Any] | None,
            ready_when: ReadyWhen, *, exit_code: int = 0, sleep: float = 0,
            monkeypatch: pytest.MonkeyPatch | None = None) -> tuple[Any, list[str]]:
    payload = body if isinstance(body, bytes) else json.dumps(body).encode()
    env_file = tmp_path / "gh-env.json"
    script, argv_file = _write_gh(tmp_path, payload, exit_code=exit_code, sleep=sleep, env_file=env_file)
    if monkeypatch is not None:
        monkeypatch.setenv(SECRET_ENV, SECRET)
    started = time.monotonic()
    outcome = asyncio.run(run_github_pr(_check(ready_when), baseline, gh=str(script)))
    assert time.monotonic() - started < 30
    argv = json.loads(argv_file.read_text(encoding="utf-8"))
    assert argv == [str(script), "pr", "view", str(NUMBER), "--repo", REPO, "--json", JSON_FIELDS]
    if (recorded := json.loads(env_file.read_text(encoding="utf-8"))) is not None:
        assert SECRET_ENV not in recorded
        assert "LOOPER_DATABASE_URL" not in recorded
        assert "PATH" in recorded
    return outcome, argv


def test_github_first_view_returns_a_baseline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # The injected executable must work without any machine-installed gh on PATH.
    monkeypatch.setenv("PATH", str(tmp_path))
    body = _pr(comments=[{"body": "one"}], reviews=[])
    (result, output, error, extra), _ = _github(tmp_path, body, None, ReadyWhen.HEAD_CHANGED, monkeypatch=monkeypatch)
    assert result is CheckResult.NOT_READY
    assert error == ""
    assert extra["baseline"] == {"head": "1a2b3c4", "comments": 1}
    assert extra["resolution"] == ""
    assert "1a2b3c4" in output


def test_github_check_uses_the_task_directory(tmp_path: Path) -> None:
    task_dir = tmp_path / "task"
    task_dir.mkdir()
    (task_dir / "head").write_text("new-head")
    script = tmp_path / "gh"
    script.write_text(f"#!{sys.executable}\nimport json\nfrom pathlib import Path\n"
                      "print(json.dumps({'headRefOid': Path('head').read_text(), 'state': 'OPEN', "
                      "'comments': [], 'reviews': [], 'statusCheckRollup': []}))\n")
    script.chmod(0o755)
    result, _, error, extra = asyncio.run(run_github_pr(
        _check(ReadyWhen.HEAD_CHANGED), {"head": "old-head", "comments": 0}, gh=str(script), work_dir=str(task_dir)))
    assert result is CheckResult.READY
    assert error == ""
    assert extra["baseline"]["head"] == "new-head"


def test_github_head_unchanged_is_not_ready(tmp_path: Path) -> None:
    baseline = {"head": "1a2b3c4", "comments": 0}
    (result, _output, error, extra), _ = _github(tmp_path, _pr(), baseline, ReadyWhen.HEAD_CHANGED)
    assert result is CheckResult.NOT_READY
    assert error == ""
    assert extra["baseline"] == baseline
    assert extra["resolution"] == ""


def test_github_head_change_is_ready(tmp_path: Path) -> None:
    (result, _output, error, extra), _ = _github(
        tmp_path, _pr(headRefOid="5d6e7f8"), {"head": "1a2b3c4", "comments": 0}, ReadyWhen.HEAD_CHANGED)
    assert result is CheckResult.READY
    assert error == ""
    assert extra["baseline"] == {"head": "5d6e7f8", "comments": 0}
    assert extra["resolution"] == "Ready: the head of owner/name#12 changed (1a2b3c4 → 5d6e7f8)."


def test_github_reads_the_whole_view_and_returns_its_tail(tmp_path: Path) -> None:
    # The head sits at the start of gh's JSON. A long comment pushes it outside the stored tail.
    body = _pr(headRefOid="5d6e7f8", comments=[{"body": "x" * 4000}])
    raw = json.dumps(body).encode()
    assert len(raw) > OUTPUT_MAX_BYTES
    (result, output, error, extra), _ = _github(
        tmp_path, raw, {"head": "1a2b3c4", "comments": 0}, ReadyWhen.HEAD_CHANGED)
    assert result is CheckResult.READY
    assert error == ""
    assert extra["baseline"] == {"head": "5d6e7f8", "comments": 1}
    assert extra["resolution"] == "Ready: the head of owner/name#12 changed (1a2b3c4 → 5d6e7f8)."
    assert output == raw[-OUTPUT_MAX_BYTES:].decode("utf-8", errors="replace")
    assert "5d6e7f8" not in output
    assert "5d6e7f8" in extra["resolution"]


@pytest.mark.parametrize(("rollup", "ready"), [
    ([], False),
    ([{"__typename": "CheckRun", "status": "IN_PROGRESS", "name": "ci"}], False),
    ([{"__typename": "StatusContext", "state": "PENDING", "context": "ci"}], False),
    ([{"__typename": "StatusContext", "state": "EXPECTED", "context": "ci"}], False),
    ([{"__typename": "CheckRun", "status": "COMPLETED", "conclusion": "SUCCESS"},
      {"__typename": "StatusContext", "state": "PENDING", "context": "ci"}], False),
    ([{"__typename": "CheckRun", "status": "COMPLETED", "conclusion": "FAILURE"},
      {"__typename": "StatusContext", "state": "FAILURE", "context": "ci"}], True),
])
def test_github_checks_finished(tmp_path: Path, rollup: list[dict[str, Any]], ready: bool) -> None:
    (result, _output, error, extra), _ = _github(
        tmp_path, _pr(statusCheckRollup=rollup), {"head": "1a2b3c4", "comments": 0}, ReadyWhen.CHECKS_FINISHED)
    assert error == ""
    assert result is (CheckResult.READY if ready else CheckResult.NOT_READY)
    if ready:
        assert extra["resolution"] == "Ready: the checks of owner/name#12 finished."
    else:
        assert extra["resolution"] == ""
    assert extra["baseline"] == {"head": "1a2b3c4", "comments": 0}


def test_github_new_comments_count_reviews(tmp_path: Path) -> None:
    body = _pr(comments=[{"body": "a"}, {"body": "b"}], reviews=[{"body": "r"}])
    (grown, _output, error, extra), _ = _github(
        tmp_path, body, {"head": "1a2b3c4", "comments": 2}, ReadyWhen.NEW_COMMENTS)
    assert error == ""
    assert grown is CheckResult.READY
    assert extra["baseline"] == {"head": "1a2b3c4", "comments": 3}
    assert extra["resolution"] == "Ready: owner/name#12 has new comments (2 → 3)."

    same = tmp_path / "same"
    same.mkdir()
    (unchanged, _, _, extra_same), _ = _github(
        same, body, {"head": "1a2b3c4", "comments": 3}, ReadyWhen.NEW_COMMENTS)
    assert unchanged is CheckResult.NOT_READY
    assert extra_same["resolution"] == ""


@pytest.mark.parametrize(("state", "sentence"), [
    ("MERGED", "Ready: owner/name#12 was merged."),
    ("CLOSED", "Ready: owner/name#12 was closed."),
])
def test_github_merged_or_closed_is_ready(tmp_path: Path, state: str, sentence: str) -> None:
    # The head did not change; closing the request is still ready, and the sentence says which.
    (result, _output, error, extra), _ = _github(
        tmp_path, _pr(state=state), {"head": "1a2b3c4", "comments": 0}, ReadyWhen.HEAD_CHANGED)
    assert result is CheckResult.READY
    assert error == ""
    assert extra["resolution"] == sentence
    assert extra["baseline"]["head"] == "1a2b3c4"


def test_github_nonzero_and_bad_json_are_errors(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    failed = tmp_path / "fail"
    failed.mkdir()
    marker = "SECRET_GH_ERROR_MARKER"
    (result, output, error, extra), _ = _github(failed, marker.encode(), {"head": "1a2b3c4", "comments": 0},
                                                ReadyWhen.HEAD_CHANGED, exit_code=1)
    assert result is CheckResult.ERROR
    assert error == "gh exited 1"
    assert output == marker
    assert extra["baseline"] is None
    assert extra["resolution"] == ""

    bad = tmp_path / "bad"
    bad.mkdir()
    payload = b'{"SECRET_BAD_JSON_MARKER": '
    (result, output, error, extra), _ = _github(bad, payload, None, ReadyWhen.CHECKS_FINISHED)
    assert result is CheckResult.ERROR
    assert error == "the pull request view is not valid JSON"
    assert output == payload.decode()
    assert extra["baseline"] is None
    assert marker not in caplog.text
    assert "SECRET_BAD_JSON_MARKER" not in caplog.text


def test_github_timeout_is_killed_and_reaped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("looper.agents.wait_service.CHECK_TIMEOUT_SEC", 1.5)
    parent = tmp_path / "gh.pid"
    child = tmp_path / "gh-child.pid"
    script = tmp_path / "gh"
    # The shell sleeps in a child, which inherits the pipe. Reaping has to outlive that child.
    script.write_text(
        "#!/bin/sh\n"
        f"echo $$ > {shlex.quote(str(parent))}\n"
        f"sleep 60 & echo $! > {shlex.quote(str(child))}\n"
        "wait\n",
        encoding="utf-8")
    script.chmod(0o755)
    started = time.monotonic()
    result, output, error, extra = asyncio.run(run_github_pr(_check(ReadyWhen.HEAD_CHANGED), None, gh=str(script)))
    pids: list[int] = []
    try:
        assert time.monotonic() - started < 20
        assert result is CheckResult.ERROR
        assert error == "timed out after 60 s"
        assert output == ""
        assert extra["baseline"] is None
        assert extra["resolution"] == ""
        pids = [int(parent.read_text()), int(child.read_text())]
        assert _running(pids) == []
    finally:
        for path in (parent, child):
            if not path.exists():
                continue
            try:
                os.kill(int(path.read_text()), signal.SIGKILL)
            except (ProcessLookupError, ValueError):
                pass


def test_command_exit_codes_cwd_and_merged_streams(tmp_path: Path) -> None:
    work = tmp_path / "work"
    work.mkdir()
    other = tmp_path / "other"
    other.mkdir()

    ready, output, error, extra = asyncio.run(run_command(
        CommandCheck("echo OUT_LINE; echo ERR_LINE >&2"), work, project_allows=True))
    assert ready is CheckResult.READY
    assert error == ""
    assert "OUT_LINE" in output and "ERR_LINE" in output
    assert extra["resolution"] == "Ready: the command exited 0."
    assert extra["baseline"] is None

    marker = work / "marker"
    touched, _, _, extra = asyncio.run(run_command(CommandCheck("touch marker"), work, project_allows=True))
    assert touched is CheckResult.READY
    assert marker.is_file()
    assert not (other / "marker").exists()
    assert extra["exit_code"] == 0

    failed, fail_output, fail_error, fail_extra = asyncio.run(run_command(
        CommandCheck("echo NOPE; exit 1"), work, project_allows=True))
    assert failed is CheckResult.NOT_READY
    assert fail_error == ""
    assert "NOPE" in fail_output
    assert fail_extra["resolution"] == ""
    assert fail_extra["exit_code"] == 1


def test_command_environment_drops_unlisted_variables(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(SECRET_ENV, SECRET)
    monkeypatch.setenv("LOOPER_DATABASE_URL", "postgres://kept-out")
    code = (
        "import os; "
        "print('SECRET=' + os.environ.get('SOME_SECRET', '')); "
        "print('DB=' + os.environ.get('LOOPER_DATABASE_URL', '')); "
        "print('HAS_PATH=' + ('1' if os.environ.get('PATH') else '0'))"
    )
    command = f"{shlex.quote(sys.executable)} -c {shlex.quote(code)}"
    result, output, error, _extra = asyncio.run(run_command(CommandCheck(command), tmp_path, project_allows=True))
    assert result is CheckResult.READY
    assert error == ""
    assert SECRET not in output
    assert "postgres://kept-out" not in output
    assert "SECRET=" in output
    assert "HAS_PATH=1" in output


def test_command_output_keeps_the_tail_across_a_utf8_boundary(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    character = "你".encode()
    end = b"VISIBLE_TAIL"
    suffix = b"Q" * (OUTPUT_MAX_BYTES - len(character) + 1 - len(end)) + end
    payload = b"P" * 3000 + b"HEADMARKER" + character + suffix
    assert len(payload) >= 5000
    cut = payload[-OUTPUT_MAX_BYTES:]
    assert cut[0] & 0xC0 == 0x80
    path = tmp_path / "blob"
    path.write_bytes(payload)
    command = f"cat {shlex.quote(str(path))}"
    result, output, error, extra = asyncio.run(run_command(CommandCheck(command), tmp_path, project_allows=True))
    assert result is CheckResult.READY
    assert error == ""
    assert extra["resolution"] == "Ready: the command exited 0."
    assert output == cut.decode("utf-8", errors="replace")
    assert output.endswith("VISIBLE_TAIL")
    assert "HEADMARKER" not in output
    assert COMMAND_MARKER not in caplog.text
    assert "HEADMARKER" not in caplog.text


def test_command_timeout_ends_the_process_tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("looper.agents.wait_service.CHECK_TIMEOUT_SEC", 1.5)
    monkeypatch.setattr(proc_tree, "TERM_GRACE_SEC", 0.4)
    parent = tmp_path / "parent.pid"
    child = tmp_path / "child.pid"
    command = f"sleep 120 & echo $! > {shlex.quote(str(child))}; echo $$ > {shlex.quote(str(parent))}; wait"
    started = time.monotonic()
    pids: list[int] = []
    try:
        result, _output, error, extra = asyncio.run(run_command(CommandCheck(command), tmp_path, project_allows=True))
        assert time.monotonic() - started < 20
        assert result is CheckResult.ERROR
        assert error == "timed out after 60 s"
        assert extra["resolution"] == ""
        assert extra["baseline"] is None
        pids = [int(parent.read_text()), int(child.read_text())]
        assert _running(pids) == []
    finally:
        for path in (parent, child):
            if not path.exists():
                continue
            try:
                os.kill(int(path.read_text()), signal.SIGKILL)
            except (ProcessLookupError, ValueError):
                pass


@pytest.mark.parametrize("exit_code", [0, 1])
@pytest.mark.parametrize("inherit_output", [True, False])
def test_command_exit_ends_background_children(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, exit_code: int, inherit_output: bool,
) -> None:
    monkeypatch.setattr(proc_tree, "TERM_GRACE_SEC", 0.2)
    child = tmp_path / "child.pid"
    redirect = "" if inherit_output else ">/dev/null 2>&1"
    command = f"sleep 120 {redirect} & echo $! > {shlex.quote(str(child))}; exit {exit_code}"

    async def test() -> None:
        try:
            result, output, error, extra = await run_command(CommandCheck(command), tmp_path, project_allows=True)
            assert result is (CheckResult.READY if exit_code == 0 else CheckResult.NOT_READY)
            assert output == error == "" and extra["exit_code"] == exit_code
            assert _running([int(child.read_text())]) == []
        finally:
            if child.exists():
                try:
                    os.kill(int(child.read_text()), signal.SIGKILL)
                except ProcessLookupError:
                    pass
                await asyncio.sleep(0.05)  # Close inherited pipes before the test's event loop exits.

    asyncio.run(test())


def test_command_does_not_run_when_the_project_disallows_it(tmp_path: Path) -> None:
    work = tmp_path / "work"
    work.mkdir()
    target = work / "marker"
    result, output, error, extra = asyncio.run(run_command(
        CommandCheck("touch marker"), work, project_allows=False))
    assert result is CheckResult.ERROR
    assert error == "wait_commands is off"
    assert output == ""
    assert extra["resolution"] == ""
    assert not target.exists()


def test_logs_omit_command_text_and_output(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    command = f"echo {OUTPUT_MARKER}; echo {COMMAND_MARKER}"
    result, output, _error, extra = asyncio.run(run_command(CommandCheck(command), tmp_path, project_allows=True))
    assert result is CheckResult.READY
    assert OUTPUT_MARKER in output and COMMAND_MARKER in output
    assert extra["resolution"] == "Ready: the command exited 0."
    text = caplog.text
    assert OUTPUT_MARKER not in text
    assert COMMAND_MARKER not in text


def test_command_output_memory_is_bounded_while_streaming(tmp_path: Path) -> None:
    command = shlex.join([sys.executable, "-c",
                          "import os; [os.write(1, b'x' * 65536) for _ in range(256)]; os.write(1, b'END')"])
    tracemalloc.start()
    try:
        result, output, error, _ = asyncio.run(run_command(CommandCheck(command), tmp_path, project_allows=True))
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert result is CheckResult.READY and error == ""
    assert output == "x" * (OUTPUT_MAX_BYTES - 3) + "END"
    # The child produces 16 MiB; the parent's retained output must not grow with it.
    assert peak < 2 * 1024 * 1024


def test_github_oversized_view_fails_with_bounded_memory(tmp_path: Path) -> None:
    script = tmp_path / "gh"
    script.write_text(f"#!{sys.executable}\nimport os\n"
                      "[os.write(1, b'x' * 65536) for _ in range(256)]\n"
                      "os.write(1, b'END')\n")
    script.chmod(0o755)
    tracemalloc.start()
    try:
        result, output, error, extra = asyncio.run(
            run_github_pr(_check(ReadyWhen.HEAD_CHANGED), None, gh=str(script)))
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert result is CheckResult.ERROR
    assert error == "the pull request view exceeds the 1048576-byte limit"
    assert extra["baseline"] is None and extra["resolution"] == ""
    assert output == "x" * (OUTPUT_MAX_BYTES - 3) + "END"
    assert peak < 4 * 1024 * 1024


@pytest.mark.parametrize("finish", ["exit", "cancel"])
def test_command_is_recorded_until_reaped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, finish: str,
) -> None:
    async def test() -> None:
        records = tmp_path / "process-records"
        records.mkdir()
        monkeypatch.setattr(proc_tree, "_records", records)
        monkeypatch.setattr(proc_tree, "_server", (os.getpid(), "current-server"))
        # Process identity comes from the OS; keep this test runnable when the sandbox denies ps.
        monkeypatch.setattr(proc_tree, "_start_time", lambda _pid: "check-started")
        entered = asyncio.Event()
        capture = wait_service._capture
        observed: list[asyncio.subprocess.Process] = []

        async def observed_capture(process: asyncio.subprocess.Process, *args: Any, **kwargs: Any) -> Any:
            observed.append(process)
            entered.set()
            outcome = await capture(process, *args, **kwargs)
            await asyncio.sleep(0)  # Let the normal-exit record removal run, unless cleanup owns it.
            assert (records / f"{process.pid}.json").is_file()
            return outcome

        monkeypatch.setattr(wait_service, "_capture", observed_capture)
        command = CommandCheck("sleep 30" if finish == "cancel" else "sleep 0.2")
        task = asyncio.create_task(run_command(command, tmp_path, project_allows=True))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            process = observed[0]
            path = records / f"{process.pid}.json"
            assert path.is_file(), "a running check must be available to the next startup sweep"
            record = json.loads(path.read_text())
            assert record["pid"] == process.pid and record["server_pid"] == os.getpid()
            if finish == "cancel":
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            if finish == "exit":
                assert task.result()[0] is CheckResult.READY
            else:
                assert task.cancelled()
            if proc_tree._forgetting:
                await asyncio.gather(*proc_tree._forgetting)
            assert not path.exists() and process.pid not in proc_tree._recorded
            assert process.returncode is not None
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            for process in observed:
                if process.returncode is None:
                    await wait_service._end_command(process)

    asyncio.run(test())


@pytest.mark.parametrize(("wait_state", "task_status"), [
    ("ready", "waiting"), ("cancel_requested", "waiting"), ("timed_out", "waiting"),
    ("waiting", "stopped"), ("waiting", "failed"), ("waiting", "completed"),
    ("waiting", "cancelled"),
])
def test_queued_command_is_not_dispatched_after_state_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, wait_state: str, task_status: str,
) -> None:
    """Exercise the semaphore race without PostgreSQL; a second queued command is a positive control."""
    async def test() -> None:
        rows = [{"task_id": name, "step_id": "step-" + name, "step_key": "w1", "state": "waiting"}
                for name in ("changed", "control")]
        current = {row["task_id"]: dict(row) for row in rows}
        tasks = {name: {"status": "waiting", "work_dir": str(tmp_path)} for name in current}
        selected = asyncio.Event()

        async def due(*_args: Any, **_kwargs: Any) -> Any:
            selected.set()
            return rows

        async def get(task_id: str, *_args: Any) -> Any:
            return current[task_id]

        async def get_task(task_id: str) -> Any:
            return tasks[task_id]

        monkeypatch.setattr(wait_service.db_waits, "due", due)
        monkeypatch.setattr(wait_service.db_waits, "get", get)
        monkeypatch.setattr(wait_service.db_tasks, "get_task", get_task)
        service = wait_service.WaitService(bus=None, has_planner=lambda _: False)
        service._semaphore = asyncio.Semaphore(1)

        async def execute(row: Any, _now: Any) -> int:
            result, _, _, _ = await run_command(
                CommandCheck(f"touch {row['task_id']}"), tmp_path, project_allows=True)
            return int(result is CheckResult.READY)

        monkeypatch.setattr(service, "_run_wait", execute)
        await service._semaphore.acquire()
        pending = asyncio.create_task(service.run_once())
        try:
            await asyncio.wait_for(selected.wait(), 2)
            current["changed"]["state"] = wait_state
            tasks["changed"]["status"] = task_status
        finally:
            service._semaphore.release()
        assert await asyncio.wait_for(pending, 5) == 1
        assert not (tmp_path / "changed").exists()
        assert (tmp_path / "control").is_file()

    asyncio.run(test())


@pytest.mark.parametrize("status", [None, "stopped", "failed", "completed", "cancelled", "init", "waiting"])
def test_timeout_notice_rechecks_task_status(monkeypatch: pytest.MonkeyPatch, status: str | None) -> None:
    from looper.agents.message import MessageType

    async def test() -> None:
        task = {"status": status, "origin_channel_id": "channel-telegram", "chat_kind": "chat",
                "chat_id": "chat-1", "title": "A task"}
        monkeypatch.setattr(wait_service.db_tasks, "get_task", AsyncMock(return_value=task if status else None))
        notice = AsyncMock()
        monkeypatch.setattr(wait_service.db_waits, "set_notice", notice)
        bus = AsyncMock()
        service = wait_service.WaitService(bus=bus, has_planner=lambda _: False)
        await service._notify_timeout({"task_id": "task-a", "step_id": "step-w1", "topic": "CI finishes"}, 60)
        if status == "waiting":
            bus.publish.assert_awaited_once()
            assert bus.publish.await_args.args[0].message_type is MessageType.CHAT_NOTICE
            notice.assert_awaited_once_with("step-w1", "sent")
        else:
            bus.publish.assert_not_awaited()
            notice.assert_not_awaited()

    asyncio.run(test())


@pytest.mark.parametrize(("queued", "result", "earliest", "deadline", "moved_to"), [
    (True, CheckResult.READY, 60, 1200, "ready"),
    (True, CheckResult.NOT_READY, 1200, 60, "timed_out"),
    (False, CheckResult.READY, 60, 1200, "ready"),
    (False, CheckResult.NOT_READY, 1200, 60, "timed_out"),
    (False, CheckResult.NOT_READY, 1200, 1800, None),
])
def test_wait_uses_execution_and_completion_times(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, queued: bool, result: CheckResult,
    earliest: int, deadline: int, moved_to: str | None,
) -> None:
    async def test() -> None:
        opened = datetime(2026, 10, 19, tzinfo=timezone.utc)
        current = opened

        class Clock(datetime):
            @classmethod
            def now(cls, tz: Any = None) -> datetime:
                return current

        monkeypatch.setattr(wait_service, "datetime", Clock)
        spec = wait_service.waits.WaitSpec("ready", 1800, check=CommandCheck("true"), every_seconds=300)
        row = {"step_id": "step-w1", "task_id": "task-a", "step_key": "w1", "state": "waiting",
               "topic": spec.topic, "spec": spec.row(), "opened_at": opened, "next_check_at": opened,
               "due_at": opened + timedelta(seconds=earliest), "deadline_at": opened + timedelta(seconds=deadline),
               "baseline": None, "last_result": None}
        selected = asyncio.Event()

        async def due(*_args: Any) -> Any:
            selected.set()
            return [row]

        async def check(*_args: Any, **_kwargs: Any) -> Any:
            nonlocal current
            if queued:
                assert current == opened + timedelta(seconds=650)
            else:
                current = opened + timedelta(seconds=650)
            return result, "", "", {"baseline": None, "resolution": "Ready.", "exit_code": 0}

        recorded = AsyncMock(return_value=True)
        moved = AsyncMock(return_value={"state": moved_to})
        monkeypatch.setattr(wait_service.db_waits, "due", due)
        monkeypatch.setattr(wait_service.db_waits, "get", AsyncMock(return_value=row))
        monkeypatch.setattr(wait_service.db_tasks, "get_task", AsyncMock(return_value={
            "status": "waiting", "work_dir": str(tmp_path)}))
        monkeypatch.setattr(wait_service, "_commands_allowed", AsyncMock(return_value=True))
        monkeypatch.setattr(wait_service, "run_command", check)
        monkeypatch.setattr(wait_service.db_waits, "record_check", recorded)
        monkeypatch.setattr(wait_service.db_waits, "move", moved)
        monkeypatch.setattr(wait_service.db_activity, "insert_activity", AsyncMock())
        service = wait_service.WaitService(bus=None, has_planner=lambda _: False)
        monkeypatch.setattr(service, "_notify_timeout", AsyncMock())
        # Hold every slot until the batch has been selected.
        service._semaphore = asyncio.Semaphore(0)
        running = asyncio.create_task(service.run_once())
        await asyncio.wait_for(selected.wait(), 2)
        if queued:
            # A previous check rescheduled this row while it was queued; it is due again at execution.
            row["next_check_at"] = opened + timedelta(seconds=60)
            current = opened + timedelta(seconds=650)
        service._semaphore.release()
        assert await asyncio.wait_for(running, 2) == int(moved_to is not None)
        assert recorded.await_args.kwargs["at"] == current
        assert recorded.await_args.kwargs["next_check_at"] == current + timedelta(seconds=300)
        if moved_to is None:
            moved.assert_not_awaited()
        else:
            assert moved.await_args.kwargs["to"] == moved_to

    asyncio.run(test())


def test_ready_command_keeps_output_out_of_the_resolution(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from looper.agents.waits import WaitSpec

    async def test() -> None:
        spec = WaitSpec("deployment ready", 3600, check=CommandCheck(f"printf {OUTPUT_MARKER}"), every_seconds=600)
        now = datetime.now(timezone.utc)
        row = {"step_id": "step-w1", "task_id": "task-a", "step_key": "w1", "topic": spec.topic,
               "spec": spec.row(), "next_check_at": now, "due_at": now, "work_dir": str(tmp_path),
               "last_result": None}
        recorded = AsyncMock(return_value=True)
        moved = AsyncMock(return_value={"state": "ready"})
        monkeypatch.setattr(wait_service, "_commands_allowed", AsyncMock(return_value=True))
        monkeypatch.setattr(wait_service.db_waits, "record_check", recorded)
        monkeypatch.setattr(wait_service.db_waits, "move", moved)
        service = wait_service.WaitService(bus=None, has_planner=lambda _: False)
        assert await service._run_wait(row, lambda: now) == 1
        assert recorded.await_args.kwargs["output"] == OUTPUT_MARKER
        assert moved.await_args.kwargs["resolution"] == "Ready: the command exited 0."
        assert OUTPUT_MARKER not in moved.await_args.kwargs["resolution"]

    asyncio.run(test())
