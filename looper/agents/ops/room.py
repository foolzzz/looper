"""The agent room's tools: what a step's run (``RoomOps``) and a task's Planner (``ops.planner``) call.

A Worker's or Reviewer's run in a project with the agent room on gets a token
of its own when it starts (``issue``, ``run.`` and 43 random characters, kept
as a hash, never logged), naming its task and its seat; its MCP entry carries
it as the Planner's does (``looper mcp-server`` with ``LOOPER_AGENT_URL`` and
``LOOPER_AGENT_TOKEN``). The token stops working when the run ends
(``revoke_run``), and the MCP configuration files that carry it are deleted
then. The endpoint (``/api/agent/v1/``) tells a run's token by its prefix.

- **Scope.** A call acts on its token's task, as its token's seat: the sender
  of what it posts is the seat, never an argument. Who may post what to whom is
  ``room.check_scope``.
- **Tools.** A work seat has all five; a review seat has no
  ``looper_room_watch``; a task's Planner has the same four as a Reviewer's
  (it is woken for what is for it) through its own token, where its project
  has the room on.
"""

from __future__ import annotations

import hashlib
import json
import logging
import secrets
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..room import POSTABLE, MemberKind, MessageKind, RoomService, Seat
from . import ErrorCode, OpError, Risk, Tool, check_arguments
from .service import library_with_server, server_config

logger = logging.getLogger(__name__)

# How a run's token starts ('.' is in no Manager token; 'planner.' is a Planner's).
TOKEN_PREFIX = "run."

ROSTER, POST, INBOX, READ, WATCH = ("looper_room_roster", "looper_room_post", "looper_room_inbox", "looper_room_read",
                                    "looper_room_watch")


async def _never(call: Any) -> dict[str, Any]:
    raise AssertionError("room tools run through run_tool")


_NAMES = {"type": "array", "description": 'Seats (backend_rd@s2), roles (fe_rd) or "planner"; ["all"] is everyone.'}
TOOLS: tuple[Tool, ...] = (
    Tool(
        name=ROSTER,
        description="Who is in this task's room: each seat (role@step), its step and whether it is online, waiting, "
                    "offline or not started yet; which seat you are; your unread counts.",
        schema={"type": "object", "additionalProperties": False, "properties": {}},
        risk=Risk.READ, run=_never,
    ),
    Tool(
        name=POST,
        description="Post a short message to this task's room. kind: question (you need an answer from whoever "
                    "decides it), answer (with reply_to, the question's id), interface (something others build "
                    "against is set or changed: an API, a schema, a field, a port; point to files or commits), "
                    "announce (a status others need), info. Most steps need no message.",
        schema={"type": "object", "required": ["kind", "body"], "additionalProperties": False, "properties": {
            "to": {**_NAMES, "description": _NAMES["description"] + " An answer without it goes to the asker."},
            "kind": {"type": "string", "enum": [k.value for k in POSTABLE]},
            "body": {"type": "string", "minLength": 1},
            "reply_to": {"type": "integer", "minimum": 1, "description": "The id of the message this replies to."},
            "files": {"type": "array", "description": "Paths, relative to your working directory."},
            "commits": {"type": "array", "description": "Commit ids (others read them with git show)."},
        }},
        risk=Risk.WRITE, run=_never,
    ),
    Tool(
        name=INBOX,
        description="Your unread room messages, oldest first (for_you: addressed to you); reading marks them read. "
                    "wait_seconds: when nothing is for you yet, wait up to that long for a message for you or an "
                    "interface post.",
        schema={"type": "object", "additionalProperties": False, "properties": {
            "wait_seconds": {"type": "integer", "minimum": 0, "maximum": 120},
            "peek": {"type": "boolean", "description": "Read without marking them read."},
        }},
        risk=Risk.READ, run=_never,
    ),
    Tool(
        name=READ,
        description="Earlier room messages you can see, newest first, marking nothing read: by kind, from (a seat "
                    "or role), about (a message id: its whole thread), unanswered questions, before (a message id).",
        schema={"type": "object", "additionalProperties": False, "properties": {
            "kind": {"type": "string", "enum": [k.value for k in MessageKind]},
            "from": {"type": "string"},
            "about": {"type": "integer", "minimum": 1},
            "unanswered": {"type": "boolean"},
            "before": {"type": "integer", "minimum": 1},
            "limit": {"type": "integer", "minimum": 1, "maximum": 50},
        }},
        risk=Risk.READ, run=_never,
    ),
    Tool(
        name=WATCH,
        description="Be told in your inbox (and reminded) when a seat or role comes online or goes offline "
                    "(presence, state), or when an interface or announce message arrives (kind, optionally from). "
                    "until: first (default) or run_end.",
        schema={"type": "object", "additionalProperties": False, "properties": {
            "presence": {"type": "string"},
            "state": {"type": "string", "enum": ["online", "offline"]},
            "kind": {"type": "string", "enum": [MessageKind.INTERFACE.value, MessageKind.ANNOUNCE.value]},
            "from": {"type": "string"},
            "until": {"type": "string", "enum": ["first", "run_end"]},
        }},
        risk=Risk.WRITE, run=_never,
    ),
)
TOOL_NAMES = frozenset(tool.name for tool in TOOLS)
_BY_NAME = {tool.name: tool for tool in TOOLS}


def tools_for(kind: MemberKind) -> tuple[Tool, ...]:
    """The room tools a member of ``kind`` has."""
    if kind is MemberKind.WORK:
        return TOOLS
    return tuple(tool for tool in TOOLS if tool.name != WATCH)


def normalize_room_arguments(raw: Any) -> Any:
    """A call's arguments with the forms models write taken as meant: ``"to": "all"`` (or one name) as a list."""
    if isinstance(raw, dict) and isinstance(raw.get("to"), str):
        return {**raw, "to": [raw["to"]]}
    return raw


async def run_tool(room: RoomService, task_id: str, seat: Seat, name: str, arguments: Mapping[str, Any]
                   ) -> dict[str, Any]:
    """Run room tool ``name`` with its checked ``arguments`` for ``seat`` in ``task_id``'s room."""
    if name == ROSTER:
        return await room.roster(task_id, seat)
    if name == POST:
        return await room.post(task_id, seat, kind=arguments["kind"], body=arguments["body"],
                               to=arguments.get("to"), reply_to=arguments.get("reply_to"),
                               files=arguments.get("files") or (), commits=arguments.get("commits") or ())
    if name == INBOX:
        return await room.inbox(task_id, seat, wait_seconds=int(arguments.get("wait_seconds") or 0),
                                peek=bool(arguments.get("peek")))
    if name == READ:
        return await room.read(task_id, seat, kind=arguments.get("kind"), sender=arguments.get("from"),
                               about=arguments.get("about"), unanswered=bool(arguments.get("unanswered")),
                               before=arguments.get("before"), limit=int(arguments.get("limit") or 20))
    if name == WATCH:
        return await room.watch(task_id, seat, presence=arguments.get("presence"), state=arguments.get("state"),
                                kind=arguments.get("kind"), sender=arguments.get("from"),
                                until=arguments.get("until") or "first")
    raise OpError(ErrorCode.INVALID, f"no room tool {name!r}")


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _bearer(header: str | None) -> str:
    scheme, _, value = (header or "").partition(" ")
    return value.strip() if scheme.lower() == "bearer" else ""


@dataclass
class _Grant:
    task_id: str
    seat: Seat


class RoomOps:
    def __init__(self, room: RoomService, *, url: str) -> None:
        self.room = room
        self._url = url.rstrip("/")
        self._grants: dict[str, _Grant] = {}  # a token's hash -> what it is good for
        self._files: dict[str, set[str]] = {}  # a run -> the MCP configuration files that carry its token

    # --- Tokens ---

    def issue(self, task_id: str, seat: Seat) -> str:
        """A new token for ``seat``'s run in ``task_id``."""
        token = TOKEN_PREFIX + secrets.token_urlsafe(32)
        self._grants[_digest(token)] = _Grant(task_id, seat)
        return token

    def revoke_run(self, run: str) -> None:
        """``run`` ended: its tokens stop working and the files that carry them go."""
        for digest in [d for d, grant in self._grants.items() if grant.seat.run == run]:
            del self._grants[digest]
        for path in self._files.pop(run, set()):
            Path(path).unlink(missing_ok=True)

    @staticmethod
    def claims(header: str | None) -> bool:
        """Whether an ``Authorization`` header carries a run's token, working or not."""
        return _bearer(header).startswith(TOKEN_PREFIX)

    def _grant(self, header: str | None) -> _Grant | None:
        token = _bearer(header)
        return self._grants.get(_digest(token)) if token.startswith(TOKEN_PREFIX) else None

    def authorized(self, header: str | None) -> bool:
        return self._grant(header) is not None

    # --- The endpoint ---

    def listing(self, header: str | None) -> list[dict[str, Any]]:
        grant = self._grant(header)
        return [tool.listing() for tool in tools_for(grant.seat.kind)] if grant is not None else []

    async def call(self, header: str | None, name: str, raw: Any) -> dict[str, Any]:
        grant = self._grant(header)
        if grant is None:
            return _error(ErrorCode.FORBIDDEN, "this run's token no longer works: its run ended")
        tools = {tool.name: tool for tool in tools_for(grant.seat.kind)}
        tool = tools.get(name)
        if tool is None:
            return _error(ErrorCode.FORBIDDEN if name in TOOL_NAMES or name.startswith("looper_") else ErrorCode.INVALID,
                          f"{name} is not one of your tools; they are: {', '.join(tools)}")
        try:
            arguments = check_arguments(tool.schema, normalize_room_arguments(raw))
            result = await run_tool(self.room, grant.task_id, grant.seat, name, arguments)
        except OpError as exc:
            if tool.risk is not Risk.READ:
                await _note_refusal(grant.task_id, grant.seat, name, exc)
            return _error(exc.code, str(exc))
        except Exception as exc:
            logger.exception("task %s: %s's call of %s failed", grant.task_id, grant.seat.key, name)
            return _error(ErrorCode.UNAVAILABLE, f"Looper could not do it now ({type(exc).__name__}: {exc}); go on "
                                                 "without it")
        return {"status": "ok", "result": json.loads(json.dumps(result, ensure_ascii=False, default=str))}

    # --- A run's MCP entry ---

    async def library_with_server(self, library: Any, runtime_root: Path, token: str, run: str) -> Any:
        """``library`` with Looper's MCP server for the run holding ``token``; its file goes when the run ends."""
        grant = self._grants.get(_digest(token))
        who = grant.seat.key if grant is not None else "a run"
        with_server = await library_with_server(library, runtime_root, server_config(self._url, token), who=who)
        if with_server.mcp_config:
            self._files.setdefault(run, set()).add(str(with_server.mcp_config))
        return with_server


def _error(code: ErrorCode, message: str) -> dict[str, Any]:
    return {"status": "error", "error": {"code": code.value, "message": message}}


async def _note_refusal(task_id: str, seat: Seat, name: str, exc: OpError) -> None:
    from ...db import activity as db_activity

    try:
        await db_activity.insert_activity(task_id=task_id, worker=seat.run or seat.key, kind="phase",
                                          summary=f"Room: {seat.key}'s {name} refused ({exc.code.value}): {exc}"[:500])
    except Exception:
        logger.warning("task %s: could not note a refused room call", task_id, exc_info=True)
