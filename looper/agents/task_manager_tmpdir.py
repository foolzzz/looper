"""The private temporary directory of one launch of a direct task Manager session (``CLAUDE_CODE_TMPDIR``).

Without it the Bash sandbox of Claude Code writes to one temporary root per user, shared by every Claude Code session
of that user. Each direct launch gets ``/tmp/lpr-<uid>/<random>``: both levels are 0700 and owned by this user, outside
the task directory and Looper's runtime root. The directory is removed when its session ends, through the descriptor of
the parent and only when the entry is still the one this process made.

The verified CLI version silently falls back to the shared root when ``<CLAUDE_CODE_TMPDIR>/claude-<uid>`` is longer
than ``MAX_ROOT_BYTES``, so a longer path refuses the launch; the readback checks the write set after the launch too.
"""
from __future__ import annotations

import logging
import os
import secrets
import shutil
import stat

logger = logging.getLogger(__name__)

BASE = "/tmp"
# Claude Code 2.1.332: the longest ``<CLAUDE_CODE_TMPDIR>/claude-<uid>``, in bytes, its Bash children use.
MAX_ROOT_BYTES = 44
ENV_NAME = "CLAUDE_CODE_TMPDIR"
_OPEN = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


class Unusable(Exception):
    """No private temporary directory can be made for this launch; the text says why and what to check."""


class PrivateTmpdir:
    def __init__(self, path: str, uid: int, parent_fd: int, name: str, identity: tuple[int, int]) -> None:
        self.path, self.uid = path, uid  # ``path`` is the value of ``CLAUDE_CODE_TMPDIR``
        self._parent_fd: int | None = parent_fd
        self._name, self._identity = name, identity

    @property
    def write_root(self) -> str:
        """The directory the session's sandbox may write, as the CLI reports it."""
        return f"{os.path.realpath(self.path)}/claude-{self.uid}/"

    def remove(self) -> None:
        """Delete the directory this object made (never another one under its name). Safe to call twice."""
        parent_fd, self._parent_fd = self._parent_fd, None
        if parent_fd is None:
            return
        try:
            found = os.stat(self._name, dir_fd=parent_fd, follow_symlinks=False)
            if (found.st_dev, found.st_ino) != self._identity:
                logger.warning("task Manager temporary directory %s was replaced; leaving it", self.path)
                return
            shutil.rmtree(self._name, dir_fd=parent_fd)
        except FileNotFoundError:
            pass
        except OSError:
            logger.warning("task Manager temporary directory %s could not be removed", self.path, exc_info=True)
        finally:
            os.close(parent_fd)


def _own_private(fd: int, path: str, uid: int) -> os.stat_result:
    found = os.fstat(fd)
    if found.st_uid != uid or stat.S_IMODE(found.st_mode) != 0o700:
        raise Unusable(f"{path} must be a directory owned by user {uid} with mode 0700 (found owner {found.st_uid}, "
                       f"mode {stat.S_IMODE(found.st_mode):04o}). Remove it, or have its owner remove it, then retry.")
    return found


def create(*, uid: int | None = None, base: str = BASE) -> PrivateTmpdir:
    """A new, empty private temporary directory for one launch, or ``Unusable``."""
    uid = os.getuid() if uid is None else uid
    parent = f"{base}/lpr-{uid}"
    name = secrets.token_hex(4)
    path = f"{parent}/{name}"
    root = f"{path}/claude-{uid}"
    if len(root.encode()) > MAX_ROOT_BYTES:
        raise Unusable(f"{root} is longer than {MAX_ROOT_BYTES} bytes: Claude Code would use the temporary directory "
                       "shared by all its sessions instead.")
    parent_fd = None
    try:
        try:
            os.mkdir(parent, 0o700)
            made = True
        except FileExistsError:
            made = False
        parent_fd = os.open(parent, _OPEN)  # a symlink or a file under this name fails here
        if made and os.fstat(parent_fd).st_uid == uid:
            os.fchmod(parent_fd, 0o700)  # whatever the umask took away
        _own_private(parent_fd, parent, uid)
        os.mkdir(name, 0o700, dir_fd=parent_fd)  # exclusive: an existing entry is never adopted
        fd = os.open(name, _OPEN, dir_fd=parent_fd)
        try:
            os.fchmod(fd, 0o700)
            found = _own_private(fd, path, uid)
        finally:
            os.close(fd)
    except BaseException as exc:
        if parent_fd is not None:
            os.close(parent_fd)
        if isinstance(exc, OSError):
            raise Unusable(f"the private temporary directory {path} could not be made ({type(exc).__name__}: "
                           f"{exc.strerror}). Check {parent}.") from exc
        raise
    return PrivateTmpdir(path, uid, parent_fd, name, (found.st_dev, found.st_ino))
