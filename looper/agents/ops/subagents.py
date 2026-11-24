"""Explorer tools of Workers and Planners, backed by the persistent spawn service."""
from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from ...db import agent_spawns
from ...db.agent_spawns import CancelReason, FINISHED, SpawnState
from ..run_usage import CostStatus, cost_status
from ...db.tasks import ACTIVE_STATUSES
from ..admission import Hold
from ..spawn import SpawnParent
from ..subagents import Delivery, Refused, RefusalReason, Spawner, SpawnService
from . import ErrorCode, OpError, RetrySafety, Risk, Tool, check_arguments

logger = logging.getLogger(__name__)
SPAWN, WAIT, CANCEL = "looper_agent_spawn", "looper_agent_wait", "looper_agent_cancel"
WAIT_MAX_SECONDS = 120
WORKER_INSTRUCTIONS = """## Explorer tools
For read-only investigation, use looper_agent_spawn (kind: explorer). Give a bounded goal,
deliverable and scope of relative paths. Explorers cannot edit files, browse the web or spawn agents.
For work that edits files, ask the Planner to add a step. Do not use native subagents to edit files.
You can continue working after spawn, then use looper_agent_wait for the complete result.
Omit spawn_ids to list this run's explorers. With the room on, results you did not wait for
are delivered to your seat; use wait for full evidence. Check unverified results yourself.
Use looper_agent_cancel when a result is no longer needed. When your run ends, unfinished
explorers are cancelled. These tools share your run's budget and task limits.
"""


PLANNER_SUBAGENTS_GUIDE = """\
## Explorers (looper_agent_spawn, looper_agent_wait, looper_agent_cancel)
This project lets you spawn read-only explorers: bounded investigations of the code that you read before you plan.
- Spawn before the [PLAN] reply when the plan depends on what the code or the repository holds. Give each a bounded
  goal, the deliverable and a scope of relative paths; one question per explorer, a few explorers at most.
- Wait for results inside the turn with looper_agent_wait (wait_seconds up to 120). A result that misses the turn
  reaches you in the next turn; looper_agent_wait with no spawn_ids lists every explorer you spawned, with its
  state, and returns the full result.
- Explorers only read: they cannot edit files, browse the web or spawn agents. Work that changes files is a step for
  a Worker. They are not the advisor: asking for advice stays the [DECISION] "advise" action.
- Unverified results (they failed their structure check) are leads, not facts: check them before you rely on them.
- Cancel an explorer you no longer need with looper_agent_cancel. Your turn ending does not cancel your explorers;
  the task ending does. They share this task's explorer limits and budget.
- Like your other changing tools, spawn and cancel work only in a turn Looper sent you.
"""


def worker_enabled(project: Mapping[str, Any] | None, *, is_verify: bool = False) -> bool:
    return not is_verify and (project or {}).get("subagents") == "on"


async def kept_explorer_records(runtime_root: Path, pool: Any) -> dict[str, int]:
    """Delegate persisted explorer keepers to SpawnService before the Worker startup sweep."""
    from ..keeper import RUNS_DIR
    rows = await pool.fetch("SELECT s.child_record_id,s.child_process,r.agent_id FROM agent_spawns s "
        "JOIN llm_records r ON r.id=s.child_record_id WHERE s.kind='explorer' AND s.state='asking' "
        "AND s.child_process->>'kept'='true' AND r.run_kind='explorer' AND r.status='running'")
    owned = {}
    root = (runtime_root / RUNS_DIR).resolve()
    for row in rows:
        identity = row["child_process"]
        if isinstance(identity, str):
            identity = json.loads(identity)
        location = identity.get("run_dir")
        if not isinstance(location, str) or not location:
            continue
        directory = Path(location).resolve()
        if directory.parent == root and directory.name == row["agent_id"]:
            owned[directory.name] = row["child_record_id"]
    return owned


async def _never(call):
    raise AssertionError("explorer tools require a run grant")


_WAIT = {"type": "integer", "minimum": 0, "maximum": WAIT_MAX_SECONDS}
TOOLS = (
    Tool(name=SPAWN, description="Spawn a bounded read-only explorer for this run; optionally wait for its result. "
         "It cannot edit files or browse the web. Use a key to repeat a request without spawning twice.",
         schema={"type": "object", "additionalProperties": False, "required": ["kind", "brief"], "properties": {
             "kind": {"type": "string", "enum": ["explorer"]},
             "brief": {"type": "object", "additionalProperties": False,
                       "required": ["goal", "deliverable", "scope"], "properties": {
                           "goal": {"type": "string", "minLength": 1, "maxLength": 2000},
                           "deliverable": {"type": "string", "minLength": 1, "maxLength": 2000},
                           "scope": {"type": "array", "minItems": 1, "maxItems": 32,
                                     "items": {"type": "string", "minLength": 1, "maxLength": 500}},
                           "questions": {"type": "array", "maxItems": 20,
                                         "items": {"type": "string", "minLength": 1, "maxLength": 1000}}}},
             "profile": {"type": "string", "minLength": 1},
             "budget_usd": {"type": "number", "minimum": .01, "maximum": 5},
             "wait_seconds": _WAIT, "key": {"type": "string", "minLength": 1, "maxLength": 200}}},
         risk=Risk.WRITE, run=_never),
    Tool(name=WAIT, description="Read this run's explorers and remaining limits; optionally wait for the selected results. "
         "Omit spawn_ids for all explorers spawned by this run. Completed results include evidence or a typed failure.",
         schema={"type": "object", "additionalProperties": False, "properties": {
             "spawn_ids": {"type": "array", "minItems": 1, "maxItems": 8, "uniqueItems": True,
                           "items": {"type": "integer", "minimum": 1}}, "wait_seconds": _WAIT}},
         risk=Risk.READ, run=_never),
    Tool(name=CANCEL, description="Cancel one explorer spawned by this run and release its unspent reservation.",
         schema={"type": "object", "additionalProperties": False, "required": ["spawn_id"], "properties": {
             "spawn_id": {"type": "integer", "minimum": 1},
             "reason": {"type": "string", "maxLength": 1000}}}, risk=Risk.WRITE, run=_never),
)
TOOL_NAMES = frozenset(tool.name for tool in TOOLS)
RETRY_SAFETY = {SPAWN: RetrySafety.IDEMPOTENT, WAIT: RetrySafety.READ_ONLY, CANCEL: RetrySafety.IDEMPOTENT}
_BY_NAME = {tool.name: tool for tool in TOOLS}


@dataclass
class _Parent:
    task_id: str
    seat: Any
    recorder: Any
    launch: Any

    @property
    def record_id(self) -> int | None:
        return self.recorder.record_id

    def owns(self, row: Mapping[str, Any]) -> bool:
        return row["parent_record_id"] == self.record_id

    def caller(self) -> Spawner:
        usage = self.recorder._usage_so_far
        cost = usage.cost_usd
        spend = getattr(self.recorder, "_spend", None)
        if cost_status(cost) is CostStatus.UNKNOWN and spend is None and any(
                getattr(usage, field, None) is not None for field in
                ("input_tokens", "output_tokens", "cache_read_tokens", "cache_creation_tokens")):
            from ..runtimes.pricing import pricer
            price = pricer(self.launch.settings.resolved_model, catalog=self.launch.settings.pricing_catalog)
            cost = price(self.launch.model, usage)
        if self.launch.max_budget_usd is not None and cost_status(cost) is CostStatus.UNKNOWN:
            models = sorted(getattr(spend, "unknown_models", ())) or [getattr(self.launch, "model", "unknown")]
            logger.warning("models %s: parent spend cap cannot be verified: cost unknown", ", ".join(models))
        return Spawner(SpawnParent(self.task_id, self.seat.step_id, self.recorder.record_id),
                       "worker", Path(self.seat.work_dir), self.seat.key,
                       budget_usd=self.launch.max_budget_usd, spent_usd=cost)


@dataclass
class _PlannerParent:
    """The task's Planner as a spawner: this turn's record is the parent, but it owns every explorer the task's
    Planner spawned, across turns. Its spend and cap are not live outside the turn, so neither is a budget term."""
    task_id: str
    record_id: int
    work_dir: Path
    writable_turn: bool
    turn_cap_usd: float = 0.0

    def owns(self, row: Mapping[str, Any]) -> bool:
        return row.get("spawner") == "planner"

    def caller(self) -> Spawner:
        return Spawner(SpawnParent(self.task_id, None, self.record_id), "planner", self.work_dir, seat="planner",
                       writable_turn=self.writable_turn, budget_usd=None, spent_usd=None,
                       unrecorded_usd=self.turn_cap_usd)


def _view(row: Mapping[str, Any], *, hold: Hold | None = None) -> dict[str, Any]:
    value = {key: row[key] for key in ("id", "state", "budget_usd", "delivery")}
    value["profile"] = row.get("profile_id")
    if row["state"] == "queued" and hold is not None:
        value.update(held=hold.reason, resets_at=hold.resets_at.isoformat() if hold.resets_at else None)
    if SpawnState(row["state"]) in FINISHED:
        if row.get("result"):
            # Service accounting and raw runtime fields remain private to the service.
            value["result"] = {k: v for k, v in row["result"].items()
                               if k in ("summary", "findings", "open_questions", "confidence", "verified", "text")}
        if row.get("failure_class"):
            value["failure"] = {"class": row["failure_class"], "cause": row["failure_cause"]}
        for key in ("detail", "cancel_reason"):
            if row.get(key):
                value[key] = row[key]
    return value


class WorkerSubagents:
    """Own launch/delivery tasks separately from an HTTP request's lifetime."""
    def __init__(self, service: SpawnService, room: Any, *, worker_pool: Any = None):
        self.service, self.room = service, room
        self.worker_pool = worker_pool
        if worker_pool is not None:
            service.stop_parent = self._stop_parent
        self._parents: dict[str, _Parent] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._jobs: dict[int, asyncio.Task] = {}
        self._waiting: dict[int, int] = {}
        self._scanner: asyncio.Task | None = None

    def bind(self, task_id: str, seat: Any, recorder: Any, launch: Any) -> None:
        directory = getattr(recorder, "_cwd", None) or seat.work_dir
        if recorder.record_id is None or not directory:
            raise ValueError("explorer tools require a recorded parent and working directory")
        # Verification Workers may run in a private checkout rather than the
        # dispatch's directory. Explore the actual recorded runtime directory.
        self._parents[seat.run] = _Parent(task_id, replace(seat, work_dir=str(directory)), recorder, launch)

    async def end(self, run: str) -> None:
        if run not in self._parents:
            return
        async with self._locks.setdefault(run, asyncio.Lock()):
            parent = self._parents.pop(run, None)
            if parent is not None:
                try:
                    for row in await self._owned(parent, None):
                        if row["state"] == "finishing":
                            await self.service.finish(row["id"])
                            await self._fallback([row["id"]])
                finally:
                    await self.service.cascade(parent_record_id=parent.recorder.record_id, reason=CancelReason.PARENT_ENDED)
        self._locks.pop(run, None)

    async def call(self, task_id: str, seat: Any, name: str, raw: Any, *, call_id: str | None = None) -> dict:
        async def resolve() -> _Parent:
            parent = self._parents.get(seat.run)
            if parent is None:
                raise OpError(ErrorCode.UNAVAILABLE, "this run's explorer tools are not ready")
            return parent
        return await self.serve(task_id, seat.run, seat.run, resolve, name, raw, call_id)

    async def serve(self, task_id: str, lock_key: str, who: str, resolve: Any, name: str, raw: Any,
                    call_id: str | None) -> dict:
        """One explorer tool call for the parent ``resolve`` finds (inside the parent's lock); shared by Workers and
        the Planner. ``who`` names the caller in activity and logs."""
        try:
            # The shared validator only accepts arrays of strings. This tool
            # has integer ids; validate that field here without changing old tools.
            if name == WAIT and isinstance(raw, dict) and "spawn_ids" in raw:
                ids = raw["spawn_ids"]
                if (not isinstance(ids, list) or not 1 <= len(ids) <= 8
                        or any(type(i) is not int or i < 1 for i in ids) or len(set(ids)) != len(ids)):
                    raise OpError(ErrorCode.INVALID, "spawn_ids must contain 1 to 8 distinct positive integers")
                args = {**check_arguments(_BY_NAME[name].schema, {k: v for k, v in raw.items() if k != "spawn_ids"}),
                        "spawn_ids": ids}
            else:
                args = check_arguments(_BY_NAME[name].schema, raw)
            async with self._locks.setdefault(lock_key, asyncio.Lock()):
                parent = await resolve()
                enabled = await self.service.pool.fetchval("SELECT p.subagents FROM projects p JOIN tasks t "
                    "ON t.project_id=p.id WHERE t.id=$1", task_id)
                if enabled != "on":
                    raise Refused(RefusalReason.OFF, "subagents are off for this project")
                if name == SPAWN:
                    key = args.get("key") or call_id
                    if not isinstance(key, str) or not 1 <= len(key) <= 200:
                        raise OpError(ErrorCode.INVALID, "spawn requires a bridge call id or an explicit key")
                    row = await self.service.admit(parent.caller(), args["brief"], key=key,
                        kind=args["kind"], profile=args.get("profile"), budget_usd=args.get("budget_usd"),
                        delivery=Delivery.TOOL)
                    rows = [row]
                else:
                    rows = await self._owned(parent, [args["spawn_id"]] if name == CANCEL else args.get("spawn_ids"))
            if name == CANCEL:
                row = await self.service.cancel(rows[0]["id"], reason=CancelReason.REQUESTED)
                if args.get("reason"):
                    from ...db import activity
                    await activity.insert_activity(task_id=task_id, worker=who, kind="subagent_cancelled",
                        summary=args["reason"], metadata={"spawn_id": row["id"]}, pool=self.service.pool)
                return {"status": "ok", "result": {"spawn": _view(row)}}
            seconds = args.get("wait_seconds") or 0
            ids = [row["id"] for row in rows]
            for spawn_id in ids:
                self._waiting[spawn_id] = self._waiting.get(spawn_id, 0) + 1
            try:
                for spawn_id in ids:
                    self.schedule(spawn_id)
                rows = await self._wait(parent, ids, seconds)
                from ..subagent_delivery import mark_tool_delivered
                # Hold off room delivery until this tool has claimed its completed results.
                await mark_tool_delivered(self.service.pool, ids)
            finally:
                for spawn_id in ids:
                    count = self._waiting[spawn_id] - 1
                    if count:
                        self._waiting[spawn_id] = count
                    else:
                        del self._waiting[spawn_id]
            await self._fallback(ids)
            rows = await self._owned(parent, ids)
            holds = await self._holds(rows)
            result = {"spawn": _view(rows[0], hold=holds.get(rows[0]["profile_id"]))} if name == SPAWN else {
                "spawns": [_view(row, hold=holds.get(row["profile_id"])) for row in rows],
                "remaining": await self._remaining(parent)}
            return {"status": "ok", "result": result}
        except Refused as exc:
            return {"status": "refused", "reason": exc.reason.value, "detail": str(exc)}
        except OpError as exc:
            return {"status": "error", "error": {"code": exc.code.value, "message": str(exc)}}
        except Exception:
            logger.exception("%s explorer tool %s failed", who, name)
            return {"status": "error", "error": {"code": "unavailable", "message": "Explorer tools unavailable; retry with the same key"}}

    async def _owned(self, parent: _Parent | _PlannerParent, ids: list[int] | None) -> list[dict]:
        rows = [row for row in await agent_spawns.for_task(parent.task_id, pool=self.service.pool)
                if row["kind"] == "explorer" and parent.owns(row)]
        if ids is not None:
            by_id = {row["id"]: row for row in rows}
            if any(spawn_id not in by_id for spawn_id in ids):
                raise OpError(ErrorCode.FORBIDDEN, "spawn_ids must belong to this run")
            rows = [by_id[spawn_id] for spawn_id in ids]
        return rows

    async def _wait(self, parent: _Parent | _PlannerParent, ids: list[int], seconds: int) -> list[dict]:
        from .. import admission
        if seconds > 0 and admission.enabled():
            return await self._wait_admitted(parent, ids, seconds)
        deadline = asyncio.get_running_loop().time() + seconds
        while True:
            rows = await self._owned(parent, ids)
            remaining = deadline - asyncio.get_running_loop().time()
            if all(SpawnState(row["state"]) in FINISHED for row in rows) or remaining <= 0:
                return rows
            await asyncio.sleep(min(.1, remaining))

    async def _wait_admitted(self, parent: _Parent | _PlannerParent, ids: list[int], seconds: int) -> list[dict]:
        from contextlib import AsyncExitStack
        from .. import admission
        deadline = asyncio.get_running_loop().time() + seconds
        lent = False
        async with AsyncExitStack() as stack:
            while True:
                rows = await self._owned(parent, ids)
                remaining = deadline - asyncio.get_running_loop().time()
                if all(SpawnState(row["state"]) in FINISHED for row in rows) or remaining <= 0:
                    return rows
                # A newly scheduled spawn builds its identity asynchronously.
                # Register once it exists, within the original wait deadline.
                if not lent:
                    owners = [owner for row in rows if row["state"] == "queued"
                              if (owner := self.service.owner_of(row["id"])) is not None]
                    preparing = any(row["state"] == "queued" and row["id"] in self._jobs
                                    and self.service.owner_of(row["id"]) is None for row in rows)
                    if owners and not preparing:
                        owner, _ = await admission.parent_for(parent.caller().parent.record_id)
                        await stack.enter_async_context(admission.lend(owner, owners))
                        lent = True
                await asyncio.sleep(min(.1, remaining))

    async def _holds(self, rows: list[dict]) -> dict:
        from .. import admission
        if not admission.enabled():
            return {}
        from ...db import profile_registry
        holds, providers = {}, {}
        for row in rows:
            profile_id = row.get("profile_id")
            if row["state"] != "queued" or profile_id in holds:
                continue
            profile = await profile_registry.get_profile(profile_id, pool=self.service.pool) if profile_id else None
            provider_id = profile["provider_id"] if profile else None
            if provider_id not in providers:
                providers[provider_id] = admission.hold(provider_id) if provider_id else None
            holds[profile_id] = providers[provider_id]
        return holds

    async def _remaining(self, parent: _Parent | _PlannerParent) -> dict:
        project = await self.service.pool.fetchrow("SELECT p.* FROM projects p JOIN tasks t ON t.project_id=p.id "
                                                   "WHERE t.id=$1", parent.task_id)
        usage = await agent_spawns.commitment(parent.task_id, parent.record_id, pool=self.service.pool)
        limit = await self.service.pool.fetchval("SELECT max_concurrency FROM task_concurrency_limits "
            "WHERE task_id=$1 AND role='subagent'", parent.task_id) or 4
        active = await self.service.pool.fetchval("SELECT count(*) FROM agent_spawns WHERE task_id=$1 "
            "AND kind='explorer' AND state IN ('asking','cancelling')", parent.task_id)
        return {"budget_usd": max(0, (project["subagent_task_budget_usd"] or 3) - usage["task_committed"]),
                "per_run": max(0, (project["subagents_per_run"] if project["subagents_per_run"] is not None else 4) - usage["parent_count"]),
                "per_task": max(0, (project["subagents_per_task"] if project["subagents_per_task"] is not None else 20) - usage["task_count"]),
                "concurrency": max(0, limit - active)}

    def schedule(self, spawn_id: int) -> None:
        if spawn_id not in self._jobs:
            self._jobs[spawn_id] = asyncio.create_task(self._run(spawn_id), name=f"explorer-{spawn_id}")

    async def _run(self, spawn_id: int) -> None:
        try:
            await self.service.run(spawn_id)
            await self._fallback([spawn_id])
        except Exception:
            logger.exception("explorer %s launch or delivery failed; recovery will retry", spawn_id)
        finally:
            self._jobs.pop(spawn_id, None)

    async def _fallback(self, ids: list[int]) -> None:
        from ..subagent_delivery import deliver_room
        for spawn_id in ids:
            if self._waiting.get(spawn_id):
                continue
            # With the room off, only the Planner has a way to get results later: its next turn.
            await self.service.pool.execute("UPDATE agent_spawns s SET delivery=CASE WHEN COALESCE(p.agent_room,'on')='on' "
                "THEN 'room' ELSE 'turn' END FROM tasks t,projects p "
                "WHERE s.id=$1 AND s.task_id=t.id AND t.project_id=p.id AND p.subagents='on' "
                "AND (COALESCE(p.agent_room,'on')='on' OR s.spawner='planner') AND s.delivered_at IS NULL", spawn_id)
            await deliver_room(self.service, self.room, spawn_id)

    async def start(self) -> None:
        await self.service.recover()
        self._scanner = asyncio.create_task(self._scan(), name="explorer-recovery")

    async def _scan(self) -> None:
        while True:
            try:
                for parent in list(self._parents.values()):
                    caller = parent.caller()
                    if caller.budget_usd is not None and caller.spent_usd is not None:
                        await self.service.enforce_parent_budget(caller.parent.record_id, caller.budget_usd,
                                                                 caller.spent_usd)
                rows = await self.service.pool.fetch("SELECT s.id,s.state,s.spawner,r.status,t.status AS task_status "
                    "FROM agent_spawns s JOIN llm_records r ON r.id=s.parent_record_id "
                    "JOIN tasks t ON t.id=s.task_id WHERE s.kind='explorer' "
                    "AND (s.finished_at IS NULL OR (s.delivered_at IS NULL AND EXISTS "
                    "(SELECT 1 FROM tasks t JOIN projects p ON p.id=t.project_id WHERE t.id=s.task_id "
                    "AND p.subagents='on' AND COALESCE(p.agent_room,'on')='on')))")
                for row in rows:
                    try:
                        if row["state"] == "finishing":
                            await self.service.finish(row["id"])
                        elif row["spawner"] == "planner":
                            # A Planner's turn ending cancels nothing; its task ending cancels everything.
                            if row["task_status"] not in ACTIVE_STATUSES and row["state"] in ("queued", "asking", "cancelling"):
                                await self.service.cancel(row["id"], reason=CancelReason.TASK_ENDED)
                            elif row["state"] == "queued":
                                self.schedule(row["id"])
                            elif row["state"] == "cancelling":
                                await self.service.cancel(row["id"], reason=CancelReason.TASK_ENDED)
                        elif row["status"] != "running":
                            if row["state"] in ("queued", "asking", "cancelling"):
                                await self.service.cancel(row["id"], reason=CancelReason.PARENT_ENDED)
                        elif row["state"] == "queued":
                            self.schedule(row["id"])
                        elif row["state"] == "cancelling":
                            await self.service.cancel(row["id"], reason=CancelReason.PARENT_ENDED)
                        await self._fallback([row["id"]])
                    except Exception:
                        logger.exception("explorer %s recovery deferred", row["id"])
            except Exception:
                logger.exception("explorer recovery scan failed")
            await asyncio.sleep(1)

    async def _stop_parent(self, record_id: int) -> None:
        parent = next((p for p in self._parents.values() if p.recorder.record_id == record_id), None)
        if parent is not None and self.worker_pool is not None:
            await self.worker_pool.kill(parent.seat.run)

    async def stop(self, *, epoch: int | None = None) -> None:
        if self._scanner is not None:
            self._scanner.cancel()
            await asyncio.gather(self._scanner, return_exceptions=True)
        if epoch is not None:
            await self.service.detach(epoch=epoch)
        else:
            rows = await self.service.pool.fetch("SELECT id,state FROM agent_spawns WHERE kind='explorer' AND finished_at IS NULL")
            for row in rows:
                try:
                    if row["state"] == "finishing":
                        await self.service.finish(row["id"])
                    else:
                        await self.service.cancel(row["id"], reason=CancelReason.PARENT_ENDED)
                except Exception:
                    logger.exception("explorer %s shutdown deferred to recovery", row["id"])
        await asyncio.gather(*list(self._jobs.values()), return_exceptions=True)


class PlannerSubagents:
    """The Planner's explorer tools: the Workers' code path, the same service, jobs and delivery, with the task's
    Planner as the spawner (its running turn is the parent record) and every explorer it spawned as its own."""

    def __init__(self, workers: WorkerSubagents):
        self.workers = workers

    async def _turn_cap(self, task_id: str) -> float:
        """What the running turn may still add to the task's spend: its per-run cap, since its own spend is not
        known here (the task cap must hold either way). 0 when no cap is set or it cannot be read."""
        from ..agent_settings import AgentType, load_agent_settings
        try:
            project_id = await self.workers.service.pool.fetchval("SELECT project_id FROM tasks WHERE id=$1", task_id)
            cap = (await load_agent_settings(AgentType.PLANNER, project_id, task_id)).max_budget_usd
        except Exception:
            logger.warning("task %s: could not read the Planner's per-run cap", task_id, exc_info=True)
            return 0.0
        return float(cap) if cap else 0.0

    async def call(self, task_id: str, name: str, raw: Any, *, call_id: str | None = None,
                   in_turn: bool = False) -> dict:
        pool = self.workers.service.pool

        async def resolve() -> _PlannerParent:
            record_id = await pool.fetchval(
                "SELECT id FROM llm_records WHERE task_id=$1 AND agent_id=$2 AND status='running' "
                "ORDER BY id DESC LIMIT 1", task_id, f"planner-{task_id}")
            if record_id is None:
                raise Refused(RefusalReason.ROLE, "the Planner has no running turn")
            work_dir = await pool.fetchval("SELECT work_dir FROM tasks WHERE id=$1", task_id)
            if not work_dir:
                raise Refused(RefusalReason.SCOPE, "the task has no working directory to explore")
            return _PlannerParent(task_id, record_id, Path(work_dir), in_turn, await self._turn_cap(task_id))
        return await self.workers.serve(task_id, f"planner-{task_id}", f"planner-{task_id}", resolve, name, raw, call_id)
