"""Shared TTADK launch boundaries: arguments, environment and noninteractive sign-in checks."""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import time
from collections.abc import Mapping, Sequence
from enum import Enum
from pathlib import Path

from ... import child_env
from . import RuntimeUnsupported


class TtadkProblem(str, Enum):
    NOT_FOUND = "not_found"
    NOT_SIGNED_IN = "not_signed_in"
    CHECK_FAILED = "check_failed"
    WRONG_TOOL = "wrong_tool"

    def message(self, runtime_type: str) -> str:
        explanation = {
            self.NOT_FOUND: "`ttadk` is not on the server's PATH: install TTADK or add it to PATH",
            self.NOT_SIGNED_IN: "TTADK has no valid sign-in on this machine: run `ttadk auth login sso` in a terminal",
            self.CHECK_FAILED: "TTADK's sign-in check timed out or returned unreadable output: "
                               "run `ttadk models` in a terminal and check TTADK's sign-in",
            self.WRONG_TOOL: "TTADK's configured tool command does not match this runtime: "
                             "correct ai_tool_commands in ~/.ttadk/setting.json",
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
ENV_OWNED = ENV_UNSET | {"TTADK_DISABLE_COMPLETION_AUTO_INSTALL"}


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
_PROBE_TTL_SEC = 60.0
_probed: dict[tuple[str, str], tuple[float, TtadkProblem | None]] = {}


def _sign_in_problem(executable: str, tool: str) -> TtadkProblem | None:
    if shutil.which(executable) is None:
        return TtadkProblem.NOT_FOUND
    try:
        done = subprocess.run([executable, "models", "-t", tool, "-f", "json"],
                              capture_output=True, text=True, stdin=subprocess.DEVNULL,
                              timeout=_SIGN_IN_TIMEOUT_SEC, check=False, env=child_env.inherited())
        if done.returncode != 0:
            return TtadkProblem.NOT_SIGNED_IN
        result = json.loads(done.stdout)
        if isinstance(result, dict) and isinstance(result.get("models"), list):
            return None
    except FileNotFoundError:
        return TtadkProblem.NOT_FOUND
    except (OSError, UnicodeError, ValueError, subprocess.TimeoutExpired):
        pass
    return TtadkProblem.CHECK_FAILED


async def sign_in_problem(executable: str, tool: str) -> TtadkProblem | None:
    key = (executable, tool)
    cached = _probed.get(key)
    if cached is None or time.monotonic() - cached[0] > _PROBE_TTL_SEC:
        problem = await asyncio.to_thread(_sign_in_problem, executable, tool)
        _probed[key] = (time.monotonic(), problem)
    return _probed[key][1]


def forget_probe() -> None:
    """Invalidate cached sign-in checks, including after test stand-ins change."""
    _probed.clear()
