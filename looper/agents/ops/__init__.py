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
from typing import Any

from ..message import MessageEnvelope


class Risk(str, Enum):
    READ = "read"
    WRITE = "write"
    HIGH = "high"


class ErrorCode(str, Enum):
    NOT_FOUND = "not_found"
    INVALID = "invalid"
    NEEDS_USER_TURN = "needs_user_turn"  # a change outside a turn that answers the user
    FORBIDDEN = "forbidden"
    CONFLICT = "conflict"  # the target's state does not allow it (an ended task, a task not started)
    EXPIRED = "expired"  # a confirmation past its time or its turn
    UNAVAILABLE = "unavailable"  # Looper could not do it now (its database); the call may be tried again


class OpError(Exception):
    """A call Looper refuses or could not run, with the code the answer carries."""

    def __init__(self, code: ErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass
class Turn:
    """The Manager turn calls belong to: the message it answers and its chat, or one Claude runs on its own.

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
    # Looper refused a call of this turn for its token: the tools did not work, whatever Claude says.
    refused: bool = False

    @property
    def autonomous(self) -> bool:
        return self.message_id is None

    @property
    def chat_key(self) -> tuple[str | None, str | None]:
        return self.channel, self.chat


@dataclass(frozen=True)
class Call:
    """One call of a tool: its checked arguments, the turn it came in, and the platform it acts on."""

    arguments: Mapping[str, Any]
    turn: Turn
    platform: Any  # service.Platform: the bus and the agents a tool acts through


@dataclass(frozen=True)
class Tool:
    """One catalog entry: an MCP tool of the Manager.

    ``risk`` is the tier of its calls; ``escalate``, when given, makes one call
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

    def listing(self) -> dict[str, Any]:
        """The tool as MCP's ``tools/list`` names it."""
        return {"name": self.name, "description": self.description, "inputSchema": dict(self.schema)}


_TYPES: dict[str, tuple[type, ...]] = {"string": (str,), "boolean": (bool,), "object": (dict,), "array": (list,)}


def check_arguments(schema: Mapping[str, Any], arguments: Any) -> dict[str, Any]:
    """``arguments`` checked against ``schema``: an object with the properties it declares (``type`` string,
    integer, boolean, object or array of strings, ``enum``, ``minLength``, ``minimum``/``maximum``,
    ``maxLength``), its ``required`` ones present, no others. Raises ``OpError(INVALID)`` naming the first
    problem."""
    if not isinstance(arguments, dict):
        raise OpError(ErrorCode.INVALID, "the arguments must be a JSON object")
    properties: Mapping[str, Any] = schema.get("properties") or {}
    unknown = sorted(set(arguments) - set(properties))
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
