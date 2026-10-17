"""Helpers for running the Claude Code CLI as a subprocess and recording its runs."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import time
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol
from uuid import uuid4

from . import proc_tree
from .run_usage import Price, Usage, compaction, reported_totals, request_context, request_tokens, run_usage

if TYPE_CHECKING:
    from ..db.llm_records import RecordStatus
    from .profile_registry import ProviderSpec
    from .agent_settings import Launch
    from .knowledge import AgentKnowledge
    from .library import AgentLibrary
    from .runtimes import RuntimeAdapter
    from .runtimes.pricing import RunSpend
    from .system_prompt import SystemPrompt

logger = logging.getLogger(__name__)


# --- What an agent's Claude Code loads ---
#
# An agent sees what Looper composes and the project's own CLAUDE.md, not the
# personal Claude Code configuration of whoever runs Looper. Verified against
# Claude Code 2.1.285:
# - "--setting-sources project" leaves out the user and local settings and,
#   with them, user and local memory (~/.claude/CLAUDE.md and its @imports,
#   CLAUDE.local.md), user hooks, skills, custom agents, output styles,
#   plugins (their skills, agents and hooks too) and user MCP servers. The
#   project's .claude/settings.json and its CLAUDE.md still load.
# - "--strict-mcp-config" also drops MCP servers from other sources, such as
#   claude.ai connectors.
# - Claude Code reads CLAUDE.md, .claude/CLAUDE.md and .claude/rules/ in every
#   directory from the working directory up to "/" as project memory, so a
#   directory under $HOME still loads ~/.claude/CLAUDE.md; "claudeMdExcludes"
#   leaves out everything above the repository.
# - Where a project has no CLAUDE.md of its own, Claude Code reads its AGENTS.md
#   files instead, from the same directories (its built-in default, the
#   "instructionFiles" setting "claude-md-or-agents-md"), so an AGENTS.md above
#   the repository came in too: "claudeMdExcludes" leaves those out as well. An
#   excluded CLAUDE.md does not keep the project's own AGENTS.md out
#   (runtimes.instructions).
# - "autoMemoryEnabled": false turns off the per-directory auto memory under
#   ~/.claude/projects/.
# Authentication is unaffected. "--bare" and a separate CLAUDE_CONFIG_DIR were
# rejected: both lose the operator's login.
#
# What an agent has comes from the capability catalog instead
# (looper/agents/library.py): the plugins and skills installed for its role,
# globally or in its project, each a "--plugin-dir" (library_args). Verified against Claude Code 2.1.285: with
# the flags above, "--plugin-dir" in --print mode loads a plugin's skills
# (named <plugin>:<skill>), agents, commands and hooks; a plugin needs no more
# than a directory (a manifest names it). The operator's own hooks, such as a
# UserPromptSubmit hook in ~/.claude/settings.json, still do not run.
# The MCP servers installed for the run come in one "--mcp-config" file
# (looper/agents/mcp.py). Verified against Claude Code 2.1.285 in --print
# stream-json mode with the flags above: a file {"mcpServers": {<name>:
# {"type": "stdio", "command", "args", "env"} or {"type": "http" | "sse",
# "url", "headers"}}} loads each server beside --strict-mcp-config (init lists
# it with source "dynamic", its tools as mcp__<name>__<tool>, and the model
# calls them without a prompt); --strict-mcp-config still drops every other
# source, claude.ai connectors and a plugin's own MCP servers (a --plugin-dir
# with .mcp.json) included, so a plugin's servers never load.

# Names this flag set in every run's record (prompt_layers.isolation); change
# it whenever the flags change.
ISOLATION_ID = "personal-config-off/v2"


def repository_root(directory: Path) -> Path:
    """The nearest directory at or above ``directory`` holding ``.git``; ``directory`` without one."""
    for candidate in (directory, *directory.parents):
        if (candidate / ".git").exists():
            return candidate
    return directory


def _outside_memory(cwd: Path) -> list[str]:
    """The memory files Claude Code would read from outside the project.

    That is, from every directory above the repository root (the work
    directory itself when it is not in a repository), and the operator's
    user memory wherever the repository is.
    """
    patterns: list[str] = []

    def add(directory: Path, *names: str) -> None:
        base = str(directory).rstrip("/")
        for name in names:
            if (pattern := f"{base}/{name}") not in patterns:
                patterns.append(pattern)

    # Both spellings of a path through a symbolic link.
    for directory in dict.fromkeys((cwd.absolute(), cwd.resolve())):
        for parent in repository_root(directory).parents:
            add(parent, "CLAUDE.md", ".claude/CLAUDE.md", ".claude/rules/**", "AGENTS.md", ".claude/AGENTS.md")
    config_dirs = [Path.home() / ".claude"]
    if os.environ.get("CLAUDE_CONFIG_DIR"):
        config_dirs.append(Path(os.environ["CLAUDE_CONFIG_DIR"]).expanduser())
    for config_dir in config_dirs:
        add(config_dir.absolute(), "CLAUDE.md", "rules/**", "AGENTS.md")
    return patterns


def isolation_args(cwd: str | os.PathLike[str]) -> list[str]:
    """The flags that keep the operator's personal Claude Code configuration out of an agent run in ``cwd``.

    Every agent's command carries them, so the agents cannot diverge. The
    result depends only on ``cwd`` (and where the operator's configuration
    lives), so a resident agent's command stays the same between turns.
    """
    settings = {"autoMemoryEnabled": False, "claudeMdExcludes": _outside_memory(Path(cwd))}
    return [
        "--setting-sources", "project",
        "--strict-mcp-config",
        "--settings", json.dumps(settings, ensure_ascii=False, separators=(",", ":")),
    ]


def library_args(library: AgentLibrary) -> list[str]:
    """The flags that load the items installed for an agent run: one ``--plugin-dir`` per plugin directory, and
    ``--mcp-config`` with the file of its MCP servers when it has any.

    Every agent's launch carries them (``agent_settings.Launch.args``), so the
    agents cannot diverge. The generated skill plugins come after the
    plugins: ``personal`` and then a project's own skills, ``project``.
    Verified against Claude Code 2.1.285 with the isolation flags above: the
    init event lists a generated plugin's skills as ``<plugin>:<name>``, the
    model invokes one with the Skill tool from a read-only directory, and two
    plugins of the same name both load, their skills listed under the one
    prefix.
    """
    return [*(arg for directory in library.plugin_dirs for arg in ("--plugin-dir", directory)),
            *(["--mcp-config", library.mcp_config] if library.mcp_config else [])]


def knowledge_args(knowledge: AgentKnowledge) -> list[str]:
    """The flags that mount a run's project knowledge: one ``--add-dir`` per directory.

    Every agent's launch carries them (``agent_settings.Launch.args``); only a
    task agent's launch has knowledge, so the Manager's adds nothing. Verified
    against Claude Code 2.1.285 with the isolation flags above: the agent reads
    files in an added directory, and a CLAUDE.md in it or above it is not
    loaded. ``--add-dir`` takes several values, so each directory gets its own.
    """
    return [arg for directory in knowledge.add_dirs for arg in ("--add-dir", directory)]


# --- What an agent may do ---
#
# Every agent (Manager, Planner, Worker, Reviewer) has every tool and runs
# without permission prompts (RFC decision 8). What an agent is for is the
# job of its role prompt and its code-owned contract, not of a tool list. The
# flags reach every command through agent_settings.Launch.args, beside the
# library's, so the four agents cannot diverge. Verified against Claude Code
# 2.1.285 in --print mode with the isolation flags above and a --plugin-dir:
# the init event reports permissionMode "bypassPermissions", and Bash, Write
# and a plugin skill that declares allowed-tools all run without a denial,
# where the default mode denies the Write and the skill. The isolation
# --settings (claudeMdExcludes, autoMemoryEnabled) leave permissions alone.
# Since the Manager can now run any command on this machine, the chat
# channels that reach it pass on only the senders they list
# (channels/allowlist.py).


def budget_args(max_budget_usd: float | None) -> list[str]:
    """The flag that caps one Claude Code process's spend, for an agent whose settings set one.

    Verified against Claude Code 2.1.285 in ``--print`` stream-json mode: Claude checks the cap after each
    API request, so a turn may overrun it by one request, and the turn that crosses it ends with a ``result``
    of subtype ``error_max_budget_usd`` and no reply text. The cap counts this process's spend only (not
    what a resumed session spent before), and every later turn of the process ends the same way at once.
    """
    if max_budget_usd is None:
        return []
    from .project_execution import format_usd

    return ["--max-budget-usd", format_usd(max_budget_usd)]


BUDGET_SUBTYPE = "error_max_budget_usd"
# Marks the result Looper ends a run with when it stops the run at its soft cap (``soft_cap_result``).
SOFT_CAP = "looper_soft_cap"


def budget_error(result_event: dict[str, Any] | None) -> str | None:
    """Why a run stopped at its spend cap (``--max-budget-usd``, or Looper's soft cap); None when it did not."""
    if not result_event or result_event.get("subtype") != BUDGET_SUBTYPE:
        return None
    errors = result_event.get("errors")
    detail = "; ".join(str(e) for e in errors) if isinstance(errors, list) and errors else "budget reached"
    if result_event.get(SOFT_CAP):
        return f"Looper stopped the run at its per-run spend cap: {detail}"
    return f"Claude Code stopped at its per-run spend cap (--max-budget-usd): {detail}"


# --- Looper's own per-run spend cap ---
#
# With a provider that Looper prices itself (runtimes.pricing), --max-budget-usd would count Claude's prices,
# so Looper adds up the run's usage events as they stream and stops the run once it is over its cap: a soft cap,
# since the request that crosses it is already made. A run stopped this way ends with ``soft_cap_result``, which
# ``budget_error`` reads like Claude Code's own. The stream carries the main loop's requests; a subagent's
# requests and Claude Code's side requests appear only in the result's totals, so they count toward the run's
# recorded cost but not toward stopping it.


@dataclass(frozen=True)
class CapStop:
    """A run that went over its soft cap: the result it ends with, and what it spent until then."""

    result_event: dict[str, Any]
    usage: Usage


def soft_cap_result(spent: float, cap: float) -> dict[str, Any]:
    """The result a run stopped at its soft cap ends with."""
    from .project_execution import format_usd

    return {"type": "result", "subtype": BUDGET_SUBTYPE, "is_error": True, "result": "", SOFT_CAP: True,
            "errors": [f"${format_usd(spent)} at the provider's prices is over the ${format_usd(cap)} cap"]}


def _request_usage(event: dict[str, Any], requests: dict[Any, dict[str, Any]]) -> dict[str, Any] | None:
    """The internal usage event of the model request ``event`` ends, if it ends one.

    Verified against Claude Code 2.1.285 with ``--include-partial-messages``:
    each request streams a ``message_start`` (its model and its usage so far),
    a ``message_delta`` with the final usage and a ``message_stop``; an
    assistant event's usage is a snapshot with its output still at 0. The
    requests of the main loop and of each subagent (``parent_tool_use_id``)
    are told apart.
    """
    if event.get("type") != "stream_event" or not isinstance(event.get("event"), dict):
        return None
    inner, key = event["event"], event.get("parent_tool_use_id")
    kind = inner.get("type")
    if kind == "message_start":
        message = inner.get("message") if isinstance(inner.get("message"), dict) else {}
        usage = message.get("usage") if isinstance(message.get("usage"), dict) else {}
        requests[key] = {"model": message.get("model"), "usage": dict(usage)}
    elif kind == "message_delta" and key in requests and isinstance(inner.get("usage"), dict):
        requests[key]["usage"].update({k: v for k, v in inner["usage"].items() if v is not None})
    elif kind == "message_stop" and key in requests:
        from .runtimes.pricing import usage_event

        request = requests.pop(key)
        model = request["model"] if isinstance(request["model"], str) else None
        return usage_event(model, request_tokens(request["usage"]), parent_tool_use_id=key)
    return None


def permission_args() -> list[str]:
    """The flags that let an agent use every tool without asking; every agent's launch carries them."""
    return ["--dangerously-skip-permissions"]


def system_prompt_args(prompt_file: Path) -> list[str]:
    """Append the system prompt in ``prompt_file`` (``SystemPrompt.write_file``).

    By default Claude Code keeps the system prompt of a session's first
    request and sends it again on every resume, ignoring a changed file; the
    snapshot is turned off so that a restarted resident agent runs with the
    prompt its record names.
    """
    return ["--append-system-prompt-file", str(prompt_file), "--system-prompt-snapshot", "off"]

_READ_CHUNK = 64 * 1024


async def iter_lines(stream: asyncio.StreamReader) -> AsyncIterator[bytes]:
    """Yield the stream's lines, each with its newline, with no length limit.

    ``StreamReader.readline()`` fails on lines longer than the reader's limit,
    and Claude's stream-json lines (the tool and skill list, large tool
    results) have no fixed bound. A final line without a newline is yielded
    as is.
    """
    pending = bytearray()
    while chunk := await stream.read(_READ_CHUNK):
        search_from = len(pending)  # earlier bytes were already searched
        pending += chunk
        start = 0
        while (end := pending.find(b"\n", search_from)) != -1:
            yield bytes(pending[start:end + 1])
            start = search_from = end + 1
        del pending[:start]
    if pending:
        yield bytes(pending)


async def stream_events(stream: asyncio.StreamReader) -> AsyncIterator[dict[str, Any]]:
    """Claude's stream-json events on ``stream``, in order, each model request followed by its internal usage
    event (``_request_usage``); a line that is not a JSON object is skipped."""
    requests: dict[Any, dict[str, Any]] = {}
    async for raw_line in iter_lines(stream):
        line = raw_line.decode("utf-8", errors="replace").strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            yield event
            if (usage := _request_usage(event, requests)) is not None:
                yield usage


async def write_user_turn(stdin: asyncio.StreamWriter, text: str, *, uuid: str | None = None) -> None:
    """One user message in Claude's stream-json input format; Claude keeps the ``uuid`` given."""
    message: dict[str, Any] = {"type": "user", "message": {"role": "user", "content": text}}
    if uuid is not None:
        message["uuid"] = uuid
    line = json.dumps(message, ensure_ascii=False)
    stdin.write(line.encode("utf-8") + b"\n")
    await stdin.drain()


# How long a stopping resident Claude may take to exit after its stdin closes before it is ended.
STOP_GRACE_SEC = 5.0
# How long the rest of an exited Claude's output may take to be read.
_DRAIN_SEC = 2.0
# How many events written between turns are kept for the turn Claude may run on its own next.
_BETWEEN_LIMIT = 2_000


# --- Which turn an event belongs to ---
#
# A resident Claude also runs turns that answer no user message. Verified
# against Claude Code 2.1.285 (recorded in tests/fixtures/claude_stream/
# background_*.jsonl):
# - A user message written with a "uuid" keeps it. Claude writes a
#   "command_lifecycle" event for it ("command_uuid") in state "queued" when it
#   reads the message, "started" when it begins the message's turn and
#   "completed" right after the turn's result. Then come the turn's
#   system/init and, with --replay-user-messages, the message's replay
#   ("isReplay", the same uuid). The result names the message in
#   "user_message_uuid" and "user_message_uuids". Without
#   --replay-user-messages only the replay is missing.
# - Background work a turn starts (a Bash command or a subagent run in the
#   background) ends later. If no turn is running then, Claude writes
#   system/task_notification and runs a turn of its own: system/init up to a
#   result that names no message and carries an "origin" ({"kind":
#   "task-notification", ...}), with no lifecycle event and no replay. A
#   background subagent's own events ("parent_tool_use_id") come before it. A
#   message that arrives meanwhile is "queued" at once but "started" only
#   after that result.
# - Work that ends during a user turn is folded into that turn (a replayed
#   <task-notification> message with an "origin") and answered by its result.
# So a user turn starts at its message's "started" event (or its replay) and
# ends at the next result; an event outside it is part of a turn Claude runs
# on its own, which runs from a system/init (or a lone result) to a result.

# The input recorded for a turn Claude ran on its own.
AUTONOMOUS_INPUT = "(no user message: Claude Code ran this turn on its own after background work it had started ended)"


@dataclass(frozen=True)
class ResidentTurn:
    """How one turn on a resident Claude ended.

    ``result_event`` is the turn's ``result``, or None when Claude exited
    before writing it; then ``exit_code`` is the reaped process's exit code.
    ``session_started`` says whether Claude began the turn with its
    ``system/init`` event, as every turn it actually runs does. Without one,
    Claude never got a session going: told to ``--resume`` a session it no
    longer has, it answers only with an error result (``num_turns`` 0,
    "No conversation found" in ``errors``) or exits without a word.
    ``usage`` is the turn's own cost and tokens (``ResidentClaude.turn_usage``).
    """

    result_event: dict[str, Any] | None
    exit_code: int | None = None
    session_started: bool = True
    usage: Usage = field(default_factory=Usage)


def exit_note(exit_code: int | None) -> str:
    """`` (exit code N)`` when Claude exited with one, else nothing."""
    return f" (exit code {exit_code})" if exit_code is not None else ""


class AutonomousTurn(Protocol):
    """What an owner does with one turn its Claude ran on its own."""

    async def event(self, event: dict[str, Any]) -> None:
        """One of the turn's events, in order; the events written since the last turn come first."""

    async def end(self, turn: ResidentTurn) -> None:
        """The turn ended: with its result, or without one when Claude stopped or moved on first."""


# Called when Claude begins a turn of its own, with the ``tag`` of the user turn it ran last (None before any).
AutonomousHandler = Callable[[Any], Awaitable[AutonomousTurn]]


def _is_replay(event: dict[str, Any]) -> bool:
    return event.get("type") == "user" and event.get("isReplay") is True


def _is_init(event: dict[str, Any]) -> bool:
    return event.get("type") == "system" and event.get("subtype") == "init"


@dataclass(eq=False)
class _UserTurn:
    """A user message written to Claude, and its turn once Claude starts it."""

    uuid: str
    text: str
    tag: Any
    on_event: Callable[[dict[str, Any]], Awaitable[None]]
    done: asyncio.Future[dict[str, Any] | None]
    started: bool = False
    session_started: bool = False
    usage: Usage = field(default_factory=Usage)
    stop: Callable[[], CapStop | None] | None = None  # asked after each event: end the turn at its soft cap?
    capped: CapStop | None = None  # the turn was ended at its soft cap

    def starts_with(self, event: dict[str, Any]) -> bool:
        """Claude began this message's turn: its "started" lifecycle event, or its replay."""
        if event.get("type") == "command_lifecycle":
            return event.get("command_uuid") == self.uuid and event.get("state") == "started"
        if not _is_replay(event):
            return False
        message = event.get("message")
        return event.get("uuid") == self.uuid or (isinstance(message, dict) and message.get("content") == self.text)

    def owns(self, event: dict[str, Any]) -> bool:
        """One of this message's lifecycle events ("queued" before its turn starts)."""
        return event.get("type") == "command_lifecycle" and event.get("command_uuid") == self.uuid


class _Output:
    """One resident process's output, read while it runs: each event goes to the turn it belongs to."""

    def __init__(self, claude: ResidentClaude, stdout: asyncio.StreamReader, autonomous: AutonomousHandler | None) -> None:
        self._claude = claude
        self._stdout = stdout
        self._autonomous = autonomous
        self._task: asyncio.Task[None] | None = None
        self.ended = False  # the output ended, or reading it failed
        self.turn: _UserTurn | None = None  # the user message waiting for, or in, its turn
        self._last_tag: Any = None  # the tag of the user turn Claude ran last
        # Whether this Claude has marked a user turn (lifecycle events, replays). Until it has (the
        # first events of a process, a stand-in), the pending message's turn starts with the next event.
        self._marks_turns = False
        self._between: deque[dict[str, Any]] = deque(maxlen=_BETWEEN_LIMIT)  # events outside any turn
        self._own_open = False  # a turn of Claude's own is running
        self._own: AutonomousTurn | None = None  # its owner's handler, if any
        self._capped = False  # a user turn was ended at its soft cap: the rest of the output belongs to no turn

    def read(self) -> None:
        """Start reading, once."""
        if self._task is None:
            self._task = asyncio.create_task(self._read())

    async def _read(self) -> None:
        try:
            async for event in stream_events(self._stdout):
                await self._dispatch(event)
        except Exception as exc:
            # Reading failed, or the owner's handling of a user turn's event did: that turn fails with it.
            turn, self.turn = self.turn, None
            if turn is not None and not turn.done.done():
                turn.done.set_exception(exc)
            else:
                logger.warning("%s: reading Claude's output failed", self._claude.label, exc_info=True)
        finally:
            self.ended = True
            turn, self.turn = self.turn, None
            if turn is not None and not turn.done.done():
                turn.done.set_result(None)  # Claude's output ended before the turn's result
        await self.close_own("Claude Code's output ended")

    async def _dispatch(self, event: dict[str, Any]) -> None:
        if self._capped:
            return  # what Claude writes before it is killed: the turn has ended
        if event.get("type") == "command_lifecycle" or _is_replay(event):
            self._marks_turns = True
        turn = self.turn
        if turn is not None and not turn.started:
            if turn.starts_with(event) or not self._marks_turns:
                await self.close_own("Claude Code began the next message")
                if self._between:
                    logger.debug("%s: %d events between turns belong to no turn", self._claude.label,
                                 len(self._between))
                    self._between.clear()
                turn.started = True
                self._last_tag = turn.tag
            elif turn.owns(event):
                await turn.on_event(event)  # "queued", maybe while Claude runs a turn of its own
                return
        if turn is not None and turn.started:
            if _is_init(event):
                turn.session_started = True
            await turn.on_event(event)
            if event.get("type") == "result":
                turn.usage = self._claude.turn_usage(event)
                self.turn = None
                if not turn.done.done():  # else the turn was given up (cancelled) meanwhile
                    turn.done.set_result(event)
            elif turn.stop is not None and (capped := turn.stop()) is not None:
                turn.capped, self.turn, self._capped = capped, None, True
                if not turn.done.done():
                    turn.done.set_result(capped.result_event)
            return
        if event.get("type") == "command_lifecycle":
            return  # the "completed" of a message whose turn has ended
        await self._outside(event)

    async def _outside(self, event: dict[str, Any]) -> None:
        """An event of no user turn: part of a turn Claude runs on its own, or written between turns."""
        if not self._own_open:
            if not (_is_init(event) or event.get("type") == "result"):
                self._between.append(event)
                return
            await self._begin_own()
        await self._to_own("event", event)
        if event.get("type") == "result":
            # Every result moves the session's running totals, so this turn's share is taken here, in order.
            await self._end_own(ResidentTurn(event, usage=self._claude.turn_usage(event)))

    async def _begin_own(self) -> None:
        self._own_open = True
        logger.info("%s: Claude Code began a turn of its own", self._claude.label)
        if self._autonomous is not None:
            try:
                self._own = await self._autonomous(self._last_tag)
            except Exception:
                logger.warning("%s: could not take a turn of Claude's own", self._claude.label, exc_info=True)
        held = list(self._between)
        self._between.clear()
        for event in held:
            await self._to_own("event", event)

    async def _to_own(self, method: str, value: Any) -> None:
        """Hand ``value`` to the open own turn's handler; its failure never stops the reading."""
        if self._own is None:
            return
        try:
            await getattr(self._own, method)(value)
        except Exception:
            logger.warning("%s: handling a turn of Claude's own failed", self._claude.label, exc_info=True)

    async def _end_own(self, turn: ResidentTurn) -> None:
        await self._to_own("end", turn)
        self._own, self._own_open = None, False

    async def close_own(self, why: str) -> None:
        """End a turn of Claude's own that has no result and will get none."""
        if self._own_open:
            logger.warning("%s: a turn of Claude's own ended without a result: %s", self._claude.label, why)
            await self._end_own(ResidentTurn(None))

    def drop_turn(self) -> None:
        """Hand nothing more to the pending user turn."""
        turn, self.turn = self.turn, None
        if turn is not None and not turn.done.done():
            turn.done.set_result(None)

    async def finish(self) -> None:
        """Once the process has exited: read the rest of its output, then close what it left open."""
        task = self._task
        if task is not None and not task.done():
            await asyncio.wait({task}, timeout=_DRAIN_SEC)
            if not task.done():
                task.cancel()
                await asyncio.wait({task})
        await self.close_own("Claude Code stopped")
        self._between.clear()


class ResidentClaude:
    """One long-lived ``claude --input-format stream-json`` process, fed one user turn at a time.

    The owner builds the command (flags, system prompt, ``--resume``,
    ``--replay-user-messages``) and serializes turns. The process's output is
    read for as long as it runs, so a turn Claude runs on its own (after
    background work ends) is read when Claude writes it: its events go to the
    ``autonomous`` handler the owner started the process with, never to a
    user turn. A user turn's events start when Claude starts its message and
    end at the next result. A process that exited, whose output ended, or
    whose ``signature`` (what it was started with) no longer matches, is
    stale: the owner stops it and starts a new one, which resumes the session.

    Claude reports the session's running totals (cost and tokens) on every
    result, and a resumed session's totals carry on from what it spent
    before. The process keeps the totals of its last result, starting from
    the totals the owner saved for the session it resumes, so each turn's own
    share, a turn of Claude's own included, is the difference
    (``turn_usage``), taken in the order Claude wrote the results.
    """

    def __init__(self, *, label: str) -> None:
        self._label = label  # names the owner in logs
        self._process: asyncio.subprocess.Process | None = None
        self._output: _Output | None = None
        self._command: list[str] = []
        self._signature: Any = None
        self._totals = Usage()  # the session's running totals at its last result
        self._price: Price | None = None  # prices the totals at the provider's prices, if Looper prices them

    @property
    def label(self) -> str:
        return self._label

    @property
    def started(self) -> bool:
        return self._process is not None

    @property
    def command(self) -> list[str]:
        """The running process's command; empty when none runs."""
        return self._command

    @property
    def pid(self) -> int | None:
        return self._process.pid if self._process is not None else None

    @property
    def totals(self) -> Usage:
        """The session's running totals as of its last result; the owner saves them beside the session id."""
        return self._totals

    def stale(self, signature: Any) -> bool:
        """True when a process was started but has exited, its output ended, or it was started with another
        ``signature``."""
        process = self._process
        return process is not None and (
            process.returncode is not None or self._signature != signature
            or (self._output is not None and self._output.ended)
        )

    async def start(
        self, command: list[str], *, cwd: str, signature: Any, env: dict[str, str] | None = None,
        totals: Usage | None = None, autonomous: AutonomousHandler | None = None, price: Price | None = None,
    ) -> None:
        """Start the process (with ``env``, else the server's environment); raises ``OSError`` when it cannot run.

        ``totals`` are the saved running totals of the session the command
        resumes; a new session starts from none. ``autonomous`` takes each
        turn Claude runs on its own; without it, such a turn only counts
        toward the totals. ``price`` prices the totals' tokens at the
        provider's prices (``Launch.price``); without it, their cost is
        Claude Code's ``total_cost_usd``.
        """
        process = await proc_tree.spawn(
            *command,
            cwd=cwd,
            env=env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        assert process.stdout is not None
        self._process, self._command, self._signature = process, command, signature
        self._totals, self._price = totals or Usage(), price
        self._output = _Output(self, process.stdout, autonomous)
        logger.info("%s started Claude Code (pid=%s)", self._label, process.pid)

    async def run_turn(
        self,
        text: str,
        on_event: Callable[[dict[str, Any]], Awaitable[None]],
        *,
        tag: Any = None,
        stop: Callable[[], CapStop | None] | None = None,
    ) -> ResidentTurn:
        """Write ``text`` as one user turn and hand each of its events to ``on_event``, up to its result.

        ``stop`` is asked after each event whether the turn went over its soft
        cap (``ClaudeRunRecorder.cap_stop``): then the turn ends with the
        cap's result and what it spent, Claude is killed at once and the
        session's totals move on by that spend, so the next process, which
        resumes the session, counts only its own.

        The message carries a uuid of its own, so Claude's start of its turn
        is told apart from a turn Claude runs on its own meanwhile. ``tag`` is
        handed to the ``autonomous`` handler of a turn Claude runs on its own
        after this one. A Claude that exits before the result (its output
        ends, or it closed stdin) is reaped, so the next turn starts a new
        process. Any exception, cancellation included, kills and reaps the
        process before it propagates: a turn that was given up must not leave
        Claude running.
        """
        process, output = self._process, self._output
        assert process is not None and process.stdin is not None and output is not None
        turn = _UserTurn(uuid=str(uuid4()), text=text, tag=tag, on_event=on_event,
                         done=asyncio.get_running_loop().create_future(), stop=stop)
        input_lost = False
        try:
            output.turn = turn
            if output.ended:
                output.drop_turn()  # nothing more will come
            output.read()
            try:
                await write_user_turn(process.stdin, text, uuid=turn.uuid)
            except (BrokenPipeError, ConnectionResetError):
                # Claude closed its stdin, so it is exiting; what it already wrote decides the turn.
                logger.warning("%s: Claude closed stdin before the turn was sent", self._label)
                input_lost = True
                await proc_tree.end(process, label=self._label)
            result_event = await turn.done
        except BaseException:
            await self.stop(kill=True)
            raise
        if turn.capped is not None:
            await self.stop(kill=True)
            self._totals = self._totals.plus(turn.capped.usage)
            return ResidentTurn(turn.capped.result_event, session_started=turn.session_started,
                                usage=turn.capped.usage)
        if result_event is None or input_lost:
            await self.stop()
        return ResidentTurn(
            result_event,
            None if result_event is not None else process.returncode,
            session_started=turn.session_started,
            usage=turn.usage,
        )

    def turn_usage(self, result_event: dict[str, Any] | None) -> Usage:
        """A turn's own cost and tokens: its result's running totals less the previous ones."""
        own = run_usage(result_event, before=self._totals, price=self._price)
        if result_event is not None:
            self._totals = self._totals.updated(reported_totals(result_event, self._price))
        return own

    async def stop(self, *, kill: bool = False) -> None:
        """End the process, if any, and forget it.

        Its stdin is closed, which ends a healthy Claude; then Claude, if it
        is still running after ``STOP_GRACE_SEC`` (at once with ``kill``),
        and whatever it started are ended (``proc_tree.end``). The process is
        always reaped, and what it wrote meanwhile is still read: a turn of
        Claude's own that ends then is handled; one that does not is closed
        without a result.
        """
        process, output = self._process, self._output
        self._process, self._output, self._command, self._signature = None, None, [], None
        if process is None:
            return
        if output is not None:
            output.drop_turn()
        # Taken while Claude runs: what it started is re-parented once it exits.
        known = None if kill else proc_tree.tree_of(process)
        if process.stdin is not None and not process.stdin.is_closing():
            process.stdin.close()
        try:
            if process.returncode is None and not kill:
                try:
                    await asyncio.wait_for(process.wait(), STOP_GRACE_SEC)
                except TimeoutError:
                    pass
        finally:
            # Also when the wait is cancelled: the process is forgotten already.
            await proc_tree.end(process, label=self._label, known=known)
            if output is not None:
                await output.finish()
        logger.info("%s stopped Claude Code (pid=%s, exit code %s)", self._label, process.pid, process.returncode)


def answer_text(result_event: dict[str, Any] | None, texts: list[str]) -> str:
    """What a turn answered: its result's ``result``, the text of the turn's last top-level assistant message, as
    Claude Code reports it and the Codex and OMP adapters write it. What Claude wrote before its tool calls ("checking
    the logs now…") is not part of it. When the result has no text (the turn ended on a tool call, or without a
    result), the turn's top-level ``texts``, joined, so nothing it said is lost."""
    return str((result_event or {}).get("result") or "") or "".join(texts)


class AutonomousRun:
    """A turn Claude ran on its own, recorded as a run of its own (``recorder``).

    When it ends, ``on_end`` gets what Claude answered in it (``answer_text``; empty when it ended without a
    result) and how it ended.
    """

    def __init__(
        self, recorder: ClaudeRunRecorder, on_end: Callable[[str, ResidentTurn], Awaitable[None]],
    ) -> None:
        self._recorder = recorder
        self._on_end = on_end
        self._text: list[str] = []

    async def event(self, event: dict[str, Any]) -> None:
        await self._recorder.record(event)
        if event.get("type") != "assistant" or event.get("parent_tool_use_id") is not None:
            return  # a subagent's words are not Claude's own
        message = event.get("message")
        for block in message.get("content", []) if isinstance(message, dict) else []:
            if isinstance(block, dict) and block.get("type") == "text" and block.get("text"):
                self._text.append(str(block["text"]))

    async def end(self, turn: ResidentTurn) -> None:
        result = turn.result_event
        text = answer_text(result, self._text) if result is not None else ""
        await self._recorder.finish(
            output_text=text or None, result_event=result, usage=turn.usage,
            error=None if result is not None else "Claude Code stopped before this turn of its own ended",
        )
        await self._on_end(text, turn)


# How often a running record's heartbeat (last_output_at) is written.
_HEARTBEAT_INTERVAL = 10.0
# How long closing a run's record may take, a second try after a database error included. A caller cancelled
# meanwhile (its run being stopped) does not stop the write; the limit keeps a stopping server from waiting on a
# database that does not answer.
CLOSE_TIMEOUT_SEC = 5.0
# How long the close waits before it tries the database again.
_CLOSE_RETRY_SEC = 0.5
# The record writes under way, held until each ends.
_closing: set[asyncio.Task[None]] = set()


def ended_by(exc: BaseException | None) -> RecordStatus:
    """How a run that ``exc`` ended ends: ``failed`` after an error, ``interrupted`` when it was cancelled (Looper
    stopped it: a kill, its task or the server stopping)."""
    from ..db.llm_records import RecordStatus

    return RecordStatus.FAILED if isinstance(exc, Exception) else RecordStatus.INTERRUPTED


def _clock() -> float:
    return time.monotonic()

# Fields of Claude's init event that list every tool, skill or server; kept as
# counts so a transcript stays readable.
_INIT_LIST_LIMIT = 2_000


def _is_thinking_progress(event: dict[str, Any]) -> bool:
    """Claude emits one of these about every second while it thinks."""
    return event.get("type") == "system" and event.get("subtype") == "thinking_tokens"


def _compact_event(event: dict[str, Any]) -> dict[str, Any]:
    if event.get("type") != "system" or event.get("subtype") != "init":
        return event
    compact: dict[str, Any] = {}
    for key, value in event.items():
        if isinstance(value, list) and len(json.dumps(value)) > _INIT_LIST_LIMIT:
            compact[key] = f"<{len(value)} items>"
        else:
            compact[key] = value
    return compact


class ClaudeRunRecorder:
    """Record one Claude CLI run in ``llm_records``.

    The record opens as ``running`` with the launch command, the system
    prompt Looper appended (its text, its sha256 and its layers' sources), the
    model and its source, the names of the environment variables Looper set
    (their values hashed) and the working directory, gets every stream-json event as it arrives
    (partial text deltas excepted: the full message follows), and closes with
    the outcome, the run's own cost and tokens, its context size at the end
    (the prompt of its last main-loop request) and the compactions it went
    through. Recording is observability: a database error is logged and never
    fails the run.

    Used with ``async with``, it opens the record and, however the block ends,
    closes a record the run left open (``end_left_open``). A record closes
    once, and a cancellation that arrives while it closes does not stop it.
    """

    def __init__(
        self,
        *,
        agent_id: str,
        task_id: str | None,
        command: list[str],
        cwd: str,
        input_text: str,
        session_id: str | None = None,
        step_id: str | None = None,
        pid: int | None = None,
        system_prompt: SystemPrompt | None = None,
        launch: Launch | None = None,
        autonomous: bool = False,
        whole_process: bool = False,
    ) -> None:
        self._agent_id = agent_id
        self._autonomous = autonomous
        # The run is a whole process (a Worker's or Reviewer's): its last result reports the run's own usage.
        self._whole_process = whole_process
        self._result: dict[str, Any] | None = None  # that result, once it came
        self._launch = launch
        self._task_id = task_id or None
        self._command = command
        self._cwd = cwd
        self._input_text = input_text
        self._session_id = session_id
        self._step_id = step_id
        self._pid = pid
        self._system_prompt = system_prompt
        self._record_id: int | None = None
        self._closed = False
        self._seq = 0
        # seq of the row holding the current thinking phase's progress, if it is the latest row
        self._thinking_seq: int | None = None
        self._model: str | None = None
        self._context_tokens: int | None = None  # the prompt of the run's latest main-loop request
        self._compactions: list[dict[str, Any]] = []
        self._streamed = Usage()  # the tokens of the run's model requests so far, from its usage events
        self._started = _clock()
        self._last_heartbeat = float("-inf")
        # What the run spent so far at its provider's prices, from its usage events, against its per-run cap;
        # None when its runtime's own figure is its cost.
        self._spend: RunSpend | None = None
        if launch is not None and launch.price is not None:
            from .runtimes.pricing import RunSpend

            self._spend = RunSpend(launch.profile.provider, launch.model, launch.max_budget_usd)

    async def start(self) -> None:
        from ..db import llm_records
        from .runtimes.instructions import record as instructions_record

        try:
            self._record_id = await llm_records.start_record(
                agent_id=self._agent_id,
                task_id=self._task_id,
                session_id=self._session_id,
                input_text=self._input_text,
                metadata={
                    "command": self._command,
                    # What Looper appended (its file holds the same text); Claude Code's own prompt is not included.
                    **(self._system_prompt.record() if self._system_prompt is not None else {}),
                    # The model asked for and where it came from; the variables' names, not their values.
                    **(self._launch.record() if self._launch is not None else {}),
                    # The repository's own CLAUDE.md and AGENTS.md the runtime was given, and how.
                    **({"project_instructions": instructions_record(
                        self._launch.runtime.instruction_files(self._cwd), self._cwd)}
                       if self._launch is not None else {}),
                    "cwd": self._cwd,
                    # The task step a Worker or Reviewer ran, so its run stays tied to that step.
                    **({"step_id": self._step_id} if self._step_id else {}),
                    # The resident process that served this turn, for a long-lived agent.
                    **({"pid": self._pid} if self._pid is not None else {}),
                    # A turn Claude ran on its own, answering no user message (input_text says so too).
                    **({"autonomous": True} if self._autonomous else {}),
                },
            )
        except Exception:
            logger.warning("could not open llm record for %s", self._agent_id, exc_info=True)

    async def __aenter__(self) -> ClaudeRunRecorder:
        await self.start()
        return self

    async def __aexit__(self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: Any) -> None:
        await self.end_left_open(exc)

    async def record(self, event: dict[str, Any]) -> None:
        await self._heartbeat()
        if event.get("type") == "usage":  # internal: counted, not part of the transcript
            from .runtimes.pricing import event_usage

            if self._spend is not None:
                self._spend.add(event)
            elif (found := event_usage(event)) is not None:
                self._streamed = self._streamed.plus(found[1])
            return
        if event.get("type") == "stream_event":
            return
        if self._whole_process and event.get("type") == "result":
            self._result = event
        if event.get("type") == "system" and event.get("subtype") == "init":
            self._model = event.get("model") or self._model
            self._session_id = event.get("session_id") or self._session_id
        if (context := request_context(event)) is not None:
            self._context_tokens = context
        if (compacted := compaction(event)) is not None:
            self._compactions.append(compacted)
        if self._record_id is None:
            return
        from ..db import llm_records

        compact = _compact_event(event)
        thinking = _is_thinking_progress(event)
        try:
            if thinking and self._thinking_seq is not None and self._thinking_seq == self._seq:
                # One row per thinking phase: keep its latest token estimate.
                await llm_records.update_event(self._record_id, self._seq, compact)
                return
            self._seq += 1
            self._thinking_seq = self._seq if thinking else None
            await llm_records.append_events(self._record_id, [(self._seq, compact)])
        except Exception:
            logger.warning("could not record event for %s", self._agent_id, exc_info=True)

    @property
    def cap_stop(self) -> CapStop | None:
        """Once the run is over its per-run cap at its provider's prices (Looper's soft cap): the result it ends
        with and what it spent. The owner stops the run then; None until then, and always for a run without a
        cap or priced by its runtime."""
        spend = self._spend
        if spend is None or not spend.exceeded or spend.cap is None or spend.usage.cost_usd is None:
            return None
        return CapStop(soft_cap_result(spend.usage.cost_usd, spend.cap), spend.usage)

    def _runtime(self) -> RuntimeAdapter:
        """The runtime the recorded run went through (Claude Code for a record without a launch)."""
        from .runtimes import CLAUDE_CODE

        return self._launch.runtime if self._launch is not None else CLAUDE_CODE

    def _provider(self) -> ProviderSpec:
        """The provider the recorded run's agent used (Claude Code's own login without a launch)."""
        from .profile_registry import BUILTIN_PROVIDER

        return self._launch.profile.provider if self._launch is not None else BUILTIN_PROVIDER

    async def _after_run(self) -> None:
        """Tell the operator what the run left wrong in its runtime's own state, as a warning in the log and an
        ``error`` row in the task's activity."""
        if self._launch is None:
            return
        try:
            warnings = await self._launch.runtime.after_run(self._launch, self._command)
        except Exception:
            logger.warning("could not check what the run of %s left behind", self._agent_id, exc_info=True)
            return
        from ..db import activity

        for warning in warnings:
            logger.warning("%s: %s", self._agent_id, warning)
            if self._task_id:
                try:
                    await activity.insert_activity(task_id=self._task_id, worker=self._agent_id, kind="error",
                                                   summary=f"Warning: {warning}"[:500])
                except Exception:
                    logger.warning("could not record the warning for %s", self._agent_id, exc_info=True)

    async def _heartbeat(self) -> None:
        """Any output, partial deltas included, shows the run is not stuck."""
        now = _clock()
        if self._record_id is None or now - self._last_heartbeat < _HEARTBEAT_INTERVAL:
            return
        self._last_heartbeat = now
        from ..db import llm_records

        try:
            await llm_records.touch_record(self._record_id)
        except Exception:
            logger.warning("could not record heartbeat for %s", self._agent_id, exc_info=True)

    @property
    def _usage_so_far(self) -> Usage:
        """What the run's usage events added up to: its tokens, and their cost when Looper prices the run."""
        return self._spend.usage if self._spend is not None else self._streamed

    async def finish(
        self,
        *,
        output_text: str | None,
        result_event: dict[str, Any] | None,
        error: str | None = None,
        usage: Usage | None = None,
        outcome: str | None = None,
        status: RecordStatus | None = None,
    ) -> None:
        """Close the record with the run's own ``usage``; without one, all the result reports is the run's.
        ``outcome`` is how a Worker's report or a Reviewer's verdict read (``worker.RunOutcome``); ``status``,
        when given, replaces the one its result gives (``ended_by``, for a run that was stopped).

        Claude reports the running totals of its session, so a run that is
        one turn of a longer-lived session passes its own share
        (``ResidentClaude.turn_usage``). What the run left wrong in its
        runtime's own state (``RuntimeAdapter.after_run``: a forked Codex
        login) goes to the server log and the task's activity first.
        """
        from ..db.llm_records import RecordStatus

        await self._after_run()
        result = result_event or {}
        succeeded = error is None and result.get("subtype") == "success" and not result.get("is_error")
        if usage is None:
            usage = run_usage(result_event, price=self._launch.price if self._launch is not None else None)
        if result_event is None or result.get(SOFT_CAP):
            # No result reported the run's spend: what its usage events add up to is what is known.
            usage = usage.filled_from(self._usage_so_far)
        await self._close(
            status or (RecordStatus.COMPLETED if succeeded else RecordStatus.FAILED),
            output_text=output_text, session_id=result.get("session_id"), usage=usage,
            error=error or (None if succeeded else f"Claude run ended: {result.get('subtype') or 'no result'}"),
            outcome=outcome,
        )

    async def end_left_open(self, exc: BaseException | None = None) -> None:
        """Close the record if the run left it open, ended as ``ended_by(exc)`` says, with what the run's usage
        events added up to, or, for a whole process whose result came, what that result reports; nothing for a
        record closed already."""
        ended = "the run had ended with its result; " if self._result is not None else ""
        if isinstance(exc, Exception):
            why = f"{ended}Looper stopped following the run after an error: {type(exc).__name__}: {exc}"
        elif ended:
            why = f"{ended}Looper was stopped while it handled the result"
        elif exc is not None:
            why = "the run was stopped before it ended: it was killed, or its task or the server stopped"
        else:
            why = "the run ended without its outcome recorded"
        usage = self._usage_so_far
        if self._result is not None:
            usage = run_usage(self._result, price=self._launch.price if self._launch is not None else None) \
                .filled_from(usage)
        await self._close(ended_by(exc), output_text=None, session_id=(self._result or {}).get("session_id"),
                          usage=usage, error=why)

    async def _close(self, status: RecordStatus, *, output_text: str | None, session_id: str | None, usage: Usage,
                     error: str | None, outcome: str | None = None) -> None:
        """Write the record's end once, with ``usage`` priced as the run's runtime prices it.

        The write goes on when the caller is cancelled meanwhile (its run being
        stopped), for at most ``CLOSE_TIMEOUT_SEC``; a failed write is logged.
        """
        if self._record_id is None or self._closed:
            return
        self._closed = True
        try:
            cost = self._runtime().cost(usage, self._provider())
            usage = cost.usage
            fields = {
                "status": status.value,
                "output_text": output_text,
                "session_id": session_id or self._session_id,
                "input_tokens": usage.input_tokens,
                "output_tokens": usage.output_tokens,
                "cache_read_tokens": usage.cache_read_tokens,
                "cache_creation_tokens": usage.cache_creation_tokens,
                "cost_usd": usage.cost_usd,
                "cost_source": cost.source.value,
                "context_tokens": self._context_tokens,
                "compactions": self._compactions,
                "duration_ms": int((_clock() - self._started) * 1000),
                "model": self._model,
                "error": error,
                "outcome": outcome,
            }
            write = asyncio.ensure_future(asyncio.wait_for(self._write_end(fields), CLOSE_TIMEOUT_SEC))
        except Exception:  # recording never fails the run; the next start ends the record
            logger.warning("could not close llm record for %s", self._agent_id, exc_info=True)
            return
        _closing.add(write)
        write.add_done_callback(self._closed_write)
        with contextlib.suppress(Exception):  # logged once the write ends (_closed_write)
            await asyncio.shield(write)

    async def _write_end(self, fields: dict[str, Any]) -> None:
        """``finish_record``, tried once more after a database error."""
        from ..db import llm_records

        try:
            await llm_records.finish_record(self._record_id, **fields)
        except Exception:
            logger.warning("could not close llm record for %s; trying again", self._agent_id, exc_info=True)
            await asyncio.sleep(_CLOSE_RETRY_SEC)
            await llm_records.finish_record(self._record_id, **fields)

    def _closed_write(self, write: asyncio.Task[None]) -> None:
        _closing.discard(write)
        if not write.cancelled() and (problem := write.exception()) is not None:
            logger.warning("could not close llm record for %s", self._agent_id, exc_info=problem)
