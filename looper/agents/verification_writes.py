"""Kernel write notifications for one private verification checkout.

Notifications survive a write followed by restoration of the original bytes.
The caller starts this before launching the verifier and finishes it only after
the verifier and its children have stopped. No observer thread or polling is
needed: the kernel retains the notifications until ``finish`` drains them.
"""

from __future__ import annotations

import ctypes
import os
from pathlib import Path
import select
import struct
import sys
from collections.abc import Sequence


class VerificationWrites:
    """Record writes to ``paths`` relative to ``root``; monitoring errors fail closed.

    kqueue cannot identify a directory entry by name. For a path already missing
    at startup, any change in its nearest existing parent conservatively counts
    as a write to that path. Existing files have individual vnode watches, so
    ordinary ignored build output beside them does not count as a code write.
    """

    def __init__(self, root: Path, paths: Sequence[str]) -> None:
        self.root = root.resolve()
        self.paths = sorted(set(paths))
        self._affected: dict[str, set[str]] = {}
        for name in self.paths:
            for ancestor in (Path(name), *Path(name).parents):
                self._affected.setdefault(ancestor.as_posix(), set()).add(name)
        self._queue = None
        self._fds: dict[int, set[str]] = {}
        self._directories: dict[int, str] = {}
        self._inodes: dict[int, set[str]] = {}
        self._inotify: int | None = None
        self._writes: set[str] = set()
        self._started = False
        self._finished = False
        self._closed = False
        self._error: OSError | None = None

    def start(self) -> None:
        if self._started or self._closed:
            raise OSError("verification write monitor already started")
        self._started = True
        try:
            for name in self.paths:
                relative = Path(name)
                if relative.is_absolute() or ".." in relative.parts:
                    raise OSError("verification write path leaves checkout")
                if not (self.root / relative).parent.resolve().is_relative_to(self.root):
                    raise OSError("verification write path crosses an external symlink")
            if sys.platform == "linux":
                self._start_inotify()
            elif hasattr(select, "kqueue"):
                self._start_kqueue()
            else:
                raise OSError("verification write monitoring is unavailable on this platform")
        except OSError as exc:
            self._error = exc
            self.close()
            raise
        except BaseException:
            self.close()
            raise

    def _start_kqueue(self) -> None:
        self._queue = select.kqueue()
        watches: dict[Path, set[str]] = {}
        missing_parents = set()
        for name in self.paths:
            path = self.root / name
            while not os.path.lexists(path):
                path = path.parent
                missing_parents.add(path)
            watches.setdefault(path, set()).add(name)
            parent = path.parent
            while parent.is_relative_to(self.root):
                watches.setdefault(parent, set()).add(name)
                parent = parent.parent
        events = []
        flags = (select.KQ_NOTE_WRITE | select.KQ_NOTE_EXTEND | select.KQ_NOTE_ATTRIB
                 | select.KQ_NOTE_DELETE | select.KQ_NOTE_RENAME | select.KQ_NOTE_REVOKE)
        for path, names in watches.items():
            options = os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC
            if path.is_symlink():
                options |= os.O_SYMLINK
            fd = os.open(path, options)
            self._fds[fd] = names
            watched_flags = flags
            if path.is_dir() and not path.is_symlink() and path not in missing_parents:
                watched_flags &= ~(select.KQ_NOTE_WRITE | select.KQ_NOTE_EXTEND)
            events.append(select.kevent(fd, filter=select.KQ_FILTER_VNODE,
                                        flags=select.KQ_EV_ADD | select.KQ_EV_CLEAR, fflags=watched_flags))
        self._queue.control(events, 0, 0)

    def _start_inotify(self) -> None:
        libc = ctypes.CDLL(None, use_errno=True)
        initialize = libc.inotify_init1
        initialize.argtypes, initialize.restype = [ctypes.c_int], ctypes.c_int
        fd = initialize(os.O_NONBLOCK | os.O_CLOEXEC)
        if fd < 0:
            raise OSError(ctypes.get_errno(), "cannot initialize verification write monitor")
        self._inotify = fd
        add = libc.inotify_add_watch
        add.argtypes, add.restype = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32], ctypes.c_int
        directories = {self.root}
        for name in self.paths:
            parent = (self.root / name).parent
            while parent.is_relative_to(self.root):
                if parent.is_dir():
                    directories.add(parent)
                parent = parent.parent
        # MODIFY, ATTRIB, CLOSE_WRITE, MOVED_FROM/TO, CREATE, DELETE, DELETE_SELF, MOVE_SELF.
        for directory in sorted(directories):
            watch = add(fd, os.fsencode(directory), 0x00000FCE)
            if watch < 0:
                raise OSError(ctypes.get_errno(), "cannot watch verification directory")
            self._directories[watch] = directory.relative_to(self.root).as_posix()
        # A directory watch does not report writes through a hardlink in an
        # unwatched ignored directory. Watch the tracked inode as well; retain
        # the directory watches to detect replacement of that inode by rename.
        for name in self.paths:
            path = self.root / name
            if not os.path.lexists(path) or (path.is_dir() and not path.is_symlink()):
                continue
            # MODIFY, ATTRIB, CLOSE_WRITE, DELETE_SELF, MOVE_SELF, DONT_FOLLOW,
            # MASK_ADD (multiple tracked hardlinks may share a watch id).
            watch = add(fd, os.fsencode(path), 0x22000C0E)
            if watch < 0:
                raise OSError(ctypes.get_errno(), "cannot watch verification file")
            self._inodes.setdefault(watch, set()).add(name)

    def finish(self) -> list[str]:
        if self._error is not None:
            raise self._error
        if self._finished:
            return sorted(self._writes)
        if not self._started or self._closed:
            raise OSError("verification write monitor is not active")
        try:
            if self._queue is not None and self._fds:
                for event in self._queue.control(None, len(self._fds), 0):
                    if event.flags & select.KQ_EV_ERROR:
                        raise OSError("verification vnode watch failed")
                    self._writes.update(self._fds[event.ident])
            elif self._inotify is not None:
                self._finish_inotify()
            self._finished = True
            return sorted(self._writes)
        except OSError as exc:
            self._error = exc
            raise
        finally:
            self.close()

    def _finish_inotify(self) -> None:
        assert self._inotify is not None
        # Bound draining even if a caller violates the stopped-process contract.
        consumed = 0
        while True:
            try:
                data = os.read(self._inotify, 65536)
            except BlockingIOError:
                break
            consumed += len(data)
            if not data or consumed > 4 * 1024 * 1024:
                raise OSError("verification write notification queue could not be drained")
            offset = 0
            while offset < len(data):
                watch, mask, _, length = struct.unpack_from("iIII", data, offset)
                name = os.fsdecode(data[offset + 16:offset + 16 + length].split(b"\0", 1)[0])
                offset += 16 + length
                if mask & 0x00004000:  # IN_Q_OVERFLOW: writes may have been lost.
                    raise OSError("verification write notification queue overflowed")
                if mask & 0x00002000:  # IN_UNMOUNT invalidates the monitoring boundary.
                    raise OSError("verification write notification filesystem was unmounted")
                # IN_IGNORED after unlink also counts: losing a watch must never
                # turn deletion and recreation into a successful verification.
                self._writes.update(self._inodes.get(watch, ()))
                directory = self._directories.get(watch)
                if directory is None:
                    continue
                changed = (Path(directory) / name).as_posix()
                self._writes.update(self._affected.get(changed, ()))

    def close(self) -> None:
        """Release all kernel watches, including when preparation or launch fails."""
        self._closed = True
        if self._queue is not None:
            self._queue.close()
            self._queue = None
        for fd in self._fds:
            os.close(fd)
        self._fds.clear()
        if self._inotify is not None:
            os.close(self._inotify)
            self._inotify = None
