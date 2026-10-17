"""``looper serve`` runs the startup self-check: PostgreSQL is required, Claude is reported; and the git checks.

The server is the M6 eval's disposable ``looper serve``; the case that needs a
database runs only when LOOPER_TEST_DATABASE_URL points at a disposable one
(it creates and drops a database beside it). ``looper doctor`` is covered in
test_cli_entry.py; the git checks it shows are checked here against stand-in
gits and real repositories.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from evals.prompt_regression.runner import fresh_database, running_server
from looper.agents import self_check
from looper.agents.self_check import mask_dsn

REPO_ROOT = Path(__file__).resolve().parents[1]
DSN = os.environ.get("LOOPER_TEST_DATABASE_URL")


@pytest.mark.parametrize(
    ("dsn", "shown"),
    [
        ("postgresql://u:pw@h:5432/db", "postgresql://u:***@h:5432/db"),
        ("postgresql://h/db?user=u&password=pw&sslmode=off", "postgresql://h/db?user=u&password=***&sslmode=off"),
        ("postgresql://u@h/db", "postgresql://u@h/db"),
    ],
    ids=["userinfo", "query", "no-password"],
)
def test_the_database_url_is_shown_without_its_password(dsn: str, shown: str) -> None:
    assert mask_dsn(dsn) == shown


def test_serve_without_postgresql_stops_with_the_fix(tmp_path: Path) -> None:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    env = {**os.environ, "LOOPER_DATABASE_URL": f"postgresql://looper:s3cret-pw@127.0.0.1:{port}/loopx",
           "PYTHONPATH": str(REPO_ROOT)}
    serve = ("from pathlib import Path; from looper.agents.serve import serve_agent; "
             "serve_agent(port=1, runtime_root=Path.cwd(), work_dir=Path.cwd())")

    proc = subprocess.run([sys.executable, "-c", serve], cwd=tmp_path, env=env, capture_output=True, text=True,
                          timeout=60)

    assert proc.returncode == 1, proc.stderr
    assert f"fail  PostgreSQL  cannot connect to postgresql://looper:***@127.0.0.1:{port}/loopx" in proc.stderr
    assert "docker run" in proc.stderr and "Traceback" not in proc.stderr
    assert "s3cret-pw" not in proc.stdout + proc.stderr


@pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")
def test_serve_without_claude_starts_and_reports_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATH", f"/usr/bin{os.pathsep}/bin")  # no claude, no lark-cli
    (tmp_path / "bin").mkdir()
    log = tmp_path / "serve.log"

    with fresh_database(str(DSN)) as (database_url, _), running_server(
        database_url=database_url, runtime_root=tmp_path, bin_dir=tmp_path / "bin", log_path=log,
    ) as api:
        checks = {check["id"]: check for check in api.get("/api/status")["checks"]}

    assert [checks[name]["status"] for name in ("database", "claude_cli", "claude_login", "lark_cli")] == [
        "ok", "fail", "skip", "skip"]
    assert "npm install -g @anthropic-ai/claude-code" in checks["claude_cli"]["fix"]
    assert "WARNING SELF-CHECK FAIL Claude Code CLI: `claude` is not on PATH." in log.read_text()


# --- The git checks ---


def _git_stub(directory: Path, version: str) -> Path:
    stub = directory / "git"
    stub.write_text(f"#!/bin/sh\necho 'git version {version}'\n")
    stub.chmod(0o755)
    return directory


@pytest.mark.parametrize(("version", "status", "detail"), [
    ("2.47.1", "ok", "git version 2.47.1"),
    ("2.40.0", "ok", "git version 2.40.0"),
    ("2.39.5 (Apple Git-154)", "warn", "git version 2.39.5 (Apple Git-154) is older than 2.40: projects whose "
                                       "parallel steps work in worktrees run their steps one at a time"),
    (None, "warn", "`git` is not on PATH: projects whose parallel steps work in worktrees run their steps one at a "
                   "time"),
])
def test_the_git_check_says_what_an_old_or_missing_git_costs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, version: str | None, status: str, detail: str,
) -> None:
    monkeypatch.setenv("PATH", str(_git_stub(tmp_path, version) if version else tmp_path))
    check = self_check.check_git()
    assert (check.status, check.detail) == (status, detail)
    assert ("install git 2.40 or later" in check.fix) == (status == "warn")


def _checkout(path: Path) -> Path:
    path.mkdir(parents=True)
    env = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"}
    for args in (("init", "-q", "-b", "main"), ("-c", "user.name=U", "-c", "user.email=u@x", "commit", "-q",
                                                "--allow-empty", "-m", "init")):
        subprocess.run(["git", *args], cwd=path, env=env, check=True)
    return path


def test_the_step_worktrees_check_reads_each_project_s_directory(tmp_path: Path) -> None:
    import asyncio

    ready = _checkout(tmp_path / "alarm")
    (ready / ".venv").mkdir()
    detached = _checkout(tmp_path / "web")
    subprocess.run(["git", "switch", "-q", "--detach"], cwd=detached, check=True)
    plain = tmp_path / "notes"
    plain.mkdir()

    def check(projects: list[dict] | None) -> tuple[str, str]:
        found = asyncio.run(self_check.check_step_worktrees(projects))
        return found.status, found.detail

    assert check(None) == ("skip", "not checked: the projects are stored in PostgreSQL, which is unreachable")
    assert check([]) == ("skip", "no project runs its parallel steps in worktrees")
    assert check([{"name": "alarm", "work_dir": str(ready), "step_workspace_links": [".venv"]},
                  {"name": "inbox", "work_dir": ""}]) == (
        "ok", "alarm: main, 0 B checkout; inbox: no default directory, each task's is checked when it runs")
    status, detail = check([
        {"name": "alarm", "work_dir": str(ready), "step_workspace_links": [".venv", ".env"]},
        {"name": "web", "work_dir": str(detached)}, {"name": "notes", "work_dir": str(plain)}])
    assert status == "warn"
    assert detail.split("; ") == [
        "alarm: main, 0 B checkout, nothing to link at .env",
        "web: the task directory's HEAD is detached (its steps run one at a time)",
        "notes: the task directory is not in a git repository (its steps share the task directory)"]
