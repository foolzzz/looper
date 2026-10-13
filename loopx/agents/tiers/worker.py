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
from ..claude_cli import ClaudeRunRecorder, iter_lines, write_user_turn
from ..message import (
    AgentAddress,
    MessageEnvelope,
    MessageType,
    worker_address,
)

from ...db import activity as db_activity
from ...db import agent_configs as db_agent_configs

logger = logging.getLogger(__name__)

MAX_POOL_SIZE = 4
DEFAULT_RUN_TIMEOUT_SEC = 86400


async def _load_run_limits() -> tuple[int, int]:
    """Return the ``worker`` agent config's time limit (seconds) and max turns.

    Read for every run, so a dashboard edit applies to the next step. Both
    are 0 for no limit. Without a readable row a run may take 86400 s and has
    no turn limit.
    """
    try:
        config = await db_agent_configs.get_config("worker")
    except Exception:
        logger.warning("cannot read the worker agent config; using defaults", exc_info=True)
        config = None
    if config is None:
        return DEFAULT_RUN_TIMEOUT_SEC, 0
    return int(config.get("timeout_seconds") or 0), int(config.get("max_turns") or 0)


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
    work_dir: Path,
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
        return (candidate if candidate.is_absolute() else work_dir / candidate).exists()

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
    sandbox: str = "workspace-write"
    max_workers: int = MAX_POOL_SIZE


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
        # stdin of each running Claude, open until its result: more user turns go here.
        self._inputs: dict[str, asyncio.StreamWriter] = {}

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
        # The pool takes delivery of work sent to any worker-<id> address.
        self._bus.subscribe_topic(MessageType.WORK_ITEM, self._on_work_item_topic, consumes=True)
        self._bus.subscribe_topic(MessageType.VERIFY_REQUEST, self._on_verify_topic, consumes=True)
        self._bus.subscribe_topic(MessageType.WORKER_INPUT, self._on_worker_input_topic, consumes=True)
        self._bus.subscribe_topic(MessageType.KILL_WORK, self._on_kill_work_topic, consumes=True)

        try:
            async for envelope in self._bus.subscribe("worker-pool"):
                if not self._running:
                    break
                await self._dispatch(envelope)
        except asyncio.CancelledError:
            pass

    def _on_work_item_topic(self, envelope: MessageEnvelope) -> bool:
        if self._running and envelope.target.agent_type == "worker":
            asyncio.get_event_loop().create_task(self._dispatch(envelope))
            return True
        return False

    def _on_verify_topic(self, envelope: MessageEnvelope) -> bool:
        return self._on_work_item_topic(envelope)

    def _on_worker_input_topic(self, envelope: MessageEnvelope) -> bool:
        if not self._running:
            return False
        asyncio.get_event_loop().create_task(self._deliver_input(envelope))
        return True

    def _on_kill_work_topic(self, envelope: MessageEnvelope) -> bool:
        if not self._running or envelope.target.agent_type != "worker":
            return False
        asyncio.get_event_loop().create_task(self._deliver_kill(envelope))
        return True

    async def _deliver_kill(self, envelope: MessageEnvelope) -> None:
        """Stop one running worker and tell the sender whether it was running."""
        assert self._bus is not None
        killed = await self.kill(envelope.target.agent_id)
        await self._bus.publish(MessageEnvelope.create(
            source=envelope.target,
            target=envelope.source,
            message_type=MessageType.KILL_WORK_RESULT,
            payload={"killed": killed},
            correlation_id=envelope.correlation_id,
            reply_to=envelope.message_id,
        ))

    async def kill(self, worker_id: str) -> bool:
        """Stop a running worker mid-run. Cancelling its task kills its Claude
        subprocess (``_execute`` kills the process when the read is cancelled)
        and runs the done-callback that frees the slot. Returns whether a
        worker was running; a finished or unknown worker returns False and
        publishes no result for it."""
        task = self._tasks.get(worker_id)
        if task is None or task.done():
            return False
        self._inputs.pop(worker_id, None)
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
        logger.info("worker %s killed", worker_id)
        return True

    async def _deliver_input(self, envelope: MessageEnvelope) -> None:
        """Pass a user turn to a running worker and tell the sender whether it landed."""
        assert self._bus is not None
        delivered = await self.send_input(envelope.target.agent_id, str(envelope.payload.get("text", "")))
        await self._bus.publish(MessageEnvelope.create(
            source=envelope.target,
            target=envelope.source,
            message_type=MessageType.WORKER_INPUT_RESULT,
            payload={"delivered": delivered},
            correlation_id=envelope.correlation_id,
            reply_to=envelope.message_id,
        ))

    def accepts_input(self, worker_id: str) -> bool:
        """True while the worker's Claude run still reads user turns."""
        return worker_id in self._inputs

    async def send_input(self, worker_id: str, text: str) -> bool:
        """Add a user turn to a running worker; Claude reads it at its next turn."""
        stdin = self._inputs.get(worker_id)
        if stdin is None or not text.strip():
            return False
        try:
            await write_user_turn(stdin, text)
        except (BrokenPipeError, ConnectionResetError, RuntimeError):
            self._inputs.pop(worker_id, None)
            return False
        logger.info("worker %s received a user turn (%d chars)", worker_id, len(text))
        return True

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
        except asyncio.TimeoutError as exc:
            wp.status = "failed"
            wp.result = {"status": "failed", "error": str(exc) or "worker timeout"}
            logger.warning("worker %s timed out on task %s: %s", worker_id, task_id, exc)
        except Exception as exc:
            wp.status = "failed"
            wp.result = {"status": "failed", "error": str(exc)}
            logger.exception("worker %s failed on task %s", worker_id, task_id)

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
        work_dir = str(context.get("work_dir") or "")
        # A task runs only in the directory the user chose; there is no default.
        problem = None
        if not work_dir:
            problem = "No work directory for this step; set the task's work directory"
        else:
            try:
                Path(work_dir).mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                problem = f"Cannot create work directory {work_dir}: {exc}"
        if problem:
            logger.error("worker %s: %s", worker_id, problem)
            if is_verify:
                return {"passed": False, "findings": [problem], "summary": problem}
            return {"status": "failed", "error": problem}

        if is_verify:
            prompt = self._build_verify_prompt(payload)
        else:
            prompt = self._build_work_prompt(payload)

        run_timeout, max_turns = await _load_run_limits()
        command = [
            self._config.claude_bin,
            "--print",
            "--verbose",
            "--output-format", "stream-json",
            # Partial deltas keep the run's heartbeat current while Claude thinks.
            "--include-partial-messages",
            # The prompt and any later user turns arrive on stdin, so the
            # Planner can step in while the run is going.
            "--input-format", "stream-json",
            "--replay-user-messages",
            "--dangerously-skip-permissions",
            *(["--max-turns", str(max_turns)] if max_turns > 0 else []),
        ]

        if parent_task_id:
            cmd_display = " ".join(command) + f"  (prompt on stdin, {len(prompt)} chars)"
            try:
                await db_activity.insert_activity(
                    task_id=parent_task_id,
                    worker=worker_id,
                    kind="tool_use",
                    summary=f"$ {cmd_display}",
                    metadata={"cwd": work_dir},
                )
            except Exception:
                pass

        recorder = ClaudeRunRecorder(
            agent_id=worker_id,
            task_id=parent_task_id,
            command=command,
            cwd=work_dir,
            input_text=prompt,
            step_id=payload.get("task_id") or None,
        )
        await recorder.start()

        logger.info("worker %s starting subprocess: %s (cwd=%s)", worker_id, command[0], work_dir)
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                cwd=work_dir,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            logger.info("worker %s subprocess started (pid=%s)", worker_id, process.pid)
            wp = self._active.get(worker_id)
            if wp is not None:
                wp.process = process
        except OSError as exc:
            logger.error("worker %s failed to start subprocess: %s", worker_id, exc)
            error = f"Cannot start Claude Code: {exc}"
            await recorder.finish(output_text=None, result_event=None, error=error)
            return {"status": "failed", "error": error}

        assert process.stdin is not None
        try:
            await write_user_turn(process.stdin, prompt)
            self._inputs[worker_id] = process.stdin
        except (BrokenPipeError, ConnectionResetError):
            # Claude exited before reading; its output decides the outcome.
            logger.warning("worker %s: Claude closed stdin before the prompt was sent", worker_id)

        def _end_input() -> None:
            # Without this Claude would wait on stdin for another user turn.
            if self._inputs.pop(worker_id, None) is not None and not process.stdin.is_closing():
                process.stdin.close()

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
                if not isinstance(event, dict):
                    continue
                await recorder.record(event)

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
                    _end_input()
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
            await asyncio.wait_for(_read_stream(), timeout=run_timeout if run_timeout > 0 else None)
            _end_input()
            await process.wait()
        except BaseException as exc:
            _end_input()
            # Never leave Claude running unsupervised once the worker gives up.
            if process.returncode is None:
                process.kill()
                await process.wait()
            timed_out = isinstance(exc, asyncio.TimeoutError)
            error = (
                f"Claude ran longer than the {run_timeout} s limit (timeout_seconds)" if timed_out
                else f"worker stopped reading Claude: {type(exc).__name__}"
            )
            await recorder.finish(output_text=None, result_event=result_event, error=error)
            if timed_out:
                raise asyncio.TimeoutError(error) from exc
            raise

        final_text = str((result_event or {}).get("result") or "") or None
        return_code = process.returncode or 0
        if return_code != 0:
            stderr = ""
            if process.stderr:
                stderr_bytes = await process.stderr.read()
                stderr = stderr_bytes.decode("utf-8", errors="replace") if stderr_bytes else ""
            error = f"Claude Code exited with code {return_code}"
            await recorder.finish(output_text=final_text, result_event=result_event, error=error)
            return {"status": "failed", "error": error, "stderr": stderr[:500]}

        outcome = judge_result(result_event, work_dir=Path(work_dir), is_verify=is_verify)
        # A review that says "not passed" is still a successful run; only work
        # failures are the run's error.
        error = outcome.get("error") if not is_verify and outcome.get("status") == "failed" else None
        await recorder.finish(output_text=final_text, result_event=result_event, error=error)
        return outcome

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

        parts = ["You are a Reviewer Worker verifying completed work."]
        if payload.get("instruction"):
            parts.append(f"The step's instruction: {payload['instruction']}")
        parts.append(f"Files changed: {json.dumps(changed, ensure_ascii=False)}")
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
