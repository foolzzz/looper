"""The operator's coco login in Looper's isolated homes.

Verified with CLI 0.214.1: the file login is ``$TRAE_HOME/cli/auth.json``,
else ``~/.trae/cli/auth.json``. Each home holds a symbolic link to that
absolute path, never a copy or a hard link; Looper never opens the target.
Whether a token refresh writes through the link or replaces it is unverified.

A replaced or removed link is repaired by ``inspect_homes`` and recorded
under ``FORK_KEY``. The login stays unavailable until the operator checks
``coco login status`` on the server's machine and clears the problem.
The link inspection and recorded problem are shared with Codex and Grok
through ``linked_login``. The agent's shell uses a home without the login
(``SHELL_HOME``), so its coco commands cannot reach the operator's login.

``probe_login`` runs ``coco login status`` in a temporary home holding only
the link. The observed answers are exit 0 with ``Logged in using Trae`` and
exit 1 with ``Not logged in``. This checks local state, not server validity.
Model names come from ``coco models --json`` in the same kind of home.
"""
from __future__ import annotations

import asyncio
import enum
import json
import os
import subprocess
import tempfile
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ... import child_env
from . import linked_login

LOGIN_FILE = "cli/auth.json"
ENV_UNSET = ("TRAE_ACCESS_TOKEN", "TRAECLI_PERSONAL_ACCESS_TOKEN", "TRAECLI_HOME")
SHELL_HOME = "shell-home"
CONFIG_MARKER = 'cli_auth_credentials_store = "file"'
FORK_KEY = "coco_login_fork"
LOGIN = "coco login"
REMOVED = "as `coco logout` there would"
PROBLEM_FIX = ("run `coco login status` on this machine (and `coco login` if it is not logged in), "
               "then clear the problem in Settings")
SUGGESTED_MODELS = ("GPT-5.5", "GPT-6.1-Sol")
RUN_SESSIONS = "run-sessions"
_STATUS_TIMEOUT_SEC = 15.0
_PROBE_TTL_SEC = 30.0
_MODELS_TTL_SEC = 600.0
LinkState = linked_login.LinkState
LoginLinkError = linked_login.LoginLinkError
LinkProblem = linked_login.LinkProblem


def login_file(environ: Mapping[str, str] | None = None) -> Path:
    """The absolute path of the operator's login; its contents are never read."""
    environ = os.environ if environ is None else environ
    home = (environ.get("TRAE_HOME") or "").strip()
    base = Path(os.path.abspath(os.path.expanduser(home))) if home else Path.home() / ".trae"
    return base / LOGIN_FILE


def link_state(home: Path, target: Path) -> LinkState:
    return linked_login.link_state(home, LOGIN_FILE, target)


def link_login(home: Path, target: Path) -> None:
    (home / "cli").mkdir(mode=0o700, parents=True, exist_ok=True)
    linked_login.link(home, LOGIN_FILE, target, login=LOGIN, fix=PROBLEM_FIX)


def unlink_login(home: Path) -> None:
    linked_login.unlink(home, LOGIN_FILE)


def _is_login_home(home: Path) -> bool:
    try:
        return CONFIG_MARKER in (home / "traecli.toml").read_text(encoding="utf-8").splitlines()
    except OSError:
        return False


def inspect_homes(coco_dir: Path, target: Path) -> list[LinkProblem]:
    """Repair and report forked links in run, resident and kept-session homes."""
    run_sessions = Path(coco_dir) / RUN_SESSIONS
    tasks = (sorted(p for p in run_sessions.iterdir() if p.is_dir() and not p.is_symlink())
             if run_sessions.is_dir() else [])
    return linked_login.inspect_homes((Path(coco_dir) / "runs", Path(coco_dir) / "sessions", *tasks),
                                      {LOGIN_FILE: target}, is_login_home=_is_login_home, login=LOGIN,
                                      removed=REMOVED)


async def recorded_problem(*, pool: Any = None) -> dict[str, str] | None:
    return await linked_login.recorded_problem(FORK_KEY, pool=pool)


async def record_problems(problems: list[LinkProblem]) -> str:
    detail = await linked_login.record_problems(FORK_KEY, problems)
    forget_probe()
    return f"coco login unavailable: {detail}. To fix: {PROBLEM_FIX}"


async def clear_problem() -> None:
    await linked_login.clear_problem(FORK_KEY)
    forget_probe()


async def launch_problem(executable: str = "coco", *, wait: bool = True) -> str | None:
    try:
        problem = await recorded_problem()
    except Exception as exc:
        return f"the state of the coco login cannot be read ({type(exc).__name__})"
    if problem is not None:
        return (f"the coco login is unavailable since {problem.get('at') or '?'}: {problem.get('detail')}. "
                f"To fix: {PROBLEM_FIX}")
    status = await asyncio.to_thread(cached_probe, executable) if wait else cached_status(executable)
    if status is None:
        return None
    if status.state is LoginState.LOGGED_IN:
        return None
    if status.state is LoginState.NOT_LOGGED_IN:
        return "coco is not logged in on this machine: run `coco login` there"
    return f"the coco login could not be checked ({status.detail}): {PROBLEM_FIX}"


async def inspect_at_start(runtime_root: Path) -> str | None:
    coco_dir = Path(runtime_root).absolute() / ".local" / "coco"
    problems = inspect_homes(coco_dir, login_file()) if coco_dir.is_dir() else []
    return await record_problems(problems) if problems else None


class LoginState(str, enum.Enum):
    LOGGED_IN = "logged_in"
    NOT_LOGGED_IN = "not_logged_in"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class LoginStatus:
    state: LoginState
    detail: str


def parse_status(returncode: int, output: str) -> LoginStatus:
    if returncode == 0 and "Logged in" in output:
        return LoginStatus(LoginState.LOGGED_IN, "logged in")
    if returncode == 1 and "Not logged in" in output:
        return LoginStatus(LoginState.NOT_LOGGED_IN, "not logged in")
    said = next((line.strip() for line in output.splitlines() if line.strip()), "")[:200]
    return LoginStatus(LoginState.UNKNOWN, f"`coco login status` exited {returncode}" + (f": {said}" if said else ""))


def _probe_env(home: Path) -> dict[str, str]:
    return {**{k: v for k, v in child_env.inherited().items() if k not in ENV_UNSET}, "TRAE_HOME": str(home)}


def probe_login(executable: str = "coco", target: Path | None = None) -> LoginStatus:
    """Ask the CLI in a disposable home containing only a link to the login."""
    try:
        with tempfile.TemporaryDirectory(prefix="looper-coco-probe-") as directory:
            home = Path(directory)
            link_login(home, target or login_file())
            done = subprocess.run([executable, "login", "status"], capture_output=True, text=True,
                                  stdin=subprocess.DEVNULL, timeout=_STATUS_TIMEOUT_SEC, check=False,
                                  env=_probe_env(home))
        return parse_status(done.returncode, f"{done.stderr}\n{done.stdout}")
    except FileNotFoundError:
        return LoginStatus(LoginState.UNKNOWN, "coco was not found")
    except (OSError, UnicodeError, subprocess.TimeoutExpired) as exc:
        return LoginStatus(LoginState.UNKNOWN, f"`coco login status` did not run: {type(exc).__name__}")


_probed: dict[str, tuple[float, LoginStatus]] = {}
_probe_locks: dict[str, threading.Lock] = {}


def _refresh_probe(executable: str) -> LoginStatus:
    now = time.monotonic()
    probed = _probed
    cached = probed.get(executable)
    if cached is None or now - cached[0] > _PROBE_TTL_SEC:
        status = probe_login(executable)
        cached = (time.monotonic(), status)
        probed[executable] = cached
    return cached[1]


def cached_probe(executable: str = "coco") -> LoginStatus:
    """Launches share one current probe per executable, including a refresh begun by a view."""
    with _probe_locks.setdefault(executable, threading.Lock()):
        return _refresh_probe(executable)


def _background_probe(executable: str, lock: threading.Lock) -> None:
    try:
        _refresh_probe(executable)
    finally:
        lock.release()


def cached_status(executable: str = "coco") -> LoginStatus | None:
    """Read cached state immediately and refresh it without occupying an executor thread."""
    cached = _probed.get(executable)
    lock = _probe_locks.setdefault(executable, threading.Lock())
    if (cached is None or time.monotonic() - cached[0] > _PROBE_TTL_SEC) and lock.acquire(blocking=False):
        try:
            threading.Thread(target=_background_probe, args=(executable, lock),
                             name="coco-login-status", daemon=True).start()
        except BaseException:
            lock.release()
            raise
    return cached[1] if cached else None


def forget_probe() -> None:
    global _probed
    # In-flight refreshes retain their old mapping and cannot repopulate this cache.
    # Invalidating login state from an async operation must never wait for a CLI.
    _probed = {}


async def login_view() -> dict[str, str | bool]:
    try:
        problem = await recorded_problem()
    except Exception as exc:
        return {"state": LoginState.UNKNOWN.value, "detail": f"cannot be read ({type(exc).__name__})"}
    if problem is not None:
        return {"state": "unavailable", "detail": f"since {problem.get('at') or '?'}: {problem.get('detail')}"}
    cached = _probed.get("coco")
    refreshing = cached is None or time.monotonic() - cached[0] > _PROBE_TTL_SEC
    status = cached_status()
    view: dict[str, str | bool] = ({"state": "checking", "detail": "checking the coco login"} if status is None
                                  else {"state": status.state.value, "detail": status.detail})
    if refreshing:
        view["refreshing"] = True
    return view


_models: dict[str, tuple[float, dict[str, str] | None]] = {}


def cached_models(executable: str) -> dict[str, str] | None:
    """Return a fresh cached catalog only; never start the CLI."""
    cached = _models.get(executable)
    if cached is not None and time.monotonic() - cached[0] <= _MODELS_TTL_SEC:
        return cached[1]
    return None


def available_models(executable: str = "coco") -> dict[str, str] | None:
    """Case-insensitive display/config names from the machine's catalog, cached per executable."""
    now = time.monotonic()
    cached = _models.get(executable)
    if cached is not None and now - cached[0] <= _MODELS_TTL_SEC:
        return cached[1]
    models = None
    try:
        with tempfile.TemporaryDirectory(prefix="looper-coco-probe-") as directory:
            home = Path(directory)
            link_login(home, login_file())
            done = subprocess.run([executable, "models", "--json"], capture_output=True, text=True,
                                  stdin=subprocess.DEVNULL, timeout=30, check=False, env=_probe_env(home))
        if done.returncode == 0:
            entries = json.loads(done.stdout)
            if isinstance(entries, list):
                models = {}
                for entry in entries:
                    if not isinstance(entry, dict) or not isinstance(entry.get("name"), str) or not entry["name"]:
                        models = None
                        break
                    name = entry["name"]
                    models[name.casefold()] = name
                    if isinstance(entry.get("config_name"), str) and entry["config_name"]:
                        models[entry["config_name"].casefold()] = name
    except (OSError, ValueError, subprocess.TimeoutExpired):
        pass
    _models[executable] = (time.monotonic(), models)
    return models


def forget_models() -> None:
    _models.clear()
