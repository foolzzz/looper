"""Code-generated explanations for task Manager sandbox denials.

The sandbox's own denial output is parsed elsewhere into Violations; this module turns violations into typed
denial classifications and redacted records. It never invents a host: when neither the allowlist nor a known
client signature explains a denial, the result stays UNKNOWN with no target (RFC task-sessions-v0
sections 25.4 and 25.9 open question 2).
"""
from __future__ import annotations

import hashlib
import os
import re
import shlex
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

from .task_manager_network import matches


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
