"""Worker tier: subprocess-isolated task execution.

Each Worker runs as a subprocess (Claude Code or Codex) with limited
permissions. The WorkerPool manages lifecycle, concurrency, and result
collection, reporting back to the Planner via the Bus.
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
from ..claude_cli import iter_lines
from ..message import (
    AgentAddress,
    MessageEnvelope,
    MessageType,
    worker_address,
)

from ...db import activity as db_activity
from ...db import llm_records as db_llm_records

logger = logging.getLogger(__name__)

MAX_POOL_SIZE = 4
WORKER_TIMEOUT_SEC = 600


def extract_report(text: str, key: str) -> dict[str, Any] | None:
    """Return the last top-level JSON object in ``text`` that has ``key``.

    Handles nested objects and markdown fences, which a flat regex cannot.
    """
    decoder = json.JSONDecoder()
    found: dict[str, Any] | None = None
    i = text.find("{")
    while i != -1:
        try:
            obj, end = decoder.raw_decode(text, i)
        except json.JSONDecodeError:
            i = text.find("{", i + 1)
            continue
        if isinstance(obj, dict) and key in obj:
            found = obj
        i = text.find("{", end)
    return found


def _work_failed(error: str, **extra: Any) -> dict[str, Any]:
    return {"status": "failed", "error": error, "summary": extra.pop("summary", error), **extra}


def _verify_failed(finding: str) -> dict[str, Any]:
    return {"passed": False, "findings": [finding], "summary": finding}


def judge_result(
    result_event: dict[str, Any] | None,
    *,
    project_root: Path,
    is_verify: bool,
) -> dict[str, Any]:
    """Decide a step's outcome from Claude's stream-json ``result`` event.

    Fails closed: the run must succeed, the final message must carry the JSON
    report, and a "completed" work report must match the files on disk.
    """
    failed = _verify_failed if is_verify else _work_failed
    if result_event is None:
        return failed("Claude Code ended without a result event")
    if result_event.get("is_error") or result_event.get("subtype") != "success":
        subtype = result_event.get("subtype") or "error"
        return failed(f"Claude Code run did not succeed ({subtype})")
    text = str(result_event.get("result") or "")
    if is_verify:
        verdict = extract_report(text, "passed")
        if verdict is None or not isinstance(verdict.get("passed"), bool):
            return _verify_failed("Reviewer did not end with the JSON verdict")
        findings = verdict.get("findings", [])
        return {
            "passed": verdict["passed"],
            "findings": findings if isinstance(findings, list) else [str(findings)],
            "summary": str(verdict.get("summary") or ""),
        }

    report = extract_report(text, "status")
    if report is None:
        # Keep the tail of what Claude said so the failure can be diagnosed.
        return _work_failed("Worker did not end with the JSON status report", final_message=text[-500:])
    status = report.get("status")
    changed = report.get("changed_files", {})
    deleted = report.get("deleted_files", [])
    summary = str(report.get("summary") or "")
    if status not in ("completed", "failed"):
        return _work_failed(f"Worker reported an unknown status {status!r}")
    if not isinstance(changed, dict) or not isinstance(deleted, list):
        return _work_failed("Worker report has malformed changed_files or deleted_files")
    if status == "failed":
        return _work_failed(summary or "Worker reported failure", summary=summary, changed_files=changed)

    def on_disk(path: Any) -> bool:
        candidate = Path(str(path))
        return (candidate if candidate.is_absolute() else project_root / candidate).exists()

    missing = [str(path) for path in changed if not on_disk(path)]
    still_present = [str(path) for path in deleted if on_disk(path)]
    if missing or still_present:
        problems = []
        if missing:
            problems.append(f"changed files not found: {', '.join(missing)}")
        if still_present:
            problems.append(f"deleted files still present: {', '.join(still_present)}")
        return _work_failed("Worker report does not match the files: " + "; ".join(problems))
    return {
        "status": "completed",
        "changed_files": changed,
        "deleted_files": deleted,
        "summary": summary,
    }


@dataclass
class WorkerConfig:
    claude_bin: str = "claude"
    work_dir: Path = field(default_factory=lambda: Path.cwd())
    sandbox: str = "workspace-write"
    max_workers: int = MAX_POOL_SIZE
    timeout_sec: float = WORKER_TIMEOUT_SEC


@dataclass
class WorkerProcess:
    """Tracks a single Worker subprocess."""

    worker_id: str
    task_id: str
    plan_id: str
    process: asyncio.subprocess.Process | None = None
    status: str = "pending"  # "pending" | "running" | "completed" | "failed"
    result: dict[str, Any] | None = None


class WorkerPool:
    """Manages a pool of Worker subprocesses.

    Each Worker receives a WORK_ITEM or VERIFY_REQUEST, runs a Claude Code
    subprocess to do the work, and publishes the result back to the Planner.
    """

    def __init__(self, *, config: WorkerConfig, runtime_root: Path) -> None:
        self._config = config
        self._runtime_root = runtime_root
        self._bus: MessageBus | None = None
        self._active: dict[str, WorkerProcess] = {}
        self._semaphore = asyncio.Semaphore(config.max_workers)
        self._tasks: dict[str, asyncio.Task[Any]] = {}
        self._listener_task: asyncio.Task[Any] | None = None
        self._running = False
        self._workers_dir = runtime_root / ".local" / "workers"
        self._workers_dir.mkdir(parents=True, exist_ok=True)

    @property
    def agent_id(self) -> str:
        return "worker-pool"

    @property
    def agent_address(self) -> AgentAddress:
        return AgentAddress(agent_type="worker", agent_id="worker-pool")

    async def start(self, bus: MessageBus) -> None:
        self._bus = bus
        self._running = True
        self._listener_task = asyncio.create_task(self._listen_loop())
        logger.info("worker pool started (max %d concurrent)", self._config.max_workers)

    async def stop(self) -> None:
        self._running = False
        if self._listener_task:
            self._listener_task.cancel()
            try:
                await self._listener_task
            except asyncio.CancelledError:
                pass

        for task in list(self._tasks.values()):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks.values(), return_exceptions=True)
        self._tasks.clear()

        for wp in self._active.values():
            if wp.process and wp.process.returncode is None:
                wp.process.kill()
        self._active.clear()

    def is_healthy(self) -> bool:
        return self._running and bool(shutil.which(self._config.claude_bin))

    async def _listen_loop(self) -> None:
        """Listen for work items addressed to any worker-* queue.

        The Planner publishes WORK_ITEM and VERIFY_REQUEST messages targeting
        specific worker IDs (e.g. worker-abc123). We listen on "worker-pool"
        as a catch-all, but also need to pick up messages to dynamically
        created worker IDs. The Bus auto-creates queues, so we subscribe
        once to a well-known pool queue and also sniff the bus for new
        worker-targeted messages via topic subscription.
        """
        assert self._bus is not None
        self._bus.subscribe_topic(MessageType.WORK_ITEM, self._on_work_item_topic)
        self._bus.subscribe_topic(MessageType.VERIFY_REQUEST, self._on_verify_topic)

        try:
            async for envelope in self._bus.subscribe("worker-pool"):
                if not self._running:
                    break
                await self._dispatch(envelope)
        except asyncio.CancelledError:
            pass

    def _on_work_item_topic(self, envelope: MessageEnvelope) -> None:
        if self._running and envelope.target.agent_type == "worker":
            asyncio.get_event_loop().create_task(self._dispatch(envelope))

    def _on_verify_topic(self, envelope: MessageEnvelope) -> None:
        if self._running and envelope.target.agent_type == "worker":
            asyncio.get_event_loop().create_task(self._dispatch(envelope))

    async def _dispatch(self, envelope: MessageEnvelope) -> None:
        worker_id = envelope.target.agent_id
        if worker_id in self._active:
            logger.debug("worker %s already active, skipping", worker_id)
            return

        logger.info("dispatching worker %s (active=%d)", worker_id, len(self._active))
        await self._semaphore.acquire()
        task = asyncio.create_task(self._run_worker(envelope))
        self._tasks[worker_id] = task
        task.add_done_callback(lambda t: self._cleanup_worker(worker_id))

    def _cleanup_worker(self, worker_id: str) -> None:
        self._tasks.pop(worker_id, None)
        self._active.pop(worker_id, None)
        self._semaphore.release()

    async def _run_worker(self, envelope: MessageEnvelope) -> None:
        assert self._bus is not None
        worker_id = envelope.target.agent_id
        payload = envelope.payload
        task_id = payload.get("task_id", "")
        plan_id = payload.get("plan_id", "")

        wp = WorkerProcess(
            worker_id=worker_id,
            task_id=task_id,
            plan_id=plan_id,
            status="running",
        )
        self._active[worker_id] = wp

        is_verify = envelope.message_type == MessageType.VERIFY_REQUEST
        prompt = self._build_work_prompt(payload) if not is_verify else str(payload)
        t0 = __import__("time").monotonic()

        parent_task_id = payload.get("parent_task_id", "")
        if parent_task_id:
            try:
                label = "Reviewer" if is_verify else "Worker"
                await db_activity.insert_activity(
                    task_id=parent_task_id,
                    worker=worker_id,
                    kind="phase",
                    summary=f"{label} started execution",
                )
            except Exception:
                pass

        try:
            result = await self._execute(envelope, is_verify=is_verify)
            wp.status = "completed"
            wp.result = result
        except asyncio.TimeoutError:
            wp.status = "failed"
            wp.result = {"status": "failed", "error": "worker timeout"}
            logger.warning("worker %s timed out on task %s", worker_id, task_id)
        except Exception as exc:
            wp.status = "failed"
            wp.result = {"status": "failed", "error": str(exc)}
            logger.exception("worker %s failed on task %s", worker_id, task_id)

        elapsed_ms = int((__import__("time").monotonic() - t0) * 1000)
        output_text = (wp.result or {}).get("summary") or (wp.result or {}).get("error") or ""
        try:
            await db_llm_records.insert_record(
                agent_id=worker_id,
                task_id=payload.get("parent_task_id") or task_id,
                session_id=worker_id,
                input_summary=prompt[:500],
                output_summary=output_text[:500] if output_text else None,
                input_text=prompt,
                output_text=output_text or None,
                duration_ms=elapsed_ms,
                error=wp.result.get("error") if wp.result else None,
            )
        except Exception:
            logger.debug("failed to record worker llm invocation", exc_info=True)

        result_type = MessageType.VERIFY_RESULT if is_verify else MessageType.WORK_RESULT
        await self._bus.publish(MessageEnvelope.create(
            source=worker_address(worker_id),
            # Reply to the dispatching per-task Planner (planner-<task_id>).
            target=envelope.source,
            message_type=result_type,
            payload={
                "task_id": task_id,
                "plan_id": plan_id,
                **(wp.result or {}),
            },
            correlation_id=envelope.correlation_id,
            metadata=envelope.metadata,
        ))

    async def _execute(
        self,
        envelope: MessageEnvelope,
        *,
        is_verify: bool = False,
    ) -> dict[str, Any]:
        """Run Claude Code as a subprocess with streaming progress."""
        payload = envelope.payload
        worker_id = envelope.target.agent_id
        parent_task_id = payload.get("parent_task_id", "")
        context = payload.get("context", {})
        project_root = context.get("project_root", str(self._config.work_dir))

        if is_verify:
            prompt = self._build_verify_prompt(payload)
        else:
            prompt = self._build_work_prompt(payload)

        command = [
            self._config.claude_bin,
            "--print",
            "--verbose",
            "--output-format", "stream-json",
            "--dangerously-skip-permissions",
            "--max-turns", "50",
            prompt,
        ]

        if parent_task_id:
            cmd_display = " ".join(command[:7]) + f" '<prompt {len(prompt)} chars>'"
            try:
                await db_activity.insert_activity(
                    task_id=parent_task_id,
                    worker=worker_id,
                    kind="tool_use",
                    summary=f"$ {cmd_display}",
                    metadata={"cwd": project_root},
                )
            except Exception:
                pass

        logger.info("worker %s starting subprocess: %s (cwd=%s)", worker_id, command[0], project_root)
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                cwd=project_root,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            logger.info("worker %s subprocess started (pid=%s)", worker_id, process.pid)
        except OSError as exc:
            logger.error("worker %s failed to start subprocess: %s", worker_id, exc)
            return {"status": "failed", "error": f"Cannot start Claude Code: {exc}"}

        result_event: dict[str, Any] | None = None

        async def _read_stream() -> None:
            nonlocal result_event
            assert process.stdout is not None
            async for raw_line in iter_lines(process.stdout):
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue

                event_type = event.get("type", "")

                if event_type == "assistant":
                    msg = event.get("message", {})
                    content_blocks = msg.get("content", [])
                    if not isinstance(content_blocks, list):
                        content_blocks = []
                    for block in content_blocks:
                        block_type = block.get("type", "")
                        if block_type == "tool_use":
                            tool_name = block.get("name", "unknown")
                            tool_input = block.get("input", {})
                            summary = self._format_tool_summary(tool_name, tool_input)
                            if parent_task_id:
                                try:
                                    await db_activity.insert_activity(
                                        task_id=parent_task_id,
                                        worker=worker_id,
                                        kind="tool_use",
                                        summary=summary,
                                    )
                                except Exception:
                                    logger.debug("failed to insert tool_use activity", exc_info=True)
                        elif block_type == "text":
                            text = block.get("text", "")
                            if text:
                                if parent_task_id and len(text.strip()) > 5:
                                    try:
                                        await db_activity.insert_activity(
                                            task_id=parent_task_id,
                                            worker=worker_id,
                                            kind="text",
                                            summary=text[:300],
                                        )
                                    except Exception:
                                        logger.debug("failed to insert text activity", exc_info=True)

                elif event_type == "result":
                    result_event = event
                    cost = event.get("total_cost_usd", event.get("cost_usd"))
                    duration_ms = event.get("duration_ms")
                    if parent_task_id:
                        try:
                            parts = ["Worker finished"]
                            if cost is not None:
                                parts[0] = f"Worker finished (cost: ${cost:.4f})"
                            meta: dict[str, Any] = {}
                            if cost is not None:
                                meta["cost_usd"] = cost
                            if duration_ms is not None:
                                meta["duration_ms"] = duration_ms
                            await db_activity.insert_activity(
                                task_id=parent_task_id,
                                worker=worker_id,
                                kind="phase",
                                summary=parts[0],
                                metadata=meta or None,
                            )
                        except Exception:
                            logger.debug("failed to insert result activity", exc_info=True)

        try:
            await asyncio.wait_for(_read_stream(), timeout=self._config.timeout_sec)
            await process.wait()
        except BaseException:
            # Never leave Claude running unsupervised once the worker gives up.
            if process.returncode is None:
                process.kill()
                await process.wait()
            raise

        return_code = process.returncode or 0
        if return_code != 0:
            stderr = ""
            if process.stderr:
                stderr_bytes = await process.stderr.read()
                stderr = stderr_bytes.decode("utf-8", errors="replace") if stderr_bytes else ""
            return {
                "status": "failed",
                "error": f"Claude Code exited with code {return_code}",
                "stderr": stderr[:500],
            }

        return judge_result(result_event, project_root=Path(project_root), is_verify=is_verify)

    def _build_work_prompt(self, payload: dict[str, Any]) -> str:
        instruction = payload.get("instruction", "")
        target_files = payload.get("target_files", [])
        validation_cmds = payload.get("validation_commands", [])
        acceptance = payload.get("acceptance_criteria", [])

        parts = [
            "You are a Worker Agent executing a specific task.",
            f"Task: {instruction}",
        ]
        if target_files:
            parts.append(f"Target files: {', '.join(target_files)}")
        if acceptance:
            parts.append("Acceptance criteria:")
            for c in acceptance:
                parts.append(f"  - {c}")
        if validation_cmds:
            parts.append("After making changes, run these validation commands:")
            for cmd in validation_cmds:
                parts.append(f"  $ {cmd}")
        parts.extend([
            "",
            "Execute the task. End your final message with exactly one JSON object:",
            '{"status": "completed", "changed_files": {"path": "description"}, '
            '"deleted_files": ["path"], "summary": "..."}',
            'Use "status": "failed" if any part of the task was not done. List every '
            "file you created or modified in changed_files and every file you deleted "
            "in deleted_files, with paths relative to the working directory. The "
            "report is checked against the files on disk.",
        ])
        return "\n".join(parts)

    def _build_verify_prompt(self, payload: dict[str, Any]) -> str:
        work_result = payload.get("work_result", {})
        acceptance = payload.get("acceptance_criteria", [])
        changed = work_result.get("changed_files", {})

        parts = [
            "You are a Reviewer Worker verifying completed work.",
            f"Files changed: {json.dumps(changed, ensure_ascii=False)}",
        ]
        if acceptance:
            parts.append("Acceptance criteria to verify:")
            for c in acceptance:
                parts.append(f"  - {c}")
        parts.extend([
            "",
            "Review the changes. End your final message with exactly one JSON object:",
            '{"passed": true/false, "findings": ["issue1", ...], "summary": "..."}',
        ])
        return "\n".join(parts)

    @staticmethod
    def _format_tool_summary(tool_name: str, tool_input: dict[str, Any]) -> str:
        if tool_name == "Read":
            return f"Reading: {tool_input.get('file_path', '?')}"
        if tool_name == "Edit":
            return f"Editing: {tool_input.get('file_path', '?')}"
        if tool_name == "Write":
            return f"Writing: {tool_input.get('file_path', '?')}"
        if tool_name == "Bash":
            cmd = tool_input.get("command", "")
            return f"Running: {cmd[:80]}"
        if tool_name in ("Glob", "Grep"):
            pattern = tool_input.get("pattern", tool_input.get("query", ""))
            return f"{tool_name}: {pattern[:60]}"
        return f"Tool: {tool_name}"

    def active_count(self) -> int:
        return len(self._active)

    def status(self) -> dict[str, Any]:
        return {
            "pool_size": self._config.max_workers,
            "active": self.active_count(),
            "workers": {
                wid: {"task_id": wp.task_id, "status": wp.status}
                for wid, wp in self._active.items()
            },
        }
