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
program can print a block of its own as the last thing it writes; its host would then be named in the explanation,
which is why a host must look like a host and the text is written from typed fields.
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
from .task_manager_network import matches

logger = logging.getLogger(__name__)


class Category(StrEnum):
    NETWORK = "network"
    WRITE = "write"
    OTHER = "other"


class Case(StrEnum):
    NOT_ALLOWED = "not_allowed"
    UNSUPPORTED_CLIENT = "unsupported_client"
    UNKNOWN = "unknown"


class Client(StrEnum):
    GO_TLS = "go_tls"
    NODE_NO_PROXY = "node_no_proxy"


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


# Only public tool names. The Go row's signature is the macOS trustd failure; the Node row matches by the
# DNS failure signature alone because Node command-line tools do not use the proxy by default.
KNOWN_CLIENTS = (
    KnownClient(Client.GO_TLS, frozenset({"gh", "gcloud", "terraform"}), "x509: OSStatus -26276"),
    KnownClient(Client.NODE_NO_PROXY, frozenset(), "getaddrinfo ENOTFOUND"),
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


def _known_client(command: str, output: str) -> Client | None:
    name = command_name(command)
    for known in KNOWN_CLIENTS:
        if name in known.executables or known.signature in output:
            return known.client
    return None


def classify(
    violations: Sequence[Violation], *, command: str, output: str, allowed: Sequence[str]
) -> list[Denial]:
    """Classify parsed violations into typed denials; returns [] when nothing explainable was denied."""
    name = command_name(command)
    denials: list[Denial] = []
    for violation in violations:
        if violation.category is Category.NETWORK:
            if violation.target is not None and matches(violation.target, allowed):
                client = _known_client(command, output)
                if client is not None:
                    denials.append(
                        Denial(Category.NETWORK, Case.UNSUPPORTED_CLIENT, violation.target, client, name)
                    )
                else:
                    # On the allowlist, but no known client explains why the sandbox still denied it.
                    denials.append(Denial(Category.NETWORK, Case.UNKNOWN, violation.target, None, name))
            else:
                denials.append(Denial(Category.NETWORK, Case.NOT_ALLOWED, violation.target, None, name))
        elif violation.category is Category.WRITE:
            # The write scope has no allowlist: a write denial can never be "allowed but unsupported".
            denials.append(Denial(Category.WRITE, Case.NOT_ALLOWED, violation.target, None, name))
        else:
            denials.append(Denial(Category.OTHER, Case.UNKNOWN, None, None, name))
    if not denials:
        # No parsed violation, but the output carries a known client failure signature.
        for known in KNOWN_CLIENTS:
            if known.signature in output:
                denials.append(
                    Denial(Category.NETWORK, Case.UNSUPPORTED_CLIENT, None, known.client, name)
                )
                break
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
    """One object per turn: passes the first denial of each (category, target) pair, suppresses the rest."""

    def __init__(self) -> None:
        self._seen: set[tuple[Category, str | None]] = set()

    def first(self, denial: Denial) -> bool:
        key = (denial.category, denial.target)
        if key in self._seen:
            return False
        self._seen.add(key)
        return True


# One count per denial; the host is never a dimension (RFC 25.10). Exposed as ``task_manager_sandbox_denials_total``.
denials_total: Counter[tuple[str, str]] = Counter()

_OPEN, _CLOSE = "<sandbox_violations>", "</sandbox_violations>"
_MAX_LINES = 50
_HOST = r"(?:[A-Za-z0-9](?:[A-Za-z0-9._-]{0,251}[A-Za-z0-9])?|\[[0-9A-Fa-f:.]{2,45}\])"
_NETWORK_LINE = re.compile(rf"deny network-outbound (?P<target>{_HOST}:(?P<port>[0-9]{{1,5}})) \([^()\n]*\)")
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
            found.append(Violation(Category.NETWORK, match["target"], "network-outbound"))
        else:
            head = _CLASS.match(line)
            found.append(Violation(Category.OTHER, None, head[0] if head else "unrecognized"))
    return found


class Observer:
    """One per turn: records the sandbox denials of the turn's Bash results (``observe``)."""

    def __init__(self, task_id: str, project: str, message_id: str, allowed: Sequence[str], sink: Any) -> None:
        self.task_id, self.project, self.message_id = task_id, project, message_id
        self.allowed, self.sink = tuple(allowed), sink
        self.throttle = TurnThrottle()

    async def observe(self, tool_use_id: str, tool_input: Any, result_content: Any, is_error: bool) -> list[Denial]:
        """Parse and classify one result. Each denial gets an activity row and a count; the first of each
        (category, target) also an event for the conversation. A recording failure never fails the turn."""
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
