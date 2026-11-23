"""Agent mode entrypoint.

Wires up the full three-tier agent system:
  AgentSupervisor → MessageBus → ChannelRegistry
    → WebChannelAdapter
    → TelegramChannelAdapter / LarkChannelAdapter (enabled on the Settings page,
      each passing on only the senders on its allowlist)
    → ManagerAgent
    → TaskRouter
    → WorkerPool
  Scheduler (fires the projects' schedules)
  AgentHTTPServer (REST API + SSE + static files)

Usage:
    looper serve
    # or directly:
    from looper.agents.serve import serve_agent
    serve_agent(port=8767)  # with the ops file's other settings
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import logging
import os
import signal
import time
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .llmrec.control import ProxyControl
    from .managers import ManagerFleet

from .. import ops_config
from ..ops_config import KeeperMode, OpsConfig
from ..db import connection
from .control_plane import ControlPlaneLease, EXIT_LOCK_LOST, LockHeld
from .broadcast import Broadcaster
from .bus import MessageBus
from .channels.allowlist import SenderAllowlist, allowlist_from_config, start_problem
from .channels.startup import ChannelStartError
from .channels.base import ChannelAdapter
from .channels.bots import BOT_TYPES, builtin_env_token, clashes, lark_app_id
from .channels.lark import LarkChannelAdapter
from .channels.registry import ChannelRegistry
from .channels.chat_routing import ChatResolver, ChatStop
from .channels.telegram import TelegramChannelAdapter
from .channels.web import WebChannelAdapter
from . import db_backup, keeper, self_check
from .kept_run import KeeperSettings
from .gate_courier import GateCourier
from .gate_service import GateReminders
from .drain import DrainPhase, DrainScope, DrainState
from .wait_service import WaitService
from .ledger_reconciler import LedgerReconciler
from .source_service import SourceService, own_bot_app_ids
from .intent_push import IntentPushService
from .memory_write import MemoryWriteService
from .memory_draft_service import MemoryDraftService
from .memory_push import MemoryPushService
from .reminders import ReminderService
from .idle_checks import IdleCheckService, PostFn
from .morning_brief import MorningBriefService
from .metrics_alerts import MetricsAlertService
from .http_server import AgentHTTPServer
from .loop_watch import LoopWatch
from .log_redaction import log_to_stderr
from .scheduler import Scheduler
from .message import MessageType
from .runtimes.runner.supervisor import RunnerSupervisor
from .supervisor import AgentSupervisor
from .tiers.manager import ManagerConfig
from .tiers.task import TaskRouter
from .tiers.worker import WorkerConfig, WorkerPool

logger = logging.getLogger(__name__)


async def _prepare_idle_checks(bus: MessageBus, fleet: ManagerFleet, drain: DrainState, *, post: PostFn) -> IdleCheckService:
    """Recover before bus replay; callbacks also follow a settings-driven Manager replacement."""
    from .tiers import idle_check_turn

    idle_check_turn.configure(post)
    service = IdleCheckService(bus, idle_for=lambda: fleet.default.idle_for(),
                               manager_running=lambda: fleet.is_running("manager"), drain=drain)
    await service.recover()
    return service


class _SuspendDetector:
    """Detect wall time that elapsed while the monotonic clock was paused."""

    def __init__(self, *, wall_clock: Callable[[], float] = time.time,
                 monotonic_clock: Callable[[], float] = time.monotonic) -> None:
        self._wall_clock, self._monotonic_clock = wall_clock, monotonic_clock
        self._wall, self._monotonic = wall_clock(), monotonic_clock()

    def check(self) -> None:
        wall, monotonic = self._wall_clock(), self._monotonic_clock()
        suspended = (wall - self._wall) - (monotonic - self._monotonic)
        self._wall, self._monotonic = wall, monotonic
        if suspended > 60:
            logger.warning("the system was suspended for about %.0f s (sleep); timers and runs paused", suspended)


class _StartupSteps:
    """Elapsed seconds in execution order; only entered steps are recorded."""

    def __init__(self, started_at: float) -> None:
        self.started_at = started_at
        self.steps: dict[str, float] = {"imports": time.monotonic() - started_at}

    @contextlib.contextmanager
    def measure(self, name: str) -> Iterator[None]:
        started = time.monotonic()
        logger.info("startup step: %s", name)
        try:
            yield
        finally:
            self.steps[name] = time.monotonic() - started

    def finish(self) -> str:
        self.steps["total"] = time.monotonic() - self.started_at
        return " ".join(f"{name}={seconds:.1f}" for name, seconds in self.steps.items())


class StartupCheckFailed(RuntimeError):
    """A check Looper cannot start without failed; the message says what to fix."""


def _sender_allowlist(channel_id: str, channel_type: str, config: dict[str, Any]) -> SenderAllowlist | None:
    """The channel's sender allowlist; None (logged) when an empty or invalid list keeps it from starting."""
    if problem := start_problem(channel_type, config):
        logger.error("%s not started: %s", channel_id, problem)
        return None
    return allowlist_from_config(channel_id, channel_type, config)


def _telegram_adapter_from_config(
    config: dict[str, Any], bot_token: str = "", *, channel_id: str = "channel-telegram",
    manager_id: str = "manager", manager_running: Callable[[str], bool] | None = None,
    resolve_chat: ChatResolver | None = None, stop_chat: ChatStop | None = None,
) -> TelegramChannelAdapter | None:
    """Build the Telegram adapter from its Settings-page config; None when its allowlist keeps it from starting.

    The token comes from ``channel_secrets`` (``bot_token``); for the built-in row that predates DB-stored tokens it
    falls back to the environment variable the old ``bot_token_env`` config key names (default
    LOOPER_TELEGRAM_BOT_TOKEN). An added bot gets only its stored token.
    """
    allowlist = _sender_allowlist(channel_id, "telegram", config)
    if allowlist is None:
        return None
    token = bot_token
    if not token and channel_id == "channel-telegram":
        token = builtin_env_token(config)
    return TelegramChannelAdapter(bot_token=token, allowlist=allowlist, channel_id=channel_id,
                                  manager_id=manager_id, manager_running=manager_running,
                                  resolve_chat=resolve_chat, stop_chat=stop_chat,
                                  backlog_max_age_seconds=config.get("backlog_max_age_seconds", 600))


def _lark_adapter_from_config(config: dict[str, Any], runtime_root: Path, *,
                              bot_open_id: str = "", bot_app_id: str = "", channel_id: str = "channel-lark",
                              manager_id: str = "manager",
                              manager_running: Callable[[str], bool] | None = None,
                              resolve_chat: ChatResolver | None = None,
                              stop_chat: ChatStop | None = None) -> LarkChannelAdapter | None:
    """Build the Lark adapter from its Settings-page config; None when its allowlist keeps it from starting.
    ``bot_open_id`` is the bot's own open_id, which tells a group message that @-mentions it."""
    allowlist = _sender_allowlist(channel_id, "lark", config)
    if allowlist is None:
        return None
    from ..db.channel_configs import record_rejected

    async def rejected(sender_id: str, chat_id: str | None) -> None:
        await record_rejected(channel_id, sender_id, chat_id=chat_id)

    return LarkChannelAdapter(profile=config.get("profile") or "", runtime_root=runtime_root, allowlist=allowlist,
                              on_rejected=rejected, bot_open_id=bot_open_id, bot_app_id=bot_app_id, channel_id=channel_id,
                              manager_id=manager_id, manager_running=manager_running,
                              resolve_chat=resolve_chat, stop_chat=stop_chat)


async def _lark_adapter(config: dict[str, Any], runtime_root: Path, *, channel_id: str = "channel-lark",
                        manager_id: str = "manager",
                        manager_running: Callable[[str], bool] | None = None,
                        same_bot_as: Callable[[str], str | None] | None = None,
                        resolve_chat: ChatResolver | None = None, stop_chat: ChatStop | None = None,
                        raise_errors: bool = False) -> LarkChannelAdapter | None:
    """The Lark adapter, once lark-cli acts as the bot under the channel's profile and the allowlist was made for
    that app, and ``same_bot_as`` (given the app it acts as) names no running channel of that app; None (logged)
    otherwise."""
    from .channels import lark_cli

    if _sender_allowlist(channel_id, "lark", config) is None:
        return None
    problem, bot = await lark_cli.check(config, fresh=True, **({"raise_errors": True} if raise_errors else {}))
    if problem or bot is None:
        logger.error("%s not started: %s", channel_id, problem)
        if raise_errors:
            raise ChannelStartError(problem or "the bot identity is unavailable")
        return None
    if same_bot_as is not None and (earlier := same_bot_as(bot.app_id)) is not None:
        logger.error("%s not started: it is the same bot as %s", channel_id, earlier)
        if raise_errors:
            raise ChannelStartError(f"it is the same bot as {earlier}")
        return None
    logger.info("%s: bot %s (app %s, lark-cli profile %s)", channel_id, bot.bot_name, bot.app_id, bot.profile)
    return _lark_adapter_from_config(config, runtime_root, bot_open_id=bot.bot_open_id, bot_app_id=bot.app_id, channel_id=channel_id,
                                     manager_id=manager_id, manager_running=manager_running,
                                     resolve_chat=resolve_chat, stop_chat=stop_chat)


async def _lark_bot_adapter(config: dict[str, Any], secret: str, runtime_root: Path, *,
                            channel_id: str = "channel-lark_bot", manager_id: str = "manager",
                            manager_running: Callable[[str], bool] | None = None,
                            resolve_chat: ChatResolver | None = None, stop_chat: ChatStop | None = None,
                            raise_errors: bool = False) -> LarkChannelAdapter | None:
    """Start the bot-token channel only with a valid allowlist and matching app credentials."""
    from ..db.channel_configs import record_rejected
    from .channels import lark_bot_settings
    from .channels.lark_bot import make_adapter
    from .log_redaction import add_secret

    allowlist = _sender_allowlist(channel_id, "lark_bot", config)
    if allowlist is None:
        return None
    add_secret(secret)
    problem, bot = await lark_bot_settings.check(
        config, secret, fresh=True, **({"raise_errors": True} if raise_errors else {}))
    if problem or bot is None:
        logger.error("%s not started: %s", channel_id, problem)
        if raise_errors:
            raise ChannelStartError(problem or "the bot identity is unavailable")
        return None
    logger.info("%s: bot %s (app %s)", channel_id, bot.app_name, config.get("app_id"))

    async def rejected(sender_id: str, chat_id: str | None) -> None:
        await record_rejected(channel_id, sender_id, chat_id=chat_id)

    return make_adapter(config, secret, runtime_root=runtime_root, allowlist=allowlist,
                        on_rejected=rejected, bot_open_id=bot.open_id, channel_id=channel_id,
                        manager_id=manager_id, manager_running=manager_running,
                        resolve_chat=resolve_chat, stop_chat=stop_chat)


async def _enabled_bot_rows() -> list[dict[str, Any]]:
    """The enabled bot rows with their stored secret and ``clashes`` keys (``env_token``, ``app_id``), built-in rows
    first. Rows of one bot are all kept: ``_register_bots`` starts the first that can start, so a row that cannot
    (no valid sender allowlist, lark-cli not signed in) leaves the bot to a later row."""
    from ..db.channel_configs import get_bot_token, list_configs

    rows: list[dict[str, Any]] = []
    for row in await list_configs():
        if row["enabled"] and row["channel_type"] in BOT_TYPES:
            rows.append({**row, "config": row.get("config") or {},
                         "secret": await get_bot_token(row["channel_id"]) or ""})
    return [{**r, "env_token": builtin_env_token(r["config"]) if r["channel_id"] == "channel-telegram" else "",
             "app_id": await lark_app_id(r["channel_type"], r["config"])} for r in rows]


async def _register_bots(registry: ChannelRegistry, rows: list[dict[str, Any]], runtime_root: Path,
                         manager_running: Callable[[str], bool], channel_types: tuple[str, ...],
                         *, stop_chat: ChatStop | None = None, startup: _StartupSteps | None = None) -> None:
    """Build and register the adapter of each row of ``channel_types``; one that fails to start leaves the rest.

    A lark-cli row's app is known for sure only once its adapter asks lark-cli (``_enabled_bot_rows`` may not have
    learnt it), so each row is checked again against the bots this pass started, with the app its adapter found."""
    started: list[dict[str, Any]] = []

    def same_bot_as(row: dict[str, Any], app_id: str | None) -> str | None:
        return clashes([*started, {**row, "app_id": app_id}]).get(row["channel_id"])

    for initial in rows:
        if initial["channel_type"] not in channel_types:
            continue

        async def start(retry: bool, initial: dict[str, Any] = initial) -> bool:
            row = initial
            if retry:
                from ..db.channel_configs import get_bot_token, get_config
                current = await get_config(initial["channel_id"])
                if current is None or not current["enabled"]:
                    return False
                config = current.get("config") or {}
                row = {**current, "config": config, "secret": await get_bot_token(current["channel_id"]) or "",
                       "env_token": builtin_env_token(config) if current["channel_id"] == "channel-telegram" else "",
                       "app_id": config.get("app_id") if current["channel_type"] == "lark_bot" else None}
            kwargs: dict[str, Any] = {"channel_id": row["channel_id"], "manager_id": row.get("manager_id") or "manager",
                                      "manager_running": manager_running, "resolve_chat": registry.resolve_chat,
                                      "stop_chat": stop_chat}
            adapter: ChannelAdapter | None
            found: list[str] = []
            if row["channel_type"] == "lark":
                def lark_same_bot_as(app_id: str) -> str | None:
                    found.append(app_id)
                    return same_bot_as(row, app_id)

                adapter = await _lark_adapter(row["config"], runtime_root, same_bot_as=lark_same_bot_as,
                                              raise_errors=True, **kwargs)
            elif (earlier := same_bot_as(row, row.get("app_id"))) is not None:
                logger.error("%s not started: it is the same bot as %s", row["channel_id"], earlier)
                raise ChannelStartError(f"it is the same bot as {earlier}")
            elif row["channel_type"] == "telegram":
                adapter = _telegram_adapter_from_config(row["config"], row["secret"], **kwargs)
            else:
                adapter = await _lark_bot_adapter(row["config"], row["secret"], runtime_root,
                                                  raise_errors=True, **kwargs)
            if adapter is None:
                raise ChannelStartError(start_problem(row["channel_type"], row["config"]) or "channel cannot start")
            await _register_optional_channel(registry, adapter, raise_errors=True)
            started.append({**row, "app_id": found[0] if found else row.get("app_id")})
            return True

        with startup.measure(f"channel:{initial['channel_id']}") if startup else contextlib.nullcontext():
            await registry.start_bot(initial["channel_id"], start)


async def seed_channel_configs(pool: Any = None) -> None:
    """Give Telegram and Lark their config rows, disabled and with empty sender allowlists; existing rows are kept."""
    from ..db.channel_configs import seed_defaults

    await seed_defaults([
        {
            "channel_id": "channel-telegram",
            "channel_type": "telegram",
            "display_name": "Telegram Bot",
            "enabled": False,
            "config": {
                "allowed_user_ids": [],
                "description": "Telegram Bot integration via python-telegram-bot",
            },
        },
        {
            "channel_id": "channel-lark",
            "channel_type": "lark",
            "display_name": LARK_DISPLAY_NAME,
            "enabled": False,
            "config": {
                "profile": "",
                "allowed_open_ids": [],
                "description": "The Manager in Lark direct messages and group @-mentions, through lark-cli",
            },
        },
        {
            "channel_id": "channel-lark_bot",
            "channel_type": "lark_bot",
            "display_name": "Lark (bot token)",
            "enabled": False,
            "config": {
                "app_id": "",
                "domain": "feishu",
                "allowed_open_ids": [],
                "description": "The Manager in Lark direct messages and group @-mentions, through the Lark open "
                               "platform with an app id and secret",
            },
        },
    ], pool=pool)


LARK_DISPLAY_NAME = "Lark (lark-cli)"


async def seed_role_configs(pool: Any = None) -> None:
    """Give every role its config row and current recommended role prompt.

    A row whose role prompt is still the recommended one follows a new
    recommendation; an edited one is kept (``role_configs.seed_defaults``).
    """
    from ..db.role_configs import seed_defaults
    from .advice import ADVISOR_ROLE_PROMPT, JUDGE_ROLE_PROMPT
    from .advisor import ADVISOR_TIMEOUT_SEC, JUDGE_TIMEOUT_SEC
    from .leads_check import CHECK_TIMEOUT_SEC, LEADS_CHECK_ROLE_PROMPT
    from .explorer import EXPLORER_MAX_TURNS, EXPLORER_ROLE_PROMPT, EXPLORER_TIMEOUT_SEC
    from .outbound import OUTBOUND_ROLE_PROMPT, REVIEW_TIMEOUT_SEC as OUTBOUND_TIMEOUT_SEC
    from .memory_draft_role import ROLE_PROMPT as MEMORY_DRAFT_ROLE_PROMPT, TIMEOUT_SEC as MEMORY_DRAFT_TIMEOUT_SEC
    from .processor import PROCESSOR_ROLE_PROMPT, PROCESSOR_TIMEOUT_SEC
    from .tiers.manager import DEFAULT_SYSTEM_PROMPT
    from .tiers.planner import DEFAULT_SYSTEM_PROMPT as PLANNER_SYSTEM_PROMPT
    from .tiers.worker import DEFAULT_SYSTEM_PROMPT as WORKER_SYSTEM_PROMPT, REVIEWER_SYSTEM_PROMPT

    await seed_defaults([
        {
            "role": "manager",
            "display_name": "Manager (管家)",
            "system_prompt": DEFAULT_SYSTEM_PROMPT,
            "timeout_seconds": 0,
        },
        {
            "role": "planner",
            "display_name": "Planner",
            "timeout_seconds": 1200,  # per Planner turn
            "system_prompt": PLANNER_SYSTEM_PROMPT,
        },
        {
            "role": "worker",
            "display_name": "Worker",
            "timeout_seconds": 86400,
            # The role prompt of every Worker (the report contract follows it).
            "system_prompt": WORKER_SYSTEM_PROMPT,
        },
        {
            "role": "reviewer",
            "display_name": "Reviewer",
            "timeout_seconds": 86400,
            # The role prompt of every Reviewer (the verdict contract follows it).
            "system_prompt": REVIEWER_SYSTEM_PROMPT,
        },
        {
            "role": "processor",
            "display_name": "Processor",
            "timeout_seconds": PROCESSOR_TIMEOUT_SEC,  # at most, per run
            # Its instructions and reply contract follow it.
            "system_prompt": PROCESSOR_ROLE_PROMPT,
        },
        {
            "role": "advisor",
            "display_name": "Advisor",
            "timeout_seconds": ADVISOR_TIMEOUT_SEC,  # at most, per round
            # Its reply contract follows it.
            "system_prompt": ADVISOR_ROLE_PROMPT,
        },
        {
            "role": "advice_check",
            "display_name": "Advice check",
            "timeout_seconds": JUDGE_TIMEOUT_SEC,  # at most, per check
            # Its reply contract follows it.
            "system_prompt": JUDGE_ROLE_PROMPT,
        },
        {
            "role": "leads_check",
            "display_name": "Leads check",
            "timeout_seconds": CHECK_TIMEOUT_SEC,  # at most, per check
            # Its instructions and reply contract follow it.
            "system_prompt": LEADS_CHECK_ROLE_PROMPT,
        },
        {
            "role": "memory_draft",
            "display_name": "Memory draft",
            "timeout_seconds": MEMORY_DRAFT_TIMEOUT_SEC,
            "max_turns": 1,
            "system_prompt": MEMORY_DRAFT_ROLE_PROMPT,
        },
        {
            "role": "explorer",
            "display_name": "Explorer",
            "timeout_seconds": EXPLORER_TIMEOUT_SEC,
            "max_turns": EXPLORER_MAX_TURNS,
            "system_prompt": EXPLORER_ROLE_PROMPT,
        },
        {
            "role": "outbound_check",
            "display_name": "Outbound check",
            "timeout_seconds": OUTBOUND_TIMEOUT_SEC,  # at most, per review
            # Its reply contract follows it.
            "system_prompt": OUTBOUND_ROLE_PROMPT,
        },
    ], pool=pool)


async def _register_optional_channel(
    registry: ChannelRegistry, adapter: ChannelAdapter, *, raise_errors: bool = False
) -> bool:
    """Whether a Settings-enabled channel started; a failure leaves the web dashboard up and its bot unclaimed."""
    try:
        await registry.register(adapter)
        if raise_errors and not adapter.healthcheck().get("healthy"):
            raise ChannelStartError("adapter did not start; check its configuration and installed transport")
    except asyncio.CancelledError:
        await registry.unregister(adapter.channel_id)
        raise
    except Exception:
        logger.exception("%s failed to start; continuing without it", adapter.channel_id)
        with contextlib.suppress(Exception):
            await registry.unregister(adapter.channel_id)
        if raise_errors:
            raise
        return False
    return True


async def _back_up_before_schema_change(pool: Any, ops: OpsConfig, runtime_root: Path, *, skip_backup: bool) -> None:
    """Dump the database (``ops.database_url``, the way ``ops`` says) into ``<runtime root>/backups`` when
    ``ensure_schema`` is about to change it.

    Raises ``StartupCheckFailed``, before any DDL, when there is no usable
    ``pg_dump`` or the dump fails. Registry upgrades cannot skip the backup.
    """
    from ..db.connection import REGISTRY_SCHEMA_VERSION, check_registry_schema_version, schema_change_pending
    from . import db_backup

    async with pool.acquire() as conn:
        if not await schema_change_pending(conn):
            return
        version = await check_registry_schema_version(conn, supported_version=REGISTRY_SCHEMA_VERSION)
        registry_upgrade = version != REGISTRY_SCHEMA_VERSION and await conn.fetchval(
            "SELECT to_regclass('llm_providers') IS NOT NULL")
        server_version_num = int(await conn.fetchval("SELECT current_setting('server_version_num')"))
    dsn = ops.database_url
    shown = self_check.mask_dsn(dsn)
    if skip_backup:
        if registry_upgrade:
            raise StartupCheckFailed("registry schema upgrade requires a verified full database backup; "
                                     "--skip-backup is not allowed; the schema was not changed")
        logger.warning("BACKUP SKIPPED (--skip-backup): the database schema of %s changes now without a backup; "
                       "the columns and tables its migrations drop cannot be restored", shown)
        return
    check, dumper = await asyncio.to_thread(self_check.check_pg_dump, server_version_num, True, ops)
    if dumper is None:
        raise StartupCheckFailed(self_check.format_checks([check]))
    started = time.monotonic()
    if registry_upgrade:
        logger.info("registry migration version=%s target_version=%s phase=backup rollback=not_needed",
                    version, REGISTRY_SCHEMA_VERSION)
    else:
        logger.info("the database schema is about to change; backing up %s with %s", shown, dumper.detail)
    try:
        path, checked = await asyncio.to_thread(
            db_backup.back_up, dumper, dsn, runtime_root / db_backup.BACKUP_DIRNAME)
    except db_backup.BackupFailed as exc:
        if registry_upgrade:
            logger.error("registry migration version=%s phase=backup_failed elapsed_seconds=%.3f rollback=not_needed",
                         version, time.monotonic() - started)
            raise StartupCheckFailed(f"registry schema backup verification failed: {exc}; the schema was not changed") from None
        raise StartupCheckFailed(self_check.format_checks([self_check.Check(
            "pg_dump", "pg_dump backup", self_check.FAIL, f"{exc}; the schema was not changed",
            "fix the problem above and start again, or migrate once without a backup: looper serve --skip-backup",
        )])) from None
    if registry_upgrade:
        logger.info("registry migration version=%s phase=backup_verified elapsed_seconds=%.3f bytes=%d rollback=not_needed",
                    version, time.monotonic() - started, path.stat().st_size)
    logger.info("backed up the database to %s (%d bytes, %s); this dump keeps the source_events schema and leaves "
                "out its rows; restore only into a new isolated database, "
                "preserving the migrated database and new writes: "
                "pg_restore --no-owner --dbname '<isolated-database-url>' '%s'",
                path, path.stat().st_size, checked, path)


@dataclass(frozen=True)
class LeftRunning:
    """The tasks a previous server was running that carry on: from their live plan (``resumable``), or from the start
    (``starting``: it had started them without giving them a Planner yet)."""

    resumable: list[str]
    starting: list[str]


async def tasks_left_running(pool: Any) -> LeftRunning:
    """The tasks the previous server was running that carry on (``carry_on_tasks``, once the agents are up); every
    other task it was running is marked stopped now."""
    from ..db.plans import resumable_task_ids
    from ..db.tasks import ACTIVE_STATUSES, mark_orphaned_tasks_stopped, started_task_ids

    resumable = await resumable_task_ids(ACTIVE_STATUSES, pool=pool)
    starting = await started_task_ids(besides=resumable, pool=pool)
    orphan_count = await mark_orphaned_tasks_stopped(keep=resumable + starting, pool=pool)
    if orphan_count:
        logger.info("marked %d orphaned task(s) as stopped", orphan_count)
    return LeftRunning(resumable, starting)


async def carry_on_tasks(task_router: TaskRouter, left: LeftRunning) -> None:
    """Resume the tasks with a live plan and start those the previous server had not given a Planner yet."""
    if left.resumable:
        logger.info("resuming %d task(s) from their saved plans", len(left.resumable))
        await task_router.resume_tasks(left.resumable)
    if left.starting:
        logger.info("starting %d task(s) the previous server had not given a Planner yet", len(left.starting))
        await task_router.start_tasks(left.starting)


# How long a shutdown may take before the server exits without finishing it: under the configured 30 s service budget
# between SIGTERM and SIGKILL. The next start ends what it leaves (the agents' processes, their runs' records).
SHUTDOWN_DEADLINE_SEC = 15.0


class ShutdownMode(StrEnum):
    END_RUNS = "end_runs"
    DETACH_KEPT = "detach_kept"


def shutdown_mode(*, keeper_on: bool, drain: DrainState, sig: int) -> ShutdownMode:
    """Only a planned turns-drain SIGTERM may leave kept agents running."""
    if (sig == signal.SIGTERM and keeper_on and drain.status.phase is not DrainPhase.SERVING
            and drain.status.scope is DrainScope.TURNS):
        return ShutdownMode.DETACH_KEPT
    return ShutdownMode.END_RUNS


@dataclass
class Fence:
    """Loss bypasses normal shutdown: stop database writes and end only owned processes."""

    http_server: AgentHTTPServer | None = None
    worker_pool: WorkerPool | None = None
    recording_proxy: ProxyControl | None = None
    exit_now: Callable[[int], Any] = os._exit
    _lost: bool = False
    _backstop: asyncio.TimerHandle | None = None
    _ending: asyncio.Task | None = None

    def lost(self, reason: str) -> None:
        if self._lost:
            return
        self._lost = True
        from . import proc_tree

        proc_tree.fence_spawns()
        loop = asyncio.get_running_loop()
        self._backstop = loop.call_later(SHUTDOWN_DEADLINE_SEC, self.exit, EXIT_LOCK_LOST)
        connection.fence()
        if self.worker_pool is not None:
            self.worker_pool.fence_kept()
        logger.error("fenced the database pool")
        if self.http_server is not None:
            self.http_server.close_listener()
            logger.error("closed the HTTP listener")

        async def end_owned() -> None:
            try:
                cleanups = [proc_tree.end_owned(deadline=10)]
                if self.recording_proxy is not None:
                    cleanups.append(self.recording_proxy.close())
                results = await asyncio.gather(*cleanups, return_exceptions=True)
                for result in results:
                    if isinstance(result, BaseException):
                        raise result
            finally:
                self.exit(EXIT_LOCK_LOST)

        self._ending = loop.create_task(end_owned())

    def exit(self, code: int) -> None:
        # Loss wins even when a normal shutdown's deadline or second signal fires first.
        if self._lost:
            logger.error("exiting with 75")
        self.exit_now(EXIT_LOCK_LOST if self._lost else code)


def stop_signal_handler(stop: asyncio.Event, *, exit_now: Callable[[int], Any] = os._exit,
                        prepare_stop: Callable[[], None] | None = None) -> Callable[[], None]:
    """What SIGINT and SIGTERM do: the first starts the shutdown (``stop``), another exits at once."""

    def handler() -> None:
        if stop.is_set():
            logger.warning("another shutdown signal: exiting now, without finishing the shutdown")
            exit_now(1)
            return
        logger.info("shutdown signal received")
        if prepare_stop is not None:
            prepare_stop()
        stop.set()

    return handler


async def shut_down_within(shutdown: Awaitable[None], *, deadline: float = SHUTDOWN_DEADLINE_SEC,
                           exit_now: Callable[[int], Any] = os._exit) -> None:
    """Run ``shutdown``; once it has taken ``deadline`` seconds, exit at once."""
    stopping = asyncio.ensure_future(shutdown)
    if not (await asyncio.wait({stopping}, timeout=deadline))[0]:
        logger.error("the shutdown did not finish within %.0f s; exiting now (the next start ends what is left)",
                     deadline)
        exit_now(1)
        return
    stopping.result()


def _local_url(host: str, port: int) -> str:
    """This server's address for a client on the same machine: loopback when it listens on every address."""
    if host in ("", "0.0.0.0"):
        host = "127.0.0.1"
    elif host == "::":
        host = "::1"
    return f"http://[{host}]:{port}" if ":" in host else f"http://{host}:{port}"


async def _run_agent_system(
    *,
    ops: OpsConfig,
    runtime_root: Path,
    work_dir: Path,
    claude_bin: str = "claude",
    skip_backup: bool = False,
    runner_supervisor: RunnerSupervisor | None = None,
) -> None:
    """Own an opted-in or supplied runner, including when startup only partly completes."""
    kwargs = dict(ops=ops, runtime_root=runtime_root, work_dir=work_dir,
                  claude_bin=claude_bin, skip_backup=skip_backup)
    supervisors = [runner_supervisor] if runner_supervisor is not None else []
    if runner_supervisor is None:
        from .runtimes import pi_runner, dsh_runner

        if ops.pi_runtime == "runner":
            try:
                pi_executable = await pi_runner.configured_executable(ops.database_url)
            except pi_runner.PiRuntimeConflict as exc:
                raise StartupCheckFailed(str(exc)) from exc
            if supervisor := pi_runner.enable(runtime_root, pi_executable):
                supervisors.append(supervisor)
        if ops.dsh_runtime == "runner":
            if supervisor := dsh_runner.enable(runtime_root):
                supervisors.append(supervisor)
    if not supervisors:
        await _run_agent_system_owned(**kwargs)
        return
    from ..db import close_pool
    async with contextlib.AsyncExitStack() as ownership:
        ownership.push_async_callback(close_pool)
        for supervisor in supervisors:
            ownership.push_async_callback(supervisor.stop)
        await _run_agent_system_owned(**kwargs, runner_supervisors=tuple(supervisors), ownership=ownership)
        ownership.pop_all()  # The normal shutdown already stopped these owners and closed the database.


async def _run_agent_system_owned(
    *,
    ops: OpsConfig,
    runtime_root: Path,
    work_dir: Path,
    claude_bin: str = "claude",
    skip_backup: bool = False,
    runner_supervisors: tuple[RunnerSupervisor, ...] = (),
    ownership: contextlib.AsyncExitStack | None = None,
    exit_now: Callable[[int], Any] = os._exit,
) -> None:
    """Start the full agent system with the ops settings ``ops`` and run until interrupted."""

    loop_watch = LoopWatch()
    loop_watch.start()  # Observe synchronous startup work before the first await.
    try:
        await _run_agent_system_watched(ops=ops, runtime_root=runtime_root, work_dir=work_dir,
                                        claude_bin=claude_bin, skip_backup=skip_backup,
                                        runner_supervisors=runner_supervisors, ownership=ownership,
                                        exit_now=exit_now, loop_watch=loop_watch)
    finally:
        await loop_watch.stop()


async def _run_agent_system_watched(
    *, ops: OpsConfig, runtime_root: Path, work_dir: Path, claude_bin: str,
    skip_backup: bool, runner_supervisors: tuple[RunnerSupervisor, ...],
    ownership: contextlib.AsyncExitStack | None, exit_now: Callable[[int], Any],
    loop_watch: LoopWatch,
) -> None:
    # --- Database (required) ---
    from ..db import init_pool, ensure_schema, close_pool

    from . import chat_settings, links, operator_notes, persona, projects, scheduler_settings, waits
    host, port = ops.host, ops.port
    dashboard_url = links.dashboard_url(host=host, port=port, public_url=ops.public_url)
    operator_notes.configure_root(ops.operator_notes_root)
    projects.configure_root(ops.projects_root)
    from .. import _PROCESS_STARTED_AT
    startup = _StartupSteps(_PROCESS_STARTED_AT)
    if ops.projects_root:
        logger.info("projects root=%s", ops.projects_root)
    else:
        logger.info("projects root unset")

    with startup.measure("operator_token"):
        from . import operator_auth
        try:
            operator_token = operator_auth.ensure_token(runtime_root)
        except operator_auth.OperatorTokenError as exc:
            raise StartupCheckFailed(str(exc)) from None

    dsn = ops.database_url
    with startup.measure("database_check"):
        database_check = await self_check.check_database(dsn)
    if database_check.status != self_check.OK:
        raise StartupCheckFailed(self_check.format_checks([database_check]))
    fence = Fence(exit_now=exit_now)
    lease = None
    advisory_checks: asyncio.Task[None] | None = None
    http_server: AgentHTTPServer | None = None
    control = None
    memory_writer: MemoryWriteService | None = None
    memory_drafter: MemoryDraftService | None = None
    memory_push_service: MemoryPushService | None = None
    mode = ShutdownMode.END_RUNS
    if ops.keeper_mode is KeeperMode.ON:
        with startup.measure("lease"):
            try:
                lease = await ControlPlaneLease.acquire(dsn, on_lost=fence.lost, loop_watch=loop_watch)
            except LockHeld as exc:
                raise StartupCheckFailed(
                    f"another looper serve already runs on this database: {exc.holder.describe()}. "
                    "Stop it first: one database runs one control plane.") from None
    try:
        if ops.keeper_mode is KeeperMode.ON:
            n = len(str(runtime_root / keeper.RUNS_DIR / ("reviewer-" + "0" * 12) / keeper.SOCKET).encode())
            if n > keeper.SOCKET_PATH_MAX:
                raise StartupCheckFailed(
                    f"the runtime root is too long for keeper sockets ({n} > 103 bytes): "
                    'use a shorter --runtime-root, or set [agent_host] keeper = "off"')
        with startup.measure("pool"):
            pool = await init_pool(dsn)
        with startup.measure("claude_settings_cleanup"):
            from .claude_cli import clear_private_settings
            removed = await asyncio.to_thread(clear_private_settings, runtime_root)
            logger.info("Removed %d private Claude settings files", removed)
        with startup.measure("schema"):
            await _back_up_before_schema_change(pool, ops, runtime_root, skip_backup=skip_backup)
            await ensure_schema(pool)
        if lease is not None:
            with startup.measure("epoch"):
                epoch = await lease.bump_epoch()
            logger.info("control plane epoch=%d", epoch)
        with startup.measure("settings"):
            try:
                await scheduler_settings.load()
                await waits.load()
                await chat_settings.load()
                from . import lanes_settings
                await lanes_settings.load()
                await persona.load()
                from .channels import down_alert_settings
                await down_alert_settings.load()
            except ValueError as exc:
                raise StartupCheckFailed(str(exc)) from None

        with startup.measure("seed_configs"):
            await seed_role_configs(pool)
            await seed_channel_configs(pool)

        # A built-in runtime for each other runtime type this machine has (Codex or OMP, when on PATH).
        with startup.measure("runtimes"):
            from .profile_registry import register_found_runtimes
            try:
                if found := await register_found_runtimes():
                    logger.info("registered the built-in runtime(s) %s", ", ".join(found))
            except Exception:
                logger.warning("could not register the runtimes found on PATH", exc_info=True)

        # Before any task resumes or agent launches: end the agents a previous server that crashed or was killed
        # left running, then record this server's.
        with startup.measure("process_sweep"):
            from . import proc_tree
            try:
                await proc_tree.sweep(runtime_root)
            except Exception:
                logger.exception("could not sweep the agents a previous server left running")

        with startup.measure("acceptance_recovery"):
            from .acceptance_runner import recover

            await recover(runtime_root)

        # Warm the interpreter's snapshot before kept adoption or HTTP startup can need it.
        with startup.measure("code_manifest"):
            from .. import components
            await components.prepare_loaded_manifest()

        # The tasks the previous server was running: they carry on once the agents are up, or are stopped now.
        with startup.measure("kept_scan"):
            left_running = await tasks_left_running(pool)
            from . import kept_dirs
            from .ops.subagents import kept_explorer_records
            explorer_records = await kept_explorer_records(runtime_root, pool) if lease is not None else {}
            try:
                scan = await kept_dirs.scan_at_start(runtime_root, epoch=epoch if lease is not None else None,
                    resumable=left_running.resumable, pool=pool,
                    **({"reserved_runs": set(explorer_records)} if explorer_records else {}))
            except OSError as exc:
                raise StartupCheckFailed(f"cannot tell whether kept runs are still running: {exc}") from exc
        # The runs the previous server did not see end: nothing runs yet, so every record still running is one.
        with startup.measure("end_unfinished_records"):
            from ..db.llm_records import end_unfinished_records
            if unfinished := await end_unfinished_records(pool=pool, keep=[*scan.record_ids, *explorer_records.values()]):
                logger.info("ended %d run record(s) a previous server left running as interrupted", unfinished)

        with startup.measure("llmrec"):
            from .llmrec.control import ProxyControl, install
            control = None
            try:
                from ..db import system_settings
                values = await system_settings.get_settings(["llmrec.enabled", "llmrec.include_oauth"], pool=pool)
                control = ProxyControl(runtime_root, dsn, enabled=values.get("llmrec.enabled", "true") == "true",
                                       include_oauth=values.get("llmrec.include_oauth") == "true")
                fence.recording_proxy = control
                install(control)
                await control.at_start(scan.record_ids)
            except Exception:
                logger.warning("request recording could not initialize", exc_info=True)

        # The step worktrees no task keeps any more (after the orphaned tasks stopped: those keep their unmerged ones).
        with startup.measure("step_worktrees"):
            from . import step_workspaces
            try:
                if swept := await step_workspaces.sweep(runtime_root):
                    logger.info("removed %d step worktree(s) no task keeps", swept)
            except Exception:
                logger.exception("could not sweep the step worktrees")
        # Then the tasks' own worktrees (the step worktrees are of them): those of completed or deleted tasks go, their
        # branches stay.
        with startup.measure("task_worktrees"):
            from . import task_workspaces
            try:
                if swept := await task_workspaces.sweep(runtime_root):
                    logger.info("removed %d task worktree(s) of tasks that ended", swept)
            except Exception:
                logger.exception("could not sweep the task worktrees")

        # A Codex, Grok or coco login that forked in a run the previous server did not see end (before the session
        # directories it may be in are reclaimed).
        from .runtimes import coco_login, codex_login, grok_login
        for login, label in ((codex_login, "Codex"), (grok_login, "Grok"), (coco_login, "coco")):
            with startup.measure(f"{label.lower()}_login"):
                try:
                    if problem := await login.inspect_at_start(runtime_root):
                        logger.warning("%s", problem)
                except Exception:
                    logger.warning("could not inspect the %s homes for a forked login", label, exc_info=True)

        # Before any agent launches: remove the read-only copies of replaced or
        # deleted content that no running process mounts.
        with startup.measure("reclaim_unused"):
            from .read_only_copies import reclaim_unused
            await reclaim_unused(runtime_root)
        # The sessions the agent room's runs kept (Codex, OMP) of tasks gone or ended over a week ago.
        with startup.measure("run_sessions"):
            from .room import RUN_SESSIONS_KEPT_DAYS, sweep_run_sessions
            try:
                from ..db import room as db_room
                keep_sessions = await db_room.tasks_keeping_sessions(days=RUN_SESSIONS_KEPT_DAYS)
                await asyncio.to_thread(sweep_run_sessions, runtime_root, keep_sessions)
            except Exception:
                logger.warning("could not remove the kept sessions of ended tasks", exc_info=True)

        logger.info("database initialised")

        with startup.measure("agent_setup"):
            broadcaster = Broadcaster()

            bus = MessageBus(runtime_root=runtime_root)
            supervisor = AgentSupervisor(bus=bus, runtime_root=runtime_root)
            if ownership is not None:
                ownership.push_async_callback(supervisor.stop_all)
            registry = ChannelRegistry(bus=bus)

        # --- Channel Adapters ---

        with startup.measure("channel:web"):
            web_adapter = WebChannelAdapter(host=host, port=port)
            await registry.register(web_adapter)

        # Every enabled bot runs as its own adapter (RFC §9.1), read once at startup.
        with startup.measure("channel_configs"):
            bot_rows = await _enabled_bot_rows()
            lark_enabled = any(r["channel_type"] == "lark" for r in bot_rows)

        # --- Agent Tiers ---

        with startup.measure("agent_tiers"):
            manager_config = ManagerConfig(
                claude_bin=claude_bin,
                work_dir=work_dir,
                http_ready=asyncio.Event(),
                dashboard_url=dashboard_url,
            )

            # It fires the schedules once the agents and the HTTP server are up, and hears from the TaskRouter when a
            # task ends, to record a scheduled run's outcome.
            drain = DrainState()  # A new control plane always starts serving; drain is never stored.
            scheduler = Scheduler(bus=bus, sender=registry.notify, dashboard_url=dashboard_url,
                                  runtime_root=runtime_root, drain=drain, poster=registry.post,
                                  claude_bin=claude_bin, own_apps=own_bot_app_ids,
                                  message_backup=db_backup.message_backup_for(ops, runtime_root))
            from . import cost_guard

            await cost_guard.initialize(day_start=scheduler.day_start, pool=pool)
            bus.subscribe_topic(MessageType.USER_MESSAGE, scheduler.observe_user)
            # The advisor's alerts go to the operator's chat through the same channels.
            from . import advisor_alerts, outbound_hook, usage_windows
            advisor_alerts.configure(registry.notify)
            # So does the daily note that skill proposals wait for the operator (names and reasons, never content).
            from . import skill_proposal_notice
            skill_proposal_notice.configure(registry.notify, dashboard_url=dashboard_url)
            usage_windows.configure(registry.notify)
            # So do the outbound review's block notices; its switch is read once here, and kept by its operations.
            outbound_hook.configure(registry.notify)
            await outbound_hook.enabled()
            # It notes the user gates that have waited for the user for their project's reminder interval; the courier
            # posts what the gates' discussions say to the chats their tasks came from.
            gate_reminders = GateReminders()
            gate_courier = GateCourier(registry.post, dashboard_url=dashboard_url, post_card=registry.post_card)
            task_router = TaskRouter(runtime_root=runtime_root, broadcaster=broadcaster, claude_bin=claude_bin,
                                     on_task_end=scheduler.task_ended, dashboard_url=dashboard_url)
            wait_service = WaitService(bus=bus, has_planner=task_router.has_planner, dashboard_url=dashboard_url, drain=drain)
            from . import completion_events
            completion_events.set_scheduler(scheduler)
            source_service = SourceService(drain=drain, own_apps=own_bot_app_ids)
            intent_push_service = IntentPushService(registry, drain=drain)
            memory_writer = MemoryWriteService(runtime_root=runtime_root, drain=drain)
            memory_push_service = MemoryPushService(registry, drain=drain, dashboard_url=dashboard_url)
            memory_drafter = MemoryDraftService(runtime_root, drain=drain, claude_bin=claude_bin, sender=registry.notify)
            drain.add_source(lambda: {"starting": memory_writer.drain_waiting() + memory_drafter.drain_waiting()})
            reminder_service = ReminderService(registry, drain=drain)
            ledger_reconciler = LedgerReconciler()
            supervisor.register(task_router)

            worker_config = WorkerConfig(claude_bin=claude_bin)
            worker_pool = WorkerPool(config=worker_config, runtime_root=runtime_root, drain=drain,
                                     keeper=KeeperSettings(epoch) if lease is not None else None,
                                     on_stale=lease.lose if lease is not None else None)
            worker_pool.set_candidates(scan.candidates)
            worker_pool._ended_at_start.update(scan.ended)
            if lease is not None:
                task_router.adopter = worker_pool.adopt
            fence.worker_pool = worker_pool
            supervisor.register(worker_pool)

            # The Manager's platform tools: its MCP server reaches this server with a token made now. Each task's Planner
            # has its own, with a token for its task made when it starts.
            from .ops.planner import PlannerOps
            from .ops.service import Platform
            from . import managers_api
            from .managers import ManagerFleet
            platform = Platform(bus=bus, task_router=task_router, worker_pool=worker_pool, manager=None,
                                supervisor=supervisor, scheduler=scheduler, runtime_root=runtime_root)
            from . import cost_holds
            cost_holds.configure(platform, sender=registry.notify, dashboard_url=dashboard_url)
            tools_ready = asyncio.Event()
            fleet = ManagerFleet(platform=platform, supervisor=supervisor, runtime_root=runtime_root,
                                 url=_local_url(host, port), config=manager_config, broadcaster=broadcaster, drain=drain,
                                 tools_ready=tools_ready, read_quote=registry.read_quote)
            if ownership is not None:
                ownership.push_async_callback(fleet.stop)
            task_router.manager_running = fleet.is_running
            # An idle_only schedule skips a firing while its Manager answers a message.
            scheduler.manager_busy = lambda manager_id: bool((agent := fleet.get(manager_id)) is not None
                                                             and agent.busy)
            await fleet.load()
            morning_brief_service = MorningBriefService(registry, drain=drain, managers=fleet)
            metrics_alert_service = MetricsAlertService(registry)
            from .completion_service import CompletionService
            completion_service = CompletionService(
                bus=bus, manager_running=fleet.is_running, drain=drain,
                running_managers=lambda: [agent.agent_id for agent in fleet.agents()])
        # Start receiving only once every enabled Manager is known, so backlog is not lost or sent to a disabled one.
        await _register_bots(registry, bot_rows, runtime_root, fleet.is_running, ("telegram",),
                             stop_chat=fleet.stop_chat, startup=startup)
        await _register_bots(registry, bot_rows, runtime_root, fleet.is_running, ("lark", "lark_bot"),
                             stop_chat=fleet.stop_chat, startup=startup)

        # Receive and persist backlog before listening. Managers wait for HTTP readiness;
        # advisory checks run afterwards and only warn here and on system/status.
        with startup.measure("http_setup"):
            checks = [database_check.to_dict(), *(check.to_dict() for group in _pending_advisory_checks() for check in group)]

            manager = fleet.default
            manager_ops = fleet.ops("manager")
            managers_api.set_change_listener(fleet.apply)
            platform = dataclasses.replace(platform, manager=manager, managers=fleet)
            from .ops.clients import ClientOps
            client_ops = ClientOps(platform)
            planner_ops = PlannerOps(platform, url=_local_url(host, port), dashboard_url=dashboard_url)
            task_router.planner_ops = planner_ops
            # Each task's agent room: its Planner's tools (with its token) and its runs' (each with a token of its own).
            from .ops.room import RoomOps
            from .room import RoomService
            room = RoomService(bus=bus, worker_pool=worker_pool)
            from .consult import ConsultService
            consults = ConsultService(room=room, worker_pool=worker_pool, runtime_root=runtime_root, claude_bin=claude_bin,
                                      drain=drain)
            drain.add_source(lambda: task_router.drain_waiting())
            drain.add_source(lambda: scheduler.drain_waiting())
            drain.add_source(lambda: {"waits": wait_service.drain_waiting(), "consults": consults.drain_waiting()})
            room.consults = consults
            planner_ops.room = room
            room_ops = RoomOps(room, url=_local_url(host, port))
            from .subagents import SpawnService
            from .ops.subagents import PlannerSubagents, WorkerSubagents
            explorers = WorkerSubagents(SpawnService(runtime_root, claude_bin=claude_bin,
                keeper=KeeperSettings(epoch) if lease is not None else None,
                on_stale=lease.lose if lease is not None else None), room, worker_pool=worker_pool)
            room_ops.subagents = explorers
            planner_ops.subagents = PlannerSubagents(explorers)
            from ..db import tasks as task_rows
            from ..db.agent_spawns import CancelReason

            async def end_explorers(task_id: str) -> None:
                # A stopped, cancelled, finished, failed or restarted task ends all its explorers at once.
                await explorers.service.cascade(task_id=task_id, reason=CancelReason.TASK_ENDED)

            task_rows.task_ended_listeners.append(end_explorers)
            worker_pool.room_ops = room_ops
            for candidate in scan.candidates:
                if candidate.grant:
                    room_ops.import_pending(candidate.grant)

            # --- HTTP Server ---

            http_server = AgentHTTPServer(
                drain=drain,
                host=host,
                port=port,
                web_adapter=web_adapter,
                task_router=task_router,
                supervisor=supervisor,
                worker_pool=worker_pool,
                manager=manager,
                broadcaster=broadcaster,
                runtime_root=runtime_root,
                operator_token=operator_token,
                dashboard_url=dashboard_url,
                self_checks=checks,
                claude_bin=claude_bin,
                scheduler=scheduler,
                manager_ops=manager_ops,
                managers=fleet,
                planner_ops=planner_ops,
                room_ops=room_ops,
                client_ops=client_ops,
                ops_config=ops,
                channels=registry,
                startup_steps=startup.steps,
                loop_watch=loop_watch,
            )
            http_server._memory_writer = memory_writer
            http_server._memory_drafter = memory_drafter
        fence.http_server = http_server
        with startup.measure("http"):
            await http_server.start()
        logger.info("startup steps: %s", startup.finish())
        advisory_checks = asyncio.create_task(_advisory_startup_checks(
            checks, database_check, claude_bin=claude_bin, lark_enabled=lark_enabled,
            projects_root=ops.projects_root), name="advisory-startup-checks")
        manager_config.http_ready.set()
        tools_ready.set()

        # Replayed messages and resumed runs can probe or call platform tools immediately.
        # Listen before starting any of their consumers.
        try:
            await explorers.start()
            await consults.resume_all()
            # Before any Planner resumes: a consult of the advisor a stopped server left running cannot finish.
            from .advisor import fail_interrupted

            if interrupted := await fail_interrupted():
                logger.warning("%d consult(s) of the advisor were interrupted by the restart and failed", interrupted)
            idle_check_service = await _prepare_idle_checks(bus, fleet, drain, post=registry.post)
            await supervisor.start_all()
            await fleet.sweep()
            await carry_on_tasks(task_router, left_running)
            if lease is not None:
                worker_pool.schedule_candidate_expiry()
        except BaseException:
            await http_server.stop()
            raise

        # Its first pass handles the fires missed while the server was down.
        await scheduler.start()
        await gate_reminders.start()
        # Check due waits only after the task router and HTTP API are ready.
        await wait_service.start()
        # Once every Manager started (and recovered its own turns): the chats' completion events, as they come due.
        await completion_service.start()
        # Sources that are off (the default) cost one query per tick.
        await source_service.start()
        await intent_push_service.start()
        await memory_writer.start()
        await memory_drafter.start(dsn)
        await memory_push_service.start()
        await reminder_service.start()  # idle while reminders are off
        await idle_check_service.start()
        await morning_brief_service.start()  # idle while the morning brief is off
        await metrics_alert_service.start()
        await ledger_reconciler.start()  # idle while the ledger is off
        await gate_courier.start()

        logger.info(
            "agent system running on http://%s:%d (channels=%s)",
            host, port,
            ",".join(c["channel_id"] for c in registry.list_channels() if c.get("healthy")),
        )

        # Wait for shutdown signal
        stop_event = asyncio.Event()
        loop = asyncio.get_running_loop()

        def prepare_stop(sig: int) -> None:
            nonlocal mode
            mode = shutdown_mode(keeper_on=lease is not None, drain=drain, sig=sig)
            logger.info("shutdown mode=%s", mode)
            registry.prepare_stop_all()

        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop_signal_handler(
                stop_event, exit_now=fence.exit, prepare_stop=lambda sig=sig: prepare_stop(sig)))

        async def check_recording_proxy() -> None:
            suspend_detector = _SuspendDetector()
            next_suspend_check = time.monotonic() + 30
            while True:
                await asyncio.sleep(5)
                if time.monotonic() >= next_suspend_check:
                    suspend_detector.check()
                    next_suspend_check = time.monotonic() + 30
                if control is not None:
                    try:
                        await control.tick()
                    except Exception:
                        logger.warning("request recording health check failed", exc_info=True)

        proxy_checks = asyncio.create_task(check_recording_proxy(), name="llmrec-health")
        from .channels.down_alerts import Monitor
        channel_checks = asyncio.create_task(Monitor(registry).run(), name="channel-health")
        try:
            await stop_event.wait()
        finally:
            proxy_checks.cancel()
            channel_checks.cancel()
            await asyncio.gather(proxy_checks, channel_checks, return_exceptions=True)

        # --- Shutdown ---

        async def shut_down() -> None:
            advisory_checks.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await advisory_checks
            if mode is ShutdownMode.DETACH_KEPT:
                await worker_pool.detach_kept(epoch=lease.epoch)
                await explorers.stop(epoch=lease.epoch)
                if control is not None:
                    await control.close(handover=True)
            if end_explorers in task_rows.task_ended_listeners:
                task_rows.task_ended_listeners.remove(end_explorers)
            logger.info("shutting down agent system...")
            if mode is not ShutdownMode.DETACH_KEPT:
                await explorers.stop()
            await consults.stop()
            await gate_reminders.stop()
            # Cancel and reap in-flight wait checks before stopping agents and their bus.
            await wait_service.stop()
            await completion_service.stop()
            await source_service.stop()
            await intent_push_service.stop()
            await memory_drafter.stop()
            await memory_push_service.stop()
            await memory_writer.stop()
            await reminder_service.stop()
            await idle_check_service.stop()
            await metrics_alert_service.stop()
            await morning_brief_service.stop()
            await ledger_reconciler.stop()
            await gate_courier.stop()
            await scheduler.stop()
            await http_server.stop()
            await fleet.stop()
            await supervisor.stop_all()
            for runner_supervisor in runner_supervisors:
                await runner_supervisor.stop()
            await registry.stop_all()
            await bus.shutdown()
            if control is not None:
                await control.close(handover=mode is ShutdownMode.DETACH_KEPT)
            await close_pool()
            if lease is not None:
                await lease.release()
            logger.info("agent system stopped")

        await shut_down_within(shut_down(), exit_now=fence.exit)

    finally:
        from . import managers_api
        from .llmrec.control import install

        managers_api.set_change_listener(None)
        install(None)
        if control is not None:
            try:
                await control.close(handover=mode is ShutdownMode.DETACH_KEPT)
            except Exception:
                logger.warning("request recording proxy shutdown failed", exc_info=True)
        if advisory_checks is not None:
            advisory_checks.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await advisory_checks
        try:
            if memory_drafter is not None:
                await memory_drafter.stop()
            if memory_push_service is not None:
                await memory_push_service.stop()
            if memory_writer is not None:
                await memory_writer.stop()
            if http_server is not None:
                await http_server.stop()
        finally:
            try:
                await close_pool()
            finally:
                if lease is not None:
                    await lease.release()


def _pending_advisory_checks() -> list[list[self_check.Check]]:
    # Each probe owns its slots; Claude's CLI probe also checks its login.
    groups = (
        (("claude_cli", "Claude Code CLI"), ("claude_login", "Claude login")),
        (("codex_login", "Codex login"),), (("grok_login", "Grok login"),),
        (("cursor_login", "Cursor login"),), (("ttadk", "TTADK"),),
        (("llmrec", "Model request recording"),), (("gateway", "Local gateway"),),
        (("lark_cli", "Lark channel"),), (("git", "git"),), (("projects_root", "Projects root"),),
        (("provider_limits", "Provider quotas"),),
    )
    return [[self_check.Check(id, label, self_check.PENDING, "Startup check in progress") for id, label in group]
            for group in groups]


async def _advisory_startup_checks(snapshot: list[dict[str, str]], database: self_check.Check, *,
                                   claude_bin: str, lark_enabled: bool, projects_root: str) -> None:
    """Publish each probe as it finishes; keep display and final log order stable. Never stop serving."""
    groups = _pending_advisory_checks()

    async def record(index: int, probe: Awaitable[self_check.Check | list[self_check.Check]]) -> None:
        try:
            result = await probe
            groups[index] = result if isinstance(result, list) else [result]
        except Exception:
            logger.exception("advisory startup check failed")
            groups[index] = [self_check.Check(check.id, check.label, self_check.WARN,
                                             "Startup check failed", "Check the service log and restart looper serve")
                             for check in groups[index]]
        snapshot[:] = [database.to_dict(), *(check.to_dict() for group in groups for check in group)]

    try:
        probes = (
            self_check.claude_check(claude_bin),
            self_check.codex_login_check(),
            self_check.grok_login_check(),
            self_check.cursor_login_check(),
            self_check.ttadk_check(),
            self_check.llmrec_check(),
            self_check.gateway_check(),
            asyncio.to_thread(self_check.check_lark_cli, lark_enabled),
            asyncio.to_thread(self_check.check_git),
            asyncio.to_thread(self_check.check_projects_root, projects_root),
            self_check.provider_limit_checks(),
        )
        await asyncio.gather(*(record(index, probe) for index, probe in enumerate(probes)))
        self_check.log_problems([database, *(check for group in groups for check in group)])
    except Exception:
        logger.exception("advisory startup checks failed")


def _configure_logging(level: str) -> None:
    """Log to stderr from ``level`` (an ops ``log_level``) with secrets masked (``log_redaction``). httpx and httpcore
    log every request URL, and a Telegram one holds the bot token: they log only from WARNING below debug. Set in full
    each time: a second call (after a refused start, or in a test) leaves nothing of the first."""
    log_to_stderr(getattr(logging, level.upper()), "%(asctime)s %(name)s %(levelname)s %(message)s")
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.NOTSET if level == "debug" else logging.WARNING)


def serve_agent(
    *,
    host: str | None = None,
    port: int | None = None,
    runtime_root: Path | None = None,
    work_dir: Path | None = None,
    claude_bin: str = "claude",
    verbose: bool = False,
    skip_backup: bool = False,
    config: Path | None = None,
) -> None:
    """Synchronous entry point for the agent system: ``host``, ``port``, ``runtime_root``, ``config`` and ``verbose``
    are the command line's flags (None and False: not given), read with the ops file (``ops_config.load``) before
    anything else; settings that cannot be used stop it here."""
    try:
        ops = ops_config.load(ops_config.Flags(runtime_root=runtime_root, config=config, host=host, port=port,
                                               log_level="debug" if verbose else None))
    except ops_config.OpsConfigError as exc:
        _configure_logging("info")
        logger.error("looper serve cannot start: %s", exc)
        raise SystemExit(1) from None
    _configure_logging(ops.log_level)
    for warning in ops.warnings:
        logger.warning("%s", warning)

    resolved_work_dir = work_dir or Path.cwd()

    try:
        asyncio.run(
            _run_agent_system(
                ops=ops,
                runtime_root=ops.runtime_root,
                work_dir=resolved_work_dir,
                claude_bin=claude_bin,
                skip_backup=skip_backup,
            )
        )
    except StartupCheckFailed as exc:
        logger.error("looper serve cannot start:\n%s", exc)
        raise SystemExit(1) from None
