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
import time
from pathlib import Path

import pytest

from evals.prompt_regression.runner import fresh_database, running_server
from looper import ops_config
from looper.agents import self_check
from looper.agents.self_check import mask_dsn
from tests.db_support import DSN

REPO_ROOT = Path(__file__).resolve().parents[1]


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
    ops = ops_config.OpsConfig(runtime_root=tmp_path, path=tmp_path / ops_config.FILE,
                               database_url=f"postgresql://looper:s3cret-pw@127.0.0.1:{port}/loopx")
    ops_config.write_private(ops.path, ops_config.render(ops))
    env = {**os.environ, "PYTHONPATH": str(REPO_ROOT)}
    serve = ("from pathlib import Path; from looper.agents.serve import serve_agent; "
             "serve_agent(port=1, runtime_root=Path.cwd(), work_dir=Path.cwd())")

    proc = subprocess.run([sys.executable, "-c", serve], cwd=tmp_path, env=env, capture_output=True, text=True,
                          timeout=60)

    assert proc.returncode == 1, proc.stderr
    assert f"fail  PostgreSQL  cannot connect to postgresql://looper:***@127.0.0.1:{port}/loopx" in proc.stderr
    assert "docker run" in proc.stderr and "Traceback" not in proc.stderr
    assert "s3cret-pw" not in proc.stdout + proc.stderr


@pytest.mark.parametrize("configured", [False, True], ids=["root-unset", "root-set"])
def test_serve_logs_projects_root_before_connecting(tmp_path, monkeypatch, caplog, configured):
    import asyncio
    import logging
    from unittest.mock import AsyncMock

    from looper.agents import operator_auth, projects, serve

    root = str(tmp_path / "projects") if configured else ""
    ops = ops_config.OpsConfig(runtime_root=tmp_path, path=tmp_path / ops_config.FILE, projects_root=root)
    configured_roots = []
    monkeypatch.setattr(projects, "configure_root", configured_roots.append)
    monkeypatch.setattr(operator_auth, "ensure_token", lambda *args: "test-token")
    unavailable = self_check.Check("database", "PostgreSQL", self_check.FAIL, "test stand-in")
    monkeypatch.setattr(self_check, "check_database", AsyncMock(return_value=unavailable))
    with caplog.at_level(logging.INFO, logger=serve.__name__), pytest.raises(serve.StartupCheckFailed):
        asyncio.run(serve._run_agent_system_owned(ops=ops, runtime_root=tmp_path, work_dir=tmp_path))
    assert configured_roots == [root]
    assert (f"projects root={root}" if configured else "projects root unset") in caplog.text


@pytest.mark.parametrize("state", ["unset", "missing", "file", "writable", "unwritable"])
def test_projects_root_check_warns_without_creating_directories(tmp_path, monkeypatch, state):
    root = tmp_path / "projects"
    if state in ("writable", "unwritable"):
        root.mkdir()
    elif state == "file":
        root.write_text("not a directory")
    if state == "unwritable":
        monkeypatch.setattr(self_check.os, "access", lambda path, mode: False)
    check = self_check.check_projects_root("" if state == "unset" else str(root))
    expected = {"unset": self_check.SKIP, "writable": self_check.OK}.get(state, self_check.WARN)
    assert check.id == "projects_root" and check.status == expected
    if state != "unset":
        assert str(root) in check.detail
    if expected == self_check.WARN:
        assert "[projects] root" in check.fix
    if state in ("unset", "missing"):
        assert not root.exists()


def test_doctor_and_system_check_include_projects_root_without_database(tmp_path, monkeypatch):
    import asyncio
    from unittest.mock import AsyncMock

    unavailable = self_check.Check("database", "PostgreSQL", self_check.FAIL, "offline")
    skipped = self_check.Check("unrelated", "Unrelated", self_check.SKIP, "test stand-in")
    monkeypatch.setattr(self_check, "check_database", AsyncMock(return_value=unavailable))
    monkeypatch.setattr(self_check, "check_claude", lambda *args: [])
    for name in ("check_codex_login", "check_grok_login", "check_lark_cli", "check_git"):
        monkeypatch.setattr(self_check, name, lambda *args: skipped)
    for name in ("check_ttadk", "check_step_worktrees"):
        monkeypatch.setattr(self_check, name, AsyncMock(return_value=skipped))
    monkeypatch.setattr(self_check, "check_pg_dump", lambda *args: (skipped, None))
    root = str(tmp_path / "missing-projects")
    ops = ops_config.OpsConfig(runtime_root=tmp_path, path=tmp_path / ops_config.FILE, projects_root=root)
    checks = asyncio.run(self_check.run_checks(ops))
    found = next(check for check in checks if check.id == "projects_root")
    assert found.status == self_check.WARN and root in found.detail


@pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")
def test_serve_without_claude_starts_and_reports_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATH", f"/usr/bin{os.pathsep}/bin")  # no claude, no lark-cli
    (tmp_path / "bin").mkdir()
    log = tmp_path / "serve.log"

    with fresh_database(str(DSN)) as (database_url, _), running_server(
        database_url=database_url, runtime_root=tmp_path, bin_dir=tmp_path / "bin", log_path=log,
        notes_root=tmp_path / "operator-notes",
    ) as api:
        deadline = time.monotonic() + 10
        while True:
            checks = {check["id"]: check for check in api.post("/api/v1/system/status", {})["result"]["checks"]}
            if "claude_cli" in checks and all(check["status"] != "pending" for check in checks.values()):
                break
            assert time.monotonic() < deadline, "advisory startup checks did not finish"
            time.sleep(0.05)

    assert [checks[name]["status"] for name in ("database", "claude_cli", "claude_login", "cursor_login", "lark_cli")] == [
        "ok", "fail", "skip", "skip", "skip"]
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
    assert check([{"name": "alarm", "work_dir": str(ready), "workspace_links": [".venv"]},
                  {"name": "inbox", "work_dir": ""}]) == (
        "ok", "alarm: main, 0 B checkout; inbox: no default directory, each task's is checked when it runs")
    status, detail = check([
        {"name": "alarm", "work_dir": str(ready), "workspace_links": [".venv", ".env"]},
        {"name": "web", "work_dir": str(detached)}, {"name": "notes", "work_dir": str(plain)}])
    assert status == "warn"
    assert detail.split("; ") == [
        "alarm: main, 0 B checkout, nothing to link at .env",
        "web: the task directory's HEAD is detached (its steps run one at a time)",
        "notes: the task directory is not in a git repository (its steps share the task directory)"]


@pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")
@pytest.mark.parametrize("partial", [False, True], ids=["empty", "unsupported-partial"])
def test_database_facts_classify_before_reading_required_columns(monkeypatch, partial):
    import asyncio
    import asyncpg
    from tests.db_support import fresh_schema

    async def test():
        async with fresh_schema() as pool:
            if partial:
                await pool.execute("CREATE TABLE tasks (id TEXT)")
            schema = await pool.fetchval("SELECT current_schema()")
            connect = asyncpg.connect

            async def isolated_connect(dsn, **kwargs):
                return await connect(dsn, **kwargs, server_settings={"search_path": schema})

            monkeypatch.setattr(asyncpg, "connect", isolated_connect)
            before = await pool.fetch("SELECT tablename FROM pg_tables WHERE schemaname = $1", schema)
            facts = await self_check._database_facts(str(DSN))
            if partial:
                assert "upgrade to v2.13.165" in facts.schema_error
                assert facts.lark_enabled is None
            else:
                assert facts.schema_error is None and facts.lark_enabled is False
            assert await pool.fetch("SELECT tablename FROM pg_tables WHERE schemaname = $1", schema) == before

    asyncio.run(test())
