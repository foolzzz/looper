"""Worker tier: subprocess-isolated task execution.

Each Worker runs as a subprocess (Claude Code, Codex or OMP) with limited
permissions. The WorkerPool manages lifecycle, concurrency, and result
collection, reporting back to the Planner via the Bus. A run starts once
fewer Workers (or Reviewers) run in its task's project than that role's
concurrency limit there (``agent_settings.load_concurrency``, read each time
it checks); until then it waits, and it checks again whenever a run ends or a
limit changes (``recheck``).

A Worker's or Reviewer's system prompt is the harness (its project's Worker
harness, else the default in Settings), its role prompt (the ``worker`` or
``reviewer`` row), the task's project background and knowledge index, the
task section (the step, with the task it belongs to) and the code-owned
report contract, last (``system_prompt``). The first user turn on stdin
carries only what other agents produced: the reports of the steps this one
depends on, or the Worker's report under review. Each run reads its row, the
Settings defaults (model, effort, environment), its project's knowledge (the
documents' directories are mounted with ``--add-dir``), the catalog items
installed for its role, globally or in its project (each plugin and generated
skill plugin a ``--plugin-dir``) and its project's execution settings
(``project_execution``: the model and ``--max-budget-usd``) as it starts.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..agent_settings import AgentType, Launch, load_agent_settings, load_concurrency
from ..bus import MessageBus
from ..claude_cli import ClaudeRunRecorder, budget_error
from .. import proc_tree
from ..run_usage import run_usage
from ..knowledge import documents_label, load_knowledge
from ..library import items_label, load_installed
from ..runtimes import RuntimeAdapter
from ..message import (
    AgentAddress,
    MessageEnvelope,
    MessageType,
    worker_address,
)
from . import journal
from ..system_prompt import (
    REVIEW_CONTRACT,
    WORK_CONTRACT,
    build_system_prompt,
    is_recommended,
    review_task_section,
    work_task_section,
)

from ...db import activity as db_activity
from ...db import role_configs as db_role_configs
from ...db import projects as db_projects

logger = logging.getLogger(__name__)

DEFAULT_RUN_TIMEOUT_SEC = 86400

# The Worker's role prompt when the ``worker`` config row has none; serve
# seeds it as the row's recommended prompt. It describes the role only: the
# working principles are the harness before it, and the report format is the
# code-owned contract after it.
DEFAULT_SYSTEM_PROMPT = """\
You are a Worker in Looper, a system of Claude Code agents that carries out a
user's task. A Planner splits the task into steps and decides what happens
after each result. Each step runs in a fresh Claude Code session like this
one, and this one carries out one step.

- You own the step's result: make the changes it asks for, in its working
  directory, until it meets its acceptance criteria.
- Build on what the steps before it produced, and leave the parts of the
  task that belong to other steps to them.
- When the step cannot be done as written, do what can be done and report
  what is missing, rather than working around the step.
"""

# The Reviewer's role prompt when the ``reviewer`` config row has none; serve
# seeds it as the row's recommended prompt. The verdict format is the
# code-owned contract after it.
REVIEWER_SYSTEM_PROMPT = """\
You are a Reviewer in Looper, a system of Claude Code agents that carries out a
user's task. A Planner splits the task into steps; a Worker has carried out
one of them, and this fresh Claude Code session checks it independently
before the Planner accepts it.

- Judge the result, not the report: the Worker's report says what it believes
  it did; check the files and the behaviour in the working directory yourself.
- Hold the work to every acceptance criterion and to what the step asked;
  report what is missing or wrong, not matters of taste.
- Make each finding specific enough to fix on a retry: what is wrong, where,
  and what the criterion expects.
"""

# Each run's role prompt when its config row has none.
_DEFAULT_ROLE_PROMPTS = {AgentType.WORKER: DEFAULT_SYSTEM_PROMPT, AgentType.REVIEWER: REVIEWER_SYSTEM_PROMPT}


@dataclass(frozen=True)
class WorkerSettings:
    """A ``worker`` or ``reviewer`` agent config, read for every run (its agent settings are read with its
    project, ``load_agent_settings``)."""

    timeout_seconds: int  # 0 for no limit
    max_turns: int  # 0 for no limit
    system_prompt: str  # the role prompt
    recommended: bool = True  # whether it is the recommended one (default_prompt)


async def _load_settings(agent: AgentType = AgentType.WORKER) -> WorkerSettings:
    """Read ``agent``'s config row for one run, so a dashboard edit applies to the next step.

    Without a readable row a run may take 86400 s, has no turn limit and uses
    the default role prompt; an empty role prompt also falls back to it.
    """
    try:
        config = await db_role_configs.get_config(agent.value)
    except Exception:
        logger.warning("cannot read the %s agent config; using defaults", agent.value, exc_info=True)
        config = None
    default_prompt = _DEFAULT_ROLE_PROMPTS[agent].strip()
    if config is None:
        return WorkerSettings(DEFAULT_RUN_TIMEOUT_SEC, 0, default_prompt)
    system_prompt = str(config.get("system_prompt") or "").strip() or default_prompt
    return WorkerSettings(
        timeout_seconds=int(config.get("timeout_seconds") or 0),
        max_turns=int(config.get("max_turns") or 0),
        system_prompt=system_prompt,
        recommended=is_recommended(system_prompt, str(config.get("default_prompt") or "") or default_prompt),
    )


def _display_command(command: list[str]) -> str:
    """The command for the activity timeline, with the isolation settings shown by their size."""
    shown = list(command)
    if "--settings" in shown:
        at = shown.index("--settings") + 1
        shown[at] = f"<{len(shown[at])} chars>"
    return " ".join(shown)


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
        return failed(budget_error(result_event) or f"Claude Code run did not succeed ({subtype})")
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
            "learnings": journal.reported_learnings(verdict.get("learnings")),
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
    learnings = journal.reported_learnings(report.get("learnings"))
    if status == "failed":
        return _work_failed(summary or "Worker reported failure", summary=summary, changed_files=changed,
                            learnings=learnings)

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
        "learnings": learnings,
    }


@dataclass
class WorkerConfig:
    claude_bin: str = "claude"
    sandbox: str = "workspace-write"


@dataclass
class WorkerProcess:
    """Tracks a single Worker subprocess."""

    worker_id: str
    task_id: str
    plan_id: str
    process: asyncio.subprocess.Process | None = None
    status: str = "pending"  # "pending" (waiting for room in its project) | "running" | "completed" | "failed"
    result: dict[str, Any] | None = None
    role: AgentType = AgentType.WORKER  # or REVIEWER
    parent_task_id: str = ""  # the task whose step this is (task_id is the step's)
    step: str = ""  # the Planner's label for the step
    instruction: str = ""
    project_id: str | None = None  # whose limit it counts against, once read
    since: datetime = field(default_factory=lambda: datetime.now(timezone.utc))  # when it began to wait, then to run


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
        self._tasks: dict[str, asyncio.Task[Any]] = {}
        # The runs counted against their project's limit: worker id -> (role, project id).
        self._slots: dict[str, tuple[AgentType, str | None]] = {}
        # Held to check a limit and take a place, so two runs cannot both take a project's last one.
        self._slot_lock = asyncio.Lock()
        # The runs waiting for a place, woken to check again when a run ends or a limit changes.
        self._slot_waiters: set[asyncio.Future[None]] = set()
        self._listener_task: asyncio.Task[Any] | None = None
        self._running = False
        self._workers_dir = runtime_root / ".local" / "workers"
        self._workers_dir.mkdir(parents=True, exist_ok=True)
        # stdin of each running Claude, open until its result, and its runtime: more user turns go here.
        self._inputs: dict[str, tuple[asyncio.StreamWriter, RuntimeAdapter]] = {}

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
        logger.info("worker pool started")

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

        await asyncio.gather(*(proc_tree.end(wp.process, label=f"worker {wp.worker_id}")
                               for wp in self._active.values() if wp.process and wp.process.returncode is None))
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
        """Stop a running worker mid-run, or one waiting for room in its project. Cancelling its task ends its
        process and everything it started (``_execute`` ends the tree when the read is cancelled) and runs the
        done-callback that frees its place. Returns whether a worker was running or waiting; a finished or unknown
        worker returns False. A killed worker publishes no result."""
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
        running = self._inputs.get(worker_id)
        if running is None or not text.strip():
            return False
        stdin, runtime = running
        try:
            await runtime.write_turn(stdin, text)
        except (BrokenPipeError, ConnectionResetError, RuntimeError):
            self._inputs.pop(worker_id, None)
            return False
        logger.info("worker %s received a user turn (%d chars)", worker_id, len(text))
        return True

    async def _dispatch(self, envelope: MessageEnvelope) -> None:
        worker_id = envelope.target.agent_id
        if worker_id in self._active or worker_id in self._tasks:
            logger.debug("worker %s already active, skipping", worker_id)
            return

        logger.info("dispatching worker %s (active=%d)", worker_id, len(self._active))
        task = asyncio.create_task(self._run_worker(envelope))
        self._tasks[worker_id] = task
        task.add_done_callback(lambda t: self._cleanup_worker(worker_id))

    def _cleanup_worker(self, worker_id: str) -> None:
        self._tasks.pop(worker_id, None)
        self._active.pop(worker_id, None)
        if self._slots.pop(worker_id, None) is not None:
            self._wake_slot_waiters()

    def _wake_slot_waiters(self) -> None:
        for waiter in self._slot_waiters:
            if not waiter.done():
                waiter.set_result(None)
        self._slot_waiters.clear()

    async def recheck(self) -> None:
        """Let every run waiting for room in its project check its limit again (a limit changed)."""
        self._wake_slot_waiters()

    async def _take_slot(self, wp: WorkerProcess) -> None:
        """Wait until fewer of the run's role run in its task's project than its limit, read each time, then take a
        place. A run whose project cannot be read counts outside any project, under the role's limit."""
        worker_id, agent, task_id = wp.worker_id, wp.role, wp.parent_task_id
        project_id = wp.project_id = await self._project_id(task_id)
        waiting = False
        while True:
            async with self._slot_lock:
                limit = await load_concurrency(agent, project_id)
                running = sum(1 for held in self._slots.values() if held == (agent, project_id))
                if running < limit:
                    self._slots[worker_id] = (agent, project_id)
                    return
                waiter = asyncio.get_running_loop().create_future()
                self._slot_waiters.add(waiter)
            if not waiting:
                waiting = True
                label = "Reviewer" if agent is AgentType.REVIEWER else "Worker"
                logger.info("worker %s waits: %d %ss run in project %s, its limit %d", worker_id, running,
                            agent.value, project_id, limit)
                if task_id:
                    await self._activity(task_id, worker_id, "phase",
                                         f"{label} waits for room in this project ({running} running, limit {limit})")
            try:
                await waiter
            finally:
                self._slot_waiters.discard(waiter)

    @staticmethod
    async def _project_id(task_id: str) -> str | None:
        """The task's project, for the concurrency limit; None when it cannot be read (the run says why)."""
        if not task_id:
            return None
        try:
            project = await db_projects.get_task_project(task_id)
        except Exception:
            return None
        return str(project["id"]) if project else None

    async def _run_worker(self, envelope: MessageEnvelope) -> None:
        assert self._bus is not None
        worker_id = envelope.target.agent_id
        payload = envelope.payload
        task_id = payload.get("task_id", "")
        plan_id = payload.get("plan_id", "")

        is_verify = envelope.message_type == MessageType.VERIFY_REQUEST
        parent_task_id = payload.get("parent_task_id", "")

        wp = WorkerProcess(
            worker_id=worker_id,
            task_id=task_id,
            plan_id=plan_id,
            role=AgentType.REVIEWER if is_verify else AgentType.WORKER,
            parent_task_id=parent_task_id,
            step=str(payload.get("step_key") or ""),
            instruction=str(payload.get("instruction") or ""),
        )
        self._active[worker_id] = wp

        await self._take_slot(wp)
        wp.status, wp.since = "running", datetime.now(timezone.utc)
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

        agent = AgentType.REVIEWER if is_verify else AgentType.WORKER
        settings = await _load_settings(agent)
        run_timeout, max_turns = settings.timeout_seconds, settings.max_turns
        # Read as the run starts: a later change reaches the next run, not this one.
        project = await self._task_project(parent_task_id, worker_id)
        knowledge = await load_knowledge(self._runtime_root, project, work_dir)
        project_id = str((project or {}).get("id") or "") or None
        library = await load_installed(self._runtime_root, agent.value, project_id)
        # The task's own choice of Agent Profile for this role wins over the layers.
        agent_settings = await load_agent_settings(agent, project_id, parent_task_id or None)
        if agent_settings.profile_error:
            # The step fails with the reason rather than run another agent.
            logger.error("worker %s: %s", worker_id, agent_settings.profile_error)
            if parent_task_id:
                await self._activity(parent_task_id, worker_id, "error", agent_settings.profile_error[:500])
            if is_verify:
                return {"passed": False, "findings": [agent_settings.profile_error],
                        "summary": agent_settings.profile_error}
            return {"status": "failed", "error": agent_settings.profile_error}
        launch = Launch(agent_settings, library, knowledge, role=AgentType.REVIEWER if is_verify else AgentType.WORKER)
        background = str((project or {}).get("background") or "")
        system_prompt = build_system_prompt(
            harness=agent_settings.harness,
            role_id=agent.value,
            role=settings.system_prompt,
            recommended=settings.recommended,
            project=project,
            task_kind="review" if is_verify else "work",
            task_id=parent_task_id or None,
            step_id=payload.get("task_id") or None,
            task=self._task_section(payload, work_dir, is_verify=is_verify),
            contract_name="REVIEW_CONTRACT" if is_verify else "WORK_CONTRACT",
            contract=REVIEW_CONTRACT if is_verify else WORK_CONTRACT,
            knowledge=knowledge,
        )
        if background.strip() and parent_task_id:
            await self._activity(parent_task_id, worker_id, "phase",
                                 f"Project background applied ({len(background)} chars)")
        if knowledge.documents and parent_task_id:
            await self._activity(parent_task_id, worker_id, "phase",
                                 f"Project knowledge applied ({documents_label(knowledge.documents)})")
        if knowledge.error and parent_task_id:
            await self._activity(parent_task_id, worker_id, "error",
                                 f"Warning: {knowledge.error}; this run goes without it"[:500])
        if (in_project := [item for item in library.items if item.scope == "project"]) and parent_task_id:
            await self._activity(parent_task_id, worker_id, "phase",
                                 f"Project installs applied ({items_label(in_project)})")
        if library.error and parent_task_id:
            await self._activity(parent_task_id, worker_id, "error",
                                 f"Warning: {library.error}; this run goes without them"[:500])
        if agent_settings.error and parent_task_id:
            await self._activity(parent_task_id, worker_id, "error",
                                 f"Warning: {agent_settings.error}; this run uses the built-in defaults"[:500])
        try:
            prompt_file = system_prompt.write_file(self._runtime_root)
        except OSError as exc:
            problem = f"Cannot write the system prompt file: {exc}"
            logger.error("worker %s: %s", worker_id, problem)
            if is_verify:
                return {"passed": False, "findings": [problem], "summary": problem}
            return {"status": "failed", "error": problem}
        runtime = launch.runtime
        command = runtime.run_command(launch.executable(self._config.claude_bin), cwd=work_dir,
                                      prompt_file=prompt_file, launch=launch, max_turns=max_turns)

        if parent_task_id:
            cmd_display = _display_command(command) + f"  (prompt on stdin, {len(prompt)} chars)"
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
            system_prompt=system_prompt,
            launch=launch,
        )
        await recorder.start()

        logger.info("worker %s starting subprocess: %s (cwd=%s)", worker_id, command[0], work_dir)
        try:
            process = await proc_tree.spawn(
                *command,
                cwd=work_dir,
                env=launch.environment(),
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
            await runtime.write_turn(process.stdin, prompt)
            self._inputs[worker_id] = (process.stdin, runtime)
        except (BrokenPipeError, ConnectionResetError):
            # Claude exited before reading; its output decides the outcome.
            logger.warning("worker %s: Claude closed stdin before the prompt was sent", worker_id)

        def _end_input() -> None:
            # Without this Claude would wait on stdin for another user turn.
            if self._inputs.pop(worker_id, None) is not None and not process.stdin.is_closing():
                process.stdin.close()

        result_event: dict[str, Any] | None = None
        finished: dict[str, Any] | None = None  # the last result Claude wrote (result_event, unless capped)

        async def _read_stream() -> None:
            nonlocal result_event, finished
            assert process.stdout is not None
            async for event in runtime.parse(process.stdout, model=launch.model):
                await recorder.record(event)
                if (capped := recorder.cap_stop) is not None:
                    # Over its per-run cap at the provider's prices: stopped now, ending with the cap's result.
                    result_event = capped.result_event
                    _end_input()
                    await proc_tree.end(process, label=f"worker {worker_id}")
                    return

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
                            summary = runtime.tool_summary(tool_name, tool_input)
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
                    # Background work (an agent team's teammates) can end in turns of Claude's own after this
                    # one, each with a result of its own, also once stdin is closed: the run's last result is
                    # its outcome.
                    result_event = finished = event
                    _end_input()

        async def _report_finished(event: dict[str, Any]) -> None:
            # The run's cost as recorded: Claude Code's figure, or its tokens at the provider's prices.
            cost = run_usage(event, price=launch.price).cost_usd if launch.price is not None \
                else event.get("total_cost_usd", event.get("cost_usd"))
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
            # Never leave Claude, or anything it started, running unsupervised once the worker gives up.
            await proc_tree.end(process, label=f"worker {worker_id}")
            timed_out = isinstance(exc, asyncio.TimeoutError)
            error = (
                f"Claude ran longer than the {run_timeout} s limit (timeout_seconds)" if timed_out
                else f"worker stopped reading Claude: {type(exc).__name__}"
            )
            await recorder.finish(output_text=None, result_event=result_event, error=error)
            if timed_out:
                raise asyncio.TimeoutError(error) from exc
            raise
        if finished is not None and finished is result_event:
            await _report_finished(finished)

        final_text = str((result_event or {}).get("result") or "") or None
        return_code = process.returncode or 0
        # A successful result decides the outcome even when the process then
        # exits non-zero: Claude Code can exit 1 for reasons unrelated to the
        # work (an MCP server or hook erroring on teardown, for example). Only
        # when there is no usable result does the exit code become the failure.
        result_ok = (
            result_event is not None
            and not result_event.get("is_error")
            and result_event.get("subtype") == "success"
        )
        # A run stopped at its spend cap exits non-zero too; its result says why.
        if return_code != 0 and not result_ok and runtime.budget_error(result_event) is None:
            stderr = ""
            if process.stderr:
                stderr_bytes = await process.stderr.read()
                stderr = stderr_bytes.decode("utf-8", errors="replace") if stderr_bytes else ""
            error = f"Claude Code exited with code {return_code}"
            await recorder.finish(output_text=final_text, result_event=result_event, error=error)
            return {"status": "failed", "error": error, "stderr": stderr[:500]}
        if return_code != 0:
            logger.warning(
                "worker %s: Claude exited %d but produced a successful result; judging the result",
                worker_id, return_code,
            )

        outcome = judge_result(result_event, work_dir=Path(work_dir), is_verify=is_verify)
        # A review that says "not passed" is still a successful run; only work
        # failures are the run's error.
        error = outcome.get("error") if not is_verify and outcome.get("status") == "failed" else None
        await recorder.finish(output_text=final_text, result_event=result_event, error=error)
        return outcome

    @staticmethod
    def _task_section(payload: dict[str, Any], work_dir: str, *, is_verify: bool) -> str:
        """The run's task (the system prompt's L3): the step, with the task it belongs to."""
        requirement = str(payload.get("requirement") or "")
        step = str(payload.get("step_key") or payload.get("task_id") or "")
        instruction = str(payload.get("instruction") or "")
        acceptance = [str(c) for c in payload.get("acceptance_criteria") or []]
        workspace = (payload.get("context") or {}).get("workspace")
        if is_verify:
            return review_task_section(requirement=requirement, step=step, instruction=instruction,
                                       acceptance_criteria=acceptance, work_dir=work_dir, workspace=workspace)
        return work_task_section(
            requirement=requirement, step=step, instruction=instruction, acceptance_criteria=acceptance,
            validation_commands=[str(c) for c in payload.get("validation_commands") or []],
            target_files=[str(f) for f in payload.get("target_files") or []],
            feedback=[str(f) for f in payload.get("feedback") or []],
            attempt=int(payload.get("attempt") or 1),
            work_dir=work_dir,
            workspace=workspace,
        )

    def _build_work_prompt(self, payload: dict[str, Any]) -> str:
        """The run's first user turn: what the steps this one depends on reported, and the task's findings.

        The step itself is the system prompt's task section; the reports and
        the findings (the task journal's section, built by the Planner) are
        other agents' output, so they stay here as data.
        """
        parts = ["Start your step (the Task section of your instructions)."]
        dependencies = [d for d in payload.get("dependencies") or [] if isinstance(d, dict)]
        if dependencies:
            parts += ["", "What the steps it depends on reported (their Workers' words, to use as data):"]
            for dependency in dependencies:
                line = f"- {dependency.get('step') or '?'}: {dependency.get('summary') or '(no summary)'}"
                changed = [str(path) for path in dependency.get("changed_files") or []]
                if changed:
                    line += f" (changed: {', '.join(changed)})"
                parts.append(line)
        findings = str(payload.get("findings") or "")
        if findings:
            parts += ["", findings]
        return "\n".join(parts)

    def _build_verify_prompt(self, payload: dict[str, Any]) -> str:
        """The run's first user turn: the Worker's report under review.

        The report is the Worker's own output, so it stays here as data to
        check; the step under review is the system prompt's task section.
        """
        work_result = payload.get("work_result") or {}
        parts = ["Review the step in the Task section of your instructions. The Worker's report, to check:"]
        if work_result.get("summary"):
            parts.append(f"Summary: {work_result['summary']}")
        parts.append(f"Files changed: {json.dumps(work_result.get('changed_files', {}), ensure_ascii=False)}")
        if work_result.get("deleted_files"):
            parts.append(f"Files deleted: {json.dumps(work_result['deleted_files'], ensure_ascii=False)}")
        return "\n".join(parts)

    async def _task_project(self, task_id: str, worker_id: str) -> dict[str, Any] | None:
        """The task's project row, read now; None without one.

        The background is guidance, so a run whose project cannot be read
        goes on without it, and the task's activity says so.
        """
        if not task_id:
            return None
        try:
            project = await db_projects.get_task_project(task_id)
        except Exception as exc:
            logger.warning("worker %s cannot read the project of task %s", worker_id, task_id, exc_info=True)
            problem = f"{type(exc).__name__}: {exc}"
        else:
            if project is not None:
                return project
            problem = "the task's project was not found"
        await self._activity(
            task_id, worker_id, "error",
            f"Warning: the project background was unavailable ({problem}); this run goes on without it"[:500],
        )
        return None

    @staticmethod
    async def _activity(task_id: str, worker_id: str, kind: str, summary: str) -> None:
        try:
            await db_activity.insert_activity(task_id=task_id, worker=worker_id, kind=kind, summary=summary)
        except Exception:
            logger.debug("failed to insert %s activity", kind, exc_info=True)

    def active_count(self) -> int:
        return len(self._active)

    def status(self) -> dict[str, Any]:
        return {
            "active": self.active_count(),
            "workers": {
                wid: {"task_id": wp.task_id, "status": wp.status}
                for wid, wp in self._active.items()
            },
        }

    def runs(self) -> list[dict[str, Any]]:
        """The Worker and Reviewer runs now, each ``running`` or ``waiting`` (for room under its project's limit),
        with its role, task, step, project (None until it is read) and ``since``: when it began that state."""
        states = {"pending": "waiting", "running": "running"}  # a finished run is reporting its result
        return [
            {"agent_id": wp.worker_id, "role": wp.role.value, "task_id": wp.parent_task_id, "step_id": wp.task_id,
             "step": wp.step, "instruction": wp.instruction, "project_id": wp.project_id,
             "state": states[wp.status], "since": wp.since}
            for wp in self._active.values() if wp.status in states
        ]
