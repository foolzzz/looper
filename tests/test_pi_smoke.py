"""Safety boundaries of the isolated, installed-wheel Pi smoke harness."""
from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import select
import signal
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import unquote, urlsplit, urlunsplit

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "examples/pi/isolated-smoke.py"
spec = importlib.util.spec_from_file_location("pi_smoke", SCRIPT)
smoke = importlib.util.module_from_spec(spec)
spec.loader.exec_module(smoke)


@pytest.mark.parametrize("runtime_args, expected", [([], "cli"), (["--pi-runtime", "cli"], "cli"),
                                                   (["--pi-runtime", "runner"], "runner")])
def test_fake_provider_mode_is_explicit_and_preserves_unverified_summary(monkeypatch, runtime_args, expected):
    monkeypatch.delenv("LOOPER_TEST_DATABASE_URL", raising=False)

    def exercise(args, _root, _output, summary):
        assert args.fake_provider is True
        assert args.pi_runtime == expected
        summary["unverified"].append("disposable database unavailable")

    monkeypatch.setattr(smoke, "smoke", exercise)
    assert smoke.main(["--fake-provider", *runtime_args]) == 2
    assert json.loads((smoke.LAST_OUTPUT / "summary.json").read_text())["passed"] == []


@contextmanager
def _fresh_smoke_database(admin_url: str):
    """An empty local ``looper_smoke_*`` database on the disposable server, dropped afterwards.

    The harness accepts only that name, and it must contain no tables. CI's
    ``LOOPER_TEST_DATABASE_URL`` is the shared ``postgres`` database, so it is
    the admin connection here and is never passed to the smoke run.
    """
    import asyncpg

    parts = urlsplit(admin_url)
    if (parts.scheme not in ("postgres", "postgresql") or parts.hostname not in ("localhost", "127.0.0.1")
            or unquote(parts.path.lstrip("/")) == "loopx"):
        pytest.skip("no local disposable server: installed fake-provider sessions remain unverified")
    name = "looper_smoke_" + uuid.uuid4().hex[:12]
    smoke_url = urlunsplit(parts._replace(path="/" + name, query="", fragment=""))

    async def create() -> None:
        conn = await asyncpg.connect(admin_url)
        try:
            await conn.execute(f'CREATE DATABASE "{name}"')
        finally:
            await conn.close()

    async def drop() -> None:
        conn = await asyncpg.connect(admin_url)
        try:
            await conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        finally:
            await conn.close()

    try:
        asyncio.run(create())
    except (OSError, asyncpg.PostgresError):
        pytest.skip("cannot create a looper_smoke_* database: installed fake-provider sessions remain unverified")
    try:
        yield smoke_url
    finally:
        asyncio.run(drop())


@pytest.fixture(scope="module", params=("cli", "runner"))
def installed_session_evidence(request):
    """One installed service on a fresh looper_smoke_* database, never an operator service."""
    admin = os.environ.get("LOOPER_TEST_DATABASE_URL")
    if not admin:
        pytest.skip("no disposable DB: installed fake-provider sessions remain unverified")
    with _fresh_smoke_database(admin) as smoke_url:
        code = smoke.main(["--fake-provider", "--database-url", smoke_url, "--disposable-db",
                           "--pi-runtime", request.param])
        summary = json.loads((smoke.LAST_OUTPUT / "summary.json").read_text())
        if code == 2:
            pytest.skip("; ".join(summary["unverified"]))
        assert code == 0, summary
        return json.loads((smoke.LAST_OUTPUT / "sessions.json").read_text())


@pytest.mark.installed
@pytest.mark.parametrize("role", smoke.ROLES)
def test_installed_fake_provider_session_closed_loop(installed_session_evidence, role):
    from types import SimpleNamespace
    from test_pi_platform_tools import _tool_messages

    evidence = installed_session_evidence["roles"][role]
    # Reuse the role driver's positive control: a real Pi request carries the
    # platform's answer; an empty provider log cannot satisfy this assertion.
    messages = _tool_messages(SimpleNamespace(requests=evidence["requests"]))
    assert any(json.loads(message["content"])["status"] == "ok" for message in messages)
    smoke.assert_role_evidence(role, evidence, pi_runtime=installed_session_evidence["pi_runtime"])


@pytest.mark.installed
@pytest.mark.parametrize("boundary", ["wrong-token", "parent-context", "cancel-tool"])
def test_installed_fake_provider_negative_controls(installed_session_evidence, boundary):
    assert installed_session_evidence["boundaries"][boundary] is True


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
            deadline = time.monotonic() + 30
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
            process.wait(timeout=30)
        if write_fd != -1:
            os.close(write_fd)
        try:
            assert process.returncode is not None
            assert select.select([read_fd], [], [], 5)[0], "descendant survived smoke cleanup"
            assert os.read(read_fd, 1) == b""
        finally:
            os.close(read_fd)
