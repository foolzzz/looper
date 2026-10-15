"""Unified message protocol for the three-tier agent system.

All inter-agent and channel communication uses a single typed MessageEnvelope.
This replaces the ad-hoc mix of HTTP JSON, stream-json lines, and filesystem
inbox records with one canonical format.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, TypedDict


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
    TASK_CANCEL = "task_cancel"
    TASK_STOP = "task_stop"
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


@dataclass(frozen=True)
class AgentAddress:
    """Identifies a message endpoint in the agent system."""

    agent_type: str  # "channel", "manager", "planner", "worker"
    agent_id: str
    channel: str | None = None  # "web" | "telegram" | "lark" (channel tier only)

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
    event: str  # "started" | "stopped" | "crashed" | "max_restarts_exceeded"
    detail: str


# --- Address helpers ---

MANAGER_ADDRESS = AgentAddress(agent_type="manager", agent_id="manager")
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


def worker_address(worker_id: str) -> AgentAddress:
    return AgentAddress(agent_type="worker", agent_id=worker_id)
