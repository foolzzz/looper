"""The outbound review in a Manager's Claude Code (the outbound-review RFC, section 6.2): while
``outbound_review.enabled`` is on, the Manager's Claude Code runs a PreToolUse hook before each tool call, and Looper
answers it here.

- **Where a call reaches** is the tool's typed level (``level_of``): Claude Code's built-in tools have one each
  (``BUILTIN_LEVELS``; a tool not listed is ``unknown``); Looper's own tools are ``inside`` here, as
  ``ManagerOps.call`` sees them; another MCP server's tools are ``unknown`` unless the operator declared that tool
  ``inside`` (``mcp_tool_levels``).
- **Whose turn:** the call's own (the turn whose stream showed its ``tool_use``, a subagent's by its parent's), never
  the turn running when it came; one Looper cannot place, or whose turn is over, is no one's (``autonomous``).
- **What is reviewed:** an ``external`` or ``unknown`` call (a shell command), by ``outbound.review``; any other call
  runs at once. A write to Looper's own files (its settings, this hook) is refused by code first.
- **Fail closed:** the hook is ``outbound_hook_client.py`` behind a shell that prints a ``deny`` when it cannot run
  or prints nothing; an alarm set before anything else ends it with a ``deny`` at ``DEADLINE_SEC``, well before
  Claude Code's wait for it (Claude Code runs a call whose hook timed out or failed). The Manager then reads no
  settings file (``--setting-sources ""``), so the model cannot turn the hook off from its working directory.
- **Without a hook:** the review cannot be turned on while an enabled Manager's runtime has none
  (``RuntimeAdapter.tool_hook``); while it is on, no turn of a Manager runs in a process without the hook (it is
  checked before each turn and handoff, and turning the review on stops such processes).

Turning it on changes the Manager's command (its settings carry the hook), so a running Manager restarts on its next
turn; off, nothing here runs and the command is what it was before.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shlex
import sys
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .outbound import CALLER_TIMEOUT_SEC, REVIEWED, Action, ActionLevel, Who

logger = logging.getLogger(__name__)

ENABLED_KEY = "outbound_review.enabled"
# The hook's waits: a review takes at most ``outbound.CALLER_TIMEOUT_SEC``; the client waits for Looper's answer
# ``CHECK_TIMEOUT_SEC``, and its alarm ends it with a deny at ``DEADLINE_SEC``, whatever it waits for; Claude Code,
# which runs the call past its own wait, waits ``HOOK_TIMEOUT_SEC``.
CHECK_TIMEOUT_SEC = CALLER_TIMEOUT_SEC + 10
DEADLINE_SEC = CHECK_TIMEOUT_SEC + 15
HOOK_TIMEOUT_SEC = DEADLINE_SEC + 15
# How long a call waits for the Manager's stream to show its tool_use, which places it in its turn.
PLACE_WAIT_SEC = 5.0
CLIENT = Path(__file__).with_name("outbound_hook_client.py")
CONTENT_TOOLS_KEPT = 5  # the last writes of a session the review of a shell command sees (``sh s.sh`` after a Write)
WRITE_EXCERPT_CHARS = 1_000

# Claude Code's built-in tools (its init event, 2.1.329) and where a call of each reaches. A tool not listed is
# ``unknown``, and reviewed.
BUILTIN_LEVELS: Mapping[str, ActionLevel] = {
    **dict.fromkeys(("Bash", "Monitor"), ActionLevel.UNKNOWN),  # a shell command: anything
    **dict.fromkeys(("WebSearch", "WebFetch", "RemoteTrigger", "SendMessage"), ActionLevel.EXTERNAL),
    "PushNotification": ActionLevel.OPERATOR,  # the operator's own devices
    **dict.fromkeys(("Write", "Edit", "MultiEdit", "NotebookEdit", "EnterWorktree", "ExitWorktree"),
                    ActionLevel.MACHINE),
    **dict.fromkeys(("Read", "Glob", "Grep", "LS", "NotebookRead", "Task", "Agent", "Skill", "ToolSearch", "TaskStop",
                     "TodoWrite", "ListAgents", "CronCreate", "CronDelete", "CronList", "ScheduleWakeup", "Workflow",
                     "ReportFindings", "ExitPlanMode", "AskUserQuestion", "BashOutput", "KillShell", "KillBash"),
                    ActionLevel.INSIDE),
}
# The built-in tools that write a file (``file_path`` or ``notebook_path``): one that would change Looper's own files
# is refused by code.
WRITE_TOOLS = frozenset({"Write", "Edit", "MultiEdit", "NotebookEdit"})


def level_of(tool_name: str) -> ActionLevel:
    """Where a call of a built-in tool ``tool_name`` reaches, by its type (never by what its arguments say); every
    MCP tool is ``unknown`` here, placed by the servers the run loaded (``resolve_level``)."""
    if tool_name.startswith("mcp__"):
        return ActionLevel.UNKNOWN
    return BUILTIN_LEVELS.get(tool_name, ActionLevel.UNKNOWN)


@dataclass(frozen=True)
class Trusted:
    """What a run's MCP tool names can stand for: the servers its MCP config loaded (Looper's own, ``looper``,
    among them; a catalog server of that name is replaced by it), and Looper's own tools on it."""

    servers: frozenset[str]
    looper_tools: frozenset[str]

    @classmethod
    def of(cls, library: Any, looper_tools: Any) -> Trusted:
        """A run's, from its library (``AgentLibrary``: its MCP items, and Looper's own server) and the names of
        Looper's tools it has."""
        from .library import ItemKind
        from .ops.service import SERVER_NAME

        servers = {item.name for item in getattr(library, "items", ()) if item.kind is ItemKind.MCP}
        return cls(frozenset({*servers, SERVER_NAME}), frozenset(looper_tools))


def mcp_tool(tool_name: str) -> list[tuple[str, str]]:
    """The (server, tool) pairs an MCP tool's name ``mcp__<server>__<tool>`` may stand for (a server's name may
    hold ``__`` too)."""
    rest = tool_name.removeprefix("mcp__") if tool_name.startswith("mcp__") else ""
    pairs, at = [], rest.find("__")
    while at > 0:
        if rest[at + 2:]:
            pairs.append((rest[:at], rest[at + 2:]))
        at = rest.find("__", at + 1)
    return pairs


async def resolve_level(tool_name: str, trusted: Trusted) -> ActionLevel:
    """``level_of``, and an MCP tool's: its name must stand for exactly one tool of a server the run loaded, else it
    is ``unknown``; then one of Looper's own tools is ``inside`` (``ManagerOps.call`` runs it), and another server's
    tool is ``inside`` only when the operator declared that tool so for that server."""
    from ..db import mcp_tool_levels
    from .ops.service import SERVER_NAME

    if not tool_name.startswith("mcp__"):
        return level_of(tool_name)
    placed = [(server, tool) for server, tool in mcp_tool(tool_name) if server in trusted.servers]
    if len(placed) != 1:
        return ActionLevel.UNKNOWN  # no loaded server's, or several could be meant
    server, tool = placed[0]
    if server == SERVER_NAME:
        return ActionLevel.INSIDE if tool in trusted.looper_tools else ActionLevel.UNKNOWN
    return ActionLevel.INSIDE if await mcp_tool_levels.declared_inside([(server, tool)]) else ActionLevel.UNKNOWN


# --- The switch ---


# The switch as this server last read or set it, for the checks that cannot wait for the database (a Manager's turn
# right before it reaches its process); ``_version`` counts its changes, so a read that began before one does not
# overwrite it.
_known = False
_version = 0
# Rebuilt for a new running loop, as settings tests and repeated starts use fresh loops.
_update_lock: asyncio.Lock | None = None
_update_lock_loop: asyncio.AbstractEventLoop | None = None


def _settings_lock() -> asyncio.Lock:
    global _update_lock, _update_lock_loop
    loop = asyncio.get_running_loop()
    if _update_lock is None or _update_lock_loop is not loop:
        _update_lock, _update_lock_loop = asyncio.Lock(), loop
    return _update_lock


async def enabled(db: Any = None) -> bool:
    """Whether the review is on (``outbound_review.enabled`` is ``true``; no row is off)."""
    from ..db import system_settings

    global _known
    began = _version
    value = (await system_settings.get_settings([ENABLED_KEY], pool=db)).get(ENABLED_KEY) == "true"
    if _version == began:  # no change was made while it read
        _known = value
    return value


def known_enabled() -> bool:
    """The switch as this server last read or set it: read when it starts (``serve``) and before each Manager turn,
    set by ``set_enabled``, the only way it changes."""
    return _known


async def managers_without_hook() -> list[dict[str, str]]:
    """Each enabled Manager whose Agent Profile runs on a runtime with no tool hook: its id, name and runtime."""
    from ..db import managers as db_managers
    from .agent_settings import AgentType, Launch, load_agent_settings

    missing = []
    for row in await db_managers.list_managers():
        if not row.get("enabled", True):
            continue
        settings = await load_agent_settings(AgentType.MANAGER, manager_id=row["id"], wait=False)
        runtime = Launch(settings, role=AgentType.MANAGER).runtime
        if not runtime.tool_hook:
            missing.append({"manager_id": row["id"], "name": row.get("name") or row["id"], "runtime": runtime.type})
    return missing


class HookUnavailable(ValueError):
    """The review cannot be turned on: an enabled Manager's runtime has no tool hook."""


async def settings() -> dict[str, Any]:
    return {"enabled": await enabled(), "managers_without_hook": await managers_without_hook()}


async def set_enabled(value: bool) -> dict[str, Any]:
    """Turn the review on or off; on is refused while an enabled Manager runs on a runtime with no tool hook."""
    from ..db.connection import get_pool

    if not isinstance(value, bool):
        raise ValueError("enabled must be true or false")
    global _known, _version
    async with _settings_lock():
        if value and (missing := await managers_without_hook()):
            names = "; ".join(f"{m['name']} ({m['runtime']})" for m in missing)
            raise HookUnavailable(f"the outbound review needs every enabled Manager on a runtime with a tool hook "
                                  f"(Claude Code); these have none: {names}. Change their Agent Profile first.")
        await get_pool().execute(
            "INSERT INTO system_settings (key, value) VALUES ($1, $2) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()",
            ENABLED_KEY, "true" if value else "false")
        # Hold the lock through the response and readback: commit order and cache order must agree even when an
        # older write's response is delayed. A read begun before this change cannot overwrite it either.
        _known, _version = value, _version + 1
        return await settings()


# --- The hook in the Manager's command ---


@dataclass(frozen=True)
class ToolHook:
    """The PreToolUse hook a run's Claude Code settings carry: its command and Claude Code's wait for it."""

    command: str
    timeout: int = HOOK_TIMEOUT_SEC
    trusted: Trusted | None = None  # the MCP names of the process launched with this hook

    def settings(self) -> dict[str, Any]:
        return {"PreToolUse": [{"matcher": "*",
                                "hooks": [{"type": "command", "command": self.command, "timeout": self.timeout}]}]}


DENY_OUTPUT = json.dumps({"hookSpecificOutput": {
    "hookEventName": "PreToolUse", "permissionDecision": "deny",
    "permissionDecisionReason": "Looper's outbound check could not run, so this action did not run. Tell the user "
                                "what you were about to do; do not try to do the same thing another way."}})


def hook_command(config: Path, *, python: str = sys.executable, client: Path = CLIENT) -> str:
    """The shell command of the hook: the client's answer when it prints one, else a ``deny`` (a missing
    interpreter or client, a crash, no output)."""
    run = " ".join(shlex.quote(part) for part in (python, "-I", str(client), str(config)))
    deny = shlex.quote(DENY_OUTPUT)
    return f'out=$({run} 2>/dev/null) && [ -n "$out" ] && printf "%s" "$out" || printf "%s" {deny}'


def write_hook(runtime_root: Path, owner: str, *, url: str, token: str) -> ToolHook:
    """Write the hook's config for ``owner`` (a Manager, or a step's run) readable by the operator only: where Looper
    is, the owner's token and the waits; and the hook that reads it. A Manager's call is placed in its turn by Looper
    (its ``tool_use_id``), never by the client."""
    directory = (runtime_root / ".local" / "outbound-hook").absolute()
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = directory / f"{owner}.json"
    content = json.dumps({"url": url, "token": token, "timeout": CHECK_TIMEOUT_SEC, "deadline": DEADLINE_SEC})
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return ToolHook(hook_command(path))


def protected(path: Any, *, cwd: Any, runtime_root: Path) -> bool:
    """Whether a write to ``path`` (relative to ``cwd``) would change Looper's own files: its private state under
    the runtime root (settings, prompts, this hook's config), its ops file, the operator's token, or the hook's
    client."""
    if not isinstance(path, str) or not path:
        return False
    base = cwd if isinstance(cwd, str) and cwd else os.getcwd()
    target = Path(os.path.realpath(os.path.join(base, os.path.expanduser(path))))
    root = Path(os.path.realpath(runtime_root))
    guarded = (root / ".local", root / "ops", root / "operator-token", Path(os.path.realpath(CLIENT)))
    return any(target == place or place in target.parents for place in guarded)


# --- Answering the hook ---


@dataclass
class Write:
    """A file a session's earlier call wrote, as the review of a later shell command sees it."""

    path: str
    excerpt: str


def action_of(tool_name: str, tool_input: Mapping[str, Any], level: ActionLevel) -> Action:
    """The call as the review reads it: a shell command's text, or a tool's arguments."""
    if tool_name in ("Bash", "Monitor"):
        command = str(tool_input.get("command") or "")
        description = str(tool_input.get("description") or "")
        return Action(tool_name, level, "command", command[:500] or "(empty)",
                      f"Run a shell command{': ' + description if description else ''}", content=command)
    target = next((str(tool_input[key]) for key in ("url", "query", "file_path", "to", "target")
                   if isinstance(tool_input.get(key), str) and tool_input.get(key)), tool_name)
    content = json.dumps(tool_input, ensure_ascii=False, sort_keys=True)
    return Action(tool_name, level, "tool_input", target[:500], f"Call {tool_name}", content=content)


BLOCKED_MESSAGE = ("Looper's outbound review blocked this action: {reason}. It did not run. Tell the user what you were "
                   "about to do and why it was stopped; do not try to do the same thing another way.")

Notify = Callable[[str], Awaitable[None]]


@dataclass(frozen=True)
class Placed:
    """The turn a call belongs to, as its review reads it: whose it is, its message, chat and outside content."""

    who: Who
    message: str = ""
    channel: str = ""
    chat: str | None = None
    context: tuple[tuple[str, str], ...] = ()


NO_ONES = Placed(Who.AUTONOMOUS)


async def answer(payload: Any, *, agent_id: str, place: Callable[[], Awaitable[Placed]], writes: Sequence[Write],
                 runtime_root: Path, claude_bin: str, trusted: Trusted,
                 task_id: str | None = None) -> tuple[dict[str, Any], Any]:
    """The hook's answer for ``payload`` (Claude Code's PreToolUse input), and the review it took (None when none was
    needed): ``{"decision": "allow"}``, or ``block`` with the ``message`` for the model. ``place`` finds the call's
    turn, only for a call that is reviewed."""
    from . import outbound

    data = payload if isinstance(payload, Mapping) else {}
    tool_name = data.get("tool_name") if isinstance(data.get("tool_name"), str) else ""
    tool_input = data.get("tool_input") if isinstance(data.get("tool_input"), Mapping) else {}
    level = await resolve_level(tool_name, trusted)
    origin = outbound.Origin(agent_id, outbound.Source.HOOK, task_id=task_id)
    if tool_name in WRITE_TOOLS:
        path = tool_input.get("file_path") or tool_input.get("notebook_path")
        if protected(path, cwd=data.get("cwd"), runtime_root=runtime_root):
            review = await outbound.refuse(Action(tool_name, level, "file", str(path),
                                                  f"Write the file {path}"), origin, outbound.Failure.PROTECTED)
            return _blocked(review), review
    if tool_name and level not in REVIEWED:
        return {"decision": "allow"}, None
    turn = await place()
    origin = outbound.Origin(agent_id, outbound.Source.HOOK, task_id=task_id, channel=turn.channel or None,
                             chat=turn.chat)
    shown = [*turn.context, *((f"an earlier write to {w.path}", w.excerpt) for w in writes)]
    review = await outbound.review(action_of(tool_name or "(unnamed)", tool_input, level),
                                   outbound.Asker(turn.who, turn.channel, turn.message), origin, context=shown,
                                   runtime_root=runtime_root, claude_bin=claude_bin)
    return ({"decision": "allow"} if review.allowed else _blocked(review)), review


def _blocked(review: Any) -> dict[str, Any]:
    return {"decision": "block", "reason": review.reason, "review_id": review.review_id,
            "message": BLOCKED_MESSAGE.format(reason=review.reason.rstrip("."))}


def write_of(payload: Any) -> Write | None:
    """The file a Write or Edit call writes and an excerpt of what it writes, for the review of a later command."""
    data = payload if isinstance(payload, Mapping) else {}
    tool_input = data.get("tool_input") if isinstance(data.get("tool_input"), Mapping) else {}
    if data.get("tool_name") not in WRITE_TOOLS:
        return None
    path = tool_input.get("file_path") or tool_input.get("notebook_path")
    text = next((tool_input[key] for key in ("content", "new_string", "new_source")
                 if isinstance(tool_input.get(key), str)), "")
    return Write(str(path), text[:WRITE_EXCERPT_CHARS]) if isinstance(path, str) and path else None


# --- A task's step runs (the outbound-review RFC, section 6.3) ---


async def task_placed(task_id: str) -> Placed:
    """Whose a task is, as the review of its Worker's or Reviewer's call reads it: the operator's for a schedule's,
    the dashboard's or the operator's own direct chat's; else a group's or another person's. Its own request is the
    message; the requirement it carries now (written after leads, or built from an intent) is the outside content."""
    from ..db import tasks as db_tasks
    from .message import parse_channel_id
    from .operator_identity import operator_sender_id

    task = await db_tasks.get_task(task_id)
    if task is None:
        return NO_ONES
    bot = parse_channel_id(task.get("origin_channel_id") or None)
    channel = bot.channel_type if bot is not None else ""  # a web or dashboard task names no bot
    if task.get("client_id"):
        who = Who.ALLOWLISTED_PERSON
    elif task.get("schedule_id") or not channel:
        who = Who.OPERATOR
    elif task.get("chat_group"):
        who = Who.GROUP_MEMBER
    else:
        operator = await operator_sender_id(task["origin_channel_id"])
        # A Lark direct chat is stored as its user; a Telegram one as its chat, whose id is the user's.
        direct = task.get("chat_kind") == "user" or channel == "telegram"
        person = task.get("chat_id") if direct else None
        who = Who.OPERATOR if operator is not None and person == operator else Who.ALLOWLISTED_PERSON
    sources = "、".join(task.get("untrusted_inputs") or ()) or "none"
    context = [(f"the task's requirement (outside content went into it: {sources})", str(task["requirement"]))]
    context += await _supplements(task_id)
    context += await _gate_answers(task_id)
    return Placed(who, str(task.get("original_user_text") or ""), channel, task.get("chat_id"), tuple(context))


SUPPLEMENTS_SHOWN = 5  # the latest supplements a task run's review sees


async def _supplements(task_id: str) -> list[tuple[str, str]]:
    """The task's latest supplements, word for word: what went into the task besides its requirement (one the
    Manager wrote in a session that found leads among them)."""
    from ..db import task_changes

    return [(f"a supplement to the task (via {row['via'] or 'unknown'})", str(row["summary"]))
            for row in await task_changes.supplements(task_id, limit=SUPPLEMENTS_SHOWN)]


async def _gate_answers(task_id: str) -> list[tuple[str, str]]:
    """What was answered in the task's user gates, word for word. The operator's own (the dashboard's and the web
    chat's; one from Lark or Telegram when its chat is the operator's own direct chat, ``operator_sender_id``) is
    an operator confirmation: evidence for the review, never an allow. An API client's, a group's or another
    person's is labelled as someone else's."""
    from ..db import user_gates
    from .gate_service import VIA_DASHBOARD
    from .message import BOT_CHANNEL_TYPES, builtin_channel_id
    from .operator_identity import operator_sender_id

    found = []
    for answer in await user_gates.answers(task_id):
        via, channel = str(answer.get("via") or ""), answer.get("channel")
        if channel in BOT_CHANNEL_TYPES:
            operator = await operator_sender_id(answer.get("channel_id") or builtin_channel_id(channel))
            own = operator is not None and answer.get("chat") == operator
        else:
            own = via in (VIA_DASHBOARD, "manager:web")
        chose = f" (chose \"{answer['option']}\" for {answer.get('agenda_item') or 'the agenda'})" \
            if answer.get("option") else ""
        label = "operator confirmation" if own else "an answer from someone other than the operator"
        found.append((f"{label} at gate {answer['step_key']}{chose}", str(answer.get("text") or "")))
    return found


# --- Telling the operator ---

Sender = Callable[[str, str, str], Awaitable[tuple[bool, str | None]]]
_sender: Sender | None = None


def configure(sender: Sender | None) -> None:
    """Send block notices through ``sender`` (channel id, chat, text); None: only log them."""
    global _sender
    _sender = sender


async def notify_unsupported(task_id: str, run: str, runtime: str) -> None:
    """Tell the operator a step's run of ``task_id`` did not start: it runs on ``runtime``, which has no tool hook,
    while the review is on and outside content went into the task."""
    await _notify(f"Looper 没有启动任务 {task_id} 的一次运行（{run}）：这个任务用过线索或意图，对外动作审查打开时它的 "
                  f"Worker 和 Reviewer 要跑在有拦截点的运行时上，而 {runtime} 没有。Planner 会决定换 Profile、改计划或失败。")


async def notify_blocked(agent_id: str, tool: str, target: str, reason: str, review_id: int | None) -> None:
    """Tell the operator, in the chat the dashboard's user gates use (never the chat the turn is in), that a call was
    blocked; an error is logged, never raised."""
    from .outbound import _masked

    text = (f"Looper 的对外动作审查拦下了一次调用：{agent_id} 的 {tool}（{_masked(target)[:200]}）。"
            f"理由：{reason}" + (f"（审计记录 #{review_id}）" if review_id is not None else ""))
    logger.warning("outbound review blocked %s's %s (review %s)", agent_id, tool, review_id)
    await _notify(text)


async def _notify(text: str) -> None:
    from .gate_service import dashboard_target

    try:
        chat = await dashboard_target()
        if chat is None or _sender is None:
            return
        sent, error = await _sender(chat.channel_id, chat.target.id, text)
        if not sent:
            logger.warning("cannot send the outbound review notice to %s: %s", chat.channel_id, error)
    except Exception:
        logger.warning("cannot send the outbound review notice", exc_info=True)
