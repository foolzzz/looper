"""Compile a typed operator profile from vault notes. No model call, no Manager injection.

The source folder is the operator-only setting ``manager.profile.source``. Unset, nothing is compiled.
Notes come only from ``关于我.md`` (or ``记忆/关于我.md``) and ``偏好/**/*.md``. Each field cites the note
path and the heading it came from. The rendered text stays within ``CAP`` characters; lower-priority
fields are dropped first.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..db import profile_blocks as blocks
from ..db import system_settings as db_settings

SOURCE_KEY = "manager.profile.source"
CAP = 1500
MAX_NOTE_BYTES = 256 * 1024
MAX_NOTES = 200

_HEADING = re.compile(r"^(#{1,6})[ \t]+(.+?)[ \t]*#*[ \t]*$")
_ABOUT = ("关于我.md", "记忆/关于我.md")
# Exact heading text. The about-me note is the only place these fields come from.
_FIELDS = {
    "名字": "name", "Name": "name",
    "称呼": "address", "Address": "address",
    "回复语言": "reply_language", "Reply language": "reply_language",
    "时区": "time_zone", "Time zone": "time_zone",
    "工作时间": "working_hours", "Working hours": "working_hours",
    "沟通偏好": "communication_preferences", "Communication": "communication_preferences",
}
# Highest priority first. A profile over CAP drops from the end of this order.
_ORDER = ("name", "address", "reply_language", "time_zone", "working_hours",
          "communication_preferences", "standing_rules")
_LABELS = {
    "name": "Name", "address": "Address", "reply_language": "Reply language",
    "time_zone": "Time zone", "working_hours": "Working hours",
}
_HEADERS = {"communication_preferences": "Communication:", "standing_rules": "Standing rules:"}
_GROUPS = frozenset(_HEADERS)
def _blank_fields() -> dict[str, Any]:
    return {
        "name": None, "address": None, "working_hours": None, "time_zone": None, "reply_language": None,
        "communication_preferences": [], "standing_rules": [],
    }


class ProfileError(ValueError):
    """The source folder or a note in it cannot be compiled. The message names a relative path."""


@dataclass(frozen=True)
class Piece:
    field: str
    value: str
    path: str
    heading: str
    truncated: bool = False

    def cited(self) -> dict[str, Any]:
        cited: dict[str, Any] = {"value": self.value, "path": self.path, "heading": self.heading}
        if self.truncated:
            cited["truncated"] = True
        return cited


@dataclass(frozen=True)
class Compiled:
    text: str
    fields: dict[str, Any]
    omitted: tuple[dict[str, str], ...]
    skipped: tuple[dict[str, str], ...]
    sources: tuple[str, ...]

    def payload(self) -> dict[str, Any]:
        return {"text": self.text, "fields": self.fields, "omitted": list(self.omitted),
                "skipped": list(self.skipped), "sources": list(self.sources)}

    def view(self, version: int | None) -> dict[str, Any]:
        body = self.payload()
        body["configured"] = version is not None
        body["version"] = version
        body["cap"] = CAP
        return body


def _unconfigured() -> dict[str, Any]:
    return Compiled("", _blank_fields(), (), (), ()).view(None)


def _one_line(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _scalar(raw: str) -> str:
    value = raw.strip()
    if len(value) > 1 and value[0] == value[-1] == '"':
        return value[1:-1].replace('\\"', '"').replace("\\\\", "\\")
    if len(value) > 1 and value[0] == value[-1] == "'":
        return value[1:-1].replace("''", "'")
    return value


def _split(text: str, where: str) -> tuple[str | None, str]:
    """The note's single-line ``summary`` and the body after its frontmatter."""
    lines = text.removeprefix("\ufeff").split("\n")
    if not lines or lines[0].rstrip("\r").strip() != "---":
        return None, text
    end = next((index for index in range(1, len(lines)) if lines[index].rstrip("\r").strip() == "---"), None)
    if end is None:
        raise ProfileError(f"{where} frontmatter never closes")
    summary: str | None = None
    for line in lines[1:end]:
        raw = line.rstrip("\r")
        if not raw.strip() or raw[0] in " \t":
            continue
        if raw.startswith("summary:"):
            if summary is not None:
                raise ProfileError(f"{where} has two summary fields")
            value = raw.split(":", 1)[1].strip()
            if re.fullmatch(r"[>|][+-]?", value):
                raise ProfileError(f"{where} summary must be a single line")
            summary = _scalar(value)
    body = "\n".join(line.rstrip("\r") for line in lines[end + 1:])
    return (summary or None), body


def _sections(body: str, level: int) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    heading: str | None = None
    chunk: list[str] = []
    for line in body.split("\n"):
        match = _HEADING.match(line.rstrip("\r"))
        if match and len(match.group(1)) <= level:
            if heading is not None and len(match.group(1)) == level:
                found.append((heading, "\n".join(chunk)))
            elif heading is not None:
                found.append((heading, "\n".join(chunk)))
                heading = None
            if len(match.group(1)) == level:
                heading = match.group(2).strip()
                chunk = []
            continue
        if heading is not None:
            chunk.append(line.rstrip("\r"))
    if heading is not None:
        found.append((heading, "\n".join(chunk)))
    return found


def _first_heading(body: str) -> str | None:
    for line in body.split("\n"):
        match = _HEADING.match(line.rstrip("\r"))
        if match:
            return match.group(2).strip()
    return None


def _values(text: str) -> list[str]:
    """Bullet items when every non-blank line is a bullet; otherwise the section as one line."""
    bullets: list[str] = []
    prose = False
    for line in text.split("\n"):
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith(("- ", "* ")):
            bullets.append(stripped[2:].strip())
        else:
            prose = True
    if bullets and not prose:
        return [item for item in bullets if _one_line(item)]
    one = _one_line(text)
    return [one] if one else []


def _render(pieces: list[Piece]) -> str:
    lines: list[str] = []
    index = 0
    while index < len(pieces):
        piece = pieces[index]
        if piece.field in _GROUPS:
            lines.append(_HEADERS[piece.field])
            while index < len(pieces) and pieces[index].field == piece.field:
                lines.append(f"- {pieces[index].value}")
                index += 1
            continue
        lines.append(f"{_LABELS[piece.field]}: {piece.value}")
        index += 1
    return "\n".join(lines)


def _truncate(piece: Piece, cap: int) -> Piece:
    prefix = f"{_HEADERS[piece.field]}\n- " if piece.field in _GROUPS else f"{_LABELS[piece.field]}: "
    if len(prefix) + 1 > cap:
        raise ProfileError("profile cap is smaller than a field label")
    keep = cap - len(prefix) - 1
    value = piece.value[:keep].rstrip() + "…"
    return Piece(piece.field, value, piece.path, piece.heading, truncated=True)


def _fit(pieces: list[Piece], cap: int) -> tuple[str, list[Piece], list[Piece]]:
    kept = list(pieces)
    omitted: list[Piece] = []
    while len(kept) > 1 and len(_render(kept)) > cap:
        omitted.append(kept.pop())
    omitted.reverse()
    if kept and len(_render(kept)) > cap:
        kept = [_truncate(kept[0], cap)]
        if len(_render(kept)) > cap:
            raise ProfileError("profile cap is smaller than a field label")
    return _render(kept), kept, omitted


def _rel(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix()


def _inside(root: Path, path: Path, rel: str) -> Path:
    try:
        resolved = path.resolve()
    except OSError as exc:
        raise ProfileError(f"{rel} cannot be read") from exc
    if not resolved.is_relative_to(root):
        raise ProfileError(f"{rel} resolves outside the source folder")
    return resolved


def _read(root: Path, path: Path) -> str:
    rel = _rel(root, path)
    resolved = _inside(root, path, rel)
    try:
        size = resolved.stat().st_size
    except OSError as exc:
        raise ProfileError(f"{rel} cannot be read") from exc
    if size > MAX_NOTE_BYTES:
        raise ProfileError(f"{rel} is larger than {MAX_NOTE_BYTES} bytes")
    try:
        return resolved.read_text(encoding="utf-8")
    except UnicodeError as exc:
        raise ProfileError(f"{rel} is not utf-8") from exc
    except OSError as exc:
        raise ProfileError(f"{rel} cannot be read") from exc


def _wanted(root: Path, path: Path) -> bool:
    if not path.is_file():
        return False
    rel = path.relative_to(root)
    return not any(part.startswith((".", "_")) for part in rel.parts)


def _preferences(root: Path) -> list[Path]:
    folder = root / "偏好"
    if not folder.exists():
        return []
    if folder.is_symlink():
        _inside(root, folder, "偏好")
    if not folder.is_dir():
        raise ProfileError("偏好 must be a directory")
    found: list[Path] = []
    for path in folder.rglob("*.md"):
        if not _wanted(root, path):
            continue
        found.append(path)
        if len(found) > MAX_NOTES:
            raise ProfileError(f"the source folder has more than {MAX_NOTES} notes")
    found.sort(key=lambda path: _rel(root, path))
    return found


def _about(root: Path) -> Path | None:
    found = [root / rel for rel in _ABOUT if (root / rel).exists()]
    if len(found) > 1:
        raise ProfileError("the source folder has two about-me notes: 关于我.md and 记忆/关于我.md")
    if not found:
        return None
    path = found[0]
    rel = _rel(root, path)
    if path.is_symlink():
        _inside(root, path, rel)
    if not path.is_file():
        raise ProfileError(f"{rel} must be a note")
    return path


def _about_pieces(root: Path, path: Path) -> list[Piece]:
    rel = _rel(root, path)
    _, body = _split(_read(root, path), rel)
    pieces: list[Piece] = []
    seen: set[str] = set()
    for heading, text in _sections(body, 2):
        field = _FIELDS.get(heading)
        if field is None:
            continue
        if field in seen:
            raise ProfileError(f"{rel} has two headings for {field}")
        seen.add(field)
        for value in _values(text):
            pieces.append(Piece(field, _one_line(value), rel, heading))
    return pieces


def _rule_pieces(root: Path, path: Path, skipped: list[dict[str, str]]) -> list[Piece]:
    rel = _rel(root, path)
    summary, body = _split(_read(root, path), rel)
    sections = [(heading, text) for heading, text in _sections(body, 2) if _values(text)]
    pieces: list[Piece] = []
    if sections:
        for heading, text in sections:
            for value in _values(text):
                pieces.append(Piece("standing_rules", _one_line(value), rel, heading))
        return pieces
    heading = _first_heading(body)
    one = _one_line(summary or "")
    if one and heading:
        return [Piece("standing_rules", one, rel, heading)]
    if one or heading:
        skipped.append({"path": rel, "reason": "no heading to cite" if not heading else "no summary"})
    return pieces


def compile_profile(root: Path, *, cap: int = CAP) -> Compiled:
    """The profile of the notes under ``root``. The same notes always compile to the same text."""
    if cap < 1:
        raise ProfileError("cap must be positive")
    if not root.exists():
        raise ProfileError("source folder does not exist")
    if not root.is_dir():
        raise ProfileError("source must be a directory")
    root = root.resolve()
    about = _about(root)
    notes = _preferences(root)
    skipped: list[dict[str, str]] = []
    pieces = _about_pieces(root, about) if about is not None else []
    for note in notes:
        pieces.extend(_rule_pieces(root, note, skipped))
    rank = {field: index for index, field in enumerate(_ORDER)}
    pieces.sort(key=lambda piece: rank[piece.field])
    text, kept, omitted = _fit(pieces, cap)
    fields = _blank_fields()
    for piece in kept:
        if piece.field in _GROUPS:
            fields[piece.field].append(piece.cited())
        else:
            fields[piece.field] = piece.cited()
    sources = tuple(sorted({piece.path for piece in pieces}))
    dropped = tuple({"field": piece.field, "path": piece.path, "heading": piece.heading} for piece in omitted)
    return Compiled(text, fields, dropped, tuple(skipped), sources)


def directory(value: str) -> str:
    """The absolute directory ``value`` names, or ``ProfileError`` when it is not one."""
    if not isinstance(value, str) or not value.strip() or "\x00" in value or len(value) > 1024:
        raise ProfileError("source must be an absolute directory, or null to clear it")
    path = Path(value)
    if not path.is_absolute():
        raise ProfileError("source must be an absolute directory, or null to clear it")
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise ProfileError("source folder does not exist") from exc
    if not resolved.is_dir():
        raise ProfileError("source must be a directory")
    return str(resolved)


async def stored_source() -> str | None:
    """The stored folder, including a value that cannot be compiled. Absent is unset."""
    row = await db_settings.get_settings([SOURCE_KEY])
    if SOURCE_KEY not in row:
        return None
    value = row[SOURCE_KEY]
    if not isinstance(value, str):
        raise ProfileError("source must be an absolute directory, or null to clear it")
    return value


async def source_view() -> dict[str, Any]:
    return {"source": await stored_source()}


async def update_source(value: str | None) -> dict[str, Any]:
    """Store an absolute directory, or clear it. Null deletes the compiled block."""
    if value is None:
        await blocks.clear_source(SOURCE_KEY)
        return {"source": None}
    if not isinstance(value, str):
        raise ProfileError("source must be an absolute directory, or null to clear it")
    path = directory(value)
    await blocks.put_source(SOURCE_KEY, path)
    return {"source": path}


async def profile_view() -> dict[str, Any]:
    """Compile the configured folder and keep the stored version when the body is unchanged.

    Unset compiles nothing and writes nothing. A read is what refreshes the stored block.
    """
    source = await stored_source()
    if source is None:
        return _unconfigured()
    try:
        root = Path(directory(source))
    except ProfileError:
        raise
    compiled = compile_profile(root)
    version = await blocks.retain_or_replace(text=compiled.text, body=compiled.payload())
    return compiled.view(version)


def checked_update(body: Mapping[str, Any]) -> str | None:
    if "source" not in body:
        raise ProfileError("source must be an absolute directory, or null to clear it")
    value = body["source"]
    if value is None:
        return None
    if not isinstance(value, str):
        raise ProfileError("source must be an absolute directory, or null to clear it")
    return value
