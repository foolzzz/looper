"""The pi runtime's identity, provider binding, private homes and safe probe.

pi is a fifth runtime type (the pi runtime RFC, sections 1, 3 and 6). Registering
it discovers ``rt-pi`` when ``pi`` is on PATH. Launch commands use private,
content-addressed homes and separate session directories. JSON events are
translated independently of OMP. Residents resume one JSON process per turn;
capabilities stay empty until the tool bridge is implemented.
There is no pi login and no schema change.

A probe runs only ``--version`` and ``--help``, in a temporary home, agent
directory and session directory, offline, with a 10 second deadline. 1.3.0 is
the oldest version that passes. An older version, a version that cannot be
read, or help that lacks a flag the launch needs is reported and gives the
runtime nothing it can do. Capabilities stay empty until the remaining
execution paths, including the Looper tool bridge, exist. The later ``mcp``
capability is that bridge only, not a general MCP client.
"""

from __future__ import annotations

import asyncio
import errno
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import tempfile
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

from ... import child_env
from ..run_usage import Usage, run_usage
from ..system_prompt_mode import SystemPromptMode
from . import (
    ApiProtocol, AuthKind, Capability, Probe, PromptBlock, ProviderBinding, ReadOnly, RunCost, RuntimeAdapter,
    RuntimeUnsupported, instructions, runtime_prompt_text,
)
from .instructions import Delivery, InstructionFile
from .per_turn import PerTurnSession, session_dir_name
from .pricing import usage_event

if TYPE_CHECKING:
    from ..agent_settings import Launch
    from ..claude_cli import CapStop, ResidentTurn
    from ..profile_registry import ProviderSpec

_PROBE_TIMEOUT_SEC = 10.0
_VERSION_MAX = 200
PROVIDER_ID = "looper"
KEY_ENV = "LOOPER_PROVIDER_API_KEY"
# Preference is independent of provider names and protocol declaration order.
API_NAMES: tuple[tuple[ApiProtocol, str], ...] = (
    (ApiProtocol.OPENAI_RESPONSES, "openai-responses"),
    (ApiProtocol.OPENAI_CHAT, "openai-completions"),
)
# pi 1.3's CLI accepts these; the selected model may clamp the actual level.
THINKING_LEVELS = ("off", "minimal", "low", "medium", "high", "xhigh", "max")
# The oldest pi whose --help has the flags a later launch uses.
MIN_VERSION = (1, 3, 0)
# Whole tokens. ``--session`` must not match ``--session-dir`` or ``--session-id``.
PROBE_HELP_TOKENS = (
    "-p",
    "--mode",
    "json",
    "rpc",
    "--offline",
    "--no-approve",
    "--no-context-files",
    "--no-extensions",
    "--no-mcp",
    "--no-skills",
    "--no-prompt-templates",
    "--no-themes",
    "-e",
    "--provider",
    "--model",
    "--thinking",
    "--system-prompt",
    "--append-system-prompt",
    "--tools",
    "--no-session",
    "--session-dir",
    "--session",
)
HOME_LAYOUT_ID = "pi-home/v1"
# Keep shells in the private HOME as well: nested runtimes must not discover personal logins.
_HIDDEN_DIRS = ("HOME", *(name for name in child_env.SYSTEM if name.startswith("XDG_")))
_PI_ENV = {
    "PI_OFFLINE": "1", "PI_TELEMETRY": "0", "PI_SKIP_VERSION_CHECK": "1",
}
_PI_DIRS = ("PI_CODING_AGENT_DIR", "PI_CODING_AGENT_SESSION_DIR")
# pi's bash tool adds these after launch, even when none came from the parent environment.
_PI_SHELL_METADATA = ("PI_SESSION_ID", "PI_SESSION_FILE", "PI_PROVIDER", "PI_MODEL", "PI_REASONING_LEVEL")
_PLATFORM_ENV = ("LOOPER_AGENT_URL", "LOOPER_AGENT_TOKEN", KEY_ENV)
_ISOLATION_FLAGS = (
    "--offline", "--no-approve", "--no-context-files", "--no-extensions", "--no-mcp",
    "--no-skills", "--no-prompt-templates", "--no-themes",
)
_VERSION = re.compile(r"(\d+)\.(\d+)\.(\d+)")


class _ProbeFailure(Exception):
    """``--version`` or ``--help`` did not finish; ``message`` is the probe error."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class PiRuntime(RuntimeAdapter):
    """pi's type; launch construction is available, execution capabilities remain gated."""

    type = "pi"
    label = "pi"
    default_executable = "pi"
    # Empty until all execution paths and the Looper tool bridge exist. Not a general MCP client.
    capabilities = frozenset()
    protocols = frozenset({ApiProtocol.OPENAI_RESPONSES, ApiProtocol.OPENAI_CHAT})
    # API keys only. pi's own /login and OAuth are not providers Looper binds.
    auth_kinds = frozenset({AuthKind.API_KEY})
    isolation_id = HOME_LAYOUT_ID
    can_replace_system_prompt = True
    read_only = ReadOnly.TOOLS
    provider_env_owned: ClassVar[frozenset[str]] = frozenset({
        *_PI_DIRS, *_PI_ENV, *_PLATFORM_ENV, "PI_SHELL_PREFIX", "PI_CONFIG_FILES", "PI_CONFIG_DIR",
    })

    def capabilities_for(self, version: str | None) -> frozenset[Capability]:
        """Nothing is advertised yet, whatever ``version`` the probe read.

        The later set is run, a resident session, resume, steer, read tools,
        compaction events, and mcp for the Looper tool bridge only. That is
        not a general MCP client, and none of it is enabled until those paths
        exist.
        """
        del version
        return frozenset()

    def probe(self, executable: str) -> Probe:
        """``executable --version`` and, when that is new enough, ``--help``.

        Both run offline in a temporary home, agent directory and session
        directory, and each has 10 seconds. Nothing of the operator's pi
        configuration is read, and no model is called.
        """
        root = Path(tempfile.mkdtemp(prefix="looper-pi-probe-"))
        try:
            home, agent, session = root / "home", root / "home" / "agent", root / "session"
            for path in (home, agent, session):
                path.mkdir(mode=0o700)
                os.chmod(path, 0o700)
            env = _probe_env(home, agent, session)
            try:
                done = _run(executable, "--version", env, home)
            except _ProbeFailure as failure:
                return Probe(None, self.capabilities_for(None), failure.message)
            line = _first_line(done.stdout or "") or _first_line(done.stderr or "")
            if done.returncode != 0 or not line:
                return Probe(None, self.capabilities_for(None), _version_failure(executable, done.returncode, line))
            shown = line[:_VERSION_MAX]
            found = _VERSION.search(line)
            if found is None:
                return Probe(shown, self.capabilities_for(shown), _unknown_version(executable, shown))
            if tuple(int(part) for part in found.groups()) < MIN_VERSION:
                return Probe(shown, self.capabilities_for(shown), _old_version(shown, executable))
            try:
                help_done = _run(executable, "--help", env, home)
            except _ProbeFailure as failure:
                return Probe(shown, self.capabilities_for(shown), f"{failure.message}; probe again")
            help_text = f"{help_done.stdout or ''}{help_done.stderr or ''}"
            if help_done.returncode != 0 or not help_text.strip():
                return Probe(shown, self.capabilities_for(shown), _help_failure(executable, help_done.returncode, help_text))
            missing = [token for token in PROBE_HELP_TOKENS if not _has_token(help_text, token)]
            if missing:
                return Probe(shown, self.capabilities_for(shown), _missing_flags(executable, missing))
            return Probe(shown, self.capabilities_for(shown))
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def prompt_blocks(self, launch: Launch, looper_prompt: str, *, cwd: str | Path | None = None) -> list[PromptBlock]:
        looper = PromptBlock("Looper instructions", "looper", _prompt_text(looper_prompt, launch, cwd=cwd),
                             "system prompt file")
        return ([looper] if launch.system_prompt_mode is SystemPromptMode.REPLACE else
                [PromptBlock("Default prompt", "runtime", None, "pi's default prompt (not shown)"), looper])

    def isolation_args(self, cwd: str | Path) -> list[str]:
        return list(_ISOLATION_FLAGS)

    def bind_provider(self, provider: ProviderSpec, model: str, effort: str) -> ProviderBinding:
        """Pin the private provider and selected model; the API key goes only in the child environment."""
        _check_provider(provider, model)
        _check_explicit_env(provider.env)
        assert provider.api_key is not None  # checked above; ProviderBinding.env contains strings only
        if effort and effort not in THINKING_LEVELS:
            raise RuntimeUnsupported(f"pi thinking must be one of {', '.join(THINKING_LEVELS)}, or empty")
        return ProviderBinding(
            args=("--provider", PROVIDER_ID, "--model", model, *(("--thinking", effort) if effort else ())),
            env=(*provider.env, (KEY_ENV, provider.api_key)),
        )

    def launch_args(self, launch: Launch) -> list[str]:
        binding = self.bind_provider(launch.profile.provider, launch.model, launch.effort)
        return ["-p", "--mode", "json", *_ISOLATION_FLAGS,
                *(["--tools", "read,grep,find,ls,mcp__looper__*"] if launch.read_only else []), *binding.args]

    def environment(self, launch: Launch) -> dict[str, str]:
        binding = self.bind_provider(launch.profile.provider, launch.model, launch.effort)
        _check_explicit_env(launch.env)
        visible = {**child_env.inherited(), **dict(launch.env), **dict(binding.env)}
        # HOME and the two directory bindings need the prompt/session paths; run_command sets them via env.
        return {**_isolated_env(visible), KEY_ENV: dict(binding.env)[KEY_ENV], **_PI_ENV}

    def resident_command(self, executable: str, *, cwd: str | Path, prompt_file: Path, launch: Launch,
                         max_turns: int = 0) -> list[str]:
        """The shared launch, with owner storage and exact resume added by ``ResidentPi``."""
        sessions = Path(prompt_file).absolute().parent.parent / "pi" / "sessions"
        return self.run_command(executable, cwd=cwd, prompt_file=prompt_file, launch=launch,
                                max_turns=max_turns, session_dir=sessions)[:-2]

    def resume_args(self, session_id: str) -> list[str]:
        # This saved ID is resolved inside the owner's directory before spawning, never passed to pi.
        return ["--session", session_id]

    def run_command(self, executable: str, *, cwd: str | Path, prompt_file: Path, launch: Launch,
                    max_turns: int = 0, session_dir: Path | None = None) -> list[str]:
        # Validate role/provider overrides before creating anything. max_turns has no pi CLI equivalent.
        self.environment(launch)
        local = Path(prompt_file).absolute().parent.parent
        home = write_home(local, models_json(launch), settings_json(tuple(name for name, _ in launch.profile.provider.env)),
                          binding_sha256=launch.binding_sha256)
        sessions = local / "pi" / "sessions"
        _private_dir(sessions)
        if session_dir is None:
            session = Path(tempfile.mkdtemp(prefix="run-", dir=sessions))
            session.chmod(0o700)
        else:
            session = Path(session_dir).absolute()
            if session.resolve().is_relative_to((local / "pi" / "homes").resolve()):
                raise RuntimeUnsupported("pi session directories must be outside shared homes")
            _private_dir(session)
        text = _prompt_text(Path(prompt_file).read_text(encoding="utf-8"), launch, cwd=cwd)
        # env execs pi, so the prompt path also keeps the home visible to startup reclamation via pi's argv.
        prompt = home / "prompts" / f"{hashlib.sha256(text.encode('utf-8')).hexdigest()}.md"
        _private_dir(prompt.parent)
        _write_private(prompt, text)
        prompt_flag = ("--system-prompt" if launch.system_prompt_mode is SystemPromptMode.REPLACE
                       else "--append-system-prompt")
        return [
            "env", f"HOME={home}", f"PI_CODING_AGENT_DIR={home / 'agent'}",
            f"PI_CODING_AGENT_SESSION_DIR={session}", executable, *self.launch_args(launch),
            prompt_flag, str(prompt),
            *(["--no-session"] if session_dir is None else ["--session-dir", str(session)]),
        ]

    def resident_session(self, label: str) -> ResidentPi:
        return ResidentPi(label=label)

    def consult_command(self, executable: str, *, cwd: str | Path, prompt_file: Path, launch: Launch,
                        session_id: str, session_dir: Path | None, fork_dir: Path, max_turns: int = 0) -> list[str]:
        """Resume only the requested private session, copied into the consult's disposable storage."""
        source = _session_file(session_dir, session_id) if session_dir is not None else None
        if source is None:
            raise RuntimeUnsupported("pi's saved session has no unique file in the run's directory")
        fork_dir = fork_dir.absolute()
        _private_dir(fork_dir.parent)
        fork_dir.mkdir(mode=0o700)
        try:
            copied = fork_dir / source.name
            shutil.copyfile(source, copied)
            copied.chmod(0o600)
            command = self.run_command(executable, cwd=cwd, prompt_file=prompt_file, launch=launch,
                                       max_turns=max_turns, session_dir=fork_dir)
            return [*command, "--session", str(copied)]
        except BaseException:
            shutil.rmtree(fork_dir, ignore_errors=True)
            raise

    async def write_turn(self, stdin: asyncio.StreamWriter, text: str, *, uuid: str | None = None) -> None:
        """Pi reads a single prompt through EOF; JSON mode has no live user-turn transport."""
        if stdin.is_closing():
            raise RuntimeError("pi reads one prompt per run: it takes no user turn while it runs")
        stdin.write(text.encode("utf-8"))
        await stdin.drain()
        stdin.close()

    def parse(self, stdout: asyncio.StreamReader, *, model: str | None = None) -> AsyncIterator[dict[str, Any]]:
        return PiStream(model=model).events(stdout)

    def tool_summary(self, tool_name: str, tool_input: dict[str, Any]) -> str:
        if tool_name == "bash":
            return f"Running: {str(tool_input.get('command') or '')[:80]}"
        if tool_name in EDIT_TOOLS:
            paths = edited_paths(tool_name, tool_input)
            action = "Writing" if tool_name == "write" else "Editing"
            return f"{action}: {paths[0][:80] if paths else '?'}"
        if tool_name in ("read", "ls"):
            path = tool_input.get("path")
            action = "Reading" if tool_name == "read" else "Listing"
            return f"{action}: {path[:80] if isinstance(path, str) and path else '?'}"
        if tool_name in ("grep", "find"):
            return f"{tool_name}: {str(tool_input.get('pattern') or tool_input.get('path') or '')[:60]}"
        return f"Tool: {tool_name}"

    def cost(self, usage: Usage, provider: ProviderSpec) -> RunCost:
        """Tokens priced from this process's modelUsage by Launch.price, never pi's model catalog costs."""
        return RunCost(usage, self.cost_source(provider))

    def budget_error(self, result_event: dict[str, Any] | None) -> str | None:
        from .. import claude_cli

        return claude_cli.budget_error(result_event) if (result_event or {}).get(claude_cli.SOFT_CAP) else None

    def instruction_files(self, cwd: str | Path) -> tuple[InstructionFile, ...]:
        """Inject the project's files once; pi's own context-file discovery stays disabled."""
        return tuple(InstructionFile(path, Delivery.INJECTED) for path in instructions.first_per_directory(
            cwd, (instructions.AGENTS_MD, instructions.CLAUDE_MD)))


EDIT_TOOLS = frozenset({"write", "edit"})


def edited_paths(tool_name: str, tool_input: Mapping[str, Any]) -> list[str]:
    """Pi write and edit both name a single file in ``path``; edit is not an OMP patch."""
    path = tool_input.get("path")
    return [path] if tool_name in EDIT_TOOLS and isinstance(path, str) and path.strip() and "://" not in path else []


def _message_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "\n".join(block["text"] for block in content if isinstance(block, Mapping)
                     and block.get("type") == "text" and isinstance(block.get("text"), str))


def _opaque_diagnostic(value: Any) -> Any:
    """Unknown payloads have no trusted string schema; keep structure and numeric diagnostics only."""
    if isinstance(value, Mapping):
        return {key: _opaque_diagnostic(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_opaque_diagnostic(item) for item in value]
    return "***" if isinstance(value, str) else value


def _count(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _pi_usage(raw: Mapping[str, Any]) -> Usage:
    """Pi separates uncached input and cache tokens; output already includes reasoning."""
    return Usage(input_tokens=_count(raw.get("input")), output_tokens=_count(raw.get("output")),
                 cache_read_tokens=_count(raw.get("cacheRead")), cache_creation_tokens=_count(raw.get("cacheWrite")))


@dataclass
class PiStream:
    """One Pi run as internal stream-json events, with final messages and settled as authorities.

    Deltas and execution events repeat message content. ``agent_end`` can precede
    automatic recovery, so only ``agent_settled`` emits a result, once. EOF (including
    a truncated record) never supplies a result. A corrupt record before settled
    makes that result fail because a final message may have been lost.
    """

    model: str | None = None
    session_id: str | None = None
    _last_text: str = ""
    _last_stop: str | None = None
    _last_error: str | None = None
    _protocol_error: str | None = None
    _settled: bool = False
    _model_usage: dict[str, Usage] = field(default_factory=dict)

    _DROPPED: ClassVar[frozenset[str]] = frozenset({
        "agent_start", "agent_end", "turn_start", "turn_end", "message_start", "message_update",
        "tool_execution_start", "tool_execution_update", "tool_execution_end",
    })

    async def events(self, stdout: asyncio.StreamReader) -> AsyncIterator[dict[str, Any]]:
        from ..claude_cli import iter_lines

        # Split bytes only on LF, before UTF-8 decoding; no readline size limit or Unicode splitlines.
        async for line in iter_lines(stdout):
            if not line.strip():
                continue
            try:
                raw = json.loads(line.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                raw = None
            events = self.translate(raw) if isinstance(raw, Mapping) else self._invalid_record()
            for event in events:
                yield event

    def _base(self) -> dict[str, Any]:
        return {"session_id": self.session_id} if self.session_id else {}

    def _diagnostic(self, raw: Mapping[str, Any]) -> list[dict[str, Any]]:
        event = _opaque_diagnostic(raw)
        kind = raw.get("type")
        if isinstance(kind, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,79}", kind):
            event["type"] = kind
        return [{"type": "system", "subtype": "pi_event", **self._base(), "event": event}]

    def _invalid_record(self) -> list[dict[str, Any]]:
        if self._settled:
            return []
        self._protocol_error = "pi emitted an invalid JSON record"
        return self._diagnostic({"type": "invalid_record"})

    def translate(self, raw: Mapping[str, Any]) -> list[dict[str, Any]]:
        if self._settled:
            return []
        kind = raw.get("type")
        if not isinstance(kind, str) or not kind:
            return self._invalid_record()
        if kind == "session":
            self.session_id = str(raw.get("id") or "") or None
            return [{"type": "system", "subtype": "init", **self._base(), "runtime": "pi",
                     "model": self.model, "cwd": raw.get("cwd")}]
        if kind in self._DROPPED:
            return []
        if kind == "message_end":
            message = raw.get("message")
            if not isinstance(message, Mapping):
                return self._invalid_record()
            role = message.get("role")
            if role == "assistant":
                if not isinstance(message.get("content"), list):
                    return self._invalid_record()
                assistant = self._assistant(message)
                usage = self._usage(message.get("usage"), model=message.get("model"))
                if usage:
                    tokens = usage[0]["usage"]
                    assistant["message"]["usage"] = {
                        "input_tokens": tokens["input_tokens"], "output_tokens": tokens["output_tokens"],
                        "cache_read_input_tokens": tokens["cache_read_tokens"],
                        "cache_creation_input_tokens": tokens["cache_creation_tokens"],
                    }
                return [assistant, *usage]
            if role == "toolResult":
                return [{"type": "user", **self._base(), "message": {"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": str(message.get("toolCallId") or ""),
                     "content": _message_text(message.get("content")), "is_error": bool(message.get("isError"))}]}},
                    *self._usage(message.get("usage"), parent_tool_use_id=str(message.get("toolCallId") or ""))]
            if role in ("user", "system"):
                return []  # Input already supplied by Looper.
        if kind == "compaction_end":
            return self._compaction(raw)
        if kind == "agent_settled":
            if not isinstance(raw.get("aborted"), bool):
                self._protocol_error = "pi emitted agent_settled without a boolean aborted flag"
            self._settled = True
            return [self._result(aborted=raw.get("aborted") is True)]
        return self._diagnostic(raw)

    def _usage(self, raw: Any, *, model: Any = None, **extra: Any) -> list[dict[str, Any]]:
        if not isinstance(raw, Mapping):
            return []
        name = model if isinstance(model, str) and model else self.model or "pi"
        tokens = _pi_usage(raw)
        self._model_usage[name] = self._model_usage.get(name, Usage()).plus(tokens)
        return [usage_event(name, tokens, **self._base(), **extra)]

    def _compaction(self, raw: Mapping[str, Any]) -> list[dict[str, Any]]:
        # Keep the attempt/retry diagnostics, including unsuccessful attempts, with opaque strings redacted.
        events = self._diagnostic(raw)
        result = raw.get("result")
        if not isinstance(result, Mapping) or raw.get("aborted") is not False or raw.get("errorMessage"):
            return events
        metadata: dict[str, Any] = {}
        reason = raw.get("reason")
        if reason in ("manual", "threshold", "overflow"):
            metadata["trigger"] = "manual" if reason == "manual" else "auto"
        for source, target in (("tokensBefore", "pre_tokens"), ("estimatedTokensAfter", "post_tokens")):
            value = result.get(source)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                metadata[target] = value
        events.append({"type": "system", "subtype": "compact_boundary", **self._base(),
                       "compact_metadata": metadata})
        events.extend(self._usage(result.get("usage")))
        return events

    def _assistant(self, message: Mapping[str, Any]) -> dict[str, Any]:
        blocks: list[dict[str, Any]] = []
        content = message.get("content")
        for block in content if isinstance(content, list) else []:
            if not isinstance(block, Mapping):
                continue
            if block.get("type") == "text":
                blocks.append({"type": "text", "text": str(block.get("text") or "")})
            elif block.get("type") == "thinking":
                blocks.append({"type": "thinking", "thinking": str(block.get("thinking") or "")})
            elif block.get("type") == "toolCall":
                arguments = block.get("arguments")
                blocks.append({"type": "tool_use", "id": str(block.get("id") or ""),
                               "name": str(block.get("name") or ""),
                               "input": dict(arguments) if isinstance(arguments, Mapping) else {}})
        # An empty final response replaces progress just as a nonempty one does.
        self._last_text = _message_text(content)
        self._last_stop = str(message.get("stopReason") or "") or None
        self._last_error = str(message.get("errorMessage") or "") or None
        return {"type": "assistant", **self._base(), "parent_tool_use_id": None, "message": {
            "role": "assistant", "model": message.get("model") or self.model, "content": blocks}}

    def _result(self, *, aborted: bool) -> dict[str, Any]:
        model_usage = {model: {
            "inputTokens": tokens.input_tokens, "outputTokens": tokens.output_tokens,
            "cacheReadInputTokens": tokens.cache_read_tokens,
            "cacheCreationInputTokens": tokens.cache_creation_tokens,
        } for model, tokens in self._model_usage.items()}
        error = self._protocol_error
        if not error and aborted:
            error = "the pi run was aborted"
        if not error and self._last_stop != "stop":
            error = self._last_error or f"the pi run ended without a completed assistant: {self._last_stop or 'missing'}"
        if error:
            return {"type": "result", "subtype": "error_during_execution", "is_error": True,
                    **self._base(), "modelUsage": model_usage, "result": "", "errors": [error]}
        return {"type": "result", "subtype": "success", "is_error": False,
                **self._base(), "modelUsage": model_usage, "result": self._last_text}


class _SessionLost(Exception):
    """The owner's saved ID has no unique private file to resume."""


def _session_file(directory: Path, session_id: str) -> Path | None:
    """Match the header's whole ID, never a partial ID or a link into another owner."""
    if not session_id or not directory.is_dir() or directory.is_symlink():
        return None
    matches = []
    for path in directory.glob("*.jsonl"):
        if path.is_symlink() or not path.is_file():
            continue
        try:
            with path.open("rb") as source:
                header = json.loads(source.readline(65_536))
        except (OSError, ValueError):
            continue
        if isinstance(header, dict) and header.get("type") == "session" and header.get("id") == session_id:
            matches.append(path)
    return matches[0] if len(matches) == 1 else None


class ResidentPi(PerTurnSession):
    """Manager/Planner turns on pi: JSON subprocesses, private owner storage, exact file resume.

    Only new requests are emitted by ``PiStream``. Saved totals therefore add
    each turn's usage, including after a server restart; history is never billed again.
    Cancellation, spend caps and process-tree cleanup belong to ``PerTurnSession``.
    """

    runtime_label = "pi"

    def _split(self, command: list[str]) -> tuple[list[str], str | None, str]:
        if (len(command) < 5 or command[0] != "env" or not command[1].startswith("HOME=")
                or not command[2].startswith("PI_CODING_AGENT_DIR=")
                or not command[3].startswith("PI_CODING_AGENT_SESSION_DIR=")):
            raise ValueError("a resident pi command starts with env HOME=<home> PI_CODING_AGENT_DIR=<agent> "
                             "PI_CODING_AGENT_SESSION_DIR=<sessions> <pi>")
        session = None
        if command[-2] == "--session":
            command, session = command[:-2], command[-1]
        if any(flag in command[5:] for flag in
               ("--session", "--session-id", "--resume", "--continue", "--no-session", "--session-dir")):
            raise ValueError("a resident pi command must leave session selection to its owner")
        return command, session, command[4]

    def _session_dir(self) -> Path:
        home = Path(self._base[1].partition("=")[2])
        return home.parent.parent / "sessions" / session_dir_name(self._label)

    def _session_file(self, session_id: str) -> Path | None:
        return _session_file(self._session_dir(), session_id)

    def _turn_argv(self) -> list[str]:
        directory = self._session_dir()
        resume = []
        if self._session is not None:
            saved = self._session_file(self._session)
            if saved is None:
                raise _SessionLost("pi's saved session has no unique file in its owner's directory")
            resume = ["--session", str(saved)]
        _private_dir(directory)
        base = list(self._base)
        base[3] = f"PI_CODING_AGENT_SESSION_DIR={directory}"
        return [*base, "--session-dir", str(directory), *resume]

    def _stream(self) -> PiStream:
        model = self._base[self._base.index("--model") + 1] if "--model" in self._base else None
        return PiStream(model=model)

    async def run_turn(
        self, text: str, on_event: Callable[[dict[str, Any]], Awaitable[None]], *, tag: Any = None,
        stop: Callable[[], CapStop | None] | None = None,
    ) -> ResidentTurn:
        from ..claude_cli import ResidentTurn

        new_session = self._session is None
        try:
            turn = await super().run_turn(text, on_event, tag=tag, stop=stop)
        except _SessionLost:
            self._ended = True
            return ResidentTurn(None, 1, session_started=False)
        if new_session and turn.session_started and turn.result_event and not turn.result_event.get("is_error"):
            self._prune_sessions()
        return turn

    def turn_usage(self, result_event: dict[str, Any] | None) -> Usage:
        own = run_usage(result_event, price=self._price)
        if result_event is not None:
            self._totals = self._totals.plus(own)
        return own

    def _prune_sessions(self) -> None:
        if self._session is None or (keep := self._session_file(self._session)) is None:
            return
        for path in self._session_dir().glob("*.jsonl"):
            if path != keep and (path.is_file() or path.is_symlink()):
                path.unlink(missing_ok=True)


def runtime_notes() -> str:
    """pi-specific guidance, leaving the shared role prompts unchanged."""
    return "\n".join([
        "# Runtime notes (pi)",
        "",
        "Looper runs you through pi. The instructions after these notes are Looper's.",
        "",
        "- Looper reads only your final message. When the instructions ask for a reply or a report in a given shape "
        "(such as a JSON block), put all of it in that final message.",
        "- Looper's platform tools use the mcp__looper__ prefix.",
    ])


def _prompt_text(looper_prompt: str, launch: Launch, *, cwd: str | Path | None = None) -> str:
    project = (instructions.injected_section(launch.runtime.instruction_files(cwd), launch.runtime.label)
               if cwd is not None else "")
    return runtime_prompt_text(launch, looper_prompt, project, runtime_notes(), keep_empty=False)


def provider_api(provider: ProviderSpec) -> str:
    """The supported API to use, preferring Responses over Chat Completions."""
    api = next((name for protocol, name in API_NAMES if protocol in provider.protocols), None)
    if api is None:
        raise RuntimeUnsupported(f"pi speaks protocols {', '.join(p.value for p, _ in API_NAMES)}; "
                                 f"provider {provider.name!r} serves none of them")
    return api


def _check_provider(provider: ProviderSpec, model: str) -> str:
    """Fail before rendering or launching; never guess an endpoint or use a pi login."""
    if provider.auth is not AuthKind.API_KEY:
        raise RuntimeUnsupported(f"pi runs with an API key provider only; provider {provider.name!r} is not one")
    api = provider_api(provider)
    if not provider.api_key or not provider.api_key.strip():
        raise RuntimeUnsupported(f"provider {provider.name!r} has no API key: set one in Settings")
    if not provider.base_url or not provider.base_url.strip():
        raise RuntimeUnsupported(f"provider {provider.name!r} has no base_url: set its endpoint in Settings")
    if provider.models and model not in provider.models:
        raise RuntimeUnsupported(f"provider {provider.name!r} does not list model {model!r}")
    return api


def models_json(launch: Launch) -> dict[str, Any]:
    """The private models.json, containing only the chosen model and an interpolated key reference.

    Missing context/output limits stay with pi's defaults; catalog prices are handled by Looper.
    """
    provider = launch.profile.provider
    api = _check_provider(provider, launch.model)
    entry = provider.catalog_model(launch.model)
    model: dict[str, Any] = {
        "id": launch.model, "name": launch.model,
        "reasoning": not (entry is not None and entry.efforts is not None and not entry.efforts),
        "input": ["text"],
        "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
    }
    if entry is not None and entry.context_window:
        model["contextWindow"] = entry.context_window
    if api == "openai-responses":
        # pi 1.3's openai-responses getCompat/convertResponsesTools (also checked in request serialization):
        # the default omits strict; this sends strict:false without making optional parameters required.
        model["compat"] = {"supportsStrictMode": True}
    return {"providers": {PROVIDER_ID: {"baseUrl": provider.base_url, "apiKey": f"${KEY_ENV}",
                                        "api": api, "models": [model]}}}


def _check_explicit_env(entries: tuple[tuple[str, str], ...]) -> None:
    for name, _ in entries:
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            raise RuntimeUnsupported("pi environment variable names must be shell identifiers")
        if name in _HIDDEN_DIRS or name.startswith(("PI_", "LOOPER_", "LOOPX_")):
            raise RuntimeUnsupported(f"pi owns {name}; provider and role environments cannot override it")


def _isolated_env(visible: Mapping[str, str]) -> dict[str, str]:
    dropped = {*_HIDDEN_DIRS, *child_env.RUNTIMES}
    return {name: value for name, value in visible.items()
            if name not in dropped and not name.startswith(("PI_", "LOOPER_", "LOOPX_"))}


def settings_json(provider_names: tuple[str, ...] = ()) -> dict[str, Any]:
    """Disable background spending/discovery; shell children keep private HOME without run credentials."""
    cleared = sorted({*_PLATFORM_ENV, *_PI_DIRS, *_PI_ENV, *_PI_SHELL_METADATA, *child_env.RUNTIMES, *provider_names})
    return {
        "cacheWarming": "off", "enableAnalytics": False, "enableInstallTelemetry": False,
        "defaultProjectTrust": "never",
        "packages": [], "extensions": [], "skills": [], "prompts": [], "themes": [],
        "shellCommandPrefix": "unset " + " ".join(shlex.quote(name) for name in cleared) + ";",
    }


def _private_dir(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.chmod(0o700)


def _write_private(path: Path, text: str) -> None:
    """Publish a complete 0600 file; failed writes leave no temporary file behind."""
    fd, building = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.write(text)
        os.replace(building, path)
    finally:
        Path(building).unlink(missing_ok=True)


def write_home(local: Path, models: Mapping[str, Any], settings: Mapping[str, Any], *, binding_sha256: str) -> Path:
    """Atomically publish reusable configuration, with no per-run session state in the home."""
    files = {name: json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
             for name, value in (("models.json", models), ("settings.json", settings))}
    key = hashlib.sha256(json.dumps([HOME_LAYOUT_ID, binding_sha256, files], sort_keys=True).encode()).hexdigest()
    root = local / "pi"
    homes = root / "homes"
    for path in (root, homes):
        _private_dir(path)
    target = homes / key
    if target.is_dir():
        return target
    building = Path(tempfile.mkdtemp(prefix=".building-", dir=homes))
    try:
        building.chmod(0o700)
        _private_dir(building / "agent")
        for name, text in files.items():
            _write_private(building / "agent" / name, text)
        try:
            os.rename(building, target)
        except OSError as exc:
            # Another process may have published the identical complete directory first.
            if exc.errno not in (errno.EEXIST, errno.ENOTEMPTY) or not target.is_dir():
                raise
    finally:
        shutil.rmtree(building, ignore_errors=True)
    return target


def _probe_env(home: Path, agent: Path, session: Path) -> dict[str, str]:
    """A child's environment pointed at the probe's directories, offline, without the operator's pi paths or logins."""
    env = _isolated_env(child_env.inherited())
    env.update({
        "HOME": str(home),
        "PI_CODING_AGENT_DIR": str(agent),
        "PI_CODING_AGENT_SESSION_DIR": str(session),
        "PI_OFFLINE": "1",
        "PI_TELEMETRY": "0",
        "PI_SKIP_VERSION_CHECK": "1",
    })
    return env


def _run(executable: str, flag: str, env: dict[str, str], cwd: Path) -> subprocess.CompletedProcess[str]:
    """One of the two probe commands. Raises ``_ProbeFailure`` when it does not finish."""
    try:
        return subprocess.run([executable, flag], capture_output=True, text=True, timeout=_PROBE_TIMEOUT_SEC,
                              check=False, env=env, cwd=cwd)
    except FileNotFoundError:
        raise _ProbeFailure(f"{executable!r} was not found") from None
    except (OSError, subprocess.TimeoutExpired, UnicodeError) as exc:
        raise _ProbeFailure(f"{executable} {flag} did not run: {type(exc).__name__}: {exc}") from None


def _first_line(text: str) -> str:
    return next((line.strip() for line in text.splitlines() if line.strip()), "")


def _has_token(text: str, token: str) -> bool:
    """Whether ``token`` appears as its own flag or word, not as the prefix of a longer one."""
    return re.search(rf"(?<![\w-]){re.escape(token)}(?![\w-])", text) is not None


def _version_failure(executable: str, code: int, line: str) -> str:
    if not line and code == 0:
        return f"{executable} --version printed nothing"
    if not line:
        return f"{executable} --version failed (exit code {code})"
    return f"{executable} --version failed (exit code {code}): {line[:_VERSION_MAX]}"


def _old_version(shown: str, executable: str) -> str:
    return (f"{shown} from {executable} --version is older than 1.3.0, the oldest pi Looper supports; "
            "upgrade pi and probe again")


def _unknown_version(executable: str, shown: str) -> str:
    return (f"{executable} --version printed {shown!r}, which is not a pi version Looper can read; "
            "install pi 1.3.0 or newer and probe again")


def _help_failure(executable: str, code: int, text: str) -> str:
    line = _first_line(text)
    if code == 0 and not line:
        return f"{executable} --help printed nothing; probe again"
    detail = f": {line[:_VERSION_MAX]}" if line else ""
    return f"{executable} --help failed (exit code {code}){detail}; probe again"


def _missing_flags(executable: str, missing: list[str]) -> str:
    return (f"{executable} --help does not show {', '.join(missing)}; "
            "upgrade pi to 1.3.0 or newer and probe again")
