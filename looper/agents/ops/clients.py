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
from collections.abc import Awaitable, Callable, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ...db import manager_actions as db_actions, projects as db_projects, tasks as db_tasks, user_gates as db_user_gates
from .. import gate_service, projects, receipts, receipts_settings
from ..managers_api import DEFAULT_MANAGER_ID
from ..message import CancelBy, channel_address
from ..tiers.journal import clip
from ..user_gates import MESSAGE_MAX_CHARS
from . import API_CHANNEL, Call, ErrorCode, OpError, Risk, Tool, Turn, check_arguments
from .config import answered
from .service import (CONFIRM_TOOL, CONFIRMATION_CHATS, CONFIRMATION_SECONDS, MANAGER_TOOLS, Platform, _created_task,
                      _error, _plain)
from .tasks import (TASKS_LISTED, TASKS_SEARCHED, _TASK_ID, _control, _gate_listed, _group, _task_argument, _via,
                    task_workflow_tool)

if TYPE_CHECKING:
    from ..api_v1 import Principal

logger = logging.getLogger(__name__)

# The Manager's tools a client does not have: a reply to a gate is the user's own words in a chat (``looper_gates``
# is the client's), and the confirmation is ``POST /api/v1/confirm``.
MANAGER_ONLY = frozenset({"looper_task_gate_reply", CONFIRM_TOOL})
SESSION = re.compile(r"[A-Za-z0-9._:-]{1,64}")
DEFAULT_SESSION = "default"


async def _pipeline(call: Call) -> dict[str, Any]:
    from ..projects import step_condition_view, task_readiness

    task_id = call.arguments["task_id"]
    task = await db_tasks.get_task_with_steps(task_id)
    if task is None:
        raise OpError(ErrorCode.NOT_FOUND, f"no task {task_id}: find its id with looper_tasks")
    waits = await task_readiness(task, compact=True)
    labels = {s["id"]: s.get("step_key") or s["id"] for s in task.get("steps") or [] if "id" in s}
    return {
        "task_id": task_id, "name": task["name"], "status": task["status"], "group": _group(task).value,
        "phase": task.get("phase"),
        "steps": [{"step": s.get("step_key") or s.get("id"), "kind": s.get("kind") or "work",
                   "status": s.get("status"), "depends_on": list(s.get("depends_on") or []),
                   **step_condition_view(s, labels),
                   **({"role": s["role_key"]} if s.get("role_key") else {}),
                   "instruction": clip(s.get("instruction") or "", 200)}
                  for s in task.get("steps") or []],
        "gates": [_gate_listed(g) for g in await db_user_gates.list_for_task(task_id) if g.get("state") != "pending"],
        "waits": waits,
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
    from .service import operator_call

    return await answered(gate_service.post_message(
        task_id, label, data, has_planner=call.platform.task_router.has_planner(task_id), bus=call.platform.bus,
        source=call.manager_address if call.turn.client_id else channel_address("web"),
        via=_via(call.turn) if call.turn.client_id else gate_service.VIA_DASHBOARD, end=end,
        from_operator=await operator_call(call.turn)))


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


def _client_tasks(original: Tool) -> Tool:
    """The client's owner filter belongs only to its copy of the Manager's task tool."""
    schema = deepcopy(original.schema)
    schema["properties"]["manager_id"] = {"type": "string", "minLength": 1}

    async def run(call: Call) -> dict[str, Any]:
        arguments = call.arguments
        if arguments["action"] != "list":
            if "manager_id" in arguments:
                raise OpError(ErrorCode.INVALID, "manager_id is only valid for action list")
            return await original.run(call)
        if "manager_id" in arguments and arguments["manager_id"] is None:
            raise OpError(ErrorCode.INVALID, "manager_id must be a nonempty string")
        project_id = None
        if arguments.get("project"):
            project = await db_projects.find_project_by_name(projects.project_name(arguments["project"]))
            if project is None:
                raise OpError(ErrorCode.NOT_FOUND, f"no project named {arguments['project']!r}")
            project_id = project["id"]
        manager_id = arguments.get("manager_id")
        if call.scoped:
            if manager_id is not None and manager_id != call.manager_id:
                return {"tasks": [], "more": False}
            manager_id = call.manager_id
        return await projects.list_tasks_page(
            limit=int(arguments.get("limit") or TASKS_LISTED), project_id=project_id,
            group=db_tasks.TaskGroup(arguments["group"]) if arguments.get("group") else None,
            manager_id=manager_id, query=str(arguments.get("query") or ""), compact=True,
            search_limit=TASKS_SEARCHED)

    return replace(original, schema=schema, run=run)


async def _client_control(call: Call) -> dict[str, Any]:
    return await _control(call, by=CancelBy.USER)


CLIENT_TOOLS: tuple[Tool, ...] = (*(_client_tasks(t) if t.name == "looper_tasks" else
                                  task_workflow_tool(t, None) if t.name == "looper_task_create" else
                                  replace(t, run=_client_control) if t.name == "looper_task_control" else t
                                  for t in MANAGER_TOOLS if t.name not in MANAGER_ONLY), PIPELINE_TOOL,
                                  GATES_TOOL)


@dataclass
class _Pending:
    """A ``high`` call waiting for its client's ``confirm``."""

    id: str
    tool: Tool
    arguments: dict[str, Any]
    impact: str
    at: float
    confirmation: dict[str, Any]


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
                   require_confirmation: bool = True,
                   level_of: Callable[[Mapping[str, Any]], Awaitable[Any]] | None = None) -> dict[str, Any]:
        """Run one operation for ``principal``; the protocol's answer (``ok``, ``needs_confirmation`` or ``error``).
        ``level_of`` is where an ``/api/v1`` operation's call reaches (``Operation.level``); a client tool's is its
        own (``ops.levels``)."""
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
        watch = receipts.Watch()
        try:
            arguments = validator(tool.schema, raw)
            risk = tool.risk_of(arguments)
            if principal.kind != "operator" and risk is not Risk.READ \
                    and (blocked := await self._outbound_check(principal, turn, tool, arguments, risk, level_of)):
                return blocked
            call = Call(arguments, turn, self._platform)
            impact = await receipts.escalate(tool, call, watch)
            if impact is not None:
                risk = Risk.HIGH
            if impact is not None and require_confirmation:
                pending = self._ask(principal, session, tool, arguments, impact, confirmation=call.confirmation)
                await self._record(turn, tool, arguments, risk, "needs_confirmation", confirmation_id=pending.id)
                return {"status": "needs_confirmation", "confirmation_id": pending.id,
                        "expires_in_seconds": CONFIRMATION_SECONDS, "impact": impact}
            result = await self._run(tool, call, watch)
        except OpError as exc:
            _refused(watch, turn, tool, arguments, exc)
            await self._record(turn, tool, arguments, risk, "error", error_code=exc.code.value,
                               result={"message": str(exc)}, watch=watch)
            answer = _error(exc.code, str(exc))
            if exc.details is not None:
                answer["error"]["details"] = exc.details
            if exc.field is not None:
                answer["error"]["field"] = exc.field
            return _with_notes(answer, turn)
        except Exception as exc:
            logger.exception("an API client's call of %s failed", name)
            _refused(watch, turn, tool, arguments, OpError(ErrorCode.UNAVAILABLE, "unavailable"))
            await self._record(turn, tool, arguments, risk, "error", error_code=ErrorCode.UNAVAILABLE.value,
                               result={"message": f"{type(exc).__name__}: {exc}"}, watch=watch)
            return _with_notes(_error(ErrorCode.UNAVAILABLE, f"Looper could not do it now ({type(exc).__name__}: "
                                                             f"{exc}); try again later"), turn)
        await self._record(turn, tool, arguments, risk, "ok", result=result, watch=watch)
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
        turn = self._turn(principal, session)
        watch = receipts.Watch()
        if self._clock() - pending.at > CONFIRMATION_SECONDS:
            expired = OpError(ErrorCode.EXPIRED, f"confirmation {confirmation_id} lapsed after "
                                                 f"{CONFIRMATION_SECONDS // 60} minutes; call the operation again")
            _refused(watch, turn, pending.tool, pending.arguments, expired)
            if watch.receipt is not None:  # the receipt is the one thing this exit adds to the trail
                await self._record(turn, pending.tool, pending.arguments, Risk.HIGH, "error", error_code="expired",
                                   result={"message": str(expired)}, confirmation_id=pending.id, watch=watch)
            return _with_notes(_error(expired.code, str(expired)), turn)
        tool, arguments = pending.tool, pending.arguments
        try:
            result = await self._run(tool, Call(arguments, turn, self._platform, confirmation=pending.confirmation),
                                     watch)
        except OpError as exc:
            _refused(watch, turn, tool, arguments, exc)
            await self._record(turn, tool, arguments, Risk.HIGH, "error", error_code=exc.code.value,
                               result={"message": str(exc)}, confirmation_id=pending.id, watch=watch)
            answer = _error(exc.code, str(exc))
            if exc.details is not None:
                answer["error"]["details"] = exc.details
            if exc.field is not None:
                answer["error"]["field"] = exc.field
            return _with_notes(answer, turn)
        except Exception as exc:
            logger.exception("an API client's confirmed call of %s failed", tool.name)
            _refused(watch, turn, tool, arguments, OpError(ErrorCode.UNAVAILABLE, "unavailable"))
            await self._record(turn, tool, arguments, Risk.HIGH, "error", error_code=ErrorCode.UNAVAILABLE.value,
                               result={"message": f"{type(exc).__name__}: {exc}"}, confirmation_id=pending.id,
                               watch=watch)
            return _with_notes(_error(ErrorCode.UNAVAILABLE, f"Looper could not do it now ({type(exc).__name__}: "
                                                             f"{exc}); call the operation again later"), turn)
        await self._record(turn, tool, arguments, Risk.HIGH, "ok", result=result, confirmation_id=pending.id,
                           watch=watch)
        return _ok(result, turn)

    async def _outbound_check(self, principal: Principal, turn: Turn, tool: Tool, arguments: Mapping[str, Any],
                              risk: Risk, level_of: Callable[[Mapping[str, Any]], Awaitable[Any]] | None
                              ) -> dict[str, Any] | None:
        """The ``blocked`` answer when the outbound review stops an API client's call (``ops.review``): a client is
        never the operator, so code blocks every ``external`` call it makes (RFC D17); None while the review is off or
        for any other call. The operator's own calls are not reviewed (RFC section 6.1)."""
        from .. import outbound, outbound_hook
        from ...db import outbound_reviews
        from . import levels, review as layer_one

        if not outbound_hook.known_enabled():  # read at startup, set by its switch
            return None
        level = await level_of(arguments) if level_of is not None else await levels.level_of(tool, arguments, turn)
        if level not in outbound.REVIEWED:
            return None
        action = await layer_one.action_of(tool.name, arguments, level)
        checked = await outbound.review(
            action, outbound.Asker(outbound.Who.ALLOWLISTED_PERSON, API_CHANNEL),
            outbound.Origin(f"client:{principal.client_id}", outbound.Source.TOOL, channel=API_CHANNEL,
                            chat=turn.chat), runtime_root=self._platform.runtime_root or Path.cwd())
        if checked.allowed:
            return None
        message = outbound_hook.BLOCKED_MESSAGE.format(reason=checked.reason.rstrip("."))
        action_id = await self._record(turn, tool, arguments, risk, "error", error_code=ErrorCode.BLOCKED.value,
                                       result={"message": message})
        if action_id is not None and checked.review_id is not None:
            try:
                await outbound_reviews.link_action(checked.review_id, action_id)
            except Exception:
                logger.warning("cannot link outbound review %s to its call", checked.review_id, exc_info=True)
        await outbound_hook.notify_blocked(f"client {principal.client_name or principal.client_id}", tool.name,
                                           action.target, checked.reason, checked.review_id)
        return _error(ErrorCode.BLOCKED, message)

    @staticmethod
    async def _run(tool: Tool, call: Call, watch: receipts.Watch | None = None) -> Any:
        answer = await (receipts.run_watched(tool, call, watch) if watch is not None and receipts_settings.enabled()
                        else tool.run(call))
        result = _plain(answer)
        if isinstance(result, dict):
            result.pop("shown_to_user", None)  # for the Manager's reply; a client has none
        return result

    def _ask(self, principal: Principal, session: str, tool: Tool, arguments: dict[str, Any],
             impact: str, *, confirmation: dict[str, Any]) -> _Pending:
        """Register the call for ``confirm``, replacing the key's earlier one."""
        key = (principal.key, session)
        self._pending.pop(key, None)
        pending = _Pending(id=f"c-{uuid.uuid4().hex[:12]}", tool=tool, arguments=arguments, impact=impact,
                           at=self._clock(), confirmation=dict(confirmation))
        self._pending[key] = pending
        while len(self._pending) > CONFIRMATION_CHATS:
            self._pending.popitem(last=False)
        return pending

    async def _record(self, turn: Turn, tool: Tool, arguments: Mapping[str, Any], risk: Risk, status: str, *,
                      error_code: str | None = None, result: Any = None,
                      confirmation_id: str | None = None, watch: receipts.Watch | None = None) -> int | None:
        if risk is Risk.READ:  # a read is not in the trail
            return
        try:
            return await db_actions.record(
                channel=API_CHANNEL, chat=turn.chat, message_id=turn.message_id, tool=tool.name,
                arguments=arguments, risk=risk.value, status=status, error_code=error_code, result=result,
                confirmation_id=confirmation_id, task_id=tool.task_id(arguments) or _created_task(result),
                manager_id=DEFAULT_MANAGER_ID, client_id=turn.client_id, **receipts.row_fields(watch),
            )
        except Exception:
            logger.warning("could not record an API client's call of %s", tool.name, exc_info=True)


def _refused(watch: receipts.Watch, turn: Turn, tool: Tool, arguments: Mapping[str, Any], error: OpError) -> None:
    """With receipts on, the failed-write receipt of a call that ended in ``error`` before it produced one."""
    if receipts_settings.enabled() and watch.receipt is None:
        receipts.record_refusal(watch, turn, tool.name, arguments, error)


def _with_notes(answer: dict[str, Any], turn: Turn) -> dict[str, Any]:
    """An error answer with the turn's lines (the receipt) in ``notes`` while receipts are on; as it was when off."""
    if receipts_settings.enabled() and turn.blocks:
        answer["notes"] = list(turn.blocks)
    return answer


def _ok(result: Any, turn: Turn) -> dict[str, Any]:
    return {"status": "ok", "result": result, **({"notes": list(turn.blocks)} if turn.blocks else {})}
