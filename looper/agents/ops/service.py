"""The platform side of the Manager's tools: the endpoint's calls, the Manager's turn, confirmations, the trail.

``looper serve`` makes one ``ManagerOps`` with a random token, which only the
Manager's MCP entry gets (``library_with_server``: ``looper mcp-server``,
with ``LOOPER_AGENT_URL`` and ``LOOPER_AGENT_TOKEN``). The endpoint
(``/api/agent/v1/...``, ``http_server``) checks the token and hands each call
to ``call``.

- **The turn.** The Manager tells which turn runs (``begin_turn`` /
  ``end_turn``): the chat and the message it answers, or none when Claude
  runs a turn on its own. A call belongs to the turn running when it comes;
  without one it counts as a turn Claude runs on its own, which only reads.
- **Confirmations.** A ``high`` call is not run: it is registered for its
  chat, at most one per chat (a new one replaces it), and what it does is
  added to the turn's reply. The user's next message in that chat starts
  with a note naming it; only in the turn answering that message may
  ``looper_confirm`` run it, with the arguments it was asked with, within
  ``CONFIRMATION_SECONDS``. After that turn it lapses. Confirmations live in
  memory: a restart drops them, and the user is asked again.
- **The trail.** Every call is a ``manager_actions`` row (``db.manager_actions``).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import secrets
import sys
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from ...db import manager_actions as db_actions
from . import Call, ErrorCode, OpError, Risk, Tool, Turn, check_arguments
from .tasks import TOOLS as TASK_TOOLS

logger = logging.getLogger(__name__)

# The MCP entry the Manager gets: its name (its tools are mcp__looper__<tool> in Claude Code) and environment.
SERVER_NAME = "looper"
URL_ENV = "LOOPER_AGENT_URL"
TOKEN_ENV = "LOOPER_AGENT_TOKEN"
CONFIRMATION_SECONDS = 600
# The most chats with a confirmation waiting at once; past it, the oldest one's is dropped.
CONFIRMATION_CHATS = 50
CONFIRM_TOOL = "looper_confirm"

CONFIRM = Tool(
    name=CONFIRM_TOOL,
    description="Run the operation the user was asked to confirm, when the user's message you answer confirms it; "
                "the note before that message gives its confirmation_id. It runs as it was asked, unchanged.",
    schema={"type": "object", "required": ["confirmation_id"], "additionalProperties": False,
            "properties": {"confirmation_id": {"type": "string", "minLength": 1}}},
    risk=Risk.HIGH,
    run=lambda call: _never(),
)


async def _never() -> dict[str, Any]:
    raise AssertionError("looper_confirm runs the confirmed call instead")  # ``ManagerOps._confirm``


@dataclass(frozen=True)
class Platform:
    """What the tools act through: the bus and the running agents."""

    bus: Any
    task_router: Any
    worker_pool: Any = None
    manager: Any = None
    supervisor: Any = None


@dataclass
class _Pending:
    """A ``high`` call waiting for the user's word in its chat."""

    id: str
    tool: Tool
    arguments: dict[str, Any]
    block: str  # what the user was shown
    asked_in: str | None  # the message of the turn that asked
    at: float
    answer_turn: str | None = None  # the message of the turn that may confirm it, once it started


class ManagerOps:
    def __init__(self, platform: Platform, *, url: str, token: str | None = None,
                 tools: Sequence[Tool] = (*TASK_TOOLS, CONFIRM), clock: Callable[[], float] = time.monotonic) -> None:
        self._platform = platform
        self._url = url.rstrip("/")
        self._token = token or secrets.token_urlsafe(32)
        self._tools = {tool.name: tool for tool in tools}
        self._clock = clock
        self._turn: Turn | None = None
        self._pending: dict[tuple[str | None, str | None], _Pending] = {}

    # --- The endpoint ---

    def authorized(self, header: str | None) -> bool:
        """Whether an ``Authorization`` header carries this server's token."""
        scheme, _, value = (header or "").partition(" ")
        return scheme.lower() == "bearer" and hmac.compare_digest(value.strip().encode(), self._token.encode())

    def listing(self) -> list[dict[str, Any]]:
        return [tool.listing() for tool in self._tools.values()]

    async def call(self, name: str, raw: Any) -> dict[str, Any]:
        """Run one tool call in the current turn; the protocol's answer."""
        tool = self._tools.get(name)
        if tool is None:
            return _error(ErrorCode.INVALID, f"no tool {name!r}; the tools are: {', '.join(self._tools)}")
        turn = self._turn or Turn(channel=None, chat=None)
        risk = tool.risk
        arguments: dict[str, Any] = raw if isinstance(raw, dict) else {}
        try:
            arguments = check_arguments(tool.schema, raw)
            if tool.name == CONFIRM_TOOL:
                return await self._confirm(arguments["confirmation_id"], turn)
            if risk is not Risk.READ and turn.autonomous:
                raise OpError(ErrorCode.NEEDS_USER_TURN, "Looper changes things only in a turn that answers a user's "
                                                         "message; tell the user what you would do instead")
            call = Call(arguments, turn, self._platform)
            impact = await tool.escalate(call) if tool.escalate is not None else None
            if impact is not None:
                risk = Risk.HIGH
                pending = self._ask(tool, arguments, impact, turn)
                await self._record(turn, tool, arguments, risk, "needs_confirmation", confirmation_id=pending.id)
                return {"status": "needs_confirmation", "confirmation_id": pending.id,
                        "expires_in_seconds": CONFIRMATION_SECONDS,
                        "shown_to_user": "Looper adds what this does and the request to confirm it after your reply. "
                                         "It has not run: do not say it did."}
            result = await tool.run(call)
        except _Recorded as exc:
            return _error(exc.code, str(exc))
        except OpError as exc:
            await self._record(turn, tool, arguments, risk, "error", error_code=exc.code.value,
                               result={"message": str(exc)})
            return _error(exc.code, str(exc))
        except Exception as exc:
            logger.exception("the Manager's call of %s failed", name)
            await self._record(turn, tool, arguments, risk, "error", error_code=ErrorCode.UNAVAILABLE.value,
                               result={"message": f"{type(exc).__name__}: {exc}"})
            return _error(ErrorCode.UNAVAILABLE, f"Looper could not do it now ({type(exc).__name__}: {exc}); tell "
                                                 "the user, and try again later")
        await self._record(turn, tool, arguments, risk, "ok", result=None if risk is Risk.READ else result)
        return {"status": "ok", "result": _plain(result)}

    async def _confirm(self, confirmation_id: str, turn: Turn) -> dict[str, Any]:
        if turn.autonomous:
            raise OpError(ErrorCode.NEEDS_USER_TURN, "only the user's own message confirms an operation")
        pending = self._pending.get(turn.chat_key)
        if pending is None or pending.id != confirmation_id:
            raise OpError(ErrorCode.NOT_FOUND, f"no operation waits for confirmation {confirmation_id} in this chat; "
                                               "run the tool again to ask the user")
        if pending.answer_turn != turn.message_id or self._clock() - pending.at > CONFIRMATION_SECONDS:
            del self._pending[turn.chat_key]
            raise OpError(ErrorCode.EXPIRED, f"confirmation {confirmation_id} lapsed: it counts only in the turn "
                                             "answering the user's very next message, within "
                                             f"{CONFIRMATION_SECONDS // 60} minutes; run the tool again to ask again")
        del self._pending[turn.chat_key]
        tool = pending.tool
        try:
            result = await tool.run(Call(pending.arguments, turn, self._platform))
        except Exception as exc:
            if isinstance(exc, OpError):
                error = exc
            else:
                logger.exception("the Manager's confirmed call of %s failed", tool.name)
                error = OpError(ErrorCode.UNAVAILABLE, f"Looper could not do it now ({type(exc).__name__}: {exc}); "
                                                       "tell the user, and ask again later")
            await self._record(turn, tool, pending.arguments, Risk.HIGH, "error", error_code=error.code.value,
                               result={"message": str(error)}, confirmation_id=pending.id)
            raise _Recorded(error) from None
        await self._record(turn, tool, pending.arguments, Risk.HIGH, "ok", result=result, confirmation_id=pending.id)
        return {"status": "ok", "result": _plain(result)}

    def _ask(self, tool: Tool, arguments: dict[str, Any], impact: str, turn: Turn) -> _Pending:
        """Register ``tool``'s call for the user to confirm, replacing the chat's earlier one, and show it."""
        earlier = self._pending.pop(turn.chat_key, None)
        if earlier is not None and earlier.asked_in == turn.message_id and earlier.block in turn.blocks:
            turn.blocks.remove(earlier.block)  # asked in this turn too: only the newest stands
        block = f"需要你确认：\n{impact}\n回复「确认」就执行；回复别的就不执行。"
        pending = _Pending(id=f"c-{uuid.uuid4().hex[:12]}", tool=tool, arguments=arguments, block=block,
                           asked_in=turn.message_id, at=self._clock())
        self._pending[turn.chat_key] = pending
        while len(self._pending) > CONFIRMATION_CHATS:
            self._pending.pop(next(iter(self._pending)))
        turn.blocks.append(block)
        return pending

    async def _record(self, turn: Turn, tool: Tool, arguments: Mapping[str, Any], risk: Risk, status: str, *,
                      error_code: str | None = None, result: Any = None, confirmation_id: str | None = None) -> None:
        try:
            await db_actions.record(
                channel=turn.channel, chat=turn.chat, message_id=turn.message_id, tool=tool.name,
                arguments=arguments, risk=risk.value, status=status, error_code=error_code, result=result,
                confirmation_id=confirmation_id, task_id=tool.task_id(arguments) or _created_task(result),
            )
        except Exception:
            logger.warning("could not record the Manager's call of %s", tool.name, exc_info=True)

    # --- The Manager's turns ---

    def begin_turn(self, turn: Turn) -> str:
        """``turn`` runs now; the note its message starts with when an operation in its chat waits for the user's
        word ('' otherwise). That operation may be confirmed in this turn only."""
        self._turn = turn
        pending = self._pending.get(turn.chat_key)
        if turn.autonomous or pending is None:
            return ""
        if pending.answer_turn is not None or self._clock() - pending.at > CONFIRMATION_SECONDS:
            del self._pending[turn.chat_key]
            return ""
        pending.answer_turn = turn.message_id
        return (f"[Looper] Your last reply in this chat asked the user to confirm an operation ({pending.tool.name}, "
                f"confirmation_id {pending.id}); Looper showed them:\n{pending.block}\nIf the message below confirms "
                f"it, call {CONFIRM_TOOL} with that confirmation_id. Otherwise do not: it lapses after this turn.")

    def end_turn(self, turn: Turn) -> None:
        """``turn`` is over: an operation it could have confirmed and did not lapses."""
        if self._turn is turn:
            self._turn = None
        pending = self._pending.get(turn.chat_key)
        if pending is not None and not turn.autonomous and pending.answer_turn == turn.message_id:
            del self._pending[turn.chat_key]

    def forget_confirmations(self) -> None:
        """Drop every waiting confirmation (the Manager's session was reset)."""
        self._pending.clear()

    # --- The Manager's MCP entry ---

    def server_config(self) -> dict[str, Any]:
        return {"command": sys.executable, "args": ["-m", "looper.cli", "mcp-server"],
                "env": {URL_ENV: self._url, TOKEN_ENV: self._token}}

    async def library_with_server(self, library: Any, runtime_root: Path) -> Any:
        """``library`` (``AgentLibrary``) with Looper's own MCP server added: always there for the Manager, and
        over a catalog server of the same name."""
        import asyncio

        from ..library import ItemKind, LoadedItem
        from ..mcp import write_config

        servers: dict[str, Any] = {}
        if library.mcp_config:
            servers = dict(json.loads(Path(library.mcp_config).read_text(encoding="utf-8")).get("mcpServers") or {})
        if SERVER_NAME in servers:
            logger.warning("the catalog's MCP server %r is replaced by Looper's own for the Manager", SERVER_NAME)
        servers[SERVER_NAME] = self.server_config()
        path = await asyncio.to_thread(write_config, runtime_root, sorted(servers.items()))
        sha = hashlib.sha256(json.dumps(servers[SERVER_NAME], sort_keys=True).encode("utf-8")).hexdigest()
        items = tuple(item for item in library.items if not (item.kind is ItemKind.MCP and item.name == SERVER_NAME))
        own = LoadedItem(ItemKind.MCP, SERVER_NAME, sha, plugin="", scope="builtin")
        return replace(library, items=(*items, own), mcp_config=str(path))


class _Recorded(OpError):
    """An ``OpError`` of a confirmed call, already recorded."""

    def __init__(self, error: OpError) -> None:
        super().__init__(error.code, str(error))


def _error(code: ErrorCode, message: str) -> dict[str, Any]:
    return {"status": "error", "error": {"code": code.value, "message": message}}


def _plain(value: Any) -> Any:
    """``value`` as JSON has it (dates as ISO text)."""
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))


def _created_task(result: Any) -> str | None:
    return result.get("task_id") if isinstance(result, Mapping) and isinstance(result.get("task_id"), str) else None
