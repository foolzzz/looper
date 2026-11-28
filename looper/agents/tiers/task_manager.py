"""Task-owned Claude Code residents, separate from the operator's Manager."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from enum import Enum
from pathlib import Path
from stat import S_ISREG
from typing import Any, Literal, Protocol

from ...db import activity, agent_sessions, projects, tasks
from .. import operator_mcp, outbound_hook, step_workspaces, task_budget, task_conversations_settings
from .. import (task_manager_alerts, task_manager_denials, task_manager_dirs, task_manager_edit,
                task_manager_network, task_manager_project_instructions, task_manager_readback, task_manager_refs,
                task_manager_safety, task_manager_sandbox, task_manager_settings, task_manager_tmpdir)
from .. import task_workspaces
from ..agent_settings import Access, Launch
from ..claude_cli import ClaudeRunRecorder, ResidentClaude
from ..library import EMPTY_LIBRARY
from ..project_execution import ProjectExecution
from ..run_usage import Usage
from ..runtimes import library_mcp_servers
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
    SANDBOX_DENIAL = "sandbox_denial"
    REF_REPORT = "ref_report"
    SANDBOX_NOTICE = "sandbox_notice"


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
    # The direct session's sandbox boundary (``task_manager_sandbox``); other cards raise most of these.
    SANDBOX_UNAVAILABLE = "sandbox_unavailable"
    READBACK_UNAVAILABLE = "readback_unavailable"
    LAUNCH_SETTING_MISMATCH = "launch_setting_mismatch"
    MCP_SET_MISMATCH = "mcp_set_mismatch"
    MCP_LAUNCHER_UNSAFE = "mcp_launcher_unsafe"
    NETWORK_POLICY_INVALID = "network_policy_invalid"
    WORKSPACE_NOT_REPO_ROOT = "workspace_not_repo_root"
    WORKSPACE_IS_HOME = "workspace_is_home"
    WORKSPACE_CONTAINS_TRUSTED_CONFIG = "workspace_contains_trusted_config"
    WORKSPACE_IN_SYSTEM_DIR = "workspace_in_system_dir"
    WORKSPACE_OVERLAPS_SESSION = "workspace_overlaps_session"
    WORKSPACE_UNAVAILABLE = "workspace_unavailable"
    WORKTREE_UNSUPPORTED = "worktree_unsupported"
    HEAD_MOVED = "head_moved"
    HEAD_DETACHED = "head_detached"
    PRIVATE_TMPDIR_UNUSABLE = "private_tmpdir_unusable"


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
    def __init__(self, reason: RefuseReason, text: str, kind: task_manager_dirs.Kind | None = None) -> None:
        super().__init__(text)
        self.reason = reason
        self.kind = kind


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
            lock, self.owner.dir_lock = self.owner.dir_lock, None
            if lock is not None:
                lock.release()
            tmp, self.owner.tmpdir = self.owner.tmpdir, None
            if tmp is not None:
                await asyncio.shield(asyncio.to_thread(tmp.remove))


@dataclass(frozen=True)
class Submitted:
    state: Literal["started", "queued", "refused"]
    reason: RefuseReason | None = None
    kind: task_manager_dirs.Kind | None = None  # the directory refusal's sub-type, typed; not on the event payload


@dataclass
class _Request:
    task_id: str
    text: str
    message_id: str
    sink: TurnSink
    history: Sequence[Mapping[str, Any] | str]
    admitted: asyncio.Future[Submitted]
    cancel: TurnCancel = field(default_factory=TurnCancel)


class _InitMismatch(Exception):
    """A turn's ``system/init`` lists other tools or another permission mode than the verified launch has."""

    def __init__(self, mismatches: list[task_manager_readback.Mismatch], *, alert: bool = True) -> None:
        super().__init__("init mismatch")
        self.mismatches, self.alert = mismatches, alert


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
        # What the direct session's launch command carries (``task_manager_sandbox``): the network allowlist (set by
        # the network policy, empty until then) and the read deny list. The readback builds its expected values
        # from these same two attributes, so the command and the readback cannot disagree.
        self.allowed_domains: tuple[str, ...] = ()
        self.read_deny: tuple[str, ...] = ()
        self.init_tools: frozenset[str] | None = None  # the MCP tools the readback saw: what a turn's init must list
        self.started_for_turn: Literal["launch", "resume"] | None = None  # what the turn's ``prepare`` started
        self.work_dir = ""
        self.dir_lock: task_manager_dirs.DirectoryLock | None = None
        # The running direct launch's private temporary directory: the one source of ``CLAUDE_CODE_TMPDIR`` for the
        # process environment and for the readback's expected write set.
        self.tmpdir: task_manager_tmpdir.PrivateTmpdir | None = None
        # The project instruction files the last direct launch could not read, and the set last reported to the user.
        self.instructions_skipped: tuple[task_manager_project_instructions.Skipped, ...] = ()
        self.instructions_notified: frozenset[tuple[str, str]] = frozenset()

    def _cli_environment(self, launch: Launch) -> dict[str, str]:
        """The CLI's environment, with the running direct launch's temporary directory once it has one."""
        env = launch.runtime.environment(launch)
        if self.tmpdir is not None:
            env[task_manager_tmpdir.ENV_NAME] = self.tmpdir.path
        return env

    def _writes_allowed(self, env: Mapping[str, str]) -> frozenset[str]:
        return task_manager_readback.writes_allowed(
            env.get("HOME") or str(Path.home()), os.getuid(), env.get(task_manager_tmpdir.ENV_NAME), self.read_deny)

    def _process_environment(self, launch: Launch, work_dir: str) -> dict[str, str]:
        direct = self.mode == "direct"
        env = task_manager_safety.environment(
            launch, self.task_id, drop=task_manager_sandbox.dropped_env(launch) if direct else (),
            edit_dir=self._edit_dir(launch, work_dir) if direct else None)
        if direct and self.tmpdir is not None:
            env[task_manager_tmpdir.ENV_NAME] = self.tmpdir.path
        return env

    def _edit_dir(self, launch: Launch, work_dir: str) -> Path:
        """The private looper-edit directory for a direct launch; never where the sandbox writes or cannot read."""
        cli = self._cli_environment(launch)
        # The sandbox's write set: ``.`` is the task directory, the device files are no directories.
        writable = sorted(self._writes_allowed(cli) - {".", *task_manager_sandbox.DEVICE_WRITE_PATHS})
        return task_manager_edit.edit_bin(cli, forbidden=(work_dir, *writable, *self.read_deny))

    async def _notice_skipped(self, sink: TurnSink, message_id: str) -> None:
        """Tell the user which project instruction files were not read, when that set changes; never in a model's input."""
        current = frozenset((item.name, item.reason.value) for item in self.instructions_skipped)
        if current and current != self.instructions_notified:
            await sink.event(TaskManagerEvent.SANDBOX_NOTICE, {
                "notice": task_manager_project_instructions.SKIPPED_NOTICE,
                "detail": "; ".join(f"{name}: {reason}" for name, reason in sorted(current)),
                "message_id": message_id})
        self.instructions_notified = current

    def _mcp_cwd(self) -> Path:
        return self.pool.runtime_root / "task-managers" / self.task_id / "mcp-cwd"

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
        phase: Literal["launch", "resume"] = "resume" if self.session_id else "launch"
        if direct and (unsupported := task_manager_sandbox.platform_problem()) is not None:
            task_manager_safety.record_refusal(task_manager_safety.Reason.SANDBOX_UNAVAILABLE, phase)
            raise Refused(RefuseReason.SANDBOX_UNAVAILABLE, unsupported)
        self.project_id, self.allowed_domains = project["id"], ()
        if direct:
            try:
                self.allowed_domains = await task_manager_network.allowed_domains(project["id"])
            except task_manager_network.PolicyInvalid as exc:
                task_manager_safety.record_refusal(task_manager_safety.Reason.NETWORK_POLICY_INVALID, phase)
                raise Refused(RefuseReason.NETWORK_POLICY_INVALID,
                              f"The project's task Manager network allowlist has an entry this version refuses "
                              f"({exc}). Fix it with projects.task_manager_sandbox_update.") from exc
        async def configure() -> Launch:
            library, hook = EMPTY_LIBRARY, None
            if self.pool.ops is not None:
                if self.token is None:
                    self.token = self.pool.ops.issue(self.task_id)
                library = await self.pool.ops.library_with_server(library, self.pool.runtime_root, self.token)
            # A direct session carries no Looper hook: the sandbox is its boundary (``task_manager_sandbox``).
            if not direct and outbound_hook.known_enabled():
                try:
                    hook = (self.pool.ops.tool_hook(self.token, self.task_id, self.pool.runtime_root,
                                                   self.pool.claude_bin, library) if self.pool.ops else None)
                except Exception as exc:
                    raise Refused(RefuseReason.OUTBOUND_REVIEW_UNAVAILABLE, "The outbound review hook is unavailable.") from exc
                if hook is None:
                    raise Refused(RefuseReason.OUTBOUND_REVIEW_UNAVAILABLE, "The outbound review hook is unavailable.")
            run = Launch(settings=settings, library=library, role=None,
                         access=Access.FULL if direct else Access.READ_ONLY, tool_hook=hook)
            if direct:
                allowed = task_conversations_settings.manager_mcp()
                try:
                    # Every stdio server, Looper's own included, starts through ``operator_mcp.wrap``.
                    run = replace(run, library=await operator_mcp.library_with_allowed(
                        library, self.pool.runtime_root, allowed,
                        environment=run.runtime.environment(run),
                        cwd=self._mcp_cwd()))
                except operator_mcp.UnresolvedServers as exc:
                    raise Refused(RefuseReason.MCP_UNRESOLVED, str(exc)) from exc
                except operator_mcp.UnsafeLauncher as exc:
                    task_manager_safety.record_refusal(task_manager_safety.Reason.MCP_LAUNCHER_UNSAFE, phase)
                    raise Refused(RefuseReason.MCP_LAUNCHER_UNSAFE, str(exc)) from exc
                if run.library.mcp_config and run.library.mcp_config != library.mcp_config:
                    self.mcp_files.add(run.library.mcp_config)
                self.mcp_fingerprints = operator_mcp.fingerprints(run.library, allowed, self._mcp_cwd().resolve())
            else:
                self.mcp_fingerprints = {}
            return run
        # Direct mode: the directory admission runs before any file of that directory is read.
        launch = await configure()

        def live_direct() -> list[tuple[str, str]]:
            return [(agent.task_id, agent.work_dir) for agent in self.pool.agents.values()
                    if agent is not self and agent.resident.started and agent.mode == "direct"]

        if direct:
            # Filesystem calls run in a thread so a slow mount does not stall the pool's event loop.
            problem, checked = await asyncio.to_thread(
                task_manager_dirs.inspect, task, reopened.work_dir, environment=launch.runtime.environment(launch),
                runtime_root=self.pool.runtime_root, live=live_direct())
            if problem is not None:
                task_manager_safety.record_refusal(task_manager_safety.Reason(problem.reason.value), phase)
                raise Refused(RefuseReason(problem.reason.value), problem.text, problem.kind)
            self.work_dir = reopened.work_dir
        # Query newest summaries directly, so a long-lived task never gets its oldest activity as its brief.
        from ...db.connection import get_pool
        # Denial records are for the operator: the model's brief is unchanged by them.
        recent = await get_pool().fetch("SELECT summary FROM activity WHERE task_id=$1 AND kind <> 'sandbox_denial' "
                                        "ORDER BY seq DESC LIMIT 20", self.task_id)
        # Direct mode reads the project's instruction files every turn; the orchestrated prompt stays as it was.
        loaded, skipped = task_manager_project_instructions.read(reopened.work_dir) if direct else ([], [])
        self.instructions_skipped = tuple(skipped)
        conventions = task_manager_project_instructions.section(loaded) if direct else ""
        prompt = SystemPrompt(instructions(self.mode) + "\n\n" + brief(task, recent_activity=list(reversed(recent)),
                                                                    reopened=reopened)
                              + (f"\n\n{conventions}" if conventions else ""))
        root = self.pool.runtime_root / "task-managers" / self.task_id
        prompt_file = prompt.write_file(root)

        executable = launch.executable(self.pool.claude_bin)

        def command_for(launch: Launch) -> list[str]:
            if direct:
                self.read_deny = task_manager_sandbox.read_deny_paths(launch.runtime.environment(launch),
                                                                      self.pool.runtime_root, self.pool.config_file)
            return (launch.runtime.operator_resident_command(executable, cwd=reopened.work_dir, prompt_file=prompt_file,
                                                             launch=launch, settings_dir=root / "settings",
                                                             allowed_domains=self.allowed_domains,
                                                             read_deny=self.read_deny)
                    if direct else launch.runtime.resident_command(executable, cwd=reopened.work_dir,
                                                                   prompt_file=prompt_file, launch=launch))

        command = command_for(launch)
        # Fails a bad edit directory before any running process is replaced.
        env = self._process_environment(launch, reopened.work_dir)
        # The file's name is the SHA-256 of its content, so the signature covers the pinned values, the network
        # allowlist and the read deny list: a change in any of them restarts the session.
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
            self.started_for_turn = None
            if restarting:
                self.started_for_turn = "resume" if self.session_id else "launch"
                await self.resident.stop()
                if direct:  # a fresh, empty start directory for the servers of the new process
                    await asyncio.to_thread(operator_mcp.reset_cwd, self._mcp_cwd())
                launch = await configure()
                command = command_for(launch)
                if direct:  # removed by ``_TaskResident.stop``, which every way a session ends goes through
                    try:
                        self.tmpdir = task_manager_tmpdir.create()
                    except task_manager_tmpdir.Unusable as exc:
                        task_manager_safety.record_refusal(task_manager_safety.Reason.PRIVATE_TMPDIR_UNUSABLE, phase)
                        await self.resident.stop(kill=True)
                        raise Refused(RefuseReason.PRIVATE_TMPDIR_UNUSABLE, str(exc)) from exc
                env = self._process_environment(launch, reopened.work_dir)
                if direct and (warning := task_manager_sandbox.credential_warning(env)):
                    logger.warning("task Manager %s: %s", self.task_id, warning)
                settings_file = command[command.index("--settings") + 1] if "--settings" in command else ""
                if direct:
                    problem = await task_manager_safety.gate(executable, cwd=reopened.work_dir, environment=self._cli_environment(launch),
                        phase=phase, settings_file=Path(settings_file), review_hook=False)
                    if problem is not None:
                        await self.resident.stop(kill=True)
                        raise Refused(RefuseReason(problem.reason.value), problem.notice)
                    if self.dir_lock is not None:
                        self.dir_lock.release()
                        self.dir_lock = None
                    # The lock is taken first; then the directory the lock was taken on must be the checked one, and
                    # the whole check runs again (including the live-session overlap). The process is then started
                    # from the path: a same-user writer swapping the path after this point is not detected (RFC 25.5).
                    lock, problem = await task_manager_dirs.admit_off_loop(
                        task, reopened.work_dir, self.task_id, environment=launch.runtime.environment(launch),
                        runtime_root=self.pool.runtime_root, live=live_direct(), expected=checked)
                    if problem is not None:
                        task_manager_safety.record_refusal(task_manager_safety.Reason(problem.reason.value), phase)
                        await self.resident.stop(kill=True)
                        raise Refused(RefuseReason(problem.reason.value), problem.text, problem.kind)
                    self.dir_lock = lock
                if self.session_id:
                    command += ["--resume", self.session_id]
                await self.resident.start(command, cwd=reopened.work_dir, signature=signature, env=env,
                                          totals=self.totals, price=launch.price, capture_stderr=True)
            self.last_used = self.pool.clock()
        return task, reopened, launch, prompt

    async def _verify(self, launch: Launch, reopened: Any) -> None:
        """Read back the CLI's effective settings before the turn's user message is written (direct sessions): any
        difference from what the launch injected, or a readback that cannot be done, refuses the turn and ends the
        process. At a launch or resume it is the first thing after the process starts."""
        if self.mode != "direct":
            return
        phase = self.started_for_turn or "turn"
        env = self._cli_environment(launch)
        if self.tmpdir is None:  # a direct process is only ever started with one
            raise RuntimeError("the direct session has no private temporary directory")
        work_dir = Path(reopened.work_dir)
        shared = task_manager_sandbox.shared_write_roots(env)
        private_dir = (self.pool.runtime_root / "task-managers" / self.task_id / "mcp-cwd").resolve()  # as launch wrapped it
        expected = task_manager_readback.Expected(
            private_dir=private_dir,
            pins=task_manager_sandbox.pins(work_dir, allowed_domains=self.allowed_domains, read_deny=self.read_deny,
                                           write_deny=shared),
            fs_allow_write=frozenset(),
            writes_allowed=self._writes_allowed(env),
            temp_root=self.tmpdir.write_root, shared_writes=frozenset(shared), uid=os.getuid(),
            mcp=task_manager_readback.expected_mcp(library_mcp_servers(launch.library), private_dir),
            known_tools=task_manager_readback.KNOWN_BUILTIN_TOOLS[task_manager_safety.VERIFIED_CLI_VERSION])
        self.init_tools = None
        try:
            readback = await task_manager_readback.read(self.resident, phase)
            mismatches = task_manager_readback.compare(readback, expected)
        except task_manager_readback.ReadbackUnavailable as exc:
            if phase == "resume" and not self.resident.running:
                return  # a resume that exited at once is the turn's own ``_ResumeFailed`` (a fresh session follows)
            mismatches = [task_manager_readback.Mismatch(task_manager_safety.Reason.READBACK_UNAVAILABLE, exc.item,
                                                         None, None, task_manager_readback.Source.UNKNOWN)]
        if not mismatches:
            self.init_tools = task_manager_readback.mcp_tool_names(readback)
            return
        await self._refuse(mismatches, phase)

    async def _refuse(self, mismatches: Sequence[task_manager_readback.Mismatch], phase: str, *,
                      alert: bool = True) -> None:
        """End the process, count and alert, and refuse with the first mismatch's reason."""
        reason = mismatches[0].reason
        task_manager_safety.record_refusal(reason, phase, task_manager_safety.VERIFIED_CLI_VERSION)
        await self.resident.stop(kill=True)
        if alert and reason is not task_manager_safety.Reason.SANDBOX_UNAVAILABLE:
            await task_manager_alerts.invariant_violation(self.task_id, reason.value, mismatches[0].item)
        raise Refused(RefuseReason(reason.value),
                      task_manager_readback.notice(mismatches, task_manager_safety.VERIFIED_CLI_VERSION))

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
        await self._verify(launch, reopened)
        # A worktree task never gets here in direct mode: ``task_manager_dirs.check`` refuses it before any launch
        # (``worktree_unsupported``), so the worktree conditions below are unreachable guards in v1.
        # An in-place direct session writes the real checkout: record its branches and tags and settle the task branch
        # (a detached or moved HEAD refuses here, before the message is admitted).
        refs_before, task_branch = ((await task_manager_refs.begin_turn(task, reopened.work_dir, sink=request.sink, phase="turn"))
                                    if self.mode == "direct" and task.get("workspace_mode") != "worktree" else (None, ""))
        if not request.admitted.done():
            request.admitted.set_result(Submitted("started"))
        # After the readback and every gate that can refuse the turn: a refused turn does not use up the notice.
        await self._notice_skipped(request.sink, request.message_id)
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
                listed: frozenset[str] | None = None  # the tools of this turn's passing ``system/init`` (direct)
                pieces: list[str] = []
                tools: dict[str, ToolRecord] = {}
                inputs: dict[str, Any] = {}
                observer = task_manager_denials.Observer(self.task_id, self.project_id, request.message_id,
                                                         self.allowed_domains, request.sink)

                async def on_event(event: dict[str, Any]) -> None:
                    nonlocal initialized, listed
                    await recorder.record(event)
                    gated = self.mode == "direct" and self.init_tools is not None
                    if event.get("type") == "system" and event.get("subtype") == "init":
                        if gated and (bad := task_manager_readback.check_init(event, self.init_tools)):
                            raise _InitMismatch(bad)  # before anything of this turn is delivered
                        listed = frozenset(name for name in event.get("tools") or () if isinstance(name, str))
                    elif gated and listed is None and event.get("type") in ("assistant", "stream_event", "result"):
                        raise _InitMismatch(task_manager_readback.init_missing())  # nothing before a passing init
                    block = ((event.get("event") or {}).get("content_block") or {}
                             if event.get("type") == "stream_event"
                             and (event.get("event") or {}).get("type") == "content_block_start" else {})
                    if gated and listed is not None and isinstance(block, dict) and block.get("type") == "tool_use" and (
                            bad := task_manager_readback.unlisted_tool(str(block.get("name") or ""), listed)):
                        raise _InitMismatch(bad)  # a tool call that starts as a partial message of the stream
                    if gated and listed is not None and event.get("type") == "assistant":
                        for block in (event.get("message") or {}).get("content") or ():
                            if isinstance(block, dict) and block.get("type") == "tool_use" and (
                                    bad := task_manager_readback.unlisted_tool(str(block.get("name") or ""), listed)):
                                raise _InitMismatch(bad)  # a tool the init did not list
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
                            inputs[record.tool_use_id] = block.get("input")
                            await request.sink.tool(record)
                        elif block.get("type") == "tool_result" and block.get("tool_use_id") in tools:
                            record = replace(tools[block["tool_use_id"]],
                                result=json.dumps(block.get("content"), ensure_ascii=False)[:200],
                                is_error=bool(block.get("is_error")))
                            await request.sink.tool(record)
                            if self.mode == "direct" and record.name == "Bash":
                                await observer.observe(record.tool_use_id, inputs.get(record.tool_use_id),
                                                       block.get("content"), record.is_error,
                                                       event.get("tool_result_meta"))

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
                        if (self.mode == "direct" and self.init_tools is not None and listed is None
                                and result.prompt_sent and request.cancel.requested):
                            raise _InitMismatch(task_manager_readback.init_missing(), alert=False)  # stopped before init
                        if resuming and not initialized and not request.cancel.requested:
                            retry_detail = result.stderr or await self.resident.stderr_tail()
                            raise _ResumeFailed(retry_detail or "Claude Code exited before init")
                    except _InitMismatch as exc:
                        await self._refuse(exc.mismatches, "turn", alert=exc.alert)
                    except _ResumeFailed as exc:
                        if attempt or request.cancel.requested:
                            raise
                        await self.resident.stop(kill=True)
                        self.session_id, self.totals = None, Usage()
                        await request.sink.event(TaskManagerEvent.SESSION_FRESH,
                                                {"reason": "resume_failed", "detail": str(exc)})
                        # Re-run all configuration and review gates before the one fresh attempt.
                        task, reopened, launch, prompt = await self.prepare()
                        await self._verify(launch, reopened)
                        await self._notice_skipped(request.sink, request.message_id)
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
            if refs_before is not None:
                try:
                    await task_manager_refs.end_turn(task, reopened.work_dir, refs_before, task_branch, sink=request.sink,
                                                     message_id=request.message_id)
                except Exception as exc:  # the turn's own outcome stands; the missing report is recorded
                    logger.warning("task Manager %s: ref report failed", self.task_id, exc_info=True)
                    await activity.insert_activity(task_id=self.task_id, kind="error",
                        summary=f"Ref report failed: {type(exc).__name__}: {exc}"[:300])
        from . import task_manager_context
        await task_manager_context.maintain(self, request, reopened, launch, prompt, record_id)
        await request.sink.final(text, outcome)


_pool: TaskManagerPool | None = None


def current_pool() -> TaskManagerPool | None:
    return _pool


class TaskManagerPool:
    def __init__(self, runtime_root: Path, claude_bin: str, *, ops: Any = None,
                 max_resident: Callable[[], int], clock: Callable[[], float] = time.monotonic,
                 config_file: Path | None = None) -> None:
        global _pool
        self.runtime_root, self.claude_bin, self.ops = runtime_root, claude_bin, ops
        self.config_file = config_file  # Looper's ops file when it lives outside the runtime root (read deny list)
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

    async def _refuse(self, request: _Request, reason: RefuseReason, text: str,
                      kind: task_manager_dirs.Kind | None = None) -> None:
        if not request.admitted.done():
            request.admitted.set_result(Submitted("refused", reason, kind))
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
            await self._refuse(request, exc.reason, str(exc), exc.kind)
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
