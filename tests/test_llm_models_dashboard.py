"""Disposable real-server/browser smoke; model editing belongs to later cases.

Install with ``uv sync --extra test`` and
``uv run --extra test python -m playwright install chromium``.
Set LOOPER_TEST_DATABASE_URL to a disposable PostgreSQL server with CREATEDB.
The URL is used only to create/drop a fresh database, never as the app database.
"""
from __future__ import annotations

import asyncio
import os
import signal
import socket
import subprocess
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from evals.prompt_regression.runner import Api, EvalError, fresh_database, free_port
from looper import ops_config
from looper.agents import operator_auth

REPO_ROOT = Path(__file__).resolve().parents[1]
SEED_NAME = "Isolated browser smoke project"


@pytest.fixture
def database_admin_url():
    url = os.environ.get("LOOPER_TEST_DATABASE_URL")
    if not url:
        pytest.skip("UNTESTED: set LOOPER_TEST_DATABASE_URL to a disposable PostgreSQL server with CREATEDB")
    return url


@dataclass
class DashboardServer:
    api: Api
    database_url: str
    home: Path
    runtime_root: Path
    env: dict[str, str]
    process: subprocess.Popen
    log_path: Path


def _database_state(url):
    async def read():
        import asyncpg

        conn = await asyncpg.connect(url, timeout=5)
        try:
            return {"database": await conn.fetchval("SELECT current_database()"),
                    "channels": [tuple(row) for row in await conn.fetch(
                        "SELECT channel_id, enabled FROM channel_configs ORDER BY channel_id")]}
        finally:
            await conn.close()

    return asyncio.run(asyncio.wait_for(read(), timeout=10))


def _seed(url, work_dir):
    async def seed():
        import asyncpg

        from looper.agents.serve import seed_channel_configs
        from looper.db.connection import ensure_schema
        from looper.db.projects import create_project

        conn = await asyncpg.connect(url, timeout=5)
        try:
            await ensure_schema(conn)
            await seed_channel_configs(conn)
            await create_project(name=SEED_NAME, work_dir=str(work_dir), pool=conn)
        finally:
            await conn.close()

    asyncio.run(asyncio.wait_for(seed(), timeout=30))


def _wait_ready(server):
    deadline = time.monotonic() + 45
    while server.process.poll() is None:
        try:
            server.api.token = operator_auth.read_token(server.runtime_root)
            if server.api.token:
                server.api.get("/api/status")
                return
        except (OSError, EvalError):
            pass
        if time.monotonic() >= deadline:
            break
        time.sleep(0.1)
    raise AssertionError(f"isolated server did not start:\n{server.log_path.read_text()}")


@contextmanager
def _running_dashboard(tmp_path, admin_url):
    home, root, work, bin_dir = (tmp_path / name for name in ("home", "runtime", "work", "bin"))
    for directory in (home, root, work, bin_dir):
        directory.mkdir()
    # An allowlist prevents ambient credentials, channel tokens, ops settings and
    # executable runtimes from entering the server; keep the suite's root guard.
    env = {key: value for key, value in os.environ.items()
           if key in ("LANG", "LC_ALL", "SYSTEMROOT", "TMPDIR") or key.startswith("PYTEST_LOOPER_")}
    env.update(HOME=str(home), PATH=str(bin_dir),
               PYTHONPATH=os.pathsep.join((str(REPO_ROOT / "tests/runtime_root_guard"), str(REPO_ROOT))),
               CLAUDE_CONFIG_DIR=str(home / ".claude"), CODEX_HOME=str(home / ".codex"),
               GROK_HOME=str(home / ".grok"), XDG_CONFIG_HOME=str(home / ".config"))
    with fresh_database(admin_url) as (url, _):
        _seed(url, work)
        port = free_port()
        ops = ops_config.OpsConfig(runtime_root=root, path=root / ops_config.FILE,
                                   database_url=url, port=port, operator_notes_root=str(home / "notes"))
        ops_config.write_private(ops.path, ops_config.render(ops))
        log_path = tmp_path / "serve.log"
        with log_path.open("w") as log:
            process = subprocess.Popen(
                [sys.executable, "-m", "looper.cli", "serve", "--host", "127.0.0.1", "--port", str(port),
                 "--runtime-root", str(root), "--config", str(ops.path)],
                cwd=work, env=env, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                start_new_session=True,
            )
            server = DashboardServer(Api(f"http://127.0.0.1:{port}", timeout=2), url, home, root, env, process, log_path)
            try:
                _wait_ready(server)
                yield server
            finally:
                if process.poll() is None:
                    process.send_signal(signal.SIGINT)
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        os.killpg(process.pid, signal.SIGKILL)
                        process.wait(timeout=5)


@pytest.fixture
def dashboard_server(tmp_path, database_admin_url):
    with _running_dashboard(tmp_path, database_admin_url) as server:
        yield server


@pytest.fixture
def chromium_browser(dashboard_server, tmp_path, monkeypatch):
    # Keep the server outside Playwright's sync event loop: fresh_database uses
    # asyncio.run() both to create the database and to drop it on teardown.
    playwright = pytest.importorskip(
        "playwright.sync_api", reason="UNTESTED: Playwright missing; run `uv sync --extra test`", exc_type=ImportError)
    # Playwright finds its installed browser before HOME is isolated. Its driver
    # and Chromium then run with a throwaway HOME and profile.
    with playwright.sync_playwright() as driver:
        executable = Path(driver.chromium.executable_path)
    if not executable.is_file():
        pytest.skip("UNTESTED: Chromium missing; run `uv run --extra test python -m playwright install chromium`")
    monkeypatch.setenv("HOME", str(tmp_path / "browser-home"))
    (tmp_path / "browser-home").mkdir()
    with playwright.sync_playwright() as driver:
        browser = driver.chromium.launch(headless=True, executable_path=str(executable), timeout=15000)
        try:
            yield browser
        finally:
            browser.close()


@pytest.fixture
def dashboard_page(chromium_browser, dashboard_server):
    context = chromium_browser.new_context(viewport={"width": 1280, "height": 800})
    try:
        page = context.new_page()
        page.set_default_timeout(10000)
        page.set_default_navigation_timeout(15000)
        yield page
    finally:
        context.close()


def test_dashboard_server_is_isolated(dashboard_server, tmp_path):
    server = dashboard_server
    assert urlsplit(server.api.base_url).port != 8767
    assert server.database_url != os.environ["LOOPER_TEST_DATABASE_URL"]
    assert server.home.is_relative_to(tmp_path)
    assert server.runtime_root.is_relative_to(tmp_path)
    assert server.home != server.runtime_root
    for key in ("HOME", "CLAUDE_CONFIG_DIR", "CODEX_HOME", "GROK_HOME", "XDG_CONFIG_HOME"):
        assert Path(server.env[key]).is_relative_to(tmp_path)
    assert not any(key.startswith(("LOOPER_", "LOOPX_")) for key in server.env)
    assert server.env["PATH"] == str(tmp_path / "bin")
    projects = server.api.get("/api/projects")["projects"]
    assert [project["name"] for project in projects] == [SEED_NAME]
    state = _database_state(server.database_url)
    assert state["database"].startswith("looper_eval_")
    assert state["channels"] == [
        ("channel-lark", False), ("channel-lark_bot", False), ("channel-telegram", False),
    ]


def test_browser_opens_own_dashboard_and_reads_seed(dashboard_page, dashboard_server):
    from playwright.sync_api import expect

    # Readiness is the rendered database seed; background dashboard requests
    # must not make the smoke wait for the whole page's network to become idle.
    response = dashboard_page.goto(dashboard_server.api.base_url, wait_until="domcontentloaded")
    assert response is not None and response.status == 200
    assert urlsplit(dashboard_page.url).netloc == urlsplit(dashboard_server.api.base_url).netloc
    expect(dashboard_page.get_by_role("region", name="Projects").get_by_text(SEED_NAME, exact=True)).to_be_visible()


@pytest.mark.parametrize("failure_at", ["startup", "test"])
def test_failed_smoke_cleans_up_server_and_database(tmp_path, database_admin_url, monkeypatch, failure_at):
    started = []
    original = _wait_ready

    def wait_ready(server):
        started.append(server)
        if failure_at == "startup":
            raise RuntimeError("injected smoke failure")
        original(server)

    monkeypatch.setattr(sys.modules[__name__], "_wait_ready", wait_ready)
    with pytest.raises(RuntimeError, match="injected smoke failure"):
        with _running_dashboard(tmp_path, database_admin_url):
            raise RuntimeError("injected smoke failure")
    [server] = started
    assert server.process.poll() is not None
    with socket.socket() as probe:
        assert probe.connect_ex(("127.0.0.1", urlsplit(server.api.base_url).port)) != 0

    async def database_exists():
        import asyncpg

        conn = await asyncpg.connect(database_admin_url, timeout=5)
        try:
            return await conn.fetchval("SELECT EXISTS (SELECT FROM pg_database WHERE datname = $1)",
                                       urlsplit(server.database_url).path.lstrip("/"))
        finally:
            await conn.close()

    assert not asyncio.run(database_exists())
