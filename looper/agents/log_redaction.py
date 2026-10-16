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

# A value this short is no usable token, and masking it would mangle every line.
_MIN_SECRET_LEN = 8

# Replaced, never changed in place: a thread formatting a line reads a snapshot.
_secrets: tuple[str, ...] = ()


def add_secret(value: str) -> None:
    """Mask ``value`` in every log line from now on."""
    global _secrets
    if len(value) >= _MIN_SECRET_LEN and value not in _secrets:
        _secrets = (*_secrets, value)


def redact(text: str) -> str:
    """``text`` with the registered secrets and any bot token in a URL path masked."""
    for secret in _secrets:
        text = text.replace(secret, MASK)
    return _BOT_TOKEN_IN_PATH.sub(MASK, text)


class RedactingFormatter(logging.Formatter):
    """A formatter whose finished lines are masked with ``redact``."""

    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


def log_to_stderr(level: int, fmt: str) -> None:
    """Send the root logger's records from ``level`` on to stderr as ``fmt``, masked."""
    handler = logging.StreamHandler()
    handler.setFormatter(RedactingFormatter(fmt))
    root = logging.getLogger()
    root.addHandler(handler)
    root.setLevel(level)
