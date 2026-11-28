"""Read a file only from inside a boundary directory.

Files that anyone who can change a repository may have written (project instruction files) are read by Looper
itself, outside any sandbox. ``read_inside`` reads one only when it is a regular file, with no other hard link,
whose resolved path is inside ``boundary`` with no other repository on the way: no directory below the boundary
down to the file's own holds a ``.git`` entry. A name that is a link to another such file is read as that file; with
``names``, only when the file it resolves to has one of those names.

The check by path alone can be raced by a process that swaps a directory for a link between the check and the
open, so the open walks the resolved path from a descriptor of the boundary one component at a time with
``O_NOFOLLOW``, looks for ``.git`` in each directory it opens, and checks the opened descriptor (``fstat``), not the
path. The link count is taken both from that descriptor and from the name it was opened by, which must still be that
file. At most ``limit + 1`` bytes are read, and a leaf that is not a regular file (a FIFO, a device) is opened
without blocking and never read. Every descriptor is closed on every path. A refused file is a typed ``Skipped``;
nothing of its content is kept.
"""

from __future__ import annotations

import errno
import os
import stat
from collections.abc import Collection
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path


class Skip(StrEnum):
    OUTSIDE = "outside"  # the resolved path is not inside the boundary
    OTHER_REPOSITORY = "other_repository"  # inside the boundary, in a repository nested below it
    NOT_AN_INSTRUCTION_FILE = "not_an_instruction_file"  # the resolved file has none of the caller's ``names``
    SYMLINK = "symlink"  # a link was found while walking the path (the path changed after it was resolved)
    HARD_LINKED = "hard_linked"  # more than one link: the same inode may also be a file elsewhere
    NOT_REGULAR = "not_regular"  # a directory, FIFO, device or socket
    TOO_LARGE = "too_large"  # larger than ``limit`` when the caller does not clip
    IO_ERROR = "io_error"  # it could not be opened or read, or its name was replaced while it was opened


@dataclass(frozen=True)
class Content:
    """The first ``limit`` bytes of the file and its size when it was opened."""

    data: bytes
    size: int


@dataclass(frozen=True)
class Skipped:
    reason: Skip
    detail: str = ""  # for IO_ERROR, the exception's class and its strerror; never file content


class _LeftTheBoundary(Exception):
    """A component of the path turned out to be a link (or not a directory) while it was walked."""


class _OtherRepository(Exception):
    """A directory on the way holds ``.git``."""


def _open(name: str, flags: int, parent: int | None = None) -> int:
    try:
        return os.open(name, flags | os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW, dir_fd=parent)
    except OSError as exc:
        if exc.errno == errno.ELOOP or (flags & os.O_DIRECTORY and exc.errno == errno.ENOTDIR):
            raise _LeftTheBoundary(name) from exc
        raise


def _holds_git(directory: int) -> bool:
    try:
        os.stat(".git", dir_fd=directory, follow_symlinks=False)
    except FileNotFoundError:
        return False
    return True


def _open_inside(base: str, relative: str) -> tuple[int, int, str]:
    """Open ``relative`` below ``base``, both already resolved, refusing any link and any other repository on the
    way. Returns the descriptors of the directory holding the file and of the file, and its name there; the caller
    closes both."""
    parts = [part for part in relative.split(os.sep) if part not in ("", ".")]
    held = _open(base, os.O_DIRECTORY)
    try:
        for part in parts[:-1]:
            following = _open(part, os.O_DIRECTORY, held)
            os.close(held)
            held = following
            if _holds_git(held):
                raise _OtherRepository(part)
        # O_NONBLOCK: opening a FIFO must not wait for a writer
        return held, _open(parts[-1], os.O_NONBLOCK, held), parts[-1]
    except BaseException:
        os.close(held)
        raise


def _read_bytes(fd: int, size: int) -> bytes:
    chunks: list[bytes] = []
    got = 0
    while got < size:
        chunk = os.read(fd, size - got)
        if not chunk:
            break
        chunks.append(chunk)
        got += len(chunk)
    return b"".join(chunks)


def _io_error(exc: OSError | RecursionError) -> Skipped:
    return Skipped(Skip.IO_ERROR, f"{type(exc).__name__}: {getattr(exc, 'strerror', None) or exc}")


def read_inside(boundary: str | Path, path: str | Path, limit: int, *, clip: bool = False,
                names: Collection[str] | None = None) -> Content | Skipped:
    """``path`` read from inside ``boundary``: up to ``limit`` bytes, or why it was not read. With ``names``, the
    instruction file names, the file ``path`` finally resolves to must have one of them.

    A file larger than ``limit`` is ``Skip.TOO_LARGE``, or with ``clip`` its first ``limit`` bytes (``size`` tells
    how large it was). Never more than ``limit + 1`` bytes are read."""
    try:
        base = os.path.realpath(boundary)
        real = os.path.realpath(path)
    except (OSError, RecursionError) as exc:  # a link replaced while it was resolved; a chain of links too deep
        return _io_error(exc)
    if real != base and not real.startswith(base.rstrip(os.sep) + os.sep):
        return Skipped(Skip.OUTSIDE)
    if real == base:
        return Skipped(Skip.NOT_REGULAR)
    # Judged on ``real``, the path that is walked below, so a link changed afterwards cannot pass another name.
    if names is not None and os.path.basename(real) not in names:
        return Skipped(Skip.NOT_AN_INSTRUCTION_FILE)
    try:
        directory, fd, name = _open_inside(base, os.path.relpath(real, base))
    except _LeftTheBoundary:
        return Skipped(Skip.SYMLINK)
    except _OtherRepository:
        return Skipped(Skip.OTHER_REPOSITORY)
    except OSError as exc:
        return _io_error(exc)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            return Skipped(Skip.NOT_REGULAR)
        # A name removed after the open lowers the count ``fstat`` sees, so the name must still be there too.
        named = os.stat(name, dir_fd=directory, follow_symlinks=False)
        if max(st.st_nlink, named.st_nlink) > 1:
            return Skipped(Skip.HARD_LINKED)
        if (named.st_dev, named.st_ino) != (st.st_dev, st.st_ino):
            return Skipped(Skip.IO_ERROR, "the file was replaced while it was opened")
        if st.st_size > limit and not clip:
            return Skipped(Skip.TOO_LARGE)
        data = _read_bytes(fd, limit + 1)
    except OSError as exc:
        return _io_error(exc)
    finally:
        os.close(fd)
        os.close(directory)
    if len(data) > limit and not clip:  # it grew after it was measured
        return Skipped(Skip.TOO_LARGE)
    return Content(data[:limit], max(st.st_size, len(data)))
