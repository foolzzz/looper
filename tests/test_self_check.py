"""``looper serve`` runs the startup self-check: PostgreSQL is required, Claude is reported.

The server is the M6 eval's disposable ``looper serve``; the case that needs a
database runs only when LOOPER_TEST_DATABASE_URL points at a disposable one
(it creates and drops a database beside it). ``looper doctor`` is covered in
test_cli_entry.py.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from evals.prompt_regression.runner import fresh_database, running_server
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
    env = {**os.environ, "LOOPER_DATABASE_URL": f"postgresql://loopx:s3cret-pw@127.0.0.1:{port}/loopx",
           "PYTHONPATH": str(REPO_ROOT)}
    serve = ("from pathlib import Path; from looper.agents.serve import serve_agent; "
             "serve_agent(port=1, runtime_root=Path.cwd(), work_dir=Path.cwd())")

    proc = subprocess.run([sys.executable, "-c", serve], cwd=tmp_path, env=env, capture_output=True, text=True,
                          timeout=60)

    assert proc.returncode == 1, proc.stderr
    assert f"fail  PostgreSQL  cannot connect to postgresql://loopx:***@127.0.0.1:{port}/loopx" in proc.stderr
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
