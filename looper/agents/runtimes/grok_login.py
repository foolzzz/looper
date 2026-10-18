"""Grok's own login (xAI OAuth) in Looper's isolated Grok homes (multi-runtime RFC P1, Grok runtime RFC section 8).

- **Where the login is**: ``$GROK_HOME/auth.json``. The operator's file is
  ``auth.json`` in the server's ``GROK_HOME``, else in ``~/.grok``
  (``login_file``).
- **Sharing it**: every home a login run uses holds an ``auth.json`` that is a
  symbolic link to the operator's file, by its absolute path, never a copy
  (``link_login``); Grok refreshes the login in its own process and picks up a
  file changed by another one. The link, its inspection after every run and at
  server start, and the problem recorded when a run left a copy in its place
  are ``linked_login``'s, as for Codex's login.
- **The agent's shell** is given a ``GROK_HOME`` without the login
  (``SHELL_HOME``, set by ``shell_environment_policy``), so a ``grok`` command
  the agent runs, ``grok logout`` included, does not reach it.
- **The login state** (``probe_login``): Grok has no command that reports it,
  so it is whether the login file is there, read with ``lstat`` (never its
  contents).
"""

from __future__ import annotations

import enum
import os
import stat
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import linked_login

LOGIN_FILE = "auth.json"
# What would make Grok bill an API key instead of the login: it falls back to a key when the login is gone.
ENV_UNSET = ("XAI_API_KEY", "GROK_CODE_XAI_API_KEY", "GROK_AUTH")
# The GROK_HOME the agent's shell commands get: a directory of Looper's with no login in it.
SHELL_HOME = "shell-home"
# The line a login home's config.toml has, and no other home's (``grok.config_toml``).
CONFIG_MARKER = "# Grok's own login: auth.json here links to the operator's login file, never a copy."
# The system_settings key of the recorded problem: {"detail", "at"}.
FORK_KEY = "grok_login_fork"
# The models a Grok login offers in Grok 1.0.62, the newest first, as the Agent Profile form suggests them. No
# built-in price: their runs record tokens only.
SUGGESTED_MODELS = ("grok-4.7", "grok-4.7-build-fast", "grok-4.6", "grok-4.5")
LOGIN = "Grok login"
REMOVED = "as `grok logout` there would"
PROBLEM_FIX = ("check on this machine that Grok is still signed in (`grok login` if it is not), then clear the "
               "warning in Settings → LLM Providers → grok-login")


def login_file(environ: Mapping[str, str] | None = None) -> Path:
    """The operator's login file, by its absolute path: ``auth.json`` in the server's ``GROK_HOME``, else in
    ``~/.grok``."""
    environ = os.environ if environ is None else environ
    home = (environ.get("GROK_HOME") or "").strip()
    base = Path(os.path.abspath(os.path.expanduser(home))) if home else Path.home() / ".grok"
    return base / LOGIN_FILE


# --- The link in a home (``linked_login``) ---


def link_login(home: Path, target: Path) -> None:
    """Give ``home`` an ``auth.json`` that is a symbolic link to ``target``; raises ``LoginLinkError`` when
    something else is there, which the next inspection removes (``inspect_homes``)."""
    linked_login.link(home, LOGIN_FILE, target, login=f"the {LOGIN}", fix=PROBLEM_FIX)


def unlink_login(home: Path) -> None:
    linked_login.unlink(home, LOGIN_FILE)


def _is_login_home(home: Path) -> bool:
    try:
        return CONFIG_MARKER in (home / "config.toml").read_text(encoding="utf-8").splitlines()
    except OSError:
        return False


def inspect_homes(grok_dir: Path, target: Path) -> list[linked_login.LinkProblem]:
    """Every home under ``grok_dir`` (``runs/<sha256>``, ``sessions/<owner>``) whose ``auth.json`` is not the link
    to ``target``, each put right (``linked_login.inspect_homes``)."""
    return linked_login.inspect_homes((Path(grok_dir) / "runs", Path(grok_dir) / "sessions"), {LOGIN_FILE: target},
                                      is_login_home=_is_login_home, login=LOGIN, removed=REMOVED)


# --- The recorded problem ---


async def recorded_problem(*, pool: Any = None) -> dict[str, str] | None:
    return await linked_login.recorded_problem(FORK_KEY, pool=pool)


async def record_problems(problems: list[linked_login.LinkProblem]) -> str:
    """Record ``problems`` (the login is unavailable to runs from now on) and return what to tell the operator."""
    detail = await linked_login.record_problems(FORK_KEY, problems)
    forget_probe()
    return f"Grok login unavailable: {detail}. To fix: {PROBLEM_FIX}"


async def clear_problem() -> None:
    await linked_login.clear_problem(FORK_KEY)
    forget_probe()


async def launch_problem() -> str | None:
    """Why a run cannot start with the login now: a recorded problem, or no login file; None when it can."""
    try:
        problem = await recorded_problem()
    except Exception as exc:  # the database decides; without it, no run
        return f"the state of the Grok login cannot be read ({type(exc).__name__})"
    if problem is not None:
        return f"the Grok login is unavailable since {problem.get('at') or '?'}: {problem.get('detail')}. " \
               f"To fix: {PROBLEM_FIX}"
    status = probe_login()
    if status.state is not LoginState.LOGGED_IN:
        return f"Grok is not signed in on this machine ({status.detail}): run `grok login`"
    return None


async def inspect_at_start(runtime_root: Path) -> str | None:
    """Inspect every home under ``runtime_root`` when the server starts (a run the server did not see end);
    what was recorded, or None."""
    grok_dir = Path(runtime_root).absolute() / ".local" / "grok"
    problems = inspect_homes(grok_dir, login_file()) if grok_dir.is_dir() else []
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
