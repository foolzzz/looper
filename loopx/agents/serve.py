"""Agent mode entrypoint.

Wires up the full three-tier agent system:
  AgentSupervisor → MessageBus → ChannelRegistry
    → WebChannelAdapter
    → TelegramChannelAdapter (optional)
    → LarkChannelAdapter (optional)
    → ManagerAgent
    → PlannerAgent
    → WorkerPool

Usage:
    loopx serve --agent-mode
    # or directly:
    from loopx.agents.serve import serve_agent
    serve_agent(host="127.0.0.1", port=8767)
"""

from __future__ import annotations

import asyncio
import logging
import signal
from pathlib import Path
from typing import Any

from .bus import MessageBus
from .channels.registry import ChannelRegistry
from .channels.web import WebChannelAdapter
from .supervisor import AgentSupervisor
from .tiers.manager import ManagerAgent, ManagerConfig
from .tiers.planner import PlannerAgent
from .tiers.worker import WorkerConfig, WorkerPool

logger = logging.getLogger(__name__)


async def _run_agent_system(
    *,
    host: str = "127.0.0.1",
    port: int = 8767,
    runtime_root: Path,
    work_dir: Path,
    claude_bin: str = "claude",
    enable_telegram: bool = False,
    enable_lark: bool = False,
    lark_app_id: str = "",
) -> None:
    """Start the full agent system and run until interrupted."""

    bus = MessageBus(runtime_root=runtime_root)
    supervisor = AgentSupervisor(bus=bus)
    registry = ChannelRegistry(bus=bus)

    # --- Channel Adapters ---

    web_adapter = WebChannelAdapter(host=host, port=port)
    await registry.register(web_adapter)

    if enable_telegram:
        from .channels.telegram import TelegramChannelAdapter
        telegram_adapter = TelegramChannelAdapter()
        await registry.register(telegram_adapter)
        logger.info("telegram channel enabled")

    if enable_lark:
        from .channels.lark import LarkChannelAdapter
        lark_adapter = LarkChannelAdapter(
            app_id=lark_app_id,
            runtime_root=runtime_root,
        )
        await registry.register(lark_adapter)
        logger.info("lark channel enabled")

    # --- Agent Tiers ---

    manager_config = ManagerConfig(
        claude_bin=claude_bin,
        work_dir=work_dir,
    )
    manager = ManagerAgent(config=manager_config, runtime_root=runtime_root)
    supervisor.register(manager)

    planner = PlannerAgent(runtime_root=runtime_root)
    supervisor.register(planner)

    worker_config = WorkerConfig(
        claude_bin=claude_bin,
        work_dir=work_dir,
    )
    worker_pool = WorkerPool(config=worker_config, runtime_root=runtime_root)
    supervisor.register(worker_pool)

    # --- Start ---

    await supervisor.start_all()
    logger.info(
        "agent system running on %s:%d (web=%s, telegram=%s, lark=%s)",
        host, port,
        True,
        enable_telegram,
        enable_lark,
    )

    # Wait for shutdown signal
    stop_event = asyncio.Event()

    def _signal_handler() -> None:
        logger.info("shutdown signal received")
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _signal_handler)

    await stop_event.wait()

    # --- Shutdown ---

    logger.info("shutting down agent system...")
    await supervisor.stop_all()
    await registry.stop_all()
    await bus.shutdown()
    logger.info("agent system stopped")


def serve_agent(
    *,
    host: str = "127.0.0.1",
    port: int = 8767,
    runtime_root: Path | None = None,
    work_dir: Path | None = None,
    claude_bin: str = "claude",
    enable_telegram: bool = False,
    enable_lark: bool = False,
    lark_app_id: str = "",
    verbose: bool = False,
) -> None:
    """Synchronous entry point for the agent system."""
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    resolved_root = runtime_root or Path.cwd()
    resolved_work_dir = work_dir or Path.cwd()

    asyncio.run(
        _run_agent_system(
            host=host,
            port=port,
            runtime_root=resolved_root,
            work_dir=resolved_work_dir,
            claude_bin=claude_bin,
            enable_telegram=enable_telegram,
            enable_lark=enable_lark,
            lark_app_id=lark_app_id,
        )
    )
