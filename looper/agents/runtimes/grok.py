"""The Grok runtime: xAI's ``grok`` CLI (Grok Build) in headless mode, one process per turn (Grok runtime RFC).

Verified against Grok 1.0.62 (the Grok runtime RFC, sections 2 and 12):

- **A run** is ``grok --prompt-file /dev/stdin --output-format
  streaming-messages-json ...``: headless mode reads no prompt from stdin, so
  the prompt is read as a file, the process's stdin to its end; it runs one turn
  and exits. It takes no user turn while it runs, so a Worker cannot be steered
  (``write_turn`` closes stdin after the prompt).
- **A resident session** (the Manager, a Planner) is one such process per
  turn, resuming the session (``--resume <id>``, ``ResidentGrok``) in a home of
  the owner's own, since Grok keeps its sessions in its home.
- **Isolation**: every process runs with ``GROK_HOME`` and ``HOME`` both set
  to a home of Looper's own under ``<runtime_root>/.local/grok``
  (``write_home``), holding the ``config.toml`` and the rules file Looper
  writes. ``GROK_HOME`` keeps the operator's ``~/.grok`` (settings, rules,
  skills, memory, plugins, hooks) out; ``HOME`` keeps out what Grok finds
  through it whatever its config says: the skills in ``~/.agents/skills``, the
  Claude Code plugins in ``~/.claude/plugins`` (their skills, hooks, agents and
  MCP servers) and the permissions in ``~/.claude/settings.json``. The config
  and the environment also turn Grok's scans of Claude Code's and Cursor's
  directories off (``COMPAT_VARS``). Memory, the update check, the shared
  leader, telemetry and trace upload are off. The variables that would point
  Grok elsewhere are not passed (``DIRECTORY_VARS``, ``RESTORED_VARS``); the
  agent's shell commands and the stdio MCP servers get ``HOME`` and the others
  back. Grok unpacks its bundled skills (13 MB) and its user guide into every
  new home, so each home links them to one shared copy (``SHARED_DIRS``).
- **The project's own content** is read, as Grok reads it in a trusted folder
  (``--trust``): its instruction files from the repository's root down to the
  work directory, every name Grok knows in each (``INSTRUCTION_NAMES``), and its
  ``.grok`` directory (rules, skills, MCP servers, hooks).
- **The system prompt** is the home's ``rules/looper.md``, a rule Grok reads
  at every start: a short block of runtime notes (``runtime_notes``), then
  Looper's prompt, which stays runtime-neutral. ``--rules`` takes text only,
  which would put the prompt on the command line.
- **The provider** is Grok's own login (``grok_login``): the home's
  ``auth.json`` links to the operator's login file, the variables that would
  make Grok bill an API key instead are removed, and the agent's shell commands
  get a ``GROK_HOME`` without the link.
- **The catalog** (RFC P9): MCP servers become ``[mcp_servers.<name>]``; the
  model reaches their tools through Grok's ``search_tool`` and ``use_tool`` as
  ``<server>__<tool>``, which the runtime notes say. The skills of the
  installed plugins load natively (``[skills] paths``); a plugin's commands,
  agents, hooks, MCP file and output styles are left out. What was left out is
  in the run's record (``launch_record``). Knowledge is read by the paths the
  prompt's index names: Grok has no ``--add-dir``.
- **Events and usage**: ``GrokStream`` reads ``streaming-messages-json``,
  which is Claude Code's stream-json, and adds each response's tokens as an
  internal ``usage`` event. The result's ``total_cost_usd`` is what xAI
  stamped for the run, with Grok's own login too, so it is the run's cost
  (``runtime``); a 0 with tokens spent means Grok did not know it, and is left
  out. Grok has no spend cap of its own and reports the cost only at the end,
  so the per-run cap does not stop a Grok run (``runtime_cost_capped``).
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import shutil
import subprocess
import tempfile
import time
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

from ..run_usage import Usage, request_tokens, run_usage
from ..system_prompt_mode import SystemPromptMode
from . import (
    ApiProtocol, AuthKind, Capability, CostSource, Probe, ProviderBinding, RunCost, RuntimeAdapter, RuntimeUnsupported,
    grok_login, instructions,
)
from .codex import _HASHED, _toml_key, _toml_value
from .instructions import InstructionFile
from .per_turn import PerTurnSession, session_dir_name
from .pricing import usage_event

if TYPE_CHECKING:
    from ..profile_registry import ProviderSpec
    from ..agent_settings import Launch
    from ..library import AgentLibrary

_PROBE_TIMEOUT_SEC = 10.0
# Names the layout of a generated home in each run's record; change it when the layout changes.
HOME_LAYOUT_ID = "grok-home/v1"
RULES_FILE = "rules/looper.md"
# Where a run's prompt is read from: its stdin, to its end.
PROMPT_FILE = "/dev/stdin"
# What would point Grok at another home, configuration, agent or sandbox: never passed to it.
DIRECTORY_VARS = ("GROK_HOME", "GROK_CONFIG", "GROK_CONFIG_PATH", "GROK_AGENT", "GROK_SANDBOX")
# What Grok would find the operator's directories through, which the agent's shell commands and the stdio MCP
# servers get back. HOME is the run's home; the others are not passed to Grok.
RESTORED_VARS = ("HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME", "CLAUDE_CONFIG_DIR")
# What Grok unpacks into every new home that every home can share: its bundled skills and its user guide.
SHARED_DIRS = ("bundled", "docs")
# Grok's scans of Claude Code's and Cursor's user directories, each its own variable, which wins over config.toml.
COMPAT_VARS = tuple(f"GROK_{vendor}_{cell}_ENABLED" for vendor in ("CLAUDE", "CURSOR")
                    for cell in ("SKILLS", "RULES", "AGENTS", "MCPS", "HOOKS"))
_COMPAT_CELLS = ("skills", "rules", "agents", "mcps", "hooks")
# What every run's environment sets.
FIXED_ENV = (("GROK_DISABLE_AUTOUPDATER", "1"), ("GROK_MEMORY", "0"), *((name, "false") for name in COMPAT_VARS))
# The project instruction files Grok reads in each directory, in its order, every one it finds.
INSTRUCTION_NAMES = ("Agents.md", "Claude.md", "CLAUDE.md", "CLAUDE.local.md", "AGENT.md", "AGENTS.md")
# The parts of a Claude Code plugin Grok does not get from Looper.
_PLUGIN_PARTS_LEFT_OUT = ("commands", "agents", "hooks", ".mcp.json", "output-styles")
# How long a run home may go unwritten before it is removed (``_remove_idle_homes``): far beyond any run.
RUN_HOME_IDLE_SEC = 24 * 3600


class GrokRuntime(RuntimeAdapter):
    can_replace_system_prompt = True
    type = "grok"
    label = "Grok"
    default_executable = "grok"
    # Headless Grok: one process per turn, resumed; MCP; compaction events; its cost, once a run ends.
    capabilities = frozenset({Capability.RUN, Capability.RESIDENT_SESSION, Capability.RESUME, Capability.MCP,
                              Capability.COMPACTION_EVENTS, Capability.COST_REPORT})
    # Its login's models answer on the Responses API (Grok's model catalog, api_backend "responses").
    protocols = frozenset({ApiProtocol.OPENAI_RESPONSES})
    # Its own login only (``grok_login``).
    auth_kinds = frozenset({AuthKind.OAUTH})
    login_models = grok_login.SUGGESTED_MODELS
    isolation_id = HOME_LAYOUT_ID
    # Set by the binding and the run's home: a provider's own variables cannot set them.
    provider_env_owned: ClassVar[frozenset[str]] = frozenset({*DIRECTORY_VARS, *RESTORED_VARS, *grok_login.ENV_UNSET,
                                                              *(name for name, _ in FIXED_ENV)})
    runtime_cost_capped = False

    def probe(self, executable: str) -> Probe:
        """``<executable> --version``, with an empty ``GROK_HOME`` of its own so nothing of the operator's is read."""
        home = tempfile.mkdtemp(prefix="looper-grok-probe-")
        try:
            done = subprocess.run([executable, "--version"], capture_output=True, text=True,
                                  timeout=_PROBE_TIMEOUT_SEC, check=False,
                                  env={**_grok_env(os.environ), "GROK_HOME": home, "HOME": home, **dict(FIXED_ENV)})
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
        """None: Grok's isolation is its own ``GROK_HOME`` and the config Looper writes there (``config_toml``)."""
        return []

    def bind_provider(self, provider: ProviderSpec, model: str, effort: str) -> ProviderBinding:
        """The model and the effort as flags, and the provider's own variables; Grok's own login needs no key, and
        what would replace it is removed from the environment."""
        if provider.auth is not AuthKind.OAUTH or provider.oauth_runtime_type != self.type:
            raise RuntimeUnsupported(f"Grok runs with its own login only; provider {provider.name!r} is not it")
        if effort and effort not in ("low", "medium", "high", "xhigh"):
            raise RuntimeUnsupported(f"Grok does not support effort {effort!r}; use one of: low, medium, high, xhigh")
        return ProviderBinding(args=("-m", model, *(("--effort", effort) if effort else ())), env=provider.env,
                               unset=(*DIRECTORY_VARS, *RESTORED_VARS[1:], *grok_login.ENV_UNSET))

    def launch_args(self, launch: Launch) -> list[str]:
        """The headless flags of every run: the prompt from stdin, Claude Code's stream-json, every tool without
        asking, the project's own content trusted, no update; the model and the effort."""
        binding = self.bind_provider(launch.profile.provider, launch.model, launch.effort)
        return [
            "--prompt-file", PROMPT_FILE, "--output-format", "streaming-messages-json",
            "--always-approve", "--trust", "--no-auto-update",
            *binding.args,
        ]

    def environment(self, launch: Launch) -> dict[str, str]:
        """The server's environment and the settings' variables, less what the binding removes, then the
        provider's own variables and what every run sets (``FIXED_ENV``). ``GROK_HOME`` and ``HOME`` come with the
        command."""
        binding = self.bind_provider(launch.profile.provider, launch.model, launch.effort)
        visible = {**os.environ, **dict(launch.env)}
        return {**_grok_env(visible), **dict(binding.env), **dict(FIXED_ENV)}

    def _command(self, executable: str, prompt_file: Path, launch: Launch, max_turns: int) -> list[str]:
        args = self.launch_args(launch)  # refuses a provider it cannot run with before anything is written
        local = _local_dir(prompt_file)
        rules, agent = rules_md(prompt_file, launch), None
        if launch.system_prompt_mode is SystemPromptMode.REPLACE:
            agent = "---\nname: looper\ndescription: Looper agent\nprompt_mode: full\n---\n\n" + rules
            rules = ""
        home = write_home(local, config_toml(launch, shell_home=_shell_home(local)), rules,
                          login=grok_login.login_file(), agent=agent)
        return ["env", f"GROK_HOME={home}", f"HOME={home}", executable, *args,
                *(["--agent", str(home / "agent.md")] if agent is not None else []),
                *(["--max-turns", str(max_turns)] if max_turns > 0 else [])]

    def resident_command(self, executable: str, *, cwd: str | Path, prompt_file: Path, launch: Launch,
                         max_turns: int = 0) -> list[str]:
        """The command of the turns of a resident session, without the session to resume (``ResidentGrok`` adds
        it, and the owner's home)."""
        return self._command(executable, prompt_file, launch, max_turns)

    def resume_args(self, session_id: str) -> list[str]:
        return ["--resume", session_id]

    def run_command(self, executable: str, *, cwd: str | Path, prompt_file: Path, launch: Launch,
                    max_turns: int = 0) -> list[str]:
        """One run: the prompt read from stdin; its session stays in the run's home, removed with it."""
        return self._command(executable, prompt_file, launch, max_turns)

    def resident_session(self, label: str) -> ResidentGrok:
        return ResidentGrok(label=label)

    async def write_turn(self, stdin: asyncio.StreamWriter, text: str, *, uuid: str | None = None) -> None:
        """Write a run's prompt and close stdin, which ends the prompt file Grok reads. There is no second user
        turn: writing one raises ``RuntimeError``."""
        if stdin.is_closing():
            raise RuntimeError("Grok reads one prompt per run: it takes no user turn while it runs")
        stdin.write(text.encode("utf-8"))
        await stdin.drain()
        stdin.close()

    def parse(self, stdout: asyncio.StreamReader, *, model: str | None = None) -> AsyncIterator[dict[str, Any]]:
        """A run's events; their tokens are named after the model of each response, else the run's ``model``."""
        return GrokStream(model=model).events(stdout)

    def tool_summary(self, tool_name: str, tool_input: dict[str, Any]) -> str:
        if tool_name in SHELL_TOOLS:
            return f"Running: {str(tool_input.get('command') or tool_input.get('cmd') or '')[:80]}"
        if tool_name in READ_TOOLS:
            return f"Reading: {_path(tool_input)}"
        if tool_name in EDIT_TOOLS:
            return f"Editing: {_path(tool_input)}"
        if tool_name in WRITE_TOOLS:
            return f"Writing: {_path(tool_input)}"
        if tool_name in SEARCH_TOOLS:
            return f"{tool_name}: {str(tool_input.get('pattern') or tool_input.get('query') or _path(tool_input))[:60]}"
        if tool_name == "web_search":
            return f"Searching: {str(tool_input.get('query') or '')[:60]}"
        if tool_name == "use_tool":
            return f"Tool: {tool_input.get('tool_name') or '?'}"
        if tool_name in SUBAGENT_TOOLS:
            return "Delegating to a subagent"
        return f"Tool: {tool_name}"

    def cost_source(self, provider: ProviderSpec) -> CostSource:
        """Grok's own figure: what xAI stamped for the run (the module's notes)."""
        return CostSource.RUNTIME

    def cost(self, usage: Usage, provider: ProviderSpec) -> RunCost:
        """The run's ``usage`` with Grok's own figure as its cost (``total_cost_usd``, ``runtime``)."""
        return RunCost(usage, self.cost_source(provider))

    def budget_error(self, result_event: dict[str, Any] | None) -> str | None:
        """None: Grok has no spend cap of its own, and a run priced by its own figure has no soft cap."""
        return None

    async def login_problem(self, provider: ProviderSpec) -> str | None:
        """Why a run with Grok's own login cannot start now (``grok_login.launch_problem``)."""
        return await grok_login.launch_problem()

    async def after_run(self, launch: Launch, command: list[str]) -> list[str]:
        """After a run: every home under the run's ``grok`` directory whose login link is not intact, its copy
        removed and the problem recorded (``grok_login.inspect_homes``)."""
        home = next((arg.partition("=")[2] for arg in command[:3] if arg.startswith("GROK_HOME=")), "")
        if not home:
            return []
        problems = await asyncio.to_thread(grok_login.inspect_homes, Path(home).parent.parent, grok_login.login_file())
        return [await grok_login.record_problems(problems)] if problems else []

    def instruction_files(self, cwd: str | Path) -> tuple[InstructionFile, ...]:
        """Every instruction file Grok knows in each directory of the project, the root's first, which Grok reads
        itself (``INSTRUCTION_NAMES``)."""
        return tuple(map(InstructionFile, every_named(cwd, INSTRUCTION_NAMES)))

    def launch_record(self, launch: Launch) -> dict[str, Any]:
        """The skills Grok loads natively and what it does not get, when there are any (RFC P9)."""
        catalog = materialize(launch.library)
        skipped = [*catalog.skipped, *mcp_servers(launch.library)[1]]
        if not catalog.skill_dirs and not skipped:
            return {}
        return {"runtime_catalog": {"runtime": self.type, "skill_dirs": list(catalog.skill_dirs), "skipped": skipped}}


def every_named(cwd: str | Path, names: tuple[str, ...]) -> list[Path]:
    """In each directory of the project of a run in ``cwd``, the root first, every file of ``names`` it has, by
    the name on disk, in the order of ``names``."""
    found = []
    for directory in instructions.project_dirs(cwd):
        try:
            present = {entry.name for entry in os.scandir(directory) if entry.is_file()}
        except OSError:
            continue
        found += [directory / name for name in names if name in present]
    return found


def _grok_env(visible: Mapping[str, str]) -> dict[str, str]:
    """``visible`` without the variables that point Grok elsewhere or replace its login."""
    unset = (*DIRECTORY_VARS, *RESTORED_VARS[1:], *grok_login.ENV_UNSET)
    return {k: v for k, v in visible.items() if k not in unset}


# --- The catalog (RFC P9) ---


@dataclass(frozen=True)
class RuntimeCatalog:
    """What a run's installed plugins give Grok: their skill directories, and what was left out."""

    skill_dirs: tuple[str, ...] = ()
    skipped: tuple[str, ...] = ()


def materialize(library: AgentLibrary) -> RuntimeCatalog:
    """Each installed plugin's ``skills`` directory, which Grok scans for ``SKILL.md`` files itself, and the parts
    of a plugin it does not get. Read from the read-only copies ``library.load_installed`` wrote."""
    dirs: list[str] = []
    skipped: list[str] = []
    for directory in map(Path, library.plugin_dirs):
        if (directory / "skills").is_dir():
            dirs.append(str(directory / "skills"))
        if parts := [part for part in _PLUGIN_PARTS_LEFT_OUT if (directory / part).exists()]:
            skipped.append(f"plugin {directory.name}: {', '.join(parts)} (Grok does not get them)")
    return RuntimeCatalog(tuple(dirs), tuple(skipped))


def mcp_servers(library: AgentLibrary, visible: Mapping[str, str] | None = None,
                ) -> tuple[list[tuple[str, dict[str, Any]]], list[str]]:
    """The run's MCP servers as Grok's ``[mcp_servers.<name>]`` tables, and the ones it cannot reach.

    They come from the run's ``--mcp-config`` file (``mcp.write_config``), the
    servers in Claude Code's shape: stdio servers keep their command, arguments
    and environment, and get ``visible``'s ``RESTORED_VARS`` (the environment
    Looper runs agents with) back unless they set them, since Grok starts them
    with its own; HTTP and SSE servers keep their URL and headers (``type =
    "sse"``, as ``grok mcp add -t sse`` writes it).
    """
    import json

    if not library.mcp_config:
        return [], []
    restored = {name: visible[name] for name in RESTORED_VARS if (visible or {}).get(name)}
    servers = json.loads(Path(library.mcp_config).read_text(encoding="utf-8")).get("mcpServers") or {}
    tables: list[tuple[str, dict[str, Any]]] = []
    skipped: list[str] = []
    for name, server in sorted(servers.items()):
        transport = server.get("type") or ("stdio" if server.get("command") else "")
        if transport == "stdio":
            table: dict[str, Any] = {"command": server["command"], "args": list(server.get("args") or [])}
            if env := {**restored, **(server.get("env") or {})}:
                table["env"] = env
        elif transport in ("http", "sse"):
            table = {"url": server["url"], **({"type": "sse"} if transport == "sse" else {})}
            if server.get("headers"):
                table["headers"] = dict(server["headers"])
        else:
            skipped.append(f"MCP server {name}: Grok has no {transport or 'unknown'} transport")
            continue
        tables.append((name, table))
    return tables, skipped


def runtime_notes(servers: list[str]) -> str:
    """What only a Grok agent needs to know, before Looper's own prompt: how to reach the run's MCP servers'
    tools (``servers``)."""
    lines = [
        "# Runtime notes (Grok)",
        "",
        "Looper runs you through Grok. The instructions after these notes are Looper's.",
        "",
        "- Looper reads only your final message. When the instructions ask for a reply or a report in a given shape "
        "(such as a JSON block), put all of it in that final message.",
    ]
    if servers:
        named = ", ".join(f"`{name}`" for name in servers)
        example = " (`looper__looper_tasks` is the tool `looper_tasks` of `looper`)" if "looper" in servers else ""
        lines.append(f"- The tools of this run's MCP servers ({named}) are not among your own tools: call one with "
                     f"`use_tool` and its full name, `<server>__<tool>`{example}; `search_tool` lists them.")
    return "\n".join(lines)


def rules_md(prompt_file: Path, launch: Launch) -> str:
    """A run's ``rules/looper.md``: the runtime notes, then Looper's system prompt as its file holds it."""
    servers = [name for name, _ in mcp_servers(launch.library)[0]]
    return runtime_notes(servers) + "\n\n---\n\n" + Path(prompt_file).read_text(encoding="utf-8")


def config_toml(launch: Launch, *, shell_home: Path) -> str:
    """The ``config.toml`` of a run's home: isolation (the module's notes), the login, the agent's shell's
    ``GROK_HOME`` and ``RESTORED_VARS``, the installed skills and the MCP servers."""
    visible = {**os.environ, **dict(launch.env)}
    shell = {"GROK_HOME": str(shell_home), **{name: visible[name] for name in RESTORED_VARS if visible.get(name)}}
    lines = [
        "# Written by Looper for its Grok runs. Only this file, rules/looper.md and the auth.json link come from "
        "Looper.",
        grok_login.CONFIG_MARKER,
        "",
        "# No update check, no shared leader.",
        "[cli]",
        "auto_update = false",
        "use_leader = false",
        "",
        "# No memory carried between sessions.",
        "[memory]",
        "enabled = false",
        "",
        "# No product telemetry and no session trace upload from these runs.",
        "[features]",
        "telemetry = false",
        "",
        "[telemetry]",
        "trace_upload = false",
        "",
        "# None of Claude Code's or Cursor's user directories (~/.claude, ~/.claude.json, ~/.cursor).",
        *(line for vendor in ("claude", "cursor")
          for line in (f"[compat.{vendor}]", *(f"{cell} = false" for cell in _COMPAT_CELLS), "")),
        "# The agent's shell commands get a GROK_HOME without the login, so a grok command there cannot use it or",
        "# log it out, and the operator's HOME and the other directories Grok does not get.",
        "[shell_environment_policy]",
        f"set = {_toml_value(shell)}",
    ]
    if skill_dirs := materialize(launch.library).skill_dirs:
        lines += ["", "# The skills of the plugins installed for the run.", "[skills]",
                  f"paths = {_toml_value(list(skill_dirs))}"]
    for name, table in mcp_servers(launch.library, visible)[0]:
        lines += ["", f"[mcp_servers.{_toml_key(name)}]", *(f"{_toml_key(k)} = {_toml_value(v)}" for k, v in table.items())]
    return "\n".join(lines) + "\n"


# --- What a run's home holds ---


def _local_dir(prompt_file: Path) -> Path:
    """``<runtime_root>/.local``: the prompt file is ``<runtime_root>/.local/prompts/<sha256>.md``
    (``SystemPrompt.write_file``)."""
    return Path(prompt_file).absolute().parent.parent


def _grok_dir(local: Path) -> Path:
    """``<runtime_root>/.local/grok``, readable by this user only: its configs hold MCP servers' values."""
    directory = local / "grok"
    directory.mkdir(parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    return directory


def _shell_home(local: Path) -> Path:
    """``<runtime_root>/.local/grok/shell-home``: the ``GROK_HOME`` the agent's shell commands get, without the
    login."""
    directory = _grok_dir(local) / grok_login.SHELL_HOME
    directory.mkdir(exist_ok=True)
    return directory


def _write_private(path: Path, text: str) -> None:
    """Write ``path`` (0600) through a temporary file beside it, so a reader never sees a partial one."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, building = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(building, path)
    except BaseException:
        Path(building).unlink(missing_ok=True)
        raise


def _remove_idle_homes(runs: Path, now: float) -> None:
    """Remove the run homes in ``runs`` that nothing has written to for ``RUN_HOME_IDLE_SEC``: a home in use is
    written to all along (Grok keeps its session and logs there), and the next run writes its home again when it
    is gone."""
    for home in runs.iterdir():
        if not _HASHED.fullmatch(home.name) or not home.is_dir() or home.is_symlink():
            continue
        try:
            newest = max([home.stat().st_mtime, *(path.lstat().st_mtime for path in home.rglob("*"))])
        except OSError:
            continue
        if now - newest > RUN_HOME_IDLE_SEC:
            shutil.rmtree(home, ignore_errors=True)


def _link_shared(home: Path) -> None:
    """Link ``home``'s ``SHARED_DIRS`` to the copies every home shares (``<grok>/shared/<name>``): Grok unpacks into
    them through the link when they are empty or out of date, and leaves them alone otherwise."""
    shared = home.parent.parent / "shared"
    for name in SHARED_DIRS:
        (shared / name).mkdir(parents=True, exist_ok=True)
        if not os.path.lexists(home / name):
            (home / name).symlink_to(shared / name, target_is_directory=True)


def write_home(local: Path, config: str, rules: str, *, login: Path, agent: str | None = None) -> Path:
    """The home of the runs of this ``config.toml`` and rules file: ``<local>/grok/runs/<sha256>``, with generated
    files restored before reuse; Grok keeps its own state there too. Its ``auth.json`` is a link to ``login``
    (the operator's login file, ``grok_login.link_login``), and its ``SHARED_DIRS`` link to the shared copies
    (``_link_shared``). Writing a new
    one removes the idle ones (``_remove_idle_homes``)."""
    named = f"{HOME_LAYOUT_ID}\n{config}\0{rules}\0{login}"
    if agent is not None:
        named += f"\0{agent}"
    key = hashlib.sha256(named.encode("utf-8")).hexdigest()
    runs = _grok_dir(local) / "runs"
    target = runs / key
    if target.is_dir():
        grok_login.link_login(target, login)
        _write_private(target / "config.toml", config)
        _write_private(target / RULES_FILE, rules)
        if agent is not None:
            _write_private(target / "agent.md", agent)
        return target
    runs.mkdir(exist_ok=True)
    _remove_idle_homes(runs, time.time())
    building: Path | None = Path(tempfile.mkdtemp(prefix=".building.", dir=runs))
    try:
        _write_private(building / "config.toml", config)
        _write_private(building / RULES_FILE, rules)
        if agent is not None:
            _write_private(building / "agent.md", agent)
        grok_login.link_login(building, login)
        _link_shared(building)
        try:
            os.rename(building, target)
        except OSError:
            if not (target / RULES_FILE).is_file():
                raise
        else:
            building = None
    finally:
        if building is not None:
            shutil.rmtree(building, ignore_errors=True)
    return target


def _sync_home(source: Path, home: Path) -> None:
    """Give a session's ``home`` the ``config.toml``, the rules file, the login link and the shared directories of
    the run home ``source``."""
    home.mkdir(parents=True, exist_ok=True)
    os.chmod(home, 0o700)
    _link_shared(home)
    link = source / grok_login.LOGIN_FILE
    if link.is_symlink():
        grok_login.link_login(home, Path(os.readlink(link)))
    for name in ("config.toml", RULES_FILE, *(["agent.md"] if (source / "agent.md").is_file() else [])):
        text = (source / name).read_text(encoding="utf-8")
        target = home / name
        if not target.is_file() or target.read_text(encoding="utf-8") != text:
            _write_private(target, text)
    if not (source / "agent.md").is_file():
        (home / "agent.md").unlink(missing_ok=True)


# --- Events ---

# Grok's tool names, on both of its routes (the shell's ids and the daemon's names), as the activity reads them.
SHELL_TOOLS = frozenset({"run_terminal_cmd", "run_terminal_command", "Shell"})
READ_TOOLS = frozenset({"read_file", "hashline_read", "Read"})
EDIT_TOOLS = frozenset({"search_replace", "hashline_edit", "StrReplace", "EditNotebook"})
WRITE_TOOLS = frozenset({"write", "Write"})
SEARCH_TOOLS = frozenset({"grep", "hashline_grep", "Grep", "list_dir", "Glob"})
SUBAGENT_TOOLS = frozenset({"task", "spawn_subagent"})


def _path(tool_input: Mapping[str, Any]) -> str:
    return str(tool_input.get("path") or tool_input.get("file_path") or tool_input.get("target_file")
               or tool_input.get("target_directory") or "?")


def _readable_results(raw: Mapping[str, Any]) -> dict[str, Any]:
    """A ``user`` event with its tool results readable: Grok gives a result as a JSON object in which a command's
    output is a list of byte values (``{"type": "Bash", "output": [72, 79, ...]}``), here decoded to text."""
    import json

    message = raw.get("message")
    content = message.get("content") if isinstance(message, Mapping) else None
    if not isinstance(content, list):
        return dict(raw)
    blocks = []
    for block in content:
        text = block.get("content") if isinstance(block, Mapping) and block.get("type") == "tool_result" else None
        try:
            result = json.loads(text) if isinstance(text, str) and text.startswith("{") else None
        except ValueError:
            result = None
        if isinstance(result, dict):
            result = {key: bytes(value).decode("utf-8", errors="replace") if _byte_list(value) else value
                      for key, value in result.items()}
            block = {**block, "content": json.dumps(result, ensure_ascii=False)}
        blocks.append(block)
    return {**raw, "message": {**message, "content": blocks}}


def _byte_list(value: Any) -> bool:
    return isinstance(value, list) and bool(value) and all(
        isinstance(item, int) and not isinstance(item, bool) and 0 <= item < 256 for item in value)


def _model_usage(usage: Any) -> dict[str, int]:
    """One model's tokens in Claude Code's ``modelUsage`` terms, every field there (Grok's rows may lack one)."""
    usage = usage if isinstance(usage, Mapping) else {}

    def count(key: str) -> int:
        value = usage.get(key)
        return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0

    return {"inputTokens": count("inputTokens"), "outputTokens": count("outputTokens"),
            "cacheReadInputTokens": count("cacheReadInputTokens"),
            "cacheCreationInputTokens": count("cacheCreationInputTokens")}


@dataclass
class GrokStream:
    """One Grok process's ``streaming-messages-json`` lines as Looper's internal events.

    The lines are Claude Code's stream-json already: ``system`` (``init``,
    ``compact_boundary``), ``assistant``, ``user`` and ``result`` pass through.
    Each ``assistant`` line is one model response with its usage, followed here
    by an internal ``usage`` event with its tokens; the ``result`` keeps its
    tokens (``modelUsage`` with every field) and Grok's cost, which is left out
    when it reads 0 for tokens spent (Grok did not know it). Anything else is a
    ``system`` event the record keeps.
    """

    model: str | None = None  # the run's model, which names a response's tokens when the response does not
    session_id: str | None = None
    usage: Usage = field(default_factory=Usage)  # this process's tokens so far

    async def events(self, stdout: asyncio.StreamReader) -> AsyncIterator[dict[str, Any]]:
        from ..claude_cli import stream_events

        async for raw in stream_events(stdout):
            for event in self.translate(raw):
                yield event

    def translate(self, raw: Mapping[str, Any]) -> list[dict[str, Any]]:
        kind = raw.get("type")
        if kind == "system":
            event = dict(raw)
            if raw.get("subtype") == "init":
                self.session_id = str(raw.get("session_id") or "") or self.session_id
                event["runtime"] = "grok"
            return [event]
        if kind == "assistant":
            return self._assistant(raw)
        if kind == "user":
            return [_readable_results(raw)]
        if kind == "result":
            return [self._result(raw)]
        if kind == "stream_event":
            return []
        return [{"type": "system", "subtype": "grok_event",
                 **({"session_id": self.session_id} if self.session_id else {}), "event": dict(raw)}]

    def _assistant(self, raw: Mapping[str, Any]) -> list[dict[str, Any]]:
        message = raw.get("message") if isinstance(raw.get("message"), Mapping) else {}
        tokens = request_tokens(message.get("usage"))
        events = [dict(raw)]
        if any(tokens.to_dict().values()):
            self.usage = self.usage.plus(tokens)
            model = message.get("model") if isinstance(message.get("model"), str) and message.get("model") != "unknown" \
                else self.model
            events.append(usage_event(model, tokens, parent_tool_use_id=raw.get("parent_tool_use_id"),
                                      **({"session_id": self.session_id} if self.session_id else {})))
        return events

    def _result(self, raw: Mapping[str, Any]) -> dict[str, Any]:
        result = {k: v for k, v in raw.items() if k != "modelUsage"}
        cost = raw.get("total_cost_usd")
        models = raw.get("modelUsage")
        if isinstance(models, Mapping) and models:
            result["modelUsage"] = {str(name): _model_usage(usage) for name, usage in models.items()}
        else:
            tokens = request_tokens(raw.get("usage")).filled_from(self.usage)
            result["modelUsage"] = {self.model or "grok": {
                "inputTokens": tokens.input_tokens or 0, "outputTokens": tokens.output_tokens or 0,
                "cacheReadInputTokens": tokens.cache_read_tokens or 0,
                "cacheCreationInputTokens": tokens.cache_creation_tokens or 0}}
        if cost == 0 and (any(request_tokens(raw.get("usage")).to_dict().values())
                          or any(any(usage.values()) for usage in result["modelUsage"].values())):
            del result["total_cost_usd"]
        return result


# --- Resident sessions ---


def prune_sessions(home: Path, keep: str) -> None:
    """Remove the sessions in a session ``home`` other than ``keep``: only the session the owner saved can be
    resumed, and a new one replaces it. Grok keeps each in ``sessions/<work directory>/<id>``."""
    sessions = home / "sessions"
    for group in sessions.iterdir() if sessions.is_dir() else []:
        for path in group.iterdir() if group.is_dir() and not group.is_symlink() else []:
            if path.is_dir() and not path.is_symlink() and path.name != keep:
                shutil.rmtree(path, ignore_errors=True)


class ResidentGrok(PerTurnSession):
    """The Manager's or a Planner's Grok session: one headless ``grok`` process per turn, resuming the session
    (``per_turn.PerTurnSession``).

    ``start`` keeps the command (``GrokRuntime.resident_command``, plus
    ``--resume <id>`` for a saved session); each turn runs it with the session
    to resume, the message on stdin. Grok keeps a session in its home, so the
    turns run in a home of the owner's own,
    ``<runtime_root>/.local/grok/sessions/<label>``, given the run home's
    ``config.toml``, rules file and login link before each turn (``_sync_home``);
    the turn that starts a new session removes the owner's earlier ones. A
    changed system prompt restarts the session (its command changes), and the
    resumed session reads the new rules file.

    A turn's own tokens are what its result reports; the session's totals add
    them up.
    """

    runtime_label = "Grok"

    def _split(self, command: list[str]) -> tuple[list[str], str | None, str]:
        if len(command) < 4 or command[0] != "env" or not command[1].startswith("GROK_HOME=") \
                or not command[2].startswith("HOME="):
            raise ValueError("a resident Grok command starts with env GROK_HOME=<home> HOME=<home> <grok>")
        if command[-2] == "--resume":
            return command[:-2], command[-1], command[3]
        return command, None, command[3]

    def _home(self) -> Path:
        source = Path(self._base[1].partition("=")[2])
        return source.parent.parent / "sessions" / session_dir_name(self._label)

    def _turn_argv(self) -> list[str]:
        source = Path(self._base[1].partition("=")[2])
        home = self._home()
        _sync_home(source, home)
        resume = ["--resume", self._session] if self._session else []
        return ["env", f"GROK_HOME={home}", f"HOME={home}", *self._base[3:], *resume]

    def _stream(self) -> GrokStream:
        base = self._base
        return GrokStream(model=base[base.index("-m") + 1] if "-m" in base else None)

    def turn_usage(self, result_event: dict[str, Any] | None) -> Usage:
        """A turn's own tokens, which its result reports; the session's totals add them up."""
        own = run_usage(result_event, price=self._price)
        if result_event is not None:
            self._totals = self._totals.plus(own)
        return own

    def _turn_ended(self, *, new_session: bool) -> None:
        if new_session and self._session:
            prune_sessions(self._home(), self._session)
