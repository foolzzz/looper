"""Normalize lark-cli's rendered inbound content for the shared Lark adapter."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Any


class ContentKind(str, Enum):
    TEXT = "text"
    RICH = "rich"
    # A merged forward: no words of the sender's own; the forwarded messages are read as a quote (``lark_quote``).
    FORWARD = "forward"
    UNSUPPORTED = "unsupported"


RICH_TYPES = frozenset({"post", "image", "file"})
UNSUPPORTED_REPLY = "暂不支持这类飞书消息（{kind}），请发文字、富文本、图片或文件。"


class ResourceKind(str, Enum):
    IMAGE = "image"
    FILE = "file"


@dataclass(frozen=True)
class ResourceRef:
    index: int
    kind: ResourceKind
    key: str
    name: str = ""
    # The message it is downloaded through, when not the one received: a quoted message. A merged forward's raw
    # read gives each child the keys of its own message, so a child's resource comes through the child's id;
    # lark-cli's rendering of a forward shows the forward's own copies, which come through the forward's id.
    message_id: str = ""


@dataclass(frozen=True)
class Inbound:
    kind: ContentKind
    text: str
    resources: tuple[ResourceRef, ...] = ()


def placeholder(ref: ResourceRef) -> str:
    return f"[image {ref.index}]" if ref.kind is ResourceKind.IMAGE else f"[file {ref.index}: {ref.name}]"


def _without_mentions(text: str, mentions: list[dict[str, Any]]) -> str:
    """Keep the existing plain-text mention removal and space normalization."""
    for found in mentions:
        shown = next((form for form in (f"@{found.get('name') or ''}", str(found.get("key") or ""))
                      if len(form) > 1 and form in text), None)
        if shown is not None:
            text = text.replace(shown, "", 1)
    return re.sub(r" {2,}", " ", text).strip()


_RESOURCES = re.compile(
    r'!\[Image\]\((?P<inline_image>img_[A-Za-z0-9_-]+)\)'
    r'|\[Image: (?P<image>img_[A-Za-z0-9_-]+)\]'
    r'|<file key="(?P<file>file_[A-Za-z0-9_-]+)"(?: name="(?P<name>(?:[^"\\]|\\.)*)")?/>'
    r'|(?P<video>\[Media: [^\]]*\]|<video [^>]*/>)'
    r'|(?P<audio><audio [^>]*/>)'
    r'|(?P<folder><folder [^>]*/>|<folder [^>]*>.*?</folder>)',
    re.DOTALL,
)


def parse(message_type: str, content: str, bot_mentions: list[dict[str, Any]], *,
          after: int = 0, message_id: str = "") -> Inbound:
    """``after``: how many resources come before these (they are numbered on from it); ``message_id``: the message
    they are downloaded through (``ResourceRef.message_id``)."""
    if message_type == "text":
        return Inbound(ContentKind.TEXT, _without_mentions(content, bot_mentions))
    if message_type == "merge_forward":
        return Inbound(ContentKind.FORWARD, "")
    if message_type not in RICH_TYPES:
        return Inbound(ContentKind.UNSUPPORTED, "")

    bot_ids = {mention.get("id") for mention in bot_mentions if mention.get("id")}

    def at(found: re.Match[str]) -> str:
        user_id, name = found[1], found[2]
        if user_id in bot_ids:
            return ""
        return "@" + ("all" if user_id == "all" else name or user_id) + found[3]

    text = re.sub(r'<at user_id="([^"]*)">([^<]*)</at>( ?)', at, content)
    for mention in bot_mentions:
        name, key = mention.get("name") or "", mention.get("key") or ""
        shown = next((form for form in (f"@@{name}" if name else "", f"@{name}" if name else "",
                                       f"@@{key}" if key else "", key)
                      if len(form) > 1 and form in text), None)
        if shown is not None:
            text = re.sub(re.escape(shown) + " ?", "", text, count=1)

    resources: list[ResourceRef] = []

    def resource(found: re.Match[str]) -> str:
        index = after + len(resources) + 1
        if key := found["inline_image"] or found["image"]:
            ref = ResourceRef(index, ResourceKind.IMAGE, key, message_id=message_id)
        elif key := found["file"]:
            name = re.sub(r"\\(.)", lambda char: " " if char[1] in "nrt" else char[1], found["name"] or "")
            ref = ResourceRef(index, ResourceKind.FILE, key, name or key, message_id=message_id)
        else:
            kind = "video" if found["video"] else "voice message" if found["audio"] else "folder"
            return f"[{kind}: not supported yet]"
        resources.append(ref)
        return placeholder(ref)

    return Inbound(ContentKind.RICH, _RESOURCES.sub(resource, text).strip(), tuple(resources))
