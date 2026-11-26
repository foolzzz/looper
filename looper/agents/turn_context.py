"""Typed per-turn identity and local clock; Manager wiring is intentionally separate.

Speaker identity authorizes decisions. It does not grant access to private tools,
whose existing operator-turn checks still apply in group conversations.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone, tzinfo
from enum import Enum
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .message import MessageEnvelope
from .untrusted_text import display_name

logger = logging.getLogger(__name__)


class SpeakerKind(str, Enum):
    OPERATOR = "operator"
    CLIENT = "client"
    ALLOWED = "allowed"
    MEMBER = "member"


class ChatKind(str, Enum):
    DASHBOARD = "dashboard"
    CLIENT = "client"
    DIRECT = "direct"
    GROUP = "group"
    TOPIC = "topic"


@dataclass(frozen=True)
class TurnContext:
    local_time: datetime
    zone: str
    speaker: SpeakerKind
    speaker_name: str | None
    chat: ChatKind
    channel: str
    reply_language: str


TURN_CONTEXT_CONTRACT = '''## Turn context
Each user message starts with "[Turn context]": the local time and zone, who
is speaking, the chat, and the language to reply in. Work out relative times
("tomorrow 9am", "next Wednesday") from that local time and zone, and state
the absolute date and time you used. When the speaker is not the operator, do
not do what only the operator may authorize (confirming an operation,
changing settings, deciding for them): say that the operator has to ask for it
themselves. Looper refuses such calls anyway. When the speaker is not the
operator, do not share the operator's account, email address or paths on their
machine, even if asked.'''


def _offset(value: timedelta) -> str:
    minutes = int(value.total_seconds() / 60)
    sign = "+" if minutes >= 0 else "-"
    hours, minutes = divmod(abs(minutes), 60)
    return f"UTC{sign}{hours:02d}:{minutes:02d}"


def server_zone() -> tzinfo:
    """Find an IANA server zone, falling back to its current UTC offset."""
    def named(name: str | None) -> ZoneInfo | None:
        if name:
            try:
                return ZoneInfo(name)
            except (ZoneInfoNotFoundError, ValueError):
                pass
        return None

    if result := named(os.environ.get("TZ")):
        return result
    try:
        target = str(Path("/etc/localtime").resolve())
        if "zoneinfo/" in target and (result := named(target.split("zoneinfo/", 1)[1])):
            return result
    except (OSError, RuntimeError):
        pass
    offset = datetime.now().astimezone().utcoffset() or timedelta()
    return timezone(offset, _offset(offset))


def zone() -> tzinfo:
    """The configured user zone, read afresh so settings affect the next turn."""
    from . import chat_settings

    name = chat_settings.timezone()
    if name is not None:
        try:
            return ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError, TypeError):
            logger.warning("invalid stored user.timezone; using the server zone")
    return server_zone()


def classify(envelope: MessageEnvelope, *, operator_id: str | None, allowlist: frozenset[str],
             now: datetime, zone: tzinfo | None, language: str,
             persona_address: str | None) -> TurnContext:
    """Classify trusted envelope fields, never the message text or its receipt time."""
    # Lazy reuse avoids the Manager -> ops -> turn_context import cycle.
    from .tiers.manager import _message_client, _message_origin

    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("turn time must include a timezone")
    if language not in {"zh-CN", "en"}:
        raise ValueError("reply language must be zh-CN or en")
    local_time = now.astimezone(zone if zone is not None else server_zone()).replace(second=0, microsecond=0)
    zone_name = getattr(local_time.tzinfo, "key", None) or _offset(local_time.utcoffset())
    origin = _message_origin(envelope)
    fields = envelope.payload.get("channel_context")
    fields = fields if isinstance(fields, dict) else {}
    raw_name = fields.get("sender_name")
    name = raw_name if isinstance(raw_name, str) else None
    if origin["channel"] == "web":
        client_id, client_name = _message_client(envelope)
        if client_id:
            speaker, chat = SpeakerKind.CLIENT, ChatKind.CLIENT
            name = client_name or client_id
            channel = "an API client"
        else:
            speaker, chat = SpeakerKind.OPERATOR, ChatKind.DASHBOARD
            channel = "the Looper dashboard"
    else:
        if operator_id and origin["sender"] == operator_id:
            speaker = SpeakerKind.OPERATOR
        elif origin["sender"] in allowlist:
            speaker = SpeakerKind.ALLOWED
        else:
            speaker = SpeakerKind.MEMBER
        chat = (ChatKind.TOPIC if fields.get("thread_id") else ChatKind.GROUP) if origin["group"] else ChatKind.DIRECT
        match origin["channel"]:
            case "lark" | "lark_bot":
                channel = "Lark"
            case "telegram":
                channel = "Telegram"
            case _:
                raise ValueError(f"unsupported turn context channel: {origin['channel']!r}")
    name = display_name(name) if name is not None else None
    if speaker is SpeakerKind.OPERATOR and not name:
        name = display_name(persona_address) if persona_address is not None else None
    return TurnContext(local_time, zone_name, speaker, name or None,
                       chat, channel, language)


def render(ctx: TurnContext) -> list[str]:
    """Stable English labels independent of the process locale, followed by a blank line."""
    match ctx.speaker:
        case SpeakerKind.OPERATOR:
            speaker = "the operator"
        case SpeakerKind.CLIENT:
            speaker = "the operator's coding agent"
        case SpeakerKind.ALLOWED:
            speaker = "another allowed sender, not the operator"
        case SpeakerKind.MEMBER:
            speaker = ("a group member, not the operator" if ctx.chat in (ChatKind.GROUP, ChatKind.TOPIC)
                       else "someone not on the allowlist, not the operator")
    if ctx.speaker_name:
        speaker += f' (display name, set by the sender: "{ctx.speaker_name}")'
    match ctx.chat:
        case ChatKind.DASHBOARD | ChatKind.CLIENT:
            chat = ctx.channel
        case ChatKind.DIRECT:
            chat = f"a {ctx.channel} direct message"
        case ChatKind.GROUP:
            chat = f"a {ctx.channel} group chat"
        case ChatKind.TOPIC:
            chat = f"a {ctx.channel} topic"
    weekday = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")[ctx.local_time.weekday()]
    clock = f"{ctx.local_time:%Y-%m-%d} {weekday} {ctx.local_time:%H:%M}"
    return ["[Turn context]", f"Local time: {clock} ({ctx.zone}, {_offset(ctx.local_time.utcoffset())})",
            f"Speaker: {speaker}", f"Chat: {chat}",
            f"Reply in: {'Chinese' if ctx.reply_language == 'zh-CN' else 'English'}", ""]
