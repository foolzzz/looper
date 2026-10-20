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
from pathlib import Path
from typing import Any

import pytest

from looper.agents import proc_tree
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
    body = _pr(comments=[{"body": "one"}], reviews=[])
    (result, output, error, extra), _ = _github(tmp_path, body, None, ReadyWhen.HEAD_CHANGED, monkeypatch=monkeypatch)
    assert result is CheckResult.NOT_READY
    assert error == ""
    assert extra["baseline"] == {"head": "1a2b3c4", "comments": 1}
    assert extra["resolution"] == ""
    assert "1a2b3c4" in output


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
