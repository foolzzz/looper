"""Agent mode entrypoint.

Wires up the full three-tier agent system:
  AgentSupervisor → MessageBus → ChannelRegistry
    → WebChannelAdapter
    → TelegramChannelAdapter / LarkChannelAdapter (enabled on the Settings page)
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
import contextlib
import logging
import os
import signal
from pathlib import Path
from typing import Any

from .broadcast import Broadcaster
from .bus import MessageBus
from .channels.base import ChannelAdapter
from .channels.registry import ChannelRegistry
from .channels.telegram import TelegramChannelAdapter, parse_allowed_user_ids
from .channels.web import WebChannelAdapter
from .http_server import AgentHTTPServer
from .supervisor import AgentSupervisor
from .tiers.manager import ManagerAgent, ManagerConfig
from .tiers.task import TaskRouter
from .tiers.worker import WorkerConfig, WorkerPool

logger = logging.getLogger(__name__)


def _telegram_adapter_from_config(config: dict[str, Any]) -> TelegramChannelAdapter | None:
    """Build the Telegram adapter from its Settings-page config, or None if invalid."""
    try:
        allowed = parse_allowed_user_ids(str(config.get("allowed_user_ids") or ""))
        return TelegramChannelAdapter(
            bot_token=os.environ.get(
                config.get("bot_token_env") or "LOOPX_TELEGRAM_BOT_TOKEN", ""
            ),
            allowed_user_ids=allowed or None,
        )
    except ValueError as exc:
        # Fail closed: a malformed allowlist must not fall back to "allow all".
        logger.error("telegram channel disabled: invalid allowed user IDs (%s)", exc)
        return None


async def _register_optional_channel(
    registry: ChannelRegistry, adapter: ChannelAdapter
) -> None:
    """Start a Settings-enabled channel; a failure leaves the web dashboard up."""
    try:
        await registry.register(adapter)
    except Exception:
        logger.exception("%s failed to start; continuing without it", adapter.channel_id)
        with contextlib.suppress(Exception):
            await registry.unregister(adapter.channel_id)


async def _run_agent_system(
    *,
    host: str = "127.0.0.1",
    port: int = 8767,
    runtime_root: Path,
    work_dir: Path,
    claude_bin: str = "claude",
) -> None:
    """Start the full agent system and run until interrupted."""

    # --- Database (required) ---
    from ..db import init_pool, ensure_schema, close_pool
    from ..db.agent_configs import seed_defaults as seed_agent_configs
    from ..db.llm_profiles import seed_defaults as seed_llm_profiles
    from .tiers.manager import DEFAULT_SYSTEM_PROMPT

    pool = await init_pool()
    await ensure_schema(pool)

    await seed_llm_profiles([
        {
            "name": "anthropic-default",
            "display_name": "Anthropic (Claude)",
            "provider_kind": "anthropic",
            "model": "claude-sonnet-4-20250514",
            "description": "Default Anthropic provider — used with Claude Code CLI",
        },
        {
            "name": "openai-default",
            "display_name": "OpenAI",
            "provider_kind": "openai",
            "model": "o3",
            "description": "Default OpenAI provider — used with Codex CLI",
        },
    ])

    await seed_agent_configs([
        {
            "agent_id": "manager",
            "display_name": "Manager (管家)",
            "role": "manager",
            "runtime": "claude-code",
            "system_prompt": DEFAULT_SYSTEM_PROMPT,
            "timeout_seconds": 0,
        },
        {
            "agent_id": "planner",
            "display_name": "Planner",
            "role": "planner",
            "runtime": "claude-code",
            "timeout_seconds": 1200,
            "system_prompt": (
                "You are a Planner Agent. Your job is to:\n"
                "1. Receive a task requirement\n"
                "2. Decompose it into concrete, executable steps\n"
                "3. Dispatch steps to Worker agents\n"
                "4. Monitor progress and verify results\n"
                "5. Report task completion or failure\n\n"
                "Always decompose complex tasks into multiple focused steps. "
                "Each step should be independently executable and verifiable."
            ),
        },
        {
            "agent_id": "worker",
            "display_name": "Worker",
            "role": "worker",
            "runtime": "claude-code",
            "max_concurrency": 3,
            "timeout_seconds": 600,
            "system_prompt": (
                "You are a Worker Agent. Execute the assigned task precisely:\n"
                "1. Read and understand the instruction\n"
                "2. Make the required code changes\n"
                "3. Run validation commands if provided\n"
                "4. Report results as structured JSON\n\n"
                "Focus on the specific task. Do not make changes beyond what is requested."
            ),
        },
    ])
    from ..db.channel_configs import seed_defaults as seed_channel_configs
    await seed_channel_configs([
        {
            "channel_id": "channel-telegram",
            "channel_type": "telegram",
            "display_name": "Telegram Bot",
            "enabled": False,
            "config": {
                "bot_token_env": "LOOPX_TELEGRAM_BOT_TOKEN",
                "allowed_user_ids": "",
                "description": "Telegram Bot integration via python-telegram-bot",
            },
        },
        {
            "channel_id": "channel-lark",
            "channel_type": "lark",
            "display_name": "Lark (飞书) Bot",
            "enabled": False,
            "config": {
                "app_id_env": "LARK_APP_ID",
                "app_secret_env": "LARK_APP_SECRET",
                "description": "Lark/Feishu Bot integration via lark-cli",
            },
        },
    ])

    from ..db.tasks import mark_orphaned_tasks_stopped
    orphan_count = await mark_orphaned_tasks_stopped(pool=pool)
    if orphan_count:
        logger.info("marked %d orphaned task(s) as stopped", orphan_count)

    logger.info("database initialised")

    broadcaster = Broadcaster()

    bus = MessageBus(runtime_root=runtime_root)
    supervisor = AgentSupervisor(bus=bus, runtime_root=runtime_root)
    registry = ChannelRegistry(bus=bus)

    # --- Channel Adapters ---

    web_adapter = WebChannelAdapter(host=host, port=port)
    await registry.register(web_adapter)

    from ..db.channel_configs import get_config as get_channel_config

    # Telegram and Lark are always available; the Settings page decides
    # whether they run (channel_configs.enabled), read once at startup.
    tg_cfg = await get_channel_config("channel-telegram")
    if tg_cfg and tg_cfg.get("enabled"):
        telegram_adapter = _telegram_adapter_from_config(tg_cfg.get("config") or {})
        if telegram_adapter is not None:
            await _register_optional_channel(registry, telegram_adapter)

    lark_cfg = await get_channel_config("channel-lark")
    if lark_cfg and lark_cfg.get("enabled"):
        from .channels.lark import LarkChannelAdapter
        lark_config = lark_cfg.get("config") or {}
        lark_adapter = LarkChannelAdapter(
            app_id=os.environ.get(lark_config.get("app_id_env") or "LARK_APP_ID", ""),
            runtime_root=runtime_root,
        )
        await _register_optional_channel(registry, lark_adapter)

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
        "agent system running on http://%s:%d (channels=%s)",
        host, port,
        ",".join(c["channel_id"] for c in registry.list_channels() if c.get("healthy")),
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
    await close_pool()
    logger.info("agent system stopped")


def serve_agent(
    *,
    host: str = "127.0.0.1",
    port: int = 8767,
    runtime_root: Path | None = None,
    work_dir: Path | None = None,
    claude_bin: str = "claude",
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
        )
    )
