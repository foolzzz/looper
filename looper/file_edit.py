"""``looper-edit``: exact text replace, or apply a unified diff, from the shell.

``looper-edit replace <file>`` reads ``{"old", "new", "count"}`` from stdin.
``count`` is optional and defaults to 1. It is the maximum number of exact
matches allowed: fewer matches are all replaced, zero or more than ``count``
refuses. An empty ``old`` creates a missing file and does nothing else.

``looper-edit patch`` reads a multi-file unified diff from stdin and applies
every hunk, or none. A mismatch reports the hunk, the expected lines and the
actual lines.

Paths must resolve inside the working directory. That check makes mistakes
obvious; it is not a sandbox. Existing files keep their mode and newline style.
Each write is a temporary file in the same directory followed by rename.
Exit 0 applies the edit, exit 1 applies nothing, exit 2 is usage.
"""

from __future__ import annotations

import json
import os
import re
import stat
import sys
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

USAGE = "usage: looper-edit replace <file> | looper-edit patch"
_CONTEXT = 2
_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)$")
_MARKER = "\\ No newline at end of file"


class UsageError(Exception):
    """The command line or the replace payload is not usable."""


class ApplyError(Exception):
    """The edit was refused and no file was changed."""


@dataclass(frozen=True)
class Hunk:
    header: str
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    old_lines: tuple[str, ...]
    new_lines: tuple[str, ...]
    old_no_nl: bool
    new_no_nl: bool
    raw: tuple[str, ...]


@dataclass(frozen=True)
class FilePatch:
    old_path: str
    new_path: str
    mode: int | None
    hunks: tuple[Hunk, ...]


@dataclass(frozen=True)
class _Write:
    path: Path
    display: str
    text: str
    mode: int


@dataclass(frozen=True)
class _Delete:
    path: Path
    display: str


def main(argv: Sequence[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    try:
        if list(argv) == ["patch"]:
            patch(_read_stdin())
        elif len(argv) == 2 and argv[0] == "replace":
            old, new, count = _replace_payload(_read_stdin())
            replace(argv[1], old, new, count)
        else:
            raise UsageError(USAGE)
    except UsageError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except ApplyError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"error: {exc.strerror or exc}", file=sys.stderr)
        return 1
    print("ok")
    return 0


def replace(raw: str, old: str, new: str, count: int) -> None:
    """Replace ``old`` in one file, or create the file when ``old`` is empty."""
    path = _resolve_inside(raw)
    if old == "":
        if path.exists():
            raise ApplyError("empty old only creates a missing file")
        if not path.parent.is_dir():
            raise ApplyError(f"parent not found: {raw}")
        _atomic_write(path, new, _creation_mode())
        return
    if not path.is_file():
        raise ApplyError(f"not found: {raw}" if not path.exists() else f"not a file: {raw}")
    text = _read_text(path, raw)
    spans = _find_spans(text, old)
    if not spans:
        raise ApplyError(f"no match in {raw}")
    if len(spans) > count:
        raise ApplyError(_format_matches(raw, text, spans, count))
    updated = text.replace(old, new)
    if updated != text:
        _atomic_write(path, updated, stat.S_IMODE(path.stat().st_mode))


def patch(diff: str) -> None:
    """Apply every file in ``diff``, or leave the working directory untouched."""
    patches = _parse_patch(diff)
    if not patches:
        raise ApplyError("empty patch")
    plan: list[_Write | _Delete] = []
    seen: set[Path] = set()
    for item in patches:
        _plan_file(item, plan, seen)
    _commit(plan)


def _read_stdin() -> str:
    try:
        return sys.stdin.buffer.read().decode("utf-8")
    except UnicodeDecodeError as exc:
        raise UsageError("stdin is not utf-8") from exc


def _replace_payload(raw: str) -> tuple[str, str, int]:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise UsageError("invalid json") from exc
    if not isinstance(payload, dict) or set(payload) - {"old", "new", "count"} or "old" not in payload or "new" not in payload:
        raise UsageError("invalid replace payload")
    old, new = payload["old"], payload["new"]
    count = payload.get("count", 1)
    if not isinstance(old, str) or not isinstance(new, str) or isinstance(count, bool) or not isinstance(count, int) or count < 1:
        raise UsageError("invalid replace payload")
    return old, new, count


def _resolve_inside(raw: str) -> Path:
    if raw == "" or "\x00" in raw:
        raise ApplyError(f"path escapes cwd: {raw}")
    cwd = Path.cwd().resolve()
    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = cwd / candidate
    resolved = candidate.resolve()
    try:
        resolved.relative_to(cwd)
    except ValueError as exc:
        raise ApplyError(f"path escapes cwd: {raw}") from exc
    if resolved == cwd:
        raise ApplyError(f"not a file: {raw}")
    return resolved


def _read_text(path: Path, raw: str) -> str:
    try:
        return path.read_bytes().decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ApplyError(f"not utf-8: {raw}") from exc


def _find_spans(text: str, old: str) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    start = 0
    while True:
        found = text.find(old, start)
        if found < 0:
            return spans
        spans.append((found, found + len(old)))
        start = found + len(old)


def _format_matches(raw: str, text: str, spans: list[tuple[int, int]], count: int) -> str:
    lines = text.splitlines()
    parts = [f"{len(spans)} matches in {raw}, count is {count}"]
    for start, end in spans:
        last = end - 1
        if end > start and text[end - 1] == "\n":
            last = max(start, end - 2)
        first_line = text.count("\n", 0, start) + 1
        last_line = text.count("\n", 0, last) + 1
        if first_line == last_line:
            parts.append(f"match line {first_line}")
        else:
            parts.append(f"match lines {first_line}-{last_line}")
        for number in range(max(1, first_line - _CONTEXT), min(len(lines), last_line + _CONTEXT) + 1):
            parts.append(f"{number}|{lines[number - 1]}")
    return "\n".join(parts)


def _creation_mode() -> int:
    current = os.umask(0)
    os.umask(current)
    return 0o666 & ~current


def _atomic_write(path: Path, text: str, mode: int) -> None:
    """Write ``text`` via a temporary file in ``path``'s directory, then rename it into place."""
    if not path.parent.is_dir():
        path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".looper-edit-", dir=path.parent)
    tmp = Path(name)
    replaced = False
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(text.encode("utf-8"))
        os.chmod(tmp, mode)
        os.replace(tmp, path)
        replaced = True
    finally:
        if not replaced:
            tmp.unlink(missing_ok=True)


def _commit(plan: list[_Write | _Delete]) -> None:
    temps: list[Path] = []
    try:
        staged: list[tuple[Path, Path]] = []
        for item in plan:
            if not isinstance(item, _Write):
                continue
            if not item.path.parent.is_dir():
                item.path.parent.mkdir(parents=True, exist_ok=True)
            fd, name = tempfile.mkstemp(prefix=".looper-edit-", dir=item.path.parent)
            tmp = Path(name)
            temps.append(tmp)
            with os.fdopen(fd, "wb") as handle:
                handle.write(item.text.encode("utf-8"))
            os.chmod(tmp, item.mode)
            staged.append((tmp, item.path))
        for tmp, path in staged:
            os.replace(tmp, path)
            temps.remove(tmp)
        for item in plan:
            if isinstance(item, _Delete):
                item.path.unlink()
    finally:
        for tmp in temps:
            tmp.unlink(missing_ok=True)


def _plan_file(item: FilePatch, plan: list[_Write | _Delete], seen: set[Path]) -> None:
    deleting = item.new_path == "/dev/null"
    creating = item.old_path == "/dev/null"
    if deleting and creating:
        raise ApplyError("malformed patch")
    if not deleting and not creating and item.old_path != item.new_path:
        raise ApplyError(f"rename not supported: {item.old_path} -> {item.new_path}")
    raw = item.old_path if deleting else item.new_path
    path = _resolve_inside(raw)
    if path in seen:
        raise ApplyError(f"duplicate path: {raw}")
    seen.add(path)
    if creating:
        if path.exists():
            raise ApplyError(f"already exists: {raw}")
        if path.parent.exists() and not path.parent.is_dir():
            raise ApplyError(f"parent not found: {raw}")
        lines, _newline, trailing = [], "\n", False
        updated, trailing = _apply_hunks(raw, lines, trailing, item.hunks)
        plan.append(_Write(path, raw, _join(updated, "\n", trailing), item.mode if item.mode is not None else _creation_mode()))
        return
    if not path.is_file():
        raise ApplyError(f"not found: {raw}" if not path.exists() else f"not a file: {raw}")
    original = _read_text(path, raw)
    lines, newline, trailing = _split_file(original)
    updated, trailing = _apply_hunks(raw, lines, trailing, item.hunks)
    text = _join(updated, newline, trailing)
    if deleting:
        if text != "":
            raise ApplyError(f"hunk mismatch in {raw}\ndeleted file is not empty")
        plan.append(_Delete(path, raw))
        return
    if text != original:
        plan.append(_Write(path, raw, text, stat.S_IMODE(path.stat().st_mode)))


def _split_file(text: str) -> tuple[list[str], str, bool]:
    if text == "":
        return [], "\n", False
    newline = "\r\n" if "\r\n" in text else ("\r" if "\n" not in text and "\r" in text else "\n")
    trailing = text.endswith(newline)
    parts = text.split(newline)
    if trailing:
        parts = parts[:-1]
    return parts, newline, trailing


def _join(lines: list[str], newline: str, trailing: bool) -> str:
    if not lines:
        return ""
    text = newline.join(lines)
    return text + newline if trailing else text


def _apply_hunks(raw: str, lines: list[str], trailing: bool, hunks: tuple[Hunk, ...]) -> tuple[list[str], bool]:
    out: list[str] = []
    pos = 0
    result_trailing = trailing
    consumed_end = False
    for hunk in hunks:
        at = hunk.old_start if hunk.old_count == 0 else hunk.old_start - 1
        if at < pos:
            raise ApplyError("malformed patch: hunks out of order")
        if at > len(lines):
            _mismatch(raw, hunk, [])
        out.extend(lines[pos:at])
        actual = lines[at:at + hunk.old_count]
        end = at + hunk.old_count
        reaches_end = end == len(lines) and at <= len(lines)
        content_ok = actual == list(hunk.old_lines)
        newline_ok = True
        if reaches_end and hunk.old_count > 0:
            newline_ok = trailing == (not hunk.old_no_nl)
        elif hunk.old_no_nl:
            newline_ok = False
        if not content_ok or not newline_ok:
            _mismatch(raw, hunk, actual, newline_only=content_ok and not newline_ok, file_has_newline=trailing)
        out.extend(hunk.new_lines)
        pos = end
        if reaches_end:
            consumed_end = True
            result_trailing = (not hunk.new_no_nl) if hunk.new_lines else bool(out)
    out.extend(lines[pos:])
    if not consumed_end:
        result_trailing = trailing
    return out, result_trailing


def _mismatch(raw: str, hunk: Hunk, actual: list[str], *, newline_only: bool = False, file_has_newline: bool = True) -> None:
    expected = "\n".join(hunk.old_lines)
    found = "\n".join(actual)
    if len(actual) < len(hunk.old_lines):
        found = (found + "\n" if found else "") + "<eof>"
    elif newline_only:
        expected = (expected + "\n" if expected else "") + ("<no newline at eof>" if hunk.old_no_nl else "<newline at eof>")
        found = (found + "\n" if found else "") + ("<newline at eof>" if file_has_newline else "<no newline at eof>")
    body = "\n".join(hunk.raw)
    raise ApplyError(
        f"hunk mismatch in {raw}\n"
        f"hunk:\n{hunk.header}\n{body}\n"
        f"expected:\n{expected}\n"
        f"actual:\n{found}"
    )


def _parse_patch(diff: str) -> list[FilePatch]:
    lines = diff.splitlines()
    patches: list[FilePatch] = []
    index = 0
    while index < len(lines):
        while index < len(lines) and lines[index] == "":
            index += 1
        if index >= len(lines):
            break
        mode: int | None = None
        if lines[index].startswith("diff --git "):
            index += 1
            while index < len(lines) and not lines[index].startswith("--- ") and not lines[index].startswith("@@"):
                meta = lines[index]
                index += 1
                if meta == "":
                    continue
                if meta.startswith("new file mode ") or meta.startswith("new mode "):
                    mode = _mode(meta.rsplit(" ", 1)[1])
                elif meta.startswith(("deleted file mode ", "old mode ", "index ", "similarity index ", "dissimilarity index ")):
                    continue
                elif meta.startswith(("rename ", "copy ")):
                    raise ApplyError("rename not supported")
                elif meta.startswith("Binary ") or meta.startswith("GIT binary"):
                    raise ApplyError("binary diff not supported")
                else:
                    raise ApplyError("malformed patch")
        if index >= len(lines) or not lines[index].startswith("--- "):
            raise ApplyError("malformed patch")
        old_path = _header_path(lines[index][4:])
        index += 1
        if index >= len(lines) or not lines[index].startswith("+++ "):
            raise ApplyError("malformed patch")
        new_path = _header_path(lines[index][4:])
        index += 1
        hunks: list[Hunk] = []
        while index < len(lines) and lines[index].startswith("@@"):
            hunk, index = _read_hunk(lines, index)
            hunks.append(hunk)
        if not hunks:
            raise ApplyError("malformed patch: no hunks")
        patches.append(FilePatch(old_path, new_path, mode, tuple(hunks)))
    return patches


def _header_path(rest: str) -> str:
    token = rest.split("\t", 1)[0].rstrip()
    if len(token) >= 2 and token[0] == '"' and token[-1] == '"':
        token = _unquote(token)
    if token == "/dev/null":
        return token
    if token.startswith(("a/", "b/")):
        token = token[2:]
    if token == "" or token == ".":
        raise ApplyError("malformed patch: empty path")
    return token


def _unquote(token: str) -> str:
    body = token[1:-1]
    out: list[str] = []
    index = 0
    while index < len(body):
        if body[index] != "\\":
            out.append(body[index])
            index += 1
            continue
        index += 1
        if index >= len(body):
            raise ApplyError("malformed patch: bad quote")
        escaped = body[index]
        plain = {"n": "\n", "t": "\t", "\\": "\\", '"': '"', "a": "\a", "b": "\b", "f": "\f", "v": "\v"}
        if escaped in plain:
            out.append(plain[escaped])
            index += 1
            continue
        if escaped not in "01234567":
            raise ApplyError("malformed patch: bad quote")
        octal = escaped
        index += 1
        for _ in range(2):
            if index < len(body) and body[index] in "01234567":
                octal += body[index]
                index += 1
        out.append(chr(int(octal, 8)))
    return "".join(out)


def _mode(token: str) -> int:
    try:
        return int(token, 8) & 0o7777
    except ValueError as exc:
        raise ApplyError("malformed patch: bad mode") from exc


def _read_hunk(lines: list[str], index: int) -> tuple[Hunk, int]:
    header = lines[index]
    match = _HUNK.match(header)
    if match is None:
        raise ApplyError("malformed patch: bad hunk header")
    old_start = int(match.group(1))
    old_count = int(match.group(2)) if match.group(2) is not None else 1
    new_start = int(match.group(3))
    new_count = int(match.group(4)) if match.group(4) is not None else 1
    if old_count < 0 or new_count < 0 or (old_count == 0 and old_start < 0) or (old_count > 0 and old_start < 1):
        raise ApplyError("malformed patch: bad hunk header")
    index += 1
    need_old, need_new = old_count, new_count
    raw: list[str] = []
    old_lines: list[str] = []
    new_lines: list[str] = []
    old_no_nl = False
    new_no_nl = False
    marked = False
    while index < len(lines) and (need_old or need_new or lines[index].startswith("\\")):
        line = lines[index]
        index += 1
        if line.startswith("\\"):
            if line != _MARKER or not raw or marked:
                raise ApplyError("malformed patch: bad newline marker")
            previous = raw[-1]
            if previous[:1] in " -":
                if old_no_nl:
                    raise ApplyError("malformed patch: bad newline marker")
                old_no_nl = True
            if previous[:1] in " +":
                if new_no_nl:
                    raise ApplyError("malformed patch: bad newline marker")
                new_no_nl = True
            raw.append(line)
            marked = True
            continue
        if (line[:1] == " " and (need_old <= 0 or need_new <= 0 or old_no_nl or new_no_nl)
                or (line[:1] == "-" and (need_old <= 0 or old_no_nl))
                or (line[:1] == "+" and (need_new <= 0 or new_no_nl))
                or line[:1] not in " +-"):
            raise ApplyError("malformed patch: bad hunk line")
        marked = False
        raw.append(line)
        if line.startswith(" "):
            need_old -= 1
            need_new -= 1
            old_lines.append(line[1:])
            new_lines.append(line[1:])
        elif line.startswith("-"):
            need_old -= 1
            old_lines.append(line[1:])
        else:
            need_new -= 1
            new_lines.append(line[1:])
    if need_old or need_new or len(old_lines) != old_count or len(new_lines) != new_count:
        raise ApplyError("malformed patch: hunk counts do not match")
    return Hunk(header, old_start, old_count, new_start, new_count, tuple(old_lines), tuple(new_lines),
                old_no_nl, new_no_nl, tuple(raw)), index


if __name__ == "__main__":
    sys.exit(main())
