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
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .. import env, ops_config
from ..ops_config import OpsConfig
from .broadcast import Broadcaster
from .bus import MessageBus
from .channels.allowlist import SenderAllowlist, allowlist_from_config, start_problem
from .channels.base import ChannelAdapter
from .channels.bots import BOT_TYPES, builtin_env_token, clashes, lark_app_id
from .channels.lark import LarkChannelAdapter
from .channels.registry import ChannelRegistry
from .channels.chat_routing import ChatResolver, ChatStop
from .channels.telegram import TelegramChannelAdapter
from .channels.web import WebChannelAdapter
from . import self_check
from .gate_courier import GateCourier
from .gate_service import GateReminders
from .wait_service import WaitService
from .http_server import AgentHTTPServer
from .log_redaction import log_to_stderr
from .scheduler import Scheduler
from .runtimes.runner.supervisor import RunnerSupervisor
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
                                  resolve_chat=resolve_chat, stop_chat=stop_chat)


def _lark_adapter_from_config(config: dict[str, Any], runtime_root: Path, *,
                              bot_open_id: str = "", channel_id: str = "channel-lark",
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
                              on_rejected=rejected, bot_open_id=bot_open_id, channel_id=channel_id,
                              manager_id=manager_id, manager_running=manager_running,
                              resolve_chat=resolve_chat, stop_chat=stop_chat)


async def _lark_adapter(config: dict[str, Any], runtime_root: Path, *, channel_id: str = "channel-lark",
                        manager_id: str = "manager",
                        manager_running: Callable[[str], bool] | None = None,
                        same_bot_as: Callable[[str], str | None] | None = None,
                        resolve_chat: ChatResolver | None = None, stop_chat: ChatStop | None = None) -> LarkChannelAdapter | None:
    """The Lark adapter, once lark-cli acts as the bot under the channel's profile and the allowlist was made for
    that app, and ``same_bot_as`` (given the app it acts as) names no running channel of that app; None (logged)
    otherwise."""
    from .channels import lark_cli

    if _sender_allowlist(channel_id, "lark", config) is None:
        return None
    problem, bot = await lark_cli.check(config, fresh=True)
    if problem or bot is None:
        logger.error("%s not started: %s", channel_id, problem)
        return None
    if same_bot_as is not None and (earlier := same_bot_as(bot.app_id)) is not None:
        logger.error("%s not started: it is the same bot as %s", channel_id, earlier)
        return None
    logger.info("%s: bot %s (app %s, lark-cli profile %s)", channel_id, bot.bot_name, bot.app_id, bot.profile)
    return _lark_adapter_from_config(config, runtime_root, bot_open_id=bot.bot_open_id, channel_id=channel_id,
                                     manager_id=manager_id, manager_running=manager_running,
                                     resolve_chat=resolve_chat, stop_chat=stop_chat)


async def _lark_bot_adapter(config: dict[str, Any], secret: str, runtime_root: Path, *,
                            channel_id: str = "channel-lark_bot", manager_id: str = "manager",
                            manager_running: Callable[[str], bool] | None = None,
                            resolve_chat: ChatResolver | None = None, stop_chat: ChatStop | None = None) -> LarkChannelAdapter | None:
    """Start the bot-token channel only with a valid allowlist and matching app credentials."""
    from ..db.channel_configs import record_rejected
    from .channels import lark_bot_settings
    from .channels.lark_bot import make_adapter
    from .log_redaction import add_secret

    allowlist = _sender_allowlist(channel_id, "lark_bot", config)
    if allowlist is None:
        return None
    add_secret(secret)
    problem, bot = await lark_bot_settings.check(config, secret, fresh=True)
    if problem or bot is None:
        logger.error("%s not started: %s", channel_id, problem)
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
                         *, stop_chat: ChatStop | None = None) -> None:
    """Build and register the adapter of each row of ``channel_types``; one that fails to start leaves the rest.

    A lark-cli row's app is known for sure only once its adapter asks lark-cli (``_enabled_bot_rows`` may not have
    learnt it), so each row is checked again against the bots this pass started, with the app its adapter found."""
    started: list[dict[str, Any]] = []

    def same_bot_as(row: dict[str, Any], app_id: str | None) -> str | None:
        return clashes([*started, {**row, "app_id": app_id}]).get(row["channel_id"])

    for row in rows:
        if row["channel_type"] not in channel_types:
            continue
        kwargs: dict[str, Any] = {"channel_id": row["channel_id"], "manager_id": row.get("manager_id") or "manager",
                                  "manager_running": manager_running, "resolve_chat": registry.resolve_chat,
                                  "stop_chat": stop_chat}
        adapter: ChannelAdapter | None
        found: list[str] = []
        if row["channel_type"] == "lark":
            def lark_same_bot_as(app_id: str, row: dict[str, Any] = row) -> str | None:
                found.append(app_id)
                return same_bot_as(row, app_id)

            adapter = await _lark_adapter(row["config"], runtime_root, same_bot_as=lark_same_bot_as, **kwargs)
        elif (earlier := same_bot_as(row, row.get("app_id"))) is not None:
            logger.error("%s not started: it is the same bot as %s", row["channel_id"], earlier)
            continue
        elif row["channel_type"] == "telegram":
            adapter = _telegram_adapter_from_config(row["config"], row["secret"], **kwargs)
        else:
            adapter = await _lark_bot_adapter(row["config"], row["secret"], runtime_root, **kwargs)
        if adapter is not None and await _register_optional_channel(registry, adapter):
            started.append({**row, "app_id": found[0] if found else row.get("app_id")})


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
    await _migrate_lark_config(pool)


LARK_DISPLAY_NAME = "Lark (lark-cli)"


async def _migrate_lark_config(pool: Any = None) -> None:
    """Lark settings from before lark-cli's profile chose the app: drop ``app_id_env`` and ``app_secret_env``, add
    the empty ``profile`` a new row has (Settings → Channels shows the fields a row holds), and give the row its new
    name when it still has the old default one."""
    from ..db.channel_configs import drop_config_keys
    from ..db.connection import get_pool
    from .channels.lark_cli import LEGACY_KEYS

    db = pool or get_pool()
    for channel_id in await drop_config_keys("lark", list(LEGACY_KEYS), pool=db):
        logger.info("%s: dropped app_id_env and app_secret_env from its settings; lark-cli's profile chooses the "
                    "app now (empty: its active profile)", channel_id)
    await db.execute("UPDATE channel_configs SET config = config || '{\"profile\": \"\"}'::jsonb, updated_at = now() "
                     "WHERE channel_type = 'lark' AND NOT config ? 'profile'")
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
) -> bool:
    """Whether a Settings-enabled channel started; a failure leaves the web dashboard up and its bot unclaimed."""
    try:
        await registry.register(adapter)
    except Exception:
        logger.exception("%s failed to start; continuing without it", adapter.channel_id)
        with contextlib.suppress(Exception):
            await registry.unregister(adapter.channel_id)
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
    logger.info("backed up the database to %s (%d bytes, %s); restore only into a new isolated database, "
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


# How long a shutdown may take before the server exits without finishing it: under the 20 s launchd gives a service
# between SIGTERM and SIGKILL. The next start ends what it leaves (the agents' processes, their runs' records).
SHUTDOWN_DEADLINE_SEC = 15.0


def stop_signal_handler(stop: asyncio.Event, *, exit_now: Callable[[int], Any] = os._exit) -> Callable[[], None]:
    """What SIGINT and SIGTERM do: the first starts the shutdown (``stop``), another exits at once."""

    def handler() -> None:
        if stop.is_set():
            logger.warning("another shutdown signal: exiting now, without finishing the shutdown")
            exit_now(1)
            return
        logger.info("shutdown signal received")
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
    if runner_supervisor is None and ops.pi_runtime == "runner":
        from .runtimes import pi_runner

        runner_supervisor = pi_runner.enable(runtime_root)
    if runner_supervisor is None:
        await _run_agent_system_owned(**kwargs)
        return
    from ..db import close_pool
    async with contextlib.AsyncExitStack() as ownership:
        ownership.push_async_callback(close_pool)
        ownership.push_async_callback(runner_supervisor.stop)
        await _run_agent_system_owned(**kwargs, runner_supervisor=runner_supervisor, ownership=ownership)
        ownership.pop_all()  # The normal shutdown already stopped these owners and closed the database.


async def _run_agent_system_owned(
    *,
    ops: OpsConfig,
    runtime_root: Path,
    work_dir: Path,
    claude_bin: str = "claude",
    skip_backup: bool = False,
    runner_supervisor: RunnerSupervisor | None = None,
    ownership: contextlib.AsyncExitStack | None = None,
) -> None:
    """Start the full agent system with the ops settings ``ops`` and run until interrupted."""

    # --- Database (required) ---
    from ..db import init_pool, ensure_schema, close_pool

    from . import chat_settings, links, operator_notes, scheduler_settings, waits
    host, port = ops.host, ops.port
    dashboard_url = links.dashboard_url(host=host, port=port, public_url=ops.public_url)
    operator_notes.configure_root(ops.operator_notes_root)

    from . import operator_auth
    try:
        operator_token = operator_auth.ensure_token(runtime_root)
    except operator_auth.OperatorTokenError as exc:
        raise StartupCheckFailed(str(exc)) from None

    dsn = ops.database_url
    database_check = await self_check.check_database(dsn)
    if database_check.status != self_check.OK:
        raise StartupCheckFailed(self_check.format_checks([database_check]))
    pool = await init_pool(dsn)
    await _back_up_before_schema_change(pool, ops, runtime_root, skip_backup=skip_backup)
    await ensure_schema(pool)
    try:
        await scheduler_settings.load()
        await waits.load()
        await chat_settings.load()
    except ValueError as exc:
        raise StartupCheckFailed(str(exc)) from None

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

    # The tasks the previous server was running: they carry on once the agents are up, or are stopped now.
    left_running = await tasks_left_running(pool)
    # The runs the previous server did not see end: nothing runs yet, so every record still running is one.
    from ..db.llm_records import end_unfinished_records
    if unfinished := await end_unfinished_records(pool=pool):
        logger.info("ended %d run record(s) a previous server left running as interrupted", unfinished)

    # The step worktrees no task keeps any more (after the orphaned tasks stopped: those keep their unmerged ones).
    from . import step_workspaces
    try:
        if swept := await step_workspaces.sweep(runtime_root):
            logger.info("removed %d step worktree(s) no task keeps", swept)
    except Exception:
        logger.exception("could not sweep the step worktrees")
    # Then the tasks' own worktrees (the step worktrees are of them): those of completed or deleted tasks go, their
    # branches stay.
    from . import task_workspaces
    try:
        if swept := await task_workspaces.sweep(runtime_root):
            logger.info("removed %d task worktree(s) of tasks that ended", swept)
    except Exception:
        logger.exception("could not sweep the task worktrees")

    # A Codex or Grok login that forked in a run the previous server did not see end (before the session
    # directories it may be in are reclaimed).
    from .runtimes import codex_login, grok_login
    for login, label in ((codex_login, "Codex"), (grok_login, "Grok")):
        try:
            if problem := await login.inspect_at_start(runtime_root):
                logger.warning("%s", problem)
        except Exception:
            logger.warning("could not inspect the %s homes for a forked login", label, exc_info=True)

    # Before any agent launches: remove the read-only copies of replaced or
    # deleted content that no running process mounts.
    from .read_only_copies import reclaim_unused
    await reclaim_unused(runtime_root)
    # The sessions the agent room's runs kept (Codex, OMP) of tasks gone or ended over a week ago.
    from .room import RUN_SESSIONS_KEPT_DAYS, sweep_run_sessions
    try:
        from ..db import room as db_room
        sweep_run_sessions(runtime_root, await db_room.tasks_keeping_sessions(days=RUN_SESSIONS_KEPT_DAYS))
    except Exception:
        logger.warning("could not remove the kept sessions of ended tasks", exc_info=True)

    logger.info("database initialised")

    broadcaster = Broadcaster()

    bus = MessageBus(runtime_root=runtime_root)
    supervisor = AgentSupervisor(bus=bus, runtime_root=runtime_root)
    if ownership is not None:
        ownership.push_async_callback(supervisor.stop_all)
    registry = ChannelRegistry(bus=bus)

    # --- Channel Adapters ---

    web_adapter = WebChannelAdapter(host=host, port=port)
    await registry.register(web_adapter)

    # Every enabled bot runs as its own adapter (RFC §9.1), read once at startup.
    bot_rows = await _enabled_bot_rows()
    lark_enabled = any(r["channel_type"] == "lark" for r in bot_rows)

    # --- Agent Tiers ---

    manager_config = ManagerConfig(
        claude_bin=claude_bin,
        work_dir=work_dir,
    )

    # It fires the schedules once the agents and the HTTP server are up, and hears from the TaskRouter when a
    # task ends, to record a scheduled run's outcome.
    scheduler = Scheduler(bus=bus, sender=registry.notify, dashboard_url=dashboard_url)
    # It notes the user gates that have waited for the user for their project's reminder interval; the courier
    # posts what the gates' discussions say to the chats their tasks came from.
    gate_reminders = GateReminders()
    gate_courier = GateCourier(registry.post, dashboard_url=dashboard_url)
    task_router = TaskRouter(runtime_root=runtime_root, broadcaster=broadcaster, claude_bin=claude_bin,
                             on_task_end=scheduler.task_ended, dashboard_url=dashboard_url)
    wait_service = WaitService(bus=bus, has_planner=task_router.has_planner, dashboard_url=dashboard_url)
    supervisor.register(task_router)

    worker_config = WorkerConfig(claude_bin=claude_bin)
    worker_pool = WorkerPool(config=worker_config, runtime_root=runtime_root)
    supervisor.register(worker_pool)

    # The Manager's platform tools: its MCP server reaches this server with a token made now. Each task's Planner
    # has its own, with a token for its task made when it starts.
    from .ops.planner import PlannerOps
    from .ops.service import Platform
    from . import managers_api
    from .managers import ManagerFleet
    platform = Platform(bus=bus, task_router=task_router, worker_pool=worker_pool, manager=None,
                        supervisor=supervisor, scheduler=scheduler, runtime_root=runtime_root)
    fleet = ManagerFleet(platform=platform, supervisor=supervisor, runtime_root=runtime_root,
                         url=_local_url(host, port), config=manager_config, broadcaster=broadcaster)
    if ownership is not None:
        ownership.push_async_callback(fleet.stop)
    task_router.manager_running = fleet.is_running
    await fleet.load()
    # Start receiving only once every enabled Manager is known, so backlog is not lost or sent to a disabled one.
    await _register_bots(registry, bot_rows, runtime_root, fleet.is_running, ("telegram",), stop_chat=fleet.stop_chat)
    await _register_bots(registry, bot_rows, runtime_root, fleet.is_running, ("lark", "lark_bot"), stop_chat=fleet.stop_chat)

    # Poll before the advisory checks: Telegram drops pending updates when polling starts, so messages sent
    # during a slow check must already be received. Problems only warn here and on /api/v1/system/status.
    checks = [
        database_check,
        *await asyncio.to_thread(self_check.check_claude, claude_bin),
        await self_check.codex_login_check(),
        await self_check.grok_login_check(),
        self_check.check_lark_cli(lark_enabled),
        await asyncio.to_thread(self_check.check_git),
    ]
    self_check.log_problems(checks)

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
    consults = ConsultService(room=room, worker_pool=worker_pool, runtime_root=runtime_root, claude_bin=claude_bin)
    room.consults = consults
    planner_ops.room = room
    room_ops = RoomOps(room, url=_local_url(host, port))
    worker_pool.room_ops = room_ops

    # The consults the last server left queued or running run again.
    await consults.resume_all()

    # --- Start ---

    await supervisor.start_all()
    await fleet.sweep()
    await carry_on_tasks(task_router, left_running)

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
        operator_token=operator_token,
        dashboard_url=dashboard_url,
        self_checks=[check.to_dict() for check in checks],
        claude_bin=claude_bin,
        scheduler=scheduler,
        manager_ops=manager_ops,
        managers=fleet,
        planner_ops=planner_ops,
        room_ops=room_ops,
        client_ops=client_ops,
        ops_config=ops,
        channels=registry,
    )
    await http_server.start()
    # Its first pass handles the fires missed while the server was down.
    await scheduler.start()
    await gate_reminders.start()
    # Check due waits only after the task router and HTTP API are ready.
    await wait_service.start()
    await gate_courier.start()

    logger.info(
        "agent system running on http://%s:%d (channels=%s)",
        host, port,
        ",".join(c["channel_id"] for c in registry.list_channels() if c.get("healthy")),
    )

    # Wait for shutdown signal
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop_signal_handler(stop_event))

    await stop_event.wait()

    # --- Shutdown ---

    async def shut_down() -> None:
        logger.info("shutting down agent system...")
        await consults.stop()
        await gate_reminders.stop()
        # Cancel and reap in-flight wait checks before stopping agents and their bus.
        await wait_service.stop()
        await gate_courier.stop()
        await scheduler.stop()
        await http_server.stop()
        await fleet.stop()
        await supervisor.stop_all()
        if runner_supervisor is not None:
            await runner_supervisor.stop()
        await registry.stop_all()
        await bus.shutdown()
        await close_pool()
        logger.info("agent system stopped")

    await shut_down_within(shut_down())


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
