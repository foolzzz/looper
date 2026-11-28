"""Persistent, bounded explorer runs. No tool or agent entry point is installed here.

Admission and reservation release serialize on the task row. The spawn row is
its reservation until terminal, and its idempotency key survives every restart.
Recovery only finishes, adopts or cancels existing runs; it never launches one.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math

import asyncpg
from collections.abc import Mapping
from contextlib import AsyncExitStack
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path
from typing import Any

from ..db import agent_spawns, llm_records, role_configs
from ..db.connection import get_pool
from ..db.agent_spawns import FINISHED, CancelReason, SpawnState
from ..db.tasks import ACTIVE_STATUSES
from . import admission, git_jail, spawn
from .agent_settings import Access, AgentType, Launch, load_agent_settings
from .run_usage import CostStatus, cost_status
from .runtimes import ReadOnly
from .spawn import ChildFailure, ChildResult, ChildRun, MIN_RUN_USD, SpawnKind, SpawnParent

logger = logging.getLogger(__name__)


class RefusalReason(StrEnum):
    KIND = "kind"
    ROLE = "role"
    BRIEF = "brief"
    SCOPE = "scope"
    PROFILE = "profile"
    NO_CHECKOUT = "no_checkout"
    LIMIT_RUN = "limit_run"
    LIMIT_TASK = "limit_task"
    BUDGET = "budget"
    BREAKER = "breaker"
    NESTING = "nesting"
    OFF = "off"


class Delivery(StrEnum):
    TOOL = "tool"
    ROOM = "room"
    TURN = "turn"


class Refused(ValueError):
    def __init__(self, reason: RefusalReason, detail: str):
        self.reason = reason
        super().__init__(detail)


class Unavailable(RuntimeError):
    """A failed reservation can be retried with the same key."""


@dataclass(frozen=True)
class Spawner:
    parent: SpawnParent
    role: str
    work_dir: Path
    seat: str = ""
    writable_turn: bool = True
    # The caller supplies live streamed spend/cap, never a model argument.
    budget_usd: float | None = None
    spent_usd: float | None = None
    # Spend the parent will still add to the task but has not recorded (a running Planner turn's cost is written
    # when its record ends): counted against the task's remaining budget even when its own spend is unknown.
    unrecorded_usd: float = 0.0


def _brief(value: Any, directory: Path) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) - {"goal", "deliverable", "scope", "questions"}:
        raise Refused(RefusalReason.BRIEF, "brief must contain goal, deliverable, scope and optional questions")
    for name, maximum in (("goal", 2000), ("deliverable", 2000)):
        if not isinstance(value.get(name), str) or not 1 <= len(value[name].strip()) <= maximum:
            raise Refused(RefusalReason.BRIEF, f"{name} must contain 1 to {maximum} characters")
    scope = value.get("scope")
    if not isinstance(scope, list) or not 1 <= len(scope) <= 32:
        raise Refused(RefusalReason.BRIEF, "scope must contain 1 to 32 relative paths")
    root = directory.resolve()
    for item in scope:
        if (not isinstance(item, str) or not 1 <= len(item) <= 500 or Path(item).is_absolute()
                or ".." in Path(item).parts or not (root / item).resolve().is_relative_to(root)):
            raise Refused(RefusalReason.SCOPE, "scope must stay within the spawner's directory")
    questions = value.get("questions", [])
    if (not isinstance(questions, list) or len(questions) > 20
            or any(not isinstance(q, str) or not 1 <= len(q) <= 1000 for q in questions)):
        raise Refused(RefusalReason.BRIEF, "questions must contain at most 20 strings of 1 to 1000 characters")
    return dict(value)


def _number(value: Any) -> bool:
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def _unknown_models(record: Mapping[str, Any] | None) -> str:
    record = record or {}
    metadata = record.get("metadata") or {}
    if isinstance(metadata, str):
        metadata = json.loads(metadata)
    return ", ".join(metadata.get("unknown_price_models") or
                     [metadata.get("model_reported") or record.get("model") or "unknown"])


class SpawnService:
    def __init__(self, runtime_root: Path, *, pool: Any = None, claude_bin: str = "claude",
                 keeper: Any = None, on_stale: Any = None, stop_parent: Any = None):
        self.runtime_root = runtime_root
        self.pool = pool or get_pool()
        self.claude_bin = claude_bin
        self.keeper = keeper
        self.on_stale = on_stale or (lambda reason: None)
        self.stop_parent = stop_parent
        self._running: dict[int, asyncio.Task] = {}
        self._handles: dict[int, Any] = {}
        self._children: dict[int, asyncio.Task] = {}
        self._homes: dict[int, str] = {}

    def owner_of(self, spawn_id: int) -> str | None:
        return self._homes.get(spawn_id)

    async def admit(self, caller: Spawner, brief: Any, *, key: str, kind: str = "explorer",
                    profile: str | None = None, budget_usd: float | None = None,
                    delivery: Delivery = Delivery.TOOL) -> dict[str, Any]:
        """Validate and reserve once, committing before a process can start."""
        try:
            return await self._admit(caller, brief, key=key, kind=kind, profile=profile,
                                     budget_usd=budget_usd, delivery=Delivery(delivery))
        except Refused as exc:
            from ..db import activity
            if caller.parent.task_id:
                try:
                    await activity.insert_activity(task_id=caller.parent.task_id,
                        worker=caller.seat or caller.role, kind="subagent_refused", summary=str(exc),
                        metadata={"reason": exc.reason.value}, pool=self.pool)
                except (asyncpg.PostgresError, asyncpg.InterfaceError, TimeoutError, OSError):
                    logger.warning("could not record explorer refusal", exc_info=True)
            raise
        except (asyncpg.PostgresError, asyncpg.InterfaceError, TimeoutError, OSError) as exc:
            raise Unavailable("subagent reservation unavailable; retry with the same key") from exc

    async def _admit(self, caller: Spawner, brief: Any, *, key: str, kind: str, profile: str | None,
                     budget_usd: float | None, delivery: Delivery) -> dict[str, Any]:
        if caller.role == "explorer":
            raise Refused(RefusalReason.NESTING, "explorers cannot spawn children")
        if caller.role not in ("planner", "worker"):
            raise Refused(RefusalReason.ROLE, "only Planner and Worker runs may spawn explorers")
        if kind != "explorer":
            raise Refused(RefusalReason.KIND, "only explorer is supported")
        if caller.role == "planner" and not caller.writable_turn:
            raise Refused(RefusalReason.ROLE, "Planner writes require a Looper-issued turn")
        brief = _brief(brief, caller.work_dir)
        if not isinstance(key, str) or not 1 <= len(key) <= 200:
            raise Refused(RefusalReason.BRIEF, "idempotency key must contain 1 to 200 characters")
        if budget_usd is not None and (not _number(budget_usd) or not .01 <= budget_usd <= 5):
            raise Refused(RefusalReason.BUDGET, "budget_usd must be between 0.01 and 5")
        if any(value is not None and not _number(value) for value in (caller.budget_usd, caller.spent_usd)):
            raise Refused(RefusalReason.BUDGET, "invalid parent budget or spend")
        task = await self.pool.fetchrow("SELECT * FROM tasks WHERE id = $1", caller.parent.task_id)
        parent = await llm_records.get_record(caller.parent.record_id, pool=self.pool) if caller.parent.record_id else None
        if (not task or not parent or parent["task_id"] != caller.parent.task_id
                or (parent.get("run_kind") or llm_records.run_kind_of(parent["agent_id"])) != caller.role
                or parent["status"] != "running" or task["status"] not in ACTIVE_STATUSES):
            raise Refused(RefusalReason.ROLE, "the spawner must be a running parent in a running task")
        project = dict(await self.pool.fetchrow("SELECT * FROM projects WHERE id = $1", task["project_id"]))
        if project.get("subagents") != "on":
            raise Refused(RefusalReason.OFF, "subagents are off for this project")
        # Return the first request unchanged, even if its arguments or available budget changed.
        if previous := await agent_spawns.by_key(caller.parent.record_id, key, pool=self.pool):
            return previous
        if profile is not None:
            from .projects import choosable_profiles
            allowed, _ = await choosable_profiles(task["project_id"])
            chosen = next((p for p in allowed if profile in (p["id"], p["name"])), None)
            if chosen is None:
                raise Refused(RefusalReason.PROFILE, "profile is not allowed by the project")
            profile = chosen["id"]
        settings = await load_agent_settings(AgentType.EXPLORER, task["project_id"], profile_id=profile)
        from .projects import choosable_profiles
        allowed, restricted = await choosable_profiles(task["project_id"], pool=self.pool)
        if restricted and settings.profile.id not in {item["id"] for item in allowed}:
            raise Refused(RefusalReason.PROFILE, "the explorer profile is not allowed by this project")
        config = await role_configs.get_config("explorer", pool=self.pool)
        if config is not None and not config.get("enabled", True):
            raise Refused(RefusalReason.PROFILE, "the explorer role is disabled")
        mode = None
        try:
            launch = Launch(settings, role=AgentType.EXPLORER, access=Access.EXPLORE)
            if settings.error or settings.profile_error:
                raise ValueError(settings.error or settings.profile_error)
            mode = launch.read_only_mode
            if mode is ReadOnly.CHECK:
                raise ValueError("runtime cannot enforce read-only exploration")
            if mode is ReadOnly.TOOLS:
                from .explorer_checkout import repository
                await asyncio.to_thread(repository, caller.work_dir)
        except Exception as exc:
            reason = RefusalReason.NO_CHECKOUT if mode is ReadOnly.TOOLS else RefusalReason.PROFILE
            raise Refused(reason, str(exc)) from exc
        async with agent_spawns.task_lock(caller.parent.task_id, pool=self.pool) as conn:
            if previous := await agent_spawns.by_key(caller.parent.record_id, key, pool=conn):
                return previous
            # The lock also orders task stop and the reservation's release.
            if await conn.fetchval("SELECT status FROM tasks WHERE id = $1", caller.parent.task_id) not in ACTIVE_STATUSES:
                raise Refused(RefusalReason.ROLE, "task ended before reservation")
            usage = await agent_spawns.commitment(caller.parent.task_id, caller.parent.record_id, pool=conn)
            if usage["parent_count"] >= (project.get("subagents_per_run") if project.get("subagents_per_run") is not None else 4):
                raise Refused(RefusalReason.LIMIT_RUN, "per-run explorer limit reached")
            if usage["task_count"] >= (project.get("subagents_per_task") if project.get("subagents_per_task") is not None else 20):
                raise Refused(RefusalReason.LIMIT_TASK, "per-task explorer limit reached")
            requested = budget_usd if budget_usd is not None else (project.get("subagent_budget_usd") or settings.max_budget_usd or .5)
            limits = [requested, (project.get("subagent_task_budget_usd") or 3) - usage["task_committed"]]
            if caller.budget_usd is not None:
                if cost_status(caller.spent_usd) is CostStatus.UNKNOWN:
                    logger.warning("model %s: parent spend cap cannot be verified: cost unknown (record %s)",
                                   _unknown_models(parent), caller.parent.record_id)
                else:
                    limits.append(caller.budget_usd - caller.spent_usd - usage["parent_committed"])
            if project.get("task_budget_usd") is not None:
                costs = await conn.fetchrow(
                    f"SELECT COALESCE(sum(cost_usd), 0) AS spent, array_agg(DISTINCT COALESCE("
                    f"NULLIF(metadata->>'unknown_price_models', '[]'), metadata->>'model_reported', model, 'unknown')) "
                    f"FILTER (WHERE cost_usd IS NULL AND status <> 'running') AS unknown_models "
                    f"FROM llm_records WHERE task_id = $1 AND {llm_records.TASK_SPEND}", caller.parent.task_id)
                if costs["unknown_models"]:
                    logger.warning("models %s: task spend cap cannot be verified: cost unknown (task %s)",
                                   ", ".join(sorted(costs["unknown_models"])), caller.parent.task_id)
                limits.append(project["task_budget_usd"] - float(costs["spent"]) - usage["task_reserved"]
                              - caller.unrecorded_usd)
            budget = min(limits)
            if budget < MIN_RUN_USD - 1e-9:
                raise Refused(RefusalReason.BUDGET, "less than the minimum explorer budget remains")
            if usage["consecutive_failures"] >= 3:
                raise Refused(RefusalReason.BREAKER, "three consecutive explorers failed")
            return await agent_spawns.create(kind="explorer", task_id=caller.parent.task_id,
                step_id=caller.parent.step_id, parent_record_id=caller.parent.record_id, brief=brief,
                inputs={"work_dir": str(caller.work_dir.resolve()), "parent_budget_usd": caller.budget_usd},
                budget_usd=budget, state=SpawnState.QUEUED, idempotency_key=key,
                spawner=caller.seat or caller.role, profile_id=settings.profile.id, delivery=delivery.value,
                parent_spend_known=caller.spent_usd is not None, pool=conn)

    async def run(self, spawn_id: int) -> dict[str, Any]:
        """Start a queued reservation once when its task has a subagent slot."""
        task = self._running.get(spawn_id)
        if task is not None:
            return await asyncio.shield(task)
        task = asyncio.create_task(self._run(spawn_id))
        self._running[spawn_id] = task
        try:
            return await asyncio.shield(task)
        finally:
            if task.done():
                self._running.pop(spawn_id, None)

    async def _run(self, spawn_id: int) -> dict[str, Any]:
        from .explorer import EXPLORER_MAX_TURNS, EXPLORER_TIMEOUT_SEC, build_explorer_prompt
        from .knowledge import load_knowledge
        from .project_execution import ProjectExecution
        from . import explorer_checkout
        row = await agent_spawns.get(spawn_id, pool=self.pool)
        if row is None or row["kind"] != "explorer":
            raise ValueError("unknown explorer spawn")
        if row["state"] != SpawnState.QUEUED.value:
            return row
        directory = Path(row["inputs"]["work_dir"])
        checkout = None
        child_task = None
        record_id = None
        admission_stack = None
        grant = None
        try:
            task_row = dict(await self.pool.fetchrow("SELECT * FROM tasks WHERE id = $1", row["task_id"]))
            project = dict(await self.pool.fetchrow("SELECT * FROM projects WHERE id = $1", task_row["project_id"]))
            settings = await load_agent_settings(AgentType.EXPLORER, task_row["project_id"], row["task_id"],
                                                 step_id=row["step_id"], profile_id=row["profile_id"])
            if settings.error or settings.profile_error:
                raise ValueError(settings.error or settings.profile_error)
            role = await role_configs.get_config("explorer", pool=self.pool) or {}
            if not role.get("enabled", True):
                raise ValueError("the explorer role is disabled")
            knowledge = await load_knowledge(self.runtime_root, project, str(directory),
                                             notes=ProjectExecution.from_row(project).notes_on)
            launch = Launch(settings, role=AgentType.EXPLORER, access=Access.EXPLORE)
            if launch.read_only_mode is ReadOnly.TOOLS:
                git_jail.add_workspace_root(self.runtime_root)
                checkout = await asyncio.to_thread(explorer_checkout.prepare, directory,
                    self.runtime_root / ".local" / "explorers" / str(spawn_id))
                directory = checkout.cwd
            step = dict(await self.pool.fetchrow("SELECT * FROM task_steps WHERE id = $1", row["step_id"]) or {})
            system = build_explorer_prompt(settings, project=project, task_id=row["task_id"],
                requirement=task_row["requirement"], step=step, knowledge=knowledge, role_config=role)
            child = ChildRun(SpawnKind.EXPLORER, system,
                f"Spawned by {row['spawner']} to investigate {row['brief']['goal']}.\n" + json.dumps(row["brief"], ensure_ascii=False),
                timeout_s=role.get("timeout_seconds") or EXPLORER_TIMEOUT_SEC,
                max_turns=role.get("max_turns") or EXPLORER_MAX_TURNS,
                role=AgentType.EXPLORER)
            self._homes[spawn_id] = child.agent_id
            if admission.enabled():
                try:
                    # Check the task class before joining an account queue. The
                    # locked launch below must still re-check after admission.
                    limit = await self.pool.fetchval("SELECT max_concurrency FROM task_concurrency_limits WHERE task_id=$1 AND role='subagent'", row["task_id"]) or 4
                    active = await self.pool.fetchval("SELECT count(*) FROM agent_spawns WHERE task_id=$1 AND kind='explorer' AND state IN ('asking','cancelling')", row["task_id"])
                except Exception:
                    logger.exception("Explorer %s task-class pre-check failed; skipping account admission", spawn_id)
                else:
                    if active >= limit:
                        return await agent_spawns.get(spawn_id, pool=self.pool)
                    _, lane = await admission.parent_for(row["parent_record_id"])

                    async def on_wait(message):
                        from ..db import activity
                        await activity.insert_activity(task_id=row["task_id"], worker=child.agent_id,
                            kind="subagent_wait", summary=f"Explorer {spawn_id}: {message}",
                            metadata={"spawn_id": spawn_id}, pool=self.pool)

                    admission_stack = AsyncExitStack()
                    grant = await admission_stack.enter_async_context(admission.slot(
                        launch.profile.provider.id, lane, owner=child.agent_id, on_wait=on_wait))
            claimed = asyncio.Event()
            launch_ready = asyncio.Event()
            async with agent_spawns.task_lock(row["task_id"], pool=self.pool) as conn:
                latest = await agent_spawns.get(spawn_id, pool=conn)
                if latest["state"] != "queued":
                    return latest
                task_status = await conn.fetchval("SELECT status FROM tasks WHERE id=$1", row["task_id"])
                subagents_on = await conn.fetchval("SELECT subagents FROM projects WHERE id=$1 FOR UPDATE", task_row["project_id"])
                if task_status not in ACTIVE_STATUSES or subagents_on != "on":
                    moved = await agent_spawns.move(spawn_id, SpawnState.CANCELLED, expect=SpawnState.QUEUED,
                        cancel_reason=CancelReason.TASK_ENDED.value, pool=conn)
                    return moved or await agent_spawns.get(spawn_id, pool=conn)
                limit = await conn.fetchval("SELECT max_concurrency FROM task_concurrency_limits WHERE task_id=$1 AND role='subagent'", row["task_id"]) or 4
                active = await conn.fetchval("SELECT count(*) FROM agent_spawns WHERE task_id=$1 AND kind='explorer' AND state IN ('asking','cancelling')", row["task_id"])
                if active >= limit:
                    return latest
                async def on_record(value):
                    nonlocal record_id
                    record_id = value
                    # Claim before OS launch: a crash in the process/identity gap
                    # must recover as an interrupted run, never launch it again.
                    await agent_spawns.move(spawn_id, SpawnState.ASKING, expect=SpawnState.QUEUED,
                        child_record_id=value, child_process={"run_id": child.agent_id}, pool=conn)
                    claimed.set()
                    # Do not start until the claim's transaction is committed.
                    await launch_ready.wait()
                async def on_process(identity):
                    if not await agent_spawns.record_process(spawn_id, identity, pool=self.pool):
                        raise RuntimeError("explorer cancelled before its first input")
                child_task = asyncio.create_task(spawn.run_child(child,
                    parent=SpawnParent(row["task_id"], row["step_id"], row["parent_record_id"]),
                    settings=settings, cap_usd=row["budget_usd"], runtime_root=self.runtime_root,
                    claude_bin=self.claude_bin, cwd=directory, on_record=on_record, on_process=on_process,
                    keeper=self.keeper, on_handle=lambda handle: self._handles.__setitem__(spawn_id, handle),
                    on_stale=self.on_stale))
                self._children[spawn_id] = child_task
                wait_started = asyncio.create_task(claimed.wait())
                try:
                    await asyncio.wait((child_task, wait_started), return_when=asyncio.FIRST_COMPLETED)
                finally:
                    wait_started.cancel()
                    await asyncio.gather(wait_started, return_exceptions=True)
            launch_ready.set()
            result = await child_task
            current = await agent_spawns.get(spawn_id, pool=self.pool)
            expected = SpawnState(current["state"])
            if expected is SpawnState.QUEUED:
                return await agent_spawns.move(spawn_id,
                    SpawnState.OVER_BUDGET if result.failure is ChildFailure.BUDGET else SpawnState.FAILED,
                    expect=expected, failure_class="judgment" if result.failure is ChildFailure.BUDGET else "environment",
                    failure_cause="budget" if result.failure is ChildFailure.BUDGET else "profile",
                    detail=result.detail, pool=self.pool)
            if expected is SpawnState.ASKING:
                await agent_spawns.move(spawn_id, SpawnState.FINISHING, expect=expected,
                    child_record_id=result.record_id, result={"child": {"output": result.output,
                    "cost_usd": result.cost_usd, "failure": result.failure.value if result.failure else None,
                    "detail": result.detail}}, pool=self.pool)
                return await self.finish(spawn_id)
            return current
        except spawn.ChildDetached:
            if grant is not None:
                # The adopter observes the eventual probe result. Detaching is
                # only a local release, not the end of this kept process.
                grant.reported = True
            return await agent_spawns.get(spawn_id, pool=self.pool)
        except asyncio.CancelledError:
            if child_task is not None:
                child_task.cancel()
                await asyncio.gather(child_task, return_exceptions=True)
            await self.cancel(spawn_id, reason=CancelReason.SERVER)
            raise
        except Exception as exc:
            logger.warning("explorer %s could not start or finish", spawn_id, exc_info=True)
            current = await agent_spawns.get(spawn_id, pool=self.pool)
            if current["state"] == "queued":
                return await agent_spawns.move(spawn_id, SpawnState.FAILED,
                    expect=SpawnState(current["state"]), failure_class="environment",
                    failure_cause="start_failure", detail=str(exc)[:1000], pool=self.pool)
            if current["state"] == "asking":
                await self._end_process(current)
                await agent_spawns.move(spawn_id, SpawnState.FINISHING, expect=SpawnState.ASKING,
                    result={"child": {"failure": ChildFailure.RUNTIME.value, "detail": str(exc)[:1000]}},
                    pool=self.pool)
                return await self.finish(spawn_id)
            raise
        finally:
            if admission_stack is not None:
                if grant is not None and record_id is None:
                    # An unstarted run cannot prove that the account's quota recovered.
                    grant.reported = True
                await admission_stack.aclose()
            self._homes.pop(spawn_id, None)
            self._children.pop(spawn_id, None)
            handle = self._handles.pop(spawn_id, None)
            from .kept_run import KeptRunState
            detached = handle is not None and handle.state is KeptRunState.DETACHED
            if checkout is not None and not detached:
                await asyncio.to_thread(explorer_checkout.remove, checkout)

    async def finish(self, spawn_id: int) -> dict[str, Any]:
        """Complete a finishing row from durable output; CAS books it exactly once."""
        from .explorer import parse_explorer_result
        row = await agent_spawns.get(spawn_id, pool=self.pool)
        if row["state"] != "finishing":
            return row
        record = await llm_records.get_record(row["child_record_id"], pool=self.pool) if row["child_record_id"] else None
        child = row["result"].get("child") or {}
        cost = record.get("cost_usd") if record else None
        if cost is None:
            cost = child.get("cost_usd")
        output = record.get("output_text") if record else child.get("output")
        failure = child.get("failure")
        detail = child.get("detail", "")
        if failure == ChildFailure.START_FAILURE.value:
            state, failure_class, cause = SpawnState.FAILED, "environment", "start_failure"
            result = {}
        elif failure == ChildFailure.TIMEOUT.value:
            state, failure_class, cause = SpawnState.FAILED, "judgment", "timeout"
            result = {}
        elif cost is None or not _number(cost) or cost > row["budget_usd"] or failure == ChildFailure.BUDGET.value:
            state, failure_class, cause = SpawnState.OVER_BUDGET, "judgment", "budget"
            result = {"cost_known": cost is not None}
        elif failure or (record and record["status"] != "completed"):
            state, failure_class, cause = SpawnState.FAILED, "environment", "runtime"
            result = {}
        else:
            state, failure_class, cause = SpawnState.ANSWERED, None, None
            result = parse_explorer_result(output or "", Path(row["inputs"]["work_dir"]))
        finished = await agent_spawns.move(spawn_id, state, expect=SpawnState.FINISHING,
            spent_usd=float(cost) if _number(cost) else row["budget_usd"], result=result,
            failure_class=failure_class, failure_cause=cause, detail=detail or None, pool=self.pool)
        if finished and self.stop_parent and row["inputs"].get("parent_budget_usd") is not None:
            await self.enforce_parent_budget(row["parent_record_id"], row["inputs"]["parent_budget_usd"])
        return finished or await agent_spawns.get(spawn_id, pool=self.pool)

    async def enforce_parent_budget(self, parent_record_id: int, cap: float, spent_usd: float | None = None) -> None:
        """Soft-stop a parent when its own spend plus completed child spend exceeds its cap."""
        if cost_status(spent_usd) is CostStatus.UNKNOWN:
            parent = await self.pool.fetchrow("SELECT cost_usd, model, metadata FROM llm_records WHERE id=$1", parent_record_id)
            spent_usd = parent["cost_usd"] if parent else None
            if cost_status(spent_usd) is CostStatus.UNKNOWN:
                logger.warning("model %s: parent spend cap cannot be verified: cost unknown (record %s)",
                               _unknown_models(parent), parent_record_id)
                return
        children = await self.pool.fetchval("SELECT COALESCE(sum(spent_usd),0) FROM agent_spawns WHERE parent_record_id=$1 AND kind='explorer'", parent_record_id)
        if float(spent_usd) + float(children) > cap and self.stop_parent:
            await self.stop_parent(parent_record_id)

    async def _end_process(self, row: Mapping[str, Any]) -> None:
        from . import proc_tree
        child_task = self._children.get(row["id"])
        if child_task is not None and not child_task.done():
            child_task.cancel()
            await asyncio.gather(child_task, return_exceptions=True)
        if handle := self._handles.get(row["id"]):
            await handle.end()
        identity = row.get("child_process")
        if identity and identity.get("pid") and identity.get("started"):
            pid, started = identity["pid"], identity["started"]
            await proc_tree.end_orphan(pid, started, label=f"explorer {row['id']}")
            current = await asyncio.to_thread(proc_tree._start_time, pid)
            if current == started:
                raise Unavailable("explorer process still alive after termination")

    async def cancel(self, spawn_id: int, *, reason: CancelReason = CancelReason.REQUESTED) -> dict[str, Any]:
        reason = CancelReason(reason)
        row = await agent_spawns.get(spawn_id, pool=self.pool)
        if row is None or row["kind"] != "explorer":
            raise ValueError("unknown explorer spawn")
        state = SpawnState(row["state"])
        if state in FINISHED or state is SpawnState.FINISHING:
            return row
        if state is SpawnState.QUEUED:
            moved = await agent_spawns.move(spawn_id, SpawnState.CANCELLED, expect=state,
                                            cancel_reason=reason.value, pool=self.pool)
            if moved and admission.enabled():
                running = self._running.get(spawn_id)
                if running is not None and running is not asyncio.current_task() and not running.done():
                    running.cancel()
                    await asyncio.gather(running, return_exceptions=True)
            return moved or await agent_spawns.get(spawn_id, pool=self.pool)
        if state is SpawnState.ASKING:
            row = await agent_spawns.move(spawn_id, SpawnState.CANCELLING, expect=state,
                                           cancel_reason=reason.value, pool=self.pool)
            if row is None:
                return await agent_spawns.get(spawn_id, pool=self.pool)
        await self._end_process(row)
        cost = 0.0
        if row["child_record_id"]:
            await llm_records.end_record_interrupted(row["child_record_id"], "explorer cancelled", pool=self.pool)
            record = await llm_records.get_record(row["child_record_id"], pool=self.pool)
            known_cost = record.get("cost_usd")
            cost = float(known_cost) if known_cost is not None else (row["budget_usd"] if (row.get("child_process") or {}).get("pid") else 0)
            # The spawn cancellation and the recorded runtime outcome settle independently.
            # A runtime that already ended keeps its final bill and status.
        moved = await agent_spawns.move(spawn_id, SpawnState.CANCELLED, expect=SpawnState.CANCELLING,
                                        spent_usd=cost, cancel_reason=row["cancel_reason"], pool=self.pool)
        return moved or await agent_spawns.get(spawn_id, pool=self.pool)

    async def cascade(self, *, reason: CancelReason, task_id: str | None = None,
                      parent_record_id: int | None = None, step_id: str | None = None) -> None:
        """Cancel the one-level subtree selected by a task, parent run or dropped step."""
        if task_id is None and parent_record_id is None and step_id is None:
            raise ValueError("cascade requires a task, parent or step")
        try:
            rows = await self.pool.fetch("SELECT id FROM agent_spawns WHERE kind='explorer' "
                "AND ($1::text IS NULL OR task_id=$1) AND ($2::bigint IS NULL OR parent_record_id=$2) "
                "AND ($3::text IS NULL OR step_id=$3) AND finished_at IS NULL ORDER BY id", task_id, parent_record_id, step_id)
            for row in rows:
                try:
                    await self.cancel(row["id"], reason=reason)
                except Unavailable:
                    logger.warning("explorer %s still cancelling during cascade", row["id"], exc_info=True)
        finally:
            await self._cleanup_abandoned()

    async def recover(self) -> None:
        """Recover durable states without invoking the spawn path."""
        try:
            rows = await self.pool.fetch("SELECT id FROM agent_spawns WHERE kind='explorer' AND finished_at IS NULL ORDER BY id")
            for entry in rows:
                if entry["id"] in self._running:
                    continue
                try:
                    row = await agent_spawns.get(entry["id"], pool=self.pool)
                    if row["state"] == "finishing":
                        await self.finish(row["id"])
                    elif row["state"] in ("asking", "cancelling"):
                        if row["state"] == "asking" and await self._adopt(row):
                            continue
                        await self.cancel(row["id"], reason=CancelReason.SERVER)
                except Unavailable:
                    logger.warning("explorer %s still cancelling during recovery", entry["id"], exc_info=True)
        finally:
            await self._cleanup_abandoned()

    async def _cleanup_abandoned(self) -> None:
        # Only live or adopted owners retain their checkout and runtime home.
        from .explorer_checkout import remove_abandoned
        try:
            await asyncio.to_thread(remove_abandoned, self.runtime_root / ".local" / "explorers",
                                    keep={str(spawn_id) for spawn_id in self._running})
        finally:
            kept_homes = set(self._homes.values()) | {handle.manifest["run_id"] for handle in self._handles.values()}
            await asyncio.to_thread(remove_abandoned, self.runtime_root / ".local" / "explorer-homes", keep=kept_homes)

    async def _adopt(self, row: Mapping[str, Any]) -> bool:
        if self.keeper is None or not (row.get("child_process") or {}).get("kept"):
            return False
        return await self._resume_kept(row)

    async def _resume_kept(self, row: Mapping[str, Any]) -> bool:
        from . import keeper as keeper_module
        from .claude_cli import ClaudeRunRecorder
        from .kept_run import KeptRun
        identity = row["child_process"]
        directory = Path(identity["run_dir"])
        if not directory.resolve().is_relative_to((self.runtime_root / keeper_module.RUNS_DIR).resolve()):
            return False
        handle = None
        admitted = False
        try:
            manifest = keeper_module.read_json(directory / keeper_module.MANIFEST) or {}
            if (manifest.get("role") != "explorer" or manifest.get("record_id") != row["child_record_id"]
                    or manifest.get("task_id") != row["task_id"]):
                return False
            project_id = await self.pool.fetchval("SELECT project_id FROM tasks WHERE id=$1", row["task_id"])
            settings = await load_agent_settings(AgentType.EXPLORER, project_id, row["task_id"],
                                                 step_id=row["step_id"], profile_id=row["profile_id"])
            launch = Launch(replace(settings, max_budget_usd=row["budget_usd"]),
                            role=AgentType.EXPLORER, access=Access.EXPLORE)
            if not launch.runtime.keepable or launch.runtime.hosts_runs:
                return False
            saved = manifest.get("launch") or {}
            current = launch.record()
            # Audit metadata (prices, setting sources and recorder routing) may
            # change without changing the authority of the existing process.
            if (manifest.get("runtime_type") != launch.runtime.type
                    or saved.get("profile", {}).get("runtime", {}).get("type") != launch.runtime.type
                    or saved.get("model_choice", {}).get("model") != launch.model
                    or any(saved.get(key) != current.get(key) for key in ("read_only", "access"))):
                return False
            handle = await KeptRun.attach(directory, runtime=launch.runtime, launch=launch,
                epoch=self.keeper.epoch, on_stale=self.on_stale, label=f"explorer {row['id']}")
            recorder = await ClaudeRunRecorder.resume(row["child_record_id"], agent_id=manifest["run_id"],
                task_id=row["task_id"], command=handle.command, cwd=row["inputs"]["work_dir"],
                step_id=row["step_id"], launch=launch, max_budget_usd=row["budget_usd"])
            if admission.enabled():
                _, lane = await admission.parent_for(row["parent_record_id"])
                await admission.register(launch.profile.provider.id, lane, owner=manifest["run_id"])
                admitted = True
        except Exception:
            if handle is not None:
                await handle.end()
                handle.release()
            logger.warning("cannot adopt explorer %s", row["id"], exc_info=True)
            return False
        self._handles[row["id"]] = handle
        task = asyncio.create_task(self._collect_kept(row, handle, recorder, launch, manifest, admitted=admitted))
        self._running[row["id"]] = task
        return True

    async def _collect_kept(self, row, handle, recorder, launch, manifest, *, admitted=False):
        from datetime import datetime, timezone
        try:
            record = await self.pool.fetchrow("SELECT created_at FROM llm_records WHERE id=$1", row["child_record_id"])
            remaining = max(.01, float(manifest["timeout_s"]) -
                            (datetime.now(timezone.utc) - record["created_at"]).total_seconds())
            child = asyncio.create_task(spawn.run_kept_child(handle, recorder, launch=launch,
                prompt=None, timeout=remaining))
            self._children[row["id"]] = child
            finished = await child
            failure = "timeout" if finished.timed_out else None
            await agent_spawns.move(row["id"], SpawnState.FINISHING, expect=SpawnState.ASKING,
                result={"child": {"failure": failure}}, pool=self.pool)
            return await self.finish(row["id"])
        except spawn.ChildDetached:
            return await agent_spawns.get(row["id"], pool=self.pool)
        except asyncio.CancelledError:
            await self.cancel(row["id"], reason=CancelReason.SERVER)
            raise
        except Exception:
            logger.warning("adopted explorer %s failed", row["id"], exc_info=True)
            await self.cancel(row["id"], reason=CancelReason.SERVER)
            raise
        finally:
            if admitted:
                try:
                    await admission.release(manifest["run_id"])
                except Exception:
                    logger.exception("Could not release adopted explorer %s account slot", row["id"])
            self._children.pop(row["id"], None)
            try:
                from .kept_run import KeptRunState
                if handle.state is not KeptRunState.DETACHED:
                    import shutil
                    home = self.runtime_root / ".local" / "explorer-homes" / manifest["run_id"]
                    await asyncio.to_thread(shutil.rmtree, home, True)
            finally:
                self._handles.pop(row["id"], None)
                self._running.pop(row["id"], None)

    async def detach(self, *, epoch: int, timeout: float = 5) -> None:
        """Hand keepable children to the next controller; cancel the others."""
        from .kept_run import DetachKind
        detached = set()
        for spawn_id, handle in list(self._handles.items()):
            outcome = await handle.detach(epoch=epoch, timeout=timeout)
            if outcome.kind is DetachKind.DETACHED:
                detached.add(spawn_id)
                child = self._children.get(spawn_id)
                if child is not None:
                    child.cancel()
                    await asyncio.gather(child, return_exceptions=True)
                running = self._running.get(spawn_id)
                if running is not None:
                    await asyncio.gather(running, return_exceptions=True)
        for spawn_id in list(self._running):
            if spawn_id in detached:
                continue
            await self.cancel(spawn_id, reason=CancelReason.SERVER)
