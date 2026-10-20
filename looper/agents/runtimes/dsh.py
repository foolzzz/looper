"""DSH provider policy, private owner homes and literal prompts; the executable adapter is not registered yet."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar
from uuid import uuid4

from ... import child_env
from . import (
    ApiProtocol, AuthKind, PromptBlock, ProviderBinding, RuntimeAdapter, RuntimeUnsupported, instructions,
    runtime_prompt_text,
)
from .instructions import Delivery, InstructionFile

if TYPE_CHECKING:
    from ..agent_settings import Launch
    from ..library import AgentLibrary
    from ..profile_registry import ProviderSpec


PROVIDER_ID = "deepseek-official"
KEY_ENV = "DEEPSEEK_API_KEY"
DEFAULT_BASE_URL = "https://api.deepseek.com/anthropic"
MODEL_MAP = {
    "deepseek-flash": "deepseek-flash",
    "deepseek-flash[1m]": "deepseek-flash",
    "deepseek-v4-pro": "deepseek-v4-pro",
    "deepseek-v4-pro[1m]": "deepseek-v4-pro",
}

HOME_LAYOUT_ID = "dsh-home/v1"
# The private paths and provider overlay are authoritative, even over explicit settings/provider env.
_DIRECTORY_VARS = frozenset({
    "HOME", *child_env.RUNTIMES,
    "OMP_PROFILE", "PI_PROFILE", "PI_CODING_AGENT_DIR", "PI_CODING_AGENT_SESSION_DIR", "PI_CONFIG_DIR",
    "PI_CONFIG_FILES", "AGENTS_HOME", "AGENT_HOME", "AGENT_DIR", "AGENT_DIRS",
})
_OWNED_PREFIXES = ("DSH_", "XDG_", "DEEPSEEK_", "ANTHROPIC_", "OPENAI_")


class DshRuntime(RuntimeAdapter):
    """Prompt hooks for the future executable adapter; deliberately abstract and unregistered."""

    type = "dsh"
    label = "DSH"
    can_replace_system_prompt = False

    def prompt_blocks(self, launch: Launch, looper_prompt: str, *, cwd: str | Path | None = None) -> list[PromptBlock]:
        return [PromptBlock("Default prompt", "runtime", None, "DSH's default prompt (not shown; append only)"),
                PromptBlock("Looper instructions", "looper", _prompt_text(looper_prompt, launch, cwd=cwd),
                            "literal system prompt section (order 20000)")]

    def instruction_files(self, cwd: str | Path) -> tuple[InstructionFile, ...]:
        # Native agent-instructions discovery is disabled by the managed overlay.
        return tuple(InstructionFile(path, Delivery.INJECTED) for path in instructions.first_per_directory(
            cwd, (instructions.AGENTS_MD, instructions.CLAUDE_MD)))


def runtime_notes() -> str:
    return ("# Runtime notes (DSH)\n\n"
            "Looper runs you through DSH. Put the complete reply or report requested by Looper's instructions "
            "in your final message; Looper reads only that final message.")


def _prompt_text(looper_prompt: str, launch: Launch, *, cwd: str | Path | None = None) -> str:
    project = (instructions.injected_section(launch.runtime.instruction_files(cwd), launch.runtime.label)
               if cwd is not None else "")
    return runtime_prompt_text(launch, looper_prompt, runtime_notes(), project, keep_empty=False)


def bootstrap_files(executable: str, looper_prompt: str, launch: Launch, *,
                    cwd: str | Path | None = None) -> dict[str, str]:
    """An immutable entry module, resolved from the detected installation, never the private home's npm tree.

    Config and launcher-provided task/session/json values stay owned by the official headless runner.
    Both launch materialization and preview use ``_prompt_text``; JSON encodes text without interpolation.
    """
    try:
        anchor = Path(shutil.which(executable) or executable).resolve(strict=True)
        if not anchor.is_file():
            raise OSError
    except OSError:
        raise RuntimeUnsupported("DSH bootstrap requires an installed executable") from None
    module = Path(__file__).with_name("dsh_bootstrap.mjs").resolve().as_uri()
    mcp = mcp_config(launch.library, role=launch.role.value)
    options = json.dumps({"executable": str(anchor), "text": _prompt_text(looper_prompt, launch, cwd=cwd),
                          "mcp": mcp.servers if mcp else []},
                         ensure_ascii=False)
    return {"looper-bootstrap.mjs": f"import {{ createBootstrap }} from {json.dumps(module)};\n"
            f"export const {{ name, Config, inject, apply, platformTools, diagnostics }} = "
            f"await createBootstrap({options});\n"}


def bootstrap_entry(home: DshHome) -> dict[str, str]:
    """Patch only the required runner's module; retain its config and headlessStartup dependency."""
    return {"id": "headless-runner", "name": (home.config / "looper-bootstrap.mjs").as_uri()}


def _external_environment(values: Mapping[str, str]) -> dict[str, str]:
    return {k: v for k, v in values.items() if k not in _DIRECTORY_VARS and not k.startswith(_OWNED_PREFIXES)}


@dataclass(frozen=True)
class DshMcpConfig:
    """Secret-free bootstrap inputs and values supplied only to this run's child environment."""

    servers: tuple[dict[str, Any], ...]
    env: dict[str, str] = field(repr=False)


def _platform_tools(role: str) -> dict[str, Any]:
    from ..ops import planner, room, service, vault
    from ..room import MemberKind

    if role == "manager":
        required = allowed = {tool.name for tool in service.MANAGER_TOOLS}
        groups = []
    elif role == "planner":
        required = {tool.name for tool in planner.TOOLS}
        allowed = required | {tool.name for tool in planner.ROLE_TOOLS}
    elif role in ("worker", "reviewer"):
        required = set()
        allowed = set()
    else:
        raise RuntimeUnsupported("DSH platform MCP requires a known agent role")
    if role != "manager":
        # Room and vault are independently enabled. A partially published group must fail startup too.
        room_names = {tool.name for tool in room.tools_for(MemberKind.WORK if role == "worker" else MemberKind.REVIEW)}
        allowed |= room_names | vault.TOOL_NAMES
        groups = [{"any": sorted(room_names), "all": sorted(room_names)},
                  {"any": sorted(vault.TOOL_NAMES), "all": [vault.SEARCH, vault.READ]}]
    return {"requiredTools": sorted(required), "expectedTools": sorted(allowed), "toolGroups": groups}


def mcp_config(library: AgentLibrary, *, role: str | None = None) -> DshMcpConfig:
    """Translate the catalog's MCP map without persisting env/header values or mistaking the agent API for MCP.

    References are stable across token rotation; every child gets freshly read run values, including on resume.
    The required runner owns all entries, so there is no second, optional platform MCP activation.
    """
    if not library.mcp_config:
        raise RuntimeUnsupported("DSH MCP configuration requires the Looper stdio bridge")

    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise RuntimeUnsupported("DSH MCP configuration contains a duplicate name")
            result[key] = value
        return result

    try:
        raw = json.loads(Path(library.mcp_config).read_text(encoding="utf-8"), object_pairs_hook=unique)
        servers = raw["mcpServers"]
        if not isinstance(servers, dict) or "looper" not in servers:
            raise ValueError
    except (OSError, ValueError, KeyError, TypeError):
        raise RuntimeUnsupported("DSH MCP configuration requires the Looper stdio bridge") from None
    values: dict[str, str] = {}
    translated = []
    platform = _platform_tools(role or library.role or "")
    for index, (name, server) in enumerate(sorted(servers.items(), key=lambda item: item[0] != "looper")):
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", name) or not isinstance(server, dict):
            raise RuntimeUnsupported("DSH MCP server has an invalid name or configuration")
        own = name == "looper"
        transport = server.get("type", "stdio" if server.get("command") else "http")
        config: dict[str, Any] = {"serverName": name}
        if transport == "stdio" and isinstance(server.get("command"), str) and server["command"]:
            args = server.get("args", [])
            if not isinstance(args, list) or not all(isinstance(arg, str) for arg in args):
                raise RuntimeUnsupported("DSH MCP stdio args must be strings")
            config.update(transport="stdio", command=server["command"], args=args)
            if server.get("cwd"):
                config["cwd"] = server["cwd"]
            secret_field, reference_field = "env", "envRefs"
        elif not own and transport in ("http", "sse") and isinstance(server.get("url"), str):
            # DSH has no legacy SSE transport; its schema rejects that optional entry with a diagnostic.
            config.update(transport="streamable-http" if transport == "http" else "sse", url=server["url"])
            secret_field, reference_field = "headers", "headerRefs"
        else:
            raise RuntimeUnsupported("DSH Looper MCP requires stdio; catalog MCP requires a command or HTTP URL")
        secrets = server.get(secret_field) or {}
        if not isinstance(secrets, dict) or not all(isinstance(k, str) and isinstance(v, str)
                                                   for k, v in secrets.items()):
            raise RuntimeUnsupported("DSH MCP env and headers must contain string values")
        if own and any(not secrets.get(key, "").strip() for key in ("LOOPER_AGENT_URL", "LOOPER_AGENT_TOKEN")):
            raise RuntimeUnsupported("DSH Looper MCP needs this run's LOOPER_AGENT_URL and LOOPER_AGENT_TOKEN")
        refs = {}
        for number, (key, value) in enumerate(sorted(secrets.items())):
            reference = f"LOOPER_DSH_MCP_SECRET_{index}_{secret_field.upper()}_{number}"
            refs[key] = reference
            values[reference] = value
        config[reference_field] = refs
        if own:
            config.update(platform)
        translated.append(config)
    return DshMcpConfig(tuple(translated), values)


@dataclass(frozen=True)
class DshHome:
    """One owner's mutable Session root and one immutable configuration bundle. Neither holds secrets."""

    root: Path
    config: Path

    @property
    def paths(self) -> dict[str, str]:
        home = self.root / "home"
        return {"HOME": str(home), "DSH_HOME": str(self.root / "dsh"), "DSH_AGENTS_HOME": str(self.root / "agents"),
                "XDG_CONFIG_HOME": str(home / ".config"), "XDG_DATA_HOME": str(home / ".local" / "share"),
                "XDG_STATE_HOME": str(home / ".local" / "state"), "XDG_CACHE_HOME": str(home / ".cache"),
                "XDG_RUNTIME_DIR": str(home / ".run"), "XDG_CONFIG_DIRS": str(home / ".config-dirs"),
                "XDG_DATA_DIRS": str(home / ".data-dirs")}


def environment(launch: Launch, home: DshHome) -> dict[str, str]:
    """Allowed inheritance, explicit settings, binding, then locked paths. The overlay must also explicitly set
    the validated ``baseURL`` and policy: dsh's launcher reads project .env independently of this environment."""
    binding = bind_provider(launch.profile.provider, launch.model, launch.effort)
    visible = _external_environment({**child_env.inherited(), **dict(launch.env)})
    mcp = mcp_config(launch.library, role=launch.role.value)
    return {**{k: v for k, v in visible.items() if k not in binding.unset}, **dict(binding.env),
            **(mcp.env if mcp else {}), **home.paths}


def _private_dir(directory: Path, *, root: Path) -> None:
    """Create managed directories without an intermediate world-readable parent or following a symlink."""
    if directory.is_symlink():
        raise RuntimeUnsupported("DSH private directory cannot be a symlink")
    if directory != root:
        _private_dir(directory.parent, root=root)
    directory.mkdir(mode=0o700, exist_ok=True)
    directory.chmod(0o700)


def _write_private(path: Path, text: str) -> None:
    """Publish a 0600 file by replacement; exceptions never include the supplied content."""
    fd, building = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.write(text)
        os.replace(building, path)
    finally:
        Path(building).unlink(missing_ok=True)


def write_home(local: Path, files: Mapping[str, str], *, owner: str | None = None) -> DshHome:
    """Materialize already-rendered, secret-free overlay/modules below ``<local>/dsh/owners/<owner hash>``.

    Pass the complete resident owner label (or the kept run's session_dir identity); omit it only for a new run.
    Content changes select a new configs bundle, never a new Session root. Secrets must be environment references.
    A bundle becomes visible only after every file is written, including when several callers materialize it.
    """
    for name in files:
        path = Path(name)
        if path.is_absolute() or not path.parts or ".." in path.parts:
            raise ValueError("DSH configuration filename must stay within its bundle")
    owner_key = hashlib.sha256((owner if owner is not None else uuid4().hex).encode("utf-8")).hexdigest()
    content_key = hashlib.sha256(json.dumps([HOME_LAYOUT_ID, dict(files)], ensure_ascii=False,
                                          sort_keys=True).encode("utf-8")).hexdigest()
    local = Path(local).absolute()
    root = local / "dsh" / "owners" / owner_key
    config = root / "configs" / content_key
    home = DshHome(root, config)
    for directory in (local, local / "dsh", root.parent, root, config.parent):
        _private_dir(directory, root=local)
    for path in home.paths.values():
        _private_dir(Path(path), root=local)
    if config.is_symlink():
        raise RuntimeUnsupported("DSH configuration bundle cannot be a symlink")
    if config.is_dir():
        return home
    building = Path(tempfile.mkdtemp(prefix=".building.", dir=config.parent))
    try:
        for name, text in files.items():
            _private_dir((building / name).parent, root=building)
            _write_private(building / name, text)
        try:
            os.rename(building, config)
        except OSError:
            if not config.is_dir():
                raise
    finally:
        if building.exists():
            shutil.rmtree(building)
    return home


@dataclass(frozen=True)
class DshProviderPolicy:
    """Validated overlay inputs; no credentials and no inference of a provider's vendor."""

    route: ClassVar[str] = PROVIDER_ID
    model: str
    effort: str
    base_url: str


def validate_provider(provider: ProviderSpec, model: str, effort: str | None = None) -> DshProviderPolicy:
    """Check configuration without reading a key; also used at the launch boundary."""
    from ..profile_registry import RegistryError, _base_url

    if provider.auth is not AuthKind.API_KEY:
        raise RuntimeUnsupported("DSH requires an API key provider")
    if ApiProtocol.ANTHROPIC_MESSAGES not in provider.protocols:
        raise RuntimeUnsupported("DSH requires an anthropic-messages provider")
    # Check the original catalog name before normalizing: an alias does not
    # grant membership to the corresponding raw model (or vice versa).
    if provider.models and model not in provider.models:
        raise RuntimeUnsupported(f"model {model!r} is not in provider {provider.name!r}'s catalog")
    if model not in MODEL_MAP:
        raise RuntimeUnsupported(f"DSH supports only {', '.join(MODEL_MAP)}; got {model!r}")
    if effort not in (None, "", "low", "high", "max"):
        raise RuntimeUnsupported("DSH effort must be low, high or max, or empty for high")
    try:
        _base_url(provider.base_url)
    except RegistryError as exc:
        raise RuntimeUnsupported(str(exc)) from None
    # DSH appends the Messages path itself. Preserve explicit gateway roots,
    # including /v1 and trailing slashes, rather than rewriting a Chat endpoint.
    return DshProviderPolicy(MODEL_MAP[model], effort or "high", provider.base_url or DEFAULT_BASE_URL)


def bind_provider(provider: ProviderSpec, model: str, effort: str | None = None) -> ProviderBinding:
    """Validate a launch and inject its explicit key, never an ambient login or key."""
    validate_provider(provider, model, effort)
    if not provider.api_key or not provider.api_key.strip():
        raise RuntimeUnsupported(f"provider {provider.name!r} has no API key: set one in Settings")
    return ProviderBinding(env=(*_external_environment(dict(provider.env)).items(), (KEY_ENV, provider.api_key)))
