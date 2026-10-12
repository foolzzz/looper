"""Agent mode entrypoint.

Wires up the full three-tier agent system:
  AgentSupervisor → MessageBus → ChannelRegistry
    → WebChannelAdapter
    → TelegramChannelAdapter (optional)
    → LarkChannelAdapter (optional)
    → ManagerAgent
    → TaskRouter
    → WorkerPool
  AgentHTTPServer (REST API + SSE + static files)

Usage:
    loopx serve
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

from .broadcast import Broadcaster
from .bus import MessageBus
from .channels.registry import ChannelRegistry
from .channels.web import WebChannelAdapter
from .http_server import AgentHTTPServer
from .supervisor import AgentSupervisor
from .tiers.manager import ManagerAgent, ManagerConfig
from .tiers.task import TaskRouter
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

    # --- Database ---
    db_pool = None
    try:
        from ..db import init_pool, ensure_schema, close_pool
        from ..db.agent_configs import seed_defaults as seed_agent_configs
        from .tiers.manager import DEFAULT_SYSTEM_PROMPT

        db_pool = await init_pool()
        await ensure_schema(db_pool)

        await seed_agent_configs([
            {
                "agent_id": "manager",
                "display_name": "Manager (管家)",
                "role": "manager",
                "system_prompt": DEFAULT_SYSTEM_PROMPT,
                "timeout_seconds": 0,
            },
            {
                "agent_id": "planner",
                "display_name": "Planner",
                "role": "planner",
                "timeout_seconds": 1200,
            },
            {
                "agent_id": "worker",
                "display_name": "Worker",
                "role": "worker",
                "max_concurrency": 3,
                "timeout_seconds": 600,
            },
        ])
        logger.info("database initialised")
    except Exception:
        logger.warning("database unavailable — running without persistence", exc_info=True)

    broadcaster = Broadcaster()

    bus = MessageBus(runtime_root=runtime_root)
    supervisor = AgentSupervisor(bus=bus, runtime_root=runtime_root)
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
    manager = ManagerAgent(
        config=manager_config, runtime_root=runtime_root, broadcaster=broadcaster,
    )
    supervisor.register(manager)

    task_router = TaskRouter(runtime_root=runtime_root, broadcaster=broadcaster)
    supervisor.register(task_router)

    worker_config = WorkerConfig(
        claude_bin=claude_bin,
        work_dir=work_dir,
    )
    worker_pool = WorkerPool(config=worker_config, runtime_root=runtime_root)
    supervisor.register(worker_pool)

    # --- Start ---

    await supervisor.start_all()

    # --- HTTP Server ---

    http_server = AgentHTTPServer(
        host=host,
        port=port,
        web_adapter=web_adapter,
        task_router=task_router,
        supervisor=supervisor,
        broadcaster=broadcaster,
    )
    await http_server.start()

    logger.info(
        "agent system running on http://%s:%d (web=%s, telegram=%s, lark=%s)",
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
    await http_server.stop()
    await supervisor.stop_all()
    await registry.stop_all()
    await bus.shutdown()
    if db_pool is not None:
        try:
            from ..db import close_pool
            await close_pool()
        except Exception:
            pass
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
