"""The OMP runtime: oh-my-pi's ``omp`` CLI in print mode (``-p --mode json``), one process per turn.

Verified against omp 18.4.3 (the multi-runtime RFC, section 6):

- **A run** is ``omp -p --mode json --no-session ...``: the prompt comes on
  stdin, read to its end, and the process runs it and exits. It takes no user
  turn while it runs, so a Worker cannot be steered (``write_turn`` closes
  stdin after the prompt).
- **A resident session** (the Manager, a Planner) is one such process per
  turn, keeping its session in a directory of the owner's own and resuming it
  (``--session-dir <dir> --resume <id>``, ``ResidentOmp``). Each process's
  first line names the session; a session that is gone makes omp exit 1
  without an event, which the tiers read as a session that could not be
  resumed.
- **Isolation**: every process runs with ``HOME`` set to a home of Looper's
  own, ``<runtime_root>/.local/omp/homes/<sha256>`` (``write_home``). omp keeps
  everything of a user under ``HOME``: ``~/.omp`` (settings, models, sessions,
  logs, installed plugins), the other tools' directories it imports from
  (``~/.claude``, ``~/.codex``, ``~/.agents``...) and ``~/.env``, so none of
  the operator's is read. The variables that would point it back at them are
  not passed (``DIRECTORY_VARS``, ``RESTORED_VARS``). The agent's shell
  commands get the operator's ``HOME`` and those variables back, without the
  provider's key, through omp's shell prefix (``shell_prefix``). omp looks for
  a project's own content (skills, ``AGENTS.md``, ``.omp``) from the work
  directory up to its git repository's root; outside a repository it would
  climb past the operator's home up to ``/``, so there the sources that climb
  are turned off (``CLIMBING_SOURCES``) and the work directory's own
  ``AGENTS.md`` or ``CLAUDE.md`` goes into the system prompt instead
  (``instruction_files``, ``instructions``). The homes share one copy of omp's
  native library (``<runtime_root>/.local/omp/natives``), which omp would
  otherwise unpack into each (about 167 MB).
- **Settings** go in a ``--config`` overlay (``overlay``), which a project's
  own ``.omp/config.yml`` cannot override: only the agent's model is enabled,
  for every model role, with no fallback to another; no approvals; MCP tools as
  ordinary tools (``tools.xdev`` off); no background jobs, so a subagent's
  tokens come with its tool's result; no update check, no issue reports.
- **The system prompt** is ``--append-system-prompt <file>``: after omp's own
  prompt and the project's context: the work directory's instruction file
  when omp does not read it itself, a short block of runtime notes
  (``runtime_notes``), then Looper's prompt, which stays runtime-neutral.
- **The provider** is ``models.yml``'s provider ``looper``: its endpoint, its
  API (``API_NAMES``) and the agent's model at no price (Looper prices the
  tokens); its ``apiKey`` names the variable that holds the key (``KEY_ENV``),
  which omp reads first, so the key is only in the environment.
- **The catalog** (RFC P9): the plugin directories load natively
  (``--plugin-dir``: their skills, commands and agents). Their hooks and output
  styles do not load, and their own MCP servers are turned off
  (``disabledExtensions``): on every runtime Looper's MCP servers come from the
  catalog only, and here they go in a plugin of the home (``MCP_PLUGIN``). What
  was left out is in the run's record (``launch_record``). Knowledge is read by
  the paths the prompt's index names; ``--add-dir`` is not used, because omp
  reads the ``AGENTS.md`` files above each added directory.
- **Events and usage**: ``OmpStream`` turns ``--mode json`` events into
  Looper's internal events (Claude Code's stream-json shapes). Each model
  request's tokens come with its message, so the context size is known and the
  soft cap applies between requests; a subagent's (the ``task`` tool) come with
  the tool's result. Background requests (compressing skill descriptions into
  routing hints, compaction summaries) are not reported.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shlex
import shutil
import subprocess
import tempfile
import time
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

from ..run_usage import Usage, run_usage
from . import (
    ApiProtocol, AuthKind, Capability, Probe, ProviderBinding, RunCost, RuntimeAdapter, RuntimeUnsupported,
    instructions,
)
from .instructions import Delivery, InstructionFile
from .per_turn import PerTurnSession, session_dir_name
from .pricing import usage_event

if TYPE_CHECKING:
    from ..profile_registry import ProviderSpec
    from ..agent_settings import Launch
    from ..library import AgentLibrary

_PROBE_TIMEOUT_SEC = 10.0
# The provider's id in the generated models.yml, and the variable that holds its key.
PROVIDER_ID = "looper"
KEY_ENV = "LOOPER_PROVIDER_API_KEY"
# Names the layout of a generated home in each run's record; change it when the layout changes.
HOME_LAYOUT_ID = "omp-home/v1"
OVERLAY = "looper.yml"
# The plugin of a home that holds the run's MCP servers.
MCP_PLUGIN = "looper"
# omp's prefix to every shell command it runs.
SHELL_PREFIX_VAR = "PI_SHELL_PREFIX"
# What would point omp at the operator's own directories: never passed to omp.
DIRECTORY_VARS = ("OMP_PROFILE", "PI_PROFILE", "PI_CODING_AGENT_DIR", "PI_CODING_AGENT_SESSION_DIR", "PI_CONFIG_DIR",
                  "PI_CONFIG_FILES")
# What omp would read the operator's configuration through, which the agent's shell commands get back. HOME is
# the run's home; the others are not passed to omp.
RESTORED_VARS = ("HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME", "CLAUDE_CONFIG_DIR",
                 "COPILOT_HOME", "COPILOT_CUSTOM_INSTRUCTIONS_DIRS")
# The discovery sources that look for a project's content above the work directory: turned off outside a git
# repository, where they would climb to the filesystem's root.
CLIMBING_SOURCES = ("native", "claude", "agents", "agents-md", "claude-md")
# Each protocol's API in models.yml, in the order one is chosen from a provider's protocols.
API_NAMES: tuple[tuple[ApiProtocol, str], ...] = (
    (ApiProtocol.OPENAI_RESPONSES, "openai-responses"),
    (ApiProtocol.OPENAI_CHAT, "openai-completions"),
    (ApiProtocol.ANTHROPIC_MESSAGES, "anthropic-messages"),
)
# Where an API key provider without a base_url sends its requests: the vendor's own endpoint.
DEFAULT_BASE_URLS = {"openai-responses": "https://api.openai.com/v1",
                     "openai-completions": "https://api.openai.com/v1",
                     "anthropic-messages": "https://api.anthropic.com"}
# The model roles omp may pick a model for; each is the agent's model.
MODEL_ROLES = ("default", "smol", "slow", "plan", "vision")
# The parts of a Claude Code plugin omp does not load.
_PLUGIN_PARTS_LEFT_OUT = ("hooks", "output-styles")
# How long a home's prompt files and logs may go unwritten before they are removed: far beyond any run.
IDLE_SEC = 24 * 3600


class OmpRuntime(RuntimeAdapter):
    type = "omp"
    label = "OMP"
    default_executable = "omp"
    # Verified against omp 18.4.3: Claude plugins' skills, commands and agents load (no hooks); compaction events;
    # tokens only, per request.
    capabilities = frozenset({Capability.RUN, Capability.RESIDENT_SESSION, Capability.RESUME,
                              Capability.CLAUDE_PLUGINS, Capability.MCP, Capability.ADD_DIRS,
                              Capability.COMPACTION_EVENTS})
    protocols = frozenset(protocol for protocol, _ in API_NAMES)
    # API keys only: omp's own logins are not used.
    auth_kinds = frozenset({AuthKind.API_KEY})
    isolation_id = HOME_LAYOUT_ID
    # Set by the binding and the isolation: a provider's own variables cannot set them.
    provider_env_owned: ClassVar[frozenset[str]] = frozenset({KEY_ENV, SHELL_PREFIX_VAR, *DIRECTORY_VARS})

    def probe(self, executable: str) -> Probe:
        """``<executable> --version``, with an empty ``HOME`` of its own so nothing of the operator's is read."""
        home = tempfile.mkdtemp(prefix="looper-omp-probe-")
        try:
            done = subprocess.run([executable, "--version"], capture_output=True, text=True,
                                  timeout=_PROBE_TIMEOUT_SEC, check=False, env={**_omp_env(os.environ), "HOME": home})
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
        """None: omp's isolation is its ``HOME`` and the overlay Looper writes there (``overlay``)."""
        return []

    def bind_provider(self, provider: ProviderSpec, model: str, effort: str) -> ProviderBinding:
        """The model (``looper/<model>``) and the effort (``--thinking``) as flags; the provider's own variables,
        then its key in ``KEY_ENV``, which the generated ``models.yml`` names. The endpoint and the API are in
        ``models.yml`` (``models_yml``)."""
        if provider.auth is not AuthKind.API_KEY:
            raise RuntimeUnsupported(f"OMP runs with an API key provider only; provider {provider.name!r} is not one")
        provider_api(provider)
        if not provider.api_key:
            raise RuntimeUnsupported(f"provider {provider.name!r} has no API key: set one in Settings")
        return ProviderBinding(
            args=("--model", f"{PROVIDER_ID}/{model}", *(("--thinking", effort) if effort else ())),
            env=(*provider.env, (KEY_ENV, provider.api_key)),
            unset=(*DIRECTORY_VARS, *RESTORED_VARS[1:]),
        )

    def launch_args(self, launch: Launch) -> list[str]:
        """The print-mode flags of every run, the model and the effort, and the installed plugin directories. The
        home's overlay, prompt and MCP plugin come with the command (``run_command``)."""
        binding = self.bind_provider(launch.profile.provider, launch.model, launch.effort)
        return [
            "-p", "--mode", "json", "--no-title",
            *binding.args,
            *(arg for directory in launch.library.plugin_dirs for arg in ("--plugin-dir", directory)),
        ]

    def environment(self, launch: Launch) -> dict[str, str]:
        """The server's environment and the settings' variables, less what the isolation removes, then the
        provider's binding and the shell prefix that gives the agent's shell commands back what was removed."""
        binding = self.bind_provider(launch.profile.provider, launch.model, launch.effort)
        visible = {**os.environ, **dict(launch.env)}
        return {**{k: v for k, v in visible.items() if k not in binding.unset}, **dict(binding.env),
                SHELL_PREFIX_VAR: shell_prefix(visible)}

    def _command(self, executable: str, cwd: str | Path, prompt_file: Path, launch: Launch) -> list[str]:
        local = _local_dir(prompt_file)
        visible = {**os.environ, **dict(launch.env)}
        mcp = mcp_config(launch.library, visible)
        home = write_home(local, models_yml(launch), overlay(launch, outside_repository=outside_repository(cwd)), mcp)
        project = instructions.injected_section(self.instruction_files(cwd), self.label)
        prompt = write_prompt(home, "\n\n---\n\n".join(
            part for part in (project, runtime_notes(), Path(prompt_file).read_text(encoding="utf-8")) if part))
        return [
            "env", f"HOME={home}", executable, *self.launch_args(launch),
            "--config", str(home / OVERLAY), "--append-system-prompt", str(prompt),
            *(["--plugin-dir", str(home / "plugins" / MCP_PLUGIN)] if mcp else []),
        ]

    def resident_command(self, executable: str, *, cwd: str | Path, prompt_file: Path, launch: Launch,
                         max_turns: int = 0) -> list[str]:
        """The command of the turns of a resident session, without its session directory and the session to resume
        (``ResidentOmp`` adds both). omp has no cap on a turn's model calls, so ``max_turns`` is unused."""
        return self._command(executable, cwd, prompt_file, launch)

    def resume_args(self, session_id: str) -> list[str]:
        return ["--resume", session_id]

    def run_command(self, executable: str, *, cwd: str | Path, prompt_file: Path, launch: Launch,
                    max_turns: int = 0) -> list[str]:
        """One run: no session kept (``--no-session``), the prompt read from stdin."""
        return [*self._command(executable, cwd, prompt_file, launch), "--no-session"]

    def resident_session(self, label: str) -> ResidentOmp:
        return ResidentOmp(label=label)

    async def write_turn(self, stdin: asyncio.StreamWriter, text: str, *, uuid: str | None = None) -> None:
        """Write a run's prompt and close stdin, which is how omp knows the prompt ended. There is no second user
        turn: writing one raises ``RuntimeError``."""
        if stdin.is_closing():
            raise RuntimeError("OMP reads one prompt per run: it takes no user turn while it runs")
        stdin.write(text.encode("utf-8"))
        await stdin.drain()
        stdin.close()

    def parse(self, stdout: asyncio.StreamReader, *, model: str | None = None) -> AsyncIterator[dict[str, Any]]:
        """A run's events; their tokens are named after the run's ``model``."""
        return OmpStream(model=model).events(stdout)

    def tool_summary(self, tool_name: str, tool_input: dict[str, Any]) -> str:
        if tool_name == "bash":
            return f"Running: {str(tool_input.get('command') or '')[:80]}"
        if tool_name == "read":
            return f"Reading: {tool_input.get('path', '?')}"
        if tool_name == "write":
            return f"Writing: {tool_input.get('path', '?')}"
        if tool_name in ("edit", "ast_edit"):
            return f"Editing: {', '.join(edited_paths(tool_name, tool_input))[:80] or '?'}"
        if tool_name in ("grep", "glob"):
            return f"{tool_name}: {str(tool_input.get('pattern') or tool_input.get('path') or '')[:60]}"
        if tool_name == "web_search":
            return f"Searching: {str(tool_input.get('query') or '')[:60]}"
        if tool_name == "todo":
            return "Updating its plan"
        if tool_name == "task":
            return "Delegating to subagents"
        return f"Tool: {tool_name}"

    def cost(self, usage: Usage, provider: ProviderSpec) -> RunCost:
        """omp reports tokens only: the run's ``usage`` as priced from its ``modelUsage`` at the provider's prices
        (``cost_source``, ``Launch.price``)."""
        return RunCost(usage, self.cost_source(provider))

    def budget_error(self, result_event: dict[str, Any] | None) -> str | None:
        """Why Looper stopped the run at its per-run soft cap; omp has no spend cap of its own."""
        from .. import claude_cli

        return claude_cli.budget_error(result_event) if (result_event or {}).get(claude_cli.SOFT_CAP) else None

    def instruction_files(self, cwd: str | Path) -> tuple[InstructionFile, ...]:
        """One file a directory of the project, ``AGENTS.md`` before ``CLAUDE.md``, which omp reads itself in a
        repository; outside one, where its discovery is off, the work directory's, put in the system prompt."""
        delivery = Delivery.INJECTED if outside_repository(cwd) else Delivery.NATIVE
        return tuple(InstructionFile(path, delivery) for path in instructions.first_per_directory(
            cwd, (instructions.AGENTS_MD, instructions.CLAUDE_MD)))

    def launch_record(self, launch: Launch) -> dict[str, Any]:
        """What omp could not load of the installed items, when there is any (RFC P9)."""
        skipped = left_out(launch.library)
        return {"runtime_catalog": {"runtime": self.type, "skipped": list(skipped)}} if skipped else {}


# --- The provider and the settings ---


def provider_api(provider: ProviderSpec) -> str:
    """The API in ``models.yml`` for ``provider``: the first of ``API_NAMES`` it serves."""
    api = next((name for protocol, name in API_NAMES if protocol in provider.protocols), None)
    if api is None:
        raise RuntimeUnsupported(f"OMP speaks {', '.join(sorted(p.value for p, _ in API_NAMES))}; provider "
                                 f"{provider.name!r} serves none of them")
    return api


def models_yml(launch: Launch) -> dict[str, Any]:
    """The home's ``models.yml``: the provider ``looper`` with the agent's model, priced at nothing (Looper prices
    the tokens). Its context window comes from the provider's catalog when it gives one; a model the catalog lists
    without effort levels does not reason."""
    provider = launch.profile.provider
    api = provider_api(provider)
    entry = provider.catalog_model(launch.model)
    model: dict[str, Any] = {
        "id": launch.model, "name": launch.model,
        "reasoning": not (entry is not None and entry.efforts is not None and not entry.efforts),
        "input": ["text"],
        "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
    }
    if entry is not None and entry.context_window:
        model["contextWindow"] = entry.context_window
    return {"providers": {PROVIDER_ID: {"baseUrl": provider.base_url or DEFAULT_BASE_URLS[api], "apiKey": KEY_ENV,
                                        "api": api, "models": [model]}}}


def overlay(launch: Launch, *, outside_repository: bool) -> dict[str, Any]:
    """The home's settings overlay (``--config``, the module's notes)."""
    model = f"{PROVIDER_ID}/{launch.model}"
    settings: dict[str, Any] = {
        "enabledModels": [model],
        "modelRoles": {role: model for role in MODEL_ROLES},
        "retry": {"modelFallback": False},
        "tools": {"approvalMode": "yolo", "xdev": False},
        "async": {"enabled": False},
        "startup": {"checkUpdate": False},
        "dev": {"autoqa": False},
    }
    if servers := plugin_mcp_servers(launch.library):
        settings["disabledExtensions"] = [f"mcp:{name}" for name in servers]
    if outside_repository:
        settings["disabledProviders"] = list(CLIMBING_SOURCES)
    return settings


def outside_repository(cwd: str | Path) -> bool:
    """Whether ``cwd`` has no ``.git`` in it or in a directory above it, as omp looks for one."""
    directory = Path(cwd).absolute()
    return not any((candidate / ".git").exists() for candidate in (directory, *directory.parents))


def shell_prefix(visible: Mapping[str, str]) -> str:
    """omp's prefix to the agent's shell commands: the ``RESTORED_VARS`` of ``visible`` (the environment Looper
    runs agents with) back, and the provider's key gone."""
    restored = [f"export {name}={shlex.quote(visible[name])};" for name in RESTORED_VARS if visible.get(name)]
    return " ".join([*restored, f"unset {KEY_ENV};"])


def _omp_env(visible: Mapping[str, str]) -> dict[str, str]:
    """``visible`` without the variables that point omp at the operator's directories."""
    return {k: v for k, v in visible.items() if k not in DIRECTORY_VARS and k not in RESTORED_VARS[1:]}


# --- The catalog (RFC P9) ---


def _plugin_manifest(directory: Path, names: tuple[str, ...]) -> dict[str, Any]:
    for name in names:
        try:
            manifest = json.loads((directory / name).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(manifest, dict):
            return manifest
    return {}


def _server_map(raw: Any) -> dict[str, Any]:
    """The servers of an MCP configuration: ``{"mcpServers": {...}}`` or the map itself."""
    if not isinstance(raw, dict):
        return {}
    if "mcpServers" in raw:
        return raw["mcpServers"] if isinstance(raw["mcpServers"], dict) else {}
    return raw


@lru_cache(maxsize=256)
def _plugin_servers(directory: Path) -> tuple[str, ...]:
    """The MCP servers omp would start from the plugin in ``directory``, as it names them (``<plugin>:<server>``):
    a manifest's ``mcpServers`` (a map, or a file in the plugin), else the plugin's ``.mcp.json``."""
    plugin = str(_plugin_manifest(directory, (".claude-plugin/plugin.json", "plugin.json")).get("name") or "") \
        or directory.name
    servers: dict[str, Any] = {}
    declared = False
    for manifest_dir in (".omp-plugin", ".claude-plugin"):
        pointer = _plugin_manifest(directory, (f"{manifest_dir}/plugin.json",)).get("mcpServers")
        if isinstance(pointer, dict):
            servers, declared = pointer, True
            break
        if isinstance(pointer, str) and pointer.strip():
            servers, declared = _read_servers(directory / pointer.strip()), True
            break
    if not declared:
        servers = _read_servers(directory / ".mcp.json")
    return tuple(f"{plugin}:{name}" for name, server in servers.items() if isinstance(server, dict))


def _read_servers(path: Path) -> dict[str, Any]:
    try:
        return _server_map(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return {}


def plugin_mcp_servers(library: AgentLibrary) -> tuple[str, ...]:
    """The MCP servers the run's plugins bring, which are turned off. Read from the read-only copies
    ``library.load_installed`` wrote, which are named by their content, so the result is cached."""
    return tuple(name for directory in library.plugin_dirs for name in _plugin_servers(Path(directory)))


def left_out(library: AgentLibrary) -> tuple[str, ...]:
    """What omp does not load of the run's installed items, in words for the run's record."""
    skipped = []
    for directory in map(Path, library.plugin_dirs):
        if parts := [part for part in _PLUGIN_PARTS_LEFT_OUT if (directory / part).exists()]:
            skipped.append(f"plugin {directory.name}: {', '.join(parts)} (OMP cannot load them)")
    skipped += [f"MCP server {name}: a plugin's own (Looper's MCP servers come from the catalog)"
                for name in plugin_mcp_servers(library)]
    return tuple(skipped)


def mcp_config(library: AgentLibrary, visible: Mapping[str, str]) -> dict[str, Any] | None:
    """The run's MCP servers (its ``--mcp-config`` file, ``mcp.write_config``) for the home's plugin, None without
    any. omp starts a stdio server with its own environment, so each one gets ``visible``'s ``RESTORED_VARS`` back
    unless it sets them itself."""
    if not library.mcp_config:
        return None
    servers = _server_map(json.loads(Path(library.mcp_config).read_text(encoding="utf-8")))
    restored = {name: visible[name] for name in RESTORED_VARS if visible.get(name)}
    for server in servers.values():
        if isinstance(server, dict) and server.get("command"):
            server["env"] = {**restored, **(server.get("env") or {})}
    return {"mcpServers": servers} if servers else None


def runtime_notes() -> str:
    """What only an OMP agent needs to know, before Looper's own prompt."""
    return "\n".join([
        "# Runtime notes (OMP)",
        "",
        "Looper runs you through OMP. The instructions after these notes are Looper's.",
        "",
        "- Looper reads only your final message. When the instructions ask for a reply or a report in a given shape "
        "(such as a JSON block), put all of it in that final message.",
    ])


# --- What a run's home holds ---


def _local_dir(prompt_file: Path) -> Path:
    """``<runtime_root>/.local``: the prompt file is ``<runtime_root>/.local/prompts/<sha256>.md``
    (``SystemPrompt.write_file``)."""
    return Path(prompt_file).absolute().parent.parent


def _omp_dir(local: Path) -> Path:
    """``<runtime_root>/.local/omp``, readable by this user only: its homes hold MCP servers' values."""
    directory = local / "omp"
    directory.mkdir(parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
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


def _json(value: Any) -> str:
    """``value`` as JSON, which omp reads as YAML."""
    return json.dumps(value, ensure_ascii=False, indent=1, sort_keys=True) + "\n"


def write_home(local: Path, models: Mapping[str, Any], settings: Mapping[str, Any],
               mcp: Mapping[str, Any] | None) -> Path:
    """The home of the runs of these ``models.yml``, overlay and MCP servers: ``<local>/omp/homes/<sha256>``,
    written once and shared by every run of the same settings; omp keeps its own state there too. Its
    ``.omp/natives`` links to the native library every home shares. A server start removes a home no running
    process names (``read_only_copies``)."""
    files = {".omp/agent/models.yml": _json(models), OVERLAY: _json(settings)}
    if mcp:
        files[f"plugins/{MCP_PLUGIN}/.claude-plugin/plugin.json"] = _json({"name": MCP_PLUGIN})
        files[f"plugins/{MCP_PLUGIN}/.mcp.json"] = _json(mcp)
    key = hashlib.sha256(json.dumps([HOME_LAYOUT_ID, files], sort_keys=True).encode("utf-8")).hexdigest()
    omp = _omp_dir(local)
    natives = omp / "natives"
    natives.mkdir(exist_ok=True)
    homes = omp / "homes"
    target = homes / key
    if (target / OVERLAY).is_file():
        return target
    homes.mkdir(exist_ok=True)
    building: Path | None = Path(tempfile.mkdtemp(prefix=".building.", dir=homes))
    try:
        for name, text in files.items():
            _write_private(building / name, text)
        (building / ".omp" / "natives").symlink_to(natives, target_is_directory=True)
        try:
            os.rename(building, target)
        except OSError:
            if not (target / OVERLAY).is_file():
                raise
        else:
            building = None
    finally:
        if building is not None:
            shutil.rmtree(building, ignore_errors=True)
    return target


def _remove_idle_files(directory: Path, now: float) -> None:
    """Remove the files in ``directory`` nothing has written to for ``IDLE_SEC``."""
    if not directory.is_dir():
        return
    for path in directory.iterdir():
        try:
            if path.is_file() and not path.is_symlink() and now - path.stat().st_mtime > IDLE_SEC:
                path.unlink()
        except OSError:
            continue


def write_prompt(home: Path, text: str) -> Path:
    """The file of a run's system prompt in ``home``: ``prompts/<sha256>.md``, written once. Writing a new one
    removes the home's prompt files and omp's logs that nothing has written to for a day; a run reads its
    prompt when it starts, and a resident session's next turn writes its prompt again when it is gone."""
    path = home / "prompts" / f"{hashlib.sha256(text.encode('utf-8')).hexdigest()}.md"
    if path.is_file():
        return path
    now = time.time()
    _remove_idle_files(path.parent, now)
    _remove_idle_files(home / ".omp" / "logs", now)
    _write_private(path, text)
    return path


# --- Events ---


# omp's file-editing tools, each naming the files it changes (``edited_paths``).
EDIT_TOOLS = frozenset({"write", "edit", "ast_edit"})


def edited_paths(tool_name: str, tool_input: Mapping[str, Any]) -> list[str]:
    """The files one of omp's file-editing tool calls names: ``write``'s ``path``, ``ast_edit``'s ``paths``, and
    each ``[path#TAG]`` header of an ``edit`` patch. omp's internal URLs (``agent://``, a message to a subagent)
    are no files."""
    if tool_name == "write":
        paths = [str(tool_input.get("path") or "")]
    elif tool_name == "ast_edit":
        paths = [str(path) for path in tool_input.get("paths") or []]
    elif tool_name == "edit":
        paths = [line.strip()[1:-1].rpartition("#")[0] for line in str(tool_input.get("input") or "").splitlines()
                 if line.strip().startswith("[") and line.strip().endswith("]") and "#" in line]
    else:
        return []
    return [path for path in dict.fromkeys(paths) if path and "://" not in path]


def _count(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def omp_usage(usage: Any) -> Usage:
    """omp's token counts of a request (or a subagent's): ``input`` is the uncached input."""
    usage = usage if isinstance(usage, Mapping) else {}
    return Usage(input_tokens=_count(usage.get("input")), output_tokens=_count(usage.get("output")),
                 cache_read_tokens=_count(usage.get("cacheRead")),
                 cache_creation_tokens=_count(usage.get("cacheWrite")))


def _text(content: Any) -> str:
    """The text blocks of a message's ``content``, joined."""
    if isinstance(content, str):
        return content
    return "\n".join(str(block.get("text") or "") for block in content or []
                     if isinstance(block, Mapping) and block.get("type") == "text")


@dataclass
class OmpStream:
    """One omp process's ``--mode json`` events as Looper's internal events (Claude Code's stream-json shapes).

    - the session header (``type: "session"``) → ``system``/``init`` with the ``session_id``;
    - an assistant message (``message_end``) → an ``assistant`` event with its text, thinking and tool calls
      (``tool_use``) and the request's usage in Claude Code's terms (the context size), then a ``usage`` event with
      its tokens; a tool's result message → a ``user`` ``tool_result``;
    - a ``task`` tool's end → a ``usage`` event with its subagents' tokens;
    - a compaction that ran → ``system``/``compact_boundary``;
    - the agent's end → a ``result``: successful unless its last message stopped on an error or was aborted, its
      ``result`` the last message's text, its ``modelUsage`` the tokens of this process;
    - deltas, starts and the turn's own bookkeeping are dropped; anything else is a ``system`` event the record
      keeps.

    A process that ends without the agent's end gives no ``result``.
    """

    model: str | None = None  # the run's model, which names its tokens
    session_id: str | None = None
    usage: Usage = field(default_factory=Usage)  # this process's tokens so far
    _last_text: str = ""
    _last_stop: str | None = None
    _last_error: str | None = None

    _DROPPED: ClassVar[frozenset[str]] = frozenset({
        "agent_start", "turn_start", "turn_end", "message_start", "message_update", "tool_execution_start",
        "tool_execution_update", "tool_stream_update", "advisor_cost_changed"})

    async def events(self, stdout: asyncio.StreamReader) -> AsyncIterator[dict[str, Any]]:
        from ..claude_cli import stream_events

        async for raw in stream_events(stdout):
            for event in self.translate(raw):
                yield event

    def _base(self) -> dict[str, Any]:
        return {"session_id": self.session_id} if self.session_id else {}

    def translate(self, raw: Mapping[str, Any]) -> list[dict[str, Any]]:
        kind = raw.get("type")
        if kind == "session":
            self.session_id = str(raw.get("id") or "") or None
            return [{"type": "system", "subtype": "init", **self._base(), "runtime": "omp", "model": self.model,
                     "cwd": raw.get("cwd")}]
        if kind in self._DROPPED:
            return []
        if kind == "message_end":
            message = raw.get("message")
            return self._message(message) if isinstance(message, Mapping) else []
        if kind == "tool_execution_end":
            return self._tool_end(raw)
        if kind == "auto_compaction_end":
            result = raw.get("result")
            if isinstance(result, Mapping) and not raw.get("skipped") and not raw.get("aborted"):
                return [{"type": "system", "subtype": "compact_boundary", **self._base(),
                         "compact_metadata": {"trigger": "auto", "pre_tokens": result.get("tokensBefore")}}]
        if kind == "agent_end":
            return [] if raw.get("isTerminal") is False else [self._result()]
        return [{"type": "system", "subtype": "omp_event", **self._base(), "event": dict(raw)}]

    def _message(self, message: Mapping[str, Any]) -> list[dict[str, Any]]:
        role = message.get("role")
        if role == "assistant":
            return self._assistant(message)
        if role == "toolResult":
            return [{"type": "user", **self._base(), "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": str(message.get("toolCallId") or ""),
                 "content": _text(message.get("content")), "is_error": bool(message.get("isError"))}]}}]
        return []  # the user's message, which Looper wrote

    def _assistant(self, message: Mapping[str, Any]) -> list[dict[str, Any]]:
        blocks: list[dict[str, Any]] = []
        for block in message.get("content") or []:
            if not isinstance(block, Mapping):
                continue
            if block.get("type") == "text":
                blocks.append({"type": "text", "text": str(block.get("text") or "")})
            elif block.get("type") == "thinking":
                blocks.append({"type": "thinking", "thinking": str(block.get("thinking") or "")})
            elif block.get("type") == "toolCall":
                arguments = block.get("arguments")
                blocks.append({"type": "tool_use", "id": str(block.get("id") or ""), "name": str(block.get("name")),
                               "input": dict(arguments) if isinstance(arguments, Mapping) else {}})
        self._last_stop = str(message.get("stopReason") or "") or None
        self._last_error = str(message.get("errorMessage") or "") or None
        if text := _text(message.get("content")):
            self._last_text = text
        tokens = omp_usage(message.get("usage"))
        events: list[dict[str, Any]] = []
        if blocks:
            events.append({"type": "assistant", **self._base(), "parent_tool_use_id": None, "message": {
                "role": "assistant", "model": message.get("model") or self.model, "content": blocks,
                "usage": {"input_tokens": tokens.input_tokens, "output_tokens": tokens.output_tokens,
                          "cache_read_input_tokens": tokens.cache_read_tokens,
                          "cache_creation_input_tokens": tokens.cache_creation_tokens}}})
        if tokens != omp_usage(None):
            events.append(self._usage(tokens))
        return events

    def _usage(self, tokens: Usage, **extra: Any) -> dict[str, Any]:
        self.usage = self.usage.plus(tokens)
        return usage_event(self.model, tokens, **self._base(), **extra)

    def _tool_end(self, raw: Mapping[str, Any]) -> list[dict[str, Any]]:
        """A ``task`` tool's subagents' tokens, which come only with its result."""
        result = raw.get("result")
        details = result.get("details") if isinstance(result, Mapping) else None
        if raw.get("toolName") != "task" or not isinstance(details, Mapping) or "usage" not in details:
            return []
        tokens = omp_usage(details.get("usage"))
        return [self._usage(tokens, parent_tool_use_id=str(raw.get("toolCallId") or ""))] \
            if tokens != omp_usage(None) else []

    def _result(self) -> dict[str, Any]:
        tokens = self.usage
        model_usage = {self.model or "omp": {
            "inputTokens": tokens.input_tokens or 0, "outputTokens": tokens.output_tokens or 0,
            "cacheReadInputTokens": tokens.cache_read_tokens or 0,
            "cacheCreationInputTokens": tokens.cache_creation_tokens or 0}}
        if self._last_stop in ("error", "aborted"):
            return {"type": "result", "subtype": "error_during_execution", "is_error": True, "result": "",
                    **self._base(), "modelUsage": model_usage,
                    "errors": [self._last_error or f"the run ended: {self._last_stop}"]}
        return {"type": "result", "subtype": "success", "is_error": False, "result": self._last_text,
                **self._base(), "modelUsage": model_usage}


# --- Resident sessions ---


def prune_sessions(directory: Path, keep: str) -> None:
    """Remove the sessions in an owner's session ``directory`` other than ``keep``: only the session the owner
    saved can be resumed, and a new one replaces it."""
    if not directory.is_dir():
        return
    for path in directory.iterdir():
        if keep in path.name:
            continue
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path, ignore_errors=True)
        else:
            path.unlink(missing_ok=True)


class ResidentOmp(PerTurnSession):
    """The Manager's or a Planner's OMP session: one ``omp -p`` process per turn, resuming the session
    (``per_turn.PerTurnSession``).

    ``start`` keeps the command (``OmpRuntime.resident_command``, plus
    ``--resume <id>`` for a saved session); each turn runs it with
    ``--session-dir`` and the session to resume, the message on stdin. The
    session lives in a directory of the owner's own,
    ``<runtime_root>/.local/omp/sessions/<label>``, beside the homes; the turn
    that starts a new session removes the owner's earlier ones, and a server
    start removes the directory of an owner whose saved session is not an OMP
    one (``read_only_copies``). A changed system prompt restarts the session
    (its command changes), and the resumed session runs with the new prompt.

    omp reports each request's tokens, so a turn's own are what its result
    reports, and the session's totals add them up.
    """

    runtime_label = "OMP"

    def _split(self, command: list[str]) -> tuple[list[str], str | None, str]:
        if len(command) < 3 or command[0] != "env" or not command[1].startswith("HOME="):
            raise ValueError("a resident OMP command starts with env HOME=<home> <omp>")
        if command[-2] == "--resume":
            return command[:-2], command[-1], command[2]
        return command, None, command[2]

    def _session_dir(self) -> Path:
        home = Path(self._base[1].partition("=")[2])
        return home.parent.parent / "sessions" / session_dir_name(self._label)

    def _turn_argv(self) -> list[str]:
        directory = self._session_dir()
        directory.mkdir(parents=True, exist_ok=True)
        os.chmod(directory, 0o700)
        resume = ["--resume", self._session] if self._session else []
        return [*self._base, "--session-dir", str(directory), *resume]

    def _stream(self) -> OmpStream:
        base = self._base
        model = base[base.index("--model") + 1] if "--model" in base else None
        return OmpStream(model=model.removeprefix(f"{PROVIDER_ID}/") if model else None)

    def turn_usage(self, result_event: dict[str, Any] | None) -> Usage:
        """A turn's own tokens, which its result reports; the session's totals add them up."""
        own = run_usage(result_event, price=self._price)
        if result_event is not None:
            self._totals = self._totals.plus(own)
        return own

    def _turn_ended(self, *, new_session: bool) -> None:
        if new_session and self._session:
            prune_sessions(self._session_dir(), self._session)
