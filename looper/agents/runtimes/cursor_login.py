"""The operator's existing Cursor login (on macOS, in the login keychain).

Looper never reads the keychain or a credential file. Cursor keeps the
operator's HOME and loads their personal configuration (RFC D8). The status
JSON shape from Cursor CLI 2026.10.22 was verified on 2026-11-04.
"""
from __future__ import annotations

import asyncio
import enum
import json
import shutil
import subprocess
import tempfile
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from ... import child_env

ENV_UNSET = ("CURSOR_API_KEY", "CURSOR_AUTH_TOKEN", "CURSOR_API_ENDPOINT", "AGENT_CLI_CREDENTIAL_STORE")
DIRECTORY_VARS = ("CURSOR_CONFIG_DIR", "CURSOR_DATA_DIR")
FIXED_ENV = (("NO_OPEN_BROWSER", "1"),)
# From cursor-agent --list-models on 2026-10-30; no built-in prices.
SUGGESTED_MODELS = ("auto", "gpt-5.3-codex", "gpt-5.6-sol-high", "composer-2.5", "gemini-3.7-flash-high")
_STATUS_TIMEOUT_SEC = 20.0
_PROBE_TTL_SEC = 30.0


def environment(base: Mapping[str, str]) -> dict[str, str]:
    """Keep the operator's HOME, without authentication overrides or inherited Cursor directories."""
    return {**{key: value for key, value in base.items() if key not in (*ENV_UNSET, *DIRECTORY_VARS)},
            **dict(FIXED_ENV)}


class LoginState(str, enum.Enum):
    LOGGED_IN = "logged_in"
    NOT_LOGGED_IN = "not_logged_in"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class LoginStatus:
    state: LoginState
    detail: str


def parse_status(returncode: int, stdout: str) -> LoginStatus:
    """Read the status contract without retaining account information."""
    try:
        value = json.loads(stdout)
    except (ValueError, TypeError):
        value = None
    if not isinstance(value, dict):
        value = {}
    if returncode == 0:
        if value.get("status") == "authenticated" and value.get("isAuthenticated") is True:
            user = value.get("userInfo")
            team = isinstance(user, dict) and user.get("teamId") is not None
            return LoginStatus(LoginState.LOGGED_IN, "signed in" + (" (team account)" if team else ""))
        if value.get("status") == "unauthenticated":
            return LoginStatus(LoginState.NOT_LOGGED_IN, "not signed in")
        if value.get("status") == "partially-authenticated":
            return LoginStatus(LoginState.NOT_LOGGED_IN, "partly signed in (no refresh token)")
    message = value.get("message")
    detail = f"`cursor-agent status` exited {returncode}"
    if isinstance(message, str):
        detail += ": " + message[:200]
    return LoginStatus(LoginState.UNKNOWN, detail)


def probe_login(executable: str = "cursor-agent") -> LoginStatus:
    """Ask Cursor with the operator's HOME and temporary configuration and data directories."""
    directory = Path(tempfile.mkdtemp(prefix="looper-cursor-status-"))
    try:
        for name in ("config", "data"):
            (directory / name).mkdir()
        env = {**environment(child_env.inherited()), "CURSOR_CONFIG_DIR": str(directory / "config"),
               "CURSOR_DATA_DIR": str(directory / "data")}
        done = subprocess.run([executable, "status", "--format", "json"], capture_output=True, text=True,
                              stdin=subprocess.DEVNULL, timeout=_STATUS_TIMEOUT_SEC, check=False, env=env)
        return parse_status(done.returncode, done.stdout)
    except FileNotFoundError:
        return LoginStatus(LoginState.UNKNOWN, "`cursor-agent` is not on the server's PATH")
    except subprocess.TimeoutExpired:
        return LoginStatus(LoginState.UNKNOWN, "`cursor-agent status` did not answer within 20 s")
    except (OSError, UnicodeError) as exc:
        return LoginStatus(LoginState.UNKNOWN, f"`cursor-agent status` did not run: {type(exc).__name__}")
    finally:
        shutil.rmtree(directory, ignore_errors=True)


_probed: tuple[float, LoginStatus] | None = None
_probe_lock = threading.Lock()


def _refresh_probe() -> LoginStatus:
    global _probed
    if _probed is None or time.monotonic() - _probed[0] > _PROBE_TTL_SEC:
        status = probe_login()
        _probed = (time.monotonic(), status)
    return _probed[1]


def cached_probe() -> LoginStatus:
    """Wait for a current answer, sharing at most one probe per cache interval."""
    with _probe_lock:
        return _refresh_probe()


def forget_probe() -> None:
    global _probed
    with _probe_lock:
        _probed = None


def _background_probe() -> None:
    try:
        _refresh_probe()
    finally:
        _probe_lock.release()


async def launch_problem() -> str | None:
    status = await asyncio.to_thread(cached_probe)
    if status.state is LoginState.LOGGED_IN:
        return None
    if status.state is LoginState.NOT_LOGGED_IN:
        return "Cursor is not signed in on this machine: run `cursor-agent login`"
    return (f"the Cursor login could not be checked ({status.detail}): "
            "run `cursor-agent status` on this machine")


def cached_status() -> LoginStatus | None:
    """Return the cached login and schedule one refresh without waiting for the probe lock."""
    probed = _probed
    stale = probed is None or time.monotonic() - probed[0] > _PROBE_TTL_SEC
    if stale and _probe_lock.acquire(blocking=False):
        try:
            threading.Thread(target=_background_probe, name="cursor-login-status", daemon=True).start()
        except BaseException:
            _probe_lock.release()
            raise
    return probed[1] if probed is not None else None


async def login_view() -> dict[str, str | bool]:
    """Return the last answer immediately and refresh it in one background probe when needed."""
    probed = _probed
    refreshing = probed is None or time.monotonic() - probed[0] > _PROBE_TTL_SEC or _probe_lock.locked()
    status = cached_status()
    view: dict[str, str | bool] = ({"state": "checking", "detail": "checking the Cursor login"} if status is None
                                  else {"state": status.state.value, "detail": status.detail})
    if refreshing:
        view["refreshing"] = True
    return view
