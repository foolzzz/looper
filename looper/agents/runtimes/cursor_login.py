"""Cursor's file-store login shared with isolated Looper homes through symbolic links.

The operator's auth.json is never read or copied. ``linked_login`` repairs
replaced links and records a problem that blocks runs until it is cleared.
"""
from __future__ import annotations

import enum
import os
import stat
import sys
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import linked_login

LOGIN_FILE = Path(".cursor") / "auth.json"
ENV_UNSET = ("CURSOR_API_KEY", "CURSOR_AUTH_TOKEN", "CURSOR_API_ENDPOINT", "CURSOR_CONFIG_DIR",
             "CURSOR_DATA_DIR", "XDG_CONFIG_HOME")
FIXED_ENV = (("AGENT_CLI_CREDENTIAL_STORE", "file"), ("NO_OPEN_BROWSER", "1"))
FORK_KEY = "cursor_login_fork"
# From cursor-agent --list-models on 2026-10-30; no built-in prices.
SUGGESTED_MODELS = ("auto", "gpt-5.3-codex", "gpt-5.6-sol-high", "composer-2.5", "gemini-3.7-flash-high")
LOGIN = "Cursor login"
REMOVED = "as `cursor-agent logout` there would"
PROBLEM_FIX = ("check on this machine that Cursor is signed in with its file store "
               "(`AGENT_CLI_CREDENTIAL_STORE=file cursor-agent login` if it is not), then clear the "
               "warning in Settings → LLM Providers → cursor-login")


def login_file(environ: Mapping[str, str] | None = None) -> Path:
    """The operator's absolute login path, without resolving through a symbolic link."""
    environ = os.environ if environ is None else environ
    if sys.platform == "darwin":
        return Path.home() / LOGIN_FILE
    config = environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(os.path.abspath(os.path.expanduser(config))) / "cursor" / "auth.json"


# --- The link in a home (``linked_login``) ---


def link_login(home: Path, target: Path) -> None:
    """Give ``home`` an ``auth.json`` that is a symbolic link to ``target``; raises ``LoginLinkError`` when
    something else is there, which the next inspection removes (``inspect_homes``)."""
    (home / LOGIN_FILE).parent.mkdir(parents=True, exist_ok=True)
    linked_login.link(home, str(LOGIN_FILE), target, login=f"the {LOGIN}", fix=PROBLEM_FIX)


def unlink_login(home: Path) -> None:
    linked_login.unlink(home, str(LOGIN_FILE))


def inspect_homes(cursor_dir: Path, target: Path) -> list[linked_login.LinkProblem]:
    """Every home under ``cursor_dir`` (``runs/<sha256>``, ``sessions/<owner>``) whose ``auth.json`` is not the link
    to ``target``, each put right (``linked_login.inspect_homes``)."""
    return linked_login.inspect_homes((Path(cursor_dir) / "runs", Path(cursor_dir) / "sessions"), {str(LOGIN_FILE): target},
                                      is_login_home=lambda home: True, login=LOGIN, removed=REMOVED)


# --- The recorded problem ---


async def recorded_problem(*, pool: Any = None) -> dict[str, str] | None:
    return await linked_login.recorded_problem(FORK_KEY, pool=pool)


async def record_problems(problems: list[linked_login.LinkProblem]) -> str:
    """Record ``problems`` (the login is unavailable to runs from now on) and return what to tell the operator."""
    detail = await linked_login.record_problems(FORK_KEY, problems)
    forget_probe()
    return f"Cursor login unavailable: {detail}. To fix: {PROBLEM_FIX}"


async def clear_problem() -> None:
    await linked_login.clear_problem(FORK_KEY)
    forget_probe()


async def launch_problem() -> str | None:
    """Why a run cannot start with the login now: a recorded problem, or no login file; None when it can."""
    try:
        problem = await recorded_problem()
    except Exception as exc:  # the database decides; without it, no run
        return f"the state of the Cursor login cannot be read ({type(exc).__name__})"
    if problem is not None:
        return f"the Cursor login is unavailable since {problem.get('at') or '?'}: {problem.get('detail')}. " \
               f"To fix: {PROBLEM_FIX}"
    status = probe_login()
    if status.state is not LoginState.LOGGED_IN:
        return (f"Cursor is not signed in with its file store on this machine ({status.detail}): "
                "run `AGENT_CLI_CREDENTIAL_STORE=file cursor-agent login`")
    return None


async def inspect_at_start(runtime_root: Path) -> str | None:
    """Inspect every home under ``runtime_root`` when the server starts (a run the server did not see end);
    what was recorded, or None."""
    cursor_dir = Path(runtime_root).absolute() / ".local" / "cursor-agent"
    problems = inspect_homes(cursor_dir, login_file()) if cursor_dir.is_dir() else []
    return await record_problems(problems) if problems else None


# --- The login state ---


class LoginState(str, enum.Enum):
    LOGGED_IN = "logged_in"  # the login file is there: what the login provider runs with
    NOT_LOGGED_IN = "not_logged_in"


@dataclass(frozen=True)
class LoginStatus:
    state: LoginState
    detail: str


def probe_login(target: Path | None = None) -> LoginStatus:
    """Whether the operator's login file is there, as a regular file or a link to one (``stat``, never read)."""
    target = target or login_file()
    try:
        mode = os.stat(target).st_mode
    except OSError:
        return LoginStatus(LoginState.NOT_LOGGED_IN, f"no {target}")
    if not stat.S_ISREG(mode):
        return LoginStatus(LoginState.NOT_LOGGED_IN, f"{target} is not a file")
    return LoginStatus(LoginState.LOGGED_IN, f"signed in ({target})")


_PROBE_TTL_SEC = 30.0
_probed: tuple[float, LoginStatus] | None = None


def cached_probe() -> LoginStatus:
    """``probe_login``, at most once per ``_PROBE_TTL_SEC`` (the dashboard asks every few seconds)."""
    global _probed
    now = time.monotonic()
    if _probed is None or now - _probed[0] > _PROBE_TTL_SEC:
        _probed = (now, probe_login())
    return _probed[1]


def forget_probe() -> None:
    global _probed
    _probed = None


async def login_view() -> dict[str, str]:
    """The login as the dashboard shows it, ``{"state", "detail"}``: ``unavailable`` while a problem is recorded,
    else whether the login file is there."""
    try:
        problem = await recorded_problem()
    except Exception as exc:
        return {"state": "unknown", "detail": f"cannot be read ({type(exc).__name__})"}
    if problem is not None:
        return {"state": "unavailable", "detail": f"since {problem.get('at') or '?'}: {problem.get('detail')}"}
    status = cached_probe()
    return {"state": status.state.value, "detail": status.detail}
