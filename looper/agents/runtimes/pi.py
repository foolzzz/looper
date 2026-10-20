"""The pi runtime's identity, and a probe that never calls a model.

pi is a fifth runtime type (the pi runtime RFC, sections 1 and 6). Registering
it discovers ``rt-pi`` when ``pi`` is on PATH. This module does not launch a
run, bind a provider, keep a session or speak to a model: those paths raise
``RuntimeUnsupported`` until they exist. There is no pi login and no schema
change.

A probe runs only ``--version`` and ``--help``, in a temporary home, agent
directory and session directory, offline, with a 10 second deadline. 1.3.0 is
the oldest version that passes. An older version, a version that cannot be
read, or help that lacks a flag the launch needs is reported and gives the
runtime nothing it can do. Capabilities stay empty either way until the
launch, sessions, stream and Looper tool bridge exist. The later ``mcp``
capability is that bridge only, not a general MCP client.
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import AsyncIterator
from pathlib import Path
from typing import TYPE_CHECKING, Any, NoReturn

from ... import child_env
from . import (
    ApiProtocol, AuthKind, Capability, Probe, PromptBlock, ProviderBinding, RunCost, RuntimeAdapter,
    RuntimeUnsupported,
)
from .instructions import InstructionFile

if TYPE_CHECKING:
    from ..agent_settings import Launch
    from ..profile_registry import ProviderSpec
    from ..run_usage import Usage
    from . import ResidentSession

_PROBE_TIMEOUT_SEC = 10.0
_VERSION_MAX = 200
# The oldest pi whose --help has the flags a later launch uses.
MIN_VERSION = (1, 3, 0)
# Whole tokens. ``--session`` must not match ``--session-dir`` or ``--session-id``.
PROBE_HELP_TOKENS = (
    "-p",
    "--mode",
    "json",
    "rpc",
    "--offline",
    "--no-approve",
    "--no-context-files",
    "--no-extensions",
    "--no-mcp",
    "--no-skills",
    "--no-prompt-templates",
    "--no-themes",
    "-e",
    "--provider",
    "--model",
    "--thinking",
    "--system-prompt",
    "--append-system-prompt",
    "--tools",
    "--no-session",
    "--session-dir",
    "--session",
)
# What would point the probe at the operator's own directories. HOME is replaced; the XDG homes are not passed.
_HIDDEN_DIRS = ("HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME")
_VERSION = re.compile(r"(\d+)\.(\d+)\.(\d+)")


class _ProbeFailure(Exception):
    """``--version`` or ``--help`` did not finish; ``message`` is the probe error."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class PiRuntime(RuntimeAdapter):
    """pi's type. Probes work; a run does not, until the rest of the runtime exists."""

    type = "pi"
    label = "pi"
    default_executable = "pi"
    # Empty until the launch, sessions, stream and Looper tool bridge exist. Not a general MCP client.
    capabilities = frozenset()
    protocols = frozenset({ApiProtocol.OPENAI_RESPONSES, ApiProtocol.OPENAI_CHAT})
    # API keys only. pi's own /login and OAuth are not providers Looper binds.
    auth_kinds = frozenset({AuthKind.API_KEY})
    isolation_id = "pi-home/v0"

    def capabilities_for(self, version: str | None) -> frozenset[Capability]:
        """Nothing is advertised yet, whatever ``version`` the probe read.

        The later set is run, a resident session, resume, steer, read tools,
        compaction events, and mcp for the Looper tool bridge only. That is
        not a general MCP client, and none of it is enabled until those paths
        exist.
        """
        del version
        return frozenset()

    def probe(self, executable: str) -> Probe:
        """``executable --version`` and, when that is new enough, ``--help``.

        Both run offline in a temporary home, agent directory and session
        directory, and each has 10 seconds. Nothing of the operator's pi
        configuration is read, and no model is called.
        """
        root = Path(tempfile.mkdtemp(prefix="looper-pi-probe-"))
        try:
            home, agent, session = root / "home", root / "home" / "agent", root / "session"
            for path in (home, agent, session):
                path.mkdir(mode=0o700)
                os.chmod(path, 0o700)
            env = _probe_env(home, agent, session)
            try:
                done = _run(executable, "--version", env, home)
            except _ProbeFailure as failure:
                return Probe(None, self.capabilities_for(None), failure.message)
            line = _first_line(done.stdout or "") or _first_line(done.stderr or "")
            if done.returncode != 0 or not line:
                return Probe(None, self.capabilities_for(None), _version_failure(executable, done.returncode, line))
            shown = line[:_VERSION_MAX]
            found = _VERSION.search(line)
            if found is None:
                return Probe(shown, self.capabilities_for(shown), _unknown_version(executable, shown))
            if tuple(int(part) for part in found.groups()) < MIN_VERSION:
                return Probe(shown, self.capabilities_for(shown), _old_version(shown, executable))
            try:
                help_done = _run(executable, "--help", env, home)
            except _ProbeFailure as failure:
                return Probe(shown, self.capabilities_for(shown), f"{failure.message}; probe again")
            help_text = f"{help_done.stdout or ''}{help_done.stderr or ''}"
            if help_done.returncode != 0 or not help_text.strip():
                return Probe(shown, self.capabilities_for(shown), _help_failure(executable, help_done.returncode, help_text))
            missing = [token for token in PROBE_HELP_TOKENS if not _has_token(help_text, token)]
            if missing:
                return Probe(shown, self.capabilities_for(shown), _missing_flags(executable, missing))
            return Probe(shown, self.capabilities_for(shown))
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def _refuse(self) -> NoReturn:
        raise RuntimeUnsupported("pi does not implement this yet; only version and help probes run")

    def prompt_blocks(self, launch: Launch, looper_prompt: str, *, cwd: str | Path | None = None) -> list[PromptBlock]:
        self._refuse()

    def isolation_args(self, cwd: str | Path) -> list[str]:
        self._refuse()

    def bind_provider(self, provider: ProviderSpec, model: str, effort: str) -> ProviderBinding:
        self._refuse()

    def launch_args(self, launch: Launch) -> list[str]:
        self._refuse()

    def environment(self, launch: Launch) -> dict[str, str]:
        self._refuse()

    def resident_command(self, executable: str, *, cwd: str | Path, prompt_file: Path, launch: Launch,
                         max_turns: int = 0) -> list[str]:
        self._refuse()

    def resume_args(self, session_id: str) -> list[str]:
        self._refuse()

    def run_command(self, executable: str, *, cwd: str | Path, prompt_file: Path, launch: Launch,
                    max_turns: int = 0, session_dir: Path | None = None) -> list[str]:
        self._refuse()

    def resident_session(self, label: str) -> ResidentSession:
        self._refuse()

    async def write_turn(self, stdin: asyncio.StreamWriter, text: str, *, uuid: str | None = None) -> None:
        self._refuse()

    def parse(self, stdout: asyncio.StreamReader, *, model: str | None = None) -> AsyncIterator[dict[str, Any]]:
        self._refuse()

    def tool_summary(self, tool_name: str, tool_input: dict[str, Any]) -> str:
        self._refuse()

    def cost(self, usage: Usage, provider: ProviderSpec) -> RunCost:
        self._refuse()

    def budget_error(self, result_event: dict[str, Any] | None) -> str | None:
        self._refuse()

    def instruction_files(self, cwd: str | Path) -> tuple[InstructionFile, ...]:
        self._refuse()


def _probe_env(home: Path, agent: Path, session: Path) -> dict[str, str]:
    """A child's environment pointed at the probe's directories, offline, without the operator's pi paths or logins."""
    dropped = set(_HIDDEN_DIRS) | set(child_env.RUNTIMES)
    env = {key: value for key, value in child_env.inherited().items()
           if key not in dropped and not key.startswith("PI_")}
    env.update({
        "HOME": str(home),
        "PI_CODING_AGENT_DIR": str(agent),
        "PI_CODING_AGENT_SESSION_DIR": str(session),
        "PI_OFFLINE": "1",
        "PI_TELEMETRY": "0",
        "PI_SKIP_VERSION_CHECK": "1",
    })
    return env


def _run(executable: str, flag: str, env: dict[str, str], cwd: Path) -> subprocess.CompletedProcess[str]:
    """One of the two probe commands. Raises ``_ProbeFailure`` when it does not finish."""
    try:
        return subprocess.run([executable, flag], capture_output=True, text=True, timeout=_PROBE_TIMEOUT_SEC,
                              check=False, env=env, cwd=cwd)
    except FileNotFoundError:
        raise _ProbeFailure(f"{executable!r} was not found") from None
    except (OSError, subprocess.TimeoutExpired, UnicodeError) as exc:
        raise _ProbeFailure(f"{executable} {flag} did not run: {type(exc).__name__}: {exc}") from None


def _first_line(text: str) -> str:
    return next((line.strip() for line in text.splitlines() if line.strip()), "")


def _has_token(text: str, token: str) -> bool:
    """Whether ``token`` appears as its own flag or word, not as the prefix of a longer one."""
    return re.search(rf"(?<![\w-]){re.escape(token)}(?![\w-])", text) is not None


def _version_failure(executable: str, code: int, line: str) -> str:
    if not line and code == 0:
        return f"{executable} --version printed nothing"
    if not line:
        return f"{executable} --version failed (exit code {code})"
    return f"{executable} --version failed (exit code {code}): {line[:_VERSION_MAX]}"


def _old_version(shown: str, executable: str) -> str:
    return (f"{shown} from {executable} --version is older than 1.3.0, the oldest pi Looper supports; "
            "upgrade pi and probe again")


def _unknown_version(executable: str, shown: str) -> str:
    return (f"{executable} --version printed {shown!r}, which is not a pi version Looper can read; "
            "install pi 1.3.0 or newer and probe again")


def _help_failure(executable: str, code: int, text: str) -> str:
    line = _first_line(text)
    if code == 0 and not line:
        return f"{executable} --help printed nothing; probe again"
    detail = f": {line[:_VERSION_MAX]}" if line else ""
    return f"{executable} --help failed (exit code {code}){detail}; probe again"


def _missing_flags(executable: str, missing: list[str]) -> str:
    return (f"{executable} --help does not show {', '.join(missing)}; "
            "upgrade pi to 1.3.0 or newer and probe again")
