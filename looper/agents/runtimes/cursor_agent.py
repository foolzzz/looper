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
import logging
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass, field, replace
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from ... import child_env
from ..run_usage import Usage, reported_totals, run_usage
from . import (
    ApiProtocol, AuthKind, Capability, CostSource, Launcher, Probe, PromptBlock, ProviderBinding, ReadOnly,
    RunCost, RuntimeAdapter, RuntimeUnsupported, cursor_login, runtime_prompt_text, ttadk,
)
from .grok import _local_dir, _remove_idle_homes, _write_private, every_named
from .instructions import InstructionFile
from .channel import StreamChannel
from .pricing import usage_event
from .per_turn import PerTurnSession, session_dir_name

if TYPE_CHECKING:
    from ..claude_cli import CapStop, ResidentTurn
    from ..agent_settings import Launch
    from ..library import AgentLibrary
    from ..profile_registry import LegacyProviderSpec as ProviderSpec
    from . import ResidentSession
    from .cancel import TurnCancel


logger = logging.getLogger(__name__)


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
RETRY_SUMMARY = "Cursor's CLI gave up after reconnecting; retried once"
CONTINUATION_PROMPT = (
    "[Looper] Your previous reply was cut off by a connection error before it reached Looper. "
    "Continue from where you stopped. If your step's work is already done, send your final JSON report now; "
    "do not redo work that is already in the files or commits."
)
# Observed when Cursor's CLI gives up after its reconnects, without a result.
_TRANSIENT_ERRORS = ("RetriableError: WritableIterable is closed",)


class CursorFailure(StrEnum):
    TRANSIENT_BACKEND = "transient_backend"


def cursor_failure(result: dict[str, Any] | None, stderr: str) -> CursorFailure | None:
    if result is None and any(message in stderr for message in _TRANSIENT_ERRORS):
        return CursorFailure.TRANSIENT_BACKEND
    return None


def retry_event(stderr: str, *, attempt: int = 1, mode: Literal["resume", "restart"] | None = None) -> dict[str, Any]:
    summary = f"Cursor's CLI gave up after reconnecting; resumed session (attempt {attempt}/3)" \
        if mode == "resume" else RETRY_SUMMARY
    return {"type": "system", "subtype": "cursor_run_retry", "attempt": attempt,
            **({"mode": mode} if mode is not None else {}),
            "reason": CursorFailure.TRANSIENT_BACKEND.value, "summary": summary,
            "stderr": ttadk.error_tail(stderr)[-400:]}


def retry_stderr(first: str, *following: str) -> str:
    # Share the tiers' 1,000-character diagnostic limit across every attempt.
    attempts = (first, *following)
    headers = ["Attempt 1 (RetriableError):\n", *(f"Attempt {i}:\n" for i in range(2, len(attempts) + 1))]
    budget = min(400, (1000 - sum(map(len, headers)) - len(attempts) + 1) // len(attempts))
    return "\n".join(header + ttadk.error_tail(stderr)[-budget:] for header, stderr in zip(headers, attempts))


def write_ttadk_shim(local: Path) -> Path:
    """Make TTADK's `agent` resolve to Cursor only within Looper's run directories."""
    directory = local / "cursor-agent" / "ttadk-bin"
    target = shutil.which("cursor-agent", path=child_env.inherited().get("PATH"))
    if target is None:
        raise RuntimeUnsupported("Cursor's CLI (`cursor-agent`) is not on the server's PATH")
    real = os.path.realpath(target)
    if Path(real).is_relative_to(directory.resolve()):
        raise RuntimeUnsupported("the agent shim would run itself")
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(directory, 0o700)
    text = (
        "#!/bin/sh\n"
        "# Written by Looper: TTADK runs `agent` for cursor-cli; this runs Cursor's CLI in Looper's Cursor directories.\n"
        'if [ -z "$CURSOR_CONFIG_DIR" ] || [ -z "$CURSOR_DATA_DIR" ]; then\n'
        '  echo "Looper\'s agent shim runs Cursor\'s CLI only in a Looper run '
        '(CURSOR_CONFIG_DIR and CURSOR_DATA_DIR set)" >&2\n'
        "  exit 2\n"
        "fi\n"
        "unset TTADK_JWT_TOKEN\n"
        f'exec {shlex.quote(real)} "$@"\n'
    )
    path = directory / "agent"
    if not path.exists() or path.read_text(encoding="utf-8") != text:
        _write_private(path, text)
    os.chmod(path, 0o700)
    return directory


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

    runs.mkdir(exist_ok=True, mode=0o700)
    _remove_idle_homes(runs, time.time())
    if target.is_dir():
        write(target)
        return target
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


def run_tmpdir(directory: Path) -> Path:
    # Cursor falls back to TMPDIR for worker-server sockets when DATA_DIR is long.
    # Keep enough room for its cwd slug within macOS's 104-byte socket limit.
    key = hashlib.sha256(str(directory.absolute()).encode("utf-8")).hexdigest()[:12]
    return Path("/tmp") / f"looper-cur-{key}"


def prepare_tmpdir(directory: Path) -> Path:
    target = run_tmpdir(directory)
    target.mkdir(mode=0o700, exist_ok=True)
    os.chmod(target, 0o700)
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
    first_event_known = True
    type = "cursor-agent"
    label = "Cursor Agent"
    default_executable = "cursor-agent"
    capabilities = frozenset({Capability.RUN, Capability.RESIDENT_SESSION, Capability.RESUME,
                              Capability.MCP, Capability.ADD_DIRS})
    protocols = frozenset({ApiProtocol.OPENAI_RESPONSES})
    auth_kinds = frozenset({AuthKind.OAUTH})
    launchers = frozenset({Launcher.NATIVE, Launcher.TTADK})
    ttadk_tool = "cursor-cli"
    ttadk_runtime_name = "ttadk-cursor-agent"
    login_models = cursor_login.SUGGESTED_MODELS
    effort_levels = ()
    isolation_id = HOME_LAYOUT_ID
    read_only = ReadOnly.CHECK
    runtime_cost_capped = False
    can_replace_system_prompt = False
    loads_operator_config = True

    def is_model_event(self, event: Mapping[str, Any]) -> bool:
        if event.get("type") == "system" and event.get("subtype") == "cursor_event":
            raw = event.get("event") or {}
            if raw.get("type") == "thinking" and raw.get("subtype") == "delta":
                return bool(raw.get("text"))
            return super().is_model_event(raw)
        return super().is_model_event(event)

    def reported_model(self, event: Mapping[str, Any]) -> str | None:
        model = (event.get("routed_model") if event.get("type") == "result" else
                 event.get("model_display") if event.get("type") == "system" and event.get("subtype") == "init" else None)
        return model if isinstance(model, str) and model.strip() else None

    def ttadk_problem(self) -> ttadk.TtadkProblem | None:
        return ttadk.TtadkProblem.WRONG_TOOL if ttadk.tool_command("cursor-cli") != "agent" else None

    def probe_for(self, executable: str, launcher: Launcher) -> Probe:
        if launcher is Launcher.NATIVE:
            return self.probe(executable)
        version, error = ttadk.probe_version(executable)
        if error:
            return Probe(None, self.capabilities, error)
        native = self.probe("cursor-agent")
        error = native.error
        if not error and (command := ttadk.tool_command("cursor-cli")) != "agent":
            error = f"TTADK's cursor-cli command is `{command}`, not `agent`: Looper will not launch it"
        return Probe(f"ttadk {version} / cursor-agent {native.version or 'unknown'}", native.capabilities, error)

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
        env = cursor_login.environment({**child_env.inherited(), **dict(launch.env), **dict(binding.env)})
        return ttadk.environment(env) if launch.profile.runtime.launcher is Launcher.TTADK else env

    def _program(self, executable: str, launch: Launch, run: Path) -> list[str]:
        program = ["env", f"CURSOR_CONFIG_DIR={run / CONFIG_DIR}", f"CURSOR_DATA_DIR={run / DATA_DIR}"]
        if launch.profile.runtime.launcher is Launcher.TTADK:
            shim_dir = write_ttadk_shim(run.parent.parent.parent)
            program += [f"PATH={shim_dir}{os.pathsep}{self.environment(launch).get('PATH', '')}",
                        *ttadk.prefix(executable, "cursor-cli")]
        else:
            program.append(executable)
        return program

    def _command(self, executable: str, prompt_file: Path, launch: Launch, *, private_run: bool = False) -> list[str]:
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
        if private_run:
            # Identical prompts can run concurrently: cache inputs, not execution state.
            source = run
            run = source.parent / os.urandom(32).hex()
            sync_dir(source=source, home=run)
        args += ["--plugin-dir", str(run / PLUGIN_DIR),
                 *(arg for directory in launch.library.plugin_dirs for arg in ("--plugin-dir", directory)),
                 *(arg for directory in dict.fromkeys(launch.knowledge.add_dirs) for arg in ("--add-dir", directory))]
        if launch.profile.runtime.launcher is Launcher.TTADK:
            ttadk.check_args(args)
        program = self._program(executable, launch, run)
        if private_run:
            program.insert(3, f"TMPDIR={prepare_tmpdir(run)}")
        return [*program, *args]

    def run_command(self, executable: str, *, cwd: str | Path, prompt_file: Path, launch: Launch,
                    max_turns: int = 0, session_dir: Path | None = None) -> list[str]:
        return self._command(executable, prompt_file, launch, private_run=True)

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
        stdin.write(((instructions_block(instructions) + "\n\n" if instructions else "") + text).encode("utf-8"))
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

    async def login_problem(self, provider: ProviderSpec, *, executable: str | None = None,
                            launcher: Launcher = Launcher.NATIVE) -> str | None:
        return await cursor_login.launch_problem()

    async def after_run(self, launch: Launch, command: list[str]) -> list[str]:
        # Resident commands have no TMPDIR: their session keeps it until reclamation.
        if len(command) > 3 and command[3].startswith("TMPDIR="):
            directory = Path(command[1].partition("=")[2]).parent
            if command[3] == f"TMPDIR={run_tmpdir(directory)}":
                await asyncio.to_thread(shutil.rmtree, run_tmpdir(directory), ignore_errors=True)
        return []

    def result_notes(self, result_event: dict[str, Any] | None) -> dict[str, Any]:
        return ({"routed_model": result_event["routed_model"]}
                if result_event and "routed_model" in result_event else {})

    def launch_record(self, launch: Launch) -> dict[str, Any]:
        record: dict[str, Any] = {"personal_config": list(PERSONAL_CONFIG)}
        if launch.profile.runtime.launcher is Launcher.TTADK:
            record["launcher"] = {"kind": "ttadk", "version": launch.profile.runtime.version,
                                  "tool": "cursor-cli", "mode": None}
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
        self.stream = CursorStream(model=model)

    async def start(self, prompt: str, *, resume: bool = False) -> None:
        assert self._process.stdin is not None
        await self._cursor.write_turn(self._process.stdin, prompt, instructions="" if resume else self._instructions)

    def events(self) -> AsyncIterator[dict[str, Any]]:
        assert self._process.stdout is not None
        return self.stream.events(self._process.stdout)


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
    _pending: list[dict[str, Any]] = field(default_factory=list)
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
            self._pending.append(dict(raw))
            # The rendered message waits for tool_call/result, but work has already begun.
            return [{"type": "system", "subtype": "cursor_event", "session_id": self.session_id, "event": dict(raw)}]
        if kind == "thinking" and phase == "delta":
            self._thinking.append(str(raw.get("text") or ""))
            return [{"type": "system", "subtype": "cursor_event", "session_id": self.session_id, "event": dict(raw)}]
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
            events, self._pending = self._pending, []
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
            if kind == "retry" and phase == "starting":
                self._pending.clear()
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
            events, self._pending = self._pending, []
            return [*events, usage_event(self.model, reported_totals(result), session_id=self.session_id), result]
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

    async def _run_turn(
        self, text: str, on_event: Callable[[dict[str, Any]], Awaitable[None]], *, tag: Any = None,
        stop: Callable[[], CapStop | None] | None = None, cancel: TurnCancel,
    ) -> ResidentTurn:
        cwd = self._cwd
        try:
            first = await super()._run_turn(text, on_event, tag=tag, stop=stop, cancel=cancel)
            if cancel.requested or first.stopped is not None or first.exit_code not in (0, 1) \
                    or cursor_failure(first.result_event, first.stderr) is None:
                return first
            # The CLI exited; keep this session's helper and chat for --resume.
            self._tree, self._end_task = None, None
            if cancel.requested:
                return self._stopped_turn(session_started=first.session_started, usage=first.usage,
                                          complete=first.usage_complete, sent=first.prompt_sent)
            await on_event(retry_event(first.stderr))
            self._ended = False
            second = await super()._run_turn(text, on_event, tag=tag, stop=stop, cancel=cancel)
            return replace(second, stderr_detail=retry_stderr(first.stderr, second.stderr)
                           if second.result_event is None else "")
        finally:
            self._cwd = cwd

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
        if self._session and saved is not None and saved.get("session_id") == self._session:
            cwd = saved.get("cwd")
            if isinstance(cwd, str) and cwd:
                if Path(cwd).is_dir():
                    self._cwd = cwd
                else:
                    logger.warning("%s: saved Cursor working directory %s is missing; starting a new chat",
                                   self._label, cwd)
                    self._session = None
                    self._totals = Usage()
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
                f"TMPDIR={prepare_tmpdir(home)}", *rest, *(["--resume", self._session] if self._session else [])]

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
        # Cursor keys its chat partition by the process's physical working directory.
        group = home / CONFIG_DIR / "chats" / hashlib.md5(os.path.realpath(self._cwd).encode("utf-8")).hexdigest()
        for path in group.iterdir() if group.is_dir() and not group.is_symlink() else []:
            if path.is_dir() and not path.is_symlink() and path.name != self._session:
                shutil.rmtree(path, ignore_errors=True)
