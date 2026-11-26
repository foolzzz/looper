"""Secrets masked in log output.

The Telegram Bot API puts the bot token in every request URL's path
(``https://api.telegram.org/bot<token>/getUpdates``), httpx logs each request
URL at INFO, and python-telegram-bot's ``Bot`` repr shows the token. Every
entry point that sets up logging does it through ``log_to_stderr``: its
formatter masks the finished line, the traceback included, so a record from
any logger is covered. A token in a ``bot<token>`` URL path becomes
``bot***``, and a value registered with ``add_secret`` (the configured token)
becomes ``***`` wherever it appears.
"""

from __future__ import annotations

import logging
import re

MASK = "***"

# A Telegram bot token, <bot id>:<secret>, in a Bot API URL path.
_BOT_TOKEN_IN_PATH = re.compile(r"(?<=bot)\d+:[A-Za-z0-9_-]+")

# CSI colour/control sequences, OSC titles/links, and single-character escapes.
_ANSI_ESCAPE = re.compile(r"\x1b(?:\][^\x07\x1b]*(?:\x07|\x1b\\)|\[[0-?]*[ -/]*[@-~]|[@-_])")

# A value this short is no usable token, and masking it would mangle every line.
_MIN_SECRET_LEN = 8

# Replaced, never changed in place: a thread formatting a line reads a snapshot.
_secrets: tuple[str, ...] = ()


def add_secret(value: str) -> None:
    """Mask ``value`` in every log line from now on."""
    global _secrets
    if len(value) >= _MIN_SECRET_LEN and value not in _secrets:
        _secrets = (*_secrets, value)


def registered() -> tuple[str, ...]:
    """The values registered with ``add_secret``."""
    return _secrets


def redact(text: str) -> str:
    """``text`` with the registered secrets and any bot token in a URL path masked."""
    for secret in _secrets:
        text = text.replace(secret, MASK)
    return _BOT_TOKEN_IN_PATH.sub(MASK, text)


_DIAGNOSTIC_MASK = "[redacted]"
# Keep these display rules aligned with web/src/lib/attention-display.ts.
_SECRET_SEGMENT = (r"(?:api[_-]?key|private[_-]?key|passwd|password|authorization|bearer|cookie|session|"
                   r"token|secret|credential|key)")
_SECRET_KEY = rf"(?:[A-Za-z0-9]+[_-])*{_SECRET_SEGMENT}(?:[_-][A-Za-z0-9]+)*"
_ASSIGNMENT = re.compile(
    rf'''(?<![A-Za-z0-9_-])((?:"{_SECRET_KEY}"|'{_SECRET_KEY}'|{_SECRET_KEY})\s*[:=]\s*)'''
    r'''("(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*'|\[redacted\]|[^\s,;&?#{}\[\]"'<>]+)''',
    re.IGNORECASE | re.ASCII,
)
_AUTH_HEADER = re.compile(r"\b(?:Bearer|Basic)\s+[A-Za-z0-9\-._~+/=]+", re.IGNORECASE | re.ASCII)
_TOKEN_PREFIX = re.compile(r"\b(?:sk|rk|pk|ghp|gho|github_pat|xox[baprs]|AKIA)[-_][A-Za-z0-9_\-]{8,}\b",
                           re.IGNORECASE | re.ASCII)
_JWT = re.compile(r"\b[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b", re.ASCII)
_OPAQUE = re.compile(r"(?<![A-Za-z0-9+/_-])[A-Za-z0-9+/_-]{40,}={0,2}")


def redact_diagnostic(text: str) -> str:
    """Mask credential values in attention/self-check facts, keeping unassigned diagnoses readable.

    Unlike log redaction, this is independent of configured secrets. The shared
    Python/web cases live in tests/fixtures/attention-redaction.json.
    """
    text = _AUTH_HEADER.sub(_DIAGNOSTIC_MASK, text)
    text = _ASSIGNMENT.sub(lambda match: match[1] + _DIAGNOSTIC_MASK, text)
    for pattern in (_TOKEN_PREFIX, _JWT, _OPAQUE):
        text = pattern.sub(_DIAGNOSTIC_MASK, text)
    return text


def strip_ansi(text: str) -> str:
    """Remove terminal escape sequences before retaining CLI diagnostics."""
    return _ANSI_ESCAPE.sub("", text)


class RedactingFormatter(logging.Formatter):
    """A formatter whose finished lines are masked with ``redact``."""

    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


def log_to_stderr(level: int, fmt: str) -> None:
    """Send the root logger's records from ``level`` on to stderr as ``fmt``, masked; in place of the handler an
    earlier call added, so no line is written twice."""
    handler = logging.StreamHandler()
    handler.setFormatter(RedactingFormatter(fmt))
    root = logging.getLogger()
    for earlier in [h for h in root.handlers if isinstance(h.formatter, RedactingFormatter)]:
        root.removeHandler(earlier)
    root.addHandler(handler)
    root.setLevel(level)
