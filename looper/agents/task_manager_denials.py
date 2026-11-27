"""Code-generated explanations for task Manager sandbox denials.

``parse`` reads the sandbox's ``<sandbox_violations>`` block from a Bash tool result into Violations, ``classify``
turns them into typed denials, and ``Observer`` records them. It never invents a host: when neither the allowlist
nor a known client signature explains a denial, the result stays UNKNOWN with no target (RFC task-sessions-v0
sections 25.4 and 25.9 open question 2).

The block format, captured from Claude Code 2.1.332 with the real ``operator_resident_command`` (the probes D1 and
D3 of ``tests/test_task_manager_network_probe.py``): the CLI appends, after the command's own output, one block that
ends the result::

    <sandbox_violations>
    deny network-outbound blocked.example:443 (host is not on the allow list)
    </sandbox_violations>

``deny network-outbound <host>:<port> (<reason>)`` is the one line shape seen, for a plain HTTP request (the host
with port 80, the command's output is "Connection blocked by network allowlist"), for an HTTPS CONNECT (port 443,
curl says "CONNECT tunnel failed, response 403") and for a port in the URL. A denied file write in the same runs
carried no block at all ("Operation not permitted" only), so the observed CLI reports no write path and the WRITE
category is only reachable through a future format. Any other line in the block is OTHER with no target; its class
is the line's first two words when it starts with ``deny`` (the sandbox's own shape), else ``unrecognized``. A
program can print a block of its own as the last thing it writes, so a result is only read when the CLI marked it:
the same event carries ``tool_result_meta[{id, remedy: {kind: "sandbox_violation"}}]``, which a command's output cannot
produce (observed on 2.1.332: a real block has it, a block a command echoed does not). A CLI version without the
marker therefore explains nothing, which is the safe direction.
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import shlex
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from ..db import activity
from .task_manager_network import PolicyInvalid, matches, validate_domains

logger = logging.getLogger(__name__)


class Category(StrEnum):
    NETWORK = "network"
    WRITE = "write"
    OTHER = "other"


class Case(StrEnum):
    NOT_ALLOWED = "not_allowed"
    UNSUPPORTED_CLIENT = "unsupported_client"
    UNKNOWN = "unknown"
    NOT_ALLOWABLE = "not_allowable"  # the destination can never be on an allowlist (IP literal, local name, bad name)


class Client(StrEnum):
    GO_TLS = "go_tls"


@dataclass(frozen=True)
class Violation:
    """One parsed <sandbox_violations> entry; the parser in a later change produces these."""

    category: Category
    target: str | None
    raw_class: str


@dataclass(frozen=True)
class KnownClient:
    client: Client
    executables: frozenset[str]
    signature: str


# Only public tool names. The Go row's signature is the macOS trustd failure. A client is recognized by the basename
# of the command's first word (after VAR=value assignments), so `sudo gh` or a script named `gh` in the task directory
# is read as that name: a documented limit that only changes the wording shown to the operator. Node command-line
# tools do not use the proxy either, but no executable name identifies them, so they have no row.
KNOWN_CLIENTS = (
    KnownClient(Client.GO_TLS, frozenset({"gh", "gcloud", "terraform"}), "x509: OSStatus -26276"),
)

_ASSIGNMENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=")


def command_name(command: str) -> str:
    """Basename of the first word after leading VAR=value assignments; never more of the command."""
    try:
        tokens = shlex.split(command)
    except ValueError:
        # Unbalanced quotes: keep the text before the first space instead of guessing at the command.
        tokens = command.split(" ", 1)[:1]
    for token in tokens:
        if _ASSIGNMENT_RE.match(token):
            continue
        return os.path.basename(token)
    return ""


@dataclass(frozen=True)
class Denial:
    category: Category
    case: Case
    target: str | None
    client: Client | None
    command_name: str


def _known_client(command: str) -> Client | None:
    """The client kind of the executable that ran: by name only. A failure signature in the output is not enough,
    because any program can print it and a supported client (curl) can fail in the same words."""
    name = command_name(command)
    return next((known.client for known in KNOWN_CLIENTS if name in known.executables), None)


_PORT = re.compile(r":[0-9]{1,5}$")


def _allowable(target: str) -> bool:
    host = _PORT.sub("", target)
    try:
        validate_domains([host])
    except PolicyInvalid:
        return False
    return True


def classify(
    violations: Sequence[Violation], *, command: str, output: str, allowed: Sequence[str]
) -> list[Denial]:
    """Classify parsed violations into typed denials; returns [] when nothing explainable was denied.

    ``not_allowed`` (and its "the operator can allow it" text) is only for a destination the operator could actually
    list: one that passes ``validate_domains`` and is not explained by a client that cannot work in the sandbox."""
    name = command_name(command)
    client = _known_client(command)
    denials: list[Denial] = []
    for violation in violations:
        target = violation.target
        if violation.category is Category.NETWORK:
            if client is not None:
                # gh and the like fail in the sandbox whether or not the host is listed.
                denials.append(Denial(Category.NETWORK, Case.UNSUPPORTED_CLIENT, target, client, name))
            elif target is None:
                denials.append(Denial(Category.NETWORK, Case.UNKNOWN, None, None, name))
            elif not _allowable(target):
                denials.append(Denial(Category.NETWORK, Case.NOT_ALLOWABLE, target, None, name))
            elif matches(target, allowed):
                # On the allowlist and denied anyway, with no known client to explain it.
                denials.append(Denial(Category.NETWORK, Case.UNKNOWN, target, None, name))
            else:
                denials.append(Denial(Category.NETWORK, Case.NOT_ALLOWED, target, None, name))
        elif violation.category is Category.WRITE:
            # The write scope has no allowlist: a write denial can never be "allowed but unsupported".
            denials.append(Denial(Category.WRITE, Case.NOT_ALLOWED, target, None, name))
        else:
            denials.append(Denial(Category.OTHER, Case.UNKNOWN, None, None, name))
    if not denials and client is not None:
        # No block, but a known client's executable ran and its failure signature is in the output (a listed
        # host that the client cannot verify: the proxy let it through, so there is nothing to parse).
        known = next(known for known in KNOWN_CLIENTS if known.client is client)
        if known.signature in output:
            denials.append(Denial(Category.NETWORK, Case.UNSUPPORTED_CLIENT, None, client, name))
    return denials


def record_summary(denial: Denial, *, tool_use_id: str, command: str) -> str:
    """Redacted one-line record: typed fields and a command hash, never the command text."""
    digest = hashlib.sha256(command.encode("utf-8")).hexdigest()[:16]
    target = denial.target if denial.target is not None else "-"
    return (
        f"{denial.category.value} {denial.case.value} {target} "
        f"tool_use_id={tool_use_id} command_sha256={digest}"
    )


class TurnThrottle:
    """One object per turn: passes the first denial of each (category, host) pair, suppresses the rest."""

    def __init__(self) -> None:
        self._seen: set[tuple[Category, str | None]] = set()

    def first(self, denial: Denial) -> bool:
        target = None if denial.target is None else _PORT.sub("", denial.target)
        key = (denial.category, target.lower() if target is not None else None)
        if key in self._seen:
            return False
        self._seen.add(key)
        return True


# One count per denial; the host is never a dimension (RFC 25.10). Exposed as ``task_manager_sandbox_denials_total``.
denials_total: Counter[tuple[str, str]] = Counter()

_OPEN, _CLOSE = "<sandbox_violations>", "</sandbox_violations>"
_MAX_LINES = 50
_HOST = r"(?:[A-Za-z0-9](?:[A-Za-z0-9._-]{0,251}[A-Za-z0-9])?|\[[0-9A-Fa-f:.]{2,45}\])"
_NETWORK_LINE = re.compile(rf"deny network-outbound (?P<host>{_HOST}\.?):(?P<port>[0-9]{{1,5}}) \([^()\n]*\)")
_CLASS = re.compile(r"deny(?:\([0-9]+\))? [a-z][a-z0-9*-]*")


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(block["text"] for block in content
                         if isinstance(block, Mapping) and block.get("type") == "text" and isinstance(block.get("text"), str))
    return ""


def parse(result_content: Any) -> list[Violation]:
    """The violations of a Bash tool result: only the block that ends the result, one per line."""
    text = _text(result_content).rstrip()
    if not text.endswith(_CLOSE) or (start := text.rfind(_OPEN)) < 0:
        return []
    found: list[Violation] = []
    for line in text[start + len(_OPEN):-len(_CLOSE)].splitlines()[:_MAX_LINES]:
        line = line.strip()
        if not line:
            continue
        if (match := _NETWORK_LINE.fullmatch(line)) and int(match["port"]) <= 65535:
            # A trailing dot is the same host (``matches`` ignores it too); show and match without it.
            found.append(Violation(Category.NETWORK, f'{match["host"].rstrip(".")}:{match["port"]}', "network-outbound"))
        else:
            head = _CLASS.match(line)
            found.append(Violation(Category.OTHER, None, head[0] if head else "unrecognized"))
    return found


def reported(meta: Any, tool_use_id: str) -> bool:
    """Whether the CLI marked this tool result as a sandbox violation (``tool_result_meta[].remedy.kind``)."""
    return isinstance(meta, list) and any(
        isinstance(item, Mapping) and item.get("id") == tool_use_id
        and isinstance(item.get("remedy"), Mapping) and item["remedy"].get("kind") == "sandbox_violation"
        for item in meta)


class Observer:
    """One per turn: records the sandbox denials of the turn's Bash results (``observe``)."""

    def __init__(self, task_id: str, project: str, message_id: str, allowed: Sequence[str], sink: Any) -> None:
        self.task_id, self.project, self.message_id = task_id, project, message_id
        self.allowed, self.sink = tuple(allowed), sink
        self.throttle = TurnThrottle()

    async def observe(self, tool_use_id: str, tool_input: Any, result_content: Any, is_error: bool,
                      meta: Any = None) -> list[Denial]:
        """Parse and classify one result. Each denial gets an activity row and a count; the first of each
        (category, host) also an event for the conversation. A recording failure never fails the turn.

        ``meta`` is the event's ``tool_result_meta``: only a result the CLI itself marked as a sandbox violation is
        read, because a command can print a block of its own."""
        if not reported(meta, tool_use_id):
            return []
        output = _text(result_content)
        command = str(tool_input.get("command") or "") if isinstance(tool_input, Mapping) else ""
        found = classify(parse(result_content), command=command, output=output, allowed=self.allowed)
        if not found:
            return []
        from .tiers.task_manager import TaskManagerEvent

        for denial in found:
            denials_total[(denial.category.value, self.project)] += 1
            try:
                await activity.insert_activity(
                    task_id=self.task_id, kind="sandbox_denial",
                    summary=record_summary(denial, tool_use_id=tool_use_id, command=command),
                    metadata={"message_id": self.message_id, "tool_use_id": tool_use_id})
            except Exception:
                logger.warning("task %s: could not store a sandbox denial record", self.task_id, exc_info=True)
            if not self.throttle.first(denial):
                continue
            try:
                await self.sink.event(TaskManagerEvent.SANDBOX_DENIAL, {
                    "tool_use_id": tool_use_id, "category": denial.category.value, "case": denial.case.value,
                    "target": denial.target, "client": denial.client.value if denial.client else None,
                    "command_name": denial.command_name})
            except Exception:
                logger.warning("task %s: could not send a sandbox denial event", self.task_id, exc_info=True)
        return found
