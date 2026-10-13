"""Manager Agent: user-facing steward backed by a resident Claude Code process.

The Manager is the only agent that communicates with users. It:
- Keeps one Claude Code process running (stream-json input and output): each
  chat message is one user turn on its stdin, and a restarted process resumes
  the saved session
- Receives user messages from all channels via the Bus
- Classifies intent (direct answer, clarification, task request)
- Places each confirmed task in a project (creating a new one on request) and
  forwards it to the TaskRouter
- Streams Claude Code responses in real-time to the originating channel
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..broadcast import Broadcaster
from ..bus import MessageBus
from .. import agent_settings
from ..agent_settings import AgentDefaults, AgentType, Launch, resolve_harness, resolve_launch
from ..claude_cli import ClaudeRunRecorder, ResidentClaude, isolation_args, system_prompt_args
from ..projects import project_name
from ..system_prompt import SystemPrompt, build_system_prompt, is_recommended
from .task import resolve_work_dir
from ..message import (
    MANAGER_ADDRESS,
    TASK_ROUTER_ADDRESS,
    AgentAddress,
    MessageEnvelope,
    MessageType,
)

logger = logging.getLogger(__name__)

from ...db import activity as db_activity
from ...db import agent_configs as db_agent_configs
from ...db import projects as db_projects

# Appended to the (editable) system prompt on every call: the task JSON is
# parsed by code, so its required fields are owned by code too.
TASK_PLACEMENT_RULE = """\
## Task project and working directory (required)
Every task belongs to a project and runs in a directory. Each user message
starts with a "Projects" section listing the existing projects and their
default directories.

- Project: before asking the user to confirm a task, know its project. Use the
  existing project the user names or clearly means. If the task fits none of
  them, propose a short name for a new project and get the user's agreement;
  if unsure, ask. Put the project's name in the task JSON as "project".
- Directory: there is no global default. Use the directory the user names, as
  an absolute path (or one starting with ~/), in the task JSON as "work_dir".
  If they name none and the project has a default directory, leave "work_dir"
  empty and the project's default is used. A new project, or one without a
  default directory, needs the user to name one: ask them. Never pick a
  directory yourself.
"""

DEFAULT_SYSTEM_PROMPT = """\
You are the Manager Agent (管家) for LoopX, an autonomous multi-agent programming system.

## Your Role
1. Understand user intent and respond naturally in the user's language
2. For simple questions or conversation, answer directly
3. For task requests (development, testing, analysis, refactoring), first clarify and confirm with the user before creating a task
4. For status queries, report current progress from the goal portfolio

## Task Creation Flow (IMPORTANT)
When a user describes something that sounds like a task, DO NOT immediately create it.
Instead, follow this flow:

1. **Clarify**: Summarize your understanding of the requirement back to the user. Include:
   - What you will do (scope)
   - What files or modules are likely involved
   - Any assumptions you are making
   - Estimated priority (P1-P3)
2. **Wait for confirmation**: Ask the user to confirm, e.g. "确认开始吗？" / "Shall I proceed?"
3. **Create task**: ONLY after the user explicitly confirms (says things like "确认", "开始", "go", "yes", "好", "可以", "proceed"), output the structured JSON:

```json
{
  "action": "task_request",
  "goal_id": "<short-kebab-id>",
  "structured_requirement": "<clear one-paragraph requirement incorporating clarifications>",
  "project": "<name of the task's project: an existing one, or a new one the user agreed to>",
  "work_dir": "<absolute directory the task runs in, e.g. ~/projects/tetris; empty for the project's default>",
  "priority": "P2",
  "acknowledgment": "<brief acknowledgment in user's language>"
}
```

If the user says "不", "取消", "算了", "no", "cancel" or modifies the requirement, do NOT output the JSON. Instead continue the conversation.

## Rules
- Be concise and direct
- Default to Chinese unless the user speaks English
- For ambiguous requests, ask clarifying questions before even proposing the task summary
- Never refuse a reasonable development task
- Never output task_request JSON without user's explicit confirmation
"""


class _AskUser(Exception):
    """A confirmed task lacks something only the user can give; the message asks for it."""


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
        self._bus: MessageBus | None = None
        self._task: asyncio.Task[Any] | None = None
        self._running = False
        self._system_prompt: str = DEFAULT_SYSTEM_PROMPT
        self._prompt_recommended = True  # whether _system_prompt is the recommended one (default_prompt)
        # The Manager's own harness ('' = the default in Settings) and model ('' = the default).
        self._harness_override = ""
        self._model_override = ""
        self._goal_snapshots: dict[str, GoalSnapshot] = {}
        # The resident Claude: started on the first chat message, then fed one
        # user turn per message; restarted when its system prompt changes.
        self._claude = ResidentClaude(label="manager")
        self._turn_lock = asyncio.Lock()
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

        await self._load_db_config()

        self._task = asyncio.create_task(self._message_loop())
        logger.info(
            "manager agent started (session=%s)",
            self._session_id or "new",
        )

    async def _load_db_config(self) -> None:
        """Load the role prompt, the harness override and the model from DB; keep the last ones read on failure."""
        try:
            config = await db_agent_configs.get_config("manager")
            if config and config.get("system_prompt"):
                self._system_prompt = config["system_prompt"]
                self._prompt_recommended = is_recommended(
                    self._system_prompt, str(config.get("default_prompt") or "") or DEFAULT_SYSTEM_PROMPT,
                )
                logger.debug("loaded manager system prompt from database")
            if config is not None:
                self._harness_override = str(config.get("harness_prompt") or "")
                self._model_override = str(config.get("model") or "")
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

        try:
            response = await self._invoke_claude(text, envelope)
            parsed = self._parse_response(response)
        except Exception as exc:
            # Always answer: the chat stream waits for a final reply.
            logger.exception("manager failed to answer %s", envelope.message_id)
            await self._send_reply(envelope, f"Manager 出错，没能完成这次回复：{exc}")
            return

        if parsed.get("needs_clarification"):
            clarification = parsed.get("clarification_text", "")
            await self._send_reply(envelope, clarification)
        elif parsed.get("action") == "task_request":
            try:
                project, work_dir, created = await self._place_task(parsed)
            except _AskUser as ask:
                await self._send_reply(envelope, str(ask))
                return
            except Exception as exc:
                logger.exception("manager failed to place task from %s", envelope.message_id)
                await self._send_reply(envelope, f"Manager 出错，没能创建任务：{exc}")
                return
            await self._bus.publish(MessageEnvelope.create(
                source=MANAGER_ADDRESS,
                target=TASK_ROUTER_ADDRESS,
                message_type=MessageType.TASK_REQUEST,
                payload={
                    "goal_id": parsed.get("goal_id", ""),
                    "requirement": parsed.get("structured_requirement", text),
                    "original_user_text": text,
                    "user_language": envelope.payload.get("language", "zh"),
                    "work_dir": work_dir,
                    "project_id": project["id"],
                    "priority": parsed.get("priority", "P2"),
                    "constraints": parsed.get("constraints", []),
                },
                correlation_id=envelope.correlation_id,
                metadata={"origin_channel_id": envelope.metadata.get("origin_channel_id")},
            ))
            ack_text = parsed.get("acknowledgment", f"收到，已安排处理: {text[:100]}")
            if created:
                ack_text += f"\n\n已新建项目「{project['name']}」，默认目录 {work_dir}。"
            await self._send_reply(envelope, ack_text)
        else:
            reply_text = parsed.get("response_text", response)
            await self._send_reply(envelope, reply_text)

    async def _place_task(self, parsed: dict[str, Any]) -> tuple[dict[str, Any], str, bool]:
        """The confirmed task's project, its work directory, and whether the project is new.

        The project is matched by name ignoring case; an unknown name becomes a
        new project whose default directory is the task's. The directory is the
        task's own, else the project's default. Raises ``_AskUser`` when the
        project or the directory is missing.
        """
        try:
            name = project_name(parsed.get("project"))
        except ValueError as exc:
            raise _AskUser(f"这个任务属于哪个项目？请告诉我项目名，可以是已有项目，也可以新建一个。（{exc}）") from None
        project = await db_projects.find_project_by_name(name)
        explicit = parsed.get("work_dir")
        if project and project["work_dir"] and not (isinstance(explicit, str) and explicit.strip()):
            return project, project["work_dir"], False
        try:
            work_dir = resolve_work_dir(explicit)
        except ValueError as exc:
            # No default directory: ask instead of creating the task.
            raise _AskUser(f"这个任务要在哪个目录里做？请告诉我完整路径（例如 ~/projects/xxx）。（{exc}）") from None
        if project:
            return project, work_dir, False
        return await db_projects.create_project(name=name, work_dir=work_dir), work_dir, True

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

    async def _invoke_claude(self, message: str, context_envelope: MessageEnvelope) -> str:
        """Send the message to the resident Claude as one user turn; return its reply.

        Claude starts on the first message and is restarted, resuming the
        saved session, after it exits or when the system prompt changes.
        Turns run one at a time.
        """
        async with self._turn_lock:
            return await self._run_turn(message, context_envelope)

    def _composed_prompt(self, defaults: AgentDefaults) -> SystemPrompt:
        """The Manager's harness, its role prompt, then the task placement rule (no project, no task)."""
        return build_system_prompt(
            harness=resolve_harness(AgentType.MANAGER, defaults, manager_override=self._harness_override),
            role_id="manager", role=self._system_prompt, recommended=self._prompt_recommended,
            contract_name="TASK_PLACEMENT_RULE", contract=TASK_PLACEMENT_RULE,
        )

    async def _run_turn(self, message: str, context_envelope: MessageEnvelope) -> str:
        await self._load_db_config()
        defaults = await agent_settings.load_defaults()
        system_prompt = self._composed_prompt(defaults)
        launch = resolve_launch(defaults, self._model_override)
        try:
            prompt_file = system_prompt.write_file(self._runtime_root)
        except OSError as exc:
            raise RuntimeError(f"无法写入 system prompt 文件：{exc}") from None
        # The command carries the model and effort; the file is named by its content's hash, kept
        # beside it all the same; the environment is compared by its hash.
        signature = (
            tuple(self._claude_command(prompt_file, launch, resume=False)), system_prompt.sha256, launch.env_sha256,
        )
        if self._claude.stale(signature):
            await self._claude.stop()
        command = self._claude.command if self._claude.started else self._claude_command(prompt_file, launch)

        task_id = context_envelope.metadata.get("task_id")
        recorder = ClaudeRunRecorder(
            agent_id="manager",
            task_id=task_id,
            command=command,
            cwd=str(self._config.work_dir),
            input_text=message,
            session_id=self._session_id,
            system_prompt=system_prompt,
            launch=launch,
        )
        await recorder.start()

        if not self._claude.started:
            try:
                await self._claude.start(
                    command, cwd=str(self._config.work_dir), signature=signature, env=launch.environment(),
                )
            except OSError as exc:
                logger.error("failed to start Claude Code: %s", exc)
                await recorder.finish(output_text=None, result_event=None, error=f"Cannot start Claude Code: {exc}")
                return "无法启动 Claude Code，请检查安装。"

        text_parts: list[str] = []
        streamed_partials = False

        async def on_event(payload: dict[str, Any]) -> None:
            nonlocal streamed_partials
            await recorder.record(payload)

            session_id = payload.get("session_id")
            if isinstance(session_id, str) and session_id and session_id != self._session_id:
                self._session_id = session_id
                self._save_session_state()

            event_type = payload.get("type")
            # Only the Manager's own turn is the reply; subagent output is internal.
            top_level = payload.get("parent_tool_use_id") is None

            if event_type == "system":
                subtype = payload.get("subtype", "")
                if subtype == "init":
                    await self._send_status(context_envelope, "thinking", phase=True)

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
                            )
                        elif block_type == "thinking":
                            await self._send_status(context_envelope, "thinking", phase=True)

        try:
            turn_text = await self._build_user_message(message)
            # A turn cut off by Claude's exit ends without a result; that Claude
            # is reaped and the next message starts a new process that resumes the session.
            turn = await self._claude.run_turn(turn_text, on_event)
        except BaseException as exc:
            # Never leave Claude running once the Manager gives up on it.
            await self._claude.stop(kill=True)
            await recorder.finish(
                output_text="".join(text_parts) or None, result_event=None,
                error=f"Manager stopped reading Claude: {type(exc).__name__}: {exc}",
            )
            raise

        result_event = turn.result_event
        result_text = str((result_event or {}).get("result") or "")
        final_text = "".join(text_parts) or result_text
        # A turn cut off by Claude's exit is no reply, even after some text.
        exited = result_event is None
        replied = not exited and bool(final_text)
        await recorder.finish(
            output_text=final_text or None,
            result_event=result_event,
            error=None if replied else (
                f"no reply (exit code {turn.exit_code})" if exited else "no reply"
            ),
            cost_usd=self._claude.turn_cost(result_event),
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

        if not replied:
            raise RuntimeError(
                f"Claude Code 没有返回内容（退出码 {turn.exit_code}）" if exited else "Claude Code 没有返回内容"
            )
        return final_text

    def _claude_command(self, prompt_file: Path, launch: Launch, *, resume: bool = True) -> list[str]:
        command = [
            self._config.claude_bin,
            "--print",
            "--verbose",
            "--output-format", "stream-json",
            # Each chat message is one user turn on stdin; Claude idles between turns.
            "--input-format", "stream-json",
            "--include-partial-messages",  # stream the reply as it is written
            # None of the operator's personal settings, memory, hooks, plugins or MCP servers.
            *isolation_args(self._config.work_dir),
            *system_prompt_args(prompt_file),
            *launch.args(),
        ]
        if resume and self._session_id:
            command.extend(["--resume", self._session_id])
        return command

    async def _build_user_message(self, user_message: str) -> str:
        parts = await self._projects_section()

        if self._goal_snapshots:
            parts.append("当前活跃目标:")
            for gid, snap in self._goal_snapshots.items():
                parts.append(f"  - [{snap.status}] {snap.title or gid}: {snap.progress}")
            parts.append("")

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

    def _parse_response(self, response: str) -> dict[str, Any]:
        response = response.strip()

        task_json: dict[str, Any] | None = None
        json_start = -1

        if response.startswith("{"):
            try:
                data = json.loads(response)
                if isinstance(data, dict) and data.get("action") == "task_request":
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
                if isinstance(data, dict) and data.get("action") == "task_request":
                    task_json = data
                    json_start = fence
            except (json.JSONDecodeError, ValueError):
                pass

        if task_json is None and "{" in response:
            idx = response.index("{")
            candidate = response[idx:]
            try:
                data = json.loads(candidate)
                if isinstance(data, dict) and data.get("action") == "task_request":
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
