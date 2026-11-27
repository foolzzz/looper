"""Per-project external domain allowlist for the task Manager sandbox.

Pure validation and matching helpers; storage and wiring live elsewhere. Only external domains are accepted:
IP literals, loopback and non-public name suffixes are refused up front. Names that resolve to private
addresses cannot be caught statically; the sandbox's proxy and Seatbelt still refuse loopback and private
destinations (RFC task-sessions-v0 section 25.4).
"""
from __future__ import annotations

import ipaddress
import re
from collections.abc import Sequence
from typing import Any

MAX_DOMAINS = 50
FIELD = "network_allowed_domains"

# Names under these suffixes are local or otherwise not external, so they can never be operator-approved
# external destinations ("home.arpa" is the RFC 8375 residential-use name).
NON_PUBLIC_SUFFIXES = ("local", "localhost", "internal", "home.arpa", "lan")

WILDCARD = "*."
_LABEL_RE = re.compile(r"^(?!-)[a-z0-9-]{1,63}(?<!-)$")
_MAX_HOSTNAME_LEN = 253


class PolicyInvalid(ValueError):
    """A network_allowed_domains value rejected by validation; carries typed context for the operation error."""

    def __init__(self, *, entry: str | None, why: str) -> None:
        super().__init__(f"{FIELD}: {entry if entry is not None else '<list>'}: {why}")
        self.field = FIELD
        self.entry = entry
        self.why = why


def _refuse(entry: str | None, why: str) -> PolicyInvalid:
    return PolicyInvalid(entry=entry, why=why)


def _is_ip_literal(value: str) -> bool:
    try:
        ipaddress.ip_address(value)
    except ValueError:
        return False
    return True


def _validate_entry(entry: str) -> None:
    if not entry:
        raise _refuse(entry, "empty entries are not allowed")
    # Bracketed IPv6 literals such as "[::1]"; a bare IPv6 literal (for example "::1") is caught before
    # the port check so its colons do not make it look like a host:port pair.
    if (entry.startswith("[") and entry.endswith("]") and _is_ip_literal(entry[1:-1])) or _is_ip_literal(entry):
        raise _refuse(entry, "IP literals are not external domains")
    # A bare "*" or a "*" anywhere except a leading "*." prefix.
    if "*" in entry and (not entry.startswith(WILDCARD) or entry.count("*") > 1):
        raise _refuse(entry, "a wildcard is only allowed as a leading '*.' prefix")
    if any(character.isspace() for character in entry):
        raise _refuse(entry, "whitespace is not allowed")
    if ":" in entry:
        raise _refuse(entry, "ports are not allowed")
    if "/" in entry:
        raise _refuse(entry, "paths are not allowed")
    if "@" in entry:
        raise _refuse(entry, "userinfo is not allowed")
    if entry == "localhost":
        raise _refuse(entry, "localhost is not an external domain")
    for suffix in NON_PUBLIC_SUFFIXES:
        if entry == suffix or entry.endswith("." + suffix):
            raise _refuse(entry, "names under non-public suffixes are not external domains")
    hostname = entry[len(WILDCARD):] if entry.startswith(WILDCARD) else entry
    if not hostname:
        raise _refuse(entry, "a wildcard must be followed by a hostname")
    labels = hostname.split(".")
    if len(labels) < 2:
        raise _refuse(entry, "single-label names are not external domains")
    if len(hostname) > _MAX_HOSTNAME_LEN or any(not _LABEL_RE.fullmatch(label) for label in labels):
        raise _refuse(entry, "invalid hostname")
    # "127.1" and "0x7f.1" are not IP literals to ``ipaddress`` but resolvers read them as IPv4 addresses; no
    # public top-level domain is all digits or hexadecimal-prefixed.
    if labels[-1].isdigit() or labels[-1].startswith("0x"):
        raise _refuse(entry, "IP literals are not external domains")


def validate_domains(values: Any) -> tuple[str, ...]:
    """Validate an operator-provided allowlist and return normalized entries in input order."""
    if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
        raise PolicyInvalid(entry=None, why="not a list of strings")
    if len(values) > MAX_DOMAINS:
        raise PolicyInvalid(entry=None, why=f"at most {MAX_DOMAINS} entries")
    normalized: list[str] = []
    seen: set[str] = set()
    for raw in values:
        entry = raw.lower()
        if entry.endswith("."):
            entry = entry[:-1]
        if entry in seen:
            # Duplicates are an error, never dropped or truncated; name the second occurrence.
            raise PolicyInvalid(entry=entry, why="duplicate entry")
        _validate_entry(entry)
        seen.add(entry)
        normalized.append(entry)
    return tuple(normalized)


def matches(host: str, allowed: Sequence[str]) -> bool:
    """Tell whether a denied host (optionally host:port) is covered by a normalized allowlist."""
    if host.count(":") == 1:
        host = host.rsplit(":", 1)[0]
    host = host.lower().rstrip(".")
    for entry in allowed:
        if host == entry:
            return True
        if entry.startswith(WILDCARD) and host.endswith("." + entry[len(WILDCARD):]):
            return True
    return False


# RFC task-sessions-v0 section 25.3 "scope": reused by operation descriptions and the dashboard.
SCOPE_TEXT = (
    "Only commands that leave the sandbox through its HTTP(S) proxy are affected by this allowlist "
    "(for example curl, npm, pip and ordinary Python HTTP clients). Go-based command-line tools such as "
    "gh cannot be used in a v1 task Manager session even when their domain is listed: on macOS their "
    "certificate verification needs the system trust service, which the sandbox does not expose. "
    "Node-based command-line tools do not use the proxy by default and are not guaranteed to work. "
    "A listed domain can itself be used to move data out of the sandbox; choosing the entries is the "
    "operator's responsibility."
)


async def allowed_domains(project_id: str) -> tuple[str, ...]:
    """The project's stored allowlist, validated again: an entry this version refuses raises ``PolicyInvalid``."""
    from ..db import task_manager_sandbox_policy

    stored, _ = await task_manager_sandbox_policy.get(project_id)
    return validate_domains(stored)
