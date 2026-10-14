"""The Claude Code runtime: the ``claude`` CLI in ``--print`` stream-json mode.

The process mechanics (isolation flags, the resident process, the stream)
live in ``claude_cli``; this adapter lays them out into each kind of run's
command, the way every tier started Claude Code before runtimes were
pluggable.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
from collections.abc import AsyncIterator
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .. import claude_cli
from ..run_usage import run_usage
from . import Capability, Probe, RuntimeAdapter

if TYPE_CHECKING:
    from ..agent_settings import Launch
    from ..run_usage import Usage

_PROBE_TIMEOUT_SEC = 10.0


class ClaudeCodeRuntime(RuntimeAdapter):
    type = "claude-code"
    label = "Claude Code"
    default_executable = "claude"
    capabilities = frozenset({Capability.RUN, Capability.RESIDENT_SESSION, Capability.RESUME})
    isolation_id = claude_cli.ISOLATION_ID

    def probe(self, executable: str) -> Probe:
        """``<executable> --version``: Claude Code prints its version and exits without calling a model."""
        try:
            done = subprocess.run([executable, "--version"], capture_output=True, text=True,
                                  timeout=_PROBE_TIMEOUT_SEC, check=False)
        except FileNotFoundError:
            return Probe(None, self.capabilities, f"{executable!r} was not found")
        except (OSError, subprocess.TimeoutExpired) as exc:
            return Probe(None, self.capabilities, f"{executable} --version did not run: {type(exc).__name__}: {exc}")
        first = next((line.strip() for line in (done.stdout or done.stderr).splitlines() if line.strip()), "")
        if done.returncode != 0 or not first:
            return Probe(None, self.capabilities, f"{executable} --version failed (exit code {done.returncode})"
                                                  + (f": {first}" if first else ""))
        return Probe(first[:200], self.capabilities_for(first))

    def isolation_args(self, cwd: str | Path) -> list[str]:
        return claude_cli.isolation_args(cwd)

    def launch_args(self, launch: Launch) -> list[str]:
        """The Claude Code flags every agent starts with: the permissions, the model, the effort, the
        plugin directories of the installed items, the directories of the project knowledge and the
        per-run spend cap."""
        from ..claude_cli import budget_args, knowledge_args, library_args, permission_args

        return [
            *permission_args(),
            "--model", launch.model,
            *(["--effort", launch.effort] if launch.effort else []),
            *library_args(launch.library),
            *knowledge_args(launch.knowledge),
            *budget_args(launch.max_budget_usd),
        ]

    def environment(self, launch: Launch) -> dict[str, str] | None:
        """The subprocess environment: the server's, then the settings' variables; None without any."""
        return {**os.environ, **dict(launch.env)} if launch.env else None

    def resident_command(self, executable: str, *, cwd: str | Path, prompt_file: Path, launch: Launch,
                         max_turns: int = 0) -> list[str]:
        return [
            executable,
            "--print",
            "--verbose",
            "--output-format", "stream-json",
            # Each message is one user turn on stdin; Claude idles between turns.
            "--input-format", "stream-json",
            # Partial deltas stream the reply as it is written and keep the turn's heartbeat current.
            "--include-partial-messages",
            # Claude echoes each message, marked with the uuid it was sent with, when it starts the message's
            # turn; a turn Claude runs on its own has no such echo.
            "--replay-user-messages",
            # None of the operator's personal settings, memory, hooks, plugins or MCP servers; the
            # permissions every agent shares and the plugins and skills installed for it come with the launch
            # args.
            *claude_cli.isolation_args(cwd),
            *claude_cli.system_prompt_args(prompt_file),
            *self.launch_args(launch),
            *(["--max-turns", str(max_turns)] if max_turns > 0 else []),
        ]

    def resume_args(self, session_id: str) -> list[str]:
        return ["--resume", session_id]

    def run_command(self, executable: str, *, cwd: str | Path, prompt_file: Path, launch: Launch,
                    max_turns: int = 0) -> list[str]:
        return [
            executable,
            "--print",
            "--verbose",
            "--output-format", "stream-json",
            # Partial deltas keep the run's heartbeat current while Claude thinks.
            "--include-partial-messages",
            # The prompt and any later user turns arrive on stdin, so the
            # Planner can step in while the run is going.
            "--input-format", "stream-json",
            "--replay-user-messages",
            # None of the operator's personal settings, memory, hooks, plugins
            # or MCP servers (whose teardown could also make the CLI exit
            # non-zero). Built-in tools are unaffected; the permissions every
            # agent shares and the plugins and skills installed for this run
            # come with the launch args.
            *claude_cli.isolation_args(cwd),
            *claude_cli.system_prompt_args(prompt_file),
            *self.launch_args(launch),
            *(["--max-turns", str(max_turns)] if max_turns > 0 else []),
        ]

    def resident_session(self, label: str) -> claude_cli.ResidentClaude:
        return claude_cli.ResidentClaude(label=label)

    async def write_turn(self, stdin: asyncio.StreamWriter, text: str, *, uuid: str | None = None) -> None:
        await claude_cli.write_user_turn(stdin, text, uuid=uuid)

    def events(self, stdout: asyncio.StreamReader) -> AsyncIterator[dict[str, Any]]:
        return claude_cli.stream_events(stdout)

    def usage(self, result_event: dict[str, Any] | None, *, before: Usage | None = None) -> Usage:
        return run_usage(result_event, before=before)

    def budget_error(self, result_event: dict[str, Any] | None) -> str | None:
        return claude_cli.budget_error(result_event)
