"""Task-owned Claude Code residents, separate from the operator's Manager."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from enum import Enum
from pathlib import Path
from stat import S_ISREG
from typing import Any, Literal, Protocol

from ...db import activity, agent_sessions, projects, tasks
from .. import operator_mcp, outbound_hook, step_workspaces, task_budget, task_conversations_settings
from .. import task_manager_safety, task_manager_settings, task_workspaces
from ..agent_settings import Access, Launch
from ..claude_cli import ClaudeRunRecorder, ResidentClaude
from ..library import EMPTY_LIBRARY
from ..project_execution import ProjectExecution
from ..run_usage import Usage
from ..runtimes.cancel import TurnCancel
from ..system_prompt import SystemPrompt
from ..task_manager_brief import brief, instructions
from ..task_commands import history_for_model
from ..untrusted import Source, fence

logger = logging.getLogger(__name__)


class TaskManagerEvent(str, Enum):
    QUEUED = "queued"
    SESSION_FRESH = "session_fresh"
    TURN_SUMMARY = "turn_summary"
    REFUSED = "refused"
    STOPPED = "stopped"
    MODEL_SWITCHED = "model_switched"
    MODEL_SWITCH_REJECTED = "model_switch_rejected"


@dataclass(frozen=True)
class ToolRecord:
    tool_use_id: str
    name: str
    args: str
    result: str
    is_error: bool


class TurnSink(Protocol):
    async def delta(self, text: str) -> None: ...
    async def tool(self, record: ToolRecord) -> None: ...
    async def event(self, kind: TaskManagerEvent, payload: Mapping[str, Any]) -> None: ...
    async def final(self, text: str, outcome: str) -> None: ...


class RefuseReason(str, Enum):
    WORK_MODE_OFF = "work_mode_off"
    RUNTIME_UNSUPPORTED = "runtime_unsupported"
    BUDGET = "budget"
    OUTBOUND_REVIEW_UNAVAILABLE = "outbound_review_unavailable"
    WORKSPACE = "workspace"
    SHUTTING_DOWN = "shutting_down"
    REVIEW_HOOK_DISABLED = "review_hook_disabled"
    CLI_UNVERIFIED = "cli_unverified"
    MANAGER_ANSWERS_OFF = "manager_answers_off"
    MCP_UNRESOLVED = "mcp_unresolved"


def _off_text(reason: RefuseReason) -> str:
    if reason is RefuseReason.MANAGER_ANSWERS_OFF:
        return task_conversations_settings.MANAGER_OFF_TEXT
    return "Task conversations are disabled (tasks.conversations.enabled is off)."


def _off_reason() -> RefuseReason | None:
    """Why no task Manager may start now: work mode, or its own switch (default off), is off."""
    if not task_conversations_settings.enabled():
        return RefuseReason.WORK_MODE_OFF
    if not task_conversations_settings.manager_answers():
        return RefuseReason.MANAGER_ANSWERS_OFF
    return None


def _visible_history_item(item: Any) -> str:
    if isinstance(item, str):
        return item
    visible = {key: value for key, value in dict(item).items() if key != "entry_kind"}
    return json.dumps(visible, ensure_ascii=False)


def resume_message(text: str, history: Sequence[Any]) -> str:
    """The text a fresh or rotating turn sends. Fixed commands and their code replies are matched and left out."""
    kept = history_for_model(history)[-20:]
    if not kept:
        return text
    rendered = "\n".join(_visible_history_item(item) for item in kept)
    return fence(rendered, source=Source.CHAT_HISTORY) + "\n\n" + text


class Refused(Exception):
    def __init__(self, reason: RefuseReason, text: str) -> None:
        super().__init__(text)
        self.reason = reason


class _TaskResident(ResidentClaude):
    """Keep credentials aligned with every resident exit and every Looper-sent turn."""

    def __init__(self, owner: TaskManagerAgent) -> None:
        super().__init__(label=f"task-manager-{owner.task_id}")
        self.owner = owner

    async def run_turn(self, *args: Any, **kwargs: Any) -> Any:
        ops, token = self.owner.pool.ops, self.owner.token
        if ops is not None and token is not None:
            ops.begin_turn(token)
        try:
            return await super().run_turn(*args, **kwargs)
        finally:
            if ops is not None and token is not None:
                ops.end_turn(token)

    async def stop(self, **kwargs: Any) -> None:
        try:
            await super().stop(**kwargs)
        finally:
            if self.owner.token is not None:
                self.owner.pool.ops.revoke(self.owner.token)
                self.owner.token = None
            # They can hold server secrets: no file outlives its session or survives a replacement.
            for path in self.owner.mcp_files:
                Path(path).unlink(missing_ok=True)
            self.owner.mcp_files.clear()


@dataclass(frozen=True)
class Submitted:
    state: Literal["started", "queued", "refused"]
    reason: RefuseReason | None = None


@dataclass
class _Request:
    task_id: str
    text: str
    message_id: str
    sink: TurnSink
    history: Sequence[Mapping[str, Any] | str]
    admitted: asyncio.Future[Submitted]
    cancel: TurnCancel = field(default_factory=TurnCancel)


class _ResumeFailed(Exception):
    pass


_DIGEST_FILE = 1 << 20  # a file larger than this is never hashed
_DIGEST_BUDGET = 8 << 20  # total bytes one decision may hash


async def _files(cwd: str) -> dict[str, tuple[Any, ...]]:
    """Status plus file metadata distinguishes another edit without reading large files. A staged rename is one
    change of two paths: both are recorded, each naming the other as its partner (second field)."""
    status = await step_workspaces._git(cwd, "status", "--porcelain", "-z", "--untracked-files=all", check=False)
    if status.returncode:
        return {}

    def snapshot() -> dict[str, tuple[Any, ...]]:
        found = {}
        entries = iter(status.stdout.decode("utf-8", "surrogateescape").split("\0"))
        for entry in entries:
            if not entry:
                continue
            name = entry[3:]
            origin = next(entries, "") if "R" in entry[:2] or "C" in entry[:2] else ""
            pairs = ((name, origin), (origin, name)) if "R" in entry[:2] else ((name, ""),)
            for path, partner in pairs:
                try:
                    stat = (Path(cwd) / path).lstat()
                    value = (stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size)
                except OSError:
                    value = ()
                found[path] = (entry[:2], partner, *value)
        return found

    return await asyncio.to_thread(snapshot)


async def _digests(cwd: str, names: Sequence[str]) -> tuple[dict[str, str], set[str]]:
    """Content hashes of the non-empty regular files among ``names``, at most ``_DIGEST_BUDGET`` bytes in all and
    ``_DIGEST_FILE`` each; the second value holds the files that were skipped for size or budget (unknown content)."""
    def hashed() -> tuple[dict[str, str], set[str]]:
        found, unknown, spent = {}, set(), 0
        for name in sorted(names):
            try:
                path = Path(cwd) / name
                stat = path.lstat()
                if not S_ISREG(stat.st_mode) or not stat.st_size:
                    continue
                if stat.st_size > _DIGEST_FILE or spent + stat.st_size > _DIGEST_BUDGET:
                    unknown.add(name)
                    continue
                found[name] = hashlib.sha1(path.read_bytes()).hexdigest()
                spent += stat.st_size
            except OSError:
                continue
        return found, unknown
    return await asyncio.to_thread(hashed)


async def _own_paths(cwd: str, files: Sequence[str], before: Mapping[str, tuple[Any, ...]],
                     after: Mapping[str, tuple[Any, ...]], before_digests: Mapping[str, str],
                     before_unknown: set[str]) -> tuple[list[str], list[str], list[str]]:
    """Split the turn's changed ``files`` into the paths it owns (safe to commit), the ones that were the user's
    before it began, and the ones whose owner could not be settled within the hashing budget (never committed)."""
    def partner(snapshot: Mapping[str, tuple[Any, ...]], name: str) -> str:
        return snapshot[name][1] if name in snapshot else ""
    theirs = [name for name in files if name in before or partner(after, name) in before]
    rest = [name for name in files if name not in theirs]
    # An unstaged rename shows as a deletion plus a new file: a new file holding the content of a path that was
    # dirty before the turn, and is gone now, is that path's, moved.
    vanished = [name for name in before if name not in after or "D" in after[name][0]]
    candidates = [name for name in rest if "?" in after[name][0] or "A" in after[name][0]] if vanished else []
    unverified: list[str] = []
    if candidates:
        now, now_unknown = await _digests(cwd, candidates)
        moved = {before_digests[name] for name in vanished if name in before_digests}
        theirs += [name for name in candidates if now.get(name) in moved]
        if now_unknown or any(name in before_unknown for name in vanished):
            unverified = [name for name in candidates if name not in theirs and (
                name in now_unknown or any(v in before_unknown for v in vanished))]
    return ([name for name in rest if name not in theirs and name not in unverified], theirs, unverified)


class TaskManagerAgent:
    def __init__(self, pool: TaskManagerPool, task_id: str) -> None:
        self.pool, self.task_id = pool, task_id
        self.token: str | None = None
        self.resident = _TaskResident(self)
        self.last_used = pool.clock()
        self.busy = False
        self.session_id: str | None = None
        self.totals = Usage()
        self.loaded = False
        self.context_rotation_pending = False
        self.running: asyncio.Task[None] | None = None
        self.mcp_fingerprints: dict[str, str] = {}  # of the allowed servers launched, to compare with what ran
        self.mcp_files: set[str] = set()  # generated configs holding the operator's server definitions

    async def prepare(self, *, sink: TurnSink | None = None) -> tuple[Mapping[str, Any], Any, Launch, SystemPrompt]:
        """Read configuration afresh; no launch bypasses the direct-mode review checks."""
        try:
            return await self._prepare()
        except BaseException:
            if not self.resident.started:
                await self.resident.stop(kill=True)
            raise

    async def _prepare(self) -> tuple[Mapping[str, Any], Any, Launch, SystemPrompt]:
        if (off := _off_reason()) is not None:
            raise Refused(off, _off_text(off))
        if self.pool.closing:
            raise Refused(RefuseReason.SHUTTING_DOWN, "The task Manager is shutting down.")
        if not self.loaded:
            saved = await agent_sessions.get_session(f"task-manager-{self.task_id}")
            if saved:
                self.session_id = saved["session_id"]
                self.totals = Usage.from_dict(saved["session_totals"])
                self.context_rotation_pending = saved.get("pending_handoff") == "context_bound"
            self.loaded = True
        task = await tasks.get_task(self.task_id)
        if task is None:
            raise Refused(RefuseReason.WORKSPACE, "The task no longer exists.")
        settings = await task_manager_settings.load_task_manager_settings(task)
        if settings.profile.runtime.type != "claude-code":
            raise Refused(RefuseReason.RUNTIME_UNSUPPORTED,
                          f"Profile {settings.profile.name}: the task Manager runs only on Claude Code for now.")
        if settings.error or settings.profile_error:
            raise RuntimeError(settings.error or settings.profile_error)
        project = await projects.get_task_project(self.task_id)
        links = ProjectExecution.from_row(project).link_paths
        self.links = links

        async def note(kind: str, text: str) -> None:
            await activity.insert_activity(task_id=self.task_id, kind=kind, summary=text)

        try:
            reopened = await task_workspaces.reopen(task, runtime_root=self.pool.runtime_root, links=links, note=note)
        except task_workspaces.TaskWorkspaceError as exc:
            raise Refused(RefuseReason.WORKSPACE, str(exc)) from exc
        direct = (tasks.interaction_mode(task["status"]) is tasks.InteractionMode.DIRECT
                  and reopened.mode is task_workspaces.ReopenMode.WRITE)
        self.mode = "direct" if direct else "orchestrated"
        async def configure() -> Launch:
            library, hook = EMPTY_LIBRARY, None
            if self.pool.ops is not None:
                if self.token is None:
                    self.token = self.pool.ops.issue(self.task_id)
                library = await self.pool.ops.library_with_server(library, self.pool.runtime_root, self.token)
            if outbound_hook.known_enabled():
                try:
                    hook = (self.pool.ops.tool_hook(self.token, self.task_id, self.pool.runtime_root,
                                                   self.pool.claude_bin, library) if self.pool.ops else None)
                except Exception as exc:
                    raise Refused(RefuseReason.OUTBOUND_REVIEW_UNAVAILABLE, "The outbound review hook is unavailable.") from exc
                if hook is None:
                    raise Refused(RefuseReason.OUTBOUND_REVIEW_UNAVAILABLE, "The outbound review hook is unavailable.")
            run = Launch(settings=settings, library=library, role=None,
                         access=Access.FULL if direct else Access.READ_ONLY, tool_hook=hook)
            if direct and (allowed := task_conversations_settings.manager_mcp()):
                try:
                    run = replace(run, library=await operator_mcp.library_with_allowed(
                        library, self.pool.runtime_root, allowed,
                        environment=run.runtime.environment(run)))
                except operator_mcp.UnresolvedServers as exc:
                    raise Refused(RefuseReason.MCP_UNRESOLVED, str(exc)) from exc
                self.mcp_files.add(run.library.mcp_config)
                self.mcp_fingerprints = operator_mcp.fingerprints(run.library, allowed)
            else:
                self.mcp_fingerprints = {}
            return run
        # Query newest summaries directly, so a long-lived task never gets its oldest activity as its brief.
        from ...db.connection import get_pool
        recent = await get_pool().fetch("SELECT summary FROM activity WHERE task_id=$1 ORDER BY seq DESC LIMIT 20",
                                        self.task_id)
        prompt = SystemPrompt(instructions(self.mode) + "\n\n" + brief(task, recent_activity=list(reversed(recent)),
                                                                    reopened=reopened))
        root = self.pool.runtime_root / "task-managers" / self.task_id
        prompt_file = prompt.write_file(root)
        launch = await configure()
        executable = launch.executable(self.pool.claude_bin)

        def command_for(launch: Launch) -> list[str]:
            return (launch.runtime.operator_resident_command(executable, cwd=reopened.work_dir, prompt_file=prompt_file,
                                                             launch=launch, settings_dir=root / "settings")
                    if direct else launch.runtime.resident_command(executable, cwd=reopened.work_dir,
                                                                   prompt_file=prompt_file, launch=launch))

        command = command_for(launch)
        env = task_manager_safety.environment(launch, self.task_id)
        settings_file = command[command.index("--settings") + 1] if "--settings" in command else ""
        signature = (self.mode, reopened.work_dir, launch.profile.id, launch.model, launch.effort, launch.profile.provider, prompt.sha256, settings_file,
                     task_conversations_settings.manager_mcp() if direct else ())
        async with self.pool._available:
            notified = False
            while not await self.pool._slot(self):
                if self.pool.closing:
                    raise Refused(RefuseReason.SHUTTING_DOWN, "The task Manager is shutting down.")
                if sink is not None and not notified:
                    await sink.event(TaskManagerEvent.QUEUED, {"reason": "cap", "position": 1,
                                                              "active": self.pool.working_count()})
                    notified = True
                await self.pool._available.wait()
            if self.pool.closing:
                raise Refused(RefuseReason.SHUTTING_DOWN, "The task Manager is shutting down.")
            restarting = not self.resident.started or self.resident.stale(signature)
            if restarting:
                await self.resident.stop()
                launch = await configure()
                command = command_for(launch)
                env = task_manager_safety.environment(launch, self.task_id)
                if direct:
                    problem = await task_manager_safety.gate(executable, cwd=reopened.work_dir, environment=launch.runtime.environment(launch),
                        phase="resume" if self.session_id else "launch", settings_file=Path(settings_file))
                    if problem is not None:
                        await self.resident.stop(kill=True)
                        raise Refused(RefuseReason(problem.reason.value), problem.notice)
                if self.session_id:
                    command += ["--resume", self.session_id]
                await self.resident.start(command, cwd=reopened.work_dir, signature=signature, env=env,
                                          totals=self.totals, price=launch.price, capture_stderr=True)
            self.last_used = self.pool.clock()
        return task, reopened, launch, prompt

    async def turn(self, request: _Request) -> None:
        try:
            gate = await task_budget.gate(self.task_id)
        except Exception:
            logger.warning("task Manager %s: budget_unverifiable; allowing this turn", self.task_id, exc_info=True)
            await activity.insert_activity(task_id=self.task_id, kind="error",
                summary="budget_unverifiable: cannot read task costs; allowing this task Manager turn")
            gate = None
        if gate is not None and not gate.allowed:
            raise Refused(RefuseReason.BUDGET, "这个任务已到花费上限，提高上限后再说")
        if gate is not None and gate.unknown_models:
            await activity.insert_activity(task_id=self.task_id, kind="error",
                summary="Warning: task Manager cost is unknown for " + ", ".join(gate.unknown_models))
        task, reopened, launch, prompt = await self.prepare()
        if not request.admitted.done():
            request.admitted.set_result(Submitted("started"))
        rotating = self.context_rotation_pending
        if rotating:
            await request.sink.event(TaskManagerEvent.SESSION_FRESH,
                                     {"reason": "context_bound", "detail": "The task session reached its context bound."})
        elif reopened.reason and not self.session_id:
            await request.sink.event(TaskManagerEvent.SESSION_FRESH,
                                     {"reason": "read_only_open", "detail": reopened.reason})
        before = await _files(reopened.work_dir)
        before_digests, before_unknown = await _digests(reopened.work_dir, list(before))
        text, outcome = "", "failed"
        written = False
        record_id = None
        try:
            for attempt in range(2):
                resuming = "--resume" in self.resident.command
                expected = self.session_id if resuming else None
                initialized = False
                pieces: list[str] = []
                tools: dict[str, ToolRecord] = {}

                async def on_event(event: dict[str, Any]) -> None:
                    nonlocal initialized
                    await recorder.record(event)
                    if event.get("type") == "system" and event.get("subtype") == "init":
                        if expected and event.get("session_id") != expected:
                            raise _ResumeFailed("Claude Code resumed a different session")
                        initialized = True
                        session_id = event.get("session_id") or self.session_id
                        await agent_sessions.save_session(f"task-manager-{self.task_id}", session_id,
                            self.resident.totals.to_dict(), runtime_type="claude-code",
                            profile_id=launch.profile.id, provider_id=launch.profile.provider.id)
                        self.session_id = session_id
                    if event.get("parent_tool_use_id") is not None:
                        return
                    delta = (event.get("event") or {}).get("delta") or {}
                    if delta.get("type") == "text_delta":
                        chunk = str(delta.get("text") or "")
                        pieces.append(chunk)
                        await request.sink.delta(chunk)
                    content = (event.get("message") or {}).get("content") or []
                    for block in content if isinstance(content, list) else []:
                        if not isinstance(block, dict):
                            continue
                        if block.get("type") == "tool_use" and event.get("type") == "assistant":
                            name = str(block.get("name") or "")
                            record = ToolRecord(str(block.get("id") or ""), name,
                                (name + " " + json.dumps(block.get("input"), ensure_ascii=False))[:200], "", False)
                            tools[record.tool_use_id] = record
                            await request.sink.tool(record)
                        elif block.get("type") == "tool_result" and block.get("tool_use_id") in tools:
                            record = replace(tools[block["tool_use_id"]],
                                result=json.dumps(block.get("content"), ensure_ascii=False)[:200],
                                is_error=bool(block.get("is_error")))
                            await request.sink.tool(record)

                message = request.text
                if (attempt or rotating) and request.history:
                    message = resume_message(request.text, request.history)
                retry_detail = ""
                async with ClaudeRunRecorder(agent_id=f"task-manager-{self.task_id}", task_id=self.task_id,
                    command=self.resident.command, cwd=reopened.work_dir, input_text=message,
                    session_id=self.session_id, system_prompt=prompt, launch=launch,
                    pid_of=lambda: self.resident.pid) as recorder:
                    try:
                        result = await self.resident.run_turn(message, on_event, tag=request,
                                                              stop=recorder.cap_stop, cancel=request.cancel)
                        written |= result.prompt_sent
                        if resuming and not initialized and not request.cancel.requested:
                            retry_detail = result.stderr or await self.resident.stderr_tail()
                            raise _ResumeFailed(retry_detail or "Claude Code exited before init")
                    except _ResumeFailed as exc:
                        if attempt or request.cancel.requested:
                            raise
                        await self.resident.stop(kill=True)
                        self.session_id, self.totals = None, Usage()
                        await request.sink.event(TaskManagerEvent.SESSION_FRESH,
                                                {"reason": "resume_failed", "detail": str(exc)})
                        # Re-run all configuration and review gates before the one fresh attempt.
                        task, reopened, launch, prompt = await self.prepare()
                        continue
                    text = str((result.result_event or {}).get("result") or "".join(pieces))
                    outcome = ("stopped" if result.stopped else "success"
                               if (result.result_event or {}).get("subtype") == "success" else "failed")
                    self.totals = self.resident.totals
                    record_id = recorder.record_id
                    self.context_rotation_pending = False
                    await recorder.finish(output_text=text, result_event=result.result_event,
                                          usage=result.usage, outcome=outcome)
                if result.stopped:
                    await request.sink.event(TaskManagerEvent.STOPPED, {"method": result.stopped.value})
                break
        finally:
            if self.session_id:
                await agent_sessions.save_session(f"task-manager-{self.task_id}", self.session_id, self.totals.to_dict(),
                    runtime_type="claude-code", profile_id=launch.profile.id, provider_id=launch.profile.provider.id)
            after = await _files(reopened.work_dir)
            files = sorted(name for name in before.keys() | after.keys() if before.get(name) != after.get(name))
            commit, summary = None, {}
            if written and task.get("workspace_mode") == "worktree" and self.mode == "direct":
                # A path that already had uncommitted changes when the turn began belongs to the user: it is never
                # committed here, even when this turn edited it too.
                own, theirs, unverified = await _own_paths(reopened.work_dir, files, before, after,
                                                           before_digests, before_unknown)
                if kept := [name for name in theirs if name in after]:
                    summary["uncommitted"] = kept
                if unverified:
                    summary["unverified"] = unverified
                if own:
                    commit = await step_workspaces.commit_paths(Path(reopened.work_dir), own, self.links,
                        f"looper: task Manager turn of task {self.task_id}\n\nLooper-Task: {self.task_id}")
            await request.sink.event(TaskManagerEvent.TURN_SUMMARY, {"files": files, "commit": commit, "mode": self.mode, **summary})
        from . import task_manager_context
        await task_manager_context.maintain(self, request, reopened, launch, prompt, record_id)
        await request.sink.final(text, outcome)


_pool: TaskManagerPool | None = None


def current_pool() -> TaskManagerPool | None:
    return _pool


class TaskManagerPool:
    def __init__(self, runtime_root: Path, claude_bin: str, *, ops: Any = None,
                 max_resident: Callable[[], int], clock: Callable[[], float] = time.monotonic) -> None:
        global _pool
        self.runtime_root, self.claude_bin, self.ops = runtime_root, claude_bin, ops
        self.max_resident, self.clock = max_resident, clock
        self.agents: dict[str, TaskManagerAgent] = {}
        self.closing = False
        self._lock = asyncio.Lock()
        self._available = asyncio.Condition(self._lock)
        self._pending: list[_Request] = []
        self._active: dict[str, _Request] = {}
        self._closing_tasks: set[str] = set()
        self._close_tasks: dict[str, asyncio.Task[None]] = {}
        self._forgetting: set[str] = set()
        self._ticker = asyncio.create_task(self._tick(), name="task-manager-reclaim")
        _pool = self

    def agent(self, task_id: str) -> TaskManagerAgent:
        if task_id not in self.agents:
            self.agents[task_id] = TaskManagerAgent(self, task_id)
        return self.agents[task_id]

    def in_turn(self, task_id: str) -> bool:
        agent = self.agents.get(task_id)
        return bool(agent and agent.busy)

    def working_count(self) -> int:
        return sum(agent.busy for agent in self.agents.values())

    async def _slot(self, agent: TaskManagerAgent) -> bool:
        """Called under the pool gate; an idle eviction never races a turn admission."""
        if agent.resident.started:
            return True
        live = sum(a.resident.started or a.busy for a in self.agents.values() if a is not agent)
        if live < self.max_resident():
            return True
        for other in sorted(self.agents.values(), key=lambda a: a.last_used):
            if not other.busy and await other.resident.try_stop_if_idle():
                return True
        return False

    async def _refuse(self, request: _Request, reason: RefuseReason, text: str) -> None:
        if not request.admitted.done():
            request.admitted.set_result(Submitted("refused", reason))
        await request.sink.event(TaskManagerEvent.REFUSED, {"reason": reason})
        await request.sink.final(text, "refused")

    async def submit(self, task_id: str, text: str, *, message_id: str, sink: TurnSink,
                     history: Sequence[Mapping[str, Any] | str] = ()) -> Submitted:
        request = _Request(task_id, text, message_id, sink, history, asyncio.get_running_loop().create_future())
        async with self._lock:
            reason = (RefuseReason.SHUTTING_DOWN if self.closing or task_id in self._closing_tasks
                      else _off_reason())
            if reason is None:
                self._pending.append(request)
                await self._dispatch()
                if request in self._pending:
                    busy = self.in_turn(task_id)
                    position = sum(r.task_id == task_id for r in self._pending) if busy else len(self._pending)
                    await sink.event(TaskManagerEvent.QUEUED, {"reason": "turn" if busy else "cap", "position": position,
                                                              "active": self.working_count()})
                    return Submitted("queued")
        if reason is not None:
            await self._refuse(request, reason, "The task Manager is shutting down." if reason is RefuseReason.SHUTTING_DOWN
                               else _off_text(reason))
            return request.admitted.result()
        return await asyncio.shield(request.admitted)

    async def _dispatch(self) -> None:
        if self.closing:
            return
        for request in list(self._pending):
            if request.task_id in self._closing_tasks:
                continue
            agent = self.agent(request.task_id)
            if agent.busy or not await self._slot(agent):
                continue
            self._pending.remove(request)
            agent.busy = True
            self._active[request.task_id] = request
            agent.running = asyncio.create_task(self._drive(agent, request), name=f"task-manager-{request.task_id}")

    async def _drive(self, agent: TaskManagerAgent, request: _Request) -> None:
        try:
            if (off := _off_reason()) is not None:
                raise Refused(off, _off_text(off))
            await agent.turn(request)
        except Refused as exc:
            await self._refuse(request, exc.reason, str(exc))
        except asyncio.CancelledError:
            if not request.admitted.done():
                request.admitted.set_result(Submitted("refused", RefuseReason.SHUTTING_DOWN))
            raise  # Interrupted turns are not replayed on restart.
        except Exception:
            logger.exception("task Manager turn failed for %s", request.task_id)
            await agent.resident.stop(kill=True)
            if not request.admitted.done():
                request.admitted.set_result(Submitted("started"))
            await request.sink.final("The task Manager turn failed; inspect its run record before retrying.", "failed")
        finally:
            async with self._lock:
                agent.busy, agent.running, agent.last_used = False, None, self.clock()
                self._active.pop(request.task_id, None)
                self._available.notify_all()
                await self._dispatch()

    async def stop_current(self, task_id: str) -> bool:
        """Stop the turn running for ``task_id``. A turn that is only queued stays queued."""
        active = self._active.get(task_id)
        if active is None:
            return False
        return await self.stop_turn(task_id, active.message_id)

    async def stop_by_message(self, message_id: str) -> bool:
        """Stop the running or queued turn whose message id this is."""
        for task_id, active in list(self._active.items()):
            if active.message_id == message_id:
                return await self.stop_turn(task_id, message_id)
        queued = next((request for request in self._pending if request.message_id == message_id), None)
        if queued is None:
            return False
        return await self.stop_turn(queued.task_id, message_id)

    async def stop_turn(self, task_id: str, message_id: str) -> bool:
        active = self._active.get(task_id)
        if active and active.message_id == message_id:
            active.cancel.request()
            return True
        async with self._lock:
            queued = next((r for r in self._pending if r.task_id == task_id and r.message_id == message_id), None)
            if queued is None:
                return False
            self._pending.remove(queued)
        await queued.sink.event(TaskManagerEvent.STOPPED, {"method": "kill"})
        await queued.sink.final("", "stopped")
        return True

    async def reclaim(self) -> None:
        async with self._lock:
            for agent in self.agents.values():
                if not agent.busy and self.clock() - agent.last_used >= 1800:
                    await agent.resident.try_stop_if_idle()
            self._available.notify_all()

    async def _tick(self) -> None:
        while True:
            await asyncio.sleep(60)
            await self.reclaim()

    async def close(self, task_id: str, *, forget: bool = False) -> None:
        async with self._lock:
            if forget:
                self._forgetting.add(task_id)
            cleanup = self._close_tasks.get(task_id)
            if cleanup is None:
                self._closing_tasks.add(task_id)
                queued = [r for r in self._pending if r.task_id == task_id]
                self._pending = [r for r in self._pending if r.task_id != task_id]
                cleanup = asyncio.create_task(self._finish_close(task_id, queued), name=f"task-manager-close-{task_id}")
                self._close_tasks[task_id] = cleanup
        # A driver needs the gate in its finally block; never wait for it while holding that gate.
        await asyncio.shield(cleanup)

    async def _finish_close(self, task_id: str, queued: list[_Request]) -> None:
        try:
            for request in queued:
                await self._refuse(request, RefuseReason.SHUTTING_DOWN, "The task Manager was closed.")
            agent = self.agents.get(task_id)
            if agent:
                running = agent.running
                if running:
                    running.cancel()
                await agent.resident.stop(kill=True)
                if running:
                    await asyncio.gather(running, return_exceptions=True)
            async with self._lock:
                if task_id in self._forgetting:
                    from ...db.connection import get_pool
                    await get_pool().execute("DELETE FROM agent_sessions WHERE agent_id=$1", f"task-manager-{task_id}")
                    self.agents.pop(task_id, None)
        finally:
            async with self._lock:
                self._forgetting.discard(task_id)
                self._closing_tasks.discard(task_id)
                self._close_tasks.pop(task_id, None)
                self._available.notify_all()
                await self._dispatch()

    async def forget(self, task_id: str) -> None:
        await self.close(task_id, forget=True)

    async def shutdown(self) -> None:
        global _pool
        async with self._lock:
            self.closing = True
            queued, self._pending = self._pending, []
            running = [a.running for a in self.agents.values() if a.running]
            closing = list(self._close_tasks.values())
            self._available.notify_all()
        for request in queued:
            await self._refuse(request, RefuseReason.SHUTTING_DOWN, "The task Manager is shutting down.")
        self._ticker.cancel()
        await asyncio.gather(self._ticker, return_exceptions=True)
        for task in running:
            task.cancel()
        await asyncio.gather(*(a.resident.stop(kill=True) for a in list(self.agents.values())))
        await asyncio.gather(*running, *closing, return_exceptions=True)
        if _pool is self:
            _pool = None
