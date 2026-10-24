"""The API client's side of the ops catalog (RFC §7–§8): one request is one turn, writes run, ``high`` waits for
``confirm`` from the same client and session.

A client (``/api/v1``) has the Manager's tools but for the two that exist only in a chat (``MANAGER_ONLY``), and two of
its own: ``looper_task_pipeline`` and ``looper_gates``. It is not in a chat that answers a user's message, so Looper
does not wait for one: a write runs at once, as the operator's own request, and a ``high`` call is registered under
the client and its session (``X-Looper-Session``) until the same client confirms it there, within
``CONFIRMATION_SECONDS``. Confirmations live in memory, as the Manager's do.
"""

from __future__ import annotations

import logging
import re
import time
import uuid
from collections import OrderedDict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ...db import manager_actions as db_actions, tasks as db_tasks, user_gates as db_user_gates
from .. import gate_service
from ..managers_api import DEFAULT_MANAGER_ID
from ..message import channel_address
from ..tiers.journal import clip
from ..user_gates import MESSAGE_MAX_CHARS
from . import API_CHANNEL, Call, ErrorCode, OpError, Risk, Tool, Turn, check_arguments
from .config import answered
from .service import (CONFIRM_TOOL, CONFIRMATION_CHATS, CONFIRMATION_SECONDS, MANAGER_TOOLS, Platform, _created_task,
                      _error, _plain)
from .tasks import _TASK_ID, _gate_listed, _group, _task_argument, _via

if TYPE_CHECKING:
    from ..api_v1 import Principal

logger = logging.getLogger(__name__)

# The Manager's tools a client does not have: a reply to a gate is the user's own words in a chat (``looper_gates``
# is the client's), and the confirmation is ``POST /api/v1/confirm``.
MANAGER_ONLY = frozenset({"looper_task_gate_reply", CONFIRM_TOOL})
SESSION = re.compile(r"[A-Za-z0-9._:-]{1,64}")
DEFAULT_SESSION = "default"


async def _pipeline(call: Call) -> dict[str, Any]:
    from ..wait_service import waits_view

    task_id = call.arguments["task_id"]
    task = await db_tasks.get_task_with_steps(task_id)
    if task is None:
        raise OpError(ErrorCode.NOT_FOUND, f"no task {task_id}: find its id with looper_tasks")
    return {
        "task_id": task_id, "name": task["name"], "status": task["status"], "group": _group(task).value,
        "phase": task.get("phase"),
        "steps": [{"step": s.get("step_key") or s.get("id"), "kind": s.get("kind") or "work",
                   "status": s.get("status"), "depends_on": list(s.get("depends_on") or []),
                   **({"role": s["role_key"]} if s.get("role_key") else {}),
                   "instruction": clip(s.get("instruction") or "", 200)}
                  for s in task.get("steps") or []],
        "gates": [_gate_listed(g) for g in await db_user_gates.list_for_task(task_id) if g.get("state") != "pending"],
        "waits": await waits_view(task_id),
    }


PIPELINE_TOOL = Tool(
    name="looper_task_pipeline",
    description="A task's pipeline: its steps in plan order with kind, status, what each depends on and the role it "
                "runs as, its user gates and wait steps.",
    schema={"type": "object", "required": ["task_id"], "additionalProperties": False,
            "properties": {"task_id": _TASK_ID}},
    risk=Risk.READ, run=_pipeline, task_id=_task_argument,
)

_GATE_LIST_ARGUMENTS = ("state", "waiting_on", "project_id", "limit", "offset")


def _gate_target(arguments: Mapping[str, Any]) -> tuple[str, str]:
    task_id, label = arguments.get("task_id"), arguments.get("gate")
    if not task_id or not label:
        raise OpError(ErrorCode.INVALID, f"task_id and gate are required for action {arguments['action']}")
    return task_id, label


async def _gates(call: Call) -> dict[str, Any]:
    arguments = call.arguments
    action = arguments["action"]
    if action == "list":
        return await answered(gate_service.list_gates(
            {k: str(v) for k, v in arguments.items() if k in _GATE_LIST_ARGUMENTS and v is not None}))
    task_id, label = _gate_target(arguments)
    if action == "messages":
        return await answered(gate_service.thread(task_id, label))
    if action == "reply" and not arguments.get("text"):
        raise OpError(ErrorCode.INVALID, "text is required for action reply")
    end = action == "end"
    data = ({"text": arguments.get("text") or ""} if end else
            {key: arguments[key] for key in ("text", "agenda_item", "option") if arguments.get(key) is not None})
    return await answered(gate_service.post_message(
        task_id, label, data, has_planner=call.platform.task_router.has_planner(task_id), bus=call.platform.bus,
        source=call.manager_address if call.turn.client_id else channel_address("web"),
        via=_via(call.turn) if call.turn.client_id else gate_service.VIA_DASHBOARD, end=end))


GATES_TOOL = Tool(
    name="looper_gates",
    description="User gates: list them (state, waiting_on, project_id, limit, offset), read one's discussion "
                "(messages), add the user's answer (reply: text, or an option of an agenda item), or end its "
                "discussion (end, with an optional text). The text is the user's own words.",
    schema={
        "type": "object", "required": ["action"], "additionalProperties": False,
        "properties": {
            "action": {"type": "string", "enum": ["list", "messages", "reply", "end"]},
            "task_id": _TASK_ID,
            "gate": {"type": "string", "minLength": 1, "description": "The gate's label (its step), from list."},
            "text": {"type": "string", "minLength": 1, "maxLength": MESSAGE_MAX_CHARS},
            "agenda_item": {"type": "string"},
            "option": {"type": "string"},
            "state": {"type": "string", "enum": ["open", "closed", "all"]},
            "waiting_on": {"type": "string", "enum": ["user", "planner"]},
            "project_id": {"type": "string"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 200},
            "offset": {"type": "integer", "minimum": 0},
        },
    },
    risk=Risk.WRITE, run=_gates, task_id=_task_argument, read_actions=frozenset({"list", "messages"}),
)

CLIENT_TOOLS: tuple[Tool, ...] = (*(t for t in MANAGER_TOOLS if t.name not in MANAGER_ONLY), PIPELINE_TOOL,
                                  GATES_TOOL)


@dataclass
class _Pending:
    """A ``high`` call waiting for its client's ``confirm``."""

    id: str
    tool: Tool
    arguments: dict[str, Any]
    impact: str
    at: float


class ClientOps:
    def __init__(self, platform: Platform, *, tools: Sequence[Tool] | None = None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._platform = platform
        self._clock = clock
        self.tools: Mapping[str, Tool] = {tool.name: tool for tool in (CLIENT_TOOLS if tools is None else tools)}
        # Per (client, session), at most one; the oldest key goes first.
        self._pending: OrderedDict[tuple[str, str], _Pending] = OrderedDict()

    def _turn(self, principal: Principal, session: str) -> Turn:
        return Turn(channel=API_CHANNEL, chat=session, message_id=f"api-{uuid.uuid4().hex[:12]}",
                    client_id=principal.client_id, client_name=principal.client_name)

    async def call(self, principal: Principal, session: str, name: str, raw: Any, *,
                   tool: Tool | None = None, validator: Callable[[Mapping[str, Any], Any], dict[str, Any]] = check_arguments,
                   require_confirmation: bool = True) -> dict[str, Any]:
        """Run one operation for ``principal``; the protocol's answer (``ok``, ``needs_confirmation`` or ``error``)."""
        if not SESSION.fullmatch(session):
            return _error(ErrorCode.INVALID, "X-Looper-Session: 1–64 of letters, digits, '.', '_', ':' or '-'")
        tool = tool or self.tools.get(name)
        if tool is None:
            if name in MANAGER_ONLY:
                return _error(ErrorCode.FORBIDDEN, f"{name} is for the Manager's chat; here use looper_gates reply "
                                                   "for a gate, and POST /api/v1/confirm for a confirmation")
            return _error(ErrorCode.INVALID, f"no operation {name!r}")
        turn = self._turn(principal, session)
        risk = tool.risk
        arguments: dict[str, Any] = raw if isinstance(raw, dict) else {}
        try:
            arguments = validator(tool.schema, raw)
            risk = tool.risk_of(arguments)
            call = Call(arguments, turn, self._platform)
            impact = await tool.escalate(call) if tool.escalate is not None else None
            if impact is not None:
                risk = Risk.HIGH
            if impact is not None and require_confirmation:
                pending = self._ask(principal, session, tool, arguments, impact)
                await self._record(turn, tool, arguments, risk, "needs_confirmation", confirmation_id=pending.id)
                return {"status": "needs_confirmation", "confirmation_id": pending.id,
                        "expires_in_seconds": CONFIRMATION_SECONDS, "impact": impact}
            result = await self._run(tool, call)
        except OpError as exc:
            await self._record(turn, tool, arguments, risk, "error", error_code=exc.code.value,
                               result={"message": str(exc)})
            answer = _error(exc.code, str(exc))
            if exc.details is not None:
                answer["error"]["details"] = exc.details
            return answer
        except Exception as exc:
            logger.exception("an API client's call of %s failed", name)
            await self._record(turn, tool, arguments, risk, "error", error_code=ErrorCode.UNAVAILABLE.value,
                               result={"message": f"{type(exc).__name__}: {exc}"})
            return _error(ErrorCode.UNAVAILABLE, f"Looper could not do it now ({type(exc).__name__}: {exc}); try "
                                                 "again later")
        await self._record(turn, tool, arguments, risk, "ok", result=result)
        return _ok(result, turn)

    async def confirm(self, principal: Principal, session: str, confirmation_id: str) -> dict[str, Any]:
        """Run the ``high`` operation this client asked in this session and ``confirmation_id`` names."""
        if not SESSION.fullmatch(session):
            return _error(ErrorCode.INVALID, "X-Looper-Session: 1–64 of letters, digits, '.', '_', ':' or '-'")
        key = (principal.key, session)
        pending = self._pending.get(key)
        if pending is None or pending.id != confirmation_id:
            return _error(ErrorCode.NOT_FOUND, f"no operation waits for confirmation {confirmation_id} for this "
                                               "client and session; call the operation again")
        del self._pending[key]
        if self._clock() - pending.at > CONFIRMATION_SECONDS:
            return _error(ErrorCode.EXPIRED, f"confirmation {confirmation_id} lapsed after "
                                             f"{CONFIRMATION_SECONDS // 60} minutes; call the operation again")
        turn = self._turn(principal, session)
        tool, arguments = pending.tool, pending.arguments
        try:
            result = await self._run(tool, Call(arguments, turn, self._platform))
        except OpError as exc:
            await self._record(turn, tool, arguments, Risk.HIGH, "error", error_code=exc.code.value,
                               result={"message": str(exc)}, confirmation_id=pending.id)
            answer = _error(exc.code, str(exc))
            if exc.details is not None:
                answer["error"]["details"] = exc.details
            return answer
        except Exception as exc:
            logger.exception("an API client's confirmed call of %s failed", tool.name)
            await self._record(turn, tool, arguments, Risk.HIGH, "error", error_code=ErrorCode.UNAVAILABLE.value,
                               result={"message": f"{type(exc).__name__}: {exc}"}, confirmation_id=pending.id)
            return _error(ErrorCode.UNAVAILABLE, f"Looper could not do it now ({type(exc).__name__}: {exc}); call "
                                                 "the operation again later")
        await self._record(turn, tool, arguments, Risk.HIGH, "ok", result=result, confirmation_id=pending.id)
        return _ok(result, turn)

    @staticmethod
    async def _run(tool: Tool, call: Call) -> Any:
        result = _plain(await tool.run(call))
        if isinstance(result, dict):
            result.pop("shown_to_user", None)  # for the Manager's reply; a client has none
        return result

    def _ask(self, principal: Principal, session: str, tool: Tool, arguments: dict[str, Any],
             impact: str) -> _Pending:
        """Register the call for ``confirm``, replacing the key's earlier one."""
        key = (principal.key, session)
        self._pending.pop(key, None)
        pending = _Pending(id=f"c-{uuid.uuid4().hex[:12]}", tool=tool, arguments=arguments, impact=impact,
                           at=self._clock())
        self._pending[key] = pending
        while len(self._pending) > CONFIRMATION_CHATS:
            self._pending.popitem(last=False)
        return pending

    async def _record(self, turn: Turn, tool: Tool, arguments: Mapping[str, Any], risk: Risk, status: str, *,
                      error_code: str | None = None, result: Any = None, confirmation_id: str | None = None) -> None:
        if risk is Risk.READ:  # a read is not in the trail
            return
        try:
            await db_actions.record(
                channel=API_CHANNEL, chat=turn.chat, message_id=turn.message_id, tool=tool.name,
                arguments=arguments, risk=risk.value, status=status, error_code=error_code, result=result,
                confirmation_id=confirmation_id, task_id=tool.task_id(arguments) or _created_task(result),
                manager_id=DEFAULT_MANAGER_ID, client_id=turn.client_id,
            )
        except Exception:
            logger.warning("could not record an API client's call of %s", tool.name, exc_info=True)


def _ok(result: Any, turn: Turn) -> dict[str, Any]:
    return {"status": "ok", "result": result, **({"notes": list(turn.blocks)} if turn.blocks else {})}
