"""The Manager's platform operations: one catalog, a risk tier each, typed errors (Manager tools RFC, sections 6-7).

The Manager reaches Looper through MCP tools (``looper mcp-server``, a thin
forwarder); each tool is one entry of this catalog: its name, what it does,
the JSON Schema of its arguments, how risky a call is, and the code that runs
it. ``service.ManagerOps`` checks a call against the schema, its tier and the
turn it belongs to, asks the user first when it is ``high``, runs it and
keeps the audit trail. The MCP tool list is generated from the catalog.

- **Tiers.** ``read`` runs at once; ``write`` runs only in a turn that answers
  a user's message (never in a turn Claude runs on its own); ``high`` is
  registered and shown to the user, and runs only when the user's next
  message confirms it (``looper_confirm``).
- **Answers.** ``{"status": "ok", "result": ...}``,
  ``{"status": "needs_confirmation", "confirmation_id", "expires_in_seconds"}``
  or ``{"status": "error", "error": {"code", "message"}}``; the codes are
  ``ErrorCode``, the message says what to change.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any, TypedDict

from ..message import AgentAddress, MessageEnvelope, manager_address

if TYPE_CHECKING:
    from ..turn_context import SpeakerKind
    from ..delivery_guard import DeliveryDebt
    from .service import OpsScope


# The channel of a call that came through ``/api/v1`` (``ops.clients``).
API_CHANNEL = "api"


class Risk(str, Enum):
    READ = "read"
    WRITE = "write"
    HIGH = "high"


class Audience(str, Enum):
    """Whose turns may run a tool: any turn, or only the user's own (``operator_identity.is_operator``: the web
    chat without an API client, or the direct chat of the bot's operator)."""

    ANY = "any"
    OPERATOR = "operator"


class RetrySafety(str, Enum):
    """Whether a possibly executed call can be repeated, independently of its permission tier."""

    NOT_RETRYABLE = "not_retryable"
    READ_ONLY = "read_only"
    READ_ONLY_IF_PEEK = "read_only_if_peek"
    IDEMPOTENT = "idempotent"  # the same bridged call id returns the existing effect


class ErrorCode(str, Enum):
    INTERNAL = "internal"  # a stored exchange cannot be reconstructed completely
    NOT_FOUND = "not_found"
    INVALID = "invalid"
    NEEDS_USER_TURN = "needs_user_turn"  # a change outside a turn that answers the user
    FORBIDDEN = "forbidden"
    UNAUTHORIZED = "unauthorized"  # /api/v1 without a valid client token (the Manager never sees it)
    CONFLICT = "conflict"  # the target's state does not allow it (an ended task, a task not started)
    EXPIRED = "expired"  # a confirmation past its time or its turn
    STOPPED = "stopped"  # the user stopped the turn the call came in
    NOT_THIS_TURN = "not_this_turn"  # a write call no tool_use of the running turn's stream matches
    UNAVAILABLE = "unavailable"  # Looper could not do it now (its database); the call may be tried again
    RATE_LIMITED = "rate_limited"  # over a limit of how often or how much (a room's); the message says when
    BLOCKED = "blocked"  # the outbound review stopped it (``ops.review``): it did not run


class ErrorDetails(TypedDict, total=False):
    manager_id: str
    state: str
    phase: str | None
    method: str | None


class OpError(Exception):
    """A call Looper refuses or could not run, with the code the answer carries."""

    def __init__(self, code: ErrorCode, message: str, *, details: ErrorDetails | None = None,
                 field: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.details = details
        self.field = field


class PublicOpError(OpError):
    """A domain refusal safe to show even when the operation accepts credentials.

    Only domain code that has checked the message's source may use this type;
    parser errors and arbitrary service exceptions must remain ``OpError``.
    """


class TurnState(str, Enum):
    """Where a Manager turn is: answering, stopped by the user, or over. Only an answering turn's calls run."""

    ANSWERING = "answering"
    STOPPED = "stopped"
    ENDED = "ended"


class TurnOrigin(str, Enum):
    """Why a Manager turn runs: a user's message (``user``), Claude on its own (``own``), or Looper telling the chat
    that tasks ended (``completion``), that something may need them (``idle_check``) or that important messages
    arrived (``triage``). Only a user's turn changes anything."""

    USER = "user"
    OWN = "own"
    COMPLETION = "completion"
    IDLE_CHECK = "idle_check"
    TRIAGE = "triage"


@dataclass
class Turn:
    """The Manager turn calls belong to: the message it answers and its chat, one Claude runs on its own, or a
    completion turn (``origin``).

    ``blocks`` is what Looper itself adds to the turn's reply, after Claude's
    words: text code wrote (a task's configuration, what a confirmation is
    about), which the user sees as written (RFC decision 7).
    """

    channel: str | None
    chat: str | None
    message_id: str | None = None  # the user's message it answers; None for a turn Claude runs on its own
    sender: str | None = None  # who sent it (Lark's open_id, Telegram's user id; the web chat names none)
    group: bool = False  # the chat is a group chat
    envelope: MessageEnvelope | None = None
    blocks: list[str] = field(default_factory=list)
    # The cards with buttons Looper sends with the reply (``card_actions.Card.to_dict``); each is one of ``blocks``.
    cards: list[dict[str, Any]] = field(default_factory=list)
    # Created in this turn: their ready configuration blocks can change after a successful start.
    created_task_blocks: dict[str, str] = field(default_factory=dict)
    # What this turn's calls created (a schedule's id): the user sees it only after this turn's reply.
    created: set[str] = field(default_factory=set)
    # Schedules whose delete this turn refused while their command ran: disabling one asks the user first.
    refused_deletes: set[str] = field(default_factory=set)
    task_ids: set[str | None] = field(default_factory=set)  # each call's named task; None for global calls or creates
    # Looper refused a call of this turn for its token: the tools did not work, whatever Claude says.
    refused: bool = False
    # Set by the Manager when the user stops the turn and by ``end_turn``: a call of it still under way runs no
    # further, and asks the user nothing.
    state: TurnState = TurnState.ANSWERING
    # A resident runtime's turns share one bridge: a write call runs only when it matches a tool_use of this turn.
    require_match: bool = False
    runtime: str | None = None  # the runtime that answers it (its type), once the Manager knows it
    # The API client the turn is for (``/api/v1``, or the Manager answering a client's chat message); None for none.
    client_id: str | None = None
    client_name: str | None = None
    channel_id: str | None = None  # the bot the message came through (``channel-telegram-2``)
    # Why the turn runs; by default a user's when it answers a message, else Claude's own.
    origin: TurnOrigin | None = None
    # Set only with per-turn context enabled; None preserves the existing authorization behavior.
    speaker: SpeakerKind | None = None
    operator_id_missing: bool = False
    # Initialized explicitly by delivery-guard wiring; absent when disabled.
    debt: DeliveryDebt | None = None
    # The outside content its calls brought in (source, text), as the outbound review reads it: the leads it found.
    outside: list[tuple[str, str]] = field(default_factory=list)
    # The receipts whose lines are among ``blocks`` (``agents.receipts``); empty while receipts are off.
    receipts: list[Any] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.origin is None:
            self.origin = TurnOrigin.USER if self.message_id is not None else TurnOrigin.OWN

    @property
    def autonomous(self) -> bool:
        """It answers no user's message: its calls only read (a change answers ``needs_user_turn``)."""
        return self.origin is not TurnOrigin.USER

    @property
    def chat_key(self) -> tuple[str | None, str | None, str | None]:
        """The chat a confirmation waits in: its channel, the bot it came through, and the chat. Two bots of one
        Manager talking to one user are two chats; a turn naming no bot (the web chat) is keyed by channel alone."""
        return self.channel, self.channel_id, self.chat


@dataclass(frozen=True)
class Call:
    """One call of a tool: its checked arguments, the turn it came in, and the platform it acts on."""

    arguments: Mapping[str, Any]
    turn: Turn
    platform: Any  # service.Platform: the bus and the agents a tool acts through
    # The scope the call was judged by (``ManagerOps`` sets it); a Call built without one (a Planner's tool, a test)
    # is the default Manager's: unscoped.
    scope: OpsScope = None  # type: ignore[assignment]
    # Trusted state captured while composing a confirmation, carried to its execution; never tool arguments.
    confirmation: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.scope is None:
            from .service import DEFAULT_SCOPE

            object.__setattr__(self, "scope", DEFAULT_SCOPE)

    @property
    def manager_id(self) -> str:
        return self.scope.manager_id

    @property
    def manager_address(self) -> AgentAddress:
        return manager_address(self.manager_id)

    @property
    def scoped(self) -> bool:
        """The Manager sees and acts on its own tasks only."""
        return self.scope.scoped


@dataclass(frozen=True)
class Tool:
    """One catalog entry: an MCP tool of the Manager.

    ``risk`` is the tier of its calls, but for those whose ``action`` is one of
    ``read_actions``, which only read; ``escalate``, when given, makes one call
    ``high``: it returns what the user is asked to confirm (code's words), or
    None for a call of the tool's own tier. It may refuse the call
    (``OpError``) before anything is asked.
    """

    name: str
    description: str
    schema: Mapping[str, Any]  # its arguments, a JSON Schema object (the subset ``check_arguments`` reads)
    risk: Risk
    run: Callable[[Call], Awaitable[dict[str, Any]]]
    escalate: Callable[[Call], Awaitable[str | None]] | None = None
    # The task a call acts on, for the audit trail and the task page; None for none.
    task_id: Callable[[Mapping[str, Any]], str | None] = lambda arguments: None
    read_actions: frozenset[str] = frozenset()

    def risk_of(self, arguments: Mapping[str, Any]) -> Risk:
        """The tier of a call with these (checked) arguments, before ``escalate``."""
        return Risk.READ if arguments.get("action") in self.read_actions else self.risk

    def listing(self) -> dict[str, Any]:
        """The tool as MCP's ``tools/list`` names it."""
        return {"name": self.name, "description": self.description, "inputSchema": dict(self.schema)}


@dataclass(frozen=True)
class RestrictedTool(Tool):
    """A ``Tool`` that says whose turns may run it and what the audit trail keeps of its answers.

    A subclass rather than new ``Tool`` fields: an upgrade reads ``Tool``'s exact declaration from its target
    (``looper/_component_probe.py``), and a running Looper refuses a target whose ``Tool`` it cannot read.
    """

    # Whose turns may run it (``ManagerOps.call`` refuses the others with ``forbidden``).
    audience: Audience = Audience.ANY
    # What ``manager_actions.result`` keeps of a successful call's answer; None: what a plain tool's call keeps.
    audit_result: Callable[[Any], Any] | None = None
    # What a call from a turn outside ``audience`` is told (the ``forbidden`` message): required unless it is ANY.
    refusal: str = ""

    def __post_init__(self) -> None:
        if self.audience is not Audience.ANY and not self.refusal.strip():
            raise ValueError(f"{self.name} runs only in some turns: it must say what the others are told (refusal)")


_TYPES: dict[str, tuple[type, ...]] = {"string": (str,), "boolean": (bool,), "object": (dict,), "array": (list,)}


def check_arguments(schema: Mapping[str, Any], arguments: Any) -> dict[str, Any]:
    """``arguments`` checked against ``schema``: an object with the properties it declares (``type`` string,
    integer, boolean, object or array of strings, a nullable type, ``enum``, ``minLength``, ``minimum``/``maximum``,
    ``maxLength``), its ``required`` ones present, no others. Raises ``OpError(INVALID)`` naming the first
    problem, or ``OpError(FORBIDDEN)`` for an argument named like a secret, which no tool takes."""
    from ...db.manager_actions import SECRET_KEY

    if not isinstance(arguments, dict):
        raise OpError(ErrorCode.INVALID, "the arguments must be a JSON object")
    properties: Mapping[str, Any] = schema.get("properties") or {}
    unknown = sorted(set(arguments) - set(properties))
    if secret := next((name for name in unknown if SECRET_KEY.search(str(name))), None):
        raise OpError(ErrorCode.FORBIDDEN, f"{secret}: Looper's tools never take a key, token or password, and you "
                                           "must not ask for one in chat. The user enters it on the dashboard (an "
                                           "LLM provider's API key: Settings → LLM Providers → the provider)")
    if unknown:
        raise OpError(ErrorCode.INVALID, f"unknown argument {unknown[0]!r}; the arguments are: "
                                         f"{', '.join(properties) or 'none'}")
    for name in schema.get("required") or ():
        if arguments.get(name) is None:
            raise OpError(ErrorCode.INVALID, f"{name} is required")
    for name, value in arguments.items():
        if value is not None:
            _check_value(name, properties[name], value)
    return dict(arguments)


def _check_value(name: str, spec: Mapping[str, Any], value: Any) -> None:
    kind = spec.get("type")
    if isinstance(kind, list):
        kind = next((item for item in kind if item != "null"), None)
    if kind == "integer":
        if isinstance(value, bool) or not isinstance(value, int):
            raise OpError(ErrorCode.INVALID, f"{name} must be an integer")
        if "minimum" in spec and value < spec["minimum"] or "maximum" in spec and value > spec["maximum"]:
            raise OpError(ErrorCode.INVALID, f"{name} must be from {spec.get('minimum')} to {spec.get('maximum')}")
    elif kind in _TYPES:
        if not isinstance(value, _TYPES[kind]):
            raise OpError(ErrorCode.INVALID, f"{name} must be a{'n' if kind[0] in 'aeiou' else ''} {kind}")
    if "enum" in spec and value not in spec["enum"]:
        raise OpError(ErrorCode.INVALID, f"{name} must be one of: {', '.join(map(str, spec['enum']))}")
    if kind == "string" and len(value.strip()) < int(spec.get("minLength", 0)):
        raise OpError(ErrorCode.INVALID, f"{name} must not be empty")
    if kind == "string" and "maxLength" in spec and len(value) > spec["maxLength"]:
        raise OpError(ErrorCode.INVALID, f"{name} has {len(value)} characters; at most {spec['maxLength']} are "
                                         "allowed")
    if kind == "array" and any(not isinstance(item, str) for item in value):
        raise OpError(ErrorCode.INVALID, f"{name} must be a list of strings")
    if kind == "object":
        for key, item in value.items():
            allowed = spec.get("properties")
            if allowed is not None and key not in allowed:
                raise OpError(ErrorCode.INVALID, f"{name} has an unknown key {key!r}; its keys are: "
                                                 f"{', '.join(allowed)}")
            if allowed is not None and item is not None:
                _check_value(f"{name}.{key}", allowed[key], item)
