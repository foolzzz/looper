"""The Claude Code runtime: the ``claude`` CLI in ``--print`` stream-json mode.

The process mechanics (isolation flags, the resident process, the stream)
live in ``claude_cli``; this adapter lays them out into each kind of run's
command, the way every tier started Claude Code before runtimes were
pluggable.

**Providers.** With its own login (the Claude subscription) a run needs
nothing beyond ``--model``. With an API key, the binding sets the endpoint, the
key and, for any endpoint other than Anthropic's own, every Claude model alias
Claude Code may use behind the agent's back. Verified against Claude Code
2.1.285 in ``--print`` stream-json mode, through a logging proxy in front of an
Anthropic-format endpoint, with the operator's subscription signed in:

- ``ANTHROPIC_API_KEY`` is sent as ``x-api-key`` and reported by the init
  event as ``apiKeySource: "ANTHROPIC_API_KEY"``; ``ANTHROPIC_AUTH_TOKEN`` is
  sent as ``Authorization: Bearer`` and the init event says ``apiKeySource:
  "none"``, as it does for the subscription. Either way the request carries the
  key and not the subscription's token, and Claude Code warns that the key
  "takes precedence over your claude.ai login". Set together, both headers go.
  Third-party endpoints document ``ANTHROPIC_AUTH_TOKEN`` (GLM, DeepSeek and
  Kimi; Kimi asks for ``ANTHROPIC_API_KEY`` to be removed), Anthropic's own
  takes ``x-api-key``: so the key goes in ``ANTHROPIC_AUTH_TOKEN`` with a
  ``base_url`` and in ``ANTHROPIC_API_KEY`` without one, and the other variable
  is removed from the environment the run inherits.
- ``--model`` wins over ``ANTHROPIC_MODEL``; the agent's model is ``--model``.
- A subagent started without a model runs the main model, or
  ``CLAUDE_CODE_SUBAGENT_MODEL`` when set. One started with an alias
  (``haiku``, ``sonnet``, ``opus``, as the model may choose) requests the
  model the alias names: ``claude-haiku-4-5-20251001`` unless
  ``ANTHROPIC_DEFAULT_HAIKU_MODEL`` maps it, and that request fails at an
  endpoint without Claude models. ``CLAUDE_CODE_SUBAGENT_MODEL`` does not
  override an alias. So the binding maps the opus and sonnet aliases and the
  subagent model to the agent's model, and the haiku alias and the small/fast
  model (``ANTHROPIC_SMALL_FAST_MODEL``, its older name) to the background
  model of the provider's catalog (the agent's model when it names none).
- ``total_cost_usd`` and ``modelUsage[*].costUSD`` price any model, Claude's
  or not, at Claude's prices (``costBasis: "unknown"`` for a model Claude
  Code does not know), so a run on another endpoint is priced by Looper at
  its provider's prices (``cost_source``), and ``--max-budget-usd`` is not
  passed: Looper enforces the per-run cap itself (``claude_cli``).
"""

from __future__ import annotations

import asyncio
import os
import subprocess
from collections.abc import AsyncIterator
from pathlib import Path
from typing import TYPE_CHECKING, Any

from urllib.parse import urlsplit

from .. import claude_cli
from . import (
    ApiProtocol, AuthKind, Capability, CostSource, Probe, ProviderBinding, RunCost, RuntimeAdapter, RuntimeUnsupported,
)

if TYPE_CHECKING:
    from ..agent_registry import ProviderSpec
    from ..agent_settings import Launch
    from ..run_usage import Usage

_PROBE_TIMEOUT_SEC = 10.0

BASE_URL = "ANTHROPIC_BASE_URL"
API_KEY = "ANTHROPIC_API_KEY"  # sent as x-api-key
AUTH_TOKEN = "ANTHROPIC_AUTH_TOKEN"  # sent as Authorization: Bearer
# The variables that name a model Claude Code uses besides --model: the aliases a subagent or a side request
# may ask for, and the subagents' own model.
MAIN_MODEL_VARS = ("ANTHROPIC_DEFAULT_OPUS_MODEL", "ANTHROPIC_DEFAULT_SONNET_MODEL", "CLAUDE_CODE_SUBAGENT_MODEL")
BACKGROUND_MODEL_VARS = ("ANTHROPIC_DEFAULT_HAIKU_MODEL", "ANTHROPIC_SMALL_FAST_MODEL")
# Anthropic's own API: the endpoint Claude Code uses without a base URL.
ANTHROPIC_HOSTS = frozenset({"api.anthropic.com"})


def official_endpoint(base_url: str) -> bool:
    """Whether ``base_url`` is Anthropic's own API: none given, or its host."""
    return not base_url or (urlsplit(base_url).hostname or "").lower() in ANTHROPIC_HOSTS


class ClaudeCodeRuntime(RuntimeAdapter):
    type = "claude-code"
    label = "Claude Code"
    default_executable = "claude"
    # Verified against Claude Code 2.1.285 (claude_cli); its replies follow Looper's contracts by prompt, so no
    # structured_output.
    capabilities = frozenset(set(Capability) - {Capability.STRUCTURED_OUTPUT})
    protocols = frozenset({ApiProtocol.ANTHROPIC_MESSAGES})
    # Its own login (the Claude subscription), and an API key for Anthropic's API or an Anthropic-format endpoint.
    auth_kinds = frozenset({AuthKind.OAUTH, AuthKind.API_KEY})
    isolation_id = claude_cli.ISOLATION_ID
    provider_env_owned = frozenset({BASE_URL, API_KEY, AUTH_TOKEN, "ANTHROPIC_MODEL", *MAIN_MODEL_VARS,
                                    *BACKGROUND_MODEL_VARS})

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

    def bind_provider(self, provider: ProviderSpec, model: str, effort: str) -> ProviderBinding:
        """What points a run at ``provider`` (the module's notes), followed by the provider's own variables.

        Claude Code's own login (the ``claude-subscription`` provider) needs
        nothing beside ``--model`` and ``--effort``: it is how every agent ran
        before providers. An API key goes in ``ANTHROPIC_API_KEY`` for
        Anthropic's own API and in ``ANTHROPIC_AUTH_TOKEN`` with the
        ``base_url`` of any other endpoint, whose binding also maps every Claude
        model alias to the provider's models.
        """
        own = tuple(provider.env)
        if provider.auth is AuthKind.OAUTH and provider.oauth_runtime_type == self.type:
            return ProviderBinding(env=own)
        if provider.auth is not AuthKind.API_KEY:
            raise RuntimeUnsupported(f"Claude Code cannot run with provider {provider.name!r}, the login of "
                                     f"{provider.oauth_runtime_type} runtimes")
        if not provider.api_key:
            raise RuntimeUnsupported(f"provider {provider.name!r} has no API key for this run")
        if official_endpoint(provider.base_url):
            return ProviderBinding(
                env=((API_KEY, provider.api_key), *(((BASE_URL, provider.base_url),) if provider.base_url else ()),
                     *own),
                unset=(AUTH_TOKEN, *(() if provider.base_url else (BASE_URL,))))
        entry = provider.catalog_model(model)
        background = (entry.background_model if entry else None) or model
        return ProviderBinding(
            env=((BASE_URL, provider.base_url), (AUTH_TOKEN, provider.api_key),
                 *((name, model) for name in MAIN_MODEL_VARS), *((name, background) for name in BACKGROUND_MODEL_VARS),
                 *own),
            unset=(API_KEY,))

    def launch_args(self, launch: Launch) -> list[str]:
        """The Claude Code flags every agent starts with: the permissions, the model, the effort, the
        plugin directories of the installed items, the directories of the project knowledge, the
        per-run spend cap (when Claude Code's own figure is the run's cost) and the provider's binding."""
        from ..claude_cli import budget_args, knowledge_args, library_args, permission_args

        provider = launch.agent.provider
        return [
            *permission_args(),
            "--model", launch.model,
            *(["--effort", launch.effort] if launch.effort else []),
            *library_args(launch.library),
            *knowledge_args(launch.knowledge),
            *(budget_args(launch.max_budget_usd) if self.cost_source(provider) is CostSource.RUNTIME else []),
            *self.bind_provider(provider, launch.model, launch.effort).args,
        ]

    def environment(self, launch: Launch) -> dict[str, str] | None:
        """The subprocess environment: the server's, then the settings' variables and the provider's binding,
        less what the binding removes; None without any."""
        binding = self.bind_provider(launch.agent.provider, launch.model, launch.effort)
        added = {**dict(launch.env), **dict(binding.env)}
        if not added and not binding.unset:
            return None
        return {**{k: v for k, v in os.environ.items() if k not in binding.unset}, **added}

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

    def parse(self, stdout: asyncio.StreamReader, *, model: str | None = None) -> AsyncIterator[dict[str, Any]]:
        return claude_cli.stream_events(stdout)  # Claude Code names its models itself

    def tool_summary(self, tool_name: str, tool_input: dict[str, Any]) -> str:
        if tool_name == "Read":
            return f"Reading: {tool_input.get('file_path', '?')}"
        if tool_name == "Edit":
            return f"Editing: {tool_input.get('file_path', '?')}"
        if tool_name == "Write":
            return f"Writing: {tool_input.get('file_path', '?')}"
        if tool_name == "Bash":
            cmd = tool_input.get("command", "")
            return f"Running: {cmd[:80]}"
        if tool_name in ("Glob", "Grep"):
            pattern = tool_input.get("pattern", tool_input.get("query", ""))
            return f"{tool_name}: {pattern[:60]}"
        return f"Tool: {tool_name}"

    def cost_source(self, provider: ProviderSpec) -> CostSource:
        """Claude Code prices every model at Claude's prices: right for its own login (an "equivalent" cost) and
        for Anthropic's API, and for anything else Looper prices the tokens at the provider's prices."""
        if provider.auth is AuthKind.OAUTH or official_endpoint(provider.base_url):
            return CostSource.RUNTIME
        return CostSource.PROVIDER_PRICE

    def cost(self, usage: Usage, provider: ProviderSpec) -> RunCost:
        """The run's ``usage``, priced where its totals were read: from ``total_cost_usd`` (``runtime``), or from
        ``modelUsage`` at the provider's prices (``provider_price``, ``run_usage.reported_totals``)."""
        return RunCost(usage, self.cost_source(provider))

    def budget_error(self, result_event: dict[str, Any] | None) -> str | None:
        return claude_cli.budget_error(result_event)
