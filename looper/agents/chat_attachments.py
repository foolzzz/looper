"""Private, content-checked files carried by a Manager chat message."""

from __future__ import annotations

import codecs
from collections.abc import Sequence
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta
from enum import Enum
import logging
import mimetypes
import os
from pathlib import Path
import re
import shutil
from typing import Any
import unicodedata

from .runtimes import ImageInput

logger = logging.getLogger(__name__)
ROOT_DIR = "chat-attachments"
IMAGE_MAX_BYTES = FILE_MAX_BYTES = 20 * 1024 * 1024
PER_MESSAGE_MAX = 10
WEB_TOTAL_MAX_BYTES = 6_000_000
RETENTION = timedelta(days=7)


class TooLarge(Exception):
    """A transport stopped a resource download at the attachment size limit."""


class AttachmentKind(str, Enum):
    IMAGE = "image"
    FILE = "file"


class AttachmentState(str, Enum):
    SAVED = "saved"
    TOO_LARGE = "too_large"
    NOT_ACCEPTED = "not_accepted"
    FAILED = "failed"
    OVER_LIMIT = "over_limit"


class FileClass(str, Enum):
    IMAGE = "image"
    PDF = "pdf"
    OFFICE = "office"
    TEXT = "text"


@dataclass(frozen=True)
class Attachment:
    index: int
    kind: AttachmentKind
    state: AttachmentState
    name: str
    source: str
    path: str | None = None
    mime: str | None = None
    size: int | None = None
    reason: str | None = None
    told: bool = False

    def to_payload(self) -> dict[str, Any]:
        return {key: value for key, value in asdict(self).items() if value is not None}


def root(runtime_root: Path) -> Path:
    return runtime_root / ROOT_DIR


def message_dir(runtime_root: Path, source: str, message_id: str) -> Path:
    def safe_id(value: str) -> str:
        return re.sub(r"[^A-Za-z0-9_-]", "_", value)[:100] or "_"
    directory = root(runtime_root)
    for part in (directory, directory / safe_id(source)):
        if part.is_symlink():
            raise ValueError("attachment directories must not be symlinks")
        part.mkdir(mode=0o700, parents=True, exist_ok=True)
        part.chmod(0o700)
    directory = directory / safe_id(source) / safe_id(message_id)
    if directory.is_symlink():
        directory.unlink()
    elif directory.exists():
        shutil.rmtree(directory)
    directory.mkdir(mode=0o700)
    directory.chmod(0o700)
    return directory


def safe_name(raw: str, fallback: str) -> str:
    name = re.split(r"[/\\]", raw)[-1]
    name = "".join(c if c.isalnum() or c in "._- " else "_" for c in name
                   if not unicodedata.category(c).startswith("C")).lstrip(". ")
    suffix = Path(name).suffix
    if len(name) > 100:
        name = name[:100 - len(suffix)] + suffix if len(suffix) < 100 else name[:100]
    return name or fallback


def _classify(path: Path, name: str) -> tuple[FileClass, str] | None:
    with path.open("rb") as stream:
        head = stream.read(65536)
    for magic, mime in ((b"\x89PNG\r\n\x1a\n", "image/png"), (b"\xff\xd8\xff", "image/jpeg"),
                        (b"GIF87a", "image/gif"), (b"GIF89a", "image/gif")):
        if head.startswith(magic):
            return FileClass.IMAGE, mime
    if head.startswith(b"RIFF") and head[8:12] == b"WEBP":
        return FileClass.IMAGE, "image/webp"
    if head.startswith(b"%PDF-"):
        return FileClass.PDF, "application/pdf"
    suffix = Path(name).suffix.lower()
    if head.startswith(b"PK\x03\x04"):
        office = {".docx": "wordprocessingml.document", ".xlsx": "spreadsheetml.sheet", ".pptx": "presentationml.presentation"}
        return (FileClass.OFFICE, "application/vnd.openxmlformats-officedocument." + office[suffix]) if suffix in office else None
    if b"\0" in head or head.startswith((b"MZ", b"\x7fELF")):
        return None
    try:
        codecs.getincrementaldecoder("utf-8")().decode(head, final=len(head) < 65536)
    except UnicodeDecodeError:
        return None
    mime = mimetypes.guess_type(name)[0] or ("text/markdown" if suffix in {".md", ".markdown"} else "text/plain")
    return FileClass.TEXT, mime if mime.startswith("text/") or mime == "application/json" else "text/plain"


def classify(path: Path) -> tuple[FileClass, str] | None:
    return _classify(path, path.name)


def accept(part: Path, *, index: int, kind: AttachmentKind, name: str, source: str, max_bytes: int) -> Attachment:
    item = Attachment(index, kind, AttachmentState.SAVED, safe_name(name, kind.value), source)
    size = part.stat().st_size
    classified = _classify(part, item.name) if size <= max_bytes else None
    if size > max_bytes:
        item = replace(item, state=AttachmentState.TOO_LARGE)
    elif classified is None or (kind is AttachmentKind.IMAGE and classified[0] is not FileClass.IMAGE):
        item = replace(item, state=AttachmentState.NOT_ACCEPTED, reason=(
            "images must be PNG, JPEG, GIF or WEBP" if kind is AttachmentKind.IMAGE
            else "only images, text, PDF and Office files are accepted"))
    if item.state is not AttachmentState.SAVED:
        part.unlink()
        return item
    file_class, mime = classified
    name = item.name
    if mime != mimetypes.guess_type(name)[0]:
        name += mimetypes.guess_extension(mime) or ".txt"
    target = part.with_name(f"{index}-{name}")
    part.rename(target)
    target.chmod(0o600)
    return replace(item, kind=AttachmentKind.IMAGE if file_class is FileClass.IMAGE else AttachmentKind.FILE,
                   path=str(target.resolve()), mime=mime, size=size)


def store_upload(runtime_root: Path, source: str, upload_id: str, items: list[tuple[str, bytes]]) -> list[Attachment]:
    directory = message_dir(runtime_root, source, upload_id)
    try:
        saved = []
        for index, (name, content) in enumerate(items, 1):
            part = directory / f"{index}.part"
            with os.fdopen(os.open(part, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as stream:
                stream.write(content)
            saved.append(accept(part, index=index, kind=AttachmentKind.FILE, name=name, source=source, max_bytes=FILE_MAX_BYTES))
        return saved
    except Exception:
        shutil.rmtree(directory)
        raise


def from_payload(raw: Any, runtime_root: Path) -> list[Attachment]:
    if not isinstance(raw, list):
        return []
    attachments = []
    for value in raw:
        try:
            if not isinstance(value, dict) or type(value.get("index")) is not int or not isinstance(value.get("name"), str):
                raise ValueError("invalid index or name")
            item = Attachment(value["index"], AttachmentKind(value.get("kind")), AttachmentState(value.get("state")),
                              value["name"], str(value.get("source", "")),
                              **{key: value[key] for key in ("path", "mime", "size", "reason", "told") if key in value})
            if item.state is AttachmentState.SAVED:
                try:
                    path = Path(item.path).resolve(strict=True)
                    if not path.is_relative_to(root(runtime_root).resolve()) or not path.is_file():
                        raise ValueError("outside attachment storage or not a file")
                    item = replace(item, path=str(path))
                except (OSError, ValueError, TypeError, RuntimeError):
                    item = replace(item, state=AttachmentState.FAILED, path=None, reason="no longer available")
            attachments.append(item)
        except (TypeError, ValueError):
            logger.warning("ignoring malformed chat attachment")
    return sorted(attachments, key=lambda item: item.index)


def section(attachments: Sequence[Attachment], image_input: ImageInput) -> list[str]:
    if not attachments:
        return []
    lines = ["Attachments sent with this message (kept on this machine for 7 days; never put their paths in a reply):"]
    for item in attachments:
        name = item.name.replace('"', "'")
        line = f'  {item.index}. {item.kind.value} "{name}"'
        if item.state is AttachmentState.SAVED:
            size = item.size or 0
            label = f"{size} B" if size < 1024 else f"{size / 1024:.1f} KB" if size < 1024**2 else f"{size / 1024**2:.1f} MB"
            line += f" ({item.mime}, {label}): {item.path}"
        else:
            reason = {AttachmentState.TOO_LARGE: "larger than 20 MB", AttachmentState.OVER_LIMIT: "more than 10 attachments"}.get(
                item.state, item.reason or "the download failed")
            line += f": not received ({reason})" + ("; the user was told" if item.told else "")
        lines.append(line)
    lines.append("Open one with your file-reading tool when the message needs it." if image_input is ImageInput.READ_TOOL else
                 "Open a file with your file-reading tool when the message needs it; your runtime cannot view images, "
                 "so if the user asks what an image shows, say you cannot see it.")
    return lines


def prune(runtime_root: Path, now: datetime) -> int:
    directory = root(runtime_root)
    if directory.is_symlink():
        directory.unlink()
        return 0
    if not directory.exists():
        return 0
    removed = 0
    for source in directory.iterdir():
        try:
            if source.is_symlink():
                source.unlink()
                continue
            if not source.is_dir():
                continue
            for message in source.iterdir():
                try:
                    if message.is_symlink():
                        message.unlink()
                    elif message.is_dir() and message.stat().st_mtime < (now - RETENTION).timestamp():
                        shutil.rmtree(message)
                        removed += 1
                except OSError:
                    logger.warning("could not prune chat attachment directory", exc_info=True)
        except OSError:
            logger.warning("could not prune chat attachment source", exc_info=True)
    return removed


def redact(text: str, runtime_root: Path, *, paths: Sequence[str] = ()) -> str:
    prefix = str(root(runtime_root).resolve()) + "/"
    known = {path for path in paths if isinstance(path, str) and path.startswith(prefix)}
    # Posts can refer to files from earlier turns. Inspect only directories named in the text.
    for found in re.finditer(re.escape(prefix) + r"[A-Za-z0-9_-]+/[A-Za-z0-9_-]+/", text):
        directory = Path(found[0])
        try:
            if directory.resolve().is_relative_to(root(runtime_root).resolve()):
                known.update(str(path) for path in directory.iterdir() if path.is_file())
        except OSError:
            pass
    for path in sorted(known, key=len, reverse=True):
        text = text.replace(path, "[attachment]")
    return re.sub(re.escape(prefix) + r'''[^\s)\]`"']*''', "[attachment]", text)
