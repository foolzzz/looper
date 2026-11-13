"""The operator's Cursor login, checked only through stand-in status commands."""

import asyncio
import json
import os
from pathlib import Path
import sys
import threading
import time

import pytest

from looper import child_env
from looper.agents import profile_registry, self_check
from looper.agents.runtimes import ApiProtocol, AuthKind, VendorType, cursor_login
from looper.db import profile_registry as db_registry


@pytest.fixture(autouse=True)
def reset_probe():
    cursor_login.forget_probe()
    yield
    cursor_login.forget_probe()


def status_command(tmp_path, source):
    executable = tmp_path / "cursor-agent"
    executable.write_text(f"#!{sys.executable}\n{source}\n")
    executable.chmod(0o700)
    return executable


@pytest.mark.parametrize(("returncode", "payload", "state", "detail"), [
    (0, {"status": "authenticated", "isAuthenticated": True}, "logged_in", "signed in"),
    (0, {"status": "authenticated", "isAuthenticated": True, "userInfo": {"teamId": True}},
     "logged_in", "signed in (team account)"),
    (0, {"status": "authenticated", "isAuthenticated": True, "userInfo": {"teamId": None}},
     "logged_in", "signed in"),
    (0, {"status": "authenticated", "isAuthenticated": True, "userInfo": []}, "logged_in", "signed in"),
    (0, {"status": "unauthenticated"}, "not_logged_in", "not signed in"),
    (0, {"status": "partially-authenticated"}, "not_logged_in", "partly signed in (no refresh token)"),
    (0, {"status": "authenticated", "isAuthenticated": 1}, "unknown", "`cursor-agent status` exited 0"),
    (0, {"status": "authenticated"}, "unknown", "`cursor-agent status` exited 0"),
    (0, {"status": "other"}, "unknown", "`cursor-agent status` exited 0"),
    (0, {"status": "error", "message": "unavailable"}, "unknown", "`cursor-agent status` exited 0: unavailable"),
    (3, {"status": "authenticated", "isAuthenticated": True}, "unknown", "`cursor-agent status` exited 3"),
    (2, {"message": "x" * 220}, "unknown", "`cursor-agent status` exited 2: " + "x" * 200),
    (0, {"message": 42}, "unknown", "`cursor-agent status` exited 0"),
    (0, [], "unknown", "`cursor-agent status` exited 0"),
    (0, "invalid JSON", "unknown", "`cursor-agent status` exited 0"),
    (None, None, "unknown", "`cursor-agent status` did not answer within 20 s"),
    (None, "missing", "unknown", "`cursor-agent` is not on the server's PATH"),
], ids=["signed-in", "team", "no-team", "non-object-user", "signed-out", "partial", "non-boolean-auth",
        "missing-auth", "unknown-status", "error", "nonzero", "bounded-message", "non-string-message",
        "non-object", "invalid-json", "timeout", "missing-cli"])
def test_cursor_status_check(returncode, payload, state, detail, tmp_path, monkeypatch):
    if returncode is None:
        source = "import time; time.sleep(2)" if payload is None else ""
    else:
        stdout = payload if isinstance(payload, str) else json.dumps(payload)
        source = f"print({stdout!r})\nraise SystemExit({returncode})"
    executable = status_command(tmp_path, source)
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ.get("PATH", ""))
    if payload == "missing":
        executable.unlink()
    if returncode is None and payload is None:
        monkeypatch.setattr(cursor_login, "_STATUS_TIMEOUT_SEC", 0.5, raising=False)
    result = cursor_login.probe_login(str(executable))
    assert (result.state.value, result.detail) == (state, detail)
    if returncode is not None:
        assert cursor_login.parse_status(returncode, stdout) == result


def test_cursor_status_environment_keeps_operator_home_and_discards_directories(tmp_path, monkeypatch):
    home = tmp_path / "operator"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    recorded = tmp_path / "status.json"
    executable = status_command(tmp_path, f"""
import json, os, pathlib, sys
names = ('HOME', 'CURSOR_CONFIG_DIR', 'CURSOR_DATA_DIR', 'NO_OPEN_BROWSER', 'XDG_CONFIG_HOME',
         'CURSOR_API_KEY', 'CURSOR_AUTH_TOKEN', 'CURSOR_API_ENDPOINT', 'AGENT_CLI_CREDENTIAL_STORE')
record = {{'argv': sys.argv[1:], 'env': {{name: os.environ[name] for name in names if name in os.environ}},
          'directories': [pathlib.Path(os.environ[name]).is_dir() for name in ('CURSOR_CONFIG_DIR', 'CURSOR_DATA_DIR')],
          'stdin': sys.stdin.read()}}
pathlib.Path({str(recorded)!r}).write_text(json.dumps(record))
print('{{"status":"authenticated","isAuthenticated":true}}')
""")
    inherited = {"HOME": str(home), "XDG_CONFIG_HOME": str(home / "xdg"), "SAFE_VALUE": "retained",
                 **{name: "poison" for name in ("CURSOR_API_KEY", "CURSOR_AUTH_TOKEN", "CURSOR_API_ENDPOINT",
                                               "AGENT_CLI_CREDENTIAL_STORE", "CURSOR_CONFIG_DIR", "CURSOR_DATA_DIR")}}
    monkeypatch.setattr(child_env, "inherited", lambda: inherited)
    assert cursor_login.probe_login(str(executable)).state.value == "logged_in"
    record = json.loads(recorded.read_text())
    assert record["argv"] == ["status", "--format", "json"]
    assert record["stdin"] == "" and record["directories"] == [True, True]
    env = record["env"]
    assert env["HOME"] == os.environ["HOME"] and env["XDG_CONFIG_HOME"] == str(home / "xdg")
    assert env["NO_OPEN_BROWSER"] == "1"
    assert not set(cursor_login.ENV_UNSET) & env.keys()
    directories = [Path(env[name]) for name in cursor_login.DIRECTORY_VARS]
    assert all(not directory.is_relative_to(home) and not directory.exists() for directory in directories)
    assert list(home.iterdir()) == []
    assert cursor_login.environment(inherited) == {
        "HOME": str(home), "XDG_CONFIG_HOME": str(home / "xdg"), "SAFE_VALUE": "retained", "NO_OPEN_BROWSER": "1"}



@pytest.mark.parametrize(("state", "detail", "problem"), [
    ("logged_in", "signed in", None),
    ("not_logged_in", "not signed in", "Cursor is not signed in on this machine: run `cursor-agent login`"),
    ("unknown", "status failed", "the Cursor login could not be checked (status failed): run `cursor-agent status` on this machine"),
])
def test_cursor_launch_gate_and_view(state, detail, problem, monkeypatch):
    status = cursor_login.LoginStatus(cursor_login.LoginState(state), detail)
    calls = []

    def probe():
        calls.append(True)
        return status

    monkeypatch.setattr(cursor_login, "probe_login", probe)
    assert asyncio.run(cursor_login.launch_problem()) == problem
    assert asyncio.run(cursor_login.login_view()) == {"state": state, "detail": detail}
    assert len(calls) == 1


@pytest.mark.parametrize("cached", [False, True], ids=["first-check", "stale-check"])
def test_cursor_login_view_returns_while_one_status_check_runs(cached, monkeypatch):
    previous = cursor_login.LoginStatus(cursor_login.LoginState.NOT_LOGGED_IN, "not signed in")
    if cached:
        monkeypatch.setattr(cursor_login, "probe_login", lambda: previous)
        assert cursor_login.cached_probe() == previous
        monkeypatch.setattr(cursor_login, "_PROBE_TTL_SEC", -1)
    complete = threading.Event()
    calls = []

    def probe():
        calls.append(True)
        time.sleep(2)
        complete.set()
        return cursor_login.LoginStatus(cursor_login.LoginState.LOGGED_IN, "signed in")

    monkeypatch.setattr(cursor_login, "probe_login", probe)

    async def check():
        try:
            start = time.monotonic()
            view = await cursor_login.login_view()
            assert time.monotonic() - start < 0.2
            assert view == ({"state": "not_logged_in", "detail": "not signed in", "refreshing": True} if cached else
                            {"state": "checking", "detail": "checking the Cursor login", "refreshing": True})
            for _ in range(3):
                assert await cursor_login.login_view() == view
            assert await asyncio.to_thread(complete.wait, 3)
            monkeypatch.setattr(cursor_login, "_PROBE_TTL_SEC", 30)
            await asyncio.to_thread(cursor_login.cached_probe)
            view = await cursor_login.login_view()
            assert view == {"state": "logged_in", "detail": "signed in"}
            assert len(calls) == 1
        finally:
            if calls:
                await asyncio.to_thread(complete.wait, 3)

    asyncio.run(asyncio.wait_for(check(), timeout=5))


@pytest.mark.parametrize(("profiles", "state", "expected"), [
    (None, "not_logged_in", self_check.SKIP),
    (0, "not_logged_in", self_check.SKIP),
    (1, "not_logged_in", self_check.FAIL),
    (1, "unknown", self_check.WARN),
    (1, "logged_in", self_check.OK),
], ids=["no-database", "no-profile", "signed-out", "unknown", "signed-in"])
def test_cursor_doctor_check(profiles, state, expected, monkeypatch):
    status = cursor_login.LoginStatus(cursor_login.LoginState(state), "synthetic status")
    calls = []

    def probe():
        calls.append(True)
        return status

    async def count(runtime_type):
        assert runtime_type == "cursor-agent"
        if profiles is None:
            raise ConnectionError("unreachable")
        return profiles

    monkeypatch.setattr(db_registry, "login_profile_count", count)
    monkeypatch.setattr(cursor_login, "probe_login", probe)
    monkeypatch.setattr(cursor_login, "cached_status", probe)
    check = asyncio.run(self_check.LOGIN_CHECKS["cursor_login"]())
    assert (check.id, check.label, check.status) == ("cursor_login", "Cursor login", expected)
    assert len(calls) == (1 if profiles else 0)
    if expected == self_check.FAIL:
        assert check.fix == "run `cursor-agent login` on this machine"
    if expected == self_check.WARN:
        assert check.fix == "run `cursor-agent status` on this machine"
    if expected in (self_check.OK, self_check.FAIL, self_check.WARN):
        assert "synthetic status" in check.detail


def test_cursor_vendor_exposes_its_login_module():
    vendor = VendorType.CURSOR_LOGIN
    assert (vendor.auth_kind, vendor.oauth_runtime_type, vendor.login_protocol) == (
        AuthKind.OAUTH, "cursor-agent", ApiProtocol.OPENAI_RESPONSES)
    assert profile_registry._login_modules()["cursor-agent"] is cursor_login
