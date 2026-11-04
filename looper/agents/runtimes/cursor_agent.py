"""Cursor Agent runs with the operator's login and personal configuration.

Looper owns the run's config, data and plugin directories, but
keeps HOME: Cursor also loads the operator's Cursor and Claude Code content.
A Worker or Reviewer sends one prompt on stdin and reads stream-json; only
assistant text after the last tool call or retry is the run's report.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ... import child_env
from ..run_usage import Usage, reported_totals, run_usage
from . import (
    ApiProtocol, AuthKind, Capability, CostSource, Probe, PromptBlock, ProviderBinding, ReadOnly,
    RunCost, RuntimeAdapter, RuntimeUnsupported, cursor_login, runtime_prompt_text,
)
from .grok import _local_dir, _remove_idle_homes, _write_private, every_named
from .instructions import InstructionFile
from .channel import StreamChannel
from .pricing import usage_event
from .per_turn import PerTurnSession, session_dir_name

if TYPE_CHECKING:
    from ..agent_settings import Launch
    from ..library import AgentLibrary
    from ..profile_registry import LegacyProviderSpec as ProviderSpec
    from . import ResidentSession


HOME_LAYOUT_ID = "cursor-operator-home/v1"
CONFIG_DIR = "config"
DATA_DIR = "data"
PLUGIN_DIR = "looper-plugin"
INSTRUCTIONS_FILE = "looper-instructions.md"
INSTRUCTION_NAMES = ("AGENTS.md", "CLAUDE.md", "CLAUDE.local.md")
PERSONAL_CONFIG = ("cursor", "claude-code")
_PROBE_TIMEOUT_SEC = 10.0
# Cursor CLI 2026.10.22: MCP request() defaults to 6e4 ms, RequestTimeout is -32001.
# Leave ten seconds for the HTTP round trip and serialization.
MCP_MAX_WAIT_SECONDS = 50
_ROUTED_MODEL = re.compile(r"^> Auto routed to ([^\n]+)\n\n?")


def write_run_dir(local: Path, files: Mapping[str, str], *, instructions: str = "") -> Path:
    """Restore a content-named plugin, leaving the CLI's own state alongside it."""
    # Instructions now travel on stdin, but still separate runs with different prompts.
    named = HOME_LAYOUT_ID + "\n" + instructions + "\0" + "\0".join(f"{path}\0{text}" for path, text in sorted(files.items()))
    key = hashlib.sha256(named.encode("utf-8")).hexdigest()
    directory = local / "cursor-agent"
    directory.mkdir(parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    runs = directory / "runs"
    target = runs / key

    def write(run: Path) -> None:
        _write_private(run / INSTRUCTIONS_FILE, instructions)
        for path, text in files.items():
            _write_private(run / PLUGIN_DIR / path, text)
        for name in (CONFIG_DIR, DATA_DIR):
            (run / name).mkdir(exist_ok=True, mode=0o700)
            os.chmod(run / name, 0o700)

    if target.is_dir():
        write(target)
        return target
    runs.mkdir(exist_ok=True, mode=0o700)
    _remove_idle_homes(runs, time.time())
    building: Path | None = Path(tempfile.mkdtemp(prefix=".building.", dir=runs))
    try:
        write(building)
        try:
            os.rename(building, target)
        except OSError:
            if not (target / PLUGIN_DIR / ".cursor-plugin/plugin.json").is_file():
                raise
        else:
            building = None
    finally:
        if building is not None:
            shutil.rmtree(building, ignore_errors=True)
    return target


def mcp_servers(library: AgentLibrary) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """Translate Claude Code's MCP config without changing the servers' HOME."""
    if not library.mcp_config:
        return {}, []
    servers = json.loads(Path(library.mcp_config).read_text(encoding="utf-8")).get("mcpServers") or {}
    translated: dict[str, dict[str, Any]] = {}
    skipped: list[str] = []
    for name, server in sorted(servers.items()):
        transport = server.get("type") or ("stdio" if server.get("command") else "")
        if transport == "stdio":
            entry = {"command": server["command"], "args": list(server.get("args") or [])}
            if "env" in server:
                entry["env"] = dict(server["env"] or {})
        elif transport in ("http", "sse"):
            entry = {"url": server["url"]}
            if transport == "sse":
                entry["type"] = "sse"
            if "headers" in server:
                entry["headers"] = dict(server["headers"] or {})
        else:
            skipped.append(f"MCP server {name}: Cursor Agent has no {transport or 'unknown'} transport")
            continue
        if name == "looper" and transport == "stdio":
            entry["env"] = {**entry.get("env", {}), "LOOPER_MCP_MAX_WAIT_SECONDS": str(MCP_MAX_WAIT_SECONDS)}
        translated[name] = entry
    return translated, skipped


def runtime_notes(servers: list[str]) -> str:
    lines = [
        "# Runtime notes (Cursor Agent)",
        "",
        "Looper runs you through Cursor's CLI. The instructions after these notes are Looper's.",
        "",
        "- Looper reads only your final message. When the instructions ask for a reply or a report in a given shape "
        "(such as a JSON block), put all of it in that final message.",
        "- Rules, skills and tools from the user's own Cursor and Claude Code configuration may also be loaded. "
        "Where they conflict with Looper's instructions, follow Looper's.",
    ]
    if "looper" in servers:
        lines.append("- Looper platform tools include `looper_room_inbox`, `looper_room_post` and other "
                     "`looper_room_*` tools; use the tool names available in Cursor.")
    return "\n".join(lines)


class CursorAgentRuntime(RuntimeAdapter):
    type = "cursor-agent"
    label = "Cursor Agent"
    default_executable = "cursor-agent"
    capabilities = frozenset({Capability.RUN, Capability.RESIDENT_SESSION, Capability.RESUME,
                              Capability.MCP, Capability.ADD_DIRS})
    protocols = frozenset({ApiProtocol.OPENAI_RESPONSES})
    auth_kinds = frozenset({AuthKind.OAUTH})
    login_models = cursor_login.SUGGESTED_MODELS
    effort_levels = ()
    isolation_id = HOME_LAYOUT_ID
    read_only = ReadOnly.CHECK
    runtime_cost_capped = False
    can_replace_system_prompt = False
    loads_operator_config = True

    def probe(self, executable: str) -> Probe:
        with tempfile.TemporaryDirectory(prefix="looper-cursor-probe-") as temporary:
            directory = Path(temporary)
            for name in (CONFIG_DIR, DATA_DIR):
                (directory / name).mkdir(mode=0o700)
            env = {**cursor_login.environment(child_env.inherited()),
                   "CURSOR_CONFIG_DIR": str(directory / CONFIG_DIR), "CURSOR_DATA_DIR": str(directory / DATA_DIR)}
            try:
                done = subprocess.run([executable, "--version"], capture_output=True, text=True,
                                      timeout=_PROBE_TIMEOUT_SEC, check=False, env=env)
            except FileNotFoundError:
                return Probe(None, self.capabilities, f"{executable!r} was not found")
            except (OSError, subprocess.TimeoutExpired) as exc:
                return Probe(None, self.capabilities,
                             f"{executable} --version did not run: {type(exc).__name__}: {exc}")
        first = next((line.strip() for line in (done.stdout or done.stderr).splitlines() if line.strip()), "")
        if done.returncode != 0 or not first:
            return Probe(None, self.capabilities, f"{executable} --version failed (exit code {done.returncode})"
                         + (f": {first}" if first else ""))
        return Probe(first[:200], self.capabilities_for(first))

    def isolation_args(self, cwd: str | Path) -> list[str]:
        return []

    def bind_provider(self, provider: ProviderSpec, model: str, effort: str) -> ProviderBinding:
        if provider.auth is not AuthKind.OAUTH or provider.oauth_runtime_type != self.type:
            raise RuntimeUnsupported(f"Cursor Agent runs with the Cursor login only; provider {provider.name!r} is not it")
        if provider.protocols != self.protocols:
            raise RuntimeUnsupported("Cursor Agent requires the exact openai-responses model protocol")
        if len(provider.catalog) != 1 or provider.catalog[0].name != model:
            raise RuntimeUnsupported("Cursor Agent requires one resolved model matching the launch model")
        if effort:
            raise RuntimeUnsupported("Cursor Agent has no effort setting")
        return ProviderBinding(args=("--model", model), env=provider.env,
                               unset=(*cursor_login.ENV_UNSET, *cursor_login.DIRECTORY_VARS))

    def launch_args(self, launch: Launch) -> list[str]:
        binding = self.bind_provider(launch.profile.provider, launch.model, launch.effort)
        return ["-p", "--output-format", "stream-json", "--force", "--trust", "--sandbox", "disabled",
                "--disable-auto-update", *binding.args]

    def environment(self, launch: Launch) -> dict[str, str]:
        binding = self.bind_provider(launch.profile.provider, launch.model, launch.effort)
        for name in ("HOME", *cursor_login.ENV_UNSET, *cursor_login.DIRECTORY_VARS):
            if name in dict(launch.env) or name in dict(binding.env):
                raise RuntimeUnsupported(f"Cursor Agent runs with the operator's Cursor login: remove {name} "
                                         "from the Agent settings' or the provider's variables")
        return cursor_login.environment({**child_env.inherited(), **dict(launch.env), **dict(binding.env)})

    def _command(self, executable: str, prompt_file: Path, launch: Launch) -> list[str]:
        args = self.launch_args(launch)  # Refuse an unusable provider before writing anything.
        servers, _ = mcp_servers(launch.library)
        files = {
            ".cursor-plugin/plugin.json": json.dumps({"name": "looper", "version": "1",
                                                     "description": "Looper platform tools"}),
        }
        if servers:
            files[".mcp.json"] = json.dumps({"mcpServers": servers}, ensure_ascii=False)
        run = write_run_dir(_local_dir(prompt_file), files, instructions=runtime_prompt_text(
            launch, prompt_file.read_text(encoding="utf-8"), runtime_notes(list(servers))))
        return ["env", f"CURSOR_CONFIG_DIR={run / CONFIG_DIR}", f"CURSOR_DATA_DIR={run / DATA_DIR}", executable,
                *args, "--plugin-dir", str(run / PLUGIN_DIR),
                *(arg for directory in launch.library.plugin_dirs for arg in ("--plugin-dir", directory)),
                *(arg for directory in dict.fromkeys(launch.knowledge.add_dirs) for arg in ("--add-dir", directory))]

    def run_command(self, executable: str, *, cwd: str | Path, prompt_file: Path, launch: Launch,
                    max_turns: int = 0, session_dir: Path | None = None) -> list[str]:
        return self._command(executable, prompt_file, launch)

    def resident_command(self, executable: str, *, cwd: str | Path, prompt_file: Path, launch: Launch,
                         max_turns: int = 0) -> list[str]:
        return self._command(executable, prompt_file, launch)

    def resume_args(self, session_id: str) -> list[str]:
        return ["--resume", session_id]

    def resident_session(self, label: str) -> ResidentSession:
        return ResidentCursor(label=label)

    def instructions_changed(self, command: list[str], *, label: str, session_id: str) -> bool:
        session = ResidentCursor(label=label)
        session._base, _, _ = session._split(command)
        saved = session._saved_session()
        instructions = (session._run_dir() / INSTRUCTIONS_FILE).read_bytes()
        return saved is None or saved.get("session_id") != session_id or \
            saved.get("instructions_sha256") != hashlib.sha256(instructions).hexdigest()

    def run_channel(self, process: asyncio.subprocess.Process, *, launch: Launch, cwd: str | Path,
                    session_dir: Path | None = None, prompt_file: Path | None = None) -> StreamChannel:
        if prompt_file is None:
            raise RuntimeUnsupported("Cursor Agent needs Looper's instruction prompt before starting a run")
        servers, _ = mcp_servers(launch.library)
        instructions = runtime_prompt_text(launch, prompt_file.read_text(encoding="utf-8"), runtime_notes(list(servers)))
        return CursorChannel(self, process, model=launch.model, instructions=instructions)

    async def write_turn(self, stdin: asyncio.StreamWriter, text: str, *, uuid: str | None = None,
                         instructions: str = "") -> None:
        if stdin.is_closing():
            raise RuntimeError("Cursor Agent reads one prompt per run: it takes no user turn while it runs")
        stdin.write((instructions_block(instructions) + "\n\n" + text).encode("utf-8"))
        await stdin.drain()
        stdin.close()

    def parse(self, stdout: asyncio.StreamReader, *, model: str | None = None) -> AsyncIterator[dict[str, Any]]:
        return CursorStream(model=model).events(stdout)

    def prompt_blocks(self, launch: Launch, looper_prompt: str, *, cwd: str | Path | None = None) -> list[PromptBlock]:
        servers, _ = mcp_servers(launch.library)
        return [
            PromptBlock("Default prompt", "runtime", None, "Cursor's default prompt (not shown)"),
            PromptBlock("Operator configuration", "runtime", None,
                        "the operator's own Cursor and Claude Code rules, skills, hooks and MCP servers (not shown)"),
            PromptBlock("Looper instructions", "looper",
                        instructions_block(runtime_prompt_text(launch, looper_prompt, runtime_notes(list(servers)))),
                        "stdin (before the turn prompt)"),
            PromptBlock("Turn prompt", "looper", None, "stdin (after Looper instructions)"),
        ]

    def instruction_files(self, cwd: str | Path) -> tuple[InstructionFile, ...]:
        return tuple(map(InstructionFile, every_named(cwd, INSTRUCTION_NAMES)))

    def tool_summary(self, tool_name: str, tool_input: dict[str, Any]) -> str:
        if tool_name == "shell":
            return f"Running: {str(tool_input.get('command') or '')[:80]}"
        if tool_name == "read":
            return f"Reading: {tool_input.get('path') or ''}"
        if tool_name in ("edit", "write"):
            return f"Editing: {tool_input.get('path') or ''}"
        if tool_name in ("grep", "glob"):
            pattern = tool_input.get("globPattern") if tool_name == "glob" else None
            return f"{tool_name}: {str(pattern or tool_input.get('pattern') or '')[:60]}"
        return f"Tool: {tool_name}"

    def cost(self, usage: Usage, provider: ProviderSpec) -> RunCost:
        return RunCost(usage, CostSource.PROVIDER_PRICE)

    def budget_error(self, result_event: dict[str, Any] | None) -> str | None:
        return None

    async def login_problem(self, provider: ProviderSpec) -> str | None:
        return await cursor_login.launch_problem()

    def result_notes(self, result_event: dict[str, Any] | None) -> dict[str, Any]:
        return ({"routed_model": result_event["routed_model"]}
                if result_event and "routed_model" in result_event else {})

    def launch_record(self, launch: Launch) -> dict[str, Any]:
        record: dict[str, Any] = {"personal_config": list(PERSONAL_CONFIG)}
        if skipped := mcp_servers(launch.library)[1]:
            record["runtime_catalog"] = {"runtime": self.type, "skipped": skipped}
        return record


def instructions_block(instructions: str) -> str:
    return f"<looper_instructions>\n{instructions}\n</looper_instructions>"


class CursorChannel(StreamChannel):
    """Bind instructions to this run, never to the shared runtime adapter."""

    def __init__(self, runtime: CursorAgentRuntime, process: asyncio.subprocess.Process, *,
                 model: str, instructions: str) -> None:
        super().__init__(runtime, process, model=model)
        self._cursor = runtime
        self._instructions = instructions

    async def start(self, prompt: str) -> None:
        assert self._process.stdin is not None
        await self._cursor.write_turn(self._process.stdin, prompt, instructions=self._instructions)


def tool_name(key: str, args: Mapping[str, Any]) -> str:
    if key == "mcpToolCall":
        return f"mcp__{args.get('providerIdentifier') or args.get('server')}__{args.get('toolName') or args.get('name')}"
    return key.removesuffix("ToolCall") or "tool"


def _tool_result(key: str, result: Mapping[str, Any]) -> tuple[str, bool]:
    success = result.get("success")
    if key == "shellToolCall":
        output = success if isinstance(success, Mapping) else result.get("failure")
        output = output if isinstance(output, Mapping) else {}
        text = output.get("interleavedOutput")
        if text is None:
            text = "\n".join(str(output[name]) for name in ("stdout", "stderr") if output.get(name))
        return str(text), "success" not in result or output.get("exitCode") != 0
    output = success if isinstance(success, Mapping) else result.get("failure", result)
    content = output.get("content") if isinstance(output, Mapping) else None
    if key == "mcpToolCall" and isinstance(content, list):
        texts = []
        for item in content:
            if not isinstance(item, Mapping):
                continue
            text = item.get("text")
            if isinstance(text, Mapping):  # protobuf JSON's text oneof
                text = text.get("text")
            elif item.get("type") != "text":
                continue
            if isinstance(text, str):
                texts.append(text)
        return "\n".join(texts), "success" not in result or bool(output.get("isError"))
    return json.dumps(result, ensure_ascii=False), "success" not in result


@dataclass
class CursorStream:
    model: str | None = None
    session_id: str | None = None
    routed_model: str | None = None
    _final: list[str] = field(default_factory=list)
    _thinking: list[str] = field(default_factory=list)
    _open: set[str] = field(default_factory=set)

    async def events(self, stdout: asyncio.StreamReader) -> AsyncIterator[dict[str, Any]]:
        from ..claude_cli import stream_events

        async for raw in stream_events(stdout):
            for event in self.translate(raw):
                yield event

    def _assistant(self, block: dict[str, Any]) -> dict[str, Any]:
        return {"type": "assistant", "session_id": self.session_id, "parent_tool_use_id": None,
                "message": {"role": "assistant", "content": [block]}}

    def translate(self, raw: Mapping[str, Any]) -> list[dict[str, Any]]:
        kind, phase = raw.get("type"), raw.get("subtype")
        if raw.get("session_id"):
            self.session_id = str(raw["session_id"])
        if kind == "system" and phase == "init":
            return [{**raw, "runtime": "cursor-agent", "model": self.model or raw.get("model"),
                     "model_display": raw.get("model")}]
        if kind == "user":
            return []
        if kind == "assistant":
            text = "".join(block.get("text", "") for block in raw.get("message", {}).get("content", [])
                           if block.get("type") == "text")
            if match := _ROUTED_MODEL.match(text):
                if self.routed_model is None:
                    self.routed_model = match[1]
                text = text[match.end():]
            self._final.append(text)
            return [dict(raw)]
        if kind == "thinking" and phase == "delta":
            self._thinking.append(str(raw.get("text") or ""))
            return []
        if kind == "thinking" and phase == "completed":
            text = "".join(self._thinking)
            self._thinking.clear()
            return [self._assistant({"type": "thinking", "thinking": text})] if text else []
        if kind == "tool_call" and phase in ("started", "completed"):
            call = raw.get("tool_call") or {}
            keys = [key for key in call if key.endswith("ToolCall")]
            key = keys[0] if len(keys) == 1 else ""
            detail = call.get(key) or {}
            args = detail.get("args") or {}
            call_id = str(raw.get("call_id") or "")
            events = []
            if phase == "started" or call_id not in self._open:
                self._final.clear()
                self._open.add(call_id)
                events.append(self._assistant({"type": "tool_use", "id": call_id,
                                               "name": tool_name(key, args) if key else "tool",
                                               "input": (args.get("args") or {}) if key == "mcpToolCall" else args}))
            if phase == "completed":
                self._open.discard(call_id)
                content, is_error = _tool_result(key, detail.get("result") or {})
                events.append({"type": "user", "session_id": self.session_id,
                               "message": {"role": "user", "content": [
                                   {"type": "tool_result", "tool_use_id": call_id,
                                    "content": content, "is_error": is_error}]}})
            return events
        if kind in ("connection", "retry"):
            if kind == "retry" and phase in ("starting", "resuming"):
                self._final.clear()
            return [{"type": "system", "subtype": f"cursor_{kind}", "phase": phase,
                     "attempt": raw.get("attempt"), "session_id": self.session_id}]
        if kind == "result":
            usage = raw.get("usage") or {}
            counts = {}
            for internal, external in (("inputTokens", "inputTokens"), ("outputTokens", "outputTokens"),
                                       ("cacheReadInputTokens", "cacheReadTokens"),
                                       ("cacheCreationInputTokens", "cacheWriteTokens")):
                value = usage.get(external)
                counts[internal] = value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0
            result = {key: value for key, value in raw.items() if key != "usage"}
            result.update(result="".join(self._final), modelUsage={self.model or "cursor-agent": counts})
            if self.routed_model is not None:
                result["routed_model"] = self.routed_model
            return [usage_event(self.model, reported_totals(result), session_id=self.session_id), result]
        return [{"type": "system", "subtype": "cursor_event", "session_id": self.session_id, "event": dict(raw)}]


def sync_dir(*, source: Path, home: Path) -> None:
    """Copy Looper's generated input to the owner; an expired run leaves the last copy usable."""
    for directory in (home, home / CONFIG_DIR, home / DATA_DIR):
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(directory, 0o700)
    if not source.is_dir():
        return
    inputs = {Path(INSTRUCTIONS_FILE): (source / INSTRUCTIONS_FILE).read_text(encoding="utf-8")}
    inputs.update({path.relative_to(source): path.read_text(encoding="utf-8")
                   for path in (source / PLUGIN_DIR).rglob("*") if path.is_file()})
    for relative, text in inputs.items():
        target = home / relative
        if not target.is_file() or target.read_text(encoding="utf-8") != text:
            _write_private(target, text)
    for path in (home / PLUGIN_DIR).rglob("*"):
        if path.is_file() and path.relative_to(home) not in inputs:
            path.unlink()


class ResidentCursor(PerTurnSession):
    """One Cursor process per turn, resuming the owner's chat in fixed config and data directories.

    Instructions go on stdin only when starting a chat. The hash saved beside
    its id binds that chat to its original instructions; changed or unknown
    instructions go through the owner's handoff and resume switch. The owner
    copy survives run-directory cleanup and stays unchanged for the handoff.

    Usage assumes the result reports the turn's own tokens, including after
    resume; whether Cursor reports a chat total remains unverified.
    """

    runtime_label = "Cursor Agent"

    def _split(self, command: list[str]) -> tuple[list[str], str | None, str]:
        message = "a resident Cursor Agent command starts with env CURSOR_CONFIG_DIR=<dir> CURSOR_DATA_DIR=<dir> <program>"
        if len(command) < 4 or command[0] != "env" or not command[1].startswith("CURSOR_CONFIG_DIR=") \
                or not command[2].startswith("CURSOR_DATA_DIR="):
            raise ValueError(message)
        index = 4 if command[3].startswith("PATH=") else 3
        if len(command) <= index:
            raise ValueError(message)
        if command[-2] == "--resume":
            return command[:-2], command[-1], command[index]
        return command, None, command[index]

    def _run_dir(self) -> Path:
        return Path(self._base[1].partition("=")[2]).parent

    def _home(self) -> Path:
        return self._run_dir().parent.parent / "sessions" / session_dir_name(self._label)

    def _saved_session(self) -> dict[str, Any] | None:
        try:
            saved = json.loads((self._home() / "session.json").read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            return None
        return saved if isinstance(saved, dict) else None

    def _turn_argv(self) -> list[str]:
        source, home = self._run_dir(), self._home()
        saved = self._saved_session()
        if not self._session:
            sync_dir(source=source, home=home)
        elif source.is_dir() and saved is not None and saved.get("instructions_sha256") == hashlib.sha256(
                (source / INSTRUCTIONS_FILE).read_bytes()).hexdigest():
            sync_dir(source=source, home=home)
        self._instructions = (home / INSTRUCTIONS_FILE).read_text(encoding="utf-8")
        self._instructions_sha256 = hashlib.sha256(self._instructions.encode("utf-8")).hexdigest()
        rest = list(self._base[3:])
        for index in range(1, len(rest)):
            if rest[index - 1] == "--plugin-dir" and rest[index] == str(source / PLUGIN_DIR):
                rest[index] = str(home / PLUGIN_DIR)
        return ["env", f"CURSOR_CONFIG_DIR={home / CONFIG_DIR}", f"CURSOR_DATA_DIR={home / DATA_DIR}",
                *rest, *(["--resume", self._session] if self._session else [])]

    def _turn_input(self, text: str) -> str:
        return text if self._session else instructions_block(self._instructions) + "\n\n" + text

    def _stream(self) -> CursorStream:
        return CursorStream(model=self._base[self._base.index("--model") + 1] if "--model" in self._base else None)

    def turn_usage(self, result_event: dict[str, Any] | None) -> Usage:
        own = run_usage(result_event, price=self._price)
        if result_event is not None:
            self._totals = self._totals.plus(own)
        return own

    def _session_initialized(self) -> None:
        if not self._session:
            return
        home = self._home()
        _write_private(home / "session.json", json.dumps({"session_id": self._session,
                                                       "instructions_sha256": self._instructions_sha256,
                                                       "cwd": self._cwd}))

    def _turn_ended(self, *, new_session: bool) -> None:
        if not self._session:
            return
        # A replacement that was interrupted after init may finish its first turn on resume.
        home = self._home()
        chats = home / CONFIG_DIR / "chats"
        for group in chats.iterdir() if chats.is_dir() else []:
            for path in group.iterdir() if group.is_dir() and not group.is_symlink() else []:
                if path.is_dir() and not path.is_symlink() and path.name != self._session:
                    shutil.rmtree(path, ignore_errors=True)
