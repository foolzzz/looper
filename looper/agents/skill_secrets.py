"""A fixed table of secret shapes a skill proposal's text must not have.

``find`` returns the rule's name and where it matched, never the matched text,
so a finding can be logged or shown without repeating the secret. The table is
a backstop: it does not catch every secret, and the operator still reads the
proposal before confirming it.

It does not share a table with ``log_redaction`` (that masks values registered
at runtime, such as the configured tokens, not patterns) or ``advice_lint``
(its pattern is one alternation without names, tuned for reports, with a
base64-run rule that would refuse ordinary skill text).
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Finding:
    rule: str
    start: int
    end: int


# One named pattern per secret shape.
RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("pem_private_key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("aws_access_key", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("github_token", re.compile(r"gh[pousr]_[A-Za-z0-9]{36,}")),
    ("api_key_sk", re.compile(r"sk-[A-Za-z0-9_-]{20,}")),
    ("slack_token", re.compile(r"xox[abprs]-[A-Za-z0-9-]{10,}")),
    ("bearer_token", re.compile(r"Bearer\s+[A-Za-z0-9._~+/=-]{20,}")),
    ("credential_assignment", re.compile(r"(?i)(password|secret|token|api[_-]?key)\s*[:=]\s*\S{8,}")),
)


def find(text: str) -> list[Finding]:
    """Every match of every rule, by position. Each carries its rule's name and span, not the text."""
    found = [Finding(rule, match.start(), match.end()) for rule, pattern in RULES for match in pattern.finditer(text)]
    return sorted(found, key=lambda finding: (finding.start, finding.rule))
