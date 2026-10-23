"""The Managers that run: the default one and every enabled scenario Manager (scenario Managers RFC §3, §4 D2, D6,
§6). A ``ManagerFleet`` builds a ``ManagerAgent`` and its ``ManagerOps`` (with a token and a binding file of their
own) for each, registers them with the supervisor, and starts, stops or refreshes one when the API changed its
definition. A change that cannot be applied is retried until it is in force.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..db import managers as db_managers
from .bus import MessageBus
from .managers_api import DEFAULT_MANAGER_ID, MAX_ENABLED_MANAGERS, ManagerDefinition
from .message import MANAGER_ADDRESS, MessageEnvelope, MessageType, manager_address
from .ops.service import ManagerOps, Platform, scope_for
from .supervisor import AgentSupervisor
from .tiers.manager import (
    TURNED_OFF_REPLY, ManagerAgent, ManagerConfig, ReplyOutcome, default_definition, reply_envelope, stopped_reply,
)
from .tiers.turn_token import TurnToken

logger = logging.getLogger(__name__)

# A failed apply is retried after these many seconds, then every RETRY_LATER_SECONDS.
RETRY_DELAYS = (2, 5, 15, 30, 60)
RETRY_LATER_SECONDS = 60

_TASK_REPORTS = (MessageType.GOAL_STATUS, MessageType.TASK_ACCEPTED, MessageType.WORK_RESULT)


@dataclass
class SyncState:
    error: str
    attempt: int


class ManagerFleet:
    def __init__(self, *, platform: Platform, supervisor: AgentSupervisor, runtime_root: Path, url: str,
                 config: ManagerConfig, broadcaster: Any | None) -> None:
        self._platform = platform
        self._supervisor = supervisor
        self._runtime_root = runtime_root
        self._url = url
        self._config = config
        self._broadcaster = broadcaster
        self._agents: dict[str, ManagerAgent] = {}
        self._ops: dict[str, ManagerOps] = {}
        self._retiring: set[str] = set()
        self._sync: dict[str, SyncState] = {}
        self._retries: dict[str, asyncio.Task[None]] = {}
        self._turned_off_tasks: set[asyncio.Task[Any]] = set()
        self._lock = asyncio.Lock()

    @property
    def _bus(self) -> MessageBus:
        return self._supervisor.bus

    # --- Building ---

    def _build(self, definition: ManagerDefinition, template_keys: Sequence[str] = ()) -> ManagerAgent:
        agent = ManagerAgent(config=self._config, runtime_root=self._runtime_root, broadcaster=self._broadcaster,
                             definition=definition)
        ops = ManagerOps(dataclasses.replace(self._platform, manager=agent, managers=self), url=self._url,
                         scope=scope_for(definition, template_keys),
                         turn_file=self._runtime_root / ".local" / f"{definition.id}-turn.json")
        agent.ops = ops
        agent.on_turned_off = self._turned_off
        self._agents[definition.id] = agent
        self._ops[definition.id] = ops
        return agent

    def _turned_off(self, manager_id: str) -> None:
        task = asyncio.create_task(self.apply(manager_id))
        self._turned_off_tasks.add(task)
        task.add_done_callback(self._turned_off_tasks.discard)

    def _drop(self, manager_id: str) -> None:
        self._agents.pop(manager_id, None)
        self._ops.pop(manager_id, None)

    async def load(self) -> None:
        """Build the default Manager and every enabled scenario Manager (at most MAX_ENABLED_MANAGERS) and register
        them; the default one is built even when the definitions cannot be read."""
        try:
            rows = await db_managers.list_managers()
        except Exception:
            logger.exception("could not read the Manager definitions; only the default Manager runs")
            rows = []
        default_row = next((row for row in rows if row["id"] == DEFAULT_MANAGER_ID), None)
        default = ManagerDefinition.from_row(default_row) if default_row else default_definition()
        self._supervisor.register(self._build(default))
        for row in rows:
            if row["id"] == DEFAULT_MANAGER_ID or not row.get("enabled"):
                continue
            if len(self._agents) >= MAX_ENABLED_MANAGERS:
                logger.error("Manager %s not started: at most %d Managers run at once", row["id"],
                             MAX_ENABLED_MANAGERS)
                continue
            self._supervisor.register(self._build(ManagerDefinition.from_row(row), await self._template_keys(row["id"])))

    @staticmethod
    async def _template_keys(manager_id: str) -> list[str]:
        try:
            return await ManagerFleet._read_template_keys(manager_id)
        except Exception:
            logger.exception("could not read the task templates of Manager %s", manager_id)
            return []

    async def stop(self) -> None:
        """The service shuts down: no retry goes on."""
        for task in list(self._retries.values()):
            task.cancel()
        await asyncio.gather(*self._retries.values(), return_exceptions=True)
        self._retries.clear()

    # --- Lookup ---

    def get(self, manager_id: str) -> ManagerAgent | None:
        return self._agents.get(manager_id)

    @property
    def default(self) -> ManagerAgent:
        return self._agents[DEFAULT_MANAGER_ID]

    def ops(self, manager_id: str) -> ManagerOps | None:
        return self._ops.get(manager_id)

    def ops_for(self, authorization: str | None) -> ManagerOps | None:
        return next((ops for ops in list(self._ops.values()) if ops.authorized(authorization)), None)

    def agents(self) -> list[ManagerAgent]:
        return [self._agents[i] for i in sorted(self._agents, key=lambda i: (i != DEFAULT_MANAGER_ID, i))]

    def turn_for(self, message_id: str) -> tuple[ManagerAgent, TurnToken] | None:
        for agent in self.agents():
            token = agent.turn_for(message_id)
            if token is not None:
                return agent, token
        return None

    def binding_report(self) -> dict[str, Any]:
        reports = [(agent.agent_id, self._ops[agent.agent_id].binding_report()) for agent in self.agents()
                   if agent.agent_id in self._ops]
        return {"enforced": any(report["enforced"] for _, report in reports),
                "mismatches": sum(report["mismatches"] for _, report in reports),
                "by": [{**item, "manager": manager_id} for manager_id, report in reports for item in report["by"]]}

    def state(self, manager_id: str) -> dict[str, Any]:
        agent = self._agents.get(manager_id)
        state: dict[str, Any] = {
            "running": agent.process_started if agent else False,
            "busy": agent.busy if agent else False,
            "healthy": agent.is_healthy() if agent else False,
        }
        sync = self._sync.get(manager_id)
        state["sync"] = "pending" if sync else "ok"
        if sync:
            state["sync_error"] = sync.error
        return state

    def is_running(self, manager_id: str) -> bool:
        return manager_id in self._agents and manager_id not in self._retiring

    # --- Applying a change ---

    async def apply(self, manager_id: str) -> None:
        """Start, stop or refresh one Manager after a change; a failure is retried until it succeeds or a newer
        change supersedes it."""
        pending = self._retries.pop(manager_id, None)
        if pending is not None:
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
        try:
            async with self._lock:
                await self._apply_once(manager_id)
        except Exception as exc:
            self._failed(manager_id, exc, attempt=1)
        else:
            self._sync.pop(manager_id, None)

    def _failed(self, manager_id: str, exc: BaseException, *, attempt: int) -> None:
        logger.exception("applying the change of Manager %s failed; retrying", manager_id, exc_info=exc)
        self._sync[manager_id] = SyncState(error=str(exc) or type(exc).__name__, attempt=attempt)
        if manager_id not in self._retries:
            self._retries[manager_id] = asyncio.create_task(self._retry(manager_id))

    async def _retry(self, manager_id: str) -> None:
        me = asyncio.current_task()
        try:
            attempt = 0
            while True:
                delay = RETRY_DELAYS[attempt] if attempt < len(RETRY_DELAYS) else RETRY_LATER_SECONDS
                attempt += 1
                await asyncio.sleep(delay)
                try:
                    async with self._lock:
                        await self._apply_once(manager_id)
                except Exception as exc:
                    logger.warning("retry %d of applying Manager %s failed: %s", attempt, manager_id, exc)
                    self._sync[manager_id] = SyncState(error=str(exc) or type(exc).__name__, attempt=attempt + 1)
                    continue
                self._sync.pop(manager_id, None)
                return
        finally:
            if self._retries.get(manager_id) is me:
                del self._retries[manager_id]

    async def _apply_once(self, manager_id: str) -> None:
        row = await db_managers.get_manager(manager_id)
        enabled = row is not None and bool(row.get("enabled"))
        running = manager_id in self._agents
        if manager_id == DEFAULT_MANAGER_ID:
            return  # always on
        if not enabled:
            if running:
                await self._retire(manager_id)
        elif not running:
            if len(self._agents) >= MAX_ENABLED_MANAGERS:
                # Raising keeps the sync pending and the retry going: a Manager retiring meanwhile frees a place.
                raise RuntimeError(f"Manager {manager_id} not started: at most {MAX_ENABLED_MANAGERS} Managers "
                                   "run at once")
            agent = self._build(ManagerDefinition.from_row(row), await self._read_template_keys(manager_id))
            await self._supervisor.start_agent(agent)
        else:
            # Running and still enabled: the definition (tools, task scope, task policy, templates) may have changed.
            ops = self._ops.get(manager_id)
            if ops is not None and ops.scope.revision != row.get("revision"):
                ops.set_scope(scope_for(ManagerDefinition.from_row(row), await self._read_template_keys(manager_id)))

    @staticmethod
    async def _read_template_keys(manager_id: str) -> list[str]:
        """The keys of the Manager's templates; a failure propagates, so the apply is retried."""
        return [template["key"] for template in await db_managers.list_templates(manager_id)]

    # --- Retiring ---

    async def _retire(self, manager_id: str) -> None:
        self._retiring.add(manager_id)
        agent = self._agents.get(manager_id)
        await self._supervisor.stop_agent(manager_id)
        bus = self._bus
        taken = bus.take_queue(manager_id)
        queued = {envelope.message_id for envelope in taken}
        for envelope, partial in (agent.stopped_in_flight if agent else ()):
            if envelope.message_id in queued:
                continue
            if envelope.message_type in _TASK_REPORTS:
                await self._hand_over(envelope)
            else:
                # The stopped turn's final reply first: the chat waits for it, and settling alone ends no stream.
                await bus.publish(reply_envelope(envelope, stopped_reply(partial, []), manager_address(manager_id),
                                                 outcome=ReplyOutcome.STOPPED))
                bus.settle(envelope.message_id)  # a user message's token ended as stopped
        await self._deal_with(manager_id, taken)
        self._retiring.discard(manager_id)
        self._drop(manager_id)

    async def _deal_with(self, manager_id: str, taken: list[MessageEnvelope]) -> None:
        source = manager_address(manager_id)
        for envelope in taken:
            if envelope.message_type == MessageType.USER_MESSAGE:
                await self._bus.publish(reply_envelope(envelope, TURNED_OFF_REPLY, source))
                self._bus.settle(envelope.message_id)
            elif envelope.message_type in _TASK_REPORTS:
                await self._hand_over(envelope, manager_id)
            else:
                self._bus.settle(envelope.message_id)

    async def _hand_over(self, envelope: MessageEnvelope, manager_id: str | None = None) -> None:
        """Pass a task's report to the default Manager (a new message), and only then settle the original: a crash
        between the two repeats the report, never loses it."""
        origin = manager_id or envelope.target.agent_id
        await self._bus.publish(MessageEnvelope.create(
            source=envelope.source, target=MANAGER_ADDRESS, message_type=envelope.message_type,
            payload=envelope.payload, correlation_id=envelope.correlation_id,
            metadata={**envelope.metadata, "handed_over_from": origin}))
        self._bus.settle(envelope.message_id)

    async def sweep(self) -> None:
        """After the supervisor started: deal with what the bus replayed to a Manager that does not run (turned
        off, deleted, or not started past the cap)."""
        ids = {name for name in self._bus.queue_ids() if name.startswith("manager-")}
        try:
            ids |= {row["id"] for row in await db_managers.list_managers(include_deleted=True)}
        except Exception:
            logger.exception("could not read the Manager definitions to sweep their queues")
        for manager_id in sorted(ids):
            if manager_id == DEFAULT_MANAGER_ID or manager_id in self._agents:
                continue
            if self._bus.pending_count(manager_id) == 0:
                continue
            logger.warning("Manager %s does not run; dealing with its %d queued message(s)", manager_id,
                           self._bus.pending_count(manager_id))
            await self._deal_with(manager_id, self._bus.take_queue(manager_id))
