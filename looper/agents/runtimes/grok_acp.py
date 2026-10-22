"""The Manager's and a Planner's Grok session as one resident process over ACP (Grok runtime RFC, section 14).

``grok agent --always-approve ... stdio`` is JSON-RPC 2.0 over stdio, one JSON object per line. One process serves
all the turns of a session (``AcpGrok``), where ``grok.ResidentGrok`` starts a headless process per turn and replays
the session with ``--resume``:

- **Launch**: ``AcpGrok`` is given the command ``GrokRuntime.resident_command`` builds and turns it into the ACP
  command (``parse_command``, ``acp_argv``): the same ``GROK_HOME`` and ``HOME`` (the owner's own home, with the
  rules file and ``config.toml`` with the MCP servers), the model and the effort, the project trusted. What the ACP
  command has no flag for goes where Grok reads it: ``--tools`` (a Planner's reading tools only, which ``agent
  stdio`` does not apply) becomes the ``tools:`` line of an agent profile (``agent_profile``), and ``--max-turns`` is
  counted here.
- **A session** is ``session/new`` (``mcpServers`` empty: the servers are in ``config.toml``) or, for the saved one,
  ``session/load``, which keeps the id. A session Grok no longer has is a turn that never started, as for the
  per-turn session.
- **A turn** is one ``session/prompt``. Its ``session/update`` notifications and the ``response_completed``
  notification of each model response become Claude Code's stream-json events (``AcpTurn``), its result carries the
  turn's usage, and ``stopReason`` says how it ended.
- **Cancelling** is ``session/cancel``: the turn ends with ``stopReason: cancelled`` within a fraction of a second,
  the tool commands it was running are ended, and the session stays usable. A turn given up (cancellation, a soft
  cap, ``--max-turns``) is cancelled this way; a process that does not answer in ``CANCEL_GRACE_SEC`` is ended
  through ``proc_tree``. The user's stop of a turn is the turn's ``cancel`` (``run_turn(cancel=)``; ``interrupt`` only
  requests it): ``StopMethod.CANCEL`` when Grok ends the turn in time (the processes the turn started are then ended
  from the tree's snapshot), ``KILL`` when its process had to be ended.
- **Falling back**: when Grok cannot serve the session over ACP (no ``agent stdio``, no ``initialize`` answer, no
  ``loadSession`` for a saved session, a flag with no ACP form) the session runs per turn (``ResidentGrok``), which
  keeps its sessions in the same home. A process that dies mid-turn is a turn without a result, as for the per-turn
  session; the next one is a new process that loads the session.

A runtime runs no turn of its own between turns here, so the ``autonomous`` handler is never called.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import shutil
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .. import proc_tree
from ..run_usage import Usage, run_usage
from . import grok
from .per_turn import session_dir_name, stderr_tail

if TYPE_CHECKING:
    from ..claude_cli import CapStop, ResidentTurn
    from ..run_usage import Price
    from . import StopMethod
    from .cancel import TurnCancel

logger = logging.getLogger(__name__)

PROTOCOL_VERSION = 1
INIT_TIMEOUT_SEC = 30.0  # `initialize`: Grok answers without calling a model
SETUP_TIMEOUT_SEC = 60.0  # `session/new`, `session/load`: the MCP servers start here
_EXIT_POLL_SEC = 0.2
CANCEL_GRACE_SEC = 5.0  # how long a cancelled turn may take to end before the process is ended
PROFILE_FILE = "acp-agent.md"  # the agent profile of a read-only session, in the owner's home
# xAI's cost unit: a tick is 1e-10 USD (Grok runtime RFC section 14.5).
TICKS_PER_USD = 10_000_000_000
# What the `stop_reason` of a prompt result says about how the turn ended; others are errors.
_STOP_ERRORS = {"cancelled": "the turn was cancelled", "max_tokens": "the model ran out of output tokens",
                "max_turn_requests": "the turn went over its model request limit",
                "refusal": "the model refused the request"}
# Notifications that carry the turn's content.
_USER_STOP: dict[str, Any] = {"looper": "user stop"}  # what ``interrupt`` puts in the inbox for the turn to see
_UPDATE = "session/update"
_EXTENSION = "_x.ai/session_notification"


def _cancelled(result: dict[str, Any]) -> dict[str, Any]:
    """``result`` as a turn the user stopped ends: cancelled, an error, with what it spent."""
    return {**result, "subtype": "error_during_execution", "is_error": True, "stop_reason": "cancelled",
            "errors": [_STOP_ERRORS["cancelled"]]}


class AcpUnavailable(Exception):
    """Grok cannot serve this session over ACP; the message says why. The session runs per turn instead."""


class SessionGone(Exception):
    """``session/load`` found no such session."""


class AcpError(Exception):
    """A JSON-RPC error response."""

    def __init__(self, method: str, error: Any) -> None:
        # Only the code reaches a message or a log: Grok's text may hold paths and values.
        code = error.get("code") if isinstance(error, Mapping) else None
        super().__init__(f"{method} failed (code {code if isinstance(code, int) else 'unknown'})")
        self.error = error


def _missing_session(error: Any) -> bool:
    """Whether a ``session/load`` error says the session is not there (Grok 1.0.62: -32603 with ``FS_NOT_FOUND``);
    any other error may be transient and does not discard the session."""
    data = error.get("data") if isinstance(error, Mapping) else None
    return isinstance(data, Mapping) and data.get("code") == "FS_NOT_FOUND"


# --- The command ---


@dataclass(frozen=True)
class AcpLaunch:
    """What a resident Grok command says that the ACP command needs."""

    executable: str
    model: str | None = None
    effort: str | None = None
    tools: str | None = None  # the reading tools of a read-only session
    agent: str | None = None  # the agent definition that replaces Grok's prompt
    max_turns: int = 0


# The flags of the headless command that ACP has no use for (the prompt and the output format of a headless run),
# those that are the same on `agent`, and those it takes at the root.
_DROPPED = frozenset({"--prompt-file", "--output-format"})
_BOOLEAN = frozenset({"--always-approve", "--trust", "--no-auto-update"})
_VALUED = frozenset({"-m", "--effort", "--tools", "--agent", "--max-turns"})


def parse_command(base: list[str]) -> AcpLaunch:
    """The launch of a resident Grok command (``GrokRuntime.resident_command``, without the session to resume);
    raises ``AcpUnavailable`` for a flag the ACP command has no form of."""
    values: dict[str, str] = {}
    args = base[4:]
    i = 0
    while i < len(args):
        flag = args[i]
        if flag in _BOOLEAN:
            i += 1
        elif flag in _DROPPED | _VALUED and i + 1 < len(args):
            if flag in _VALUED:
                values[flag] = args[i + 1]
            i += 2
        else:
            raise AcpUnavailable(f"the flag {flag} has no ACP form")
    try:
        max_turns = int(values.get("--max-turns", 0))
    except ValueError:
        raise AcpUnavailable(f"--max-turns {values['--max-turns']!r} is not a number") from None
    return AcpLaunch(base[3], values.get("-m"), values.get("--effort"), values.get("--tools"), values.get("--agent"),
                     max_turns)


def acp_argv(launch: AcpLaunch, home: Path, profile: Path | None) -> list[str]:
    """The ACP process's command: root flags (the project trusted, no update) before ``agent``, the agent's own
    flags (every tool without asking, the model, the effort, the profile) after it, then ``stdio``."""
    return ["env", f"GROK_HOME={home}", f"HOME={home}", launch.executable, "--trust", "--no-auto-update",
            "agent", "--always-approve",
            *(["--agent-profile", str(profile)] if profile is not None else []),
            *(["-m", launch.model] if launch.model else []), *(["--effort", launch.effort] if launch.effort else []),
            "stdio"]


def agent_profile(definition: str | None, tools: str) -> str:
    """The agent profile of a read-only session: ``definition`` (the agent that replaces Grok's prompt, if the run
    has one) with a ``tools:`` line in its front matter, else a profile of the tools alone, which leaves Grok's
    prompt and Looper's rules as they are. Grok's MCP meta-tools (``search_tool``, ``use_tool``) stay."""
    if definition is None:
        return f"---\nname: looper\ndescription: Looper agent\ntools: {tools}\n---\n"
    end = definition.find("\n---\n", 4) if definition.startswith("---\n") else -1
    if end < 0:
        raise AcpUnavailable("the agent definition has no front matter to restrict the tools in")
    return f"{definition[:end]}\ntools: {tools}{definition[end:]}"


# --- A turn's events ---


def _tokens(usage: Any) -> dict[str, int]:
    """Claude's request usage from a ``response_completed`` usage (``input_tokens`` without the cache's)."""
    usage = usage if isinstance(usage, Mapping) else {}

    def count(key: str) -> int:
        value = usage.get(key)
        return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0

    return {"input_tokens": count("input_tokens"), "output_tokens": count("output_tokens"),
            "cache_read_input_tokens": count("cache_read_input_tokens"),
            "cache_creation_input_tokens": count("cache_creation_input_tokens")}


def _count(usage: Mapping[str, Any], key: str) -> int:
    value = usage.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _uncached(usage: Mapping[str, Any]) -> dict[str, int]:
    """A prompt result's tokens in the headless terms: ACP's ``inputTokens`` is the whole prompt, cache hits
    included; the headless ``input_tokens`` (and Looper's) leave them out."""
    cached, created = _count(usage, "cachedReadTokens"), _count(usage, "cacheCreationTokens")
    return {"inputTokens": max(_count(usage, "inputTokens") - cached - created, 0),
            "outputTokens": _count(usage, "outputTokens"), "cacheReadInputTokens": cached,
            "cacheCreationInputTokens": created}


class AcpTurn:
    """One turn's ``session/update`` notifications as Claude Code's stream-json events, and its result.

    A model response is one ``assistant`` event: its thinking, its text and its tool calls, with the usage its
    ``response_completed`` notification gave (that notification comes after the response's text and before its tool
    calls). It is sent when the response's first tool call starts (so the call shows while it runs; a later call of
    the same response is its own event), and at the end of the turn. A finished tool call is a ``user`` event with
    its result.
    """

    def __init__(self, *, model: str | None, session_id: str) -> None:
        self.model, self.session_id = model, session_id
        self.responses = 0  # the model responses so far
        self.final_text = ""  # the text of the last response that called no tool
        self._stream = grok.GrokStream(model=model, session_id=session_id)
        self._blocks: list[dict[str, Any]] = []
        self._open: tuple[str, list[str]] | None = None  # the text or thinking being streamed
        self._usage: dict[str, int] | None = None  # the last response's, for the next assistant event
        self._calls: dict[str, dict[str, Any]] = {}
        self._messages = 0

    def init(self, *, cwd: str) -> dict[str, Any]:
        return {"type": "system", "subtype": "init", "session_id": self.session_id, "model": self.model,
                "cwd": cwd, "apiKeySource": "oauth", "permissionMode": "bypassPermissions", "runtime": "grok",
                "transport": "acp"}

    def feed(self, message: Mapping[str, Any]) -> list[dict[str, Any]]:
        """The events of one notification, none for what the turn's record has no use for."""
        params = message.get("params")
        update = params.get("update") if isinstance(params, Mapping) else None
        kind = update.get("sessionUpdate") if isinstance(update, Mapping) else None
        if not isinstance(update, Mapping) or not isinstance(kind, str):
            return []
        if message.get("method") == _EXTENSION:
            if kind == "response_completed":
                self.responses += 1
                self._usage = _tokens(update.get("usage"))
            return []
        if kind in ("agent_message_chunk", "agent_thought_chunk"):
            content = update.get("content")
            text = content.get("text") if isinstance(content, Mapping) else None
            if isinstance(text, str) and text:
                self._stream_text("text" if kind == "agent_message_chunk" else "thinking", text)
        elif kind == "tool_call":
            self._tool_call(update)
            return self._flush()  # the call shows in the task's activity while it runs
        elif kind == "tool_call_update" and update.get("status") in ("completed", "failed"):
            return self._tool_result(update)
        return []

    def _stream_text(self, kind: str, text: str) -> None:
        if self._open is not None and self._open[0] != kind:
            self._close()
        if self._open is None:
            self._open = (kind, [])
        self._open[1].append(text)

    def _close(self) -> None:
        if self._open is not None:
            kind, parts = self._open
            self._blocks.append({"type": kind, kind: "".join(parts)})
            self._open = None

    def _tool_call(self, update: Mapping[str, Any]) -> None:
        self._close()
        meta = update.get("_meta") if isinstance(update.get("_meta"), Mapping) else {}
        tool = meta.get("x.ai/tool") if isinstance(meta.get("x.ai/tool"), Mapping) else {}
        raw = update.get("rawInput")
        call = {"type": "tool_use", "id": str(update.get("toolCallId") or ""),
                "name": str(tool.get("name") or update.get("title") or "tool"),
                "input": dict(raw) if isinstance(raw, Mapping) else {}}
        self._calls[call["id"]] = call
        self._blocks.append(call)

    def _tool_result(self, update: Mapping[str, Any]) -> list[dict[str, Any]]:
        call_id = str(update.get("toolCallId") or "")
        events = self._flush()
        raw = update.get("rawOutput")
        if isinstance(raw, Mapping):
            content = json.dumps(raw, ensure_ascii=False)
        else:
            parts = [block.get("content", {}).get("text", "") for block in update.get("content") or []
                     if isinstance(block, Mapping) and isinstance(block.get("content"), Mapping)]
            content = "".join(part for part in parts if isinstance(part, str))
        event = {"type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": call_id, "content": content,
             "is_error": update.get("status") == "failed"}]},
            "parent_tool_use_id": None, "session_id": self.session_id}
        return [*events, grok._readable_results(event)]

    def _flush(self) -> list[dict[str, Any]]:
        """The ``assistant`` event of the response so far (and its usage event), none when it has nothing."""
        self._close()
        if not self._blocks:
            return []
        blocks, self._blocks = self._blocks, []
        calls = any(block["type"] == "tool_use" for block in blocks)
        if not calls:
            self.final_text = "".join(block["text"] for block in blocks if block["type"] == "text")
        message: dict[str, Any] = {
            "id": f"msg_{self._messages}", "type": "message", "role": "assistant", "model": self.model or "grok",
            "content": blocks, "stop_reason": "tool_use" if calls else "end_turn", "stop_sequence": None}
        if self._usage is not None:
            message["usage"], self._usage = self._usage, None
        self._messages += 1
        return self._stream._assistant({"type": "assistant", "message": message, "parent_tool_use_id": None,
                                        "session_id": self.session_id})

    def finish(self, response: Mapping[str, Any] | None, *, duration_ms: int, stop: str | None = None,
               ) -> list[dict[str, Any]]:
        """The rest of the response, then the turn's ``result`` from the ``session/prompt`` response (or from
        ``stop``, the reason Looper ended the turn)."""
        events = self._flush()
        result = response.get("result") if isinstance(response, Mapping) else None
        error = response.get("error") if isinstance(response, Mapping) else None
        meta = result.get("_meta") if isinstance(result, Mapping) and isinstance(result.get("_meta"), Mapping) else {}
        usage = meta.get("usage") if isinstance(meta.get("usage"), Mapping) else {}
        reason = stop or (result.get("stopReason") if isinstance(result, Mapping) else None)
        event: dict[str, Any] = {
            "type": "result", "subtype": "success", "is_error": False, "duration_ms": duration_ms,
            "duration_api_ms": _count(usage, "apiDurationMs"), "num_turns": _count(usage, "numTurns"),
            "result": self.final_text, "stop_reason": reason, "session_id": self.session_id}
        if error is not None or reason != "end_turn":
            event.update(subtype="error_max_turns" if stop == "max_turns" else "error_during_execution",
                         is_error=True)
            event["errors"] = [_STOP_ERRORS.get(str(reason)) or (
                str(error.get("message") if isinstance(error, Mapping) else error) if error is not None
                else f"the turn ended with stop reason {reason!r}")]
        total = _uncached(usage)
        event["usage"] = {"input_tokens": total["inputTokens"], "output_tokens": total["outputTokens"],
                          "cache_read_input_tokens": total["cacheReadInputTokens"],
                          "cache_creation_input_tokens": total["cacheCreationInputTokens"]}
        models = usage.get("modelUsage") if isinstance(usage.get("modelUsage"), Mapping) else {}
        event["modelUsage"] = {str(name): _uncached(row) for name, row in models.items() if isinstance(row, Mapping)} \
            or {self.model or "grok": total}
        ticks = usage.get("costUsdTicks")
        if isinstance(ticks, int) and not isinstance(ticks, bool) and ticks > 0:
            event["total_cost_usd"] = ticks / TICKS_PER_USD  # absent, not 0, when Grok does not know it
        return [*events, event]


# --- The session ---


class AcpGrok:
    """The Manager's or a Planner's Grok session over ACP (the module's notes); the same interface as
    ``grok.ResidentGrok`` (``runtimes.ResidentSession``).

    ``start`` keeps the command and starts nothing; it fails only when the executable cannot be found. The first
    turn starts the process and the session; the next ones are ``session/prompt``s to it.
    """

    runtime_label = "Grok"

    def __init__(self, *, label: str) -> None:
        self._label = label
        self._legacy = grok.ResidentGrok(label=label)  # the per-turn session, for what ACP cannot serve
        self._fallback = False
        self._command: list[str] = []
        self._base: list[str] = []
        self._launch: AcpLaunch | None = None
        self._signature: Any = None
        self._cwd = ""
        self._env: dict[str, str] | None = None
        self._session: str | None = None
        self._ended = False  # the last turn ended without a result
        self._totals = Usage()
        self._price: Price | None = None
        self._autonomous: Callable[[Any], Awaitable[Any]] | None = None
        # The process and what talks to it.
        self._process: asyncio.subprocess.Process | None = None
        self._tasks: list[asyncio.Task[Any]] = []
        self._stderr: asyncio.Task[str] | None = None
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._inbox: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        self._requests = 0
        self._prompt_id: int | None = None
        self._turn_cancel: TurnCancel | None = None  # the running turn's cancel (what ``interrupt`` requests)
        self._done: asyncio.Future[StopMethod] | None = None  # how the turn ended, once it has (the ``method`` slot)
        self._interrupting: asyncio.Task[None] | None = None  # the stop's own work (ending the process), once asked
        self._tree: proc_tree.TurnTree | None = None  # the process's tree, followed while it runs
        self._readying = False  # ... and has not sent its prompt yet
        self._writing: asyncio.Future[None] | None = None  # the prompt's write while it is under way
        self._active = False  # the process holds the session
        self._can_load = False
        self._new_session = False

    # --- What the owner reads ---

    @property
    def label(self) -> str:
        return self._label

    @property
    def shares_bridge(self) -> bool:
        return not self._fallback

    @property
    def started(self) -> bool:
        return self._legacy.started if self._fallback else bool(self._command)

    @property
    def command(self) -> list[str]:
        return self._legacy.command if self._fallback else self._command

    @property
    def pid(self) -> int | None:
        if self._fallback:
            return self._legacy.pid
        return self._process.pid if self._process is not None and self._process.returncode is None else None

    @property
    def totals(self) -> Usage:
        return self._legacy.totals if self._fallback else self._totals

    @property
    def transport(self) -> str:
        """``acp`` while the session is served over ACP, ``per-turn`` once it fell back."""
        return "per-turn" if self._fallback else "acp"

    def stale(self, signature: Any) -> bool:
        if self._fallback:
            return self._legacy.stale(signature)
        return self.started and (self._ended or self._signature != signature)

    def _home(self) -> Path:
        source = Path(self._base[1].partition("=")[2])
        return source.parent.parent / "sessions" / session_dir_name(self._label)

    # --- The session ---

    async def start(
        self, command: list[str], *, cwd: str, signature: Any, env: dict[str, str] | None = None,
        totals: Usage | None = None, autonomous: Callable[[Any], Awaitable[Any]] | None = None,
        price: Price | None = None,
    ) -> None:
        """Keep the command for the turns; raises ``OSError`` when its executable cannot be found."""
        base, session, executable = self._legacy._split(list(command))
        if shutil.which(executable) is None:
            raise OSError(f"{executable!r} was not found")
        self._command, self._signature, self._cwd, self._env = list(command), signature, cwd, env
        self._base, self._session, self._ended, self._fallback = base, session, False, False
        self._totals, self._price, self._autonomous = totals or Usage(), price, autonomous
        self._active = False
        try:
            self._launch = parse_command(base)
        except AcpUnavailable as exc:
            self._launch = None
            await self._fall_back(str(exc))

    async def _fall_back(self, reason: str) -> None:
        """Serve the rest of the session per turn, resuming the session ``self._session``."""
        logger.warning("%s: Grok is not served over ACP (%s); its turns run one process each", self._label, reason)
        await self._end_process()
        command = self._base + (grok.GrokRuntime().resume_args(self._session) if self._session else [])
        await self._legacy.start(command, cwd=self._cwd, signature=self._signature, env=self._env,
                                 totals=self._totals, autonomous=self._autonomous, price=self._price)
        self._fallback = True

    async def stop(self, *, kill: bool = False) -> None:
        """Forget the command and the session; the process, with a turn still running, is ended."""
        await self._end_process()
        self._command, self._signature, self._session, self._ended = [], None, None, False
        if self._fallback:
            await self._legacy.stop(kill=kill)
        self._fallback = False
        logger.info("%s stopped its Grok session", self._label)

    # --- The process ---

    async def _spawn(self) -> None:
        assert self._launch is not None
        home = self._home()
        await asyncio.to_thread(self._prepare_home, home)
        profile = home / PROFILE_FILE if self._launch.tools else (home / "agent.md" if self._launch.agent else None)
        argv = acp_argv(self._launch, home, profile)
        self._inbox = asyncio.Queue()
        process = await proc_tree.spawn(
            *argv, cwd=self._cwd, env=self._env, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE)
        self._process, self._active = process, False
        self._tree = proc_tree.track(process, label=self._label)
        self._tree.start_ticker()
        assert process.stdout is not None
        inbox = self._inbox
        self._tasks = [asyncio.create_task(self._read(process.stdout)),
                       asyncio.create_task(self._watch(process, inbox))]
        self._stderr = asyncio.create_task(stderr_tail(process.stderr))
        logger.info("%s started a Grok ACP process (pid=%s)", self._label, process.pid)

    def _prepare_home(self, home: Path) -> None:
        """The owner's home with the run home's files (``grok._sync_home``), and the agent profile of a read-only
        session."""
        assert self._launch is not None
        grok._sync_home(Path(self._base[1].partition("=")[2]), home)
        if self._launch.tools:
            agent = (home / "agent.md").read_text(encoding="utf-8") if self._launch.agent else None
            grok._write_private(home / PROFILE_FILE, agent_profile(agent, self._launch.tools))

    async def _end_process(self) -> None:
        process, self._process, self._active = self._process, None, False
        tree, self._tree = self._tree, None
        tasks, self._tasks = self._tasks, []
        stderr, self._stderr = self._stderr, None
        if process is not None:
            # From the snapshot followed while the process ran; not cut short by a cancel of the caller.
            await proc_tree.finish(tree.end() if tree is not None else proc_tree.end(process, label=self._label))
        for task in (*tasks, *([stderr] if stderr is not None else [])):
            task.cancel()
        await asyncio.gather(*tasks, *([stderr] if stderr is not None else []), return_exceptions=True)
        self._fail_pending(ConnectionError("the Grok process ended"))

    def _fail_pending(self, error: Exception) -> None:
        pending, self._pending = self._pending, {}
        for future in pending.values():
            if not future.done():
                future.set_exception(error)

    async def _send(self, message: dict[str, Any]) -> None:
        process = self._process
        if process is None or process.stdin is None or process.returncode is not None:
            raise ConnectionError("the Grok process is not running")
        try:
            process.stdin.write(json.dumps({"jsonrpc": "2.0", **message}, ensure_ascii=False).encode("utf-8") + b"\n")
            await process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError) as exc:
            raise ConnectionError("the Grok process closed its input") from exc

    async def _request(self, method: str, params: dict[str, Any], *, timeout: float) -> Any:
        self._requests += 1
        number = self._requests
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[number] = future
        try:
            await self._send({"id": number, "method": method, "params": params})
            response = await asyncio.wait_for(future, timeout)
        finally:
            self._pending.pop(number, None)
        if "error" in response:
            raise AcpError(method, response["error"])
        return response.get("result")

    async def _read(self, stdout: asyncio.StreamReader) -> None:
        """Route what Grok writes: responses to their requests (the prompt's, in order with the notifications,
        into the inbox), notifications of the session into the inbox, requests of Grok's own answered."""
        from ..claude_cli import iter_lines

        try:
            async for raw in iter_lines(stdout):
                line = raw.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    message = None
                if not isinstance(message, dict) or message.get("jsonrpc") != "2.0" \
                        or not isinstance(message.get("id", 0), (int, str)) or isinstance(message.get("id"), bool):
                    logger.warning("%s: Grok wrote a line that is not a JSON-RPC 2.0 message (%d bytes)", self._label,
                                   len(raw))
                    continue
                await self._route(message)
        finally:
            self._inbox.put_nowait(None)  # the output ended
            self._fail_pending(ConnectionError("the Grok process ended"))

    async def _watch(self, process: asyncio.subprocess.Process, inbox: asyncio.Queue[dict[str, Any] | None]) -> None:
        """End a waiting turn when the process exits, though a tool it left running still holds its output open."""
        while process.returncode is None:  # ``wait()`` itself returns only once every pipe is closed
            await asyncio.sleep(_EXIT_POLL_SEC)
        inbox.put_nowait(None)

    async def _route(self, message: dict[str, Any]) -> None:
        method, ident = message.get("method"), message.get("id")
        if method is None:  # a response: exactly one of result and error
            if ("result" in message) == ("error" in message):
                logger.warning("%s: Grok wrote a response with %s", self._label,
                               "both result and error" if "result" in message else "neither")
                return
            if self._prompt_id is not None and ident == self._prompt_id:
                self._inbox.put_nowait(message)
            elif isinstance(ident, int) and (future := self._pending.get(ident)) is not None and not future.done():
                future.set_result(message)
            return
        if ident is not None:  # a request of Grok's: with every tool approved there should be none
            if method == "session/request_permission":
                reply: dict[str, Any] = {"result": {"outcome": {"outcome": "cancelled"}}}
            else:
                reply = {"error": {"code": -32601, "message": f"{method} is not supported"}}
            logger.warning("%s: Grok asked %s", self._label, method)
            with contextlib.suppress(ConnectionError):
                await self._send({"id": ident, **reply})
            return
        params = message.get("params")
        if method in (_UPDATE, _EXTENSION) and isinstance(params, dict) and params.get("sessionId") == self._session:
            self._inbox.put_nowait(message)

    async def _ensure(self) -> None:
        """A running process holding the session: spawned, ``initialize``d, then ``session/new`` or
        ``session/load``. Raises ``AcpUnavailable`` (serve per turn) or ``SessionGone``."""
        if self._process is None or self._process.returncode is not None or not self._active:
            if self._process is None or self._process.returncode is not None:
                await self._end_process()
                await self._spawn()
                if self._turn_cancel is not None and self._turn_cancel.requested:
                    raise ConnectionError("the user stopped the turn")  # it came before there was a process to end
            try:
                init = await self._request("initialize", {"protocolVersion": PROTOCOL_VERSION,
                                                          "clientCapabilities": {},
                                                          "clientInfo": {"name": "looper", "version": "1"}},
                                           timeout=INIT_TIMEOUT_SEC)
            except (AcpError, ConnectionError, TimeoutError) as exc:
                raise AcpUnavailable(f"initialize failed: {exc or type(exc).__name__}{await self._stderr_note()}") \
                    from None
            if not isinstance(init, Mapping) or type(init.get("protocolVersion")) is not int \
                    or init.get("protocolVersion") != PROTOCOL_VERSION:
                raise AcpUnavailable("Grok does not speak ACP protocol version " + str(PROTOCOL_VERSION))
            capabilities = init.get("agentCapabilities") if isinstance(init, Mapping) else None
            self._can_load = bool(isinstance(capabilities, Mapping) and capabilities.get("loadSession"))
            if self._session:
                if not self._can_load:
                    raise AcpUnavailable("Grok does not load sessions (loadSession)")
                try:
                    await self._request("session/load", {"sessionId": self._session, "cwd": self._cwd,
                                                         "mcpServers": []}, timeout=SETUP_TIMEOUT_SEC)
                except AcpError as exc:
                    if not _missing_session(exc.error):
                        raise AcpUnavailable(f"session/load failed: {exc}") from None
                    raise SessionGone(str(exc)) from None
            else:
                created = await self._request("session/new", {"cwd": self._cwd, "mcpServers": []},
                                              timeout=SETUP_TIMEOUT_SEC)
                session = created.get("sessionId") if isinstance(created, Mapping) else None
                if not isinstance(session, str) or not session:
                    raise AcpUnavailable("session/new returned no session id")
                self._session, self._new_session = session, True
            self._active = True

    async def _stderr_note(self) -> str:
        """How much the exited process wrote to stderr (its text may hold paths and values: it is not logged)."""
        process, stderr = self._process, self._stderr
        if process is None or stderr is None or process.returncode is None:
            return ""
        with contextlib.suppress(Exception):
            return f" (stderr: {len((await asyncio.wait_for(stderr, 2)).strip())} bytes)"
        return ""

    # --- A turn ---

    async def run_turn(
        self, text: str, on_event: Callable[[dict[str, Any]], Awaitable[None]], *, tag: Any = None,
        stop: Callable[[], CapStop | None] | None = None, cancel: TurnCancel | None = None,
    ) -> ResidentTurn:
        """Run ``text`` as one ``session/prompt`` and hand each event to ``on_event``. A turn given up (an
        exception, cancellation included) is cancelled in Grok before it propagates; ``stop`` is asked after each
        event whether the turn went over its soft cap (``ClaudeRunRecorder.cap_stop``).

        ``cancel`` is the one way to stop the turn by the user. Requested while the session is readied, it ends the
        process (``KILL``; no prompt is sent, the session is kept and the next turn loads it) and does not fall the
        session back to per turn; requested during the prompt, it is ``session/cancel`` (``CANCEL``: the process
        and the session stay, every process the turn started is ended from the tree's snapshot), or, when Grok does
        not answer it within ``CANCEL_GRACE_SEC`` or takes no prompt, the process is ended (``KILL``). From the
        request on the turn's events are not handed to ``on_event`` (its result still ends it). A session served per
        turn (fallen back to before or during the turn) is stopped by the same ``cancel``."""
        from ..claude_cli import ResidentTurn, user_stop_result
        from . import StopMethod
        from .cancel import TurnCancel

        assert self.started
        cancel = cancel or TurnCancel.never()
        if self._fallback:
            return await self._legacy.run_turn(text, on_event, tag=tag, stop=stop, cancel=cancel)
        if cancel.requested:  # stopped before anything was started: Grok is untouched
            return ResidentTurn(user_stop_result(), session_started=False, usage=Usage(), stopped=StopMethod.KILL,
                                prompt_sent=False)
        self._turn_cancel, self._interrupting = cancel, None
        self._done = asyncio.get_running_loop().create_future()
        self._readying = True
        unregister = cancel.on_request(self._on_stop)
        turn: ResidentTurn | None = None
        try:
            turn = await self._run_turn(text, on_event, tag=tag, stop=stop, cancel=cancel)
            await self._settle_stop()
            return turn
        finally:
            unregister()
            self._readying = False
            done, self._done = self._done, None
            self._turn_cancel, self._interrupting = None, None
            if done is not None and not done.done():  # what ``interrupt`` returns: how the turn ended
                done.set_result(turn.stopped if turn is not None and turn.stopped else StopMethod.KILL)

    def _stop_bound(self) -> float:
        return CANCEL_GRACE_SEC + proc_tree.TERM_GRACE_SEC + proc_tree.REAP_SEC

    async def _settle_stop(self) -> None:
        """Wait (bounded) for the stop's own work, ending the process, to be done."""
        task = self._interrupting
        if task is not None:
            await asyncio.wait({task}, timeout=self._stop_bound())

    def _on_stop(self) -> None:
        """The running turn's cancel was requested. Readying: the process is ended (a process not yet spawned is
        ended as it appears: ``_ensure``). Prompt sent: the turn sees the marker in its inbox and cancels; a prompt
        write that does not finish in ``CANCEL_GRACE_SEC`` ends the process."""
        if self._interrupting is not None:
            return
        if self._readying:
            # Nothing to cancel yet; readying takes up to INIT_TIMEOUT_SEC and SETUP_TIMEOUT_SEC.
            self._interrupting = asyncio.ensure_future(self._end_process())
        elif self._prompt_id is not None:
            self._inbox.put_nowait(_USER_STOP)
            writing, process = self._writing, self._process
            if writing is not None and process is not None:
                self._interrupting = asyncio.ensure_future(self._end_stuck(writing, process))

    async def _end_stuck(self, writing: asyncio.Future[None], process: asyncio.subprocess.Process) -> None:
        if not (await asyncio.wait({writing}, timeout=CANCEL_GRACE_SEC))[0] and self._process is process:
            logger.warning("%s: Grok took no prompt in %ss; ending its process", self._label, CANCEL_GRACE_SEC)
            await self._end_process()

    async def _run_turn(
        self, text: str, on_event: Callable[[dict[str, Any]], Awaitable[None]], *, tag: Any,
        stop: Callable[[], CapStop | None] | None, cancel: TurnCancel,
    ) -> ResidentTurn:
        from ..claude_cli import ResidentTurn, user_stop_result
        from . import StopMethod

        try:
            await self._ensure()
        except BaseException as exc:
            if cancel.requested and isinstance(exc, Exception):
                # The user's stop ended the process while the session was readied: no prompt was sent, the session
                # is kept, and the next turn starts a process that loads it. (Not a reason to fall back.)
                logger.info("%s: the user stopped the turn while Grok readied its session", self._label)
                await self._settle_stop()
                await self._end_process()
                return ResidentTurn(user_stop_result(), session_started=False, usage=Usage(), stopped=StopMethod.KILL,
                                    prompt_sent=False)
            if isinstance(exc, AcpUnavailable):
                await self._fall_back(str(exc))
                self._readying = False
                # The legacy turn sees a request that came meanwhile (``cancel.requested``) and starts no process.
                return await self._legacy.run_turn(text, on_event, tag=tag, stop=stop, cancel=cancel)
            if isinstance(exc, SessionGone):
                logger.warning("%s: Grok no longer has session %s (%s)", self._label, self._session, exc)
                self._ended = True
                return ResidentTurn(None, 1, session_started=False)
            if isinstance(exc, (AcpError, ConnectionError, TimeoutError)):
                logger.warning("%s: Grok could not start its session: %s", self._label, exc)
                code = await self._lost()
                return ResidentTurn(None, code, session_started=False)
            self._ended = True
            await self._end_process()
            raise
        return await self._prompt(text, on_event, stop, cancel)

    async def end_turn_processes(self) -> None:
        """After a ``CANCEL``: end what the turn started (tools of Grok's that outlived the cancel). A session that
        fell back runs per turn: the turn's tree is the per-turn session's."""
        if self._fallback:
            await self._legacy.end_turn_processes()
            return
        tree = self._tree
        if tree is None:
            return
        try:
            await tree.end_new()
        except Exception:
            logger.warning("%s: could not end the cancelled turn's processes", self._label, exc_info=True)

    async def _prompt(
        self, text: str, on_event: Callable[[dict[str, Any]], Awaitable[None]],
        stop: Callable[[], CapStop | None] | None, cancel: TurnCancel,
    ) -> ResidentTurn:
        from ..claude_cli import ResidentTurn, user_stop_result
        from . import StopMethod

        if self._tree is not None:
            # The process and its MCP servers: what the turn starts is what a stop ends. (Before anything below,
            # which reads state an await could change.)
            await self._tree.baseline()
        assert self._session is not None and self._launch is not None
        session, new = self._session, self._new_session
        while not self._inbox.empty():  # what the process wrote between turns
            self._inbox.get_nowait()
        turn = AcpTurn(model=self._launch.model, session_id=session)
        if cancel.requested:
            # Asked before the prompt: it is not sent (the process is ended by the stop; the session is kept).
            return ResidentTurn(user_stop_result(), session_started=True, usage=Usage(), stopped=StopMethod.KILL,
                                prompt_sent=False)
        if stop is not None and (early := stop()) is not None:
            # A turn of text alone writes no event before its end that ``stop`` would be asked after, so the prompt
            # is not sent.
            await on_event(turn.init(cwd=self._cwd))
            self._totals = self._totals.plus(early.usage)
            return ResidentTurn(early.result_event, session_started=True, usage=early.usage)
        started = time.monotonic()
        self._requests += 1
        self._prompt_id = self._requests
        self._readying = False
        reason: str | None = None  # why Looper ended the turn
        capped: CapStop | None = None
        user_stopped = False  # the user's stop ended the turn
        answered = False  # ... after Grok had answered the prompt already
        response: dict[str, Any] | None = None
        try:
            try:
                self._writing = asyncio.ensure_future(self._send(
                    {"id": self._prompt_id, "method": "session/prompt",
                     "params": {"sessionId": session, "prompt": [{"type": "text", "text": text}]}}))
                try:
                    await self._writing
                    cancel.sent()
                except ConnectionError:
                    if not cancel.requested:
                        raise
                    user_stopped = True  # the prompt could not be written: the stop ended the process
                finally:
                    self._writing = None
                if not cancel.requested:
                    await on_event(turn.init(cwd=self._cwd))
                while not user_stopped:
                    message = await self._inbox.get()
                    if message is None:
                        break  # the process ended before it answered
                    if message is _USER_STOP:
                        user_stopped = True
                        response = await self._cancel(session)
                        break
                    if message.get("method") is None:
                        response = message
                        if cancel.requested:  # the stop was asked first: the answer is not the turn's end
                            user_stopped = answered = True
                        break
                    if cancel.requested:  # asked, its marker still queued behind this: stop now
                        user_stopped = True
                        response = await self._cancel(session)
                        break
                    for event in turn.feed(message):
                        if cancel.requested:  # (R1) from the request on, nothing reaches ``on_event``
                            break
                        await on_event(event)
                        if reason is None and stop is not None and (capped := stop()) is not None:
                            reason = "cap"
                    if cancel.requested:
                        continue  # the marker is queued: the next read sees it
                    if reason is None and self._launch.max_turns and turn.responses > self._launch.max_turns:
                        reason = "max_turns"
                    if reason is not None:
                        response = await self._cancel(session)
                        break
            except BaseException:
                await self._abandon(session)
                raise
            sent = cancel.prompt_sent
            if response is None:  # the process died mid-turn, or the stop had to end it
                code = await self._lost()
                if user_stopped or cancel.requested:
                    return ResidentTurn(user_stop_result(), code, session_started=True, usage=self._turn_usage(None),
                                        stopped=StopMethod.KILL, prompt_sent=sent)
                return ResidentTurn(None, code, session_started=True, usage=self._turn_usage(None), prompt_sent=sent)
            elapsed = int((time.monotonic() - started) * 1000)
            events = turn.finish(response, duration_ms=elapsed,
                                 stop="cancelled" if answered else "max_turns" if reason == "max_turns" else None)
            for event in events[:-1]:
                if cancel.requested:  # an answer the stop outran is not shown
                    user_stopped = answered = True
                    break
                await on_event(event)
            result = events[-1]
            if new:
                self._new_session = False
                await asyncio.to_thread(grok.prune_sessions, self._home(), session)
            if capped is not None:  # the turn ends with the cap's result, not the cancelled turn's
                self._totals = self._totals.plus(capped.usage)
                if cancel.requested:
                    await self.end_turn_processes()
                return ResidentTurn(capped.result_event, session_started=True, usage=capped.usage, prompt_sent=sent,
                                    stopped=StopMethod.CANCEL if cancel.requested else None)
            if cancel.requested and not user_stopped:
                user_stopped = True  # asked after Grok answered, before the turn handed its result on
            if user_stopped:
                result = _cancelled(result)
            await on_event(result)
            if cancel.requested and not user_stopped:
                user_stopped, result = True, _cancelled(result)  # asked while its result was handled
            if user_stopped:
                await self.end_turn_processes()  # (a CANCEL ending: the process and the session stay)
            return ResidentTurn(result, session_started=True, usage=self._turn_usage(result), prompt_sent=sent,
                                stopped=StopMethod.CANCEL if user_stopped else None)
        finally:
            self._prompt_id = None

    async def interrupt(self) -> StopMethod | None:
        """A wrapper over the running turn's cancel: request it and wait (bounded) for the stop's work and the turn
        to end; how the turn ended (``CANCEL``, ``KILL``; see ``run_turn``). None when no turn runs."""
        from . import StopMethod

        cancel, done = self._turn_cancel, self._done
        if cancel is None or done is None:
            return await self._legacy.interrupt() if self._fallback else None
        cancel.request()
        try:
            await asyncio.wait_for(asyncio.shield(done), self._stop_bound() + 1)
        except asyncio.TimeoutError:
            logger.warning("%s: the stopped turn did not end in time", self._label)
            return StopMethod.KILL
        return done.result()

    def _turn_usage(self, result_event: dict[str, Any] | None) -> Usage:
        own = run_usage(result_event, price=self._price)
        if result_event is not None:
            self._totals = self._totals.plus(own)
        return own

    async def _cancel(self, session: str) -> dict[str, Any] | None:
        """``session/cancel`` and wait for the prompt's response (its notifications are dropped); None, with the
        process ended, when it does not come in ``CANCEL_GRACE_SEC``."""
        try:
            async with asyncio.timeout(CANCEL_GRACE_SEC):  # the write and the wait share one grace period
                with contextlib.suppress(ConnectionError):
                    await self._send({"method": "session/cancel", "params": {"sessionId": session}})
                while (message := await self._inbox.get()) is not None:
                    if message is _USER_STOP:
                        continue  # a stop that raced the cancel: the cancel is already sent
                    if message.get("method") is None and message.get("id") == self._prompt_id:
                        return message
        except TimeoutError:
            logger.warning("%s: Grok did not end a cancelled turn in %ss; ending its process", self._label,
                           CANCEL_GRACE_SEC)
        await self._lost()
        return None

    async def _abandon(self, session: str) -> None:
        """A turn given up: cancelled in Grok, which keeps the session; the process is ended when it will not."""
        self._ended = False
        if await self._cancel(session) is None:
            self._ended = True

    async def _lost(self) -> int | None:
        """The process died or had to be ended: the turn has no result and the owner starts over. Returns its
        exit code, if it had exited."""
        process = self._process
        if process is not None:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(process.wait(), 1)
        code = process.returncode if process is not None else None
        errors = await self._stderr_note()
        logger.warning("%s: the Grok turn ended without a result (exit code %s)%s", self._label, code, errors)
        self._ended = True
        await self._end_process()
        return code
