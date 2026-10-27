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
- **A stopped turn.** When the user stops a turn (``stop_turn``) or it ends,
  its calls still under way run no further: one waiting in ``escalate``
  registers nothing, and a call that comes later, ``looper_confirm`` too, is
  refused and told so. The confirmation the stopped turn asked is dropped.
- **The trail.** Every call is a ``manager_actions`` row (``db.manager_actions``).
- **When the tools do not work.** Before a turn answering the user, the
  Manager asks ``tools_problem``: it starts the MCP entry's command as the
  runtime would and lists the tools through it, so a server that does not
  start or a token Looper refuses is found before Claude runs (a success is
  kept for ``PROBE_KEPT_SECONDS``). A request the endpoint refuses during a
  turn marks the turn (``refuse``). Either is logged as an error, and the
  user gets ``tools_down_reply`` instead of anything Claude says.
"""

from __future__ import annotations

import hashlib
import asyncio
import hmac
import json
import logging
import os
import secrets
import subprocess
import sys
import time
import uuid
from collections import Counter, OrderedDict
from collections.abc import Callable, Mapping, Sequence
from contextlib import nullcontext
from dataclasses import dataclass, replace
from datetime import date
from pathlib import Path
from types import MappingProxyType
from typing import Any

from ... import child_env
from .. import chat_settings
from ...db import manager_actions as db_actions, managers as db_managers, tasks as db_tasks
from ..managers_api import DEFAULT_MANAGER_ID, ManagerDefinition, ManagerTools, TaskPolicy, TaskScope
from . import Call, ErrorCode, OpError, Risk, Tool, Turn, TurnState, check_arguments
from .concurrency import TOOLS as CONCURRENCY_TOOLS
from .config import TOOLS as CONFIG_TOOLS
from .schedules import TOOLS as SCHEDULE_TOOLS
from .settings import TOOLS as SETTINGS_TOOLS
from .tasks import TOOLS as TASK_TOOLS, task_create_tool

logger = logging.getLogger(__name__)

# The MCP entry the Manager gets: its name (its tools are mcp__looper__<tool> in Claude Code) and environment.
SERVER_NAME = "looper"
URL_ENV = "LOOPER_AGENT_URL"
TOKEN_ENV = "LOOPER_AGENT_TOKEN"
TURN_FILE_ENV = "LOOPER_AGENT_TURN_FILE"
TRACE_ENV = "LOOPER_MCP_TRACE"  # the bridge's trace of each call's ``_meta`` keys (``mcp_server._trace_meta``)
BINDINGS_KEPT = 64
CONFIRMATION_SECONDS = 600
# How long a write call waits for the tool_use of its turn's stream that it matches (the event may trail the call).
MATCH_WAIT_SEC = 2.0
# The most chats with a confirmation waiting at once; past it, the oldest one's is dropped.
CONFIRMATION_CHATS = 50
CONFIRM_TOOL = "looper_confirm"
# How long a working MCP entry is trusted before the next turn checks it again, and how long a check may take.
PROBE_KEPT_SECONDS = 300
PROBE_TIMEOUT_SECONDS = 20

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
    managers: Any = None  # the Manager fleet; the overview lists every Manager from it
    supervisor: Any = None
    scheduler: Any = None  # a schedule's Run now
    runtime_root: Path | None = None  # where a configuration export is written (``exports/``)


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


# The Manager's tools (a task's Planner has its own, ``ops.planner``).
MANAGER_TOOLS: tuple[Tool, ...] = (*TASK_TOOLS, *CONCURRENCY_TOOLS, *CONFIG_TOOLS, *SETTINGS_TOOLS, *SCHEDULE_TOOLS,
                                   CONFIRM)


# What a Manager with ``tools = tasks`` has: the task tools, projects (read only) and the confirmation, in
# MANAGER_TOOLS order.
TASKS_SCOPE_TOOLS: tuple[Tool, ...] = (*TASK_TOOLS, *(t for t in CONFIG_TOOLS if t.name == "looper_projects"), CONFIRM)
READ_ONLY_TASKS = frozenset({"looper_projects"})
_MANAGER_TOOL_NAMES = {tool.name: tool for tool in MANAGER_TOOLS}


@dataclass(frozen=True)
class OpsScope:
    """What a Manager's calls may do, from its definition (RFC §7): the tools it has, those among them it may only
    read, whether it sees its own tasks only, and its task policy. ``revision`` is the definition's
    ``managers.revision`` it was computed from (-1: never compared with the database). The two settings are
    independent: ``tools`` picks the tools, ``task_scope`` alone sets ``scoped``."""

    manager_id: str
    revision: int
    tools: Mapping[str, Tool]
    read_only: frozenset[str]
    scoped: bool
    task_policy: TaskPolicy
    template_keys: tuple[str, ...] = ()


def _tool_map(tools: Sequence[Tool]) -> Mapping[str, Tool]:
    return MappingProxyType({tool.name: tool for tool in tools})


def scope_for(definition: ManagerDefinition, template_keys: Sequence[str] = ()) -> OpsScope:
    """The scope a Manager definition gives; with templates, ``looper_task_create`` takes ``template`` and
    ``inputs``."""
    all_tools = definition.tools is ManagerTools.ALL
    tools = tuple(task_create_tool(template_keys) if tool.name == "looper_task_create" else tool
                  for tool in (MANAGER_TOOLS if all_tools else TASKS_SCOPE_TOOLS))
    return OpsScope(
        manager_id=definition.id, revision=definition.revision,
        tools=_tool_map(tools),
        read_only=frozenset() if all_tools else READ_ONLY_TASKS,
        scoped=definition.task_scope is TaskScope.OWN, task_policy=definition.task_policy,
        template_keys=tuple(template_keys),
    )


def _default_scope() -> OpsScope:
    """The default Manager's: every tool, every task, never compared with the database."""
    definition = ManagerDefinition(
        id=DEFAULT_MANAGER_ID, name="Manager", description="", system_prompt=None, profile_id=None,
        tools=ManagerTools.ALL, task_scope=TaskScope.ALL, task_policy=TaskPolicy.FREE, enabled=True, builtin=True,
        revision=-1)
    return scope_for(definition)


DEFAULT_SCOPE = _default_scope()


class ManagerOps:
    def __init__(self, platform: Platform, *, url: str, token: str | None = None,
                 scope: OpsScope | None = None, tools: Sequence[Tool] | None = None,
                 clock: Callable[[], float] = time.monotonic, turn_file: Path | None = None) -> None:
        self._platform = platform
        self._url = url.rstrip("/")
        self._token = token or secrets.token_urlsafe(32)
        # One attribute holds everything a call is judged by; ``set_scope`` replaces it whole.
        self._scope = scope if scope is not None else (
            replace(DEFAULT_SCOPE, tools=_tool_map(tools)) if tools is not None else DEFAULT_SCOPE)
        self._clock = clock
        self._turn: Turn | None = None
        self._turn_file = turn_file
        self._epoch = 0
        self._bindings: OrderedDict[str, Turn] = OrderedDict()  # the last BINDINGS_KEPT turns, ended ones too
        self._pending: dict[tuple[str | None, str | None, str | None], _Pending] = {}
        self._working_at: float | None = None  # when the MCP entry last listed the tools
        # Per binding, the tool calls its turn's stream showed and no call has claimed yet: (call id, tool, args).
        self._expected: dict[str, list[tuple[str | None, str, str]]] = {}
        self._expected_event: dict[str, asyncio.Event] = {}
        # The calls whose binding disagreed with how Looper placed them, per (reason, runtime, tool): what
        # observe-only shows before the binding is enforced (``binding_report``).
        self._mismatches: Counter[tuple[str, str, str]] = Counter()
        # How the calls that had to match their turn's stream did: (by "id"|"arguments", "at_arrival"|"waited"|
        # "missed"): whether the bridge sent the runtime's call id, and whether its tool_use was read first.
        self._matches: Counter[tuple[str, str]] = Counter()
        self._observing: set[asyncio.Task[None]] = set()
        self._executing = 0

    @property
    def executing(self) -> bool:
        """Admitted executions, independent of the HTTP connection or the lifetime of their turn."""
        return self._executing > 0

    async def _run_tool(self, tool: Tool, call: Call) -> Any:
        fleet = self._platform.managers
        admitted = False
        try:
            # Share retirement's lock only for admission, never while a tool needs the Manager row.
            async with fleet.cleanup_guard(self.scope.manager_id) if fleet is not None else nullcontext():
                if fleet is not None and not fleet.is_running(self.scope.manager_id):
                    raise OpError(ErrorCode.UNAVAILABLE, "The Manager is retiring; this operation did not run")
                _check_answering(call.turn)
                self._executing += 1
                admitted = True
            return await tool.run(call)
        finally:
            if admitted:
                self._executing -= 1

    @property
    def platform(self) -> Platform:
        """What the tools act on (the Manager routes a reply to a user gate through it before its model)."""
        return self._platform

    # --- The endpoint ---

    def authorized(self, header: str | None) -> bool:
        """Whether an ``Authorization`` header carries this server's token."""
        scheme, _, value = (header or "").partition(" ")
        return scheme.lower() == "bearer" and hmac.compare_digest(value.strip().encode(), self._token.encode())

    @property
    def scope(self) -> OpsScope:
        return self._scope

    def set_scope(self, scope: OpsScope) -> None:
        """Judge calls by ``scope`` from now on (one assignment), and drop the confirmations of a tool it lacks."""
        self._scope = scope
        for key, pending in list(self._pending.items()):
            if pending.tool.name not in scope.tools:
                del self._pending[key]

    async def current_scope(self) -> OpsScope:
        """The scope in force now, judged by the database (RFC §7): the Manager's ``revision`` read, and the scope
        rebuilt when the definition changed since. Never falls back to a scope that may be older than what was
        committed."""
        scope = self._scope
        if scope.revision < 0 or scope.manager_id == DEFAULT_MANAGER_ID:
            return scope
        try:
            found = await db_managers.revision_of(scope.manager_id)
            if found is not None and found[1] and found[0] == scope.revision:
                return scope
            row = keys = None
            if found is not None and found[1]:
                row = await db_managers.get_manager(scope.manager_id)
                keys = [t["key"] for t in await db_managers.list_templates(scope.manager_id)] if row else None
            definition = ManagerDefinition.from_row(row) if row is not None else None
        except Exception:
            logger.warning("could not read the settings of Manager %s", scope.manager_id, exc_info=True)
            raise OpError(ErrorCode.UNAVAILABLE, "this Manager's settings changed and are not in force yet; nothing "
                                                 "was done; try again in a moment") from None
        if definition is None or not definition.enabled:
            raise OpError(ErrorCode.FORBIDDEN, "this Manager was turned off")
        current = scope_for(definition, keys or ())
        if self._scope is scope:  # not replaced meanwhile by the fleet
            self.set_scope(current)
        return current

    def listing(self) -> list[dict[str, Any]]:
        return [tool.listing() for tool in self._scope.tools.values()]

    async def current_listing(self) -> list[dict[str, Any]]:
        """The tools the Manager has now (``current_scope``), as ``tools/list`` names them."""
        return [tool.listing() for tool in (await self.current_scope()).tools.values()]

    async def _authorize(self, scope: OpsScope, name: str, arguments: Mapping[str, Any]) -> Tool:
        """The one check of what ``scope`` allows: the tool is among its tools, a read-only tool is only read, and a
        scoped Manager acts only on a task of its own. Returns the tool to run."""
        tool = scope.tools.get(name)
        if tool is None:
            if name in _MANAGER_TOOL_NAMES:
                raise OpError(ErrorCode.FORBIDDEN, f"this Manager no longer has {name}")
            raise OpError(ErrorCode.INVALID, f"no tool {name!r}; the tools are: {', '.join(scope.tools)}")
        if name in scope.read_only and tool.risk_of(arguments) is not Risk.READ:
            raise OpError(ErrorCode.FORBIDDEN, "this Manager can only read projects; ask the user to use the "
                                               "general Manager or the dashboard for that")
        task_id = tool.task_id(arguments)
        if scope.scoped and isinstance(task_id, str) and task_id:
            task = await db_tasks.get_task(task_id)
            if task is not None and task.get("manager_id") != scope.manager_id:
                raise OpError(ErrorCode.FORBIDDEN, f"task {task_id} belongs to another Manager; this Manager acts "
                                                   "only on the tasks it created")
        return tool

    def refuse(self, path: str) -> None:
        """The endpoint refused a request for its token: logged, and the turn running (if any) is marked."""
        logger.error("refused a request to %s: not the Manager's token; the Manager's tools do not work", path)
        self._working_at = None
        if self._turn is not None and not self._turn.autonomous:
            self._turn.refused = True

    async def tools_problem(self) -> str | None:
        """Why the Manager's tools cannot work now (its MCP server does not start, or Looper refuses it); None when
        they work."""
        import asyncio

        if self._working_at is not None and self._clock() - self._working_at < PROBE_KEPT_SECONDS:
            return None
        problem = await asyncio.to_thread(self._probe)
        if problem is None:
            self._working_at = self._clock()
        return problem

    def _probe(self) -> str | None:
        """Start the MCP entry's command as a runtime would, and list the tools through it."""
        config = self.server_config()
        requests = [{"jsonrpc": "2.0", "id": 1, "method": "initialize",
                     "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "looper"}}},
                    {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}]
        try:
            done = subprocess.run([config["command"], *config["args"]], env={**child_env.inherited(), **config["env"]},
                                  input="".join(json.dumps(r) + "\n" for r in requests), capture_output=True,
                                  text=True, timeout=PROBE_TIMEOUT_SECONDS, check=False)
        except (OSError, subprocess.SubprocessError) as exc:
            return f"looper mcp-server did not start: {exc}"
        for line in done.stdout.splitlines():
            try:
                answer = json.loads(line)
            except json.JSONDecodeError:
                continue
            if answer.get("id") == 2:
                if (answer.get("result") or {}).get("tools"):
                    return None
                return str((answer.get("error") or {}).get("message") or "it listed no tools")
        last = (done.stderr.strip().splitlines() or [""])[-1]
        return f"looper mcp-server exited ({done.returncode}) without listing the tools{': ' + last if last else ''}"

    async def call(self, name: str, raw: Any, *, binding: str | None = None,
                    call_id: str | None = None) -> dict[str, Any]:
        """Run one tool call; the protocol's answer.

        With stop enabled (``chat.stop_enabled``) the binding is enforced: the call runs in the turn its
        ``binding`` names (no binding, or one unknown, is Claude's own turn: it only reads), and a resident
        runtime's write call runs only when it matches a tool_use of that turn's stream. Off (the default), the
        binding only observes: the call runs in the turn running when it comes, as it did before turns were bound,
        and each disagreement is logged and counted (``binding_report``), never refused."""
        tool = self._scope.tools.get(name) or _MANAGER_TOOL_NAMES.get(name)
        if tool is None:
            from .planner import TOOLS as PLANNER_TOOLS

            if name in {t.name for t in PLANNER_TOOLS}:
                return _error(ErrorCode.FORBIDDEN, f"{name} is a tool of a task's Planner, for its own task; the "
                                                   f"Manager's tools are: {', '.join(self._scope.tools)}")
            return _error(ErrorCode.INVALID, f"no tool {name!r}; the tools are: {', '.join(self._scope.tools)}")
        enforced = chat_settings.stop_enabled()
        bound = self._bindings.get(binding) if binding else None
        self._observe_turn(name, binding, bound, enforced=enforced)
        turn = (bound if enforced else self._turn) or Turn(channel=None, chat=None)
        risk = tool.risk
        arguments: dict[str, Any] = raw if isinstance(raw, dict) else {}
        try:
            scope = await self.current_scope()  # once: this call is judged by it throughout
            tool = scope.tools.get(name, tool)  # its schema too: a template added since lists in the new one
            arguments = check_arguments(tool.schema, raw)
            risk = tool.risk_of(arguments)
            if tool.name != CONFIRM_TOOL:
                tool = await self._authorize(scope, name, arguments)
            _check_answering(turn)
            matched_turn = turn if enforced else bound
            if (matched_turn is not None and matched_turn.require_match and binding
                    and (risk is not Risk.READ or tool.name == CONFIRM_TOOL)):
                if not enforced:
                    self._observe_match(binding, tool.name, arguments, call_id, matched_turn)
                elif not await self._matched(binding, tool.name, arguments, call_id):
                    self._mismatch("not_this_turn", turn, tool.name, enforced=True)
                    raise OpError(ErrorCode.NOT_THIS_TURN, "this call was not made in the running turn; it did not run")
                _check_answering(turn)
            if tool.name == CONFIRM_TOOL:
                return await self._confirm(arguments["confirmation_id"], turn)
            if risk is not Risk.READ and turn.autonomous:
                raise OpError(ErrorCode.NEEDS_USER_TURN, "Looper changes things only in a turn that answers a user's "
                                                         "message; tell the user what you would do instead")
            call = Call(arguments, turn, self._platform, scope=scope)
            impact = await tool.escalate(call) if tool.escalate is not None else None
            _check_answering(turn)  # stopped while escalate read: nothing runs and the user is asked nothing
            if impact is not None:
                risk = Risk.HIGH
                pending = self._ask(tool, arguments, impact, turn)
                await self._record(turn, tool, arguments, risk, "needs_confirmation", confirmation_id=pending.id)
                return {"status": "needs_confirmation", "confirmation_id": pending.id,
                        "expires_in_seconds": CONFIRMATION_SECONDS,
                        "shown_to_user": "Looper adds what this does and the request to confirm it after your reply. "
                                         "It has not run: do not say it did."}
            result = _plain(await self._run_tool(tool, call))
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
        return {"status": "ok", "result": result}

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
            scope = await self.current_scope()
            try:
                tool = await self._authorize(scope, pending.tool.name, pending.arguments)
            except OpError:
                raise OpError(ErrorCode.FORBIDDEN, "the operation was not run: this Manager may no longer do "
                                                   "it") from None
            result = _plain(await self._run_tool(tool, Call(pending.arguments, turn, self._platform, scope=scope)))
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
        return {"status": "ok", "result": result}

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
        task_id = tool.task_id(arguments)
        turn.task_ids.add(task_id)
        task_id = task_id or _created_task(result)
        try:
            await db_actions.record(
                channel=turn.channel, chat=turn.chat, message_id=turn.message_id, tool=tool.name,
                arguments=arguments, risk=risk.value, status=status, error_code=error_code, result=result,
                confirmation_id=confirmation_id, task_id=task_id, manager_id=self._scope.manager_id,
                client_id=turn.client_id,
            )
        except Exception:
            logger.warning("could not record the Manager's call of %s", tool.name, exc_info=True)

    # --- The Manager's turns ---

    def register(self, turn: Turn, binding: str) -> None:
        """Calls that carry ``binding`` belong to ``turn``; the last ``BINDINGS_KEPT`` stay, ended turns too."""
        self._bindings[binding] = turn
        self._bindings.move_to_end(binding)
        while len(self._bindings) > BINDINGS_KEPT:
            old, _ = self._bindings.popitem(last=False)
            self._expected.pop(old, None)
            self._expected_event.pop(old, None)

    def expect(self, binding: str, tool: str, arguments: Mapping[str, Any], *, call_id: str | None = None) -> None:
        """The turn of ``binding`` streamed a tool_use of ``tool``: one call with these arguments may match it."""
        self._expected.setdefault(binding, []).append((call_id or None, tool, _canonical(arguments)))
        event = self._expected_event.get(binding)
        if event is not None:
            event.set()

    def _take(self, binding: str, tool: str, arguments: Mapping[str, Any], call_id: str | None) -> bool:
        entries = self._expected.get(binding, [])
        canon = _canonical(arguments)
        for i, (eid, etool, ecanon) in enumerate(entries):
            if (eid == call_id) if call_id else (etool == tool and ecanon == canon):
                del entries[i]
                return True
        return False

    async def _matched(self, binding: str, tool: str, arguments: Mapping[str, Any], call_id: str | None) -> bool:
        """Whether a tool_use of the turn matches this call (consumed), waiting up to ``MATCH_WAIT_SEC`` for it."""
        by = "id" if call_id else "arguments"
        if self._take(binding, tool, arguments, call_id):
            self._matches[(by, "at_arrival")] += 1
            return True
        deadline = self._clock() + MATCH_WAIT_SEC
        while True:
            remaining = deadline - self._clock()
            if remaining <= 0:
                matched = False
                break
            event = self._expected_event.setdefault(binding, asyncio.Event())
            event.clear()
            if self._take(binding, tool, arguments, call_id):
                matched = True
                break
            try:
                await asyncio.wait_for(event.wait(), remaining)
            except asyncio.TimeoutError:
                matched = self._take(binding, tool, arguments, call_id)
                break
            if self._take(binding, tool, arguments, call_id):
                matched = True
                break
        self._matches[(by, "waited" if matched else "missed")] += 1
        return matched

    # --- Observe-only binding (stop off) ---

    def _observe_turn(self, tool: str, binding: str | None, bound: Turn | None, *, enforced: bool) -> None:
        """Count a call whose binding names another turn than the one running when it came (which it runs in
        while the binding only observes)."""
        running = self._turn
        if binding is None:
            if running is not None:
                self._mismatch("no_binding", running, tool, enforced=enforced)
        elif bound is None:
            self._mismatch("unknown_binding", running, tool, enforced=enforced)
        elif bound is not running:
            self._mismatch("other_turn", bound, tool, enforced=enforced)

    def _observe_match(self, binding: str, tool: str, arguments: Mapping[str, Any], call_id: str | None,
                       turn: Turn) -> None:
        """Look for the call's tool_use as enforcement would (waiting for it), beside the call: it runs meanwhile."""
        async def look() -> None:
            if not await self._matched(binding, tool, arguments, call_id):
                self._mismatch("not_this_turn", turn, tool)

        task = asyncio.get_running_loop().create_task(look())
        self._observing.add(task)
        task.add_done_callback(self._observing.discard)

    def _mismatch(self, reason: str, turn: Turn | None, tool: str, *, enforced: bool = False) -> None:
        runtime = (turn.runtime if turn is not None else None) or "unknown"
        self._mismatches[(reason, runtime, tool)] += 1
        logger.warning(
            "turn binding mismatch (%s): reason=%s runtime=%s tool=%s message=%s total=%d",
            "enforced" if enforced else "observe-only, the call runs", reason, runtime, tool,
            turn.message_id if turn is not None else None, sum(self._mismatches.values()),
            extra={"binding_mismatch": {"reason": reason, "runtime": runtime, "tool": tool,
                                        "enforced": enforced}})

    def binding_report(self) -> dict[str, Any]:
        """Whether the binding is enforced, and the mismatches counted since the server started."""
        return {"enforced": chat_settings.stop_enabled(), "mismatches": sum(self._mismatches.values()),
                "by": [{"reason": reason, "runtime": runtime, "tool": tool, "count": count}
                       for (reason, runtime, tool), count in sorted(self._mismatches.items())],
                "matches": [{"by": by, "when": when, "count": count}
                            for (by, when), count in sorted(self._matches.items())]}

    def publish_binding(self, binding: str | None) -> None:
        """Write the binding file the MCP bridge reads at each call (synchronously and atomically: it must be on
        disk before the prompt reaches the runtime). ``None``: no turn runs."""
        if self._turn_file is None:
            return
        self._epoch += 1
        path = self._turn_file
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(json.dumps({"epoch": self._epoch, "turn": binding}))
            os.replace(tmp, path)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise

    def begin_turn(self, turn: Turn, binding: str | None = None) -> str:
        """``turn`` runs now; the note its message starts with when an operation in its chat waits for the user's
        word ('' otherwise). That operation may be confirmed in this turn only.

        A turn begun again after it ended is live again, and its calls run; one the user stopped stays stopped."""
        if turn.state is TurnState.ENDED:
            turn.state = TurnState.ANSWERING
        self._turn = turn
        if binding:
            self.register(turn, binding)
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

    def stop_turn(self, turn: Turn) -> None:
        """The user stopped ``turn``: its calls still under way run no further, and the confirmation it asked, if
        any, is dropped, with what its reply would have shown."""
        turn.state = TurnState.STOPPED
        for key, pending in list(self._pending.items()):
            # Asked in this turn, or waiting for this turn's word: a stopped turn confirms nothing.
            if (pending.asked_in == turn.message_id or pending.answer_turn == turn.message_id) \
                    and not turn.autonomous:
                del self._pending[key]
                if pending.block in turn.blocks:
                    turn.blocks.remove(pending.block)

    def end_turn(self, turn: Turn) -> None:
        """``turn`` is over: an operation it could have confirmed and did not lapses."""
        if turn.state is TurnState.ANSWERING:
            turn.state = TurnState.ENDED
        if self._turn is turn:
            self._turn = None
        pending = self._pending.get(turn.chat_key)
        if pending is not None and not turn.autonomous and pending.answer_turn == turn.message_id:
            del self._pending[turn.chat_key]

    def asks_in(self, message_id: str) -> bool:
        """Whether a confirmation asked in the turn of ``message_id`` waits."""
        return any(pending.asked_in == message_id for pending in self._pending.values())

    def forget_confirmations(self) -> None:
        """Drop every waiting confirmation (the Manager's session was reset)."""
        self._pending.clear()

    # --- The Manager's MCP entry ---

    def server_config(self) -> dict[str, Any]:
        config = server_config(self._url, self._token)
        if self._turn_file is not None:
            config["env"][TURN_FILE_ENV] = str(self._turn_file)
        if trace := os.environ.get(TRACE_ENV, "").strip():  # a server started to trace its bridge (an eval)
            config["env"][TRACE_ENV] = trace
        return config

    async def library_with_server(self, library: Any, runtime_root: Path) -> Any:
        """``library`` (``AgentLibrary``) with Looper's own MCP server added: always there for the Manager, and
        over a catalog server of the same name."""
        library = await library_with_server(library, runtime_root, self.server_config(), who="the Manager")
        if self._scope.manager_id != DEFAULT_MANAGER_ID:
            library = replace(library, manager_tools=tuple(self._scope.tools))
        return library


def server_config(url: str, token: str) -> dict[str, Any]:
    """Looper's MCP entry for an agent with ``token``: ``looper mcp-server``, reaching the server at ``url``."""
    return {"command": sys.executable, "args": ["-m", "looper.cli", "mcp-server"],
            "env": {URL_ENV: url, TOKEN_ENV: token}}


async def library_with_server(library: Any, runtime_root: Path, config: Mapping[str, Any], *, who: str) -> Any:
    """``library`` (``AgentLibrary``) with Looper's own MCP server ``config`` added (``server_config``), over a
    catalog server of the same name; ``who`` is the agent it is for, as a warning names it."""
    import asyncio

    from ..library import ItemKind, LoadedItem
    from ..mcp import write_config

    servers: dict[str, Any] = {}
    if library.mcp_config:
        servers = dict(json.loads(Path(library.mcp_config).read_text(encoding="utf-8")).get("mcpServers") or {})
    if SERVER_NAME in servers:
        logger.warning("the catalog's MCP server %r is replaced by Looper's own for %s", SERVER_NAME, who)
    servers[SERVER_NAME] = dict(config)
    path = await asyncio.to_thread(write_config, runtime_root, sorted(servers.items()))
    sha = hashlib.sha256(json.dumps(servers[SERVER_NAME], sort_keys=True).encode("utf-8")).hexdigest()
    items = tuple(item for item in library.items if not (item.kind is ItemKind.MCP and item.name == SERVER_NAME))
    own = LoadedItem(ItemKind.MCP, SERVER_NAME, sha, plugin="", scope="builtin")
    return replace(library, items=(*items, own), mcp_config=str(path))


class _Recorded(OpError):
    """An ``OpError`` of a confirmed call, already recorded."""

    def __init__(self, error: OpError) -> None:
        super().__init__(error.code, str(error))


def _check_answering(turn: Turn) -> None:
    """Refuse a call of a turn the user stopped, or one that is over."""
    if turn.state is TurnState.STOPPED:
        raise OpError(ErrorCode.STOPPED, "the user stopped this turn: this call did not run and asks the user "
                                         "nothing; stop here and make no more calls")
    if turn.state is TurnState.ENDED:
        raise OpError(ErrorCode.EXPIRED, "the turn this call came in is over: it did not run and asks the user "
                                         "nothing")


def tools_down_reply(problem: str) -> str:
    """What the user gets when the Manager's tools do not work: nothing was done, whatever Claude says."""
    return f"平台工具不可用，没有执行任何操作：{problem}。请检查 looper serve 的日志，或先在控制台里操作。"


def _canonical(arguments: Mapping[str, Any]) -> str:
    return json.dumps(arguments, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def _error(code: ErrorCode, message: str) -> dict[str, Any]:
    return {"status": "error", "error": {"code": code.value, "message": message}}


def _plain(value: Any) -> Any:
    """``value`` as JSON has it (dates as ISO text)."""
    return json.loads(json.dumps(value, ensure_ascii=False,
                                default=lambda obj: obj.isoformat() if isinstance(obj, date) else str(obj)))


def _created_task(result: Any) -> str | None:
    return result.get("task_id") if isinstance(result, Mapping) and isinstance(result.get("task_id"), str) else None
