"""Shared TTADK launch boundaries: arguments, environment and noninteractive sign-in checks."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
from collections.abc import Mapping, Sequence
from contextlib import ExitStack
from enum import Enum
from pathlib import Path

from ... import child_env
from . import RuntimeUnsupported


def error_tail(stderr: str) -> str:
    """Redact credentials before retaining the last 1000 characters for a run and its activity."""
    from ..log_redaction import redact
    from .omp import safe_error

    return safe_error(redact(stderr), limit=None)[-1000:]


class TtadkProblem(str, Enum):
    NOT_FOUND = "not_found"
    NOT_SIGNED_IN = "not_signed_in"
    CHECK_FAILED = "check_failed"
    CHECKING = "checking"
    WRONG_TOOL = "wrong_tool"

    @property
    def blocks_launch(self) -> bool:
        return self in (self.NOT_FOUND, self.NOT_SIGNED_IN, self.WRONG_TOOL)

    def message(self, runtime_type: str) -> str:
        if self is self.CHECKING:
            return f"checking TTADK's sign-in for `{runtime_type}`"
        if self is self.WRONG_TOOL:
            explanation = (f"TTADK's settings make cursor-cli run `{tool_command('cursor-cli')}`, "
                           "so Looper cannot confirm it is Cursor's CLI")
        else:
            explanation = {
                self.NOT_FOUND: "`ttadk` is not on the server's PATH: install TTADK or add it to PATH",
                self.NOT_SIGNED_IN: "TTADK has no valid sign-in on this machine: run `ttadk auth login sso` in a terminal",
                self.CHECK_FAILED: "TTADK's sign-in check timed out or returned unreadable output: "
                                   "run `ttadk models` in a terminal and check TTADK's sign-in",
            }[self]
        return (f"{explanation}, or choose the native `{runtime_type}` runtime for this Agent Profile "
                "(its runs do not count in TTADK's records)")


def prefix(executable: str, tool: str) -> list[str]:
    if tool == "codex":
        return [executable, "codex", "--skip-check", "--mode", "mixed", "--"]
    if tool == "cursor-cli":
        return [executable, "code", "-t", "cursor-cli", "--skip-check", "--"]
    raise ValueError(f"unsupported TTADK tool: {tool!r}")


FORBIDDEN_ARGS = frozenset({"-v", "--version", "handoff", "--debug", "--verbose", "--log-level"})


def check_args(args: Sequence[str]) -> None:
    """Check tool arguments, all of which are placed after TTADK's ``--`` separator."""
    for index, arg in enumerate(args):
        if arg in FORBIDDEN_ARGS:
            raise RuntimeUnsupported(f"TTADK cannot forward tool argument {arg!r}")
        if arg == "-c" and index + 1 < len(args) and args[index + 1].startswith("model_provider"):
            raise RuntimeUnsupported(f"TTADK cannot forward -c {args[index + 1]!r}: it would bypass TTADK's gateway")


ENV_UNSET = frozenset({"CI", "TTADK_NO_TELEMETRY", "TTADK_DEBUG", "DEBUG", "FORCE_COLOR"})


def environment(base: Mapping[str, str]) -> dict[str, str]:
    return {**{key: value for key, value in base.items() if key not in ENV_UNSET},
            "TTADK_DISABLE_COMPLETION_AUTO_INSTALL": "1"}


def probe_version(executable: str) -> tuple[str | None, str | None]:
    try:
        done = subprocess.run([executable, "--version"], capture_output=True, text=True,
                              stdin=subprocess.DEVNULL, timeout=10, check=False, env=child_env.inherited())
    except (OSError, UnicodeError, subprocess.TimeoutExpired) as exc:
        return None, f"TTADK --version did not run: {type(exc).__name__}"
    first = next((line.strip() for line in (done.stdout or done.stderr).splitlines() if line.strip()), "")
    if done.returncode != 0 or not first:
        return None, f"TTADK --version failed (exit code {done.returncode})"
    return first[:200], None


def tool_command(tool: str) -> str:
    default = {"codex": "codex", "cursor-cli": "agent"}[tool]
    try:
        settings = json.loads((Path.home() / ".ttadk" / "setting.json").read_text(encoding="utf-8"))
        commands = settings.get("ai_tool_commands", {})
        command = commands.get(tool) if isinstance(commands, dict) else None
        return command if isinstance(command, str) and command.strip() else default
    except (OSError, UnicodeError, ValueError, AttributeError):
        return default


_SIGN_IN_TIMEOUT_SEC = 20.0
_CURSOR_SIGN_IN_TIMEOUT_SEC = 45.0
_PROBE_TTL_SEC = 60.0
_probed: dict[tuple[str, str], tuple[float, TtadkProblem | None]] = {}
_probe_locks: dict[tuple[str, str], threading.Lock] = {}


def launch_note(executable: str, tool: str) -> str | None:
    """Retain an inconclusive preflight in the launch record without probing again."""
    cached = _probed.get((executable, tool))
    if cached is not None and cached[1] is TtadkProblem.CHECK_FAILED:
        return f"could not finish ({cached[1].value})"
    return None


def _sign_in_problem(executable: str, tool: str) -> TtadkProblem | None:
    if shutil.which(executable) is None:
        return TtadkProblem.NOT_FOUND
    try:
        with ExitStack() as stack:
            env = child_env.inherited()
            timeout = _SIGN_IN_TIMEOUT_SEC
            if tool == "cursor-cli":
                from . import cursor_login
                from .cursor_agent import write_ttadk_shim

                directory = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix="looper-ttadk-cursor-")))
                shim = write_ttadk_shim(directory)
                env = environment(cursor_login.environment(env))
                env["PATH"] = f"{shim}{os.pathsep}{env.get('PATH', '')}"
                for name, child in zip(cursor_login.DIRECTORY_VARS, ("config", "data")):
                    (directory / child).mkdir(mode=0o700)
                    env[name] = str(directory / child)
                timeout = _CURSOR_SIGN_IN_TIMEOUT_SEC
            done = subprocess.run([executable, "models", "-t", tool, "-f", "json"],
                                  capture_output=True, text=True, stdin=subprocess.DEVNULL,
                                  timeout=timeout, check=False, env=env)
        if done.returncode != 0:
            return TtadkProblem.NOT_SIGNED_IN
        result = json.loads(done.stdout)
        if isinstance(result, dict) and isinstance(result.get("models"), list):
            return None
    except FileNotFoundError:
        return TtadkProblem.NOT_FOUND
    except (OSError, UnicodeError, ValueError, subprocess.TimeoutExpired, RuntimeUnsupported):
        pass
    return TtadkProblem.CHECK_FAILED


def _refresh_probe(executable: str, tool: str) -> TtadkProblem | None:
    key = (executable, tool)
    cached = _probed.get(key)
    if cached is None or time.monotonic() - cached[0] > _PROBE_TTL_SEC:
        problem = _sign_in_problem(executable, tool)
        _probed[key] = (time.monotonic(), problem)
    return _probed[key][1]


def _cached_probe(executable: str, tool: str) -> TtadkProblem | None:
    with _probe_locks.setdefault((executable, tool), threading.Lock()):
        return _refresh_probe(executable, tool)


def _background_probe(executable: str, tool: str, lock: threading.Lock) -> None:
    try:
        _refresh_probe(executable, tool)
    finally:
        lock.release()


async def sign_in_problem(executable: str, tool: str, *, wait: bool = True) -> TtadkProblem | None:
    """Launches wait for a current answer; status views return cached state and refresh in the background."""
    if wait:
        return await asyncio.to_thread(_cached_probe, executable, tool)
    key = (executable, tool)
    cached = _probed.get(key)
    lock = _probe_locks.setdefault(key, threading.Lock())
    if (cached is None or time.monotonic() - cached[0] > _PROBE_TTL_SEC) and lock.acquire(blocking=False):
        try:
            threading.Thread(target=_background_probe, args=(executable, tool, lock),
                             name="ttadk-sign-in", daemon=True).start()
        except BaseException:
            lock.release()
            raise
    return cached[1] if cached else TtadkProblem.CHECKING


def forget_probe() -> None:
    """Invalidate cached sign-in checks, including after test stand-ins change."""
    for lock in list(_probe_locks.values()):
        with lock:
            pass
    _probed.clear()
