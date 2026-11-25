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
  memory: a restart drops them, and the user is asked again; with card buttons
  on (``card_actions``) each is also a ``card_actions`` row, which a restart
  brings back (``restore_confirmations``), the reply carries it as a card with
  确认 / 取消 where the channel has buttons (``press``), and the typed
  confirmation and the button claim the same row, so it runs once.
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
from collections import Counter, OrderedDict, deque
from collections.abc import Callable, Mapping, Sequence
from contextlib import nullcontext
from dataclasses import dataclass, replace
from datetime import date, timedelta
from pathlib import Path
from types import MappingProxyType
from typing import Any, cast

from ... import child_env
from .. import card_actions, chat_settings, cost_manager, receipts, receipts_settings
from ...db import card_actions as db_cards
from ...db import ledger as db_ledger, manager_actions as db_actions, managers as db_managers, tasks as db_tasks, workflows as db_workflows
from ..managers_api import DEFAULT_MANAGER_ID, ManagerDefinition, ManagerTools, TaskPolicy, TaskScope
from ..message import BOT_CHANNEL_TYPES, MessageEnvelope, builtin_channel_id
from ..operator_identity import is_operator, operator_sender_id
from .. import session_routing, session_visibility
from ..reminders import reminders_on
from ..tiers.manager_session import MAIN_SESSION, on_session_replaced
from ..turn_context import SpeakerKind
from . import API_CHANNEL, Audience, Call, ErrorCode, OpError, RestrictedTool, Risk, Tool, Turn, TurnState, check_arguments
from .concurrency import TOOLS as CONCURRENCY_TOOLS
from . import intents_recognize
from . import ledger as ledger_tools
from . import reminders as reminder_tools
from . import skill_proposals as skill_proposal_tools
from . import memory_propose as memory_tools
from . import task_hold as task_hold_tools
from .config import TOOLS as CONFIG_TOOLS, project_tool
from .leads import LEADS_TOOL, TOOL as LEADS_SEARCH, leads_tool
from .manager_vault import TOOLS as VAULT_READ_TOOLS, manager_vault
from .schedules import TOOLS as SCHEDULE_TOOLS
from .session_search import TOOL as SESSION_SEARCH, enabled as session_search_on
from .settings import TOOLS as SETTINGS_TOOLS, engine_tool
from .tasks import TOOLS as TASK_TOOLS, task_create_tool, task_directory_tool, task_workflow_tool

logger = logging.getLogger(__name__)

# The MCP entry the Manager gets: its name (its tools are mcp__looper__<tool> in Claude Code) and environment.
SERVER_NAME = "looper"
URL_ENV = "LOOPER_AGENT_URL"
TOKEN_ENV = "LOOPER_AGENT_TOKEN"
TURN_FILE_ENV = "LOOPER_AGENT_TURN_FILE"
TRACE_ENV = "LOOPER_MCP_TRACE"  # the bridge's trace of each call's ``_meta`` keys (``mcp_server._trace_meta``)
BINDINGS_KEPT = 64
# The tools that put what a turn says into a task (``looper_task_update`` only with a new requirement): in a session
# that found leads, the task they write is marked (RFC section 6.3).
TAINTING_TOOLS = frozenset({"looper_task_create", "looper_task_instruct", "looper_task_gate_reply",
                            "looper_task_update"})
CALLS_KEPT = 4096  # the tool_use ids the hook can place in their turn
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
    confirmation: dict[str, Any]
    answer_turn: str | None = None  # the message of the turn that may confirm it, once it started
    card: str | None = None  # its ``card_actions`` token, while card buttons are on
    asker: Turn | None = None  # the turn that asked: a button runs the call as in it
    impact: str = ""  # what it does, as the block says it


# The Manager's tools (a task's Planner has its own, ``ops.planner``).
MANAGER_TOOLS: tuple[Tool, ...] = (*TASK_TOOLS, *CONCURRENCY_TOOLS, *CONFIG_TOOLS, *SETTINGS_TOOLS, *SCHEDULE_TOOLS,
                                   CONFIRM)


# What a Manager with ``tools = tasks`` has: task tools, projects (read only), agent schedules and confirmation, in
# MANAGER_TOOLS order.
TASKS_SCOPE_TOOLS: tuple[Tool, ...] = (*TASK_TOOLS, *(t for t in CONFIG_TOOLS if t.name == "looper_projects"),
                                    *SCHEDULE_TOOLS, CONFIRM)
READ_ONLY_TASKS = frozenset({"looper_projects"})
# Every tool a Manager may have had: a call of one it no longer has is told so. The leads search is the default
# Manager's while leads are on, the vault's reads while it lets the Manager read it, session search while it is on,
# the reminder tools while reminders are on, and recognition while its switches are on
# (``ManagerOps._with_operator_tools``).
_MANAGER_TOOL_NAMES = {tool.name: tool for tool in (*MANAGER_TOOLS, LEADS_SEARCH, *VAULT_READ_TOOLS, SESSION_SEARCH,
                                                    *reminder_tools.TOOLS, intents_recognize.TOOL,
                                                    skill_proposal_tools.TOOL, memory_tools.TOOL, task_hold_tools.TOOL)}
# The default Manager's tools that only the user's own turns run, each behind its own switch.
_OPERATOR_TOOLS = {tool.name: tool for tool in (LEADS_SEARCH, *VAULT_READ_TOOLS, SESSION_SEARCH, *reminder_tools.TOOLS,
                                              intents_recognize.TOOL, task_hold_tools.TOOL)}
_LEDGER_TOOLS = {tool.name: tool for tool in ledger_tools.TOOLS}


@dataclass(frozen=True)
class OpsScope:
    """What a Manager's calls may do, from its definition (RFC §7): the tools it has, those among them it may only
    read, which tasks it may act on and list, and its task policy. ``revision`` is the definition's
    ``managers.revision`` it was computed from (-1: never compared with the database). The two settings are
    independent: ``tools`` picks the tools, ``task_scope`` sets the task filters."""

    manager_id: str
    revision: int
    tools: Mapping[str, Tool]
    read_only: frozenset[str]
    tasks: db_tasks.TaskFilter
    listed: db_tasks.TaskFilter
    task_policy: TaskPolicy
    tool_scope: ManagerTools = ManagerTools.ALL
    template_keys: tuple[str, ...] = ()

    @property
    def scoped(self) -> bool:
        return self.tasks.manager_id is not None


def _tool_map(tools: Sequence[Tool]) -> Mapping[str, Tool]:
    return MappingProxyType({tool.name: tool for tool in tools})


def scope_for(definition: ManagerDefinition, template_keys: Sequence[str] = (),
              workflow_keys: Sequence[str] = ()) -> OpsScope:
    """The scope a Manager definition gives; with templates, ``looper_task_create`` takes ``template`` and
    ``inputs``."""
    all_tools = definition.tools is ManagerTools.ALL
    tools = tuple(task_create_tool(template_keys, workflow_keys) if tool.name == "looper_task_create" else tool
                  for tool in (MANAGER_TOOLS if all_tools else TASKS_SCOPE_TOOLS))
    return OpsScope(
        manager_id=definition.id, revision=definition.revision,
        tools=_tool_map(tools),
        read_only=frozenset() if all_tools else READ_ONLY_TASKS,
        tasks=definition.tool_tasks(), listed=definition.listed_tasks(), task_policy=definition.task_policy,
        tool_scope=definition.tools,
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
        self._session_tokens: dict[str, str] = {MAIN_SESSION: self._token}
        # One attribute holds everything a call is judged by; ``set_scope`` replaces it whole.
        self._scope = scope if scope is not None else (
            replace(DEFAULT_SCOPE, tools=_tool_map(tools)) if tools is not None else DEFAULT_SCOPE)
        self._clock = clock
        self._turn: Turn | None = None
        self._turn_file = turn_file
        self._epoch = 0
        self._bindings: OrderedDict[str, Turn] = OrderedDict()  # the last BINDINGS_KEPT turns, ended ones too
        self._binding_sessions: dict[str, str] = {}
        self._published_bindings: dict[str, str | None] = {}
        self._session_turns: dict[str, Turn] = {}
        self._routed_turns: dict[int, Turn] = {}  # enforcement survives a switch-off until end_turn
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
        self._lapsing: set[asyncio.Task[None]] = set()
        # The confirmations a card's button answered (id -> what it did), so a typed confirmation of one is told so.
        self._pressed: OrderedDict[str, str] = OrderedDict()
        # What a card button did, per chat, for the chat's next user turn (``_note_button``).
        # Per chat: (who pressed, session, that session's generation, the switch's generation, the note).
        self._button_notes: OrderedDict[tuple[str | None, str | None, str | None],
                                        list[tuple[str | None, str, int, int, str]]] = OrderedDict()
        on_session_replaced(self.forget_session)  # every reset path clears a session's id (``ManagerSession``)
        self._session_gen: dict[str, int] = {}  # bumped when a session is reset: its older notes are never told
        self._told: dict[int, tuple[str, str, int, int, list[str]]] = {}  # per turn: token, session, gens, notes
        self._chat_sessions: dict[tuple[str | None, str | None, str | None], str] = {}  # the session a chat runs in
        # The outbound review's hook (``outbound_hook``): where its reviews run, the files the session wrote last (a
        # later shell command may run one), and the turns already told the operator of a block.
        from ..outbound_hook import Trusted

        self._review_root: Path | None = None
        self._review_bin = "claude"
        self._trusted: dict[str, Trusted] = {}  # each resident trusts only the MCP servers it loaded
        self._writes: dict[str, deque[Any]] = {}
        self._notified: OrderedDict[tuple[str | None, str | None, str | None], None] = OrderedDict()
        # Each tool_use a turn's stream showed, by its id: the binding of the turn it belongs to (a subagent's call,
        # its parent call's turn), which places the hook's call in its own turn.
        self._call_turns: OrderedDict[str, str] = OrderedDict()
        self._leads_session: str | None = None  # the session that found leads ('' when none has; None: not read)
        self._call_seen = asyncio.Event()

    @property
    def executing(self) -> bool:
        """Admitted executions, independent of the HTTP connection or the lifetime of their turn."""
        return self._executing > 0

    async def _run_tool(self, tool: Tool, call: Call, watch: receipts.Watch | None = None) -> Any:
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
            if watch is not None and receipts_settings.enabled():
                return await receipts.run_watched(tool, call, watch)
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
        """Whether an ``Authorization`` header carries one of this Manager's session credentials."""
        return self.session_for(header) is not None

    def session_for(self, header: str | None) -> str | None:
        """The session a server-issued credential grants; callers cannot choose it in their arguments."""
        scheme, _, value = (header or "").partition(" ")
        if scheme.lower() != "bearer":
            return None
        return next((key for key, token in self._session_tokens.items()
                     if hmac.compare_digest(value.strip().encode(), token.encode())), None)

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
        if "looper_task_create" in scope.tools:
            keys = [row["key"] for row in await db_workflows.list_workflows()]
            if self._scope is not scope:
                return await self.current_scope()
            scope = replace(scope, tools=_tool_map([
                task_workflow_tool(tool, keys) if tool.name == "looper_task_create" else tool
                for tool in scope.tools.values()]))
            self._scope = scope
        scope = await self._with_ledger_tools(scope)
        scope = await self._with_operator_tools(scope)
        scope = await self._with_skill_proposal_tool(scope)
        scope = await self._with_memory_tool(scope)
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
        current = scope_for(definition, keys or (), [row["key"] for row in await db_workflows.list_workflows()])
        current = await self._with_skill_proposal_tool(current)
        if self._scope is scope:  # not replaced meanwhile by the fleet
            self.set_scope(current)
        return current

    async def _with_ledger_tools(self, scope: OpsScope) -> OpsScope:
        """The default Manager has the ledger's two tools exactly while ``ledger.enabled`` is on; no other Manager
        does. Off, the scope is returned as it is, so its tool list is what it was before the ledger existed."""
        if scope.manager_id != DEFAULT_MANAGER_ID or scope.tool_scope is not ManagerTools.ALL:
            return scope
        try:
            wanted = await db_ledger.enabled()
        except Exception:  # no database to read the switch from: the ledger counts as off
            logger.debug("could not read ledger.enabled", exc_info=True)
            wanted = False
        if wanted == (ledger_tools.TOOL_NAMES[0] in scope.tools):
            return scope
        kept = [tool for tool in scope.tools.values() if tool.name not in _LEDGER_TOOLS]
        adjusted = replace(scope, tools=_tool_map([*kept, *(ledger_tools.TOOLS if wanted else ())]))
        if self._scope is scope:
            self._scope = adjusted
        return adjusted

    async def _with_operator_tools(self, scope: OpsScope) -> OpsScope:
        """``scope`` with the default Manager's operator-only tools exactly while their switches are on:
        ``looper_leads_search`` while ``leads.enabled`` is, as ``leads.search_mode`` describes it
        (``ops.leads.leads_tool``), the vault's reads while the vault setting holds and says ``manager_read``,
        ``looper_session_search`` while ``manager.session_search.enabled`` is; the reminder tools while
        ``reminders.enabled`` is; recognition while both intents and on-request recognition are enabled.
        Any other Manager's as it is; off, the
        default Manager's is what it was before them."""
        if scope.manager_id != DEFAULT_MANAGER_ID:
            return scope
        from ..failures import FailureTyping, typing_mode
        act = await typing_mode() is FailureTyping.ACT
        chosen_settings = engine_tool() if act else SETTINGS_TOOLS[0]
        if "looper_settings" in scope.tools and scope.tools["looper_settings"] is not chosen_settings:
            scope = replace(scope, tools=_tool_map([chosen_settings if tool.name == "looper_settings" else tool
                                                  for tool in scope.tools.values()]))
        leads = await leads_tool()
        wanted = [*((leads,) if leads is not None else ()),
                  *(VAULT_READ_TOOLS if await manager_vault() is not None else ()),
                  *((SESSION_SEARCH,) if await session_search_on() else ()),
                  *(reminder_tools.TOOLS if await reminders_on() else ()),
                  *((intents_recognize.TOOL,) if await intents_recognize.enabled() else ()),
                  *((task_hold_tools.TOOL,) if act and scope.tool_scope is ManagerTools.ALL else ())]
        current = [tool for tool in scope.tools.values() if tool.name in _OPERATOR_TOOLS]
        if len(wanted) == len(current) and all(new is old for new, old in zip(wanted, current)):
            return scope
        kept = [tool for tool in scope.tools.values() if tool.name not in _OPERATOR_TOOLS]
        adjusted = replace(scope, tools=_tool_map([*kept, *wanted]))
        if self._scope is scope:
            self._scope = adjusted
        return adjusted

    async def _with_skill_proposal_tool(self, scope: OpsScope) -> OpsScope:
        """Any Manager may propose within its project scope, while one of those projects opts in."""
        wanted = scope.tool_scope is ManagerTools.ALL and await skill_proposal_tools.enabled(scope.tasks.project_ids)
        if wanted == (skill_proposal_tools.TOOL_NAME in scope.tools):
            return scope
        kept = [tool for tool in scope.tools.values() if tool.name != skill_proposal_tools.TOOL_NAME]
        adjusted = replace(scope, tools=_tool_map([*kept, *((skill_proposal_tools.TOOL,) if wanted else ())]))
        if self._scope is scope:
            self._scope = adjusted
        return adjusted

    async def _with_memory_tool(self, scope: OpsScope) -> OpsScope:
        if scope.manager_id != DEFAULT_MANAGER_ID:
            return scope
        wanted = await memory_tools.memory_tool()
        if wanted is scope.tools.get(memory_tools.TOOL_NAME):
            return scope
        kept = [tool for tool in scope.tools.values() if tool.name != memory_tools.TOOL_NAME]
        adjusted = replace(scope, tools=_tool_map([*kept, *((wanted,) if wanted is not None else ())]))
        if self._scope is scope:
            self._scope = adjusted
        return adjusted

    def listing(self) -> list[dict[str, Any]]:
        return [task_directory_tool(project_tool(tool)).listing() for tool in self._scope.tools.values()
                if _audience(tool) is Audience.ANY]

    async def current_listing(self, turn: Turn | None = None, *, session_key: str | None = None) -> list[dict[str, Any]]:
        """The tools the Manager has now (``current_scope``), as ``tools/list`` names them, for ``turn`` (by default
        the one running, as MCP's ``tools/list`` and the API runtime ask): a tool only the user's own turns run is
        listed only in such a turn, never with no turn running."""
        turn = (turn or self._turn) if session_key is None else (
            self._session_turns.get(session_key) or Turn(channel=None, chat=None))
        key = session_key if session_key is not None else self._session_of(turn)
        if (policy := session_visibility.for_session(key)) is not None:
            # Filter before reading any global schemas (templates, workflows, settings).
            return [tool.listing() for name, tool in self._scope.tools.items() if name in policy.tools]
        tools = list((await self.current_scope()).tools.values())
        if any(_audience(tool) is Audience.OPERATOR for tool in tools) and not (
                turn is not None and await _operator_turn(turn)):
            tools = [tool for tool in tools if _audience(tool) is Audience.ANY]
        return [task_directory_tool(project_tool(tool)).listing() for tool in tools]

    def _session_of(self, turn: Turn | None) -> str:
        return next((key for key, active in self._session_turns.items() if active is turn), MAIN_SESSION)

    async def _authorize(self, scope: OpsScope, name: str, arguments: Mapping[str, Any]) -> Tool:
        """The one check of what ``scope`` allows: the tool is among its tools, a read-only tool is only read, and
        a bounded Manager acts only on a task its filter admits. Returns the tool to run."""
        tool = scope.tools.get(name)
        if tool is None:
            if name in _MANAGER_TOOL_NAMES:
                raise OpError(ErrorCode.FORBIDDEN, f"this Manager no longer has {name}")
            raise OpError(ErrorCode.INVALID, f"no tool {name!r}; the tools are: {', '.join(scope.tools)}")
        if name in scope.read_only and tool.risk_of(arguments) is not Risk.READ:
            raise OpError(ErrorCode.FORBIDDEN, "this Manager can only read projects; ask the user to use the "
                                               "general Manager or the dashboard for that")
        task_id = tool.task_id(arguments)
        if scope.tasks.bounded and isinstance(task_id, str) and task_id:
            task = await db_tasks.get_task(task_id)
            if task is not None and not scope.tasks.admits(task):
                if scope.tasks.project_ids is not None:
                    raise OpError(ErrorCode.FORBIDDEN, f"task {task_id} is in a project outside this Manager's "
                                                       "projects; it acts only on the tasks of the projects the "
                                                       "Projects line lists")
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
                    call_id: str | None = None, session_key: str | None = None) -> dict[str, Any]:
        """Run one tool call; the protocol's answer.

        With stop enabled (``chat.stop_enabled``), or session routing on or still draining routed turns for this
        Manager, the binding is enforced: the call runs in the turn its
        ``binding`` names (no binding, or one unknown, is Claude's own turn: it only reads), and a resident
        runtime's write call runs only when it matches a tool_use of that turn's stream. Off (the default), the
        binding only observes: the call runs in the turn running when it comes, as it did before turns were bound,
        and each disagreement is logged and counted (``binding_report``), never refused. An authenticated session
        can only use its own bindings, in either mode."""
        bound = self._bindings.get(binding) if binding else None
        if bound is not None and session_key is not None and self._binding_sessions.get(binding) != session_key:
            self._mismatch("other_session", bound, name, enforced=True)
            return _error(ErrorCode.FORBIDDEN, "this turn belongs to another session; the call did not run")
        key = session_key if session_key is not None else self._binding_sessions.get(
            binding or "", self._session_of(bound or self._turn))
        if (policy := session_visibility.for_session(key)) is not None and name not in policy.tools:
            # Also deny guessed, unknown and stale tool names, without revealing global
            # schemas, validation errors or return values. Authenticated sessions cannot
            # acquire main's access by omitting their binding.
            return _error(ErrorCode.FORBIDDEN, "this session has no access to that tool")
        tool = self._scope.tools.get(name) or _MANAGER_TOOL_NAMES.get(name) or _LEDGER_TOOLS.get(name)
        if tool is None:
            from .planner import TOOLS as PLANNER_TOOLS

            if name in {t.name for t in PLANNER_TOOLS}:
                return _error(ErrorCode.FORBIDDEN, f"{name} is a tool of a task's Planner, for its own task; the "
                                                   f"Manager's tools are: {', '.join(self._scope.tools)}")
            return _error(ErrorCode.INVALID, f"no tool {name!r}; the tools are: {', '.join(self._scope.tools)}")
        # With session routing on, sessions run in parallel: a call belongs only to the turn its binding names.
        enforced = (chat_settings.stop_enabled() or session_routing.routes(self._scope.manager_id)
                    or bool(self._routed_turns) or session_key not in (None, MAIN_SESSION))
        bound = self._bindings.get(binding) if binding else None
        self._observe_turn(name, binding, bound, enforced=enforced)
        turn = (bound if enforced else self._turn) or Turn(channel=None, chat=None)
        if _audience(tool) is Audience.OPERATOR:
            # Only the turn the call is bound to, whatever the stop switch: no binding, or one Looper did not give, is
            # Claude's own turn; a turn that ended refuses it (below).
            turn = bound or Turn(channel=None, chat=None)
        risk = tool.risk
        arguments: dict[str, Any] = raw if isinstance(raw, dict) else {}
        watch = receipts.Watch()
        try:
            scope = await self.current_scope()  # once: this call is judged by it throughout
            tool = scope.tools.get(name, tool)  # its schema too: a template added since lists in the new one
            arguments = check_arguments(tool.schema, raw)
            risk = tool.risk_of(arguments)
            if tool.name != CONFIRM_TOOL:
                tool = await self._authorize(scope, name, arguments)
            if tool.name == skill_proposal_tools.TOOL_NAME:
                if turn.autonomous:
                    raise OpError(ErrorCode.NEEDS_USER_TURN, "a skill proposal needs the operator's own turn")
                if not await _operator_turn(turn):
                    raise _OperatorOnly(turn)
            if tool.name == memory_tools.TOOL_NAME and turn.autonomous:
                raise OpError(ErrorCode.NEEDS_USER_TURN, "a memory proposal needs the user's own turn")
            if _audience(tool) is Audience.OPERATOR and not await _operator_turn(turn):
                raise OpError(ErrorCode.FORBIDDEN, cast(RestrictedTool, tool).refusal)
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
                if turn.speaker in (SpeakerKind.ALLOWED, SpeakerKind.MEMBER):
                    self._lapse(self._pending.pop(turn.chat_key, None))
                    raise _OperatorOnly(turn)
                return await self._confirm(arguments["confirmation_id"], turn)
            if risk is not Risk.READ and turn.autonomous:
                raise OpError(ErrorCode.NEEDS_USER_TURN, "Looper changes things only in a turn that answers a user's "
                                                         "message; tell the user what you would do instead")
            if risk is not Risk.READ and (blocked := await self._outbound_check(tool, arguments, turn, risk)):
                return blocked
            call = Call(arguments, turn, self._platform, scope=scope)
            impact = await receipts.escalate(tool, call, watch)
            _check_answering(turn)  # stopped while escalate read: nothing runs and the user is asked nothing
            if impact is not None:
                risk = Risk.HIGH
                if turn.speaker in (SpeakerKind.ALLOWED, SpeakerKind.MEMBER):
                    raise _OperatorOnly(turn)
                pending = self._ask(tool, arguments, impact, turn, confirmation=call.confirmation)
                await self._card(pending, turn)
                action_id = await self._record(turn, tool, arguments, risk, "needs_confirmation",
                                               confirmation_id=pending.id)
                await db_ledger.register_confirmation(action_id)
                return {"status": "needs_confirmation", "confirmation_id": pending.id,
                        "expires_in_seconds": CONFIRMATION_SECONDS,
                        "shown_to_user": "Looper adds what this does and the request to confirm it after your reply. "
                                         "It has not run: do not say it did."}
            answer = await self._run_tool(tool, call, watch)
            result = _plain(answer)
            if tool.name == LEADS_TOOL and isinstance(result, Mapping) and result.get("leads"):
                # What the outbound review of a later call in this turn sees came from the leads; and the session
                # that found them marks the tasks it creates or adds to (RFC section 6.3).
                turn.outside.append(("leads", json.dumps(result["leads"], ensure_ascii=False)))
                await self._found_leads()
            elif tool.name in TAINTING_TOOLS and (tool.name != "looper_task_update" or "requirement" in arguments):
                await self._mark_task(tool.task_id(arguments) or _created_task(result))
            audited = isinstance(tool, RestrictedTool) and tool.audit_result is not None
            audit = tool.audit_result(answer) if audited else None  # type: ignore[union-attr]
        except _Recorded as exc:
            return _error(exc.code, str(exc), exc.shown)
        except OpError as exc:
            await self._record(turn, tool, arguments, risk, "error", error_code=exc.code.value,
                               result={"reason": "operator_only"} if isinstance(exc, _OperatorOnly)
                               else {"message": str(exc)}, watch=watch)
            return _error(exc.code, str(exc), watch.shown)
        except Exception as exc:
            logger.exception("the Manager's call of %s failed", name)
            await self._record(turn, tool, arguments, risk, "error", error_code=ErrorCode.UNAVAILABLE.value,
                               result={"message": f"{type(exc).__name__}: {exc}"}, watch=watch)
            return _error(ErrorCode.UNAVAILABLE, f"Looper could not do it now ({type(exc).__name__}: {exc}); tell "
                                                 "the user, and try again later", watch.shown)
        action_id = await self._record(turn, tool, arguments, risk, "ok",
                           result=audit if audited else None if risk is Risk.READ else result, watch=watch)
        if tool.name == memory_tools.TOOL_NAME:  # the candidate source names this call's audit row
            try:
                result = await memory_tools.commit(call, answer, action_id)
            except OpError as exc:
                return _error(exc.code, str(exc), watch.shown)
        if watch.shown is not None and isinstance(result, dict):
            result.setdefault("shown_to_user", watch.shown)  # after the record: the trail keeps the tool's own result
        return {"status": "ok", "result": result}

    async def _confirm(self, confirmation_id: str, turn: Turn) -> dict[str, Any]:
        if turn.autonomous:
            raise OpError(ErrorCode.NEEDS_USER_TURN, "only the user's own message confirms an operation")
        pending = self._pending.get(turn.chat_key)
        if pending is None or pending.id != confirmation_id:
            if confirmation_id in self._pressed:
                raise OpError(ErrorCode.CONFLICT, f"the user already answered confirmation {confirmation_id} with "
                                                  "its card's buttons; it does not run again")
            raise OpError(ErrorCode.NOT_FOUND, f"no operation waits for confirmation {confirmation_id} in this chat; "
                                               "run the tool again to ask the user")
        if pending.answer_turn != turn.message_id or self._clock() - pending.at > CONFIRMATION_SECONDS:
            del self._pending[turn.chat_key]
            self._lapse(pending)
            raise OpError(ErrorCode.EXPIRED, f"confirmation {confirmation_id} lapsed: it counts only in the turn "
                                             "answering the user's very next message, within "
                                             f"{CONFIRMATION_SECONDS // 60} minutes; run the tool again to ask again")
        if pending.card is None:
            del self._pending[turn.chat_key]
        else:
            # Claim its card before it leaves memory: a press meanwhile still finds it waiting, so it loses the claim
            # instead of taking the confirmation for lapsed (then neither would run it).
            claimed = await db_cards.claim(pending.card, by=turn.sender, via="typed", choice="confirm",
                                           outcome="running")
            if self._pending.get(turn.chat_key) is pending:
                del self._pending[turn.chat_key]
            if not claimed:
                raise OpError(ErrorCode.CONFLICT, f"the user already answered confirmation {confirmation_id} with "
                                                  "its card's buttons; it does not run again")
        watch = receipts.Watch()
        try:
            result = await self._run_confirmed(pending, turn, watch)
        except _Recorded:
            await self._finish_card(pending, "error")
            raise
        await self._finish_card(pending, "ok")
        if watch.shown is not None and isinstance(result, dict):
            result.setdefault("shown_to_user", watch.shown)
        return {"status": "ok", "result": result}

    async def _run_confirmed(self, pending: _Pending, turn: Turn, watch: receipts.Watch | None = None) -> Any:
        """Run the confirmed call in ``turn`` and record it; its result, or ``_Recorded``."""
        watch = watch or receipts.Watch()
        tool = pending.tool
        try:
            scope = await self.current_scope()
            try:
                tool = await self._authorize(scope, pending.tool.name, pending.arguments)
            except OpError:
                raise OpError(ErrorCode.FORBIDDEN, "the operation was not run: this Manager may no longer do "
                                                   "it") from None
            result = _plain(await self._run_tool(tool, Call(pending.arguments, turn, self._platform, scope=scope,
                                                          confirmation=pending.confirmation), watch))
        except Exception as exc:
            if isinstance(exc, OpError):
                error = exc
            else:
                logger.exception("the Manager's confirmed call of %s failed", tool.name)
                error = OpError(ErrorCode.UNAVAILABLE, f"Looper could not do it now ({type(exc).__name__}: {exc}); "
                                                       "tell the user, and ask again later")
            if receipts_settings.enabled() and watch.receipt is None and not isinstance(error, _OperatorOnly):
                receipts.record_refusal(watch, turn, tool.name, pending.arguments, error)
            await self._record(turn, tool, pending.arguments, Risk.HIGH, "error", error_code=error.code.value,
                               result={"message": str(error)}, confirmation_id=pending.id, watch=watch)
            raise _Recorded(error, watch.shown) from None
        if tool.name in TAINTING_TOOLS and (tool.name != "looper_task_update" or "requirement" in pending.arguments):
            await self._mark_task(tool.task_id(pending.arguments) or _created_task(result))
        await self._record(turn, tool, pending.arguments, Risk.HIGH, "ok", result=result, confirmation_id=pending.id,
                           watch=watch)
        return result

    # --- Confirmation cards (``card_actions``) ---

    async def _card(self, pending: _Pending, turn: Turn) -> None:
        """With card buttons on: keep the confirmation ``card_actions`` row, and add its card, with 确认 and 取消,
        to the reply when the chat's channel has buttons. A call with a secret-looking argument keeps no arguments
        there (``manager_actions.masked``), so a restart does not bring it back. A failure only logs: the user
        confirms by typing as before."""
        try:
            if not await card_actions.enabled():
                return
            token = card_actions.new_token()
            buttons = bool(turn.sender) and card_actions.has_buttons(turn.channel_id)
            card = card_actions.Card(pending.block, (
                card_actions.Button("确认", card_actions.value(token, "confirm"), "primary"),
                card_actions.Button("取消", card_actions.value(token, "cancel"))) if buttons else ())
            payload: dict[str, Any] = {"chat_key": list(turn.chat_key), "tool": pending.tool.name,
                                       "block": pending.block, "impact": pending.impact, "asked_in": pending.asked_in,
                                       "turn": _turn_record(turn), "card": card.to_dict()}
            if (db_actions.masked(pending.arguments) == pending.arguments
                    and db_actions.masked(pending.confirmation) == pending.confirmation):
                payload.update(arguments=pending.arguments, confirmation=pending.confirmation)
            await db_cards.create(token=token, kind="confirmation", subject=pending.id, digest=_digest(pending),
                                  payload=payload, manager_id=self._scope.manager_id, channel_id=turn.channel_id,
                                  chat=turn.chat, allowed=[turn.sender] if turn.sender else [],
                                  ttl=timedelta(seconds=CONFIRMATION_SECONDS))
        except Exception:
            logger.warning("could not keep the card of confirmation %s; it is confirmed by typing", pending.id,
                           exc_info=True)
            return
        pending.card = token
        if self._pending.get(turn.chat_key) is not pending:  # the turn was stopped meanwhile
            self._lapse(pending)
        elif buttons:
            turn.cards.append(card.to_dict())

    def _lapse(self, pending: _Pending | None) -> None:
        """``pending`` no longer waits: its card says so to a later press, and a restart does not bring it back."""
        if pending is None or pending.card is None:
            return
        try:
            task = asyncio.get_running_loop().create_task(self._write_lapse(pending.card))
        except RuntimeError:
            return
        self._lapsing.add(task)
        task.add_done_callback(self._lapsing.discard)

    @staticmethod
    async def _write_lapse(token: str) -> None:
        try:
            await db_cards.lapse([token])
        except Exception:
            logger.warning("could not mark a confirmation's card as lapsed", exc_info=True)

    @staticmethod
    async def _finish_card(pending: _Pending, outcome: str) -> None:
        if pending.card is None:
            return
        try:
            await db_cards.finish(pending.card, outcome)
        except Exception:
            logger.warning("could not record what a confirmation's card came to", exc_info=True)

    async def press(self, row: dict[str, Any], event: card_actions.Press, choice: str) -> card_actions.Answer:
        """A press of a confirmation card's button, its presser and time already checked (``card_actions.press``):
        ``confirm`` runs the waiting call as the typed confirmation would, in a turn like the one that asked;
        ``cancel`` drops it. Either holds only while that confirmation still waits, with the same arguments."""
        payload = row["payload"]
        card = card_actions.Card.from_dict(payload["card"])
        key = tuple(payload["chat_key"])
        pending = self._pending.get(key)  # type: ignore[arg-type]
        if pending is None or pending.card != row["token"] or _digest(pending) != row["digest"]:
            await db_cards.lapse([row["token"]])
            return card_actions.Answer(card_actions.HANDLED, card=card.settled(card_actions.HANDLED))
        confirm = choice == "confirm"
        if not await db_cards.claim(row["token"], by=event.presser, via="button", choice=choice,
                                    outcome="running" if confirm else "cancelled"):
            return card_actions.Answer(card_actions.HANDLED, card=card.settled(card_actions.HANDLED))
        if self._pending.get(key) is pending:  # type: ignore[arg-type]
            del self._pending[key]  # type: ignore[arg-type]
        self._pressed[pending.id] = choice
        while len(self._pressed) > CONFIRMATION_CHATS:
            self._pressed.popitem(last=False)
        if not confirm:
            self._note_button(key, event.presser, pending.id, "it was cancelled; it did not run")  # type: ignore[arg-type]
            return card_actions.Answer("已取消", card=card.settled("已取消，没有执行。"))
        asker = pending.asker or Turn(channel=key[0], chat=key[2], channel_id=key[1])
        turn = Turn(channel=asker.channel, chat=asker.chat, message_id=pending.asked_in or f"card:{row['token']}",
                    sender=event.presser, group=asker.group, envelope=asker.envelope, channel_id=asker.channel_id,
                    client_id=asker.client_id, client_name=asker.client_name)
        return card_actions.Answer("已确认，正在执行", "success", card=card.settled("⏳ 已确认，正在执行…"),
                                   later=self._run_pressed(pending, turn, card))

    async def _run_pressed(self, pending: _Pending, turn: Turn, card: card_actions.Card) -> card_actions.Card:
        watch = receipts.Watch()
        try:
            await self._run_confirmed(pending, turn, watch)
        except _Recorded as exc:
            await self._finish_card(pending, "error")
            self._note_button(turn.chat_key, turn.sender, pending.id, f"it failed ({receipts.reason(exc.code)})")
            if turn.blocks:  # receipts on: the failed-write line code generated
                return card.settled("\n\n".join(turn.blocks))
            return card.settled(f"❌ 没有执行：{exc}")
        await self._finish_card(pending, "ok")
        self._note_button(turn.chat_key, turn.sender, pending.id,
                          f"it ran ({receipts.render(watch.receipt)})" if watch.receipt is not None else "it ran")
        return card.settled("\n\n".join(("✅ 已执行。", *turn.blocks)))

    async def restore_confirmations(self) -> int:
        """With card buttons on, bring back the confirmations a restart dropped: each card of this Manager that
        still waits and kept its call's arguments (one that did not lapses). How many came back."""
        if not await card_actions.enabled():
            return 0
        restored = 0
        for row in await db_cards.open_confirmations(self._scope.manager_id):
            payload = row["payload"]
            key = tuple(payload.get("chat_key") or ())
            name = payload.get("tool")
            tool = self._scope.tools.get(name) or _MANAGER_TOOL_NAMES.get(name) or _LEDGER_TOOLS.get(name)
            if tool is None or "arguments" not in payload or len(key) != 3:
                await db_cards.lapse([row["token"]])
                continue
            self._lapse(self._pending.get(key))  # type: ignore[arg-type]
            self._pending[key] = _Pending(  # type: ignore[index]
                id=row["subject"], tool=tool, arguments=dict(payload["arguments"]), block=str(payload["block"]),
                asked_in=payload.get("asked_in"), at=self._clock() - float(row["age"]),
                confirmation=dict(payload.get("confirmation") or {}), card=row["token"],
                asker=_turn_from(payload.get("turn") or {}), impact=str(payload.get("impact") or payload["block"]))
            restored += 1
        return restored

    def _ask(self, tool: Tool, arguments: dict[str, Any], impact: str, turn: Turn, *,
             confirmation: dict[str, Any]) -> _Pending:
        """Register ``tool``'s call for the user to confirm, replacing the chat's earlier one, and show it."""
        earlier = self._pending.pop(turn.chat_key, None)
        self._lapse(earlier)
        if earlier is not None and earlier.asked_in == turn.message_id and earlier.block in turn.blocks:
            turn.blocks.remove(earlier.block)  # asked in this turn too: only the newest stands
            turn.cards[:] = [card for card in turn.cards if card.get("text") != earlier.block]
        block = f"需要你确认：\n{impact}\n回复「确认」就执行；回复别的就不执行。"
        pending = _Pending(id=f"c-{uuid.uuid4().hex[:12]}", tool=tool, arguments=arguments, block=block,
                           asked_in=turn.message_id, at=self._clock(), confirmation=dict(confirmation),
                           asker=turn, impact=impact)
        self._pending[turn.chat_key] = pending
        while len(self._pending) > CONFIRMATION_CHATS:
            self._lapse(self._pending.pop(next(iter(self._pending))))
        turn.blocks.append(block)
        return pending

    async def _record(self, turn: Turn, tool: Tool, arguments: Mapping[str, Any], risk: Risk, status: str, *,
                      error_code: str | None = None, result: Any = None,
                      confirmation_id: str | None = None, watch: receipts.Watch | None = None) -> int | None:
        """Record the call; its row's id (None when it could not be written)."""
        task_id = tool.task_id(arguments)
        turn.task_ids.add(task_id)
        task_id = task_id or _created_task(result)
        try:
            action_id = await db_actions.record(
                channel=turn.channel, chat=turn.chat, message_id=turn.message_id, tool=tool.name,
                arguments=arguments, risk=risk.value, status=status, error_code=error_code, result=result,
                confirmation_id=confirmation_id, task_id=task_id, manager_id=self._scope.manager_id,
                client_id=turn.client_id, **receipts.row_fields(watch),
            )
            if watch is not None and watch.receipt is not None:
                watch.receipt = replace(watch.receipt, action_id=action_id)
            return action_id
        except Exception:
            logger.warning("could not record the Manager's call of %s", tool.name, exc_info=True)
            return None

    # --- The Manager's turns ---

    def register(self, turn: Turn, binding: str, *, session_key: str = MAIN_SESSION) -> None:
        """Calls that carry ``binding`` belong to ``turn``; the last ``BINDINGS_KEPT`` stay, ended turns too."""
        self._bindings[binding] = turn
        self._binding_sessions[binding] = session_key
        self._bindings.move_to_end(binding)
        while len(self._bindings) > BINDINGS_KEPT:
            old, _ = self._bindings.popitem(last=False)
            self._binding_sessions.pop(old, None)
            self._expected.pop(old, None)
            self._expected_event.pop(old, None)

    def expect(self, binding: str, tool: str, arguments: Mapping[str, Any], *, call_id: str | None = None,
               parent_call_id: str | None = None) -> None:
        """The turn of ``binding`` streamed a tool_use of ``tool``: one call with these arguments may match it. A
        subagent's (``parent_call_id``, the call that started it) belongs to its parent call's turn, for the hook."""
        self._expected.setdefault(binding, []).append((call_id or None, tool, _canonical(arguments)))
        event = self._expected_event.get(binding)
        if event is not None:
            event.set()
        if call_id:
            owner = self._call_turns.get(parent_call_id) if parent_call_id else binding
            if owner is not None:  # a subagent whose start Looper did not see belongs to no turn
                self._call_turns[call_id] = owner
                while len(self._call_turns) > CALLS_KEPT:
                    self._call_turns.popitem(last=False)
                self._call_seen.set()

    async def _turn_of_call(self, call_id: Any) -> Turn | None:
        """The turn whose stream showed the tool_use ``call_id``, waiting up to ``PLACE_WAIT_SEC`` for it; None when
        none did, or that turn is over (its call is no one's)."""
        from ..outbound_hook import PLACE_WAIT_SEC

        if not isinstance(call_id, str) or not call_id:
            return None
        deadline = self._clock() + PLACE_WAIT_SEC
        while (binding := self._call_turns.get(call_id)) is None:
            remaining = deadline - self._clock()
            if remaining <= 0:
                return None
            self._call_seen.clear()
            try:
                await asyncio.wait_for(self._call_seen.wait(), remaining)
            except asyncio.TimeoutError:
                pass
        turn = self._bindings.get(binding)
        return turn if turn is not None and turn.state is TurnState.ANSWERING else None

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
        return {"enforced": bool(chat_settings.stop_enabled() or session_routing.routes(self._scope.manager_id)
                                  or self._routed_turns), "mismatches": sum(self._mismatches.values()),
                "by": [{"reason": reason, "runtime": runtime, "tool": tool, "count": count}
                       for (reason, runtime, tool), count in sorted(self._mismatches.items())],
                "matches": [{"by": by, "when": when, "count": count}
                            for (by, when), count in sorted(self._matches.items())]}

    def turn_file_for(self, session_key: str) -> Path | None:
        """The binding file of ``session_key``. Main keeps the supplied path; another session keeps its file in
        its own runtime root under ``manager-sessions/<digest>``. None without a ``turn_file``."""
        if not session_key:
            raise ValueError("a Manager session needs a key")
        if session_key == MAIN_SESSION or self._turn_file is None:
            return self._turn_file
        return (self._turn_file.parent / "manager-sessions" / session_routing.session_digest(self._scope.manager_id, session_key)
                / ".local" / "manager-turn.json")

    def publish_binding(self, binding: str | None, *, session_key: str = MAIN_SESSION) -> None:
        """Write the binding file of ``session_key`` (``turn_file_for``) the MCP bridge reads at each call
        (synchronously and atomically: it must be on disk before the prompt reaches the runtime). ``None``: no turn
        runs."""
        if binding is not None and (turn := self._bindings.get(binding)) is not None:
            self._binding_sessions[binding] = session_key
            self._session_turns[session_key] = turn
            if session_key != MAIN_SESSION:
                self._routed_turns[id(turn)] = turn
        path = self.turn_file_for(session_key)
        if path is None:
            self._published_bindings[session_key] = binding
            return
        self._epoch += 1
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(json.dumps({"epoch": self._epoch, "turn": binding}))
            os.replace(tmp, path)
            self._published_bindings[session_key] = binding
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise

    def clear_binding(self, binding: str, *, session_key: str = MAIN_SESSION) -> None:
        """Clear only this turn's published binding; a later turn may already own the bridge."""
        if self._published_bindings.get(session_key) == binding:
            self.publish_binding(None, session_key=session_key)

    def begin_turn(self, turn: Turn, binding: str | None = None, *, session_key: str = MAIN_SESSION) -> str:
        """``turn`` runs now; the note its message starts with (``_begin_turn``), then, in a user turn, what the
        operator's card buttons did in this chat since its last one (``_note_button``)."""
        note = self._begin_turn(turn, binding, session_key=session_key)
        if session_visibility.for_session(session_key) is not None:
            return ""
        return "\n".join(part for part in (note, *self._take_button_notes(turn, session_key)) if part)

    def _take_button_notes(self, turn: Turn, session_key: str) -> list[str]:
        """The notes this chat's card buttons left, for the user turn of the person who pressed, in the session
        they were left in (RFC 6.5), once. Never while receipts are off or after they were turned off since (stale
        ones are dropped), in a background turn, for another speaker, or after the session was reset."""
        if not turn.autonomous:
            self._chat_sessions[turn.chat_key] = session_key
            while len(self._chat_sessions) > CONFIRMATION_CHATS:
                del self._chat_sessions[next(iter(self._chat_sessions))]
        if not receipts_settings.enabled():
            self._button_notes.clear()
            return []
        live = [entry for entry in self._button_notes.get(turn.chat_key, [])
                if entry[3] == receipts_settings.generation() and entry[2] == self._session_gen.get(entry[1], 0)]
        if turn.autonomous or turn.speaker in (SpeakerKind.ALLOWED, SpeakerKind.MEMBER):
            return []
        mine = [entry for entry in live if entry[0] == turn.sender and entry[1] == session_key]
        rest = [entry for entry in live if entry not in mine]
        if rest:
            self._button_notes[turn.chat_key] = rest
        else:
            self._button_notes.pop(turn.chat_key, None)
        if not mine:
            return []
        # The notes are not put into the input here, but a token only code can name stands where they go; the
        # input as it is sent (``live_note``) has them put in, if their session still stands, or the token out.
        earlier = self._told.get(id(turn))
        token = earlier[0] if earlier is not None else f"[[looper-button-notes:{uuid.uuid4().hex}]]"
        texts = [*(earlier[4] if earlier is not None else []), *(entry[4] for entry in mine)]
        gens = earlier[1:4] if earlier is not None else (
            session_key, self._session_gen.get(session_key, 0), receipts_settings.generation())
        self._told[id(turn)] = (token, *gens, texts)
        return [token]

    def live_note(self, turn: Turn, text: str) -> str:
        """``text``, the input about to be sent, with the card-button notes of this turn put where ``begin_turn``
        left their token, if their session was not reset and the switch was not turned off since; else with the
        token taken out. Nothing else in ``text`` is touched, whatever it says."""
        told = self._told.get(id(turn))
        if told is None or told[0] not in text:
            return text
        token, session_key, session_gen, switch_gen, texts = told
        if session_gen == self._session_gen.get(session_key, 0) and switch_gen == receipts_settings.generation() \
                and receipts_settings.enabled():
            return text.replace(token, "\n".join(texts), 1)
        return text.replace(token + "\n", "", 1) if token + "\n" in text else text.replace(token, "", 1)

    def forget_session(self, session_key: str) -> None:
        """The Manager session ``session_key`` was reset: the card-button notes left in it are never told."""
        self._session_gen[session_key] = self._session_gen.get(session_key, 0) + 1
        for chat, entries in list(self._button_notes.items()):
            kept = [entry for entry in entries if entry[1] != session_key]
            if kept:
                self._button_notes[chat] = kept
            else:
                del self._button_notes[chat]

    def _note_button(self, key: tuple[str | None, str | None, str | None], sender: str | None,
                     confirmation_id: str, what: str) -> None:
        """Tell this chat's next user turn what a card button did to a confirmation (RFC 6.5): at most 3, newest
        kept, for the person who pressed. Nothing while receipts are off."""
        if not receipts_settings.enabled():
            return
        note = (f"[Looper] After your last reply, the operator answered confirmation {confirmation_id} with its "
                f"card: {what}.")
        session = self._chat_sessions.get(key, MAIN_SESSION)
        entries = [entry for entry in self._button_notes.pop(key, [])
                   if entry[3] == receipts_settings.generation() and entry[2] == self._session_gen.get(entry[1], 0)]
        entries.append((sender, session, self._session_gen.get(session, 0), receipts_settings.generation(), note))
        self._button_notes[key] = entries[-3:]
        while len(self._button_notes) > CONFIRMATION_CHATS:
            del self._button_notes[next(iter(self._button_notes))]

    def _begin_turn(self, turn: Turn, binding: str | None = None, *, session_key: str = MAIN_SESSION) -> str:
        """``turn`` runs now; the note its message starts with when an operation in its chat waits for the user's
        word ('' otherwise). That operation may be confirmed in this turn only.

        A turn begun again after it ended is live again, and its calls run; one the user stopped stays stopped."""
        cost_manager.begin_turn(turn)
        if turn.state is TurnState.ENDED:
            turn.state = TurnState.ANSWERING
        self._turn = turn
        self._session_turns[session_key] = turn
        if session_routing.routes(self._scope.manager_id) or session_key != MAIN_SESSION:
            self._routed_turns[id(turn)] = turn
        if binding:
            self.register(turn, binding, session_key=session_key)
        if session_visibility.for_session(session_key) is not None:
            return ""
        pending = self._pending.get(turn.chat_key)
        if turn.autonomous or pending is None:
            return ""
        if pending.answer_turn is not None or self._clock() - pending.at > CONFIRMATION_SECONDS:
            del self._pending[turn.chat_key]
            self._lapse(pending)
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
                self._lapse(pending)
                if pending.block in turn.blocks:
                    turn.blocks.remove(pending.block)

    def end_turn(self, turn: Turn) -> None:
        """``turn`` is over: an operation it could have confirmed and did not lapses."""
        cost_manager.end_turn(turn)
        if turn.state is TurnState.ANSWERING:
            turn.state = TurnState.ENDED
        if self._turn is turn:
            self._turn = None
        self._routed_turns.pop(id(turn), None)
        self._told.pop(id(turn), None)
        for key, active in list(self._session_turns.items()):
            if active is turn:
                del self._session_turns[key]
        pending = self._pending.get(turn.chat_key)
        if pending is not None and not turn.autonomous and pending.answer_turn == turn.message_id:
            del self._pending[turn.chat_key]
            self._lapse(pending)

    def has_confirmation(self, chat_key: tuple[str | None, str | None, str | None]) -> bool:
        """A chat with pending confirmation needs its own user turn, including while it answers one."""
        return chat_key in self._pending

    def asks_in(self, message_id: str) -> bool:
        """Whether a confirmation asked in the turn of ``message_id`` waits."""
        return any(pending.asked_in == message_id for pending in self._pending.values())

    def waiting_confirmations(self, *, unpersisted_only: bool = False) -> list[dict[str, Any]]:
        """The operations that wait for the user's word now, oldest first: asked, no turn answering them yet, and
        within ``CONFIRMATION_SECONDS``. Read only (the "needs you" summary, ``agents.attention``)."""
        now = self._clock()
        waiting = sorted(((key, pending) for key, pending in self._pending.items()
                          if pending.answer_turn is None and now - pending.at <= CONFIRMATION_SECONDS
                          and (not unpersisted_only or pending.card is None)),
                         key=lambda item: item[1].at)
        return [{"confirmation_id": pending.id, "tool": pending.tool.name, "impact": pending.impact,
                 "channel": channel, "channel_id": channel_id, "chat": chat, "waited_seconds": int(now - pending.at),
                 "expires_in_seconds": max(0, int(CONFIRMATION_SECONDS - (now - pending.at)))}
                for (channel, channel_id, chat), pending in waiting]

    def forget_confirmations(self) -> None:
        """Drop every waiting confirmation (the Manager's session was reset), and the card-button notes with them."""
        self._button_notes.clear()
        for pending in self._pending.values():
            self._lapse(pending)
        self._pending.clear()

    # --- Tasks a session that found leads wrote (the outbound-review RFC, section 6.3) ---

    def _session(self) -> str | None:
        return getattr(self._platform.manager, "current_session_id", None)

    async def _found_leads(self) -> None:
        """A leads search of this turn found leads: the Manager's session is marked, and so are the tasks it creates
        or adds to from now on, until a new session starts. A mark that cannot be written fails the search: its
        leads are not handed on unmarked."""
        from ...db import agent_sessions

        if (session := self._session()) is None:
            return
        try:
            await agent_sessions.set_leads_session(self._scope.manager_id, session)
        except Exception as exc:
            logger.warning("cannot mark %s's session as having found leads", self._scope.manager_id, exc_info=True)
            raise OpError(ErrorCode.UNAVAILABLE, "Looper could not record that this session found leads, so it does "
                                                 "not show them; tell the user, and try again later") from exc
        self._leads_session = session

    async def _mark_task(self, task_id: str | None) -> None:
        """Mark ``task_id`` as having taken leads when this session found some. A mark that cannot be written fails
        the call (the change itself was made): the task's runs would go unchecked."""
        from ...db import agent_sessions

        if task_id is None or (session := self._session()) is None:
            return
        try:
            if self._leads_session is None:
                self._leads_session = await agent_sessions.leads_session(self._scope.manager_id) or ""
            if self._leads_session == session:
                await db_tasks.mark_untrusted(task_id, "leads")
        except Exception as exc:
            logger.warning("cannot mark task %s as having taken leads", task_id, exc_info=True)
            raise OpError(ErrorCode.UNAVAILABLE, f"the change to task {task_id} was made, but Looper could not record "
                                                 "that leads went into it, so its runs are not checked: tell the "
                                                 "user") from exc

    # --- The outbound review's hook ---

    def tool_hook(self, runtime_root: Path, claude_bin: str, library: Any = None,
                  *, session_key: str = MAIN_SESSION) -> Any:
        """The PreToolUse hook (``outbound_hook.ToolHook``) for this Manager's Claude Code, its config written; the
        reviews it asks for run under ``runtime_root`` with ``claude_bin``. Its MCP trust set takes effect only
        when the Manager activates this hook for the process it actually starts."""
        from .. import outbound_hook

        self._review_root, self._review_bin = runtime_root, claude_bin
        token = self.server_config(session_key=session_key)["env"][TOKEN_ENV]
        label = self._scope.manager_id if session_key == MAIN_SESSION else (
            f"{self._scope.manager_id}.{session_routing.key_digest(session_key)}")
        return replace(outbound_hook.write_hook(runtime_root, label, url=self._url, token=token),
                       trusted=outbound_hook.Trusted.of(library, (*self._scope.tools, CONFIRM_TOOL)))

    def activate_tool_hook(self, hook: Any, *, session_key: str = MAIN_SESSION) -> None:
        """Use the immutable trust set of the actual process's hook, never a prepared capability refresh."""
        from ..outbound_hook import Trusted

        self._trusted[session_key] = (hook.trusted if hook is not None and hook.trusted is not None
                                      else Trusted.of(None, ()))

    async def check_outbound(self, payload: Any, *, session_key: str = MAIN_SESSION) -> dict[str, Any]:
        """The hook's answer for one tool call of this Manager (``outbound_hook.answer``), in the turn it belongs to:
        the one whose stream showed its tool_use, never the turn that runs when it comes; anything that goes wrong
        blocks the call."""
        from .. import outbound_hook

        data = payload if isinstance(payload, Mapping) else {}
        turn: Turn | None = None

        async def place() -> Any:
            nonlocal turn
            turn = await self._turn_of_call(data.get("tool_use_id"))
            binding = self._call_turns.get(data.get("tool_use_id"))
            if turn is None or turn.autonomous or self._binding_sessions.get(binding) != session_key:
                turn = None
                return outbound_hook.NO_ONES
            return outbound_hook.Placed(await _who(turn), _message(turn), turn.channel or "", turn.chat,
                                        tuple(turn.outside))

        writes = self._writes.setdefault(session_key, deque(maxlen=outbound_hook.CONTENT_TOOLS_KEPT))
        trusted = self._trusted.get(session_key, outbound_hook.Trusted.of(None, ()))
        try:
            answer, review = await outbound_hook.answer(
                payload, agent_id=self._scope.manager_id, place=place, writes=list(writes),
                runtime_root=self._review_root or self._platform.runtime_root or Path.cwd(),
                claude_bin=self._review_bin, trusted=trusted)
        except Exception:
            logger.exception("the outbound check of a call of %s failed", self._scope.manager_id)
            return {"decision": "block", "message": outbound_hook.BLOCKED_MESSAGE.format(
                reason="the check could not run")}
        if answer["decision"] == "allow":
            if (write := outbound_hook.write_of(payload)) is not None:
                writes.append(write)
        elif review is not None:
            tool_input = data.get("tool_input") if isinstance(data.get("tool_input"), Mapping) else {}
            await self._notify_once(turn, str(data.get("tool_name") or ""),
                                    str(tool_input.get("command") or tool_input.get("file_path") or ""), review)
        return answer

    async def _notify_once(self, turn: Turn | None, tool: str, target: str, review: Any) -> None:
        """Tell the operator a call was blocked, once a turn (``outbound_hook.notify_blocked``)."""
        from .. import outbound_hook

        key = (turn.channel, turn.chat, turn.message_id) if turn is not None else (None, None, None)
        if key in self._notified:
            return
        self._notified[key] = None
        while len(self._notified) > BINDINGS_KEPT:
            self._notified.popitem(last=False)
        await outbound_hook.notify_blocked(self._scope.manager_id, tool, target, review.reason, review.review_id)

    async def _outbound_check(self, tool: Tool, arguments: Mapping[str, Any], turn: Turn,
                              risk: Risk) -> dict[str, Any] | None:
        """The ``blocked`` answer when the outbound review stops this call of Looper's own tool (``ops.review``);
        None when it may go on: the review is off, the call is not ``external``, or the review allowed it."""
        from .. import outbound, outbound_hook
        from ...db import outbound_reviews
        from . import levels, review as layer_one

        if not outbound_hook.known_enabled():  # read at startup and by each turn, set by its switch
            return None
        level = await levels.level_of(tool, arguments, turn)
        if level not in outbound.REVIEWED:
            return None
        action = await layer_one.action_of(tool.name, arguments, level)
        checked = await outbound.review(
            action, outbound.Asker(await _who(turn), turn.channel or "", _message(turn)),
            outbound.Origin(self._scope.manager_id, outbound.Source.TOOL, channel=turn.channel, chat=turn.chat),
            context=list(turn.outside), runtime_root=self._review_root or self._platform.runtime_root or Path.cwd(),
            claude_bin=self._review_bin)
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
        await self._notify_once(turn, tool.name, action.target, checked)
        return _error(ErrorCode.BLOCKED, message)

    # --- The Manager's MCP entry ---

    def server_config(self, *, session_key: str = MAIN_SESSION) -> dict[str, Any]:
        """Looper's MCP entry for the runtime of the Manager session ``session_key``, naming its binding file."""
        token = self._session_tokens.get(session_key)
        if token is None:
            token = self._session_tokens[session_key] = secrets.token_urlsafe(32)
        config = server_config(self._url, token)
        if (turn_file := self.turn_file_for(session_key)) is not None:
            config["env"][TURN_FILE_ENV] = str(turn_file)
        if trace := os.environ.get(TRACE_ENV, "").strip():  # a server started to trace its bridge (an eval)
            config["env"][TRACE_ENV] = trace
        return config

    async def library_with_server(self, library: Any, runtime_root: Path, *, session_key: str = MAIN_SESSION) -> Any:
        """``library`` (``AgentLibrary``) with Looper's own MCP server for the Manager session ``session_key``
        added: always there for the Manager, and over a catalog server of the same name."""
        library = await library_with_server(library, runtime_root, self.server_config(session_key=session_key),
                                            who="the Manager")
        if self._scope.manager_id != DEFAULT_MANAGER_ID:
            library = replace(library, manager_tools=tuple(self._scope.tools))
        return library


def server_config(url: str, token: str, *, reconnect_seconds: int | None = None) -> dict[str, Any]:
    """Looper's MCP entry for an agent with ``token``: ``looper mcp-server``, reaching the server at ``url``."""
    env = {URL_ENV: url, TOKEN_ENV: token}
    if reconnect_seconds is not None:
        env["LOOPER_AGENT_RECONNECT_SECONDS"] = str(reconnect_seconds)
    return {"command": sys.executable, "args": ["-m", "looper.cli", "mcp-server"], "env": env}


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

    def __init__(self, error: OpError, shown: str | None = None) -> None:
        super().__init__(error.code, str(error))
        self.shown = shown  # the ``shown_to_user`` text of its receipt, if it got one


class _OperatorOnly(OpError):
    """A speaker refusal with a typed audit reason, without changing other forbidden errors."""

    operator_only = True  # no receipt: the model tells the speaker (``receipts.escalate``)

    def __init__(self, turn: Turn) -> None:
        message = ("only the operator can authorize this: it was not run and nothing waits for confirmation; "
                   "tell the speaker that the operator has to ask for it themselves")
        if turn.operator_id_missing:
            message += " (this bot has no operator id set)"
        super().__init__(ErrorCode.FORBIDDEN, message)


def _audience(tool: Tool) -> Audience:
    return tool.audience if isinstance(tool, RestrictedTool) else Audience.ANY


async def _who(turn: Turn) -> Any:
    """Whose turn it is, as the outbound review reads it (``outbound.Who``): by code, never by what the model says."""
    from ..outbound import Who

    if turn.autonomous:
        return Who.AUTONOMOUS
    if await _operator_turn(turn):
        return Who.OPERATOR
    return Who.GROUP_MEMBER if turn.group else Who.ALLOWLISTED_PERSON


def _message(turn: Turn) -> str:
    return str((turn.envelope.payload.get("text") if turn.envelope is not None else "") or "")


async def operator_call(turn: Turn) -> bool:
    """Whether a call is the operator's own: in their own turn (``_operator_turn``), or through the API with the
    operator's token (no client)."""
    if turn.channel == API_CHANNEL:
        return turn.client_id is None
    return await _operator_turn(turn)


async def _operator_turn(turn: Turn) -> bool:
    """Whether ``turn`` is the user's own (``operator_identity.is_operator``); a turn Claude runs on its own (a
    completion turn too) never is."""
    if turn.autonomous:
        return False
    operator_id = None
    if turn.channel in BOT_CHANNEL_TYPES:
        operator_id = await operator_sender_id(turn.channel_id or builtin_channel_id(turn.channel))
    return is_operator(channel=turn.channel, group=turn.group, sender=turn.sender, client_id=turn.client_id,
                       operator_id=operator_id)


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


def _digest(pending: _Pending) -> str:
    """What a confirmation card is bound to: the call and its arguments, as they were asked."""
    return card_actions.digest([pending.tool.name, pending.arguments, pending.confirmation])


def _turn_record(turn: Turn) -> dict[str, Any]:
    """What a confirmation's row keeps of the turn that asked, so a button runs the call as in it after a restart."""
    return {"channel": turn.channel, "chat": turn.chat, "channel_id": turn.channel_id, "sender": turn.sender,
            "group": turn.group, "client_id": turn.client_id, "client_name": turn.client_name,
            "envelope": turn.envelope.to_dict() if turn.envelope is not None else None}


def _turn_from(record: Mapping[str, Any]) -> Turn:
    envelope = record.get("envelope")
    return Turn(channel=record.get("channel"), chat=record.get("chat"), channel_id=record.get("channel_id"),
                sender=record.get("sender"), group=bool(record.get("group")), client_id=record.get("client_id"),
                client_name=record.get("client_name"),
                envelope=MessageEnvelope.from_dict(envelope) if isinstance(envelope, dict) else None)


def _canonical(arguments: Mapping[str, Any]) -> str:
    return json.dumps(arguments, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def _error(code: ErrorCode, message: str, shown: str | None = None) -> dict[str, Any]:
    answer: dict[str, Any] = {"status": "error", "error": {"code": code.value, "message": message}}
    if shown is not None:
        answer["shown_to_user"] = shown
    return answer


def _plain(value: Any) -> Any:
    """``value`` as JSON has it (dates as ISO text)."""
    return json.loads(json.dumps(value, ensure_ascii=False,
                                default=lambda obj: obj.isoformat() if isinstance(obj, date) else str(obj)))


def _created_task(result: Any) -> str | None:
    return result.get("task_id") if isinstance(result, Mapping) and isinstance(result.get("task_id"), str) else None
