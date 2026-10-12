"""Manager Agent: user-facing steward backed by Claude Code in pp mode.

The Manager is the only agent that communicates with users. It:
- Receives user messages from all channels via the Bus
- Classifies intent (direct answer, clarification, task request)
- Forwards structured task requests to the Planner
- Maintains a goal portfolio status snapshot for quick status queries
- Streams responses back through the originating channel
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import subprocess
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..bus import MessageBus
from ..message import (
    MANAGER_ADDRESS,
    PLANNER_ADDRESS,
    AgentAddress,
    MessageEnvelope,
    MessageType,
)

logger = logging.getLogger(__name__)


@dataclass
class ManagerConfig:
    claude_bin: str = "claude"
    work_dir: Path = field(default_factory=lambda: Path.cwd())
    tool_scope: str = "read_only"
    session_ttl_hours: int = 168  # 1 week
    max_context_tokens: int = 100_000


@dataclass
class GoalSnapshot:
    """Cached status of a goal for quick progress queries."""

    goal_id: str
    title: str = ""
    status: str = "active"
    plan_summary: str = ""
    progress: str = ""
    last_update: str = ""


class ManagerAgent:
    """Long-running Manager Agent backed by Claude Code in pp mode."""

    def __init__(
        self,
        *,
        config: ManagerConfig,
        runtime_root: Path,
    ) -> None:
        self._config = config
        self._runtime_root = runtime_root
        self._session_id: str | None = None
        self._bus: MessageBus | None = None
        self._task: asyncio.Task[Any] | None = None
        self._running = False
        self._goal_snapshots: dict[str, GoalSnapshot] = {}
        self._load_session_state()

    @property
    def agent_id(self) -> str:
        return "manager"

    @property
    def agent_address(self) -> AgentAddress:
        return MANAGER_ADDRESS

    def _state_path(self) -> Path:
        return self._runtime_root / ".local" / "manager" / "session.json"

    def _load_session_state(self) -> None:
        path = self._state_path()
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                self._session_id = data.get("session_id")
            except (json.JSONDecodeError, OSError):
                pass

    def _save_session_state(self) -> None:
        path = self._state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"session_id": self._session_id}, indent=2),
            encoding="utf-8",
        )

    async def start(self, bus: MessageBus) -> None:
        self._bus = bus
        self._running = True
        self._task = asyncio.create_task(self._message_loop())
        logger.info("manager agent started (session=%s)", self._session_id)

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        self._save_session_state()

    def is_healthy(self) -> bool:
        return self._running and bool(shutil.which(self._config.claude_bin))

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
            case MessageType.WORK_RESULT:
                await self._handle_task_result(envelope)
            case MessageType.AGENT_LIFECYCLE:
                await self._handle_lifecycle(envelope)
            case _:
                logger.debug("manager ignoring message type %s", envelope.message_type)

    async def _handle_user_message(self, envelope: MessageEnvelope) -> None:
        assert self._bus is not None
        text = envelope.payload.get("text", "")
        if not text.strip():
            return

        # Stream acknowledgment first
        await self._send_status(
            envelope, "正在处理..."
        )

        # Invoke Claude Code in pp mode
        response = await self._invoke_claude(text, envelope)
        parsed = self._parse_response(response)

        if parsed.get("needs_clarification"):
            await self._bus.publish(MessageEnvelope.create(
                source=MANAGER_ADDRESS,
                target=envelope.source,
                message_type=MessageType.CLARIFICATION_REQUEST,
                payload={"text": parsed.get("clarification_text", "")},
                correlation_id=envelope.correlation_id,
                reply_to=envelope.message_id,
                metadata={"origin_channel_id": envelope.metadata.get("origin_channel_id")},
            ))
        elif parsed.get("is_task_request"):
            # Forward to Planner
            await self._bus.publish(MessageEnvelope.create(
                source=MANAGER_ADDRESS,
                target=PLANNER_ADDRESS,
                message_type=MessageType.TASK_REQUEST,
                payload={
                    "goal_id": parsed.get("goal_id", ""),
                    "requirement": parsed.get("structured_requirement", text),
                    "original_user_text": text,
                    "user_language": envelope.payload.get("language", "zh"),
                    "project_root": str(self._config.work_dir),
                    "priority": parsed.get("priority", "P2"),
                    "constraints": parsed.get("constraints", []),
                },
                correlation_id=envelope.correlation_id,
                metadata={"origin_channel_id": envelope.metadata.get("origin_channel_id")},
            ))
            # Acknowledge to user
            ack_text = parsed.get("acknowledgment", f"收到，已安排处理: {text[:100]}")
            await self._send_reply(envelope, ack_text)
        else:
            # Direct answer
            reply_text = parsed.get("response_text", response)
            await self._send_reply(envelope, reply_text)

    async def _handle_goal_status(self, envelope: MessageEnvelope) -> None:
        goal_id = envelope.payload.get("goal_id", "")
        if goal_id:
            snapshot = self._goal_snapshots.get(goal_id, GoalSnapshot(goal_id=goal_id))
            snapshot.status = envelope.payload.get("status", snapshot.status)
            snapshot.progress = envelope.payload.get("progress", snapshot.progress)
            snapshot.last_update = envelope.payload.get("timestamp", "")
            self._goal_snapshots[goal_id] = snapshot

        # Forward progress to the originating channel
        origin = envelope.metadata.get("origin_channel_id")
        if origin:
            progress_text = envelope.payload.get("summary", "")
            if progress_text:
                await self._bus.publish(MessageEnvelope.create(
                    source=MANAGER_ADDRESS,
                    target=envelope.source,
                    message_type=MessageType.STATUS_UPDATE,
                    payload={"text": progress_text},
                    correlation_id=envelope.correlation_id,
                    metadata={"origin_channel_id": origin},
                ))

    async def _handle_task_result(self, envelope: MessageEnvelope) -> None:
        status = envelope.payload.get("status", "")
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

    async def _invoke_claude(self, message: str, context_envelope: MessageEnvelope) -> str:
        """Invoke Claude Code in pp mode, returns the response text."""
        command = [
            self._config.claude_bin,
            "--print",
            "--output-format", "stream-json",
            "--verbose",
            "--permission-mode", "plan",
            "--tools", "Read,Glob,Grep" if self._config.tool_scope == "read_only" else "",
            "--disable-slash-commands",
        ]
        if self._session_id:
            command.extend(["--resume", self._session_id])
        else:
            sid = str(uuid.uuid4())
            command.extend(["--session-id", sid])
            self._session_id = sid

        # Build prompt with manager context
        prompt = self._build_prompt(message)
        command.append(prompt)

        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                cwd=str(self._config.work_dir),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except OSError as exc:
            logger.error("failed to start Claude Code: %s", exc)
            return "无法启动 Claude Code，请检查安装。"

        parts: list[str] = []
        result_text = ""

        assert process.stdout is not None
        async for raw_line in process.stdout:
            line = raw_line.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(payload, dict):
                continue

            session_id = payload.get("session_id")
            if isinstance(session_id, str) and session_id:
                self._session_id = session_id
                self._save_session_state()

            if payload.get("type") == "stream_event":
                event = payload.get("event")
                if isinstance(event, dict):
                    if event.get("type") == "content_block_delta":
                        delta = event.get("delta")
                        if isinstance(delta, dict) and delta.get("type") == "text_delta":
                            chunk = str(delta.get("text") or "")
                            parts.append(chunk)
                            # Stream delta to user channel
                            if self._bus and chunk.strip():
                                await self._send_status(
                                    context_envelope, chunk, delta=True
                                )
            elif payload.get("type") == "result":
                result_text = str(payload.get("result") or "")

        return_code = await process.wait()
        if return_code != 0:
            logger.warning("Claude Code exited with code %d", return_code)

        self._save_session_state()
        return "".join(parts) or result_text

    def _build_prompt(self, user_message: str) -> str:
        """Build the prompt with manager context."""
        parts = [
            "You are the Manager Agent (管家) for LoopX. Your role:",
            "1. Understand user intent and respond naturally",
            "2. For simple questions, answer directly",
            "3. For task requests (development, testing, analysis), create a structured plan",
            "4. For status queries, report current progress",
            "",
            "Respond in the user's language. Be concise.",
            "",
        ]

        # Add goal portfolio snapshot
        if self._goal_snapshots:
            parts.append("Current active goals:")
            for gid, snap in self._goal_snapshots.items():
                parts.append(f"  - [{snap.status}] {snap.title or gid}: {snap.progress}")
            parts.append("")

        parts.append(f"User message: {user_message}")
        return "\n".join(parts)

    def _parse_response(self, response: str) -> dict[str, Any]:
        """Parse Claude's response to determine the action type.

        Returns a dict with keys:
        - is_task_request: bool
        - needs_clarification: bool
        - response_text: str (for direct answers)
        - clarification_text: str (if needs_clarification)
        - structured_requirement: str (if is_task_request)
        - acknowledgment: str (if is_task_request)
        - goal_id: str (if is_task_request)
        - priority: str (if is_task_request)
        """
        # For now, simple heuristic. In production, the prompt should
        # instruct Claude to output structured JSON for task requests.
        response = response.strip()

        # Try to parse as structured JSON first
        if response.startswith("{"):
            try:
                data = json.loads(response)
                if isinstance(data, dict) and "action" in data:
                    return data
            except json.JSONDecodeError:
                pass

        # Default: treat as direct answer
        return {"response_text": response}

    async def _send_reply(self, original: MessageEnvelope, text: str) -> None:
        assert self._bus is not None
        await self._bus.publish(MessageEnvelope.create(
            source=MANAGER_ADDRESS,
            target=original.source,
            message_type=MessageType.AGENT_REPLY,
            payload={"text": text},
            correlation_id=original.correlation_id,
            reply_to=original.message_id,
            metadata={"origin_channel_id": original.metadata.get("origin_channel_id")},
        ))

    async def _send_status(
        self,
        original: MessageEnvelope,
        text: str,
        *,
        delta: bool = False,
    ) -> None:
        assert self._bus is not None
        await self._bus.publish(MessageEnvelope.create(
            source=MANAGER_ADDRESS,
            target=original.source,
            message_type=MessageType.STATUS_UPDATE,
            payload={"text": text, "delta": delta},
            correlation_id=original.correlation_id,
            reply_to=original.message_id,
            metadata={"origin_channel_id": original.metadata.get("origin_channel_id")},
        ))
