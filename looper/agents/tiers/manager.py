"""Manager Agent: user-facing steward backed by a resident Claude Code process.

The Manager is the only agent that communicates with users. It:
- Keeps one Claude Code process running (stream-json input and output): each
  chat message is one user turn on its stdin, and a restarted process resumes
  the session saved in PostgreSQL (``agent_sessions``)
- Receives user messages from all channels via the Bus
- Classifies intent (direct answer, clarification, task request)
- Places each confirmed task in a project and forwards it to the TaskRouter,
  which creates it ready, not started. A project that does not exist is created
  only after Looper itself asked the user to confirm it (``_NewProject``). The
  reply ends with the task's configuration, written by code from the effective
  settings, and asks whether to start it; the user's word to start it is a
  ``task_start`` action, which code checks and passes to the TaskRouter
- Tells Claude, before the next message from that chat, what Looper itself
  said there after Claude's last reply (``_Followup``), since Claude does not
  see Looper's own replies
- Puts a ``Channel:`` line before each message naming where it came from (the
  dashboard chat, a Lark or Telegram direct message): the channels share one
  session, and the recommended prompt has Claude answer only that message
- Streams Claude Code responses in real-time to the originating channel
- Records a turn Claude runs on its own (after background work it started
  ends) as a run of its own, and sends what Claude said there as a reply of
  its own to the chat of the message Claude answered before it
- Starts a new session when its agent can no longer resume the saved one
  (``session_switch``): the old session, while its Claude still runs, first
  writes a handoff in one HANDOFF turn. The first turn of any new session
  starts with a resume brief: why it is new, the handoff, and the recent
  conversation of each chat, from the stored chat messages
- Answers ``/new`` itself, from any channel, without Claude: it stops Claude
  and drops the saved session, so the next message starts a new session with
  a clean context (no resume brief), and records the reset as a run
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..broadcast import Broadcaster
from ..bus import MessageBus
from ..agent_settings import (
    TASK_ROLES, AgentSettings, AgentType, Launch, Layer, index_layers, load_agent_settings, load_concurrency, resolve,
    resolve_concurrency,
)
from ..claude_cli import (
    AUTONOMOUS_INPUT,
    AutonomousHandler,
    AutonomousRun,
    ClaudeRunRecorder,
    ResidentTurn,
    answer_text,
    exit_note,
)
from ..library import load_installed
from ..project_execution import LessonsMode, ProjectExecution, ReviewPolicy, TaskBudgetAction, format_usd
from ..projects import RequestError, project_name, start_ready_task
from ..run_usage import Usage
from .. import session_switch
from ..runtimes import CLAUDE_CODE
from ..session_switch import SessionSwitch
from ..system_prompt import SystemPrompt, build_system_prompt, is_recommended
from .journal import HANDOFF_MAX_CHARS, clip, kept_text
from .task import resolve_work_dir
from ..task_notice import Notice, notify_chat
from ..message import (
    MANAGER_ADDRESS,
    TASK_ROUTER_ADDRESS,
    AgentAddress,
    MessageEnvelope,
    MessageType,
    channel_address,
)

logger = logging.getLogger(__name__)

from ...db import activity as db_activity
from ...db import agent_setting_layers as db_layers
from ...db import role_configs as db_role_configs
from ...db import tasks as db_tasks
from ...db import chat as db_chat
from ...db import agent_sessions as db_agent_sessions
from ...db import llm_records as db_llm_records
from ...db import projects as db_projects

# Appended to the (editable) system prompt on every call: the task JSON is
# parsed by code, so its required fields are owned by code too.
TASK_PLACEMENT_RULE = """\
## Task project and working directory (required)
Every task belongs to a project and runs in a directory. Each user message
starts with a "Projects" section listing the existing projects and their
default directories, and, when there are any, a "Ready tasks" section listing
the tasks created but not started yet.

- Project: before asking the user to confirm a task, know its project. Use the
  existing project the user names or clearly means. Otherwise ask the user:
  offer the closest existing projects or propose a short name for a new one;
  never present a project as decided before the user chose it. Put the
  project's name in the task JSON as "project".
- New project: Looper creates a project only after it asked the user itself.
  A task JSON naming a project that does not exist creates nothing: Looper asks
  the user to confirm that project (its name and default directory) or to
  choose an existing one, and your next message tells you so. When the user
  confirms, output the task JSON again with the same "project" and "work_dir"
  and "new_project": true; when they choose an existing project, output it with
  that project's name. Never set "new_project" to true otherwise.
- Directory: there is no global default. Use the directory the user names, as
  an absolute path (or one starting with ~/), in the task JSON as "work_dir".
  If they name none and the project has a default directory, leave "work_dir"
  empty and the project's default is used. A new project, or one without a
  default directory, needs the user to name one: ask them. Never pick a
  directory yourself.

## Starting a ready task
The task JSON creates the task ready: it does not start. Looper answers with
the task's configuration and asks the user whether to start it now. When the
user asks to start a ready task, output only this JSON, with the task's id from
"Ready tasks" (or from the note about it):
{"action": "task_start", "task_id": "<task id>"}
Looper starts it; it waits while its project runs as many tasks as its limit.
When the user declines or wants to wait, output no JSON: remind them that they
can start it later by asking you, or with Start on the Tasks page.
"""

DEFAULT_SYSTEM_PROMPT = """\
You are the Manager Agent (管家) for Looper, an autonomous multi-agent programming system.

## Your Role
1. Understand user intent and respond naturally
2. For simple questions or conversation, answer directly
3. For task requests (development, testing, analysis, refactoring), first clarify and confirm with the user before creating a task
4. For status queries, report current progress from the goal portfolio

## Task Creation Flow (IMPORTANT)
When a user describes something that sounds like a task, DO NOT immediately create it.
Instead, follow this flow:

1. **Clarify**: Summarize your understanding of the requirement back to the user. Include:
   - What you will do (scope)
   - Which project the task belongs to and which directory it runs in. When the user has not named them, ask both as explicit questions: offer the existing project that matches, or propose a short name for a new one, and ask for the directory. Never state a project or a directory as decided before the user has answered.
   - What files or modules are likely involved
   - Any assumptions you are making
   - Estimated priority (P1-P3)
2. **Wait for confirmation**: Ask the user to confirm, e.g. "确认创建吗？" / "Shall I create it?"
3. **Create task**: ONLY after the user explicitly confirms (says things like "确认", "开始", "go", "yes", "好", "可以", "proceed"), output the structured JSON:

```json
{
  "action": "task_request",
  "goal_id": "<short-kebab-id>",
  "structured_requirement": "<clear one-paragraph requirement incorporating clarifications>",
  "project": "<name of the task's project: an existing one, or a new one the user agreed to>",
  "work_dir": "<absolute directory the task runs in, e.g. ~/projects/tetris; empty for the project's default>",
  "new_project": false,
  "priority": "P2",
  "acknowledgment": "<brief acknowledgment in user's language>"
}
```

4. **Start**: Creating a task does not start it. Looper answers with the task's configuration and asks the user whether to start it now. When the user says to start it, output the `task_start` JSON. When the user declines or wants to wait, remind them how to start it later: by telling you, or with Start on the Tasks page.

If the user says "不", "取消", "算了", "no", "cancel" or modifies the requirement, do NOT output the JSON. Instead continue the conversation.

## Rules
- Answer the message you were just sent. The dashboard chat, Lark and Telegram share this one conversation (the "Channel:" line before each message says where it came from), so do not continue an earlier topic, run checks or volunteer a status report unless this message asks for it
- A greeting or a short check-in gets a short reply
- For ambiguous requests, ask clarifying questions before even proposing the task summary
- Never refuse a reasonable development task
- Never output task_request JSON without user's explicit confirmation
"""

# The ready tasks each user turn lists, newest first, so Claude can start one by its id.
READY_TASKS_LISTED = 10
# The existing projects a new-project question offers instead.
PROJECTS_OFFERED = 10
# The most chats that keep what Looper said after Claude's last reply there (``_Followup``) at once.
FOLLOWUP_CHATS = 50
# The actions Claude's JSON may name.
ACTIONS = ("task_request", "task_start")

# Where a setting in a task's configuration came from (``agent_settings.Layer``), named as the dashboard names it.
SOURCE_WORDS = {
    Layer.BUILTIN: "内置默认",
    Layer.SYSTEM: "共用默认（Settings → Roles → Shared defaults）",
    Layer.ROLE: "角色默认",
    Layer.PROJECT: "本项目",
    Layer.ROLE_PROJECT: "本项目里的这个角色",
    Layer.TASK: "本任务",
}
START_QUESTION = "现在开始吗？回复「开始」就会启动；也可以稍后在 Tasks 页面点 Start。"


# A new session's resume brief: the recent conversation of the most recently active chats, from the stored
# chat messages, each message clipped, all of it in at most CONVERSATION_MAX_CHARS.
CONVERSATION_CHATS = 3
CONVERSATION_MESSAGES = 8  # per chat
CONVERSATION_MESSAGE_CHARS = 300
CONVERSATION_MAX_CHARS = 4_000
# How long the old session may take to write its handoff before the new one starts without it.
HANDOFF_TIMEOUT_SEC = 300

# The one command Looper answers itself in a chat with the Manager: a message that is exactly this (trimmed, any
# case) starts a new session. Anything else, "/new foo" or "/news" too, is a message for Claude.
NEW_SESSION_COMMAND = "/new"
NEW_SESSION_REPLY = "已开始新的 Manager 会话，下一条消息从全新的上下文开始。"


def is_new_session_command(text: str) -> bool:
    return text.strip().lower() == NEW_SESSION_COMMAND


# How the line before each message names the channel it came from (``AgentAddress.channel``). The Lark channel
# takes direct messages only; Telegram's are told apart by the chat id.
CHANNEL_NAMES = {"web": "the Looper dashboard chat", "lark": "a Lark direct message"}


def channel_line(envelope: MessageEnvelope) -> str:
    """The line before a message that tells Claude where it came from, and so where its reply goes."""
    origin = _message_origin(envelope)
    channel = origin["channel"] or "unknown"
    if channel == "telegram":
        where = "a Telegram direct message" if _private_chat(origin) else "a Telegram group chat"
    else:
        where = CHANNEL_NAMES.get(channel, f"the {channel} channel")
    return f"Channel: {where}"


def _message_origin(envelope: MessageEnvelope) -> dict[str, str | None]:
    """The channel a message came from, its sender (Lark's open_id, Telegram's user id; the web chat names none)
    and its chat (the Lark or Telegram chat, the web chat session)."""
    context = envelope.payload.get("channel_context")
    context = context if isinstance(context, dict) else {}
    sender = context.get("sender_id") or context.get("user_id")
    chat = context.get("chat_id") or context.get("session_id")
    return {"channel": envelope.source.channel or envelope.source.agent_id,
            "sender": str(sender) if sender else None, "chat": str(chat) if chat else None}


def _private_chat(origin: dict[str, str | None]) -> bool:
    """Whether a message (``_message_origin``) came from a private chat: the web chat and a Lark direct message
    always (the Lark channel takes nothing else), a Telegram chat when its id is its user's, since a group's or a
    channel's id is negative. Only private chats are stored (``_session_key``)."""
    return not (origin["channel"] == "telegram" and (origin["chat"] or "").startswith("-"))


def _session_key(envelope: MessageEnvelope) -> str | None:
    """The chat_messages session id a turn is stored under: the web chat's ``session_id``, or
    ``<channel>:<chat_id>`` for Telegram/Lark. None when neither identifies a chat, and for a chat that is not
    private (a Telegram group), whose turns are never stored."""
    origin = _message_origin(envelope)
    if not _private_chat(origin):
        return None
    context = envelope.payload.get("channel_context")
    context = context if isinstance(context, dict) else {}
    if context.get("session_id"):
        return str(context["session_id"])
    if origin["channel"] and origin["chat"]:
        return f"{origin['channel']}:{origin['chat']}"
    return None


@dataclass(frozen=True)
class _NewProject:
    """A project that does not exist, which Looper asked the user to confirm creating, with its default directory."""

    name: str
    work_dir: str

    def same(self, other: _NewProject) -> bool:
        return self.name.casefold() == other.name.casefold() and self.work_dir == other.work_dir


def _role_line(role: AgentType, settings: AgentSettings) -> str:
    """One task role's line in a task's configuration: its Agent Profile with the profile's effort and where the
    profile came from, and, but for the Planner (which never has them), whether it has agent teams."""
    effort = f"effort {settings.effort}" if settings.effort else "effort 默认"
    line = (f"- {role.value.capitalize()}：{settings.profile.name}"
            f"（{effort}，来源：{SOURCE_WORDS[settings.profile_source]}）")
    if role is not AgentType.PLANNER:
        line += f"，agent teams {'开' if settings.profile.agent_teams else '关'}"
    return line


class _AskUser(Exception):
    """A confirmed task lacks something only the user can give; the message asks for it. With ``new_project``, it
    asks the user to confirm creating that project."""

    def __init__(self, question: str, new_project: _NewProject | None = None) -> None:
        super().__init__(question)
        self.new_project = new_project

    def note(self) -> str:
        """What Claude is told about the question before the user's answer."""
        if self.new_project is None:
            return (f"[Looper] Looper did not create the task of your last task JSON in this chat; it asked the user "
                    f"instead: {self}")
        name, work_dir = self.new_project.name, self.new_project.work_dir
        return (
            f'[Looper] Looper did not create the task of your last task JSON in this chat: its project "{name}" does not '
            "exist. "
            f"Looper asked the user to confirm creating that project, with default directory {work_dir}, or to "
            "choose an existing project. If the message below confirms, output the task JSON again with "
            f'"project": "{name}", "work_dir": "{work_dir}" and "new_project": true; if it names an existing project, '
            "output the task JSON with that project instead."
        )


@dataclass(frozen=True)
class _Followup:
    """What Looper itself said in a chat after Claude's last reply there; Claude does not see Looper's own replies.

    Only the next message, and only one from that chat, carries it: Claude
    gets ``note`` before the message, and the project the user was asked to
    confirm (``new_project``) may be created by that message's task JSON.
    """

    chat: tuple[str | None, str | None]  # the channel and the chat (``_message_origin``)
    note: str
    new_project: _NewProject | None = None
    # The message that asked for the task Looper then asked a question about: its text names the task.
    asked_for: str = ""


@dataclass(frozen=True)
class _Process:
    """What the resident Claude was started with; a turn on it is recorded with these."""

    command: list[str]
    signature: Any
    system_prompt: SystemPrompt
    launch: Launch


@dataclass
class ManagerConfig:
    claude_bin: str = "claude"
    work_dir: Path = field(default_factory=lambda: Path.cwd())
    max_context_tokens: int = 100_000


@dataclass
class GoalSnapshot:
    goal_id: str
    title: str = ""
    status: str = "active"
    plan_summary: str = ""
    progress: str = ""
    last_update: str = ""


class ManagerAgent:
    """Long-running Manager Agent backed by a resident Claude Code process."""

    def __init__(
        self,
        *,
        config: ManagerConfig,
        runtime_root: Path,
        broadcaster: Broadcaster | None = None,
    ) -> None:
        self._config = config
        self._runtime_root = runtime_root
        self._broadcaster = broadcaster
        self._session_id: str | None = None
        # The session's running totals (cost and tokens) at its last result. A process that resumes the
        # session starts from them, so its first turn records only its own share.
        self._session_totals = Usage()
        # The runtime type and the provider the session belongs to and the Agent Profile that ran it last (None:
        # a session saved before they were recorded).
        self._session_runtime: str | None = None
        self._session_profile: str | None = None
        self._session_provider: str | None = None
        self._bus: MessageBus | None = None
        self._task: asyncio.Task[Any] | None = None
        self._running = False
        self._system_prompt: str = DEFAULT_SYSTEM_PROMPT
        self._prompt_recommended = True  # whether _system_prompt is the recommended one (default_prompt)
        # The agent settings the last turn ran with, kept when a turn cannot read them (None before any).
        self._agent_settings: AgentSettings | None = None
        self._goal_snapshots: dict[str, GoalSnapshot] = {}
        # The resident Claude: started on the first chat message, then fed one
        # user turn per message; restarted when its system prompt changes.
        self._claude = CLAUDE_CODE.resident_session("manager")
        self._claude_runtime = CLAUDE_CODE  # the runtime type whose session self._claude is
        self._turn_lock = asyncio.Lock()
        self._session_loaded = False  # whether the saved session was read (at start, else before the first turn)
        self._process: _Process | None = None  # what the running Claude was started with
        # Why the session was last dropped and the handoff its old session wrote, until the new session's first
        # turn has its resume brief.
        self._pending_switch: SessionSwitch | None = None
        self._pending_handoff: str | None = None
        # The user dropped the session (/new): the next one starts without a resume brief. Saved with the session,
        # so a restart before the next message keeps it; cleared once the new session has started.
        self._clean_start = False
        # What Looper said after Claude's last reply, for the next message only.
        # At most FOLLOWUP_CHATS chats keep one; past it, the oldest is dropped (``_keep_followup``).
        self._followup: dict[tuple[str | None, str | None], _Followup] = {}

    @property
    def agent_id(self) -> str:
        return "manager"

    @property
    def agent_address(self) -> AgentAddress:
        return MANAGER_ADDRESS

    async def _load_session_state(self) -> None:
        """Read the session a previous run saved, once; without a readable one the next turn starts a new one."""
        self._session_loaded = True
        try:
            saved = await db_agent_sessions.get_session(self.agent_id)
        except Exception:
            logger.warning("manager: cannot read its saved session; starting a new one", exc_info=True)
            return
        if saved:
            self._session_id = saved.get("session_id")
            self._session_totals = Usage.from_dict(saved.get("session_totals"))
            self._session_runtime = saved.get("runtime_type")
            self._session_profile = saved.get("profile_id")
            self._session_provider = saved.get("provider_id")
            self._clean_start = bool(saved.get("clean_start"))

    async def _save_session_state(self) -> None:
        try:
            await self._write_session_state()
        except Exception:
            logger.exception("manager: failed to save its session")

    async def _write_session_state(self) -> None:
        await db_agent_sessions.save_session(self.agent_id, self._session_id, self._session_totals.to_dict(),
                                             runtime_type=self._session_runtime, provider_id=self._session_provider,
                                             profile_id=self._session_profile, clean_start=self._clean_start)

    async def start(self, bus: MessageBus) -> None:
        self._bus = bus
        self._running = True

        await self._load_db_config()
        if not self._session_loaded:
            await self._load_session_state()

        self._task = asyncio.create_task(self._message_loop())
        logger.info(
            "manager agent started (session=%s)",
            self._session_id or "new",
        )

    async def _load_db_config(self) -> None:
        """Load the role prompt from DB; keep the last one read on failure."""
        try:
            config = await db_role_configs.get_config("manager")
            if config and config.get("system_prompt"):
                self._system_prompt = config["system_prompt"]
                self._prompt_recommended = is_recommended(
                    self._system_prompt, str(config.get("default_prompt") or "") or DEFAULT_SYSTEM_PROMPT,
                )
                logger.debug("loaded manager system prompt from database")
        except Exception:
            logger.debug("could not load manager config from database", exc_info=True)

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        async with self._turn_lock:
            await self._claude.stop()

    def is_healthy(self) -> bool:
        return self._running and bool(shutil.which(self._config.claude_bin))

    @property
    def busy(self) -> bool:
        """Whether a turn is in progress: Claude answers a message, or the session is being reset. A turn Claude
        runs on its own does not count."""
        return self._turn_lock.locked()

    async def _message_loop(self) -> None:
        assert self._bus is not None
        try:
            async for envelope in self._bus.subscribe("manager"):
                if not self._running:
                    break
                try:
                    await self._handle(envelope)
                except Exception:
                    logger.exception("error handling message %s", envelope.message_id)
        except asyncio.CancelledError:
            pass

    async def _handle(self, envelope: MessageEnvelope) -> None:
        match envelope.message_type:
            case MessageType.USER_MESSAGE:
                await self._handle_user_message(envelope)
            case MessageType.GOAL_STATUS:
                await self._handle_goal_status(envelope)
            case MessageType.TASK_ACCEPTED:
                await self._handle_task_accepted(envelope)
            case MessageType.WORK_RESULT:
                await self._handle_task_result(envelope)
            case MessageType.AGENT_LIFECYCLE:
                await self._handle_lifecycle(envelope)

    async def _handle_user_message(self, envelope: MessageEnvelope) -> None:
        assert self._bus is not None
        text = envelope.payload.get("text", "")
        if not text.strip():
            return
        if is_new_session_command(text):
            await self._start_new_session(envelope)
            return
        origin = _message_origin(envelope)
        chat = (origin["channel"], origin["chat"])
        await self._persist_chat(envelope, "user", text, envelope.message_id)
        # What Looper said after Claude's last reply applies to the next message only, from that chat only.
        # Each chat keeps its own pending followup, so another chat speaking does not drop it.
        followup = self._followup.pop(chat, None)

        await self._send_status(envelope, "thinking", phase=True, activity="thinking")

        try:
            response = await self._invoke_claude(text, envelope, note=followup.note if followup else "")
            parsed = self._parse_response(response)
        except Exception as exc:
            # Always answer: the chat stream waits for a final reply.
            logger.exception("manager failed to answer %s", envelope.message_id)
            await self._send_reply(envelope, f"Manager 出错，没能完成这次回复：{exc}", error=str(exc))
            return

        if parsed.get("needs_clarification"):
            clarification = parsed.get("clarification_text", "")
            await self._send_reply(envelope, clarification)
        elif parsed.get("action") == "task_request":
            try:
                project, work_dir, created = await self._place_task(
                    parsed, followup.new_project if followup else None)
            except _AskUser as ask:
                self._keep_followup(_Followup(chat, ask.note(), ask.new_project,
                                              (followup.asked_for if followup else "") or text))
                await self._send_reply(envelope, str(ask))
                return
            except Exception as exc:
                logger.exception("manager failed to place task from %s", envelope.message_id)
                await self._send_reply(envelope, f"Manager 出错，没能创建任务：{exc}", error=str(exc))
                return
            task_id = f"task-{uuid.uuid4().hex[:12]}"
            goal_id = str(parsed.get("goal_id") or "")
            await self._bus.publish(MessageEnvelope.create(
                source=MANAGER_ADDRESS,
                target=TASK_ROUTER_ADDRESS,
                message_type=MessageType.TASK_REQUEST,
                payload={
                    "task_id": task_id,
                    "goal_id": goal_id,
                    "requirement": parsed.get("structured_requirement", text),
                    # What the user wrote when asking for it, before any question Looper asked about it.
                    "original_user_text": (followup.asked_for if followup else "") or text,
                    "user_language": envelope.payload.get("language", "zh"),
                    "work_dir": work_dir,
                    "project_id": project["id"],
                    "priority": parsed.get("priority", "P2"),
                    "constraints": parsed.get("constraints", []),
                    # Who the TaskRouter tells when the task ends: in Telegram the chat, in Lark the sender.
                    "chat_id": notify_chat(envelope.metadata.get("origin_channel_id"), sender=origin["sender"],
                                           chat=origin["chat"]),
                },
                correlation_id=envelope.correlation_id,
                metadata={"origin_channel_id": envelope.metadata.get("origin_channel_id")},
            ))
            ack_text = parsed.get("acknowledgment", f"收到，已创建任务: {text[:100]}")
            block = await self._configuration_block(task_id, goal_id, project, work_dir, created)
            self._keep_followup(_Followup(chat, (
                f'[Looper] Looper created the task of your last task JSON in this chat as {task_id}, ready and not '
                "started, showed "
                "the user its configuration and asked whether to start it now. If the message below says to start "
                f'it, output {{"action": "task_start", "task_id": "{task_id}"}}; if it declines, remind the user how '
                "to start it later."
            )))
            await self._send_reply(envelope, f"{ack_text}\n\n{block}")
        elif parsed.get("action") == "task_start":
            reply, note = await self._start_task(parsed)
            self._keep_followup(_Followup(chat, note))
            await self._send_reply(envelope, reply)
        else:
            reply_text = parsed.get("response_text", response)
            await self._send_reply(envelope, reply_text)

    def _keep_followup(self, followup: _Followup) -> None:
        """Keep ``followup`` for its chat's next message, newest last; past ``FOLLOWUP_CHATS`` chats the oldest one's
        is dropped (that chat's next message then gets no note, and a new project it was asked about is asked
        again)."""
        self._followup.pop(followup.chat, None)
        self._followup[followup.chat] = followup
        while len(self._followup) > FOLLOWUP_CHATS:
            self._followup.pop(next(iter(self._followup)))

    async def _place_task(
        self, parsed: dict[str, Any], confirmed: _NewProject | None,
    ) -> tuple[dict[str, Any], str, bool]:
        """The confirmed task's project, its work directory, and whether the project is new.

        The project is matched by name ignoring case. The directory is the
        task's own, else the project's default. A project that does not exist
        is created only when the user confirmed it in answer to Looper's own
        question in the exchange before (``confirmed``): the task JSON says
        ``"new_project": true`` and names that project and directory (no
        directory: the confirmed one). Raises ``_AskUser`` when the project or
        the directory is missing, and to ask that question about any other new
        project.
        """
        try:
            name = project_name(parsed.get("project"))
        except ValueError as exc:
            raise _AskUser(f"这个任务属于哪个项目？请告诉我项目名，可以是已有项目，也可以新建一个。（{exc}）") from None
        project = await db_projects.find_project_by_name(name)
        explicit = parsed.get("work_dir")
        explicit = explicit.strip() if isinstance(explicit, str) else ""
        if project and project["work_dir"] and not explicit:
            return project, project["work_dir"], False
        if project is None and not explicit and confirmed is not None and confirmed.name.casefold() == name.casefold():
            explicit = confirmed.work_dir
        try:
            work_dir = resolve_work_dir(explicit)
        except ValueError as exc:
            # No default directory: ask instead of creating the task.
            raise _AskUser(f"这个任务要在哪个目录里做？请告诉我完整路径（例如 ~/projects/xxx）。（{exc}）") from None
        if project:
            return project, work_dir, False
        asked = _NewProject(name, work_dir)
        if parsed.get("new_project") is True and confirmed is not None and confirmed.same(asked):
            return await db_projects.create_project(name=confirmed.name, work_dir=work_dir), work_dir, True
        raise _AskUser(await self._new_project_question(asked), asked)

    async def _new_project_question(self, asked: _NewProject) -> str:
        """Ask the user to confirm creating ``asked``, or to choose one of the existing projects instead."""
        lines = [
            f"项目「{asked.name}」还不存在，任务还没有创建。要新建这个项目吗？它的默认目录会是 {asked.work_dir}。",
            "- 回复「确认」：新建这个项目，并在里面创建任务；",
        ]
        try:
            projects = await db_projects.list_projects()
        except Exception:
            logger.warning("manager could not list projects for a new-project question", exc_info=True)
            lines.append("- 也可以告诉我改用哪个已有项目（项目列表暂时读不到）。")
            return "\n".join(lines)
        if not projects:
            lines.append("- 目前还没有其他项目；也可以告诉我换一个名字或目录。")
            return "\n".join(lines)
        offered = "、".join(f"{p['name']}（{p['work_dir'] or '没有默认目录'}）" for p in projects[:PROJECTS_OFFERED])
        more = f"，共 {len(projects)} 个" if len(projects) > PROJECTS_OFFERED else ""
        lines.append(f"- 或者告诉我改用哪个已有项目：{offered}{more}。")
        return "\n".join(lines)

    async def _configuration_block(
        self, task_id: str, goal_id: str, project: dict[str, Any], work_dir: str, created: bool,
    ) -> str:
        """The new ready task's configuration, from the settings in effect in its project (resolved as the project's
        agent settings view resolves them), ending with the question whether to start it."""
        project_id = project["id"]
        lines = [
            "任务已创建，还没有开始（Ready — not started）：",
            f"- 任务：{goal_id}（{task_id}）" if goal_id else f"- 任务：{task_id}",
            f"- 项目：{project['name']}（{'新建' if created else '已有'}）",
            f"- 目录：{work_dir}",
        ]
        limit: int | None = None
        try:
            layers = index_layers(await db_layers.list_layers(project_id), project_id)
        except Exception as exc:
            logger.warning("manager could not read the agent settings of project %s", project_id, exc_info=True)
            lines.append(f"- Agent 设置：暂时读不到（{type(exc).__name__}），可以在项目的 Settings 页查看")
        else:
            lines += [_role_line(role, resolve(role, layers, project_id)) for role in TASK_ROLES]
            limit = resolve_concurrency(AgentType.PLANNER, layers, project_id).value
        execution = ProjectExecution.from_row(project)
        lines.append("- 评审：每一步都要 Reviewer 评审通过" if execution.review_policy is ReviewPolicy.ALL
                     else "- 评审：由 Planner 决定哪些步骤要 Reviewer 评审")
        if execution.task_budget_usd is None:
            lines.append("- 每个任务的花费上限：不限")
        else:
            at_cap = ("再让 Planner 总结一轮后结束" if execution.on_task_budget is TaskBudgetAction.WRAP_UP
                      else "立即失败")
            lines.append(f"- 每个任务的花费上限：${format_usd(execution.task_budget_usd)}，达到时{at_cap}")
        lines.append({
            LessonsMode.OFF: "- 经验沉淀：关",
            LessonsMode.DRAFT: "- 经验沉淀：任务完成后生成草稿，由你确认后写入项目知识",
            LessonsMode.WRITE: "- 经验沉淀：任务完成后直接写入项目知识",
        }[execution.distill_lessons])
        if limit is not None:
            lines.append(f"- 并发：这个项目同时最多运行 {limit} 个任务")
        return "\n".join([*lines, "", START_QUESTION])

    async def _start_task(self, parsed: dict[str, Any]) -> tuple[str, str]:
        """Start the ready task Claude names (``task_start``) through the TaskRouter; the reply, and the note that
        tells Claude what happened."""
        assert self._bus is not None
        task_id = parsed.get("task_id")
        task_id = task_id.strip() if isinstance(task_id, str) else ""
        if not task_id:
            return ("要开始哪个任务？没有拿到任务 id，没有开始任何任务。",
                    "[Looper] Your task_start JSON named no task_id; Looper started nothing.")
        try:
            started = await start_ready_task(task_id, self._bus, MANAGER_ADDRESS)
        except RequestError as exc:
            return (f"没能开始任务 {task_id}：{exc}",
                    f"[Looper] Looper did not start {task_id}: {exc}. It told the user so.")
        limit = await load_concurrency(AgentType.PLANNER, started["project_id"])
        return (f"已开始任务 {task_id}。它的项目同时最多运行 {limit} 个任务，已满时它会排队，等有任务结束再开始。",
                f"[Looper] Looper started {task_id}; it waits while its project runs {limit} tasks.")

    async def _handle_goal_status(self, envelope: MessageEnvelope) -> None:
        goal_id = envelope.payload.get("goal_id", "")
        if goal_id:
            snapshot = self._goal_snapshots.get(goal_id, GoalSnapshot(goal_id=goal_id))
            snapshot.status = envelope.payload.get("status", snapshot.status)
            snapshot.progress = envelope.payload.get("progress", snapshot.progress)
            snapshot.last_update = envelope.payload.get("timestamp", "")
            self._goal_snapshots[goal_id] = snapshot

        origin = envelope.metadata.get("origin_channel_id")
        notice = Notice.from_payload(envelope.payload.get("notice"))
        if notice is not None:
            # A Telegram or Lark task's end goes to who it recorded, instead of its final summary, behind the
            # progress sent before it (``task_notice``).
            await self._bus.publish(MessageEnvelope.create(
                source=MANAGER_ADDRESS,
                target=channel_address(notice.channel.value),
                message_type=MessageType.CHAT_NOTICE,
                payload=notice.payload(),
                correlation_id=envelope.correlation_id,
                metadata={"origin_channel_id": origin},
            ))
        elif origin:
            progress_text = envelope.payload.get("summary", "")
            if progress_text:
                await self._bus.publish(MessageEnvelope.create(
                    source=MANAGER_ADDRESS,
                    target=AgentAddress(agent_type="channel", agent_id=origin),
                    message_type=MessageType.STATUS_UPDATE,
                    payload={"text": progress_text},
                    correlation_id=envelope.correlation_id,
                    metadata={"origin_channel_id": origin},
                ))

    async def _handle_task_accepted(self, envelope: MessageEnvelope) -> None:
        origin = envelope.metadata.get("origin_channel_id")
        if origin:
            step_count = envelope.payload.get("step_count", 0)
            await self._bus.publish(MessageEnvelope.create(
                source=MANAGER_ADDRESS,
                target=AgentAddress(agent_type="channel", agent_id=origin),
                message_type=MessageType.STATUS_UPDATE,
                payload={"text": f"任务已分解为 {step_count} 个步骤，开始执行..."},
                correlation_id=envelope.correlation_id,
                metadata={"origin_channel_id": origin},
            ))

    async def _handle_task_result(self, envelope: MessageEnvelope) -> None:
        summary = envelope.payload.get("summary", "任务完成")
        origin = envelope.metadata.get("origin_channel_id")
        if origin:
            await self._bus.publish(MessageEnvelope.create(
                source=MANAGER_ADDRESS,
                target=AgentAddress(agent_type="channel", agent_id=origin),
                message_type=MessageType.AGENT_REPLY,
                payload={"text": summary},
                correlation_id=envelope.correlation_id,
                metadata={"origin_channel_id": origin},
            ))

    async def _handle_lifecycle(self, envelope: MessageEnvelope) -> None:
        event = envelope.payload.get("event", "")
        agent_id = envelope.payload.get("agent_id", "")
        if event == "max_restarts_exceeded":
            logger.error("agent %s has exceeded max restarts", agent_id)

    async def _invoke_claude(self, message: str, context_envelope: MessageEnvelope, *, note: str = "") -> str:
        """Send the message to the resident Claude as one user turn, after ``note`` (what Looper said in the chat
        after Claude's last reply); return its reply.

        Claude starts on the first message and is restarted, resuming the
        saved session, after it exits or when the system prompt changes.
        Turns run one at a time.
        """
        async with self._turn_lock:
            return await self._run_turn(message, context_envelope, note=note)

    async def _load_agent_settings(self) -> AgentSettings:
        """The Manager's agent settings (system and role layers), read now; the last ones read when they
        cannot be read (the built-in defaults before any)."""
        agent_settings = await load_agent_settings(AgentType.MANAGER)
        if agent_settings.error is not None:
            return self._agent_settings or agent_settings
        self._agent_settings = agent_settings
        return agent_settings

    def _composed_prompt(self, agent_settings: AgentSettings) -> SystemPrompt:
        """The Manager's harness, its role prompt, then the task placement rule (no project, no task)."""
        return build_system_prompt(
            harness=agent_settings.harness,
            role_id="manager", role=self._system_prompt, recommended=self._prompt_recommended,
            contract_name="TASK_PLACEMENT_RULE", contract=TASK_PLACEMENT_RULE,
        )

    async def _run_turn(self, message: str, context_envelope: MessageEnvelope, *, note: str = "",
                        retried: bool = False) -> str:
        await self._load_db_config()
        if not self._session_loaded:
            await self._load_session_state()
        agent_settings = await self._load_agent_settings()
        if agent_settings.profile_error:
            raise RuntimeError(agent_settings.profile_error)  # answered as an error; never another profile
        system_prompt = self._composed_prompt(agent_settings)
        launch = Launch(agent_settings, await load_installed(self._runtime_root, AgentType.MANAGER.value),
                        role=AgentType.MANAGER)
        try:
            prompt_file = system_prompt.write_file(self._runtime_root)
        except OSError as exc:
            raise RuntimeError(f"无法写入 system prompt 文件：{exc}") from None
        # The command carries the model, the effort and the library's directories; the file is named by
        # its content's hash, kept beside it all the same; the environment and the library are compared
        # by their hashes.
        signature = (
            tuple(self._claude_command(prompt_file, launch, resume=False)), system_prompt.sha256, launch.env_sha256,
            launch.library.sha256, launch.binding_sha256,
        )
        switch = session_switch.profile_switch(self._session_id, self._session_runtime, self._session_profile,
                                               launch, session_provider=self._session_provider)
        if switch is not None:
            # This agent cannot resume the session: a new one starts, after the old one hands over.
            await self._switch_session(switch)
        if launch.runtime is not self._claude_runtime:
            # Each runtime type keeps its resident sessions its own way (after the old one's handoff).
            await self._claude.stop()
            self._claude, self._claude_runtime = launch.runtime.resident_session("manager"), launch.runtime
        if self._claude.stale(signature):
            await self._claude.stop()
        command = self._claude.command if self._claude.started else self._claude_command(prompt_file, launch)
        # A new session's first turn starts with the resume brief ('' when it has nothing to say), unless the user
        # asked for a clean one.
        brief = "" if self._claude.started or self._session_id or self._clean_start \
            else await self._resume_brief(context_envelope, message)

        task_id = context_envelope.metadata.get("task_id")
        asked = f"{note}\n\n{message}" if note else message
        recorder = ClaudeRunRecorder(
            agent_id="manager",
            task_id=task_id,
            command=command,
            cwd=str(self._config.work_dir),
            input_text=f"{brief}\n\n{asked}" if brief else asked,
            session_id=self._session_id,
            system_prompt=system_prompt,
            launch=launch,
        )
        await recorder.start()

        # The session this turn's new process resumes, if it starts one (its command resumes the saved session).
        resuming = self._session_id if not self._claude.started else None
        if not self._claude.started:
            self._session_runtime, self._session_profile = launch.runtime.type, launch.profile.id
            self._session_provider = launch.profile.provider.id
            try:
                await self._claude.start(
                    command, cwd=str(self._config.work_dir), signature=signature, env=launch.environment(),
                    totals=self._session_totals if resuming else None,
                    autonomous=self._autonomous_turns(command, system_prompt, launch), price=launch.price,
                )
            except OSError as exc:
                logger.error("failed to start Claude Code: %s", exc)
                await recorder.finish(output_text=None, result_event=None, error=f"Cannot start Claude Code: {exc}")
                return "无法启动 Claude Code，请检查安装。"
            self._process = _Process(command, signature, system_prompt, launch)

        text_parts: list[str] = []
        streamed_partials = False

        async def on_event(payload: dict[str, Any]) -> None:
            nonlocal streamed_partials
            await recorder.record(payload)

            session_id = payload.get("session_id")
            if isinstance(session_id, str) and session_id and session_id != self._session_id:
                self._session_id, self._clean_start = session_id, False
                await self._save_session_state()
                self._pending_switch = self._pending_handoff = None  # the new session has its resume brief

            event_type = payload.get("type")
            # Only the Manager's own turn is the reply; subagent output is internal.
            top_level = payload.get("parent_tool_use_id") is None

            if event_type == "system":
                subtype = payload.get("subtype", "")
                if subtype == "init":
                    await self._send_status(context_envelope, "thinking", phase=True, activity="thinking")

            elif event_type == "stream_event":
                event = payload.get("event")
                delta = event.get("delta") if isinstance(event, dict) else None
                if top_level and isinstance(delta, dict) and delta.get("type") == "text_delta":
                    chunk = str(delta.get("text") or "")
                    if chunk:
                        streamed_partials = True
                        await self._send_status(context_envelope, chunk, delta=True)

            elif event_type == "assistant" and top_level:
                msg = payload.get("message")
                if isinstance(msg, dict):
                    content_blocks = msg.get("content", [])
                    for block in content_blocks:
                        if not isinstance(block, dict):
                            continue
                        block_type = block.get("type", "")
                        if block_type == "text":
                            chunk = str(block.get("text") or "")
                            if chunk:
                                text_parts.append(chunk)
                                if not streamed_partials:  # else already sent chunk by chunk
                                    await self._send_status(context_envelope, chunk, delta=True)
                        elif block_type == "tool_use":
                            tool_name = block.get("name", "tool")
                            await self._send_status(
                                context_envelope,
                                f"正在使用工具: {tool_name}",
                                phase=True,
                                activity="tool",
                                tool=str(tool_name),
                            )
                        elif block_type == "thinking":
                            await self._send_status(context_envelope, "thinking", phase=True, activity="thinking")

        try:
            turn_text = await self._build_user_message(message, note, channel=channel_line(context_envelope))
            if brief:
                turn_text = f"{brief}\n\n{turn_text}"
            # A turn cut off by Claude's exit ends without a result; that Claude
            # is reaped and the next message starts a new process that resumes the session.
            turn = await self._claude.run_turn(turn_text, on_event, tag=context_envelope,
                                               stop=lambda: recorder.cap_stop)
        except BaseException as exc:
            # Never leave Claude running once the Manager gives up on it.
            await self._claude.stop(kill=True)
            await recorder.finish(
                output_text="".join(text_parts) or None, result_event=None,
                error=f"Manager stopped reading Claude: {type(exc).__name__}: {exc}",
            )
            raise

        if resuming and not turn.session_started and not retried:
            # Claude never started the saved session: it no longer exists. Every
            # turn would resume it again and never answer, so drop it and run
            # this message in a new session; the context each turn carries does
            # not depend on the old conversation.
            logger.warning("manager: session %s could not be resumed; starting a new session", resuming)
            await recorder.finish(
                output_text=None, result_event=None,
                error=f"session {resuming} could not be resumed{exit_note(turn.exit_code)}; retried in a new session",
            )
            await self._claude.stop()
            self._session_id, self._session_totals = None, Usage()  # its totals went with it
            await self._save_session_state()
            self._pending_switch = session_switch.session_lost(
                f"its saved session {resuming} could not be resumed", runtime=launch.runtime.type,
                profile=launch.profile.id)
            self._pending_handoff = None
            return await self._run_turn(message, context_envelope, note=note, retried=True)

        result_event = turn.result_event
        # The reply is the turn's final answer; what Claude wrote before its tool calls only streamed.
        final_text = answer_text(result_event, text_parts)
        # A turn cut off by Claude's exit is no reply, even after some text.
        exited = result_event is None
        # A turn stopped at its spend cap is no reply either; the next message starts a new process (a new cap).
        over_cap = launch.runtime.budget_error(result_event)
        if over_cap:
            await self._claude.stop()
        replied = not exited and not over_cap and bool(final_text)
        usage = turn.usage
        if not exited:
            self._session_totals = self._claude.totals
            await self._save_session_state()
        await recorder.finish(
            output_text=final_text or None,
            result_event=result_event,
            error=over_cap or (None if replied else (
                f"no reply (exit code {turn.exit_code})" if exited else "no reply"
            )),
            usage=usage,
        )

        if task_id:
            try:
                await db_activity.insert_activity(
                    task_id=task_id,
                    worker="manager",
                    kind="text",
                    summary=final_text[:500] if final_text else None,
                    detail=final_text if len(final_text) > 500 else None,
                )
            except Exception:
                logger.debug("failed to record manager activity", exc_info=True)

        if over_cap:
            raise RuntimeError(over_cap)
        if not replied:
            raise RuntimeError(
                f"Claude Code 没有返回内容（退出码 {turn.exit_code}）" if exited else "Claude Code 没有返回内容"
            )
        return final_text

    # --- A new session the user asks for (/new) ---

    async def _start_new_session(self, envelope: MessageEnvelope) -> None:
        """``/new``: stop Claude and drop the saved session, so the next message starts a new session without a
        resume brief; record the reset as a run and confirm it in the chat it came from. A turn under way finishes
        first. Tasks and their Planners are not touched."""
        origin = _message_origin(envelope)
        async with self._turn_lock:
            if not self._session_loaded:
                await self._load_session_state()
            # First: what a stopping Claude still writes (a turn of its own ending) saves the old session again.
            await self._claude.stop()
            previous = self._session_id, self._session_totals, self._clean_start
            self._session_id, self._session_totals, self._clean_start = None, Usage(), True
            try:
                await self._write_session_state()
            except Exception as exc:
                # Nothing is dropped: the next message resumes the saved session in a new process.
                self._session_id, self._session_totals, self._clean_start = previous
                logger.exception("manager: could not drop its session for /new")
                await self._send_reply(envelope, f"Manager 出错，没能开始新会话：{exc}", error=str(exc))
                return
            self._pending_switch = self._pending_handoff = None
            self._followup = {}
            await self._record_new_session(envelope, origin, previous[0])
        logger.info("manager: a new session on /new from %s (sender %s); the previous one was %s",
                    origin["channel"], origin["sender"] or "-", previous[0] or "none")
        await self._send_reply(envelope, NEW_SESSION_REPLY)

    async def _record_new_session(self, envelope: MessageEnvelope, origin: dict[str, str | None],
                                  previous: str | None) -> None:
        """The reset as one of the Manager's runs, under the session it ended (``metadata.session_reset``: who
        asked, from which channel and chat); a database error is logged, as for any run."""
        try:
            record_id = await db_llm_records.start_record(
                agent_id=self.agent_id, session_id=previous, input_text=str(envelope.payload.get("text", "")),
                metadata={"session_reset": {**origin, "previous_session_id": previous}},
            )
            await db_llm_records.finish_record(record_id, status="completed", output_text=NEW_SESSION_REPLY,
                                               cost_usd=0.0, duration_ms=0)
        except Exception:
            logger.warning("manager: could not record the new session", exc_info=True)

    # --- A new session: the handoff before it and the resume brief that starts it ---

    async def _switch_session(self, switch: SessionSwitch) -> None:
        """Move to a new session because the Manager's agent changed. While the old session's Claude still runs,
        it first writes a handoff for the new one (one HANDOFF turn, recorded as a run and never sent to a
        chat)."""
        process = self._process
        handoff: str | None = None
        if self._claude.started and process is not None and not self._claude.stale(process.signature):
            handoff = await self._handoff(switch, process)
        await self._claude.stop()
        logger.warning("manager: starting a new session (%s)%s", switch.describe(),
                       "; its previous session wrote a handoff" if handoff else "")
        self._session_id, self._session_totals = None, Usage()
        await self._save_session_state()
        self._pending_switch, self._pending_handoff = switch, handoff

    async def _handoff(self, switch: SessionSwitch, process: _Process) -> str | None:
        """The handoff the running session writes before ``switch``; None when it writes none in time."""
        text = (
            f"[HANDOFF] This session ends here ({switch.describe()}), and the next one cannot read this "
            "conversation. Write a compact handoff for it: what you and the user are working on in each chat, "
            "what you promised or are waiting for, the decisions and preferences the user gave, and open "
            f"questions. At most {HANDOFF_MAX_CHARS} characters. Reply with the handoff only; the user does not "
            "see it."
        )
        recorder = ClaudeRunRecorder(
            agent_id="manager", task_id=None, command=process.command, cwd=str(self._config.work_dir),
            input_text=text, session_id=self._session_id, system_prompt=process.system_prompt,
            launch=process.launch,
        )
        await recorder.start()
        parts: list[str] = []

        async def on_event(payload: dict[str, Any]) -> None:
            await recorder.record(payload)
            message = payload.get("message")
            if payload.get("type") == "assistant" and payload.get("parent_tool_use_id") is None \
                    and isinstance(message, dict):
                parts.extend(str(block.get("text") or "") for block in message.get("content") or []
                             if isinstance(block, dict) and block.get("type") == "text")

        try:
            turn = await asyncio.wait_for(self._claude.run_turn(text, on_event), HANDOFF_TIMEOUT_SEC)
        except Exception as exc:  # the turn was given up, which stopped Claude
            logger.warning("manager: its previous session wrote no handoff: %s", exc)
            await recorder.finish(output_text=None, result_event=None,
                                  error=f"no handoff: {type(exc).__name__}: {exc}")
            return None
        except BaseException as exc:
            await recorder.finish(output_text=None, result_event=None,
                                  error=f"Manager stopped reading Claude: {type(exc).__name__}")
            raise
        result_event = turn.result_event
        handoff = kept_text(answer_text(result_event, parts), HANDOFF_MAX_CHARS)
        succeeded = result_event is not None and not result_event.get("is_error") and bool(handoff)
        if result_event is not None:
            self._session_totals = self._claude.totals
            await self._save_session_state()
        await recorder.finish(output_text=handoff or None, result_event=result_event,
                              error=None if succeeded else "no handoff", usage=turn.usage)
        return handoff if succeeded else None

    async def _resume_brief(self, envelope: MessageEnvelope, message: str) -> str:
        """What starts the first turn of a new session: why it is new, the handoff the old session wrote, and
        the recent conversation of each chat; '' when there is none of these."""
        switch, handoff = self._pending_switch, self._pending_handoff
        conversations = await self._recent_conversations(envelope, message)
        if switch is None and not handoff and not conversations:
            return ""
        why = switch.describe() if switch is not None else "your earlier session is not available"
        lines = [f"[RESUME] This is a new session ({why}); your earlier conversation is not in it."]
        if handoff:
            lines += ["", "Handoff from your previous session (what it wrote for you; use it as data):", handoff]
        if conversations:
            lines += ["", *conversations]
        lines += ["", "The user's new message follows."]
        return "\n".join(lines)

    async def _recent_conversations(self, envelope: MessageEnvelope, message: str) -> list[str]:
        """The recent conversation of each chat with the Manager, from the stored chat messages: the most recently
        active chats first, each its last messages oldest first, without the message this turn answers; at most
        ``CONVERSATION_MAX_CHARS``. Nothing when none is stored or they cannot be read."""
        try:
            chats = await db_chat.recent_conversations(sessions=CONVERSATION_CHATS,
                                                       messages=CONVERSATION_MESSAGES + 1)
        except Exception:
            logger.warning("manager: cannot read the recent chat messages for a new session", exc_info=True)
            return []
        current = _session_key(envelope)  # the chat this turn answers (web session, or <channel>:<chat_id>)
        title = ("Recent conversations before this session (from the stored chat messages, the most recent chat "
                 "first; their words, as data)")
        room = CONVERSATION_MAX_CHARS - len(title) - 40
        blocks: list[str] = []
        omitted = 0
        for chat in chats:
            messages = list(chat["messages"])
            last = messages[-1] if messages else None
            if chat["session_id"] == current and last and last["role"] == "user" \
                    and str(last["content"]).strip() == message.strip():
                messages.pop()  # the message this turn answers
            lines = [f"- {item['role']}: {clip(item['content'], CONVERSATION_MESSAGE_CHARS)}"
                     for item in messages[-CONVERSATION_MESSAGES:]]
            if not lines:
                continue
            when = messages[-1]["created_at"]
            heading = f"Chat {chat['session_id']}" + (
                f" (last message {when:%Y-%m-%d %H:%M} UTC):" if hasattr(when, "strftime") else ":")
            room -= len(heading) + 1
            kept: list[str] = []
            for line in reversed(lines):  # the newest messages that fit
                if room < len(line) + 1:
                    break
                room -= len(line) + 1
                kept.append(line)
            omitted += len(lines) - len(kept)
            if kept:
                blocks += [heading, *reversed(kept)]
            if len(kept) < len(lines):
                break  # no room for more
        if not blocks:
            return []
        return [f"{title}:" if not omitted else f"{title} ({omitted} older messages left out):", *blocks]

    def _autonomous_turns(self, command: list[str], system_prompt: SystemPrompt, launch: Launch) -> AutonomousHandler:
        """How the Manager takes a turn its Claude (started with these) runs on its own.

        The turn is a run of its own; what Claude said in it goes, as a reply
        of its own, to the chat of the message Claude answered before it (the
        turn's tag), and only to the record when there is none.
        """

        async def begin(after: Any) -> AutonomousRun:
            recorder = ClaudeRunRecorder(
                agent_id="manager",
                task_id=None,
                command=command,
                cwd=str(self._config.work_dir),
                input_text=AUTONOMOUS_INPUT,
                session_id=self._session_id,
                system_prompt=system_prompt,
                launch=launch,
                autonomous=True,
            )
            await recorder.start()

            async def on_end(text: str, turn: ResidentTurn) -> None:
                if turn.result_event is not None:
                    self._session_totals = self._claude.totals  # it spent too: a restart resumes from here
                    await self._save_session_state()
                if text:
                    await self._send_own_reply(after if isinstance(after, MessageEnvelope) else None, text)

            return AutonomousRun(recorder, on_end)

        return begin

    async def _send_own_reply(self, last: MessageEnvelope | None, text: str) -> None:
        """Send what Claude said on its own to the channel of ``last``, as a reply to no message.

        It names ``last``'s web chat session (``chat_session_id``), so the web
        channel adds it to that chat instead of a message's stream.
        """
        if last is None or self._bus is None:
            logger.info("manager: Claude spoke on its own before any message; its words are only recorded")
            return
        payload: dict[str, Any] = {"text": text, "autonomous": True}
        context = last.payload.get("channel_context")
        if isinstance(context, dict) and context.get("session_id"):
            payload["chat_session_id"] = context["session_id"]
        await self._bus.publish(MessageEnvelope.create(
            source=MANAGER_ADDRESS,
            target=last.source,
            message_type=MessageType.AGENT_REPLY,
            payload=payload,
            correlation_id=last.correlation_id,
            metadata={"origin_channel_id": last.metadata.get("origin_channel_id")},
        ))
        await self._persist_chat(last, "assistant", text, f"auto-{uuid.uuid4().hex[:12]}")

    def _claude_command(self, prompt_file: Path, launch: Launch, *, resume: bool = True) -> list[str]:
        """The resident process's command (``RuntimeAdapter.resident_command``), resuming the saved session."""
        runtime = launch.runtime
        command = runtime.resident_command(launch.executable(self._config.claude_bin), cwd=self._config.work_dir,
                                           prompt_file=prompt_file, launch=launch)
        if resume and self._session_id:
            command.extend(runtime.resume_args(self._session_id))
        return command

    async def _build_user_message(self, user_message: str, note: str = "", *, channel: str = "") -> str:
        parts = await self._projects_section()
        parts += await self._ready_tasks_section()

        if self._goal_snapshots:
            parts.append("当前活跃目标:")
            for gid, snap in self._goal_snapshots.items():
                parts.append(f"  - [{snap.status}] {snap.title or gid}: {snap.progress}")
            parts.append("")

        if note:
            parts += [note, ""]
        if channel:
            parts += [channel, ""]
        parts.append(user_message)
        return "\n".join(parts)

    async def _projects_section(self) -> list[str]:
        """The existing projects, so Claude can name a task's project."""
        try:
            projects = await db_projects.list_projects()
        except Exception:
            logger.warning("manager could not list projects", exc_info=True)
            return ["Projects: (could not be read)", ""]
        lines = ["Projects:"]
        for project in projects:
            directory = f"default directory {project['work_dir']}" if project["work_dir"] else "no default directory"
            line = f"  - {project['name']} ({directory})"
            description = " ".join(project["description"].split())
            if description:
                line += f": {description if len(description) <= 80 else description[:79] + '…'}"
            lines.append(line)
        if not projects:
            lines.append("  (none yet)")
        lines.append("")
        return lines

    async def _ready_tasks_section(self) -> list[str]:
        """The tasks created but not started, newest first, so Claude can start one by its id; none, no section."""
        try:
            tasks = await db_tasks.list_tasks(limit=READY_TASKS_LISTED, status=db_tasks.READY)
        except Exception:
            logger.warning("manager could not list the ready tasks", exc_info=True)
            return ["Ready tasks: (could not be read)", ""]
        if not tasks:
            return []
        lines = ["Ready tasks (created, not started; newest first):"]
        for task in tasks:
            goal = f", goal {task['goal_id']}" if task.get("goal_id") else ""
            lines.append(f"  - {task['id']} (project {task['project_name']}{goal}): {clip(task['requirement'], 80)}")
        lines.append("")
        return lines

    def _parse_response(self, response: str) -> dict[str, Any]:
        response = response.strip()

        task_json: dict[str, Any] | None = None
        json_start = -1

        if response.startswith("{"):
            try:
                data = json.loads(response)
                if isinstance(data, dict) and data.get("action") in ACTIONS:
                    task_json = data
                    json_start = 0
            except json.JSONDecodeError:
                pass

        if task_json is None and "```json" in response:
            try:
                fence = response.index("```json")
                start = fence + 7
                end = response.index("```", start)
                data = json.loads(response[start:end].strip())
                if isinstance(data, dict) and data.get("action") in ACTIONS:
                    task_json = data
                    json_start = fence
            except (json.JSONDecodeError, ValueError):
                pass

        if task_json is None and "{" in response:
            idx = response.index("{")
            candidate = response[idx:]
            try:
                data = json.loads(candidate)
                if isinstance(data, dict) and data.get("action") in ACTIONS:
                    task_json = data
                    json_start = idx
            except json.JSONDecodeError:
                pass

        if task_json is not None:
            prose_before = response[:json_start].strip() if json_start > 0 else ""
            if prose_before and len(prose_before) > 20:
                task_json["needs_clarification"] = True
                task_json["clarification_text"] = prose_before
                return task_json
            return task_json

        return {"response_text": response}

    async def _persist_chat(
        self, env: MessageEnvelope, role: str, content: str, message_id: str
    ) -> None:
        """Record a non-web turn in chat_messages so the console shows every channel's conversation.

        Web turns are already persisted by the web channel's own path; here we cover Telegram and Lark,
        keyed by ``<channel>:<chat_id>``. A persistence failure never blocks answering the user.
        """
        channel = _message_origin(env)["channel"]
        key = _session_key(env)
        if channel == "web" or not key or not content:
            return
        try:
            await db_chat.save_message(
                session_id=key, message_id=message_id,
                role=role, content=content, channel=channel,
            )
        except Exception:
            logger.warning("manager: could not persist a %s chat turn", channel, exc_info=True)

    async def _send_reply(self, original: MessageEnvelope, text: str, *, error: str | None = None) -> None:
        """Answer ``original``; ``error`` says why the turn failed (a chat channel shows it on its status line)."""
        assert self._bus is not None
        await self._bus.publish(MessageEnvelope.create(
            source=MANAGER_ADDRESS,
            target=original.source,
            message_type=MessageType.AGENT_REPLY,
            payload={"text": text, **({"error": error} if error else {})},
            correlation_id=original.correlation_id,
            reply_to=original.message_id,
            metadata={"origin_channel_id": original.metadata.get("origin_channel_id")},
        ))
        await self._persist_chat(original, "assistant", text, f"{original.message_id}:reply")

    async def _send_status(
        self,
        original: MessageEnvelope,
        text: str,
        *,
        delta: bool = False,
        phase: bool = False,
        activity: str | None = None,
        tool: str | None = None,
    ) -> None:
        """A status of the turn answering ``original``: streamed text (``delta``), or a phase (``phase``) with what
        it is (``activity``: ``thinking`` or ``tool``, with the ``tool``'s name)."""
        assert self._bus is not None
        extra = {key: value for key, value in (("activity", activity), ("tool", tool)) if value}
        await self._bus.publish(MessageEnvelope.create(
            source=MANAGER_ADDRESS,
            target=original.source,
            message_type=MessageType.STATUS_UPDATE,
            payload={"text": text, "delta": delta, "phase": phase, **extra},
            correlation_id=original.correlation_id,
            reply_to=original.message_id,
            metadata={"origin_channel_id": original.metadata.get("origin_channel_id")},
        ))
