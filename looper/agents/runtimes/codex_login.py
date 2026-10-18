"""Codex's own login (ChatGPT OAuth) in Looper's isolated Codex homes (multi-runtime RFC P1).

Verified against the Codex CLI 0.162.0 source (tag ``rust-v0.162.0``):

- **Where the login is**: with ``cli_auth_credentials_store = "file"`` the
  login is ``$CODEX_HOME/auth.json``; its path cannot be set on its own. The
  operator's file is ``auth.json`` in the server's ``CODEX_HOME``, else in
  ``~/.codex`` (``login_file``).
- **Sharing it**: every home a login run uses holds an ``auth.json`` that is a
  symbolic link to the operator's file, by its absolute path, never a copy and
  never a hard link (``link_login``). Codex saves a refreshed login by
  truncating the file and writing it in place, so it writes through the link
  into the operator's file. A lock cannot help, since the refresh happens
  inside Codex; Codex re-reads the file before it refreshes.
- **A fork**: should a save ever replace the link with a file of its own, the
  file is a copy of the tokens and the operator's file holds a refresh token
  that may no longer work. After every login run, and when ``looper serve``
  starts, the homes are inspected (``inspect_homes``): a copy, or any other
  entry in place of the link, is removed (unlinked, never followed) and the
  link put back, and the problem is recorded (``FORK_KEY``), which makes the
  login unavailable to runs until the operator has checked
  ``codex login status`` on the machine and cleared it (``clear_problem``).
  The link, its inspection and the recorded problem are ``linked_login``'s,
  which Grok's login shares.
- **The agent's shell** is given a ``CODEX_HOME`` without the login
  (``SHELL_HOME``, set by ``shell_environment_policy``), so a ``codex``
  command the agent runs, ``codex logout`` included, does not reach it.
- **The login state** (``probe_login``) is ``codex login status`` in a probe
  home that holds only the link: it reads the local file and does not ask the
  server, so a revoked login still reads as logged in.
"""

from __future__ import annotations

import enum
import os
import shutil
import subprocess
import tempfile
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import linked_login

LOGIN_FILE = "auth.json"
# What would make Codex use an API key or a token of its own instead of the login.
ENV_UNSET = ("CODEX_API_KEY", "CODEX_ACCESS_TOKEN")
# The CODEX_HOME the agent's shell commands get: a directory of Looper's with no login in it.
SHELL_HOME = "shell-home"
# The line a login home's config.toml has, and an API key home's has not (``codex.config_toml``).
CONFIG_MARKER = 'cli_auth_credentials_store = "file"'
# The system_settings key of the recorded problem: {"detail", "at"}.
FORK_KEY = "codex_login_fork"
# The models a ChatGPT login offers in Codex 0.162.0, its default first, as the Agent Profile form suggests them.
# Each has a built-in price (``pricing.BUILTIN_PRICES``), so its runs show an equivalent cost.
SUGGESTED_MODELS = ("gpt-6.1-sol", "gpt-6-sol", "gpt-6-astra", "gpt-6-luna", "gpt-5.6-sol", "gpt-5.6-terra",
                    "gpt-5.6-luna")
PROBLEM_FIX = ("run `codex login status` on this machine (and `codex login` if it is not logged in), then clear "
               "the warning in Settings → LLM Providers → codex-login")
_STATUS_TIMEOUT_SEC = 15.0


def login_file(environ: Mapping[str, str] | None = None) -> Path:
    """The operator's login file, by its absolute path: ``auth.json`` in the server's ``CODEX_HOME``, else in
    ``~/.codex``."""
    environ = os.environ if environ is None else environ
    home = (environ.get("CODEX_HOME") or "").strip()
    base = Path(os.path.abspath(os.path.expanduser(home))) if home else Path.home() / ".codex"
    return base / LOGIN_FILE


# --- The link in a home (``linked_login``) ---

LinkState = linked_login.LinkState
LoginLinkError = linked_login.LoginLinkError
LinkProblem = linked_login.LinkProblem
LOGIN = "Codex login"
# What removes a home's link, as a problem says it.
REMOVED = "as `codex logout` there would, which also revokes the login"


def link_state(home: Path, target: Path) -> LinkState:
    """What ``home``'s ``auth.json`` is, read with ``lstat`` (a link is never followed)."""
    return linked_login.link_state(home, LOGIN_FILE, target)


def link_login(home: Path, target: Path) -> None:
    """Give ``home`` an ``auth.json`` that is a symbolic link to ``target``; raises ``LoginLinkError`` when
    something else is there, which the next inspection removes (``inspect_homes``)."""
    linked_login.link(home, LOGIN_FILE, target, login=f"the {LOGIN}", fix=PROBLEM_FIX)


def unlink_login(home: Path) -> None:
    """Remove ``home``'s login link, if it has one (a session home whose agent moved to an API key provider)."""
    linked_login.unlink(home, LOGIN_FILE)


def _is_login_home(home: Path) -> bool:
    try:
        return CONFIG_MARKER in (home / "config.toml").read_text(encoding="utf-8").splitlines()
    except OSError:
        return False


def inspect_homes(codex_dir: Path, target: Path) -> list[LinkProblem]:
    """Every home under ``codex_dir`` (``runs/<sha256>``, ``sessions/<agent>``) whose ``auth.json`` is not the
    link to ``target``: a copy or another entry in its place, or a login home whose link is gone. Each is put
    right (the entry removed, the link put back), so it is reported once. An API key home without a link is
    fine."""
    return linked_login.inspect_homes((Path(codex_dir) / "runs", Path(codex_dir) / "sessions"), {LOGIN_FILE: target},
                                      is_login_home=_is_login_home, login=LOGIN, removed=REMOVED)


# --- The recorded problem ---


async def recorded_problem(*, pool: Any = None) -> dict[str, str] | None:
    """The recorded problem, ``{"detail", "at"}``; None when there is none."""
    return await linked_login.recorded_problem(FORK_KEY, pool=pool)


async def record_problems(problems: list[LinkProblem]) -> str:
    """Record ``problems`` (the login is unavailable to runs from now on) and return what to tell the operator."""
    detail = await linked_login.record_problems(FORK_KEY, problems)
    forget_probe()
    return f"Codex login unavailable: {detail}. To fix: {PROBLEM_FIX}"


async def clear_problem() -> None:
    await linked_login.clear_problem(FORK_KEY)
    forget_probe()


async def launch_problem() -> str | None:
    """Why a run cannot start with the login now: a recorded problem, or no login file; None when it can."""
    try:
        problem = await recorded_problem()
    except Exception as exc:  # the database decides; without it, no run
        return f"the state of the Codex login cannot be read ({type(exc).__name__})"
    if problem is not None:
        return f"the Codex login is unavailable since {problem.get('at') or '?'}: {problem.get('detail')}. " \
               f"To fix: {PROBLEM_FIX}"
    target = login_file()
    if not target.is_file():
        return f"Codex is not logged in on this machine (no {target}): run `codex login`, or use an API key provider"
    return None


async def inspect_at_start(runtime_root: Path) -> str | None:
    """Inspect every home under ``runtime_root`` when the server starts (a run the server did not see end);
    what was recorded, or None."""
    codex_dir = Path(runtime_root).absolute() / ".local" / "codex"
    problems = inspect_homes(codex_dir, login_file()) if codex_dir.is_dir() else []
    return await record_problems(problems) if problems else None


# --- The login state ---


class LoginState(str, enum.Enum):
    CHATGPT = "chatgpt"  # logged in using ChatGPT: what the login provider runs with
    OTHER_METHOD = "other_method"  # logged in, but with an API key or another method
    NOT_LOGGED_IN = "not_logged_in"
    UNKNOWN = "unknown"  # Codex did not say (not found, timed out, another answer)


@dataclass(frozen=True)
class LoginStatus:
    state: LoginState
    detail: str


def parse_status(returncode: int, output: str) -> LoginStatus:
    """What ``codex login status`` said (it writes to stderr): exit 0 with ``Logged in using ChatGPT``, exit 1
    with ``Not logged in``; an API key login is exit 0 too, told apart by its line (its key is never kept)."""
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    if returncode == 0 and "Logged in using ChatGPT" in lines:
        return LoginStatus(LoginState.CHATGPT, "logged in using ChatGPT")
    if returncode == 0 and (method := next((line for line in lines if line.startswith("Logged in using ")), None)):
        method = method.removeprefix("Logged in using ").partition(" - ")[0]
        return LoginStatus(LoginState.OTHER_METHOD, f"logged in using {method}, not ChatGPT")
    if returncode == 1 and "Not logged in" in lines:
        return LoginStatus(LoginState.NOT_LOGGED_IN, "not logged in")
    said = next((line for line in lines if "Logged in using" not in line), "")[:200]
    return LoginStatus(LoginState.UNKNOWN, f"`codex login status` exited {returncode}" + (f": {said}" if said else ""))


def probe_login(executable: str = "codex", target: Path | None = None) -> LoginStatus:
    """``codex login status`` in a probe home that holds only the link to the login (removed afterwards, the
    link unlinked), with ``ENV_UNSET`` removed from its environment."""
    home = Path(tempfile.mkdtemp(prefix="looper-codex-login-"))
    try:
        os.symlink(target or login_file(), home / LOGIN_FILE)
        env = {k: v for k, v in os.environ.items() if k not in ENV_UNSET}
        done = subprocess.run([executable, "-c", 'cli_auth_credentials_store="file"', "login", "status"],
                              capture_output=True, text=True, stdin=subprocess.DEVNULL,
                              timeout=_STATUS_TIMEOUT_SEC, check=False, env={**env, "CODEX_HOME": str(home)})
    except FileNotFoundError:
        return LoginStatus(LoginState.UNKNOWN, f"`{executable}` is not on the server's PATH")
    except (OSError, subprocess.TimeoutExpired) as exc:
        return LoginStatus(LoginState.UNKNOWN, f"`codex login status` did not run: {type(exc).__name__}")
    finally:
        shutil.rmtree(home, ignore_errors=True)
    return parse_status(done.returncode, f"{done.stderr}\n{done.stdout}")


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
    else what ``codex login status`` says."""
    import asyncio

    try:
        problem = await recorded_problem()
    except Exception as exc:
        return {"state": LoginState.UNKNOWN.value, "detail": f"cannot be read ({type(exc).__name__})"}
    if problem is not None:
        return {"state": "unavailable", "detail": f"since {problem.get('at') or '?'}: {problem.get('detail')}"}
    status = await asyncio.to_thread(cached_probe)
    return {"state": status.state.value, "detail": status.detail}
