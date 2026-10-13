"""Who may reach the Manager through a chat channel (RFC decision 8).

Every agent runs Claude Code with every tool and without permission prompts,
so a message that reaches the Manager can have it run any command on this
machine. Telegram and Lark therefore pass on only the messages of the
senders their configuration lists:

- the list is a field of the channel's ``channel_configs.config``, edited on
  the Settings page: Telegram numeric user IDs (``allowed_user_ids``) and Lark
  ``open_id`` values (``allowed_open_ids``, the sender id the Lark adapter
  reads from each message event);
- a message from anyone else never reaches the Manager: the adapter logs one
  line with the channel and the sender id (never the text) and sends no reply;
- a channel whose list is empty or invalid does not start (``start_problem``
  says why, for the log and the Settings page).

The web channel has no list: it only answers local requests
(``request_guard``).
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

# A list longer than this is almost certainly not a list of people.
MAX_SENDERS = 100


class AllowlistError(ValueError):
    """A sender allowlist the configuration cannot hold (answered with 400)."""


@dataclass(frozen=True)
class SenderIdFormat:
    """The sender ids one channel type lists: its config field and what a valid id looks like."""

    field: str  # the channel config key holding the list
    label: str  # one id, as the user knows it
    pattern: re.Pattern[str]
    hint: str  # why an id was refused, and what to use instead


SENDER_ID_FORMATS: dict[str, SenderIdFormat] = {
    "telegram": SenderIdFormat(
        field="allowed_user_ids",
        label="Telegram user ID",
        # Telegram's user ids are positive; a negative id is a group or a channel.
        pattern=re.compile(r"[1-9][0-9]{0,19}"),
        hint="use the sender's numeric Telegram user ID, such as 123456789 "
             "(a negative number is a group or channel, not a sender)",
    ),
    "lark": SenderIdFormat(
        field="allowed_open_ids",
        label="Lark open_id",
        # The adapter matches the open_id Lark sends with each message
        # (event.sender.sender_id.open_id), stable for one user in one app.
        pattern=re.compile(r"ou_[0-9A-Za-z]{8,64}"),
        hint="use the sender's open_id for this app, which starts with ou_ "
             "(a union_id starts with on_, and a user_id has no prefix)",
    ),
}


def sender_ids(channel_type: str, raw: Any) -> tuple[str, ...]:
    """The validated ids of a channel's allowlist field, in order, without blanks or repeats.

    ``raw`` is a list of ids, or one comma-separated string as Telegram
    settings were stored before; a Telegram id may also be a number. ``None``
    is an empty list. Raises ``AllowlistError`` naming the first bad id.
    """
    fmt = SENDER_ID_FORMATS[channel_type]
    if raw is None:
        entries: list[Any] = []
    elif isinstance(raw, str):
        entries = raw.split(",")
    elif isinstance(raw, list):
        entries = raw
    else:
        raise AllowlistError(f"{fmt.field} must be a list of {fmt.label}s")
    ids: dict[str, None] = {}
    for entry in entries:
        if channel_type == "telegram" and isinstance(entry, int) and not isinstance(entry, bool):
            entry = str(entry)
        if not isinstance(entry, str):
            raise AllowlistError(f"{fmt.field}: each entry is a {fmt.label} written as a string")
        text = entry.strip()
        if not text:
            continue
        if not fmt.pattern.fullmatch(text):
            raise AllowlistError(f"{text[:80]!r} is not a {fmt.label}; {fmt.hint}")
        ids[text] = None
    if len(ids) > MAX_SENDERS:
        raise AllowlistError(f"{fmt.field} lists at most {MAX_SENDERS} senders, got {len(ids)}")
    return tuple(ids)


def start_problem(channel_type: str, config: Mapping[str, Any]) -> str | None:
    """Why a channel with this configuration does not start; None when its allowlist lets it.

    A channel type without an allowlist (or an unknown one) has no problem here.
    """
    fmt = SENDER_ID_FORMATS.get(channel_type)
    if fmt is None:
        return None
    try:
        ids = sender_ids(channel_type, config.get(fmt.field))
    except AllowlistError as exc:
        return f"its sender allowlist is invalid: {exc}"
    if not ids:
        return (f"its sender allowlist is empty; list the {fmt.label}s that may talk to the Manager, "
                "which can run any command on this machine")
    return None


def normalized_config(channel_type: str, config: Any) -> dict[str, Any]:
    """A channel config as stored: its allowlist field, when given, as a validated list of ids.

    Raises ``AllowlistError`` for a config that is not an object or a bad allowlist.
    """
    if not isinstance(config, dict):
        raise AllowlistError("config must be a JSON object")
    fmt = SENDER_ID_FORMATS.get(channel_type)
    if fmt is None or fmt.field not in config:
        return dict(config)
    return {**config, fmt.field: list(sender_ids(channel_type, config[fmt.field]))}


def channel_view(row: Mapping[str, Any]) -> dict[str, Any]:
    """A ``channel_configs`` row for the API, with ``start_problem`` (None when nothing in it stops the channel)."""
    config = row.get("config")
    return {**row, "start_problem": start_problem(str(row.get("channel_type") or ""),
                                                  config if isinstance(config, Mapping) else {})}


@dataclass(frozen=True)
class SenderAllowlist:
    """The senders one channel passes on to the Manager."""

    channel_id: str  # names the channel in log lines
    ids: frozenset[str]

    def admits(self, sender_id: object) -> bool:
        """Whether a message from ``sender_id`` may reach the Manager.

        A refused sender is logged as one line with the channel and the id,
        never the message text.
        """
        sender = "" if sender_id is None else str(sender_id)
        if sender and sender in self.ids:
            return True
        logger.warning("%s: dropped a message from sender %r, who is not on the sender allowlist",
                       self.channel_id, sender[:80] or "<unknown>")
        return False


def allowlist_from_config(channel_id: str, channel_type: str, config: Mapping[str, Any]) -> SenderAllowlist:
    """The allowlist in a channel's configuration; raises ``AllowlistError`` when it is invalid."""
    fmt = SENDER_ID_FORMATS[channel_type]
    return SenderAllowlist(channel_id, frozenset(sender_ids(channel_type, config.get(fmt.field))))
