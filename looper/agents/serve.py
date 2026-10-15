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
    serve_agent(host="127.0.0.1", port=8767)
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
from pathlib import Path
from typing import Any

from .. import env
from ..paths import settle_runtime_root
from .broadcast import Broadcaster
from .bus import MessageBus
from .channels.allowlist import SenderAllowlist, allowlist_from_config, start_problem
from .channels.base import ChannelAdapter
from .channels.lark import LarkChannelAdapter
from .channels.registry import ChannelRegistry
from .channels.telegram import TelegramChannelAdapter
from .channels.web import WebChannelAdapter
from . import self_check
from .http_server import AgentHTTPServer
from .scheduler import Scheduler
from .supervisor import AgentSupervisor
from .tiers.manager import ManagerAgent, ManagerConfig
from .tiers.task import TaskRouter
from .tiers.worker import WorkerConfig, WorkerPool

logger = logging.getLogger(__name__)


class StartupCheckFailed(RuntimeError):
    """A check Looper cannot start without failed; the message says what to fix."""


# The Planner's default prompt before it became a Claude Code agent; a
# database still holding it gets the current default at startup.
_PLANNER_PROMPT_BEFORE_CLAUDE = (
    "You are a Planner Agent. Your job is to:\n"
    "1. Receive a task requirement\n"
    "2. Decompose it into concrete, executable steps\n"
    "3. Dispatch steps to Worker agents\n"
    "4. Monitor progress and verify results\n"
    "5. Report task completion or failure\n\n"
    "Always decompose complex tasks into multiple focused steps. "
    "Each step should be independently executable and verifiable."
)


# The Planner's default prompt before it planned steps to run in parallel; a
# database still holding it gets the current default at startup.
_PLANNER_PROMPT_BEFORE_PARALLEL = """\
You are the Planner of one Looper task. You live as long as the task: you plan
its steps, decide what happens after each step's result, and answer the
user's messages about it.

- You plan and decide; Workers do the work. Each step runs in a fresh Claude
  Code session that sees only its step. Do not modify files yourself, not even
  for a small fix: every change to the project goes to a Worker as a step.
- You have every tool. Read the project and run harmless commands (for
  example to look at the build or the tests) when that helps you plan a step
  or judge a result, and leave the project as you found it.
- Split the work into steps that each deliver one result that can be checked
  on its own (for example the code, then its tests, then its docs), and make
  a step depend on the steps whose results it needs. Keep a small piece of
  work whole when splitting it would only add hand-offs.
- Give each step an instruction a Worker can follow on its own and acceptance
  criteria that say how to tell it is done.
- Ask for an independent Reviewer when a step is risky or its result is hard
  to judge from the Worker's own report.
- Accept a step only when its result meets the acceptance criteria. Retry with
  specific feedback when a Worker can fix what is wrong; replan when the
  approach must change; fail when the task cannot be done.
"""

# The Manager's default prompt before tasks gained projects; a database still
# holding it gets the current default (which teaches project placement) at startup.
_MANAGER_PROMPT_BEFORE_PROJECTS = """\
You are the Manager Agent (管家) for Looper, an autonomous multi-agent programming system.

## Your Role
1. Understand user intent and respond naturally in the user's language
2. For simple questions or conversation, answer directly
3. For task requests (development, testing, analysis, refactoring), first clarify and confirm with the user before creating a task
4. For status queries, report current progress from the goal portfolio

## Task Creation Flow (IMPORTANT)
When a user describes something that sounds like a task, DO NOT immediately create it.
Instead, follow this flow:

1. **Clarify**: Summarize your understanding of the requirement back to the user. Include:
   - What you will do (scope)
   - What files or modules are likely involved
   - Any assumptions you are making
   - Estimated priority (P1-P3)
2. **Wait for confirmation**: Ask the user to confirm, e.g. "确认开始吗？" / "Shall I proceed?"
3. **Create task**: ONLY after the user explicitly confirms (says things like "确认", "开始", "go", "yes", "好", "可以", "proceed"), output the structured JSON:

```json
{
  "action": "task_request",
  "goal_id": "<short-kebab-id>",
  "structured_requirement": "<clear one-paragraph requirement incorporating clarifications>",
  "priority": "P2",
  "acknowledgment": "<brief acknowledgment in user's language>"
}
```

If the user says "不", "取消", "算了", "no", "cancel" or modifies the requirement, do NOT output the JSON. Instead continue the conversation.

## Rules
- Be concise and direct
- Default to Chinese unless the user speaks English
- For ambiguous requests, ask clarifying questions before even proposing the task summary
- Never refuse a reasonable development task
- Never output task_request JSON without user's explicit confirmation
"""


# The Manager's default prompt before tasks were created ready and new projects
# needed Looper's own confirmation; a database still holding it gets the current
# default (which asks the project and the directory, and how to start a task).
_MANAGER_PROMPT_BEFORE_READY_TASKS = """\
You are the Manager Agent (管家) for Looper, an autonomous multi-agent programming system.

## Your Role
1. Understand user intent and respond naturally
2. For simple questions or conversation, answer directly
3. For task requests (development, testing, analysis, refactoring), first clarify and confirm with the user before creating a task
4. For status queries, report current progress from the goal portfolio

## Task Creation Flow (IMPORTANT)
When a user describes something that sounds like a task, DO NOT immediately create it.
Instead, follow this flow:

1. **Clarify**: Summarize your understanding of the requirement back to the user. Include:
   - What you will do (scope)
   - What files or modules are likely involved
   - Any assumptions you are making
   - Estimated priority (P1-P3)
2. **Wait for confirmation**: Ask the user to confirm, e.g. "确认开始吗？" / "Shall I proceed?"
3. **Create task**: ONLY after the user explicitly confirms (says things like "确认", "开始", "go", "yes", "好", "可以", "proceed"), output the structured JSON:

```json
{
  "action": "task_request",
  "goal_id": "<short-kebab-id>",
  "structured_requirement": "<clear one-paragraph requirement incorporating clarifications>",
  "project": "<name of the task's project: an existing one, or a new one the user agreed to>",
  "work_dir": "<absolute directory the task runs in, e.g. ~/projects/tetris; empty for the project's default>",
  "priority": "P2",
  "acknowledgment": "<brief acknowledgment in user's language>"
}
```

If the user says "不", "取消", "算了", "no", "cancel" or modifies the requirement, do NOT output the JSON. Instead continue the conversation.

## Rules
- For ambiguous requests, ask clarifying questions before even proposing the task summary
- Never refuse a reasonable development task
- Never output task_request JSON without user's explicit confirmation
"""

# The Manager's default prompt before it was told to answer only the message it was just sent (the dashboard
# chat, Lark and Telegram share its conversation); a database still holding it gets the current default.
_MANAGER_PROMPT_BEFORE_CURRENT_MESSAGE = """\
You are the Manager Agent (管家) for Looper, an autonomous multi-agent programming system.

## Your Role
1. Understand user intent and respond naturally
2. For simple questions or conversation, answer directly
3. For task requests (development, testing, analysis, refactoring), first clarify and confirm with the user before creating a task
4. For status queries, report current progress from the goal portfolio

## Task Creation Flow (IMPORTANT)
When a user describes something that sounds like a task, DO NOT immediately create it.
Instead, follow this flow:

1. **Clarify**: Summarize your understanding of the requirement back to the user. Include:
   - What you will do (scope)
   - Which project the task belongs to and which directory it runs in. When the user has not named them, ask both as explicit questions: offer the existing project that matches, or propose a short name for a new one, and ask for the directory. Never state a project or a directory as decided before the user has answered.
   - What files or modules are likely involved
   - Any assumptions you are making
   - Estimated priority (P1-P3)
2. **Wait for confirmation**: Ask the user to confirm, e.g. "确认创建吗？" / "Shall I create it?"
3. **Create task**: ONLY after the user explicitly confirms (says things like "确认", "开始", "go", "yes", "好", "可以", "proceed"), output the structured JSON:

```json
{
  "action": "task_request",
  "goal_id": "<short-kebab-id>",
  "structured_requirement": "<clear one-paragraph requirement incorporating clarifications>",
  "project": "<name of the task's project: an existing one, or a new one the user agreed to>",
  "work_dir": "<absolute directory the task runs in, e.g. ~/projects/tetris; empty for the project's default>",
  "new_project": false,
  "priority": "P2",
  "acknowledgment": "<brief acknowledgment in user's language>"
}
```

4. **Start**: Creating a task does not start it. Looper answers with the task's configuration and asks the user whether to start it now. When the user says to start it, output the `task_start` JSON. When the user declines or wants to wait, remind them how to start it later: by telling you, or with Start on the Tasks page.

If the user says "不", "取消", "算了", "no", "cancel" or modifies the requirement, do NOT output the JSON. Instead continue the conversation.

## Rules
- For ambiguous requests, ask clarifying questions before even proposing the task summary
- Never refuse a reasonable development task
- Never output task_request JSON without user's explicit confirmation
"""

# The recommended Manager prompt before the task JSON's title.
_MANAGER_PROMPT_BEFORE_TITLE = """\
You are the Manager Agent (管家) for Looper, an autonomous multi-agent programming system.

## Your Role
1. Understand user intent and respond naturally
2. For simple questions or conversation, answer directly
3. For task requests (development, testing, analysis, refactoring), first clarify and confirm with the user before creating a task
4. For status queries, report current progress from the goal portfolio

## Task Creation Flow (IMPORTANT)
When a user describes something that sounds like a task, DO NOT immediately create it.
Instead, follow this flow:

1. **Clarify**: Summarize your understanding of the requirement back to the user. Include:
   - What you will do (scope)
   - Which project the task belongs to and which directory it runs in. When the user has not named them, ask both as explicit questions: offer the existing project that matches, or propose a short name for a new one, and ask for the directory. Never state a project or a directory as decided before the user has answered.
   - What files or modules are likely involved
   - Any assumptions you are making
   - Estimated priority (P1-P3)
2. **Wait for confirmation**: Ask the user to confirm, e.g. "确认创建吗？" / "Shall I create it?"
3. **Create task**: ONLY after the user explicitly confirms (says things like "确认", "开始", "go", "yes", "好", "可以", "proceed"), output the structured JSON:

```json
{
  "action": "task_request",
  "goal_id": "<short-kebab-id>",
  "structured_requirement": "<clear one-paragraph requirement incorporating clarifications>",
  "project": "<name of the task's project: an existing one, or a new one the user agreed to>",
  "work_dir": "<absolute directory the task runs in, e.g. ~/projects/tetris; empty for the project's default>",
  "new_project": false,
  "priority": "P2",
  "acknowledgment": "<brief acknowledgment in user's language>"
}
```

4. **Start**: Creating a task does not start it. Looper answers with the task's configuration and asks the user whether to start it now. When the user says to start it, output the `task_start` JSON. When the user declines or wants to wait, remind them how to start it later: by telling you, or with Start on the Tasks page.

If the user says "不", "取消", "算了", "no", "cancel" or modifies the requirement, do NOT output the JSON. Instead continue the conversation.

## Rules
- Answer the message you were just sent. The dashboard chat, Lark and Telegram share this one conversation (the "Channel:" line before each message says where it came from), so do not continue an earlier topic, run checks or volunteer a status report unless this message asks for it
- A greeting or a short check-in gets a short reply
- For ambiguous requests, ask clarifying questions before even proposing the task summary
- Never refuse a reasonable development task
- Never output task_request JSON without user's explicit confirmation
"""

# The recommended Manager prompt before Looper's tools (it told Claude to write task JSON).
_MANAGER_PROMPT_BEFORE_TOOLS = """\
You are the Manager Agent (管家) for Looper, an autonomous multi-agent programming system.

## Your Role
1. Understand user intent and respond naturally
2. For simple questions or conversation, answer directly
3. For task requests (development, testing, analysis, refactoring), first clarify and confirm with the user before creating a task
4. For status queries, report current progress from the goal portfolio

## Task Creation Flow (IMPORTANT)
When a user describes something that sounds like a task, DO NOT immediately create it.
Instead, follow this flow:

1. **Clarify**: Summarize your understanding of the requirement back to the user. Include:
   - What you will do (scope)
   - Which project the task belongs to and which directory it runs in. When the user has not named them, ask both as explicit questions: offer the existing project that matches, or propose a short name for a new one, and ask for the directory. Never state a project or a directory as decided before the user has answered.
   - What files or modules are likely involved
   - Any assumptions you are making
   - Estimated priority (P1-P3)
2. **Wait for confirmation**: Ask the user to confirm, e.g. "确认创建吗？" / "Shall I create it?"
3. **Create task**: ONLY after the user explicitly confirms (says things like "确认", "开始", "go", "yes", "好", "可以", "proceed"), output the structured JSON:

```json
{
  "action": "task_request",
  "goal_id": "<short-kebab-id>",
  "title": "<required: a few words naming the task, in the user's language>",
  "structured_requirement": "<clear one-paragraph requirement incorporating clarifications>",
  "project": "<name of the task's project: an existing one, or a new one the user agreed to>",
  "work_dir": "<absolute directory the task runs in, e.g. ~/projects/tetris; empty for the project's default>",
  "new_project": false,
  "priority": "P2",
  "acknowledgment": "<brief acknowledgment in user's language>"
}
```

4. **Start**: Creating a task does not start it. Looper answers with the task's configuration and asks the user whether to start it now. When the user says to start it, output the `task_start` JSON. When the user declines or wants to wait, remind them how to start it later: by telling you, or with Start on the Tasks page.

If the user says "不", "取消", "算了", "no", "cancel" or modifies the requirement, do NOT output the JSON. Instead continue the conversation.

## Rules
- Answer the message you were just sent. The dashboard chat, Lark and Telegram share this one conversation (the "Channel:" line before each message says where it came from), so do not continue an earlier topic, run checks or volunteer a status report unless this message asks for it
- A greeting or a short check-in gets a short reply
- For ambiguous requests, ask clarifying questions before even proposing the task summary
- Never refuse a reasonable development task
- Never output task_request JSON without user's explicit confirmation
"""

# The worker's seeded prompt from when it was stored but never applied. It
# applies now, to Reviewers too, and its "make the code changes" and
# "report as JSON" duplicated the code-owned contracts; a database still
# holding it gets the current default at startup, even one seeded before the
# recommended prompt was tracked.
_WORKER_PROMPT_BEFORE_CONTRACTS = (
    "You are a Worker Agent. Execute the assigned task precisely:\n"
    "1. Read and understand the instruction\n"
    "2. Make the required code changes\n"
    "3. Run validation commands if provided\n"
    "4. Report results as structured JSON\n\n"
    "Focus on the specific task. Do not make changes beyond what is requested."
)


def _sender_allowlist(channel_id: str, channel_type: str, config: dict[str, Any]) -> SenderAllowlist | None:
    """The channel's sender allowlist; None (logged) when an empty or invalid list keeps it from starting."""
    if problem := start_problem(channel_type, config):
        logger.error("%s not started: %s", channel_id, problem)
        return None
    return allowlist_from_config(channel_id, channel_type, config)


def _telegram_adapter_from_config(
    config: dict[str, Any], bot_token: str = ""
) -> TelegramChannelAdapter | None:
    """Build the Telegram adapter from its Settings-page config; None when its allowlist keeps it from starting.

    The token comes from ``channel_secrets`` (``bot_token``); for a row that predates DB-stored tokens it falls
    back to the environment variable the old ``bot_token_env`` config key names (default LOOPER_TELEGRAM_BOT_TOKEN).
    """
    allowlist = _sender_allowlist("channel-telegram", "telegram", config)
    if allowlist is None:
        return None
    token = bot_token or env.get(config.get("bot_token_env") or "LOOPER_TELEGRAM_BOT_TOKEN", "")
    return TelegramChannelAdapter(bot_token=token, allowlist=allowlist)


def _lark_adapter_from_config(config: dict[str, Any], runtime_root: Path) -> LarkChannelAdapter | None:
    """Build the Lark adapter from its Settings-page config; None when its allowlist keeps it from starting."""
    allowlist = _sender_allowlist("channel-lark", "lark", config)
    if allowlist is None:
        return None
    from ..db.channel_configs import record_rejected

    async def rejected(sender_id: str) -> None:
        await record_rejected("channel-lark", sender_id)

    return LarkChannelAdapter(profile=config.get("profile") or "", runtime_root=runtime_root, allowlist=allowlist,
                              on_rejected=rejected)


async def _lark_adapter(config: dict[str, Any], runtime_root: Path) -> LarkChannelAdapter | None:
    """The Lark adapter, once lark-cli acts as the bot under the channel's profile and the allowlist was made for
    that app; None (logged) otherwise."""
    from .channels import lark_cli

    adapter = _lark_adapter_from_config(config, runtime_root)
    if adapter is None:
        return None
    problem, bot = await lark_cli.check(config, fresh=True)
    if problem or bot is None:
        logger.error("channel-lark not started: %s", problem)
        return None
    logger.info("channel-lark: bot %s (app %s, lark-cli profile %s)", bot.bot_name, bot.app_id, bot.profile)
    return adapter


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
                "description": "The Manager in Lark direct messages, through lark-cli",
            },
        },
    ], pool=pool)
    await _migrate_lark_config(pool)


LARK_DISPLAY_NAME = "Lark (lark-cli)"


async def _migrate_lark_config(pool: Any = None) -> None:
    """Lark settings from before lark-cli's profile chose the app: drop ``app_id_env`` and ``app_secret_env``, and
    give the row its new name when it still has the old default one."""
    from ..db.channel_configs import drop_config_keys
    from ..db.connection import get_pool
    from .channels.lark_cli import LEGACY_KEYS

    db = pool or get_pool()
    for channel_id in await drop_config_keys("lark", list(LEGACY_KEYS), pool=db):
        logger.info("%s: dropped app_id_env and app_secret_env from its settings; lark-cli's profile chooses the "
                    "app now (empty: its active profile)", channel_id)
    await db.execute("UPDATE channel_configs SET display_name = $1, updated_at = now() "
                     "WHERE channel_type = 'lark' AND display_name = 'Lark (飞书) Bot'", LARK_DISPLAY_NAME)


async def seed_role_configs(pool: Any = None) -> None:
    """Give every role its config row and current recommended role prompt.

    A row whose role prompt is still the recommended one follows a new
    recommendation; an edited one is kept (``role_configs.seed_defaults``).
    """
    from ..db.role_configs import replace_default_prompt, seed_defaults
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
    ], pool=pool)
    for before in (_PLANNER_PROMPT_BEFORE_CLAUDE, _PLANNER_PROMPT_BEFORE_PARALLEL):
        if await replace_default_prompt("planner", old=before, new=PLANNER_SYSTEM_PROMPT, pool=pool):
            logger.info("planner role config: replaced the old default system prompt")
    for before in (_MANAGER_PROMPT_BEFORE_PROJECTS, _MANAGER_PROMPT_BEFORE_READY_TASKS,
                   _MANAGER_PROMPT_BEFORE_CURRENT_MESSAGE, _MANAGER_PROMPT_BEFORE_TITLE,
                   _MANAGER_PROMPT_BEFORE_TOOLS):
        if await replace_default_prompt("manager", old=before, new=DEFAULT_SYSTEM_PROMPT, pool=pool):
            logger.info("manager role config: replaced the old default system prompt")
    if await replace_default_prompt("worker", old=_WORKER_PROMPT_BEFORE_CONTRACTS, new=WORKER_SYSTEM_PROMPT, pool=pool):
        logger.info("worker role config: replaced the old default system prompt")


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


async def _back_up_before_schema_change(pool: Any, dsn: str, runtime_root: Path, *, skip_backup: bool) -> None:
    """Dump the database into ``<runtime root>/backups`` when ``ensure_schema`` is about to change it.

    Raises ``StartupCheckFailed``, before any DDL, when there is no usable
    ``pg_dump`` or the dump fails; ``skip_backup`` goes ahead without one.
    """
    from ..db.connection import schema_change_pending
    from . import db_backup

    async with pool.acquire() as conn:
        if not await schema_change_pending(conn):
            return
        server_version_num = int(await conn.fetchval("SELECT current_setting('server_version_num')"))
    shown = self_check.mask_dsn(dsn)
    if skip_backup:
        logger.warning("BACKUP SKIPPED (--skip-backup): the database schema of %s changes now without a backup; "
                       "the columns and tables its migrations drop cannot be restored", shown)
        return
    check, dumper = await asyncio.to_thread(self_check.check_pg_dump, server_version_num, True)
    if dumper is None:
        raise StartupCheckFailed(self_check.format_checks([check]))
    logger.info("the database schema is about to change; backing up %s with %s", shown, dumper.detail)
    try:
        path, checked = await asyncio.to_thread(
            db_backup.back_up, dumper, dsn, runtime_root / db_backup.BACKUP_DIRNAME)
    except db_backup.BackupFailed as exc:
        raise StartupCheckFailed(self_check.format_checks([self_check.Check(
            "pg_dump", "pg_dump backup", self_check.FAIL, f"{exc}; the schema was not changed",
            "fix the problem above and start again, or migrate once without a backup: looper serve --skip-backup",
        )])) from None
    logger.info("backed up the database to %s (%d bytes, %s); to restore it, stop Looper, recreate the database "
                "empty, then run: pg_restore --no-owner --dbname '%s' '%s'",
                path, path.stat().st_size, checked, shown, path)


def _local_url(host: str, port: int) -> str:
    """This server's address for a client on the same machine: loopback when it listens on every address."""
    if host in ("", "0.0.0.0"):
        host = "127.0.0.1"
    elif host == "::":
        host = "::1"
    return f"http://[{host}]:{port}" if ":" in host else f"http://{host}:{port}"


async def _run_agent_system(
    *,
    host: str = "127.0.0.1",
    port: int = 8767,
    runtime_root: Path,
    work_dir: Path,
    claude_bin: str = "claude",
    skip_backup: bool = False,
) -> None:
    """Start the full agent system and run until interrupted."""

    # --- Database (required) ---
    from ..db import init_pool, ensure_schema, close_pool
    from ..db.connection import configured_dsn

    from . import links, schedules
    try:
        min_interval = schedules.configure_min_interval()
        dashboard_url = links.dashboard_url(host=host, port=port)
    except ValueError as exc:
        raise StartupCheckFailed(str(exc)) from None
    if min_interval != schedules.DEFAULT_MIN_INTERVAL_SECONDS:
        logger.warning("%s=%d: schedules may fire %d s apart instead of %d s; this setting is for tests and "
                       "validation only", schedules.MIN_INTERVAL_ENV, min_interval, min_interval,
                       schedules.DEFAULT_MIN_INTERVAL_SECONDS)

    dsn = configured_dsn()
    database_check = await self_check.check_database(dsn)
    if database_check.status != self_check.OK:
        raise StartupCheckFailed(self_check.format_checks([database_check]))
    pool = await init_pool(dsn)
    await _back_up_before_schema_change(pool, dsn, runtime_root, skip_backup=skip_backup)
    await ensure_schema(pool)

    await seed_role_configs(pool)
    await seed_channel_configs(pool)

    # A built-in runtime for each other runtime type this machine has (Codex or OMP, when on PATH).
    from .profile_registry import register_found_runtimes
    try:
        if found := await register_found_runtimes():
            logger.info("registered the built-in runtime(s) %s", ", ".join(found))
    except Exception:
        logger.warning("could not register the runtimes found on PATH", exc_info=True)

    # Once per database: the plan and session files kept before the database held them.
    from .state_import import import_file_state
    try:
        await import_file_state(runtime_root, pool=pool)
    except Exception:
        logger.exception("the one-time import of plan and session files failed; it runs again at the next start")

    # Before any task resumes or agent launches: end the agents a previous server that crashed or was killed
    # left running, then record this server's.
    from . import proc_tree
    try:
        await proc_tree.sweep(runtime_root)
    except Exception:
        logger.exception("could not sweep the agents a previous server left running")

    # The tasks the previous server was running: those with a live plan carry
    # on once the agents are up; the others are stopped.
    from ..db.plans import resumable_task_ids
    from ..db.tasks import ACTIVE_STATUSES, mark_orphaned_tasks_stopped
    resumable = await resumable_task_ids(ACTIVE_STATUSES, pool=pool)
    orphan_count = await mark_orphaned_tasks_stopped(keep=resumable, pool=pool)
    if orphan_count:
        logger.info("marked %d orphaned task(s) as stopped", orphan_count)

    # The step worktrees no task keeps any more (after the orphaned tasks stopped: those keep their unmerged ones).
    from . import step_workspaces
    try:
        if swept := await step_workspaces.sweep(runtime_root):
            logger.info("removed %d step worktree(s) no task keeps", swept)
    except Exception:
        logger.exception("could not sweep the step worktrees")

    # A Codex login that forked in a run the previous server did not see end (before the session directories
    # it may be in are reclaimed).
    from .runtimes import codex_login
    try:
        if problem := await codex_login.inspect_at_start(runtime_root):
            logger.warning("%s", problem)
    except Exception:
        logger.warning("could not inspect the Codex homes for a forked login", exc_info=True)

    # Before any agent launches: remove the read-only copies of replaced or
    # deleted content that no running process mounts.
    from .read_only_copies import reclaim_unused
    await reclaim_unused(runtime_root)

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
    # whether they run (channel_configs.enabled), read once at startup. An
    # enabled channel without a valid sender allowlist does not start.
    tg_cfg = await get_channel_config("channel-telegram")
    if tg_cfg and tg_cfg.get("enabled"):
        from ..db.channel_configs import get_bot_token
        tg_token = await get_bot_token("channel-telegram") or ""
        telegram_adapter = _telegram_adapter_from_config(tg_cfg.get("config") or {}, tg_token)
        if telegram_adapter is not None:
            await _register_optional_channel(registry, telegram_adapter)

    lark_cfg = await get_channel_config("channel-lark")
    lark_enabled = bool(lark_cfg and lark_cfg.get("enabled"))

    # Without Claude or its login the agents cannot run, but the dashboard can
    # still show and change everything: a warning here and on /api/status.
    checks = [
        database_check,
        *await asyncio.to_thread(self_check.check_claude, claude_bin),
        await self_check.codex_login_check(),
        self_check.check_lark_cli(lark_enabled),
    ]
    self_check.log_problems(checks)

    if lark_enabled:
        lark_adapter = await _lark_adapter(lark_cfg.get("config") or {}, runtime_root)
        if lark_adapter is not None:
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

    # It fires the schedules once the agents and the HTTP server are up, and hears from the TaskRouter when a
    # task ends, to record a scheduled run's outcome.
    scheduler = Scheduler(bus=bus, sender=registry.notify, dashboard_url=dashboard_url)
    task_router = TaskRouter(runtime_root=runtime_root, broadcaster=broadcaster, claude_bin=claude_bin,
                             on_task_end=scheduler.task_ended, dashboard_url=dashboard_url)
    supervisor.register(task_router)

    worker_config = WorkerConfig(claude_bin=claude_bin)
    worker_pool = WorkerPool(config=worker_config, runtime_root=runtime_root)
    supervisor.register(worker_pool)

    # The Manager's platform tools: its MCP server reaches this server with a token made now.
    from .ops.service import ManagerOps, Platform
    manager_ops = ManagerOps(Platform(bus=bus, task_router=task_router, worker_pool=worker_pool, manager=manager,
                                      supervisor=supervisor), url=_local_url(host, port))
    manager.ops = manager_ops

    # --- Start ---

    await supervisor.start_all()
    if resumable:
        logger.info("resuming %d task(s) from their saved plans", len(resumable))
        await task_router.resume_tasks(resumable)

    # --- HTTP Server ---

    http_server = AgentHTTPServer(
        host=host,
        port=port,
        web_adapter=web_adapter,
        task_router=task_router,
        supervisor=supervisor,
        worker_pool=worker_pool,
        manager=manager,
        broadcaster=broadcaster,
        runtime_root=runtime_root,
        self_checks=[check.to_dict() for check in checks],
        claude_bin=claude_bin,
        scheduler=scheduler,
        manager_ops=manager_ops,
    )
    await http_server.start()
    # Its first pass handles the fires missed while the server was down.
    await scheduler.start()

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
    await scheduler.stop()
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
    skip_backup: bool = False,
) -> None:
    """Synchronous entry point for the agent system."""
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    resolved_root = runtime_root or Path.cwd()
    settled = settle_runtime_root(resolved_root)  # ~/.loopx from before the rename becomes ~/.looper
    if settled.note:
        logger.log(logging.WARNING if settled.warning else logging.INFO, "%s", settled.note)
    resolved_root = settled.root
    resolved_work_dir = work_dir or Path.cwd()

    try:
        asyncio.run(
            _run_agent_system(
                host=host,
                port=port,
                runtime_root=resolved_root,
                work_dir=resolved_work_dir,
                claude_bin=claude_bin,
                skip_backup=skip_backup,
            )
        )
    except StartupCheckFailed as exc:
        logger.error("looper serve cannot start:\n%s", exc)
        raise SystemExit(1) from None
