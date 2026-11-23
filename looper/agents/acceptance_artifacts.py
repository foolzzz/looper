"""Bounded, content-typed acceptance artifacts; metadata never contains a host path."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import threading
from typing import Any

MAX_ARTIFACT_BYTES = 2 * 1024 * 1024
MAX_TASK_ARTIFACT_BYTES = 50 * 1024 * 1024
_storage_lock = threading.Lock()


class ArtifactInvalid(Exception):
    pass


class ArtifactMissing(Exception):
    """The stored artifact's file is not on disk."""


class ArtifactRefused(Exception):
    """The name or the file is not a code-generated artifact (a path, a symlink, a device)."""


# Names `collect` writes: 1.png, 2.txt, … gif is served when a row names one; nothing here emits it.
_STORED_NAME = re.compile(r"^[1-9][0-9]{0,8}\.(png|jpg|webp|gif|txt|json)$")
INLINE_TYPES = frozenset({"image/png", "image/jpeg", "image/gif", "image/webp"})


def _component(value: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", value):
        return value
    raise ArtifactInvalid("invalid evidence identity")


def task_directory(runtime_root: Path, task_id: str) -> Path:
    return runtime_root / "evidence" / _component(task_id)


def _read(root: Path, relative: str) -> bytes:
    path = Path(relative)
    if path.is_absolute() or ".." in path.parts or not path.parts:
        raise ArtifactInvalid("artifact leaves checkout")
    root = root.resolve()
    current = root
    for part in path.parts:
        current = current / part
        if current.is_symlink():
            raise ArtifactInvalid("artifact crosses a symlink")
    if not current.resolve().is_relative_to(root):
        raise ArtifactInvalid("artifact leaves checkout")
    # Reject FIFOs/devices before opening (a FIFO open itself could block).
    if not stat.S_ISREG(current.stat().st_mode):
        raise ArtifactInvalid("artifact is not a regular file")
    fd = os.open(current, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as source:
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_ARTIFACT_BYTES:
            raise ArtifactInvalid("artifact is too large or not regular")
        data = source.read(MAX_ARTIFACT_BYTES + 1)
    if len(data) > MAX_ARTIFACT_BYTES:
        raise ArtifactInvalid("artifact exceeds size limit")
    return data


def _type(data: bytes) -> tuple[str, str]:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png", "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg", "jpg"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp", "webp"
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise ArtifactInvalid("unsupported artifact content") from None
    if any(ord(char) < 32 and char not in "\n\r\t" for char in text):
        raise ArtifactInvalid("unsupported artifact content")
    try:
        json.loads(text)
    except (ValueError, RecursionError):
        return "text/plain", "txt"
    return "application/json", "json"


def _mkdir(directory: Path, boundary: Path) -> None:
    for part in (*reversed(directory.relative_to(boundary).parents), directory.relative_to(boundary)):
        path = boundary / part
        if path.is_symlink():
            raise ArtifactInvalid("evidence directory is a symlink")
        path.mkdir(mode=0o700, exist_ok=True)


def _prune(directory: Path) -> None:
    rounds: dict[tuple[str, str, str], list[Path]] = {}
    for path in directory.glob("*/*/*/*/*"):
        if path.is_symlink() or not path.is_file():
            continue
        step, attempt, _, run, _ = path.relative_to(directory).parts
        rounds.setdefault((step, attempt, run), []).append(path)
    size = sum(p.stat().st_size for files in rounds.values() for p in files)
    for files in sorted(rounds.values(), key=lambda group: min(p.stat().st_mtime_ns for p in group)):
        if size <= MAX_TASK_ARTIFACT_BYTES:
            break
        for path in files:
            size -= path.stat().st_size
            path.unlink()


def collect(work_dir: Path, paths: tuple[str, ...], runtime_root: Path, row: dict[str, Any]) -> list[dict[str, Any]]:
    """Read all declared files before writing any; retain rows when an old round's bytes are pruned."""
    files = [(data, *_type(data)) for path in paths for data in [_read(work_dir, path)]]
    directory = (task_directory(runtime_root, row["task_id"]) / _component(row["step_id"]) /
                 str(row["attempt"]) / _component(row["check_id"]) / str(row["run"]))
    metadata = []
    with _storage_lock:
        _mkdir(directory, runtime_root)
        try:
            for index, (data, mime, extension) in enumerate(files, 1):
                name = f"{index}.{extension}"
                with os.fdopen(os.open(directory / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as target:
                    target.write(data)
                metadata.append({"name": name, "mime": mime, "bytes": len(data),
                                 "sha256": hashlib.sha256(data).hexdigest()})
            _prune(task_directory(runtime_root, row["task_id"]))
        except BaseException:
            shutil.rmtree(directory, ignore_errors=True)
            raise
    return metadata


def content_disposition(mime: str) -> str:
    """Images the dashboard can draw are inline; every other type is a download."""
    return "inline" if mime in INLINE_TYPES else "attachment"


def read_artifact(runtime_root: Path, *, task_id: str, step_id: str, attempt: int, check_id: str,
                  run: int, name: str) -> bytes:
    """The bytes `collect` stored for this run. The name is the row's, never a request path.

    A missing file raises `ArtifactMissing`. A name that is not one `collect` writes, a symlink,
    or anything that is not a regular file under the evidence directory raises `ArtifactRefused`.
    """
    if type(attempt) is not int or attempt < 0 or type(run) is not int or run < 1 or not _STORED_NAME.fullmatch(name):
        raise ArtifactRefused("not a stored artifact")
    try:
        relative = Path("evidence") / _component(task_id) / _component(step_id) / str(attempt) / _component(check_id) / str(run) / name
    except ArtifactInvalid as exc:
        raise ArtifactRefused("not a stored artifact") from exc
    try:
        return _read(runtime_root, relative.as_posix())
    except FileNotFoundError as exc:
        raise ArtifactMissing("file missing") from exc
    except (ArtifactInvalid, OSError) as exc:
        raise ArtifactRefused("not a stored artifact") from exc


def remove_task(runtime_root: Path, task_id: str) -> None:
    directory = task_directory(runtime_root, task_id)
    with _storage_lock:
        if directory.is_symlink():
            directory.unlink()
        elif directory.exists():
            shutil.rmtree(directory)
