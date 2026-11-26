"""Task-bound MCP tools and outbound review credentials for a task Manager.

Tokens live as hashes, outlive task completion, and are revoked with their
process or when the task is forgotten. Writes require a turn Looper sent.
The CLI scope table is only a speed bump: this process shares the operator's
account and can read its token file or call the HTTP API directly.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import secrets
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ... import git_safe
from ...db import tasks
from .. import outbound_hook, projects, task_conversations_settings
from ..message import task_manager_address
from ..untrusted import Source, fence
from . import ErrorCode, OpError, Risk, Tool, check_arguments
from .service import library_with_server, server_config
from .tasks import _request_error

logger = logging.getLogger(__name__)
TOKEN_PREFIX = "taskmgr."
TOKEN_ENDED = "this task Manager's token no longer works"


async def _status(call: Any) -> dict[str, Any]:
    task = await tasks.get_task(call.task_id)
    result = await projects.task_detail(call.task_id, compact=True)
    result["requirement"] = fence(str(result.get("requirement") or ""), source=Source.TASK_REQUIREMENT)
    if result.get("latest_progress") is not None:
        result["latest_progress"] = fence(str(result["latest_progress"]), source=Source.JOURNAL)
    from ...db.connection import get_pool
    recent = await get_pool().fetch("SELECT summary FROM activity WHERE task_id=$1 ORDER BY seq DESC LIMIT 20",
                                    call.task_id)
    result["activity"] = [{"summary": fence(row["summary"], source=Source.JOURNAL)} for row in recent]
    result["interaction_mode"] = tasks.interaction_mode(task["status"]).value
    result["workspace"] = await _workspace(task)
    return result


async def _requirement_set(call: Any) -> dict[str, Any]:
    return await projects.edit_requirement(call.task_id, dict(call.arguments), task_router=call.platform.task_router,
                                           bus=call.platform.bus, source=task_manager_address(call.task_id),
                                           via="task_manager")


TOOLS = (
    Tool("task_status", "Read this task now: its title, status and whether the background is working on it "
         "(`interaction_mode`), its requirement, its plan and steps with their results when it has run in the "
         "background, open user gates, the latest activity, and the workspace (directory, branch, changed and "
         "uncommitted files, HEAD). Use it when the user asks how the task is going.",
         {"type": "object", "properties": {}, "additionalProperties": False}, Risk.READ, _status),
    Tool("task_requirement_set", "Write a new version of this task's requirement: what it is for and what done "
         "looks like, in the user's words where you have them. Only while the task is a draft or ready; the old "
         "text is kept as a version. Use it when the user agrees on what the task is.",
         {"type": "object", "properties": {"requirement": {"type": "string", "minLength": 1, "maxLength": 20000}},
          "required": ["requirement"], "additionalProperties": False}, Risk.WRITE, _requirement_set),
)


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _bearer(header: str | None) -> str:
    scheme, _, value = (header or "").partition(" ")
    return value.strip() if scheme.lower() == "bearer" else ""


@dataclass
class _Grant:
    task_id: str
    in_turn: bool = False
    hook_file: Path | None = None
    review: tuple[Path, str, outbound_hook.Trusted] | None = None
    writes: list[outbound_hook.Write] = field(default_factory=list)
    notified: bool = False


@dataclass(frozen=True)
class _Call:
    task_id: str
    arguments: dict[str, Any]
    platform: Any


class TaskManagerOps:
    def __init__(self, platform: Any, *, url: str) -> None:
        self._platform, self._url = platform, url.rstrip("/")
        self._grants: dict[str, _Grant] = {}

    def issue(self, task_id: str) -> str:
        if not task_conversations_settings.manager_active():
            raise OpError(ErrorCode.FORBIDDEN, "Task conversations are disabled.")
        for digest, grant in list(self._grants.items()):
            if grant.task_id == task_id:
                self._revoke(digest)
        token = TOKEN_PREFIX + secrets.token_urlsafe(32)
        self._grants[_digest(token)] = _Grant(task_id)
        return token

    def _revoke(self, digest: str) -> None:
        grant = self._grants.pop(digest, None)
        if grant is not None and grant.hook_file is not None:
            grant.hook_file.unlink(missing_ok=True)

    def revoke(self, token: str) -> None:
        self._revoke(_digest(token))

    @staticmethod
    def claims(header: str | None) -> bool:
        return _bearer(header).startswith(TOKEN_PREFIX)

    def _grant(self, header: str | None) -> _Grant | None:
        if not task_conversations_settings.manager_active() or not self.claims(header):
            return None
        return self._grants.get(_digest(_bearer(header)))

    def authorized(self, header: str | None) -> bool:
        return self._grant(header) is not None

    async def task_exists(self, header: str | None) -> bool:
        grant = self._grant(header)
        if grant is None:
            return False
        if await tasks.get_task(grant.task_id) is None:
            self.revoke(_bearer(header))
            return False
        return True

    def begin_turn(self, token: str) -> None:
        if (grant := self._grants.get(_digest(token))) is not None:
            grant.in_turn = True

    def end_turn(self, token: str) -> None:
        if (grant := self._grants.get(_digest(token))) is not None:
            grant.in_turn = False

    async def listing(self, header: str | None) -> list[dict[str, Any]]:
        grant = self._grant(header)
        task = await tasks.get_task(grant.task_id) if grant else None
        if task is None:
            return []
        direct = tasks.interaction_mode(task["status"]) is tasks.InteractionMode.DIRECT
        return [tool.listing() for tool in TOOLS if direct or tool.risk is Risk.READ]

    async def call(self, header: str | None, name: str, arguments: Any, *, call_id: str | None = None
                   ) -> dict[str, Any]:
        try:
            if not await self.task_exists(header):
                raise OpError(ErrorCode.FORBIDDEN, TOKEN_ENDED)
            grant = self._grant(header)
            tool = next((tool for tool in TOOLS if tool.name == name), None)
            if tool is None:
                raise OpError(ErrorCode.NOT_FOUND, f"unknown tool: {name}")
            checked = check_arguments(tool.schema, arguments)
            if tool.risk is Risk.WRITE:
                if not grant.in_turn:
                    raise OpError(ErrorCode.FORBIDDEN, "not in a turn Looper sent")
                task = await tasks.get_task(grant.task_id)
                if tasks.interaction_mode(task["status"]) is tasks.InteractionMode.ORCHESTRATED:
                    raise OpError(ErrorCode.CONFLICT, "the background is working on this task")
            result = await tool.run(_Call(grant.task_id, checked, self._platform))
            return {"status": "ok", "result": json.loads(json.dumps(result, ensure_ascii=False, default=str))}
        except projects.RequestError as exc:
            error = _request_error(exc)
        except OpError as exc:
            error = exc
        except Exception:
            logger.exception("task Manager tool %s failed", name)
            error = OpError(ErrorCode.UNAVAILABLE, "Looper could not do it now")
        return {"status": "error", "error": {"code": error.code.value, "message": str(error)}}

    async def library_with_server(self, library: Any, runtime_root: Path, token: str) -> Any:
        return await library_with_server(library, runtime_root, server_config(self._url, token), who="a task Manager")

    def tool_hook(self, token: str, task_id: str, runtime_root: Path, claude_bin: str, library: Any) -> Any:
        grant = self._grants.get(_digest(token))
        if grant is None or grant.task_id != task_id:
            raise OpError(ErrorCode.FORBIDDEN, TOKEN_ENDED)
        hook = outbound_hook.write_hook(runtime_root, f"task-manager-{task_id}", url=self._url, token=token)
        grant.hook_file = runtime_root.absolute() / ".local" / "outbound-hook" / f"task-manager-{task_id}.json"
        grant.review = runtime_root, claude_bin, outbound_hook.Trusted.of(library, [tool.name for tool in TOOLS])
        return hook

    async def check_outbound(self, header: str | None, payload: Any) -> dict[str, Any]:
        grant = self._grant(header)
        if not await self.task_exists(header) or grant is None or grant.review is None:
            return {"decision": "block", "message": outbound_hook.BLOCKED_MESSAGE.format(reason=TOKEN_ENDED)}
        runtime_root, claude_bin, trusted = grant.review
        try:
            answer, review = await outbound_hook.answer(payload, agent_id=f"task-manager-{grant.task_id}",
                place=lambda: outbound_hook.task_placed(grant.task_id), writes=list(grant.writes),
                runtime_root=runtime_root, claude_bin=claude_bin, trusted=trusted, task_id=grant.task_id)
        except Exception:
            logger.exception("task Manager outbound check failed")
            return {"decision": "block", "message": outbound_hook.BLOCKED_MESSAGE.format(reason="the check could not run")}
        if answer["decision"] == "allow":
            if (write := outbound_hook.write_of(payload)) is not None:
                grant.writes.append(write)
                del grant.writes[:-outbound_hook.CONTENT_TOOLS_KEPT]
        elif review is not None and not grant.notified:
            grant.notified = True
            data = payload if isinstance(payload, dict) else {}
            tool_input = data.get("tool_input") if isinstance(data.get("tool_input"), dict) else {}
            await outbound_hook.notify_blocked(f"task Manager of task {grant.task_id}", str(data.get("tool_name") or ""),
                                               str(tool_input.get("command") or ""), review.reason, review.review_id)
        return answer


async def _workspace(task: dict[str, Any]) -> dict[str, Any]:
    directory = str(task.get("work_dir") or task.get("workspace_dir") or "")
    empty = {"dir": directory, "mode": task.get("workspace_mode") or "in_place",
             "branch": None, "head": None, "changed": [], "uncommitted": 0}

    async def git(*args: str) -> str:
        try:
            process = await git_safe.spawn(directory, *args, stdout=asyncio.subprocess.PIPE,
                                           stderr=asyncio.subprocess.DEVNULL, stdin=asyncio.subprocess.DEVNULL)
        except git_safe.RepositoryRefused as error:
            raise ValueError("not a git checkout") from error
        try:
            async with asyncio.timeout(5):
                stdout, _ = await process.communicate()
        except BaseException:
            process.kill()
            await process.wait()
            raise
        if process.returncode:
            raise ValueError("not a git checkout")
        return stdout.decode("utf-8", errors="replace")

    try:
        head = (await git("rev-parse", "--short", "HEAD")).strip()
        branch = (await git("branch", "--show-current")).strip() or None
        records = iter((await git("status", "--porcelain", "-z", "--untracked-files=all")).split("\0"))
        changed = []
        for record in records:
            if record:
                changed.append(record[3:])
                if "R" in record[:2] or "C" in record[:2]:
                    next(records, None)  # porcelain -z puts the rename's old path in the next record
        return {**empty, "branch": branch, "head": head, "changed": changed[:50], "uncommitted": len(changed)}
    except (OSError, ValueError, TimeoutError):
        return empty
