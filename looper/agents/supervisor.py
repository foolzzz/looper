"""Agent lifecycle supervisor: startup, health monitoring, crash recovery.

Manages the startup sequence (Bus → Channels → Manager → health loop) and
restarts failed agents with exponential backoff.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
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


class ManagedAgent(Protocol):
    """Protocol for an agent managed by the supervisor."""

    @property
    def agent_id(self) -> str: ...

    @property
    def agent_address(self) -> AgentAddress: ...

    async def start(self, bus: MessageBus) -> None: ...

    async def stop(self) -> None: ...

    def is_healthy(self) -> bool: ...


@dataclass
class AgentEntry:
    agent: ManagedAgent
    task: asyncio.Task[Any] | None = None
    restart_count: int = 0
    max_restarts: int = 3
    healthy: bool = True


class AgentSupervisor:
    """Manages agent lifecycle, health monitoring, and restart."""

    def __init__(self, bus: MessageBus, runtime_root: Path) -> None:
        self._bus = bus
        self._runtime_root = runtime_root
        self._agents: dict[str, AgentEntry] = {}
        self._health_interval = 30.0
        self._health_task: asyncio.Task[Any] | None = None
        self._running = False

    @property
    def bus(self) -> MessageBus:
        return self._bus

    def register(
        self,
        agent: ManagedAgent,
        *,
        max_restarts: int = 3,
    ) -> None:
        self._agents[agent.agent_id] = AgentEntry(
            agent=agent,
            max_restarts=max_restarts,
        )

    def register_dynamic(
        self,
        agent: ManagedAgent,
    ) -> None:
        """Register an agent that is managed externally (e.g. by TaskRouter).

        Dynamic agents are tracked for status reporting but NOT restarted
        by the health loop — their owner manages lifecycle.
        """
        self._agents[agent.agent_id] = AgentEntry(
            agent=agent,
            max_restarts=0,
        )

    def unregister(self, agent_id: str) -> None:
        self._agents.pop(agent_id, None)

    async def start_all(self) -> None:
        """Start the agent hierarchy in order."""
        self._running = True

        # 1. Start message bus (replay undelivered)
        await self._bus.start()
        logger.info("message bus started")

        # 2. Start all registered agents
        for agent_id, entry in self._agents.items():
            await self._start_agent(agent_id)

        # 3. Start health monitor
        self._health_task = asyncio.create_task(self._health_loop())
        logger.info("supervisor started with %d agents", len(self._agents))

    async def _start_agent(self, agent_id: str) -> None:
        entry = self._agents.get(agent_id)
        if not entry:
            return

        try:
            await entry.agent.start(self._bus)
            entry.healthy = True
            logger.info("agent %s started", agent_id)

            await self._publish_lifecycle(agent_id, entry.agent.agent_address, "started")
        except Exception:
            logger.exception("failed to start agent %s", agent_id)
            entry.healthy = False
            await self._publish_lifecycle(agent_id, entry.agent.agent_address, "start_failed")

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
            for agent_id, entry in list(self._agents.items()):
                if entry.max_restarts == 0:
                    continue
                if not entry.agent.is_healthy():
                    logger.warning("agent %s unhealthy", agent_id)
                    entry.healthy = False
                    await self._restart(agent_id)

    async def _restart(self, agent_id: str) -> None:
        entry = self._agents.get(agent_id)
        if not entry:
            return

        if entry.restart_count >= entry.max_restarts:
            logger.error(
                "agent %s exceeded max restarts (%d)",
                agent_id,
                entry.max_restarts,
            )
            await self._publish_lifecycle(
                agent_id, entry.agent.agent_address, "max_restarts_exceeded"
            )
            return

        try:
            await entry.agent.stop()
        except Exception:
            logger.exception("error stopping agent %s for restart", agent_id)

        backoff = min(2 ** entry.restart_count, 60)
        logger.info("restarting agent %s in %ds (attempt %d)", agent_id, backoff, entry.restart_count + 1)
        await asyncio.sleep(backoff)

        entry.restart_count += 1
        await self._start_agent(agent_id)

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
            result[agent_id] = {
                "healthy": entry.healthy and entry.agent.is_healthy(),
                "restart_count": entry.restart_count,
                "max_restarts": entry.max_restarts,
                "agent_type": entry.agent.agent_address.agent_type,
            }
        return result
