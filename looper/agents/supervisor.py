"""Agent lifecycle supervisor: startup, health monitoring, crash recovery.

Manages the startup sequence (Bus → Channels → Manager → health loop) and
restarts a resident agent that is no longer healthy, for as long as the
server runs: after a delay that starts at ``RESTART_DELAY_MIN`` seconds and
doubles with each restart in a row, up to ``RESTART_DELAY_MAX``. An agent that
stays up ``STABLE_SECONDS`` after a restart starts the count again. One
restarted ``CRASH_LOOP_RESTARTS`` times in a row is crash-looping
(``AgentState``) until it is stable again: the overview shows it and the
Manager hears it (``crash_looping``, then ``recovered``).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Protocol

from .bus import MessageBus
from .message import (
    MANAGER_ADDRESS,
    AgentAddress,
    AgentLifecyclePayload,
    MessageEnvelope,
    MessageType,
)

logger = logging.getLogger(__name__)

# A restart waits this long, doubled for each restart in a row, up to the most.
RESTART_DELAY_MIN = 1.0
RESTART_DELAY_MAX = 300.0
# Up this long after its last restart, an agent is stable again: the next crash waits the least again.
STABLE_SECONDS = 300.0
# Restarted this many times in a row (without staying up STABLE_SECONDS), an agent is crash-looping.
CRASH_LOOP_RESTARTS = 3


class ManagedAgent(Protocol):
    """Protocol for an agent managed by the supervisor."""

    @property
    def agent_id(self) -> str: ...

    @property
    def agent_address(self) -> AgentAddress: ...

    async def start(self, bus: MessageBus) -> None: ...

    async def stop(self) -> None: ...

    def is_healthy(self) -> bool: ...


class AgentState(str, Enum):
    RUNNING = "running"
    RESTARTING = "restarting"  # down: it waits out its delay, then starts again
    CRASH_LOOPING = "crash_looping"  # restarted CRASH_LOOP_RESTARTS times in a row, up or down now


def restart_delay(restarts: int) -> float:
    """How long the restart after ``restarts`` restarts in a row waits."""
    return min(RESTART_DELAY_MIN * 2 ** restarts, RESTART_DELAY_MAX)


@dataclass
class AgentEntry:
    agent: ManagedAgent
    task: asyncio.Task[Any] | None = None
    # Restarts in a row since the agent last stayed up STABLE_SECONDS.
    restart_count: int = 0
    # Started and not found unhealthy since.
    up: bool = False
    started_at: datetime | None = None
    last_crash_at: datetime | None = None
    # While it waits to start again.
    next_restart_at: datetime | None = None
    restarting: asyncio.Task[Any] | None = None

    @property
    def running(self) -> bool:
        """Up now: started, not being restarted, and healthy by its own check."""
        return self.up and self.restarting is None and self.agent.is_healthy()

    @property
    def state(self) -> AgentState:
        if self.restart_count >= CRASH_LOOP_RESTARTS:
            return AgentState.CRASH_LOOPING
        return AgentState.RUNNING if self.running else AgentState.RESTARTING


class AgentSupervisor:
    """Manages agent lifecycle, health monitoring, and restart."""

    def __init__(
        self,
        bus: MessageBus,
        runtime_root: Path,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._bus = bus
        self._runtime_root = runtime_root
        self._agents: dict[str, AgentEntry] = {}
        self._health_interval = 30.0
        self._health_task: asyncio.Task[Any] | None = None
        self._running = False
        self._clock = clock
        self._sleep = sleep

    @property
    def bus(self) -> MessageBus:
        return self._bus

    def register(self, agent: ManagedAgent) -> None:
        self._agents[agent.agent_id] = AgentEntry(agent=agent)

    def unregister(self, agent_id: str) -> None:
        self._agents.pop(agent_id, None)

    async def start_all(self) -> None:
        """Start the agent hierarchy in order."""
        self._running = True

        # 1. Start message bus (replay undelivered)
        await self._bus.start()
        logger.info("message bus started")

        # 2. Start all registered agents
        for agent_id in list(self._agents):
            if not await self._start_agent(agent_id):
                self._schedule_restart(agent_id)

        # 3. Start health monitor
        self._health_task = asyncio.create_task(self._health_loop())
        logger.info("supervisor started with %d agents", len(self._agents))

    async def _start_agent(self, agent_id: str) -> bool:
        """Start the agent; whether it started."""
        entry = self._agents.get(agent_id)
        if not entry:
            return False

        try:
            await entry.agent.start(self._bus)
        except Exception:
            logger.exception("failed to start agent %s", agent_id)
            entry.up = False
            await self._publish_lifecycle(agent_id, entry.agent.agent_address, "start_failed")
            return False
        entry.up = True
        entry.started_at = self._clock()
        logger.info("agent %s started", agent_id)
        await self._publish_lifecycle(agent_id, entry.agent.agent_address, "started")
        return True

    async def stop_all(self) -> None:
        """Gracefully stop all agents and the bus."""
        self._running = False

        if self._health_task:
            self._health_task.cancel()
            try:
                await self._health_task
            except asyncio.CancelledError:
                pass
            self._health_task = None
        for entry in self._agents.values():
            if entry.restarting is not None:
                entry.restarting.cancel()
                await asyncio.gather(entry.restarting, return_exceptions=True)
                entry.restarting = None

        for agent_id, entry in reversed(list(self._agents.items())):
            try:
                await entry.agent.stop()
                logger.info("agent %s stopped", agent_id)
            except Exception:
                logger.exception("error stopping agent %s", agent_id)

        await self._bus.shutdown()
        logger.info("supervisor stopped")

    async def _health_loop(self) -> None:
        while self._running:
            await asyncio.sleep(self._health_interval)
            if not self._running:
                break
            await self.check_health()

    async def check_health(self) -> None:
        """One health check: restart each agent that is no longer healthy, and count one that stayed up long
        enough as stable again."""
        for agent_id, entry in list(self._agents.items()):
            if entry.restarting is not None:
                continue  # waiting out its delay, or starting
            if not entry.agent.is_healthy():
                entry.up = False
                entry.last_crash_at = self._clock()
                await self._publish_lifecycle(agent_id, entry.agent.agent_address, "crashed")
                self._schedule_restart(agent_id)
            elif entry.restart_count and entry.started_at is not None and \
                    (self._clock() - entry.started_at).total_seconds() >= STABLE_SECONDS:
                looping = entry.state is AgentState.CRASH_LOOPING
                logger.info("agent %s stable for %ds after %d restart(s) in a row", agent_id, STABLE_SECONDS,
                            entry.restart_count)
                entry.restart_count = 0
                if looping:
                    await self._publish_lifecycle(agent_id, entry.agent.agent_address, "recovered")

    def _schedule_restart(self, agent_id: str) -> None:
        entry = self._agents.get(agent_id)
        if entry is not None and entry.restarting is None:
            entry.restarting = asyncio.create_task(self._restart(agent_id))

    async def _restart(self, agent_id: str) -> None:
        """Start the agent again, after its delay, until it starts: it is never given up on."""
        entry = self._agents.get(agent_id)
        if not entry:
            return
        try:
            while True:  # until it starts, or stop_all cancels this
                delay = restart_delay(entry.restart_count)
                entry.next_restart_at = self._clock() + timedelta(seconds=delay)
                logger.error("agent %s is down (%d restart(s) in a row); restarting it in %gs", agent_id,
                             entry.restart_count, delay)
                try:
                    await entry.agent.stop()
                except Exception:
                    logger.exception("error stopping agent %s for restart", agent_id)
                await self._sleep(delay)
                entry.restart_count += 1
                entry.next_restart_at = None
                if entry.restart_count == CRASH_LOOP_RESTARTS:
                    logger.error("agent %s is crash-looping: restarted %d times in a row", agent_id,
                                 entry.restart_count)
                    await self._publish_lifecycle(agent_id, entry.agent.agent_address, "crash_looping")
                if await self._start_agent(agent_id):
                    return
                entry.last_crash_at = self._clock()
        finally:
            entry.next_restart_at = None
            entry.restarting = None

    async def _publish_lifecycle(
        self,
        agent_id: str,
        address: AgentAddress,
        event: str,
    ) -> None:
        try:
            envelope = MessageEnvelope.create(
                source=address,
                target=MANAGER_ADDRESS,
                message_type=MessageType.AGENT_LIFECYCLE,
                payload=AgentLifecyclePayload(
                    agent_id=agent_id,
                    agent_type=address.agent_type,
                    event=event,
                ),
            )
            await self._bus.publish(envelope)
        except Exception:
            logger.exception("failed to publish lifecycle event for %s", agent_id)

    def agent_status(self) -> dict[str, dict[str, Any]]:
        result: dict[str, dict[str, Any]] = {}
        for agent_id, entry in self._agents.items():
            state = entry.state
            result[agent_id] = {
                "healthy": state is AgentState.RUNNING,
                "state": state.value,
                "up": entry.running,
                "restart_count": entry.restart_count,
                "last_crash_at": entry.last_crash_at.isoformat() if entry.last_crash_at else None,
                "next_restart_at": entry.next_restart_at.isoformat() if entry.next_restart_at else None,
                "agent_type": entry.agent.agent_address.agent_type,
            }
        return result
