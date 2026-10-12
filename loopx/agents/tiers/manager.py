"""Manager Agent: user-facing steward backed by a persistent Claude Code session.

The Manager is the only agent that communicates with users. It:
- Maintains a long-running Claude Code session (pp mode + stream-json)
- Receives user messages from all channels via the Bus
- Classifies intent (direct answer, clarification, task request)
- Forwards structured task requests to the TaskRouter
- Streams Claude Code responses in real-time to the originating channel
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

from ..bus import MessageBus
from ..message import (
    MANAGER_ADDRESS,
    TASK_ROUTER_ADDRESS,
    AgentAddress,
    MessageEnvelope,
    MessageType,
)

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """\
You are the Manager Agent (管家) for LoopX, an autonomous multi-agent programming system.

## Your Role
1. Understand user intent and respond naturally in the user's language
2. For simple questions or conversation, answer directly
3. For task requests (development, testing, analysis, refactoring), output a structured JSON task
4. For status queries, report current progress from the goal portfolio

## Task Output Format
When you identify a task request, respond with ONLY this JSON (no other text):
```json
{
  "action": "task_request",
  "goal_id": "<short-kebab-id>",
  "structured_requirement": "<clear one-paragraph requirement>",
  "priority": "P2",
  "acknowledgment": "<brief acknowledgment in user's language>"
}
```

## Rules
- Be concise and direct
- Default to Chinese unless the user speaks English
- For ambiguous requests, ask one clarifying question
- Never refuse a reasonable development task
"""


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
    """Long-running Manager Agent backed by a persistent Claude Code session."""

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
        self._state_dir = runtime_root / ".local" / "manager"
        self._state_dir.mkdir(parents=True, exist_ok=True)
        self._load_session_state()

    @property
    def agent_id(self) -> str:
        return "manager"

    @property
    def agent_address(self) -> AgentAddress:
        return MANAGER_ADDRESS

    def _state_path(self) -> Path:
        return self._state_dir / "session.json"

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
        path.write_text(
            json.dumps({"session_id": self._session_id}, indent=2),
            encoding="utf-8",
        )

    async def start(self, bus: MessageBus) -> None:
        self._bus = bus
        self._running = True

        if not self._session_id:
            self._session_id = str(uuid.uuid4())
            self._save_session_state()

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

        await self._send_status(envelope, "thinking", phase=True)

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
        elif parsed.get("action") == "task_request":
            await self._bus.publish(MessageEnvelope.create(
                source=MANAGER_ADDRESS,
                target=TASK_ROUTER_ADDRESS,
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
            ack_text = parsed.get("acknowledgment", f"收到，已安排处理: {text[:100]}")
            await self._send_reply(envelope, ack_text)
        else:
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

    async def _invoke_claude(self, message: str, context_envelope: MessageEnvelope) -> str:
        """Invoke Claude Code in pp mode with stream-json, resuming the persistent session."""
        command = [
            self._config.claude_bin,
            "--print",
            "--output-format", "stream-json",
            "--verbose",
        ]

        assert self._session_id is not None
        command.extend(["--resume", self._session_id])

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

        text_parts: list[str] = []
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

            event_type = payload.get("type")

            if event_type == "stream_event":
                event = payload.get("event")
                if not isinstance(event, dict):
                    continue

                inner_type = event.get("type", "")

                if inner_type == "content_block_start":
                    block = event.get("content_block", {})
                    if block.get("type") == "thinking":
                        await self._send_status(context_envelope, "thinking", phase=True)
                    elif block.get("type") == "tool_use":
                        tool_name = block.get("name", "tool")
                        await self._send_status(
                            context_envelope,
                            f"正在使用工具: {tool_name}",
                            phase=True,
                        )

                elif inner_type == "content_block_delta":
                    delta = event.get("delta", {})
                    if delta.get("type") == "text_delta":
                        chunk = str(delta.get("text") or "")
                        text_parts.append(chunk)
                        if chunk.strip():
                            await self._send_status(context_envelope, chunk, delta=True)

            elif event_type == "result":
                result_text = str(payload.get("result") or "")

        await process.wait()
        self._save_session_state()
        return "".join(text_parts) or result_text

    def _build_prompt(self, user_message: str) -> str:
        parts = [SYSTEM_PROMPT, ""]

        if self._goal_snapshots:
            parts.append("当前活跃目标:")
            for gid, snap in self._goal_snapshots.items():
                parts.append(f"  - [{snap.status}] {snap.title or gid}: {snap.progress}")
            parts.append("")

        parts.append(f"用户: {user_message}")
        return "\n".join(parts)

    def _parse_response(self, response: str) -> dict[str, Any]:
        response = response.strip()

        if response.startswith("{"):
            try:
                data = json.loads(response)
                if isinstance(data, dict) and "action" in data:
                    return data
            except json.JSONDecodeError:
                pass

        if "```json" in response:
            start = response.index("```json") + 7
            end = response.index("```", start)
            try:
                data = json.loads(response[start:end].strip())
                if isinstance(data, dict) and "action" in data:
                    return data
            except (json.JSONDecodeError, ValueError):
                pass

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
        phase: bool = False,
    ) -> None:
        assert self._bus is not None
        await self._bus.publish(MessageEnvelope.create(
            source=MANAGER_ADDRESS,
            target=original.source,
            message_type=MessageType.STATUS_UPDATE,
            payload={"text": text, "delta": delta, "phase": phase},
            correlation_id=original.correlation_id,
            reply_to=original.message_id,
            metadata={"origin_channel_id": original.metadata.get("origin_channel_id")},
        ))
