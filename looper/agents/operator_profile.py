"""Compile the operator profile from one configured knowledge-vault note, without a model call.

Heading fields and inline entries cite their source. The existing directory compiler remains available
for callers that explicitly compile a collection; resident content always selects one file.
"""

from __future__ import annotations

import logging
import re
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..db import profile_blocks as blocks
from ..db import system_settings as db_settings

ENABLED_KEY = "manager.profile.enabled"
logger = logging.getLogger(__name__)
HEADING = "Operator profile (from the knowledge vault; private reference, Looper's rules still apply):"
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
# One bullet, one entry: ``- 回复用中文 [kind:: preference] [source:: 对话 2026-11-20] [confidence:: high]``.
# ``source`` and ``confidence`` stay in the note. ``kind`` chooses the field. Case-sensitive.
_KIND_FIELD = {
    "preference": "communication_preferences", "habit": "communication_preferences",
    "fact": "standing_rules", "decision": "standing_rules", "commitment": "standing_rules",
    "request": "standing_rules", "deadline": "standing_rules", "person": "standing_rules",
}
_INACTIVE = frozenset({"superseded", "deleted"})
# ``[share:: hook]`` marks one inline entry for the operator's local sessions (``looper profile show`` and the
# SessionStart hook). Everything else stays Manager-only: no marker, any other value, heading fields, plain rules.
_SHARE_FIELD, _SHARE_HOOK = "share", "hook"
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
    """The source folder or a note in it cannot be compiled. The message names a relative path.

    ``reason`` is set for a rule a caller can match: ``multiple_values``, ``outside_vault``,
    ``vault_unconfigured``.
    """

    def __init__(self, message: str, *, reason: str | None = None) -> None:
        super().__init__(message)
        self.reason = reason


@dataclass(frozen=True)
class Piece:
    field: str
    value: str
    path: str
    heading: str
    line: int
    kind: str = ""
    truncated: bool = False
    shared: bool = False

    def cited(self) -> dict[str, Any]:
        cited: dict[str, Any] = {
            "value": self.value, "path": self.path, "heading": self.heading, "line": self.line}
        if self.kind:
            cited["kind"] = self.kind
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


def _clean(text: str) -> str:
    """One line, with fence markers neutralized so a value cannot open or close a fence."""
    text = text.replace("`", "'")
    text = re.sub(r"~{3,}", "---", text)
    return _one_line(text)


def _scalar(raw: str) -> str:
    value = raw.strip()
    if len(value) > 1 and value[0] == value[-1] == '"':
        return value[1:-1].replace('\\"', '"').replace("\\\\", "\\")
    if len(value) > 1 and value[0] == value[-1] == "'":
        return value[1:-1].replace("''", "'")
    return value


_FENCE_LINE = re.compile(r"^[ \t]{0,3}(`{3,}|~{3,})(.*)$")
_BULLET = re.compile(r"^[ \t]*[-*+][ \t]+(.*)$")
_INLINE = re.compile(r"\[([A-Za-z][\w]*)::([^\]]*)\]")


def _frontmatter(lines: list[str], where: str) -> tuple[str | None, int]:
    """The note's single-line ``summary``, and the index of the first line after the frontmatter."""
    if not lines or lines[0].strip() != "---":
        return None, 0
    end = next((index for index in range(1, len(lines)) if lines[index].strip() == "---"), None)
    if end is None:
        raise ProfileError(f"{where} frontmatter never closes")
    summary: str | None = None
    for raw in lines[1:end]:
        if not raw.strip() or raw[0] in " \t":
            continue
        if raw.startswith("summary:"):
            if summary is not None:
                raise ProfileError(f"{where} has two summary fields")
            value = raw.split(":", 1)[1].strip()
            if re.fullmatch(r"[>|][+-]?", value):
                raise ProfileError(f"{where} summary must be a single line")
            summary = _scalar(value)
    return (summary or None), end + 1


def _visible(lines: list[str], start: int) -> list[tuple[int, str]]:
    """1-based lines outside code fences. An unclosed fence hides the rest of the note."""
    visible: list[tuple[int, str]] = []
    fence: tuple[str, int] | None = None
    for index in range(start, len(lines)):
        line = lines[index]
        match = _FENCE_LINE.match(line)
        if fence is None:
            if match is not None:
                fence = (match.group(1)[0], len(match.group(1)))
                continue
            visible.append((index + 1, line))
            continue
        if (match is not None and match.group(1)[0] == fence[0] and len(match.group(1)) >= fence[1]
                and not match.group(2).strip()):
            fence = None
    return visible


def _note_lines(text: str, where: str) -> tuple[str | None, list[tuple[int, str]]]:
    lines = [line.rstrip("\r") for line in text.removeprefix("\ufeff").split("\n")]
    summary, start = _frontmatter(lines, where)
    return summary, _visible(lines, start)


def _sections(lines: list[tuple[int, str]], level: int) -> list[tuple[str, int, list[tuple[int, str]]]]:
    found: list[tuple[str, int, list[tuple[int, str]]]] = []
    heading: str | None = None
    heading_line = 0
    chunk: list[tuple[int, str]] = []
    for line_no, line in lines:
        match = _HEADING.match(line)
        if match and len(match.group(1)) <= level:
            if heading is not None:
                found.append((heading, heading_line, chunk))
            heading = None
            chunk = []
            if len(match.group(1)) == level:
                heading = match.group(2).strip()
                heading_line = line_no
            continue
        if heading is not None:
            chunk.append((line_no, line))
    if heading is not None:
        found.append((heading, heading_line, chunk))
    return found


def _first_heading(lines: list[tuple[int, str]]) -> tuple[int, str] | None:
    for line_no, line in lines:
        match = _HEADING.match(line)
        if match:
            return line_no, match.group(2).strip()
    return None


def _inline_entry(line: str) -> tuple[str, dict[str, str]] | None:
    """A markdown bullet carrying a ``kind`` field, or None when the line is not that format."""
    match = _BULLET.match(line)
    if match is None:
        return None
    fields: dict[str, str] = {}

    def _grab(found: re.Match[str]) -> str:
        fields[found.group(1)] = _scalar(found.group(2).strip())
        return " "

    text = _INLINE.sub(_grab, match.group(1))
    if "kind" not in fields:
        return None
    return text, fields


def _values(lines: list[tuple[int, str]]) -> list[str]:
    """Bullet items when every content line is a plain bullet; otherwise the section as one line.

    A bullet with ``[kind:: …]`` belongs to the inline-entry pass, not to the heading.
    """
    content = [(line_no, line) for line_no, line in lines if _inline_entry(line) is None]
    bullets: list[str] = []
    prose = False
    for _, line in content:
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith(("- ", "* ", "+ ")):
            bullets.append(stripped[2:].strip())
        else:
            prose = True
    if bullets and not prose:
        return [item for item in (_clean(item) for item in bullets) if item]
    one = _clean("\n".join(line for _, line in content))
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
    return Piece(piece.field, value, piece.path, piece.heading, piece.line, piece.kind, truncated=True,
                 shared=piece.shared)


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
    except (OSError, RuntimeError) as exc:
        raise ProfileError(f"{rel} cannot be read") from exc
    if not resolved.is_relative_to(root):
        raise ProfileError(f"{rel} resolves outside the source folder")
    return resolved


def _read(root: Path, path: Path) -> str:
    rel = _rel(root, path)
    resolved = _inside(root, path, rel)
    try:
        info = resolved.stat()
        if not stat.S_ISREG(info.st_mode):
            raise ProfileError(f"{rel} must be a regular file")
        size = info.st_size
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


def _kind_pieces(lines: list[tuple[int, str]], rel: str, skipped: list[dict[str, str]]) -> list[Piece]:
    """Inline entries in file order. Inactive rows are left out. An unknown kind is skipped."""
    pieces: list[Piece] = []
    nearest = ""
    for line_no, line in lines:
        match = _HEADING.match(line)
        if match is not None:
            nearest = match.group(2).strip()
            continue
        parsed = _inline_entry(line)
        if parsed is None:
            continue
        raw, fields = parsed
        kind = fields.get("kind", "").strip()
        status = fields.get("status", "").strip() or "active"
        if status in _INACTIVE:
            continue
        field = _KIND_FIELD.get(kind)
        if field is None:
            skipped.append({"path": rel, "reason": "unknown kind"})
            continue
        value = _clean(raw)
        if not value:
            continue
        shared = fields.get(_SHARE_FIELD, "").strip() == _SHARE_HOOK
        pieces.append(Piece(field, value, rel, _clean(nearest) or kind, line_no, kind, shared=shared))
    return pieces


def _about_pieces(root: Path, path: Path, skipped: list[dict[str, str]]) -> list[Piece]:
    rel = _rel(root, path)
    _, lines = _note_lines(_read(root, path), rel)
    pieces: list[Piece] = []
    seen: set[str] = set()
    for heading, line_no, chunk in _sections(lines, 2):
        field = _FIELDS.get(heading)
        if field is None:
            continue
        if field in seen:
            raise ProfileError(f"{rel} has two headings for {field}")
        seen.add(field)
        for value in _values(chunk):
            pieces.append(Piece(field, value, rel, _clean(heading), line_no))
    pieces.extend(_kind_pieces(lines, rel, skipped))
    return pieces


def _rule_pieces(root: Path, path: Path, skipped: list[dict[str, str]]) -> list[Piece]:
    rel = _rel(root, path)
    summary, lines = _note_lines(_read(root, path), rel)
    # Any inline entry selects this format. Filtering may leave nothing; the summary is not a rule then.
    if any(_inline_entry(line) is not None for _, line in lines):
        return _kind_pieces(lines, rel, skipped)
    pieces: list[Piece] = []
    for heading, line_no, chunk in _sections(lines, 2):
        for value in _values(chunk):
            pieces.append(Piece("standing_rules", value, rel, _clean(heading), line_no))
    if pieces:
        return pieces
    found = _first_heading(lines)
    one = _clean(summary or "")
    if one and found is not None:
        line_no, heading = found
        return [Piece("standing_rules", one, rel, _clean(heading), line_no)]
    if one or found is not None:
        skipped.append({"path": rel, "reason": "no heading to cite" if found is None else "no summary"})
    return pieces


def _reject_duplicates(pieces: list[Piece]) -> None:
    """A single-value field has one value. A second one fails the compile; nothing is merged."""
    seen: dict[str, Piece] = {}
    for piece in pieces:
        if piece.field in _GROUPS:
            continue
        previous = seen.get(piece.field)
        if previous is not None:
            raise ProfileError(
                f"{piece.field} has more than one value "
                f"({previous.path}:{previous.line} and {piece.path}:{piece.line})",
                reason="multiple_values")
        seen[piece.field] = piece


def compile_profile(root: Path, *, source: str | None = None, cap: int = CAP,
                    shared_only: bool = False) -> Compiled:
    """The profile of the notes under ``root``. The same notes always compile to the same text.

    ``shared_only`` keeps only entries marked ``[share:: hook]``, after the whole note has been validated.
    """
    if cap < 1:
        raise ProfileError("cap must be positive")
    if not root.exists():
        raise ProfileError("source folder does not exist")
    if not root.is_dir():
        raise ProfileError("source must be a directory")
    root = root.resolve()
    if source is None:
        about = _about(root)
        notes = _preferences(root)
    else:
        from .vault import profile_path

        try:
            profile_path(root, source)
        except (ValueError, OSError, RuntimeError) as exc:
            raise ProfileError(str(exc)) from exc
        about = root / source
        notes = []
    skipped: list[dict[str, str]] = []
    pieces = _about_pieces(root, about, skipped) if about is not None else []
    for note in notes:
        pieces.extend(_rule_pieces(root, note, skipped))
    _reject_duplicates(pieces)
    if shared_only:
        pieces = [piece for piece in pieces if piece.shared]
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
    dropped = tuple({"field": piece.field, "path": piece.path, "heading": piece.heading, "line": piece.line}
                    for piece in omitted)
    return Compiled(text, fields, dropped, tuple(skipped), sources)


async def compiled_profile(*, shared_only: bool = False) -> Compiled | None:
    """Compile the configured note as it is now; never serve a stale cached profile on failure."""
    from . import vault

    try:
        config, problem = await vault.load()
        if problem:
            raise ProfileError(problem)
        if config is None:
            return None
        return compile_profile(config.path, source=config.profile_source, shared_only=shared_only)
    except Exception:
        logger.warning("operator profile is unavailable; omitting it", exc_info=True)
        return None


async def resident_text() -> str:
    """The exact bounded text shared by resident content and the hook command; no cache writes."""
    compiled = await compiled_profile()
    return compiled.text if compiled is not None else ""


async def session_text() -> str:
    """The entries marked ``[share:: hook]``, for local sessions; the Manager keeps reading resident_text()."""
    compiled = await compiled_profile(shared_only=True)
    return compiled.text if compiled is not None else ""


async def profile_view() -> dict[str, Any]:
    """Read the same compiled profile, retaining the version when its body is unchanged."""
    compiled = await compiled_profile()
    if compiled is None:
        return _unconfigured()
    version = await blocks.retain_or_replace(text=compiled.text, body=compiled.payload())
    return compiled.view(version)


async def source_view() -> dict[str, Any]:
    from . import vault

    config, problem = await vault.load()
    if problem:
        raise ProfileError(problem)
    return {"source": config.profile_source if config else None}


async def bound_source(value: str | None) -> str:
    from . import vault

    config, problem = await vault.load()
    if config is None:
        raise ProfileError(problem or "knowledge vault is not configured", reason="vault_unconfigured")
    source = vault.DEFAULT_PROFILE_SOURCE if value is None else value
    try:
        if value is not None:
            vault.profile_path(config.path, source)
    except (ValueError, OSError, RuntimeError) as exc:
        raise ProfileError(str(exc)) from exc
    return source


async def update_source(value: str | None) -> dict[str, Any]:
    """Compatibility operation: change the vault's relative note path while preserving its other fields."""
    from . import vault
    from .projects import RequestError

    source = await bound_source(value)
    config, problem = await vault.load()
    if config is None:
        raise ProfileError(problem or "knowledge vault is not configured", reason="vault_unconfigured")
    try:
        await vault.update_settings({
            "path": str(config.path), "writable": list(config.writable), "rules_file": config.rules_file,
            "manager_read": config.manager_read, **({"profile_source": source} if value is not None else {})})
    except RequestError as exc:
        raise ProfileError(str(exc)) from exc
    return {"source": source}


def checked_update(body: Mapping[str, Any]) -> str | None:
    if "source" not in body or (body["source"] is not None and not isinstance(body["source"], str)):
        raise ProfileError("source must be a relative Markdown file path, or null to restore the default")
    return body["source"]


async def enabled() -> bool:
    """Only the stored literal true enables resident injection; unset is off."""
    return (await db_settings.get_settings([ENABLED_KEY])).get(ENABLED_KEY) == "true"


async def settings_view() -> dict[str, bool]:
    return {"enabled": await enabled()}


async def update_enabled(value: bool | None) -> dict[str, bool]:
    if value is not None and not isinstance(value, bool):
        raise ProfileError("enabled must be true, false or null")
    await db_settings.put_settings({ENABLED_KEY: None if value is None else str(value).lower()})
    return await settings_view()
