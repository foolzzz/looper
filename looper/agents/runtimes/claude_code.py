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
  model the alias names: ``claude-haiku-5-5`` for ``haiku`` unless
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

**Agent teams** (an Agent Profile's ``agent_teams``, never for a Planner:
``Launch.agent_teams``). Claude Code 2.1.285 enables them only when
``CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS`` is set. A teammate it spawns runs
where ``--teammate-mode`` says; under its default (``auto``) a process inside
tmux starts each one as a tmux process of its own, whose cost the run's result
never counts, so a run with teams has ``--teammate-mode in-process``. Then a
teammate runs in the run's process as a background task
(``system/task_started``). One that outlives the turn that spawned it starts a
turn of the lead's own with its message and another when it ends, also after
stdin closed, and the process exits after them: every ``result`` carries the
session's totals, the teammate's models included in ``modelUsage``, and the
last one comes once the teammate is done. A teammate does not get the lead's
``--append-system-prompt-file``. Its assistant messages are in the stream (with
a ``parent_tool_use_id``) but its model requests are not (no
``message_start``), so the per-run soft cap misses their tokens. The launch
owns the variable: a run without teams has it removed, whatever the server's
environment, the settings' or the provider's variables say.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
import subprocess
from collections.abc import AsyncIterator, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

from urllib.parse import urlsplit

from ... import child_env
from .. import claude_cli
from ..chat_scopes import LaunchMode
from ..llmrec import TRANSLATED_BACKEND_ENV
from . import (
    ApiProtocol, AuthKind, Capability, CostSource, ImageInput, Launcher, LoadedSources, ManagedHooks, Probe,
    PromptBlock, ProviderBinding, ReadOnly, RunCost, RoomPush, RuntimeAdapter, RuntimeUnsupported, instructions,
    library_mcp_servers, plugin_skills,
)
from .instructions import InstructionFile

if TYPE_CHECKING:
    from ..profile_registry import LegacyProviderSpec as ProviderSpec
    from ..agent_settings import Launch
    from ..failures import Evidence, Failure
    from ..run_usage import Usage

_PROBE_TIMEOUT_SEC = 10.0

BASE_URL = "ANTHROPIC_BASE_URL"
API_KEY = "ANTHROPIC_API_KEY"  # sent as x-api-key
AUTH_TOKEN = "ANTHROPIC_AUTH_TOKEN"  # sent as Authorization: Bearer
# The variables that name a model Claude Code uses besides --model: the aliases a subagent or a side request
# may ask for, and the subagents' own model.
MAIN_MODEL_VARS = ("ANTHROPIC_DEFAULT_OPUS_MODEL", "ANTHROPIC_DEFAULT_SONNET_MODEL", "CLAUDE_CODE_SUBAGENT_MODEL")
BACKGROUND_MODEL_VARS = ("ANTHROPIC_DEFAULT_HAIKU_MODEL", "ANTHROPIC_SMALL_FAST_MODEL")
# Agent teams (the module's notes): the variable that enables them, and where their teammates run.
AGENT_TEAMS = "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS"
TEAMMATE_MODE_ARGS = ("--teammate-mode", "in-process")
# Gateway safeguards override settings and provider environment variables.
TRANSLATED_ENV = (
    *TRANSLATED_BACKEND_ENV,
    ("CLAUDE_CODE_ATTRIBUTION_HEADER", "0"),
    ("ENABLE_TOOL_SEARCH", "false"),
    ("CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS", "1"),
)
# Where Claude Code reads the administrator's managed settings (macOS, then Linux), which no setting source or
# disableAllHooks turns off (verified against 2.1.328: a hook in them ran under --setting-sources "" and
# disableAllHooks).
MANAGED_SETTINGS = (Path("/Library/Application Support/ClaudeCode/managed-settings.json"),
                    Path("/etc/claude-code/managed-settings.json"))
# Anthropic's own API: the endpoint Claude Code uses without a base URL.
ANTHROPIC_HOSTS = frozenset({"api.anthropic.com"})


def _switched_on(value: str | None) -> bool:
    """Whether a Claude Code backend switch (``CLAUDE_CODE_USE_BEDROCK=1``) is on; unset, empty, 0 or false is off."""
    return value is not None and value.strip().lower() not in ("", "0", "false", "no", "off")


def official_endpoint(base_url: str) -> bool:
    """Whether ``base_url`` is Anthropic's own API: none given, or its host."""
    return not base_url or (urlsplit(base_url).hostname or "").lower() in ANTHROPIC_HOSTS


def _prompt_input(launch: Launch, *, prompt_file: Path | None = None,
                  looper_prompt: str = "") -> tuple[list[str], list[PromptBlock]]:
    """Claude reads the supplied file verbatim; the flag also selects its fixed main-block prefix."""
    args = claude_cli.system_prompt_args(prompt_file or Path("prompt.md"), launch.system_prompt_mode)
    blocks = [
        PromptBlock("Billing header", "runtime", None, "billing header (fixed)"),
        PromptBlock("Identity", "runtime", None, "identity line (fixed; changes with append vs replace)"),
    ]
    if args[0] == "--append-system-prompt-file":
        blocks.append(PromptBlock("Default prompt", "runtime", None, "Claude Code's default prompt (not shown)"))
    return args, [*blocks, PromptBlock("Looper instructions", "looper", looper_prompt,
                                       "main system prompt block")]


class ClaudeCodeRuntime(RuntimeAdapter):
    first_event_known = True
    can_replace_system_prompt = True
    type = "claude-code"
    label = "Claude Code"
    streams_usage = True
    reports_request_context = True
    default_executable = "claude"
    image_input = ImageInput.READ_TOOL
    # A PreToolUse hook in --settings, verified against 2.1.329 (the outbound-review RFC, section 17.1): it runs for
    # the built-in tools, MCP tools and subagents' calls, and a JSON deny stops the call, its reason shown to the model.
    tool_hook = True
    # Verified against Claude Code 2.1.285 (claude_cli), no_tools against 2.1.328 (claude_cli.NO_TOOLS_ARGS); its
    # replies follow Looper's contracts by prompt, so no structured_output. Headless /compact on resume
    # was verified with 2.1.329: a manual compact_boundary and a later turn recalling the summary.
    # MID_TURN_INPUT verified on 2026-11-20 with stream-json and --replay-user-messages:
    # queued user input joins the same turn after its current tool returns, with one result.
    # Only non-hosted ResidentClaude was verified; wiring must check the session kind.
    # Keeper- and runner-hosted sessions are unverified.
    capabilities = frozenset(set(Capability) - {Capability.STRUCTURED_OUTPUT, Capability.READ_TOOLS, Capability.STEER})
    # Verified against Claude Code 2.1.329: /compact and an automatic compaction in the resident stream-json session
    # each write a compact_boundary event before the turn's result.
    resident_compaction_events = True
    launch_modes = frozenset({LaunchMode.PLATFORM_TOOLS, LaunchMode.FULL})
    room_push = RoomPush.USER_TURN
    run_input = True  # stdin can stay open between a Worker's waiting turns
    keepable = True
    can_interrupt = True  # its resident session's control request ``interrupt``
    protocols = frozenset({ApiProtocol.ANTHROPIC_MESSAGES})
    translated_protocols = frozenset({ApiProtocol.OPENAI_CHAT})
    # Its own login (the Claude subscription), and an API key for Anthropic's API or an Anthropic-format endpoint.
    auth_kinds = frozenset({AuthKind.OAUTH, AuthKind.API_KEY})
    isolation_id = claude_cli.ISOLATION_ID
    provider_env_owned = frozenset({BASE_URL, API_KEY, AUTH_TOKEN, "ANTHROPIC_MODEL", *MAIN_MODEL_VARS,
                                    *BACKGROUND_MODEL_VARS})
    # A read-only run refuses what would change files and reports each refusal (claude_cli.read_only_args).
    read_only = ReadOnly.PERMISSIONS

    def is_model_event(self, event: Mapping[str, Any]) -> bool:
        if event.get("type") == "system" and event.get("subtype") == "init":
            # init normally just echoes the requested model, before the API is called.
            from ..run_usage import request_tokens

            return bool(self.reported_model(event)) and any(request_tokens(event.get("usage")).to_dict().values())
        if event.get("type") == "stream_event":
            raw = event.get("event") or {}
            if raw.get("type") == "message_start":
                return super().is_model_event({"type": "assistant", "message": raw.get("message")})
            return raw.get("type") in {"content_block_start", "content_block_delta"}
        return super().is_model_event(event)

    def reported_model(self, event: Mapping[str, Any]) -> str | None:
        if event.get("type") == "system" and event.get("subtype") == "init":
            model = event.get("model")
        elif (event.get("type") == "assistant" and not event.get("parent_tool_use_id")
              and isinstance(event.get("message"), Mapping)):
            model = event["message"].get("model")
        else:
            return None
        return model if isinstance(model, str) and model.strip() and model != "<synthetic>" else None

    def api_model(self, model: str) -> str:
        from ..model_names import parse_model_name

        return parse_model_name(model).base  # Claude Code reads the context suffix itself

    def probe(self, executable: str) -> Probe:
        """``<executable> --version``: Claude Code prints its version and exits without calling a model."""
        try:
            done = subprocess.run([executable, "--version"], capture_output=True, text=True,
                                  timeout=_PROBE_TIMEOUT_SEC, check=False, env=child_env.inherited())
        except FileNotFoundError:
            return Probe(None, self.capabilities, f"{executable!r} was not found")
        except (OSError, subprocess.TimeoutExpired) as exc:
            return Probe(None, self.capabilities, f"{executable} --version did not run: {type(exc).__name__}: {exc}")
        first = next((line.strip() for line in (done.stdout or done.stderr).splitlines() if line.strip()), "")
        if done.returncode != 0 or not first:
            return Probe(None, self.capabilities, f"{executable} --version failed (exit code {done.returncode})"
                                                  + (f": {first}" if first else ""))
        return Probe(first[:200], self.capabilities_for(first))

    def isolation_args(self, cwd: str | Path, *, launch: Launch | None = None,
                       settings_dir: Path | None = None) -> list[str]:
        from ..llmrec import LlmrecRoute, RouteKind
        from ..llmrec.gateway import translated

        # Project settings load after process env; pin the attached route and all translation safeguards here too.
        sealed = launch is not None and (launch.no_tools or launch.explore or launch.platform_tools_only)
        env = None
        if launch is not None and translated(launch):
            env = {**dict(TRANSLATED_ENV), AUTH_TOKEN: "", API_KEY: ""}
            binding = self.bind_provider(launch.profile.provider, launch.model, launch.effort)
            process_env = dict((*launch.env, *binding.env))
            env.update({name: process_env[name] for name in ("ANTHROPIC_MODEL", *MAIN_MODEL_VARS,
                                                            *BACKGROUND_MODEL_VARS)})
            # Commands are built before Runs records; environment() refuses missing routes at process startup.
            if isinstance(launch.llmrec, LlmrecRoute) and launch.llmrec.kind is RouteKind.TRANSLATE:
                env[BASE_URL] = launch.llmrec.url
                env[AUTH_TOKEN] = launch.llmrec.local_token
        hook = launch.tool_hook if launch is not None else None
        return claude_cli.isolation_args(cwd, env=env, settings_dir=settings_dir, sealed=sealed,
                                         hooks=hook.settings() if hook is not None else None)

    def check_pairing(self, provider: ProviderSpec, model: str, effort: str | None = None, *,
                      executable: str | None = None, launcher: Launcher = Launcher.NATIVE) -> None:
        if provider.protocols == self.translated_protocols and not provider.base_url.strip():
            raise RuntimeUnsupported("Cannot prepare Claude Code: the model's OpenAI endpoint has no base URL")

    def bind_provider(self, provider: ProviderSpec, model: str, effort: str) -> ProviderBinding:
        """What points a run at ``provider`` (the module's notes), followed by the provider's own variables.

        Claude Code's own login (the ``claude-subscription`` provider) needs
        nothing beside ``--model`` and ``--effort``: it is how every agent ran
        before providers. An API key goes in ``ANTHROPIC_API_KEY`` for
        Anthropic's own API and in ``ANTHROPIC_AUTH_TOKEN`` with the
        ``base_url`` of any other endpoint, whose binding also maps every Claude
        model alias to the provider's models.
        """
        if provider.protocols == self.translated_protocols:
            return self._translated_binding(provider, model)
        if provider.protocols != self.protocols:
            raise RuntimeUnsupported(f"Claude Code speaks anthropic-messages; provider {provider.name!r} "
                                     "must supply that exact protocol")
        if len(provider.catalog) != 1 or provider.catalog[0].name != model:
            raise RuntimeUnsupported("Claude Code requires one resolved model matching the launch model")
        entry, = provider.catalog
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
        background = entry.background_model or model
        return ProviderBinding(
            env=((BASE_URL, provider.base_url), (AUTH_TOKEN, provider.api_key),
                 *((name, model) for name in MAIN_MODEL_VARS), *((name, background) for name in BACKGROUND_MODEL_VARS),
                 *own),
            unset=(API_KEY,))

    def _translated_binding(self, provider: ProviderSpec, model: str) -> ProviderBinding:
        self.check_pairing(provider, model)
        if provider.auth is not AuthKind.API_KEY or not provider.api_key:
            raise RuntimeUnsupported("Claude Code translated models require a provider API key")
        if len(provider.catalog) != 1 or provider.catalog[0].name != model:
            raise RuntimeUnsupported("Claude Code requires one resolved model matching the launch model")
        background = provider.catalog[0].background_model or model
        return ProviderBinding(
            env=(("ANTHROPIC_MODEL", model),
                 *((name, model) for name in MAIN_MODEL_VARS),
                 *((name, background) for name in BACKGROUND_MODEL_VARS), *provider.env),
            unset=(API_KEY, BASE_URL))

    def launch_args(self, launch: Launch, *, cwd: str | Path | None = None) -> list[str]:
        """The Claude Code flags every agent starts with: the permissions (read-only for a run that must not
        change files, ``Launch.read_only``; only reading tools for a consult) with the tools that reach the
        operator's other Claude Code sessions denied (``claude_cli.peer_args``), the model, the effort, the plugin
        directories of the installed items, the directories of the project knowledge, the per-run spend cap (when
        Claude Code's own figure is the run's cost), the provider's binding and, with agent teams
        (``Launch.agent_teams``), where teammates run. A run with no tools (``Launch.no_tools``) has none of the
        permissions, items, knowledge or teammates: ``claude_cli.NO_TOOLS_ARGS`` with its model, effort, cap and
        binding."""
        from ..claude_cli import (
            NO_TOOLS_ARGS, READ_ONLY_MODE, budget_args, consult_args, knowledge_args, library_args, peer_args,
            permission_args, read_only_args,
        )

        provider = launch.profile.provider
        budget = budget_args(launch.max_budget_usd) if self.cost_source(provider) is CostSource.RUNTIME else []
        if launch.no_tools:
            return [*NO_TOOLS_ARGS, "--model", launch.model, *(["--effort", launch.effort] if launch.effort else []),
                    *budget, *self.bind_provider(provider, launch.model, launch.effort).args]
        if launch.platform_tools_only:
            return ["--tools", "", "--disable-slash-commands", *READ_ONLY_MODE,
                    "--allowedTools", "mcp__looper__*", "--model", launch.model,
                    *(["--effort", launch.effort] if launch.effort else []),
                    *(["--mcp-config", launch.library.mcp_config] if launch.library.mcp_config else []),
                    *budget, *self.bind_provider(provider, launch.model, launch.effort).args]
        return [
            *([*consult_args(), "--disable-slash-commands"] if launch.explore else
              consult_args() if launch.consult else
              read_only_args(launch.library, cwd=cwd if cwd is not None else Path.cwd(),
                             add_dirs=(*launch.knowledge.add_dirs, *launch.read_dirs))
              if launch.read_only else permission_args()),
            *peer_args(launch.agent_teams),
            "--model", launch.model,
            *(["--effort", launch.effort] if launch.effort else []),
            *library_args(launch.library),
            *knowledge_args(launch.knowledge),
            *budget,
            *self.bind_provider(provider, launch.model, launch.effort).args,
            *(TEAMMATE_MODE_ARGS if launch.agent_teams else ()),
        ]

    def environment(self, launch: Launch) -> dict[str, str]:
        """The subprocess environment: ``child_env.inherited()``, then the settings' variables and the provider's
        binding, less what the binding removes, with the agent teams variable set by the launch alone
        (``Launch.agent_teams``)."""
        binding = self.bind_provider(launch.profile.provider, launch.model, launch.effort)
        added = {k: v for k, v in (*launch.env, *binding.env) if k != AGENT_TEAMS}
        if launch.agent_teams:
            added[AGENT_TEAMS] = "1"
        env = {**{k: v for k, v in child_env.inherited().items() if k not in binding.unset and k != AGENT_TEAMS}, **added}
        from ..llmrec import LlmrecRoute, RouteKind, no_proxy
        from ..llmrec.gateway import gateway_refusal, translated

        if translated(launch):
            route = launch.llmrec
            if not (isinstance(route, LlmrecRoute) and route.kind is RouteKind.TRANSLATE and route.local_token):
                raise RuntimeUnsupported(gateway_refusal(route, launch.model))
            # Settings cannot reintroduce the direct Anthropic credential or override gateway safeguards.
            env.pop(API_KEY, None)
            env[BASE_URL] = route.url
            env[AUTH_TOKEN] = route.local_token
            env.update(TRANSLATED_ENV)
            no_proxy(env)
            return env
        if isinstance(launch.llmrec, LlmrecRoute):
            env[BASE_URL] = launch.llmrec.url
            # The loopback endpoint hides first-party tool search support from Claude Code.
            if official_endpoint(launch.llmrec.upstream):
                env.setdefault("ENABLE_TOOL_SEARCH", "true")
            no_proxy(env)
        return env

    def loaded_sources(self, launch: Launch) -> LoadedSources:
        """Every plugin directory and every server of the ``--mcp-config`` file (``claude_cli.library_args``)."""
        return LoadedSources(tuple(sorted(library_mcp_servers(launch.library))),
                             plugin_skills(launch.library.plugin_dirs))

    def web_tools(self, launch: Launch) -> bool:
        """WebSearch is a tool of Anthropic's own API: only a launch whose model requests go there has it. Decided
        from the final launch environment: a backend switch it keeps on (Bedrock, Vertex, Foundry and the others of
        ``TRANSLATED_BACKEND_ENV``, from the settings or inherited) takes it away, and so does a compatible or
        translated endpoint."""
        if launch.explore:
            return False
        try:
            environment = self.environment(launch)
        except RuntimeUnsupported:  # a launch that cannot run says so where it starts; it has no web here
            return False
        if any(_switched_on(environment.get(name)) for name, _ in TRANSLATED_BACKEND_ENV):
            return False
        upstream = self.llmrec_upstream(launch)
        return upstream is not None and official_endpoint(upstream)

    def llmrec_upstream(self, launch: Launch) -> str | None:
        from . import Launcher

        if launch.profile.runtime.launcher is Launcher.TTADK:
            return None
        from ..llmrec.gateway import translated

        if translated(launch):
            self.check_pairing(launch.profile.provider, launch.model)
            return launch.profile.provider.base_url
        return self.environment(replace(launch, llmrec=None)).get(BASE_URL) or "https://api.anthropic.com"

    def prompt_blocks(self, launch: Launch, looper_prompt: str, *, cwd: str | Path | None = None) -> list[PromptBlock]:
        return _prompt_input(launch, looper_prompt=looper_prompt)[1]

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
            *self.isolation_args(cwd, launch=launch, settings_dir=prompt_file.parent / "claude-settings"),
            *_prompt_input(launch, prompt_file=prompt_file)[0],
            *self.launch_args(launch, cwd=cwd),
            *(["--max-turns", str(max_turns)] if max_turns > 0 else []),
        ]

    def operator_resident_command(self, executable: str, *, cwd: str | Path, prompt_file: Path,
                                  launch: Launch, settings_dir: Path) -> list[str]:
        """A task Manager's direct session keeps the operator's settings, memory, skills and MCP servers."""
        from ..claude_cli import budget_args, operator_settings_args, peer_args, permission_args, system_prompt_args

        provider = launch.profile.provider
        hook = launch.tool_hook
        return [
            executable, "--print", "--verbose", "--output-format", "stream-json",
            "--input-format", "stream-json", "--include-partial-messages", "--replay-user-messages",
            *operator_settings_args(settings_dir, hook.settings() if hook is not None else None),
            *system_prompt_args(prompt_file), *permission_args(), *peer_args(False),
            "--model", launch.model, *(["--effort", launch.effort] if launch.effort else []),
            *(["--mcp-config", launch.library.mcp_config] if launch.library.mcp_config else []),
            *(budget_args(launch.max_budget_usd) if self.cost_source(provider) is CostSource.RUNTIME else []),
            *self.bind_provider(provider, launch.model, launch.effort).args,
        ]

    def resume_args(self, session_id: str) -> list[str]:
        return ["--resume", session_id]

    def run_command(self, executable: str, *, cwd: str | Path, prompt_file: Path, launch: Launch,
                    max_turns: int = 0, session_dir: Path | None = None) -> list[str]:
        # Claude Code keeps every run's session (in its own configuration directory): ``session_dir`` is not used.
        return [
            executable,
            "--print",
            "--verbose",
            "--output-format", "stream-json",
            # Partial deltas keep the run's heartbeat current while Claude thinks.
            "--include-partial-messages",
            # The prompt and any later user turns arrive on stdin, so the
            # Planner can step in while the run is going. A run with no tools gets other people's messages as its
            # prompt: they are not echoed back into its stream (and its record).
            "--input-format", "stream-json",
            *([] if launch.no_tools else ["--replay-user-messages"]),
            # None of the operator's personal settings, memory, hooks, plugins
            # or MCP servers (whose teardown could also make the CLI exit
            # non-zero). Built-in tools are unaffected; the permissions every
            # agent shares and the plugins and skills installed for this run
            # come with the launch args.
            *self.isolation_args(cwd, launch=launch, settings_dir=prompt_file.parent / "claude-settings"),
            *_prompt_input(launch, prompt_file=prompt_file)[0],
            *self.launch_args(launch, cwd=cwd),
            *(["--max-turns", str(max_turns)] if max_turns > 0 else []),
        ]

    def consult_command(self, executable: str, *, cwd: str | Path, prompt_file: Path, launch: Launch,
                        session_id: str, session_dir: Path | None, fork_dir: Path, max_turns: int = 0) -> list[str]:
        """A run that resumes a fork of a finished run's session (``--resume <id> --fork-session``: a new session id,
        the original unchanged; it resumes from another directory too, verified with 2.1.285), with a ``read_only``
        launch with only reading tools. Claude Code keeps its sessions itself, ignoring ``session_dir`` and
        ``fork_dir``: the fork stays in its own session store like every Claude Code session, since Looper never
        changes anything in Claude Code's own directory."""
        return [*self.run_command(executable, cwd=cwd, prompt_file=prompt_file, launch=launch, max_turns=max_turns),
                "--resume", session_id, "--fork-session"]

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

    def instruction_files(self, cwd: str | Path) -> tuple[InstructionFile, ...]:
        """Every ``CLAUDE.md`` of the project, else every ``AGENTS.md``: Claude Code reads them itself (``claude_cli``'s
        notes)."""
        own = instructions.named(cwd, instructions.CLAUDE_MD)
        return tuple(map(InstructionFile, own or instructions.named(cwd, instructions.AGENTS_MD)))

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

    def failure_detail(self, result_event: dict[str, Any] | None, *, launch: Launch,
                       command: list[str], pid: int | None = None) -> str | None:
        from .omp import provider_error, safe_error

        if not result_event or (not result_event.get("is_error") and result_event.get("subtype") == "success"):
            return None
        text = result_event.get("result")
        status = result_event.get("status_code", result_event.get("status"))
        detail = provider_error(text if isinstance(text, str) else "", status)
        # Redact the complete diagnostic before whitespace normalization and clipping.
        detail = safe_error(detail, secrets=(launch.profile.provider.api_key or "",), limit=None)
        return " ".join(detail.split())[:300] or None

    def failure_signal(self, result_event: dict[str, Any] | None, evidence: Evidence) -> Failure | None:
        from .. import failures

        return failures.claude_code(result_event, evidence)

    def managed_hooks(self) -> ManagedHooks | None:
        """The hook events of the first managed settings file that sets any (``MANAGED_SETTINGS``); one that cannot
        be read counts as setting hooks it cannot name."""
        for path in MANAGED_SETTINGS:
            try:
                hooks = json.loads(path.read_text(encoding="utf-8")).get("hooks")
            except FileNotFoundError:
                continue
            except (OSError, ValueError, AttributeError):
                return ManagedHooks(str(path), (), readable=False)
            if not isinstance(hooks, dict):
                if hooks is not None:
                    return ManagedHooks(str(path), (), readable=False)
                continue
            if events := tuple(sorted(name for name, matchers in hooks.items() if matchers)):
                return ManagedHooks(str(path), events)
        return None

    def permission_denials(self, result_event: dict[str, Any] | None) -> list[dict[str, str]]:
        return claude_cli.permission_denials(result_event)
