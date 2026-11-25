"""Task-owned Claude Code residents, separate from the operator's Manager."""

from __future__ import annotations

import asyncio
import inspect
import time
from collections.abc import Callable, Mapping
from enum import Enum
from pathlib import Path
from typing import Any

from ...db import activity, projects, tasks
from .. import outbound_hook, task_conversations_settings, task_manager_safety, task_manager_settings, task_workspaces
from ..agent_settings import Access, Launch
from ..claude_cli import ResidentClaude
from ..library import EMPTY_LIBRARY
from ..project_execution import ProjectExecution
from ..run_usage import Usage
from ..system_prompt import SystemPrompt
from ..task_manager_brief import brief, instructions


class RefuseReason(str, Enum):
    WORK_MODE_OFF = "work_mode_off"
    RUNTIME_UNSUPPORTED = "runtime_unsupported"
    BUDGET = "budget"
    OUTBOUND_REVIEW_UNAVAILABLE = "outbound_review_unavailable"
    WORKSPACE = "workspace"
    SHUTTING_DOWN = "shutting_down"
    REVIEW_HOOK_DISABLED = "review_hook_disabled"
    CLI_UNVERIFIED = "cli_unverified"


class Refused(Exception):
    def __init__(self, reason: RefuseReason, text: str) -> None:
        super().__init__(text)
        self.reason = reason


class TaskManagerAgent:
    def __init__(self, pool: TaskManagerPool, task_id: str) -> None:
        self.pool, self.task_id = pool, task_id
        self.resident = ResidentClaude(label=f"task-manager-{task_id}")
        self.last_used = pool.clock()
        self.busy = False
        self.session_id: str | None = None
        self.totals = Usage()

    async def prepare(self) -> tuple[Mapping[str, Any], Any, Launch, SystemPrompt]:
        """Read configuration afresh; no launch bypasses the direct-mode review checks."""
        if not task_conversations_settings.enabled():
            raise Refused(RefuseReason.WORK_MODE_OFF, "Task conversations are disabled.")
        if self.pool.closing:
            raise Refused(RefuseReason.SHUTTING_DOWN, "The task Manager is shutting down.")
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
        library = getattr(self.pool.ops, "library", EMPTY_LIBRARY)
        hook = None
        if outbound_hook.known_enabled():
            try:
                hook = self.pool.ops.tool_hook(self.task_id, runtime_root=self.pool.runtime_root,
                                               claude_bin=self.pool.claude_bin) if self.pool.ops else None
                if inspect.isawaitable(hook):
                    hook = await hook
            except Exception as exc:
                raise Refused(RefuseReason.OUTBOUND_REVIEW_UNAVAILABLE, "The outbound review hook is unavailable.") from exc
            if hook is None:
                raise Refused(RefuseReason.OUTBOUND_REVIEW_UNAVAILABLE, "The outbound review hook is unavailable.")
        launch = Launch(settings=settings, library=library, role=None,
                        access=Access.FULL if direct else Access.READ_ONLY, tool_hook=hook)
        # Query newest summaries directly, so a long-lived task never gets its oldest activity as its brief.
        from ...db.connection import get_pool
        recent = await get_pool().fetch("SELECT summary FROM activity WHERE task_id=$1 ORDER BY seq DESC LIMIT 20",
                                        self.task_id)
        prompt = SystemPrompt(instructions(self.mode) + "\n\n" + brief(task, recent_activity=list(reversed(recent)),
                                                                    reopened=reopened))
        root = self.pool.runtime_root / "task-managers" / self.task_id
        prompt_file = prompt.write_file(root)
        executable = launch.executable(self.pool.claude_bin)
        command = (launch.runtime.operator_resident_command(executable, cwd=reopened.work_dir, prompt_file=prompt_file,
                                                            launch=launch, settings_dir=root / "settings")
                   if direct else launch.runtime.resident_command(executable, cwd=reopened.work_dir,
                                                                  prompt_file=prompt_file, launch=launch))
        env = task_manager_safety.environment(launch, self.task_id)
        settings_file = command[command.index("--settings") + 1] if "--settings" in command else ""
        signature = (self.mode, reopened.work_dir, launch.profile.id, launch.model, launch.effort, prompt.sha256, settings_file)
        restarting = not self.resident.started or self.resident.stale(signature)
        if restarting:
            if direct:
                problem = await task_manager_safety.gate(executable, cwd=reopened.work_dir, environment=launch.runtime.environment(launch),
                    phase="resume" if self.session_id else "launch", settings_file=Path(settings_file))
                if problem is not None:
                    await self.resident.stop(kill=True)
                    raise Refused(RefuseReason(problem.reason.value), problem.notice)
            await self.resident.stop()
            if self.session_id:
                command += ["--resume", self.session_id]
            await self.resident.start(command, cwd=reopened.work_dir, signature=signature, env=env,
                                      totals=self.totals, price=launch.price)
        return task, reopened, launch, prompt


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
        self._ticker = asyncio.create_task(self._tick(), name="task-manager-reclaim")
        _pool = self

    def agent(self, task_id: str) -> TaskManagerAgent:
        if task_id not in self.agents:
            self.agents[task_id] = TaskManagerAgent(self, task_id)
        return self.agents[task_id]

    def in_turn(self, task_id: str) -> bool:
        agent = self.agents.get(task_id)
        return bool(agent and agent.busy)

    async def _slot(self, agent: TaskManagerAgent) -> bool:
        """Called under the pool gate; an idle eviction never races a turn admission."""
        if agent.resident.started:
            return True
        live = sum(a.resident.started for a in self.agents.values())
        if live < self.max_resident():
            return True
        for other in sorted(self.agents.values(), key=lambda a: a.last_used):
            if not other.busy and await other.resident.try_stop_if_idle():
                return True
        return False

    async def reclaim(self) -> None:
        async with self._lock:
            for agent in self.agents.values():
                if not agent.busy and self.clock() - agent.last_used >= 1800:
                    await agent.resident.try_stop_if_idle()

    async def _tick(self) -> None:
        while True:
            await asyncio.sleep(60)
            await self.reclaim()

    async def close(self, task_id: str) -> None:
        agent = self.agents.get(task_id)
        if agent:
            await agent.resident.stop(kill=True)

    async def forget(self, task_id: str) -> None:
        await self.close(task_id)
        self.agents.pop(task_id, None)
        from ...db.connection import get_pool
        await get_pool().execute("DELETE FROM agent_sessions WHERE agent_id=$1", f"task-manager-{task_id}")

    async def shutdown(self) -> None:
        global _pool
        self.closing = True
        self._ticker.cancel()
        await asyncio.gather(self._ticker, return_exceptions=True)
        await asyncio.gather(*(a.resident.stop(kill=True) for a in self.agents.values()))
        if _pool is self:
            _pool = None
