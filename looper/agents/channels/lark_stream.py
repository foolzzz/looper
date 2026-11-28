"""Lark's long connection, protobuf frames and flat message events."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import math
import time
from collections import OrderedDict
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from dataclasses import dataclass, field, replace
from typing import Any
from urllib.parse import parse_qs, urlsplit

from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosedOK, WebSocketException

from ..supervisor import cancel_tasks
from .lark_api import LarkApi
from .lark_cli import RECEIVE_EVENT

logger = logging.getLogger(__name__)
_wire_logger = logging.Logger(__name__ + ".wire", level=logging.CRITICAL + 1)
_MAX_BYTES = 16 * 1024 * 1024
# A press of a card's button (the platform's new card callback), on the same connection as the messages; its answer
# (a toast, the card as it is now) goes in the frame's acknowledgement, which the platform waits 3 s for.
CARD_EVENT = "card.action.trigger"
CARD_ANSWER_SECONDS = 2.8


def _varint(value: int) -> bytes:
    if not 0 <= value < 1 << 64:
        raise ValueError("protobuf integer is outside uint64")
    out = bytearray()
    while value >= 128:
        out.append((value & 127) | 128)
        value >>= 7
    out.append(value)
    return bytes(out)


def _read_varint(data: bytes, offset: int) -> tuple[int, int]:
    value = 0
    for shift in range(0, 70, 7):
        if offset >= len(data):
            raise ValueError("truncated protobuf integer")
        byte = data[offset]
        offset += 1
        if shift == 63 and byte > 1:
            raise ValueError("protobuf integer is outside uint64")
        value |= (byte & 127) << shift
        if not byte & 128:
            return value, offset
    raise ValueError("invalid protobuf integer")


def _fields(data: bytes) -> Iterator[tuple[int, int, int | bytes]]:
    offset = 0
    groups: list[int] = []
    while offset < len(data):
        tag, offset = _read_varint(data, offset)
        number, wire = tag >> 3, tag & 7
        if not 0 < number < 1 << 29:
            raise ValueError("invalid protobuf field number")
        if wire == 3:
            groups.append(number)
            continue
        if wire == 4:
            if not groups or groups.pop() != number:
                raise ValueError("invalid protobuf group")
            continue
        if wire == 0:
            value, offset = _read_varint(data, offset)
        elif wire in (1, 2, 5):
            if wire == 2:
                length, offset = _read_varint(data, offset)
            else:
                length = 8 if wire == 1 else 4
            end = offset + length
            if end > len(data):
                raise ValueError("truncated protobuf field")
            value = data[offset:end]
            offset = end
        else:
            raise ValueError("invalid protobuf wire type")
        if not groups:
            yield number, wire, value
    if groups:
        raise ValueError("truncated protobuf group")


def _bytes_field(number: int, value: bytes) -> bytes:
    return _varint(number << 3 | 2) + _varint(len(value)) + value


@dataclass
class Frame:
    """The wire frame used for control, events and their acknowledgements."""

    seq_id: int = 0
    log_id: int = 0
    service: int = 0
    method: int = 0
    headers: list[tuple[str, str]] = field(default_factory=list)
    payload_encoding: str = ""
    payload_type: str = ""
    payload: bytes = b""
    log_id_new: str = ""

    def header(self, name: str) -> str | None:
        return next((value for key, value in self.headers if key == name), None)

    def encode(self) -> bytes:
        out = bytearray()
        for number, value in enumerate((self.seq_id, self.log_id, self.service, self.method), 1):
            if number >= 3:
                if not -(1 << 31) <= value < 1 << 31:
                    raise ValueError("protobuf integer is outside int32")
                value &= (1 << 64) - 1
            out += _varint(number << 3) + _varint(value)
        for key, value in self.headers:
            out += _bytes_field(5, _bytes_field(1, key.encode()) + _bytes_field(2, value.encode()))
        for number, value in ((6, self.payload_encoding.encode()), (7, self.payload_type.encode()),
                              (8, self.payload), (9, self.log_id_new.encode())):
            if value:
                out += _bytes_field(number, value)
        return bytes(out)

    @classmethod
    def decode(cls, data: bytes) -> Frame:
        frame = cls()
        required: set[int] = set()
        names = {1: "seq_id", 2: "log_id", 3: "service", 4: "method", 6: "payload_encoding",
                 7: "payload_type", 8: "payload", 9: "log_id_new"}
        for number, wire, value in _fields(data):
            if number in (1, 2, 3, 4):
                if wire != 0:
                    raise ValueError("invalid protobuf integer field")
                if number >= 3:
                    value &= (1 << 32) - 1
                    if value >= 1 << 31:
                        value -= 1 << 32
                setattr(frame, names[number], value)
                required.add(number)
            elif number == 5:
                if wire != 2:
                    raise ValueError("invalid protobuf header field")
                header: dict[int, str] = {}
                for key, header_wire, text in _fields(value):
                    if key in (1, 2):
                        if header_wire != 2:
                            raise ValueError("invalid protobuf header string")
                        header[key] = text.decode("utf-8")
                if 1 not in header or 2 not in header:
                    raise ValueError("protobuf header is missing required fields")
                frame.headers.append((header[1], header[2]))
            elif number in names:
                if wire != 2:
                    raise ValueError("invalid protobuf payload field")
                setattr(frame, names[number], value if number == 8 else value.decode("utf-8"))
        if required != {1, 2, 3, 4}:
            raise ValueError("protobuf frame is missing required fields")
        return frame


class SeenEvents:
    """Remember the latest 1000 event and message ids across connections."""

    def __init__(self) -> None:
        self._ids: OrderedDict[str, None] = OrderedDict()

    def first(self, *ids: str) -> bool:
        keys = [item for item in ids if item]
        first = not any(item in self._ids for item in keys)
        for item in keys:
            self._ids[item] = None
            self._ids.move_to_end(item)
        while len(self._ids) > 1000:
            self._ids.popitem(last=False)
        return first


@dataclass
class _Parts:
    started: float
    total: int
    parts: dict[int, bytes] = field(default_factory=dict)
    size: int = 0


class _Fragments:
    def __init__(self) -> None:
        self._groups: OrderedDict[str, _Parts] = OrderedDict()
        self._bytes = 0

    def _discard(self, key: str) -> None:
        self._bytes -= self._groups.pop(key).size

    def add(self, frame: Frame) -> bytes | None:
        now = time.monotonic()
        for key, group in list(self._groups.items()):
            if now - group.started >= 5:
                self._discard(key)
        total, seq = int(frame.header("sum") or "1"), int(frame.header("seq") or "0")
        if not 1 <= total <= 64 or not 0 <= seq < total:
            raise ValueError("invalid frame fragment index")
        if total == 1:
            return frame.payload
        if not frame.payload:
            raise ValueError("frame fragment payload is empty")
        key = frame.header("message_id")
        if not key:
            raise ValueError("frame fragments have no message id")
        group = self._groups.get(key)
        if group is None or group.total != total:
            if group is not None:
                self._discard(key)
            group = self._groups[key] = _Parts(now, total)
        added = len(frame.payload) - len(group.parts.get(seq, b""))
        group.parts[seq] = frame.payload
        group.size += added
        self._bytes += added
        if group.size > _MAX_BYTES:
            self._discard(key)
            raise ValueError("assembled frame is too large")
        while len(self._groups) > 32 or self._bytes > 2 * _MAX_BYTES:
            self._discard(next(iter(self._groups)))
        if key not in self._groups:
            return None
        if len(group.parts) != total:
            return None
        self._discard(key)
        return b"".join(group.parts[index] for index in range(total))


def _object(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _attribute(value: str) -> str:
    """Escape resource attributes the way lark-cli renders them."""
    for raw, escaped in (("\\", "\\\\"), ('"', '\\"'), ("\n", "\\n"), ("\r", "\\r"), ("\t", "\\t")):
        value = value.replace(raw, escaped)
    return value


def _resource_tag(kind: str, item: dict[str, Any]) -> str:
    key = item.get("file_key") or ""
    name = item.get("file_name") or key
    attributes = f'key="{_attribute(key)}"'
    if kind != "audio":
        attributes += f' name="{_attribute(name)}"'
    return f"<{kind} {attributes}/>"


def _render(message_type: str, raw_content: str, mentions: list[dict[str, Any]]) -> str:
    """Render non-text events using lark-cli's resource and rich-text notation."""
    try:
        body = _object(json.loads(raw_content))
    except (ValueError, TypeError):
        return ""
    if message_type == "image":
        return f"[Image: {body.get('image_key') or ''}]"
    if message_type in {"file", "audio", "media", "video"}:
        return _resource_tag("video" if message_type in {"media", "video"} else message_type, body)
    if message_type == "sticker":
        return "[Sticker]"
    if message_type == "interactive":
        return _card(body)
    if message_type != "post":
        return f"[{message_type}]"

    original = body
    if "content" not in body and "title" not in body:
        locales = [key for key in ("zh_cn", "en_us", "ja_jp") if isinstance(body.get(key), dict)]
        locales += [key for key in sorted(body) if key not in locales and isinstance(body[key], dict)]
        body = _object(body.get(locales[0])) if locales else {}
    names = {mention["key"]: mention["name"] for mention in mentions if mention["key"]}

    def element(item: Any) -> str:
        item = _object(item)
        text, tag = item.get("text") or "", item.get("tag")
        if tag == "text":
            styles = item.get("style") or []
            for style, before, after in (("bold", "**", "**"), ("italic", "*", "*"),
                                         ("underline", "<u>", "</u>"), ("lineThrough", "~~", "~~")):
                if style in styles:
                    text = before + text + after
            return text
        if tag == "a":
            return f"[{text}]({item.get('href') or ''})"
        if tag == "at":
            user_id = item.get("user_id") or ""
            if user_id in {"@_all", "all"}:
                return '<at user_id="all"></at>'
            return "@" + (item.get("user_name") or names.get(user_id) or user_id)
        if tag == "img":
            return f"![Image]({item.get('image_key') or ''})"
        if tag == "media":
            return f"[Media: {item.get('file_key') or ''}]"
        if tag == "emotion":
            return f":{item.get('emoji_type') or ''}:"
        if tag == "code_block":
            return f"\n```{item.get('language') or ''}\n{text}\n```\n"
        if tag == "hr":
            return "\n---\n"
        return text

    blocks = body.get("content_v2")
    if not isinstance(blocks, list) or not blocks:
        blocks = body.get("content") or []
    lines = [body.get("title") or ""]
    if isinstance(blocks, list):
        lines.extend("".join(element(item) for item in paragraph)
                     for paragraph in blocks if isinstance(paragraph, list))
    text = "\n".join(lines).strip() or "[Rich text message]"
    files = original.get("files") or body.get("files") or []
    if isinstance(files, list):
        for item in files:
            if isinstance(item, dict):
                text += "\n" + _resource_tag("folder" if item.get("is_folder") else "file", item)
    return text


def _card(body: dict[str, Any]) -> str:
    """A card's title and text, as the platform returns a card's content; ``[card message]`` without either."""
    header = _object(body.get("header"))
    title = body.get("title") or _object(header.get("title")).get("content") or ""
    texts: list[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, list):
            for item in node:
                walk(item)
        elif isinstance(node, dict):
            for key, value in node.items():
                if key in {"text", "content"} and isinstance(value, str):
                    if value.strip():
                        texts.append(value.strip())
                elif key not in {"title", "header"} and isinstance(value, (dict, list)):
                    walk(value)

    walk(body.get("elements") if "elements" in body else body.get("body"))
    lines = [line for line in (title if isinstance(title, str) else "", *texts) if line]
    return "\n".join(lines) or "[card message]"


def _flat_event(payload: bytes) -> dict[str, Any] | None:
    data = _object(json.loads(payload))
    header = _object(data.get("header"))
    if header.get("event_type") != RECEIVE_EVENT:
        return None
    event = _object(data.get("event"))
    message, sender = _object(event.get("message")), _object(event.get("sender"))
    mentions = [{"id": _object(mention.get("id")).get("open_id") or "", "key": mention.get("key") or "",
                 "name": mention.get("name") or ""} for mention in message.get("mentions") or []
                if isinstance(mention, dict)]
    content = message.get("content") or ""
    if message.get("message_type") == "text":
        try:
            content = _object(json.loads(content)).get("text") or ""
        except (ValueError, TypeError):
            content = ""
        if not isinstance(content, str):
            content = ""
        for mention in sorted(mentions, key=lambda item: len(item["key"]), reverse=True):
            if mention["key"]:
                content = content.replace(mention["key"], "@" + mention["name"])
    else:
        content = _render(message.get("message_type") or "", content, mentions)
    flat = {"type": RECEIVE_EVENT, "event_id": header.get("event_id") or "", "chat_id": message.get("chat_id") or "",
            "chat_type": message.get("chat_type") or "", "message_id": message.get("message_id") or "",
            "message_type": message.get("message_type") or "", "sender_id": _object(sender.get("sender_id")).get("open_id") or "",
            "sender_type": sender.get("sender_type") or "", "mentions": mentions, "content": content}
    for source, target in (("parent_id", "reply_to"), ("root_id", "root_id"), ("thread_id", "thread_id")):
        if message.get(source):
            flat[target] = message[source]
    return flat


def _card_event(payload: bytes) -> dict[str, Any] | None:
    """A press of a button of Looper's card (schema 2.0 ``card.action.trigger``): who pressed, in which chat and
    card, and the button's ``looper`` value; None for any other event, a card another sender of the app made
    included (it is acknowledged and ignored as before)."""
    try:
        data = _object(json.loads(payload))
    except (ValueError, TypeError):
        return None
    header = _object(data.get("header"))
    if header.get("event_type") != CARD_EVENT:
        return None
    event = _object(data.get("event"))
    context, action = _object(event.get("context")), _object(event.get("action"))
    found = _object(action.get("value")).get("looper")
    if not isinstance(found, str) or not found:
        return None
    return {"type": CARD_EVENT, "event_id": header.get("event_id") or "",
            "operator_id": _object(event.get("operator")).get("open_id") or "",
            "chat_id": context.get("open_chat_id") or "", "message_id": context.get("open_message_id") or "",
            "value": found}


def _card_ack(frame: Frame, start: float, response: dict[str, Any] | None) -> bytes:
    """The acknowledgement of a card press's frame, carrying the answer as the official SDKs do (``data``: the
    answer's JSON, base64)."""
    body: dict[str, Any] = {"code": 200}
    if response is not None:
        body = {"code": 200, "headers": None,
                "data": base64.b64encode(json.dumps(response, ensure_ascii=False).encode()).decode()}
    return replace(frame, headers=[(key, value) for key, value in frame.headers if key != "biz_rt"]
                   + [("biz_rt", str(int((time.monotonic() - start) * 1000)))],
                   payload=json.dumps(body, separators=(",", ":")).encode()).encode()


def _interval(value: Any, default: float = 120) -> float:
    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value > 0:
        return float(value)
    return default


@dataclass
class StreamStats:
    dropped: int = 0


async def _cancel_remaining(tasks: list[asyncio.Task[Any]]) -> None:
    """Cancel ``tasks`` and wait them out, even if this task is cancelled again while it waits.

    Cleanup of the connection runs on a task that is already being cancelled, and the caller cancels it
    again while the children are still ending. That second cancel lands on the next await and would leave
    a child running after it dropped the first one. The wait therefore runs on its own task; cancellation
    of this one is held back until the wait has finished. A cancel that arrived is then raised again, so
    this task stays cancelled and does not also publish the end-of-stream sentinel.
    """
    current = asyncio.current_task()
    finishing = asyncio.ensure_future(cancel_tasks(tasks))
    cancelled = False
    try:
        while not finishing.done():
            if current is not None:
                while current.cancelling():
                    current.uncancel()
                    cancelled = True
            try:
                await asyncio.shield(finishing)
            except asyncio.CancelledError:
                cancelled = True
        finishing.result()
    finally:
        if not finishing.done():
            finishing.cancel()
    if cancelled:
        raise asyncio.CancelledError


async def stream_events(api: LarkApi, *, seen: SeenEvents | None = None,
                        ping_interval: float | None = None,
                        admit: Callable[[dict], bool] | None = None,
                        stats: StreamStats | None = None,
                        on_card: Callable[[dict[str, Any]], Awaitable[dict[str, Any] | None]] | None = None,
                        ) -> AsyncIterator[dict[str, Any]]:
    """Yield messages from one connection, acknowledging frames before delivery. With ``on_card``, a press of a
    card's button is not yielded: its frame is acknowledged with what ``on_card`` answers, within
    ``CARD_ANSWER_SECONDS`` (a plain acknowledgement past it)."""
    url, config = await api.stream_endpoint()
    service = int(parse_qs(urlsplit(url).query)["service_id"][0])
    interval = _interval(ping_interval if ping_interval is not None else config.get("PingInterval"))
    seen = seen if seen is not None else SeenEvents()
    stats = stats if stats is not None else StreamStats()
    events: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue(maxsize=100)
    changed = asyncio.Event()
    answering: set[asyncio.Task[None]] = set()
    told_legacy = False

    async def answer_card(socket: ClientConnection, frame: Frame, start: float, card: dict[str, Any]) -> None:
        response = None
        if on_card is not None and seen.first(card["event_id"]):
            try:
                response = await asyncio.wait_for(on_card(card), CARD_ANSWER_SECONDS)
            except Exception as exc:
                logger.warning("Lark stream: a card press was not answered: %s", type(exc).__name__)
        try:
            await socket.send(_card_ack(frame, start, response))
        except (OSError, WebSocketException):
            logger.debug("Lark stream could not answer a card press")

    async def ping(socket: ClientConnection) -> None:
        while True:
            changed.clear()
            try:
                await asyncio.wait_for(changed.wait(), timeout=interval)
            except TimeoutError:
                await socket.send(Frame(service=service, headers=[("type", "ping")]).encode())

    async def receive(socket: ClientConnection) -> None:
        nonlocal interval, told_legacy
        fragments = _Fragments()
        while True:
            try:
                raw = await asyncio.wait_for(socket.recv(), timeout=3 * interval)
            except ConnectionClosedOK:
                return
            except TimeoutError:
                raise ConnectionError("the Lark stream received no frames within three ping intervals") from None
            start = time.monotonic()
            try:
                if not isinstance(raw, bytes):
                    raise ValueError("expected a binary frame")
                frame = Frame.decode(raw)
                kind = frame.header("type")
                if frame.method == 0:
                    if kind == "pong" and frame.payload and ping_interval is None:
                        config = _object(json.loads(frame.payload))
                        interval = _interval(config.get("PingInterval"), interval)
                        changed.set()
                    continue
                if frame.method != 1:
                    logger.debug("Lark stream ignored an unknown frame method")
                    continue
                assembled, invalid, payload = False, False, None
                if kind == "event" and on_card is not None:
                    # A card press is answered in its acknowledgement, so it is read before acknowledging.
                    assembled = True
                    try:
                        payload = fragments.add(frame)
                    except ValueError:
                        invalid = True
                    card = _card_event(payload) if payload is not None else None
                    if card is not None:
                        task = asyncio.create_task(answer_card(socket, frame, start, card))
                        answering.add(task)
                        task.add_done_callback(answering.discard)
                        continue
                ack = replace(frame, headers=[(key, value) for key, value in frame.headers if key != "biz_rt"]
                              + [("biz_rt", str(int((time.monotonic() - start) * 1000)))], payload=b'{"code":200}')
                await socket.send(ack.encode())
                if kind != "event":
                    if kind == "card" and not told_legacy:
                        told_legacy = True
                        logger.info("Lark stream ignored a frame of the old card callback; card buttons are "
                                    "answered as %s events", CARD_EVENT)
                    logger.debug("Lark stream ignored a non-event data frame")
                    continue
                if invalid:
                    raise ValueError("invalid frame fragment")
                if not assembled:
                    payload = fragments.add(frame)
                if payload is None:
                    continue
                event = _flat_event(payload)
                if event is None:
                    logger.debug("Lark stream ignored a non-message event")
                elif seen.first(event["event_id"], event["message_id"]):
                    if admit is not None:
                        event["admitted"] = admit(event)
                    if event.get("admitted") is False:
                        event = {key: event[key] for key in (
                            "type", "event_id", "message_id", "chat_id", "chat_type", "sender_id",
                            "sender_type", "mentions", "message_type", "admitted")}
                        event["content"] = ""
                    try:
                        events.put_nowait(event)
                    except asyncio.QueueFull:
                        stats.dropped += 1
                        if stats.dropped == 1 or stats.dropped % 100 == 0:
                            logger.warning("Lark stream event queue is full; dropped %d events", stats.dropped)
                else:
                    logger.debug("Lark stream ignored a duplicate event")
            except (ValueError, TypeError, AttributeError):
                logger.debug("Lark stream ignored an invalid frame")

    async def run() -> None:
        tasks: list[asyncio.Task[None]] = []
        try:
            async with connect(url, proxy=None, max_size=_MAX_BYTES, open_timeout=20, ping_interval=None,
                               logger=_wire_logger) as socket:
                logger.info("Lark stream connected")
                tasks = [asyncio.create_task(receive(socket)), asyncio.create_task(ping(socket))]
                done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    task.result()
        except (OSError, WebSocketException, TimeoutError) as exc:
            raise ConnectionError(f"the Lark stream connection failed: {type(exc).__name__}") from None
        finally:
            try:
                await _cancel_remaining([*tasks, *answering])
            finally:
                logger.info("Lark stream disconnected")
            if not asyncio.current_task().cancelling():
                await events.put(None)

    connection = asyncio.create_task(run())
    try:
        while (event := await events.get()) is not None:
            yield event
        await connection
    finally:
        await cancel_tasks([connection], raise_errors=True)
