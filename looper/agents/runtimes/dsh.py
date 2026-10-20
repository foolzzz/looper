"""DSH provider policy, private owner homes and literal prompts; the executable adapter is not registered yet."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import tempfile
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar
from uuid import uuid4

from ... import child_env
from ..run_usage import Usage
from . import (
    ApiProtocol, AuthKind, PromptBlock, ProviderBinding, RuntimeAdapter, RuntimeUnsupported, instructions,
    runtime_prompt_text,
)
from .instructions import Delivery, InstructionFile
from .pricing import usage_event

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
_USAGE_FIELDS = {"input_tokens": "inputTokens", "output_tokens": "outputTokens",
                 "cache_read_tokens": "cacheReadTokens", "cache_creation_tokens": "cacheWriteTokens"}


def _count(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


@dataclass
class DshStream:
    """Project headless JSONL, withholding the result until EOF and a verified process exit.

    Fields follow the installed ``dsh-headless/json-stream`` projection, not Session's durable log.
    ``final`` is lossless answer text, even on failure. It is never itself success evidence.
    The caller owns process cleanup, timeout and stderr draining; without ``process`` this parser
    fails closed. The executable adapter/channel and resident integration are not registered yet.
    Consumers must keep an empty result empty; ``answer_text``'s generic fallback would reuse progress.
    """

    model: str | None = None
    session_id: str | None = None
    usage: Usage = field(default_factory=Usage, init=False)
    _steps: set[tuple[int, int]] = field(default_factory=set, init=False)
    _open_steps: set[tuple[int, int] | None] = field(default_factory=set, init=False)
    _root_usage_complete: bool = field(default=True, init=False)
    _final: str | None = field(default=None, init=False)
    _reason: dict[str, Any] | None = field(default=None, init=False)
    _errors: list[str] = field(default_factory=list, init=False)
    _diagnostics: int = field(default=0, init=False)
    _cancelled: bool = field(default=False, init=False)

    _DIAGNOSTIC_LIMIT: ClassVar[int] = 8
    _DIAGNOSTIC_BYTES: ClassVar[int] = 2048

    def cancel(self) -> None:
        """Latch a caller's stop before terminating the process; a late final cannot clear it."""
        self._cancelled = True

    async def events(self, stdout: asyncio.StreamReader, *,
                     process: asyncio.subprocess.Process | None = None) -> AsyncIterator[dict[str, Any]]:
        from ..claude_cli import iter_lines

        def invalid_constant(value: str) -> None:
            raise ValueError(f"Invalid JSON constant: {value}")

        try:
            # stream_events skips malformed JSON; headless must instead report it as a failed run.
            # iter_lines also handles the unbounded final, beyond StreamReader's line limit.
            async for line in iter_lines(stdout):
                if not line.strip():
                    continue
                try:
                    raw = json.loads(line, parse_constant=invalid_constant)
                    if not isinstance(raw, dict) or not isinstance(raw.get("type"), str):
                        raise ValueError("expected an event object")
                except (ValueError, UnicodeError):
                    self._error("Invalid DSH JSON event")
                    continue
                for event in self.translate(raw):
                    yield event
            returncode = await process.wait() if process is not None else None
        except asyncio.CancelledError:
            self.cancel()
            raise
        if self._open_steps or not self._steps or self._errors:
            # A missing step_end (including EOF mid-request) must invalidate the budget too.
            # This is an unknown increment, never another copy of the result's totals.
            self._root_usage_complete = False
            yield usage_event(self.model, Usage(), **self._base(), usage_complete=False,
                              root_usage_complete=False, cost_basis="observed", raw_usage=None)
        yield self._result(returncode)

    def _base(self) -> dict[str, Any]:
        return {"session_id": self.session_id} if self.session_id else {}

    def _bounded(self, text: str) -> str:
        return text.encode("utf-8", errors="replace")[:self._DIAGNOSTIC_BYTES].decode("utf-8", errors="ignore")

    def _error(self, message: str) -> None:
        if len(self._errors) < self._DIAGNOSTIC_LIMIT:
            self._errors.append(self._bounded(message))

    def _diagnostic(self, raw: Mapping[str, Any], *, limited: bool = False) -> list[dict[str, Any]]:
        if limited:
            if self._diagnostics >= self._DIAGNOSTIC_LIMIT:
                return []
            self._diagnostics += 1
        text = json.dumps(raw, ensure_ascii=False)
        preview = self._bounded(text)
        return [{"type": "system", "subtype": "dsh_event", **self._base(),
                 "event_type": str(raw.get("type"))[:64], "event": preview,
                 "truncated": raw.get("truncated") is True or preview != text}]

    def _assistant(self, block: dict[str, Any], flags: dict[str, Any]) -> list[dict[str, Any]]:
        return [{"type": "assistant", **self._base(), **flags, "parent_tool_use_id": None,
                 "message": {"role": "assistant", "model": self.model, "content": [{**block, **flags}]}}]

    @staticmethod
    def _step_key(raw: Mapping[str, Any]) -> tuple[int, int] | None:
        turn, step = _count(raw.get("turn")), _count(raw.get("step"))
        return (turn, step) if turn is not None and step is not None else None

    def _step_usage(self, raw: Mapping[str, Any]) -> list[dict[str, Any]]:
        key = self._step_key(raw)
        if key is not None and key in self._steps:
            return []
        if key is not None:
            self._steps.add(key)
        self._open_steps.discard(key)
        reported = raw.get("usage")
        values = reported if isinstance(reported, Mapping) else {}
        tokens = Usage(**{name: _count(values.get(wire)) for name, wire in _USAGE_FIELDS.items()})
        complete = (key is not None and raw.get("truncated") is not True
                    and all(getattr(tokens, name) is not None for name in _USAGE_FIELDS))
        # Optional counters remain metadata: reasoning is already in output, retries in step usage.
        if any(name in values and _count(values[name]) is None for name in ("totalTokens", "reasoningTokens")):
            complete = False
        self._root_usage_complete = self._root_usage_complete and complete
        self.usage = self.usage.plus(tokens)
        # Headless root JSONL has not been shown to cover compaction, children and search.
        # Even a fully observed root step cannot establish completeness of the total bill.
        return [usage_event(self.model, tokens, **self._base(), turn=raw.get("turn"), step=raw.get("step"),
                            raw_usage=reported, usage_complete=False, root_usage_complete=complete,
                            cost_basis="observed", **({"truncated": True} if raw.get("truncated") is True else {}))]

    def translate(self, raw: Mapping[str, Any]) -> list[dict[str, Any]]:
        """Project one committed record. Only ``events`` publishes a terminal result."""
        kind = raw.get("type")
        flags = {"truncated": True} if raw.get("truncated") is True else {}
        if kind == "final":
            if not isinstance(raw.get("text"), str):
                self._error("Invalid DSH final text")
            elif self._final is not None:
                self._error("Duplicate DSH final")
            else:
                self._final = raw["text"]
            if flags:
                self._error("DSH final is truncated")
            return []
        if kind == "error":
            self._error(str(raw.get("message") or "DSH error event without a message"))
            return self._diagnostic(raw, limited=True)
        if kind == "status":
            phase = raw.get("phase")
            if phase == "turn_start":
                self._reason = None
            elif phase == "turn_end":
                reason = raw.get("reason")
                self._reason = dict(reason) if isinstance(reason, Mapping) else None
            elif phase == "step_start":
                key = self._step_key(raw)
                if key not in self._steps:
                    self._open_steps.add(key)
            elif phase == "step_end":
                return [*self._diagnostic(raw), *self._step_usage(raw)]
            return self._diagnostic(raw, limited=phase not in ("turn_start", "step_start", "step_end", "turn_end"))
        if kind == "session" and isinstance(raw.get("sessionId"), str) and raw["sessionId"]:
            self.session_id = raw["sessionId"]
            return [{"type": "system", "subtype": "init", **self._base(), **flags, "runtime": "dsh",
                     "model": self.model, "cwd": raw.get("cwd")}]
        if kind in ("text", "thinking") and isinstance(raw.get("text"), str):
            return self._assistant({"type": kind, "thinking" if kind == "thinking" else "text": raw["text"]}, flags)
        if kind == "tool_call" and isinstance(raw.get("callId"), str) and isinstance(raw.get("tool"), str):
            if "input" in raw or flags:
                # Invalid JSON arguments remain a string in headless; a bounded event can omit input.
                return self._assistant({"type": "tool_use", "id": raw["callId"], "name": raw["tool"],
                                        "input": raw.get("input")}, flags)
        if kind == "tool_result" and isinstance(raw.get("callId"), str) and raw.get("status") in ("completed", "error"):
            if isinstance(raw.get("result"), str) or flags:
                return [{"type": "user", **self._base(), **flags, "message": {"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": raw["callId"], "content": raw.get("result"),
                     "is_error": raw["status"] == "error", **flags}]}}]
        if kind in ("session", "text", "thinking", "tool_call", "tool_result") and not flags:
            self._error(f"Invalid DSH {kind} event")
        # Unknown types are diagnostic only; never infer a compaction boundary from them.
        # Bounded ordinary records may legitimately retain only their type and truncated flag.
        return self._diagnostic(raw, limited=kind not in ("session", "text", "thinking", "tool_call", "tool_result"))

    def _result(self, returncode: int | None) -> dict[str, Any]:
        errors = list(self._errors)
        if self._cancelled:
            errors.append("DSH run was cancelled")
        if returncode is None:
            errors.append("DSH process exit was not verified")
        elif returncode != 0:
            errors.append(f"DSH process failed with exit code {returncode}")
        if self._final is None:
            errors.append("DSH stream ended without final")
        if not self._reason or self._reason.get("kind") != "completed":
            errors.append("DSH turn did not complete: " + self._bounded(json.dumps(self._reason, ensure_ascii=False)))
        return {"type": "result", "subtype": "error_during_execution" if errors else "success",
                "is_error": bool(errors), "result": self._final if self._final is not None else "",
                **self._base(), "stop_reason": self._reason, **({"errors": errors} if errors else {}),
                "modelUsage": {self.model or "dsh": {
                    wire: value for name, wire in zip(_USAGE_FIELDS, (
                        "inputTokens", "outputTokens", "cacheReadInputTokens", "cacheCreationInputTokens"))
                    if (value := getattr(self.usage, name)) is not None}},
                "usage_complete": False, "root_usage_complete": bool(self._steps) and self._root_usage_complete,
                "cost_basis": "observed"}


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
