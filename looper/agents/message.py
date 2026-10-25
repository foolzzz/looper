"""Unified message protocol for the three-tier agent system.

All inter-agent and channel communication uses a single typed MessageEnvelope.
This replaces the ad-hoc mix of HTTP JSON, stream-json lines, and filesystem
inbox records with one canonical format.
"""

from __future__ import annotations

import re
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, TypedDict


LARK_CHANNEL_TYPES = ("lark", "lark_bot")


def is_lark(channel: str | None) -> bool:
    """Whether a channel uses Lark messages and targets."""
    return channel in LARK_CHANNEL_TYPES


class MessageType(str, Enum):
    # Channel ↔ Manager
    USER_MESSAGE = "user_message"
    AGENT_REPLY = "agent_reply"
    CLARIFICATION_REQUEST = "clarification_request"
    CLARIFICATION_RESPONSE = "clarification_response"
    STATUS_UPDATE = "status_update"
    # Manager → Telegram or Lark: tell a chat, unprompted, that a task ended (``task_notice.Notice``).
    CHAT_NOTICE = "chat_notice"

    # Manager ↔ Planner
    TASK_REQUEST = "task_request"
    TASK_ACCEPTED = "task_accepted"
    TASK_REJECTED = "task_rejected"
    GOAL_STATUS = "goal_status"

    # Planner ↔ Worker
    WORK_ITEM = "work_item"
    WORK_RESULT = "work_result"
    WORK_PROGRESS = "work_progress"
    WORKER_INPUT = "worker_input"  # a user turn for a running worker (reply: WORKER_INPUT_RESULT)
    WORKER_INPUT_RESULT = "worker_input_result"
    KILL_WORK = "kill_work"  # stop a running worker mid-run (reply: KILL_WORK_RESULT)
    KILL_WORK_RESULT = "kill_work_result"

    # Task lifecycle
    # Dashboard or Manager → TaskRouter: start a ready task (created, never started).
    TASK_START = "task_start"
    # Dashboard or Manager → a task's Planner: the task changed (a supplement, a requirement edit); stored first, so
    # this only wakes the Planner, which takes every change waiting.
    TASK_CHANGE = "task_change"
    # Dashboard → a task's Planner: the user wrote in a user gate's discussion, or ended it; stored first, so this
    # only wakes the Planner, which takes every message waiting.
    GATE_MESSAGE = "gate_message"
    # The agent room → a task's Planner: messages for it (or Looper's escalations) wait in the task's room; stored
    # first, so this only wakes the Planner, which takes every one waiting in a [ROOM] turn.
    ROOM_MESSAGE = "room_message"
    # The service or the user moved a wait; the row is stored first and this only wakes the Planner, which reads
    # ready waits (and, later, timed-out and cancel-requested ones) at the end of the event.
    WAIT_EVENT = "wait_event"
    TASK_CANCEL = "task_cancel"
    TASK_STOP = "task_stop"
    # Dashboard or Manager → TaskRouter: run a stopped, failed or cancelled task again, as its ``RestartMode`` says.
    TASK_RESTART = "task_restart"
    # TaskRouter → a task's new Planner: carry on with the plan saved before the server stopped.
    TASK_RESUME = "task_resume"

    # Verification (方案 C)
    VERIFY_REQUEST = "verify_request"
    VERIFY_RESULT = "verify_result"

    # System
    HEARTBEAT = "heartbeat"
    AGENT_LIFECYCLE = "agent_lifecycle"
    SYSTEM_EVENT = "system_event"


class RestartMode(str, Enum):
    """How a ``TASK_RESTART`` runs a stopped, failed or cancelled task again (its payload's ``mode``)."""

    # From its saved plan: accepted steps stay done, the others are back to not started, and the Planner gets one
    # [RESTART] turn on how the task goes on. The default.
    CONTINUE = "continue"
    # Planned anew: its steps, its plan, its Planner's session and its step worktrees are deleted.
    START_OVER = "start_over"
    # A task that failed at its spend cap, the cap raised (``/continue``): its plan goes on without a turn.
    AT_CAP = "at_cap"


@dataclass(frozen=True)
class AgentAddress:
    """Identifies a message endpoint in the agent system."""

    agent_type: str  # "channel", "manager", "planner", "worker"
    agent_id: str
    channel: str | None = None  # "web" | "telegram" | "lark" | "lark_bot" (channel tier only)

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"agent_type": self.agent_type, "agent_id": self.agent_id}
        if self.channel is not None:
            d["channel"] = self.channel
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> AgentAddress:
        return cls(
            agent_type=d["agent_type"],
            agent_id=d["agent_id"],
            channel=d.get("channel"),
        )


def _make_message_id() -> str:
    return str(uuid.uuid4())


def _make_timestamp() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class MessageEnvelope:
    """Canonical message envelope for all agent communication."""

    message_id: str
    correlation_id: str  # traces a user request through all tiers
    timestamp: str
    source: AgentAddress
    target: AgentAddress
    message_type: MessageType
    payload: dict[str, Any]
    reply_to: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "message_id": self.message_id,
            "correlation_id": self.correlation_id,
            "timestamp": self.timestamp,
            "source": self.source.to_dict(),
            "target": self.target.to_dict(),
            "message_type": self.message_type.value,
            "payload": self.payload,
            "reply_to": self.reply_to,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> MessageEnvelope:
        return cls(
            message_id=d["message_id"],
            correlation_id=d["correlation_id"],
            timestamp=d["timestamp"],
            source=AgentAddress.from_dict(d["source"]),
            target=AgentAddress.from_dict(d["target"]),
            message_type=MessageType(d["message_type"]),
            payload=d["payload"],
            reply_to=d.get("reply_to"),
            metadata=d.get("metadata", {}),
        )

    @classmethod
    def create(
        cls,
        *,
        source: AgentAddress,
        target: AgentAddress,
        message_type: MessageType,
        payload: dict[str, Any],
        correlation_id: str | None = None,
        reply_to: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> MessageEnvelope:
        return cls(
            message_id=_make_message_id(),
            correlation_id=correlation_id or _make_message_id(),
            timestamp=_make_timestamp(),
            source=source,
            target=target,
            message_type=message_type,
            payload=payload,
            reply_to=reply_to,
            metadata=metadata or {},
        )


# --- Typed payloads ---


class UserMessagePayload(TypedDict, total=False):
    text: str
    language: str
    attachments: list[dict[str, Any]]
    channel_context: dict[str, Any]


class TaskRequestPayload(TypedDict, total=False):
    goal_id: str
    title: str  # a few words naming the task, in the user's language
    requirement: str
    original_user_text: str
    user_language: str
    work_dir: str  # absolute directory the task runs in
    priority: str  # "P0" - "P3"
    constraints: list[str]


class WorkItemPayload(TypedDict, total=False):
    task_id: str
    plan_id: str
    step_index: int
    instruction: str
    target_files: list[str]
    validation_commands: list[str]
    context: dict[str, Any]
    acceptance_criteria: list[str]


class WorkResultPayload(TypedDict, total=False):
    task_id: str
    plan_id: str
    status: str  # "completed" | "failed" | "blocked"
    changed_files: dict[str, Any]
    test_results: list[dict[str, Any]]
    self_check: dict[str, Any]
    error: str


class VerifyRequestPayload(TypedDict, total=False):
    task_id: str
    plan_id: str
    work_result: dict[str, Any]
    acceptance_criteria: list[str]


class VerifyResultPayload(TypedDict, total=False):
    task_id: str
    plan_id: str
    passed: bool
    findings: list[str]
    suggestion: str


class AgentLifecyclePayload(TypedDict, total=False):
    agent_id: str
    agent_type: str
    # "started" | "start_failed" | "crashed" | "crash_looping" (restarted supervisor.CRASH_LOOP_RESTARTS times in a
    # row; it is still restarted) | "recovered" (stable again after crash-looping)
    event: str
    detail: str


# --- Address helpers ---

def manager_address(manager_id: str) -> AgentAddress:
    return AgentAddress(agent_type="manager", agent_id=manager_id)


MANAGER_ADDRESS = manager_address("manager")
TASK_ROUTER_ADDRESS = AgentAddress(agent_type="task_router", agent_id="task-router")

# Kept for backwards compatibility in tests; prefer planner_address(task_id).
PLANNER_ADDRESS = AgentAddress(agent_type="planner", agent_id="planner")


def planner_address(task_id: str) -> AgentAddress:
    return AgentAddress(agent_type="planner", agent_id=f"planner-{task_id}")


def channel_address(channel: str, channel_id: str | None = None) -> AgentAddress:
    return AgentAddress(
        agent_type="channel",
        agent_id=channel_id or f"channel-{channel}",
        channel=channel,
    )


BOT_CHANNEL_TYPES = ("telegram", "lark", "lark_bot")
_CHANNEL_ID = re.compile(r"^channel-(telegram|lark_bot|lark)(?:-([1-9][0-9]{0,8}))?$")
# The same shape for PostgreSQL (``~``): a Telegram or Lark bot's channel id, built-in or added.
CHANNEL_ID_PATTERN = r"^channel-(telegram|lark_bot|lark)(-[1-9][0-9]*)?$"


@dataclass(frozen=True)
class ChannelId:
    """A Telegram or Lark bot's channel id: its type and, for a bot added in Settings → Channels, its number
    (``channel-<type>-<n>``); None for the built-in row (``channel-<type>``)."""

    channel_type: str
    number: int | None

    @property
    def builtin(self) -> bool:
        return self.number is None

    def __str__(self) -> str:
        return f"channel-{self.channel_type}" if self.number is None else f"channel-{self.channel_type}-{self.number}"


def parse_channel_id(channel_id: str | None) -> ChannelId | None:
    """The bot a channel id names; None for anything else (``channel-web`` too)."""
    match = _CHANNEL_ID.match(channel_id or "")
    if match is None:
        return None
    return ChannelId(match.group(1), int(match.group(2)) if match.group(2) else None)


_THREAD_MANAGER_ID = re.compile(r"manager(?:-[a-z][a-z0-9-]{1,30})?")
# Lark also uses open ids for direct chats; only identity separators and whitespace are unsafe.
_THREAD_CHAT_IDS = {"telegram": re.compile(r"-?[0-9]+"),
                    "lark": re.compile(r"[^#:\s]+"),
                    "lark_bot": re.compile(r"[^#:\s]+")}


@dataclass(frozen=True)
class ChannelThreadId:
    """A bot conversation's immutable identity; ``manager_id`` is only its cross-owner suffix."""

    channel_type: str
    channel_id: str
    chat_id: str
    manager_id: str | None


def parse_channel_thread_id(session_id: str | None) -> ChannelThreadId | None:
    """Recover the bot and raw platform chat id, or None for a non-channel or malformed identity.

    Only a valid terminal ``#manager`` or ``#manager-<key>`` is a suffix. Empty or malformed suffixes are
    rejected; platform chat ids never contain ``#``. Built-in bots retain ``<type>:<chat>``; added bots use
    ``<channel_id>:<chat>``. Web, API client and Planner session ids have their own independent grammar.
    """
    if not isinstance(session_id, str):
        return None
    base, separator, manager_id = session_id.partition("#")
    if separator and not _THREAD_MANAGER_ID.fullmatch(manager_id):
        return None
    prefix, separator, chat_id = base.partition(":")
    if not separator:
        return None
    if prefix in BOT_CHANNEL_TYPES:
        channel_type, channel_id = prefix, f"channel-{prefix}"
    else:
        parsed = parse_channel_id(prefix)
        if parsed is None or parsed.builtin or str(parsed) != prefix:
            return None
        channel_type, channel_id = parsed.channel_type, prefix
    if not _THREAD_CHAT_IDS[channel_type].fullmatch(chat_id):
        return None
    return ChannelThreadId(channel_type, channel_id, chat_id, manager_id or None)


def channel_thread_id(channel_id: str, chat_id: str | int, manager_id: str, bot_manager_id: str) -> str:
    """Name a bot's conversation, adding its destination only when it differs from the bot's immutable owner.

    An explicit binding back to the owner reuses the legacy unsuffixed id. Invalid bot, chat or Manager ids
    raise ValueError rather than creating an identity the parser cannot round-trip.
    """
    parsed = parse_channel_id(channel_id)
    if parsed is None or str(parsed) != channel_id:
        raise ValueError(f"not a bot channel id: {channel_id!r}")
    if not _THREAD_MANAGER_ID.fullmatch(manager_id) or not _THREAD_MANAGER_ID.fullmatch(bot_manager_id):
        raise ValueError("channel thread destination and bot owner must be Manager ids")
    if not _THREAD_CHAT_IDS[parsed.channel_type].fullmatch(str(chat_id)):
        raise ValueError(f"not a {parsed.channel_type} chat id: {chat_id!r}")
    prefix = parsed.channel_type if parsed.builtin else channel_id
    suffix = f"#{manager_id}" if manager_id != bot_manager_id else ""
    return f"{prefix}:{chat_id}{suffix}"


def builtin_channel_id(channel_type: str) -> str:
    """The channel id of a type's built-in row."""
    if channel_type not in BOT_CHANNEL_TYPES:
        raise ValueError(f"not a bot channel type: {channel_type!r}")
    return f"channel-{channel_type}"


def worker_address(worker_id: str) -> AgentAddress:
    return AgentAddress(agent_type="worker", agent_id=worker_id)
