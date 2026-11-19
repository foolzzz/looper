"""Read an outside collector's per-chat NDJSON files, one raw Lark message per line (message-stream RFC, section 6).

``scan_file`` decides which bytes of one file are new from the state saved after the last scan: the byte offset, the
size, the inode, the modification time, and the id of the last complete line before the offset. A file whose size,
inode and time are unchanged is skipped. One that is smaller than the offset, has another inode, or whose line before
the offset is another message was rewritten, so it is read again from the start (the store deduplicates by message
id). Otherwise it is read from the offset. Only complete lines (ending in a newline) are read; an unterminated last
line waits for its newline. Line order is not send order, and a line may appear twice; this module reports lines as
they are and leaves both to the store.

``extract_text`` renders a message the way the outside collector does: ``@_user_N`` placeholders become ``@name``,
a ``post`` message is its paragraphs of inline elements, anything else is its title or ``[msg_type]``.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

TEXT_MAX_CHARS = 16 * 1024
# One scan of one file reads at most this much beyond the offset (more only to finish the line it ends in).
DEFAULT_MAX_BYTES = 64 * 1024 * 1024
_EXTEND = 64 * 1024
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_PLACEHOLDER = re.compile(r"@_user_\d+|@_all")


@dataclass(frozen=True)
class FileState:
    offset: int
    size: int
    inode: str
    mtime: datetime
    last_id: str | None


@dataclass(frozen=True)
class BridgeMessage:
    external_id: str
    sender_id: str
    sender_kind: str
    sent_at: datetime
    msg_type: str
    thread_id: str | None
    root_id: str | None
    parent_id: str | None
    mention_ids: list[str]
    mention_names: dict[str, str]
    text: str
    body: dict[str, Any]
    body_bytes: int


@dataclass
class Scan:
    state: FileState
    messages: list[BridgeMessage] = field(default_factory=list)
    unchanged: bool = False
    rescanned: bool = False
    bad_lines: int = 0


def _microseconds(stat: os.stat_result) -> datetime:
    return _EPOCH + timedelta(microseconds=stat.st_mtime_ns // 1000)


def _strip_nul(value: Any) -> Any:
    """PostgreSQL text and jsonb cannot hold U+0000."""
    if isinstance(value, str):
        return value.replace("\x00", "")
    if isinstance(value, list):
        return [_strip_nul(item) for item in value]
    if isinstance(value, dict):
        return {_strip_nul(key): _strip_nul(item) for key, item in value.items()}
    return value


def _fill_mentions(text: str, mentions: Any) -> str:
    names = {m["key"]: m["name"] for m in mentions if isinstance(m, dict) and m.get("key") and m.get("name")} \
        if isinstance(mentions, list) else {}

    def replace(found: re.Match[str]) -> str:
        key = found.group(0)
        return f"@{names[key]}" if key in names else "@所有人" if key == "@_all" else key
    return _PLACEHOLDER.sub(replace, text)


def _inline(element: Any) -> str:
    if not isinstance(element, dict):
        return ""
    if element.get("tag") == "at":
        return f"@{element['user_name']}" if element.get("user_name") else str(element.get("user_id") or "")
    return str(element.get("text") or "")


def extract_text(raw: dict[str, Any]) -> str:
    msg_type = raw.get("msg_type")
    fallback = f"[{msg_type}]"
    try:
        content = json.loads((raw.get("body") or {}).get("content") or "{}")
        if isinstance(content, dict):
            if isinstance(content.get("text"), str):
                return _fill_mentions(content["text"], raw.get("mentions"))
            if isinstance(content.get("content"), list):
                paragraphs = ["".join(_inline(element) for element in (paragraph or []))
                              for paragraph in content["content"]]
                text = _fill_mentions("\n".join(paragraphs).strip(), raw.get("mentions"))
                return text or fallback
            if content.get("title"):
                return str(content["title"])
    except (ValueError, TypeError, AttributeError):
        pass
    return fallback


def _parse(line: bytes) -> BridgeMessage | None:
    try:
        raw = json.loads(line)
        message_id = raw["message_id"]
        sent_at = _EPOCH + timedelta(milliseconds=int(raw["create_time"]))
    except (ValueError, KeyError, TypeError, OverflowError):
        return None
    if not isinstance(raw, dict) or not isinstance(message_id, str) or not message_id:
        return None
    raw = _strip_nul(raw)
    sender = raw.get("sender") if isinstance(raw.get("sender"), dict) else {}
    kind = {"user": "user", "app": "app"}.get(sender.get("sender_type"), "other")
    mentions = [m for m in raw.get("mentions") or [] if isinstance(m, dict) and isinstance(m.get("id"), str)]
    optional = {key: raw[key] if isinstance(raw.get(key), str) and raw[key] else None
                for key in ("thread_id", "root_id", "parent_id")}
    return BridgeMessage(
        external_id=message_id, sender_id=str(sender.get("id") or ""), sender_kind=kind, sent_at=sent_at,
        msg_type=str(raw.get("msg_type") or ""), mention_ids=[m["id"] for m in mentions],
        mention_names={m["id"]: m["name"] for m in mentions if isinstance(m.get("name"), str) and m["name"]},
        text=_strip_nul(extract_text(raw))[:TEXT_MAX_CHARS], body=raw, body_bytes=len(line), **optional)


def _last_id_before(handle: Any, offset: int) -> str | None:
    """The message id of the last complete non-blank line that ends at or before ``offset``."""
    end, window = offset, 4096
    while end > 0:
        start = max(0, end - window)
        handle.seek(start)
        chunk = handle.read(end - start).rstrip(b"\n")
        if b"\n" in chunk or start == 0:
            message = _parse(chunk.rsplit(b"\n", 1)[-1])
            if message is not None or not chunk.strip():
                return message.external_id if message else None
            return None
        window *= 4
    return None


def scan_file(path: Path, state: FileState | None, *, max_bytes: int = DEFAULT_MAX_BYTES) -> Scan:
    """What is new in ``path`` since ``state`` (None: never read). ``Scan.state`` is the state to save."""
    with path.open("rb") as handle:
        stat = os.fstat(handle.fileno())
        inode, mtime = str(stat.st_ino), _microseconds(stat)
        if state is not None and (inode, stat.st_size, mtime) == (state.inode, state.size, state.mtime):
            return Scan(state=state, unchanged=True)
        start, rescanned = 0, False
        if state is not None:
            rewritten = inode != state.inode or stat.st_size < state.offset or (
                state.offset > 0 and state.last_id is not None and _last_id_before(handle, state.offset) != state.last_id)
            start, rescanned = (0, True) if rewritten else (state.offset, False)
        handle.seek(start)
        data = handle.read(max(max_bytes, 1))
        # A window that ends inside a line is extended to the line's end; a window of no complete line makes no progress.
        while b"\n" not in data and (more := handle.read(_EXTEND)):
            data += more
        end = data.rfind(b"\n") + 1
        scan = Scan(state=FileState(offset=start + end, size=start + len(data), inode=inode, mtime=mtime,
                                    last_id=state.last_id if state is not None and not rescanned else None),
                    rescanned=rescanned)
        last_id = scan.state.last_id if not rescanned and end == 0 else None
        for line in data[:end].split(b"\n")[:-1]:
            if not line.strip():
                continue
            message = _parse(line)
            if message is None:
                scan.bad_lines += 1
                continue
            scan.messages.append(message)
            last_id = message.external_id
        scan.state = FileState(**{**scan.state.__dict__, "last_id": last_id})
        return scan
