"""The Codex runtime: the ``codex`` CLI's ``exec --json`` mode, one process per turn.

Verified against Codex CLI 0.162.0 (the multi-runtime RFC, section 6):

- **A run** is ``codex exec --json ... -``: the prompt comes on stdin, read to
  its end, and the process runs one turn and exits. It cannot take another
  user turn while it runs, so a Worker cannot be steered (``write_turn``
  closes stdin after the prompt).
- **A resident session** (the Manager, a Planner) is one such process per
  turn, resuming the session: ``codex exec ... resume <thread id> -``
  (``ResidentCodex``). ``thread.started`` names the thread on every turn; a
  thread that is gone makes Codex exit 1 without a single event, which the
  tiers already read as a session that could not be resumed.
- **Isolation**: every process gets a ``CODEX_HOME`` of Looper's own under
  ``<runtime_root>/.local/codex``, holding only the ``config.toml`` and the
  ``AGENTS.md`` Looper writes, so the operator's ``~/.codex`` is never read.
  The home is named on the command line (``env CODEX_HOME=<home> codex ...``)
  because what it holds depends on the run's system prompt. The config also
  turns off Codex's own skill list, which always includes
  ``$HOME/.agents/skills`` (the operator's personal skills), and its plugin
  sync, which clones a plugin marketplace into every new home.
- **The project's instruction files**: Codex reads a project's ``AGENTS.md``
  files, and its ``CLAUDE.md`` in a directory without one
  (``project_doc_fallback_filenames``, ``FALLBACK_NAMES``; ``instructions``).
- **The system prompt** is the home's ``AGENTS.md``: Codex puts it in the
  conversation as a user message, before the project's own ``AGENTS.md``,
  and when it changes between turns of a session it sends the new text,
  saying it replaces the old one. A short block of runtime notes comes first
  (``runtime_notes``); Looper's prompts themselves stay runtime-neutral.
- **The provider** is ``model_provider`` and ``[model_providers.looper]`` in
  the config (``wire_api = "responses"``); its key is only in the
  environment variable the config names (``KEY_ENV``), which the agent's
  shell commands do not inherit (``shell_environment_policy.exclude``).
- **Codex's own login** (ChatGPT, an OAuth provider; RFC P1,
  ``codex_login``): the config sets no ``model_provider`` (Codex's built-in
  OpenAI one) and ``cli_auth_credentials_store = "file"``, the home's
  ``auth.json`` is a link to the operator's login file, never a copy, and
  ``CODEX_API_KEY`` and ``CODEX_ACCESS_TOKEN`` are removed from the
  environment. The agent's shell commands get a ``CODEX_HOME`` without the
  link (``shell_environment_policy.set``), and every home is inspected after
  the run for a forked login (``after_run``). ChatGPT's apps are off.
- **The catalog** (RFC P9): MCP servers become ``[mcp_servers.<name>]``
  (stdio and streamable HTTP; Codex has no SSE). Skills are mounted
  read-only like project knowledge (``--add-dir``) and listed in the runtime
  notes for the agent to read; a plugin's hooks, commands, agents and MCP
  file are left out. What was left out is in the run's record
  (``launch_record``).
- **Events and usage**: ``CodexStream`` turns ``exec --json`` events into
  Looper's internal events (Claude Code's stream-json shapes). Codex reports
  tokens only, as the session's running totals, at the end of a turn; it
  reports no compaction and runs no turn of its own.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import time
import tomllib
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

from ..run_usage import Usage, reported_totals, run_usage
from ..system_prompt_mode import SystemPromptMode
from . import (
    ApiProtocol, AuthKind, Capability, Probe, ProviderBinding, RunCost, RuntimeAdapter, RuntimeUnsupported,
    codex_login, instructions,
)
from .instructions import InstructionFile
from .per_turn import PerTurnSession, session_dir_name
from .pricing import usage_event

if TYPE_CHECKING:
    from ..profile_registry import ProviderSpec
    from ..agent_settings import Launch
    from ..library import AgentLibrary

_PROBE_TIMEOUT_SEC = 10.0
# The provider's id in the generated config, and the variable that holds its key.
PROVIDER_ID = "looper"
KEY_ENV = "LOOPER_PROVIDER_API_KEY"
# Where an API key provider without a base_url sends its requests: OpenAI's own endpoint.
DEFAULT_BASE_URL = "https://api.openai.com/v1"
# Names the layout of a generated home in each run's record; change it when the layout changes.
HOME_LAYOUT_ID = "codex-home/v1"
# What Codex reads in a directory without AGENTS.md (project_doc_fallback_filenames): a project's CLAUDE.md.
FALLBACK_NAMES = (instructions.CLAUDE_MD,)
# The parts of a Claude Code plugin Codex cannot load.
_PLUGIN_PARTS_LEFT_OUT = ("commands", "agents", "hooks", ".mcp.json", "output-styles")
_DESCRIPTION_MAX_CHARS = 300
# How long a run home may go unwritten before it is removed (``_remove_idle_homes``): far beyond any run.
RUN_HOME_IDLE_SEC = 24 * 3600
_HASHED = re.compile(r"[0-9a-f]{64}")


class CodexRuntime(RuntimeAdapter):
    can_replace_system_prompt = True
    type = "codex"
    label = "Codex"
    default_executable = "codex"
    # Verified against Codex CLI 0.162.0: --output-schema holds on resumed turns too (Looper keeps its replies'
    # contracts in the prompt all the same). No compaction events, no turns of its own, tokens only.
    capabilities = frozenset({Capability.RUN, Capability.RESIDENT_SESSION, Capability.RESUME,
                              Capability.STRUCTURED_OUTPUT, Capability.MCP, Capability.ADD_DIRS})
    protocols = frozenset({ApiProtocol.OPENAI_RESPONSES})
    # An API key provider, or Codex's own login (RFC P1, ``codex_login``).
    auth_kinds = frozenset({AuthKind.API_KEY, AuthKind.OAUTH})
    login_models = codex_login.SUGGESTED_MODELS
    isolation_id = HOME_LAYOUT_ID
    # Set by the binding and the run's home: a provider's own variables cannot set them.
    provider_env_owned: ClassVar[frozenset[str]] = frozenset({KEY_ENV, "CODEX_HOME"})

    def probe(self, executable: str) -> Probe:
        """``<executable> --version``, with an empty ``CODEX_HOME`` of its own so nothing of the operator's is read."""
        home = tempfile.mkdtemp(prefix="looper-codex-probe-")
        try:
            done = subprocess.run([executable, "--version"], capture_output=True, text=True,
                                  timeout=_PROBE_TIMEOUT_SEC, check=False, env={**os.environ, "CODEX_HOME": home})
        except FileNotFoundError:
            return Probe(None, self.capabilities, f"{executable!r} was not found")
        except (OSError, subprocess.TimeoutExpired) as exc:
            return Probe(None, self.capabilities, f"{executable} --version did not run: {type(exc).__name__}: {exc}")
        finally:
            shutil.rmtree(home, ignore_errors=True)
        first = next((line.strip() for line in (done.stdout or done.stderr).splitlines() if line.strip()), "")
        if done.returncode != 0 or not first:
            return Probe(None, self.capabilities, f"{executable} --version failed (exit code {done.returncode})"
                                                  + (f": {first}" if first else ""))
        return Probe(first[:200], self.capabilities_for(first))

    def isolation_args(self, cwd: str | Path) -> list[str]:
        """None: Codex's isolation is its own ``CODEX_HOME`` and the config Looper writes there (``config_toml``)."""
        return []

    def bind_provider(self, provider: ProviderSpec, model: str, effort: str) -> ProviderBinding:
        """The model and the effort as flags; the provider's own variables, then its key in ``KEY_ENV`` (Codex
        sends it as a bearer token). The provider's endpoint is in the generated config (``config_toml``). With
        Codex's own login there is no key: what would replace the login is removed from the environment."""
        if ApiProtocol.OPENAI_RESPONSES not in provider.protocols:
            raise RuntimeUnsupported(f"Codex speaks openai-responses; provider {provider.name!r} does not serve it")
        args = ("-m", model, *(("-c", f"model_reasoning_effort={_toml_value(effort)}") if effort else ()))
        if provider.auth is AuthKind.OAUTH:
            if provider.oauth_runtime_type != self.type:
                raise RuntimeUnsupported(f"provider {provider.name!r} is the login of {provider.oauth_runtime_type} "
                                         "runtimes, not of Codex")
            return ProviderBinding(args=args, env=provider.env, unset=codex_login.ENV_UNSET)
        if not provider.api_key:
            raise RuntimeUnsupported(f"provider {provider.name!r} has no API key: set one in Settings")
        return ProviderBinding(args=args, env=(*provider.env, (KEY_ENV, provider.api_key)))

    def launch_args(self, launch: Launch) -> list[str]:
        """The ``exec`` flags of every run: JSON events, no git requirement, no approvals or sandbox (RFC
        decision 8), the model and the effort, and the knowledge and skill directories."""
        binding = self.bind_provider(launch.profile.provider, launch.model, launch.effort)
        mounts = [*launch.knowledge.add_dirs, *materialize(launch.library).skill_dirs]
        return [
            "--json", "--skip-git-repo-check", "--dangerously-bypass-approvals-and-sandbox",
            *binding.args,
            *(arg for directory in dict.fromkeys(mounts) for arg in ("--add-dir", directory)),
        ]

    def environment(self, launch: Launch) -> dict[str, str]:
        """The server's environment, then the settings' variables and the provider's binding (its own variables
        and its key)."""
        binding = self.bind_provider(launch.profile.provider, launch.model, launch.effort)
        return {**{k: v for k, v in os.environ.items() if k not in binding.unset}, **dict(launch.env),
                **dict(binding.env)}

    def _command(self, executable: str, prompt_file: Path, launch: Launch) -> list[str]:
        local = _local_dir(prompt_file)
        login = codex_login.login_file() if launch.profile.provider.auth is AuthKind.OAUTH else None
        agents = agents_md(prompt_file, launch)
        instructions = None
        if launch.system_prompt_mode is SystemPromptMode.REPLACE:
            instructions = write_instructions(local, agents)
            agents = ""
        config = config_toml(launch, shell_home=_shell_home(local) if login else None,
                             model_instructions_file=instructions)
        home = write_home(local, config, agents, login=login)
        return ["env", f"CODEX_HOME={home}", executable, "exec", *self.launch_args(launch)]

    def resident_command(self, executable: str, *, cwd: str | Path, prompt_file: Path, launch: Launch,
                         max_turns: int = 0) -> list[str]:
        """The command of the turns of a resident session, without the session to resume and the prompt
        (``ResidentCodex`` adds both). Codex has no cap on a turn's model calls, so ``max_turns`` is unused."""
        return self._command(executable, prompt_file, launch)

    def resume_args(self, session_id: str) -> list[str]:
        return ["resume", session_id]

    def run_command(self, executable: str, *, cwd: str | Path, prompt_file: Path, launch: Launch,
                    max_turns: int = 0) -> list[str]:
        """One run: no session kept (``--ephemeral``), the prompt read from stdin (``-``)."""
        return [*self._command(executable, prompt_file, launch), "--ephemeral", "-"]

    def resident_session(self, label: str) -> ResidentCodex:
        return ResidentCodex(label=label)

    async def write_turn(self, stdin: asyncio.StreamWriter, text: str, *, uuid: str | None = None) -> None:
        """Write a run's prompt and close stdin, which is how Codex knows the prompt ended. There is no second
        user turn: writing one raises ``RuntimeError``."""
        if stdin.is_closing():
            raise RuntimeError("Codex reads one prompt per run: it takes no user turn while it runs")
        stdin.write(text.encode("utf-8"))
        await stdin.drain()
        stdin.close()

    def parse(self, stdout: asyncio.StreamReader, *, model: str | None = None) -> AsyncIterator[dict[str, Any]]:
        """A run's events; Codex does not name the model, so the run's (``model``) names its tokens."""
        return CodexStream(model=model).events(stdout)

    def tool_summary(self, tool_name: str, tool_input: dict[str, Any]) -> str:
        if tool_name == SHELL_TOOL:
            return f"Running: {shell_command(str(tool_input.get('command') or ''))[:80]}"
        if tool_name == PATCH_TOOL:
            paths = [str(c.get("path") or "?") for c in tool_input.get("changes") or [] if isinstance(c, dict)]
            return f"Editing: {', '.join(paths)[:80] or '?'}"
        if tool_name == SEARCH_TOOL:
            return f"Searching: {str(tool_input.get('query') or '')[:60]}"
        if tool_name == PLAN_TOOL:
            return "Updating its plan"
        return f"Tool: {tool_name}"

    def cost(self, usage: Usage, provider: ProviderSpec) -> RunCost:
        """Codex reports tokens only: the run's ``usage`` as priced from its ``modelUsage`` at the provider's prices
        (``cost_source``, ``Launch.price``)."""
        return RunCost(usage, self.cost_source(provider))

    def budget_error(self, result_event: dict[str, Any] | None) -> str | None:
        """Why Looper stopped the run at its per-run soft cap; Codex has no spend cap of its own."""
        from .. import claude_cli

        return claude_cli.budget_error(result_event) if (result_event or {}).get(claude_cli.SOFT_CAP) else None

    async def login_problem(self, provider: ProviderSpec) -> str | None:
        """Why a run with Codex's own login cannot start now (``codex_login.launch_problem``)."""
        return await codex_login.launch_problem() if provider.auth is AuthKind.OAUTH else None

    async def after_run(self, launch: Launch, command: list[str]) -> list[str]:
        """After a run with Codex's own login: every home under the run's ``codex`` directory whose login link is
        not intact, its copy removed and the problem recorded (``codex_login.inspect_homes``)."""
        if launch.profile.provider.auth is not AuthKind.OAUTH:
            return []
        home = next((arg.partition("=")[2] for arg in command[:2] if arg.startswith("CODEX_HOME=")), "")
        if not home:
            return []
        problems = await asyncio.to_thread(codex_login.inspect_homes, Path(home).parent.parent,
                                           codex_login.login_file())
        return [await codex_login.record_problems(problems)] if problems else []

    def instruction_files(self, cwd: str | Path) -> tuple[InstructionFile, ...]:
        """One file a directory of the project, ``AGENTS.md`` else ``CLAUDE.md`` (``FALLBACK_NAMES``), which Codex
        reads itself."""
        names = (instructions.AGENTS_MD, *FALLBACK_NAMES)
        return tuple(map(InstructionFile, instructions.first_per_directory(cwd, names)))

    def launch_record(self, launch: Launch) -> dict[str, Any]:
        """The skills given as documents and what Codex could not load, when there are any (RFC P9)."""
        catalog = materialize(launch.library)
        skipped = [*catalog.skipped, *mcp_servers(launch.library)[1]]
        if not catalog.skills and not skipped:
            return {}
        return {"runtime_catalog": {"runtime": self.type,
                                    "skills_as_documents": [skill.name for skill in catalog.skills],
                                    "skipped": skipped}}


# --- What a run's home holds ---


def _local_dir(prompt_file: Path) -> Path:
    """``<runtime_root>/.local``: the prompt file is ``<runtime_root>/.local/prompts/<sha256>.md``
    (``SystemPrompt.write_file``)."""
    return Path(prompt_file).absolute().parent.parent


def _toml_value(value: Any) -> str:
    """``value`` (a string, number, boolean, list or mapping) in TOML; a mapping as an inline table."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return json.dumps(value)
    if isinstance(value, str):
        return json.dumps(value)  # escaped to ASCII: a valid TOML basic string
    if isinstance(value, Mapping):
        return "{ " + ", ".join(f"{_toml_key(str(k))} = {_toml_value(v)}" for k, v in value.items()) + " }" \
            if value else "{}"
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_toml_value(v) for v in value) + "]"
    raise TypeError(f"no TOML for {type(value).__name__}")


def _toml_key(key: str) -> str:
    return key if re.fullmatch(r"[A-Za-z0-9_-]+", key) else json.dumps(key)


def mcp_servers(library: AgentLibrary) -> tuple[list[tuple[str, dict[str, Any]]], list[str]]:
    """The run's MCP servers as Codex's ``[mcp_servers.<name>]`` tables, and the ones Codex cannot reach.

    They come from the run's ``--mcp-config`` file (``mcp.write_config``), the
    servers in Claude Code's shape: stdio servers keep their command, arguments
    and environment, HTTP servers their URL and headers. Codex has no SSE
    transport, so an SSE server is left out.
    """
    if not library.mcp_config:
        return [], []
    servers = json.loads(Path(library.mcp_config).read_text(encoding="utf-8")).get("mcpServers") or {}
    tables: list[tuple[str, dict[str, Any]]] = []
    skipped: list[str] = []
    for name, server in sorted(servers.items()):
        transport = server.get("type") or ("stdio" if server.get("command") else "")
        if transport == "stdio":
            table: dict[str, Any] = {"command": server["command"], "args": list(server.get("args") or [])}
            if server.get("env"):
                table["env"] = dict(server["env"])
        elif transport == "http":
            table = {"url": server["url"]}
            if server.get("headers"):
                table["http_headers"] = dict(server["headers"])
        else:
            skipped.append(f"MCP server {name}: Codex has no {transport or 'unknown'} transport")
            continue
        tables.append((name, table))
    return tables, skipped


def config_toml(launch: Launch, *, shell_home: Path | None = None,
                model_instructions_file: Path | None = None) -> str:
    """The ``config.toml`` of a run's home: isolation, the project's CLAUDE.md where it has no AGENTS.md, the
    provider, and the MCP servers. With Codex's own login (``shell_home``, the ``CODEX_HOME`` the agent's shell
    commands get), see ``_login_config``."""
    if launch.profile.provider.auth is AuthKind.OAUTH:
        assert shell_home is not None
        lines = _login_config(shell_home)
        if model_instructions_file is not None:
            lines.insert(lines.index(codex_login.CONFIG_MARKER) + 1,
                         f"model_instructions_file = {_toml_value(str(model_instructions_file))}")
        return _with_mcp(lines, launch)
    provider = launch.profile.provider
    lines = [
        "# Written by Looper for its Codex runs. Only this file and AGENTS.md come from Looper.",
        f"model_provider = {_toml_value(PROVIDER_ID)}",
        *([f"model_instructions_file = {_toml_value(str(model_instructions_file))}"]
          if model_instructions_file is not None else []),
        *_instruction_lines(),
        "",
        "# Codex's own skill list always includes $HOME/.agents/skills; Looper lists the run's skills itself.",
        "[skills]",
        "include_instructions = false",
        "",
        "# No plugin marketplace sync into this home.",
        "[features]",
        "plugins = false",
        "",
        "# The agent's shell commands do not get the provider's key.",
        "[shell_environment_policy]",
        f"exclude = {_toml_value([KEY_ENV])}",
        "",
        f"[model_providers.{PROVIDER_ID}]",
        f"name = {_toml_value(provider.name)}",
        f"base_url = {_toml_value(provider.base_url or DEFAULT_BASE_URL)}",
        f"env_key = {_toml_value(KEY_ENV)}",
        'wire_api = "responses"',
    ]
    return _with_mcp(lines, launch)


def _login_config(shell_home: Path) -> list[str]:
    """A login home's config: Codex's built-in OpenAI provider (no ``model_provider``) and its login read from
    this home's ``auth.json`` (a link to the operator's login file), ChatGPT's apps off, and a ``CODEX_HOME``
    without the login for the agent's shell commands."""
    return [
        "# Written by Looper for its Codex runs. Only this file, AGENTS.md and the auth.json link come from Looper.",
        "# Codex's own login: auth.json here links to the operator's login file, never a copy.",
        codex_login.CONFIG_MARKER,
        *_instruction_lines(),
        "",
        "# Codex's own skill list always includes $HOME/.agents/skills; Looper lists the run's skills itself.",
        "[skills]",
        "include_instructions = false",
        "",
        "# No plugin marketplace sync and no ChatGPT apps in this home.",
        "[features]",
        "plugins = false",
        "apps = false",
        "",
        "# The agent's shell commands get a CODEX_HOME without the login, so a codex command there cannot use it",
        "# or log it out.",
        "[shell_environment_policy]",
        f"set = {_toml_value({'CODEX_HOME': str(shell_home)})}",
    ]


def _instruction_lines() -> list[str]:
    """A project's CLAUDE.md is read where it has no AGENTS.md (``instructions``)."""
    return ["# A directory of the project without AGENTS.md: its CLAUDE.md instead.",
            f"project_doc_fallback_filenames = {_toml_value(list(FALLBACK_NAMES))}"]


def _with_mcp(lines: list[str], launch: Launch) -> str:
    for name, table in mcp_servers(launch.library)[0]:
        lines += ["", f"[mcp_servers.{_toml_key(name)}]", *(f"{_toml_key(k)} = {_toml_value(v)}" for k, v in table.items())]
    return "\n".join(lines) + "\n"


@dataclass(frozen=True)
class SkillDocument:
    """A skill given to the agent as a document to read: its name, what it is for, and its ``SKILL.md``."""

    name: str
    description: str
    path: str


@dataclass(frozen=True)
class RuntimeCatalog:
    """What a run's installed items become for Codex (RFC P9): the skills as documents, the directories to mount,
    and what was left out."""

    skills: tuple[SkillDocument, ...] = ()
    skill_dirs: tuple[str, ...] = ()
    skipped: tuple[str, ...] = ()


@lru_cache(maxsize=64)
def materialize(library: AgentLibrary) -> RuntimeCatalog:
    """The skills of the run's plugin directories (each ``skills/<name>/SKILL.md``, named ``<plugin>:<name>``
    as Claude Code names them) and the plugin parts Codex cannot load. Read from the read-only copies
    ``library.load_installed`` wrote, which are named by their content, so the result is cached."""
    from ..catalog_sources import split_skill_md

    skills: list[SkillDocument] = []
    dirs: list[str] = []
    skipped: list[str] = []
    for directory in map(Path, library.plugin_dirs):
        plugin = directory.name
        found = False
        for skill in sorted((directory / "skills").iterdir()) if (directory / "skills").is_dir() else []:
            skill_md = skill / "SKILL.md"
            if not skill_md.is_file():
                continue
            fields, _ = split_skill_md(skill_md.read_text(encoding="utf-8", errors="replace"))
            description = " ".join(str(fields.get("description") or "").split())
            if len(description) > _DESCRIPTION_MAX_CHARS:
                description = description[:_DESCRIPTION_MAX_CHARS - 1] + "…"
            skills.append(SkillDocument(f"{plugin}:{skill.name}", description, str(skill_md)))
            found = True
        if found:
            dirs.append(str(directory))
        parts = [part for part in _PLUGIN_PARTS_LEFT_OUT if (directory / part).exists()]
        if parts:
            skipped.append(f"plugin {plugin}: {', '.join(parts)} (Codex cannot load them)")
    return RuntimeCatalog(tuple(skills), tuple(dirs), tuple(skipped))


def runtime_notes(catalog: RuntimeCatalog) -> str:
    """What only a Codex agent needs to know, written before Looper's own prompt in its ``AGENTS.md``."""
    lines = [
        "# Runtime notes (Codex)",
        "",
        "Looper runs you through Codex. The instructions after these notes are Looper's, not a repository's "
        "AGENTS.md.",
        "",
        "- Looper reads only your last message of each turn. When the instructions ask for a reply or a report in "
        "a given shape (such as a JSON block), put all of it in that last message.",
    ]
    if catalog.skills:
        lines += [
            "- Skills are installed for you as read-only files. Before you use one, read its SKILL.md in full and "
            "follow it; a path it names is relative to its directory.",
            "",
            "## Available skills",
            "",
            *(f"- {skill.name}: {skill.description or '(no description)'} ({skill.path})" for skill in catalog.skills),
        ]
    return "\n".join(lines)


def agents_md(prompt_file: Path, launch: Launch) -> str:
    """A run's ``AGENTS.md``: the runtime notes, then Looper's system prompt as its file holds it."""
    return runtime_notes(materialize(launch.library)) + "\n\n---\n\n" + Path(prompt_file).read_text(encoding="utf-8")


def _codex_dir(local: Path) -> Path:
    """``<runtime_root>/.local/codex``, readable by this user only: its configs hold MCP servers' values."""
    directory = local / "codex"
    directory.mkdir(parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    return directory


def _shell_home(local: Path) -> Path:
    """``<runtime_root>/.local/codex/shell-home``: the ``CODEX_HOME`` a login run's shell commands get, empty of
    any login of Looper's."""
    directory = _codex_dir(local) / codex_login.SHELL_HOME
    directory.mkdir(exist_ok=True)
    return directory


def _write_private(path: Path, text: str) -> None:
    """Write ``path`` (0600) through a temporary file beside it, so a reader never sees a partial one."""
    fd, building = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(building, path)
    except BaseException:
        Path(building).unlink(missing_ok=True)
        raise


def _remove_idle_homes(runs: Path, now: float) -> None:
    """Remove the run homes in ``runs`` that nothing has written to for ``RUN_HOME_IDLE_SEC``.

    A home in use is written to all along (Codex keeps its state and logs
    there), and the tiers write the home of the next turn or run again when
    it is gone, so an idle one is garbage. The process list cannot tell: a
    run's command line names its home only through ``env``, which is gone
    once Codex runs.
    """
    for home in runs.iterdir():
        if not _HASHED.fullmatch(home.name) or not home.is_dir() or home.is_symlink():
            continue
        try:
            newest = max([home.stat().st_mtime, *(path.lstat().st_mtime for path in home.rglob("*"))])
        except OSError:
            continue
        if now - newest > RUN_HOME_IDLE_SEC:
            shutil.rmtree(home, ignore_errors=True)
    _remove_idle_instructions(runs.parent, now)


def _remove_idle_instructions(codex: Path, now: float) -> None:
    """Reclaim old instructions only after removing idle runs; resident session references never expire."""
    directory = codex / "instructions"
    if not directory.is_dir():
        return
    referenced: set[Path] = set()
    try:
        for root in (codex / "runs", codex / "sessions"):
            for config in root.rglob("config.toml"):
                value = tomllib.loads(config.read_text(encoding="utf-8")).get("model_instructions_file")
                if isinstance(value, str):
                    path = Path(value)
                    referenced.add((path if path.is_absolute() else config.parent / path).resolve())
    except (OSError, ValueError):
        # An unreadable or damaged home cannot prove its instructions are unreferenced.
        return
    for path in directory.iterdir():
        if path.suffix != ".md" or not _HASHED.fullmatch(path.stem) or path.is_symlink():
            continue
        try:
            if path.is_file() and path.resolve() not in referenced and now - path.stat().st_mtime > RUN_HOME_IDLE_SEC:
                path.unlink()
        except OSError:
            continue


def write_instructions(local: Path, text: str) -> Path:
    """Keep replacing instructions outside run homes, so resident config copies retain a stable target."""
    directory = _codex_dir(local) / "instructions"
    directory.mkdir(exist_ok=True)
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    target = directory / (digest + ".md")
    try:
        if hashlib.sha256(target.read_bytes()).hexdigest() == digest:
            return target
    except FileNotFoundError:
        pass
    _write_private(target, text)
    return target


def write_home(local: Path, config: str, agents: str, *, login: Path | None = None) -> Path:
    """The home of the runs of this ``config.toml`` and ``AGENTS.md``: ``<local>/codex/runs/<sha256>``, written
    once; Codex keeps its own state there too. With ``login`` (the operator's login file), its ``auth.json`` is a
    link to it (``codex_login.link_login``). Writing a new one removes the idle ones (``_remove_idle_homes``)."""
    named = f"{HOME_LAYOUT_ID}\n{config}\0{agents}" + (f"\0{login}" if login is not None else "")
    key = hashlib.sha256(named.encode("utf-8")).hexdigest()
    runs = _codex_dir(local) / "runs"
    target = runs / key
    if (target / "AGENTS.md").is_file():
        if login is not None:
            codex_login.link_login(target, login)
        return target
    runs.mkdir(exist_ok=True)
    building = Path(tempfile.mkdtemp(prefix=".building.", dir=runs))
    try:
        _write_private(building / "config.toml", config)
        _write_private(building / "AGENTS.md", agents)
        if login is not None:
            codex_login.link_login(building, login)
        try:
            os.rename(building, target)
        except OSError:
            if not (target / "AGENTS.md").is_file():
                raise
        else:
            building = None  # type: ignore[assignment]
    finally:
        if building is not None:
            shutil.rmtree(building, ignore_errors=True)
    # Publish the new config before reclaiming instructions: it may reference an old file itself.
    _remove_idle_homes(runs, time.time())
    return target


def _sync_home(source: Path, home: Path) -> None:
    """Give a session's ``home`` the ``config.toml``, the ``AGENTS.md`` and the login link (or none) of the run
    home ``source``. The link comes before a login config and goes after an API key one, so a home with a
    login config always has it (``codex_login.inspect_homes``)."""
    home.mkdir(parents=True, exist_ok=True)
    os.chmod(home, 0o700)
    link = source / codex_login.LOGIN_FILE
    login = Path(os.readlink(link)) if link.is_symlink() else None
    if login is not None:
        codex_login.link_login(home, login)
    for name in ("config.toml", "AGENTS.md"):
        text = (source / name).read_text(encoding="utf-8")
        target = home / name
        if not target.is_file() or target.read_text(encoding="utf-8") != text:
            _write_private(target, text)
    if login is None:
        codex_login.unlink_login(home)


# --- Events ---

# The names the internal events give Codex's tool calls (the activity feed reads them, ``tool_summary``).
SHELL_TOOL = "shell"
PATCH_TOOL = "apply_patch"
SEARCH_TOOL = "web_search"
PLAN_TOOL = "update_plan"


def shell_command(command: str) -> str:
    """The command a Codex shell call ran, without the ``<shell> -lc`` around it."""
    try:
        parts = shlex.split(command)
    except ValueError:
        return command
    if len(parts) == 3 and parts[1] in ("-c", "-lc"):
        return parts[2]
    return command


def _codex_usage(usage: Mapping[str, Any]) -> dict[str, int]:
    """Codex's token counts in Claude Code's ``modelUsage`` terms: its ``input_tokens`` include the cached ones,
    which Claude Code counts apart; its ``output_tokens`` include the reasoning ones."""
    def count(key: str) -> int:
        value = usage.get(key)
        return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0

    cached, written = count("cached_input_tokens"), count("cache_write_input_tokens")
    return {"inputTokens": max(count("input_tokens") - cached - written, 0), "outputTokens": count("output_tokens"),
            "cacheReadInputTokens": cached, "cacheCreationInputTokens": written}


@dataclass
class CodexStream:
    """One Codex process's ``exec --json`` events as Looper's internal events (Claude Code's stream-json shapes).

    - ``thread.started`` → ``system``/``init`` with the ``session_id`` (the thread);
    - an ``agent_message`` → an ``assistant`` text block; a ``reasoning`` item → a ``thinking`` block;
    - a command, a patch, an MCP call, a web search or a plan update → an ``assistant`` ``tool_use`` when it
      starts and a ``user`` ``tool_result`` when it completes (both at once when only its completion comes);
    - ``turn.completed`` → a ``usage`` event with the turn's own tokens (the running totals Codex reports, less
      ``before``, the session's totals when the turn started), then a successful ``result`` whose ``result`` is
      the turn's last message and whose ``modelUsage`` holds the running totals; ``turn.failed`` → a failed
      ``result``;
    - an ``error`` item or event (a warning, a retry) and anything unknown → ``system`` events the record keeps.

    A process that ends without ``turn.completed`` or ``turn.failed`` gives no ``result``.
    """

    model: str | None = None  # the run's model: Codex does not name it
    before: Usage = field(default_factory=Usage)  # the session's running totals when the turn started
    session_id: str | None = None
    _last_text: str = ""
    _open: set[str] = field(default_factory=set)

    async def events(self, stdout: asyncio.StreamReader) -> AsyncIterator[dict[str, Any]]:
        from ..claude_cli import stream_events

        async for raw in stream_events(stdout):
            for event in self.translate(raw):
                yield event

    def _base(self) -> dict[str, Any]:
        return {"session_id": self.session_id} if self.session_id else {}

    def translate(self, raw: Mapping[str, Any]) -> list[dict[str, Any]]:
        kind = raw.get("type")
        if kind == "thread.started":
            self.session_id = str(raw.get("thread_id") or "") or None
            return [{"type": "system", "subtype": "init", **self._base(), "runtime": "codex"}]
        if kind == "turn.started":
            self._last_text = ""
            return []
        if kind in ("item.started", "item.updated", "item.completed"):
            item = raw.get("item")
            return self._item(item, completed=kind == "item.completed") if isinstance(item, Mapping) else []
        if kind == "turn.completed":
            usage = raw.get("usage") if isinstance(raw.get("usage"), Mapping) else {}
            result = {"type": "result", "subtype": "success", "is_error": False, "result": self._last_text,
                      **self._base(), "modelUsage": {self.model or "codex": _codex_usage(usage)}}
            return [usage_event(self.model, reported_totals(result).since(self.before), **self._base()), result]
        if kind in ("turn.failed", "thread.failed"):
            error = raw.get("error")
            message = str(error.get("message") if isinstance(error, Mapping) else error or "the turn failed")
            return [{"type": "result", "subtype": "error_during_execution", "is_error": True, "result": "",
                     **self._base(), "errors": [message]}]
        if kind == "error":
            return [{"type": "system", "subtype": "codex_error", **self._base(), "message": str(raw.get("message"))}]
        return [{"type": "system", "subtype": "codex_event", **self._base(), "event": dict(raw)}]

    def _item(self, item: Mapping[str, Any], *, completed: bool) -> list[dict[str, Any]]:
        item_type = item.get("type")
        item_id = str(item.get("id") or "")
        if item_type == "agent_message":
            if not completed:
                return []
            self._last_text = str(item.get("text") or "")
            return [self._assistant({"type": "text", "text": self._last_text})]
        if item_type == "reasoning":
            return [self._assistant({"type": "thinking", "thinking": str(item.get("text") or "")})] if completed else []
        if item_type == "error":
            return [{"type": "system", "subtype": "codex_warning", **self._base(),
                     "message": str(item.get("message") or "")}] if completed else []
        call = _tool_call(item)
        if call is None:
            return [{"type": "system", "subtype": "codex_item", **self._base(), "item": dict(item)}] if completed else []
        name, tool_input = call
        events = []
        if item_id not in self._open:
            self._open.add(item_id)
            events.append(self._assistant({"type": "tool_use", "id": item_id, "name": name, "input": tool_input}))
        if completed:
            self._open.discard(item_id)
            content, is_error = _tool_result(item)
            events.append({"type": "user", **self._base(), "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": item_id, "content": content, "is_error": is_error}]}})
        return events

    def _assistant(self, block: dict[str, Any]) -> dict[str, Any]:
        return {"type": "assistant", **self._base(), "parent_tool_use_id": None,
                "message": {"role": "assistant", "content": [block]}}


def _tool_call(item: Mapping[str, Any]) -> tuple[str, dict[str, Any]] | None:
    """A tool call item's name and input, as the internal ``tool_use`` gives them; None for any other item."""
    item_type = item.get("type")
    if item_type == "command_execution":
        return SHELL_TOOL, {"command": str(item.get("command") or "")}
    if item_type == "file_change":
        return PATCH_TOOL, {"changes": [{"path": c.get("path"), "kind": c.get("kind")}
                                        for c in item.get("changes") or [] if isinstance(c, Mapping)]}
    if item_type == "mcp_tool_call":
        return f"mcp__{item.get('server')}__{item.get('tool')}", dict(item.get("arguments") or {})
    if item_type == "web_search":
        return SEARCH_TOOL, {"query": str(item.get("query") or "")}
    if item_type == "todo_list":
        return PLAN_TOOL, {"items": list(item.get("items") or [])}
    if item_type == "collab_tool_call":
        return str(item.get("tool") or "collab_tool_call"), {k: v for k, v in item.items() if k not in ("id", "type")}
    return None


def _tool_result(item: Mapping[str, Any]) -> tuple[str, bool]:
    """A completed tool call's output and whether it failed."""
    failed = item.get("status") == "failed"
    item_type = item.get("type")
    if item_type == "command_execution":
        exit_code = item.get("exit_code")
        return str(item.get("aggregated_output") or ""), failed or (isinstance(exit_code, int) and exit_code != 0)
    if item_type == "file_change":
        changes = [f"{c.get('kind')} {c.get('path')}" for c in item.get("changes") or [] if isinstance(c, Mapping)]
        return "\n".join(changes), failed
    if item_type == "mcp_tool_call":
        error = item.get("error")
        if error:
            return str(error.get("message") if isinstance(error, Mapping) else error), True
        result = item.get("result") if isinstance(item.get("result"), Mapping) else {}
        texts = [str(c.get("text")) for c in result.get("content") or [] if isinstance(c, Mapping) and c.get("text")]
        return "\n".join(texts), failed
    return json.dumps({k: v for k, v in item.items() if k not in ("id", "type")}, ensure_ascii=False), failed


# --- Resident sessions ---


class ResidentCodex(PerTurnSession):
    """The Manager's or a Planner's Codex session: one ``codex exec`` process per turn, resuming the session
    (``per_turn.PerTurnSession``).

    ``start`` keeps the command (``CodexRuntime.resident_command``, plus
    ``resume <thread>`` for a saved session); each turn runs
    ``codex exec ... [resume <thread>] -`` with the message on stdin, and the
    thread its first turn starts is resumed by the next.

    The session lives in a ``CODEX_HOME`` of the owner's own,
    ``<runtime_root>/.local/codex/sessions/<label>``: every session of the
    Manager (or of a task's Planner) is kept there, while what the turns run
    with, the ``config.toml`` and the ``AGENTS.md``, is copied in before each
    turn from the run home the command names, and so is its login link (never
    a copy of the login itself; ``_sync_home``). A changed system prompt
    restarts the session (its command changes): Codex then sends the new
    ``AGENTS.md`` into the resumed session.

    Codex reports the session's running totals at each turn's end, carrying on
    after a resume, so a turn's own tokens are the difference (``turn_usage``).
    """

    runtime_label = "Codex"

    def _split(self, command: list[str]) -> tuple[list[str], str | None, str]:
        if len(command) < 4 or command[0] != "env" or not command[1].startswith("CODEX_HOME="):
            raise ValueError("a resident Codex command starts with env CODEX_HOME=<home> <codex> exec")
        if len(command) >= 2 and command[-2] == "resume":
            return command[:-2], command[-1], command[2]
        return command, None, command[2]

    def _turn_argv(self) -> list[str]:
        source = Path(self._base[1].partition("=")[2])
        home = source.parent.parent / "sessions" / session_dir_name(self._label)
        _sync_home(source, home)
        resume = ["resume", self._session] if self._session else []
        return ["env", f"CODEX_HOME={home}", *self._base[2:], *resume, "-"]

    def _stream(self) -> CodexStream:
        base = self._base
        return CodexStream(model=base[base.index("-m") + 1] if "-m" in base else None, before=self._totals)

    def turn_usage(self, result_event: dict[str, Any] | None) -> Usage:
        """A turn's own tokens: its result's running totals less the previous ones."""
        own = run_usage(result_event, before=self._totals, price=self._price)
        if result_event is not None:
            self._totals = self._totals.updated(reported_totals(result_event, self._price))
        return own
