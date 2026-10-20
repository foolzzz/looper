"""The knowledge vault's tools, shared by a task's Planner and its runs."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from .. import vault
from ..vault import VaultConfig
from . import ErrorCode, OpError, Risk, Tool

logger = logging.getLogger(__name__)

SEARCH, READ, WRITE = "looper_vault_search", "looper_vault_read", "looper_vault_write"


async def _never(call: Any) -> dict[str, Any]:
    raise AssertionError("vault tools run through run_tool")


TOOLS: tuple[Tool, ...] = (
    Tool(
        name=SEARCH,
        description="Search the operator's knowledge vault (Markdown notes) for the words of query, in any case, in a "
                    "note's path or text: the notes with the most of the words first, then the most hits. Up to "
                    "limit paths (relative to the vault), each with a snippet. Each vault call re-checks that this "
                    "task's project still has the vault on.",
        schema={"type": "object", "required": ["query"], "additionalProperties": False, "properties": {
            "query": {"type": "string", "minLength": 1},
            "limit": {"type": "integer", "minimum": 1, "maximum": 30},
        }},
        risk=Risk.READ, run=_never,
    ),
    Tool(
        name=READ,
        description="Read a file of the knowledge vault by its path (relative to the vault): its text (the "
                    "first 64 KiB) and the sha256 of the whole file, which looper_vault_write needs to change it.",
        schema={"type": "object", "required": ["path"], "additionalProperties": False, "properties": {
            "path": {"type": "string", "minLength": 1},
        }},
        risk=Risk.READ, run=_never,
    ),
    Tool(
        name=WRITE,
        description="Write a Markdown note (its whole text) in a writable folder of the knowledge vault. "
                    "expected_sha256: the sha256 looper_vault_read gave, null for a new note; a conflict means "
                    "it changed: read it again. reason: one line, the commit's message. Looper commits each write.",
        schema={"type": "object", "required": ["path", "content", "reason"], "additionalProperties": False,
                "properties": {
                    "path": {"type": "string", "minLength": 1},
                    "content": {"type": "string"},
                    "expected_sha256": {"type": ["string", "null"]},
                    "reason": {"type": "string", "minLength": 1, "maxLength": 200},
                }},
        risk=Risk.WRITE, run=_never,
    ),
)
TOOL_NAMES = frozenset(tool.name for tool in TOOLS)


def tools_for(config: VaultConfig) -> tuple[Tool, ...]:
    return TOOLS if config.writable else TOOLS[:2]


@dataclass
class VaultUsage:
    written_bytes: int = 0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)


async def run_tool(config: VaultConfig, name: str, arguments: Mapping[str, Any], *, task_id: str, who: str,
                   worker: str, usage: VaultUsage) -> dict:
    if name == SEARCH:
        return await vault.search(config, arguments["query"], arguments.get("limit"))
    if name == READ:
        return await vault.read(config, arguments["path"])
    if name != WRITE:
        raise OpError(ErrorCode.INVALID, f"no vault tool {name!r}")
    content = arguments["content"]
    if not isinstance(content, str):
        raise OpError(ErrorCode.INVALID, "content must be a string")
    try:
        size = len(content.encode("utf-8"))
    except UnicodeEncodeError:
        raise OpError(ErrorCode.INVALID, "content must be valid UTF-8 text") from None

    async def write_and_note() -> dict:
        if usage.written_bytes + size > vault.RUN_WRITE_MAX_BYTES:
            raise OpError(ErrorCode.RATE_LIMITED, f"this run has written {usage.written_bytes // 1024} KiB to the vault; "
                                                "at most 256 KiB per run")
        result = await vault.write(config, arguments["path"], content, arguments.get("expected_sha256"),
                                   arguments["reason"], task_id=task_id, who=who)
        if not result.get("unchanged"):
            usage.written_bytes += result["bytes"]
            commit = result.get("commit")
            if commit:
                detail = f"commit {commit[:12]}"
            elif result.get("git_status") == "timed_out":
                detail = "not committed: git timed out"
            elif result.get("git_status") == "not_repository":
                detail = "not committed: not a git repository"
            else:
                detail = f"not committed: commit failed, git exit {result.get('git_exit')}"
            await _activity(task_id, worker,
                            f"Vault: {who} wrote {result['path']} ({detail}): {arguments['reason'].strip()}")
        return result

    async with usage.lock:
        # Keep the quota and audit together even when the caller stops waiting.
        pending = asyncio.create_task(write_and_note())
        cancelled = False
        while not pending.done():
            try:
                await asyncio.shield(pending)
            except asyncio.CancelledError:
                cancelled = True
        result = pending.result()
        if cancelled:
            raise asyncio.CancelledError
        return result


async def _activity(task_id: str, worker: str, summary: str) -> None:
    from ...db import activity

    try:
        await activity.insert_activity(task_id=task_id, worker=worker, kind="phase", summary=summary[:500])
    except Exception:
        logger.warning("task %s: could not note a vault call", task_id, exc_info=True)


async def note_refusal(task_id: str, worker: str, who: str, name: str, exc: OpError) -> None:
    await _activity(task_id, worker, f"Vault: {who}'s {name} refused ({exc.code.value}): {exc}")


def planner_guide(config: VaultConfig) -> str:
    if config.writable:
        return "Knowledge vault: looper_vault_search and looper_vault_read read the operator's notes; " \
            f"looper_vault_write writes notes under {config.folders()} only. Read them when earlier notes may help; " \
            "when the task ends, Looper asks you what to keep.\n"
    return "Knowledge vault: looper_vault_search and looper_vault_read read the operator's notes (read only). " \
        "Read them when earlier notes may help.\n"


def run_section(config: VaultConfig) -> str:
    if config.writable:
        return "## Knowledge vault\nlooper_vault_search and looper_vault_read read the operator's notes; " \
            f"looper_vault_write writes notes under {config.folders()} only. Read them when earlier notes may help. " \
            "Most steps write nothing: the Planner keeps what lasts when the task ends."
    return "## Knowledge vault\nlooper_vault_search and looper_vault_read read the operator's notes (read only). " \
        "Read them when earlier notes may help."
