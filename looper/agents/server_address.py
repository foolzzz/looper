"""The bound HTTP address, published atomically and valid only while its server holds the lock."""

from __future__ import annotations

import fcntl
import json
import os
import tempfile
from pathlib import Path

FILE = "server-address.json"
LOCK = "server-address.lock"


class Publication:
    """One listening server per runtime root; a crash releases the lock, leaving a detectable stale record."""

    def __init__(self, root: Path, host: str, port: int) -> None:
        self._fd = os.open(root / LOCK, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        self._record_fd = None
        try:
            try:
                fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ValueError("another HTTP server already owns this runtime root's address") from None
            # Lock the record's inode before publishing it. Checking a separate lock then reading the
            # path could pair an old address with a new server's lock during a restart.
            fd, temporary = tempfile.mkstemp(dir=root, prefix=f".{FILE}.")
            self._record_fd = fd
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                with os.fdopen(os.dup(fd), "w", encoding="utf-8") as handle:
                    json.dump({"host": host, "port": port}, handle)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, root / FILE)
            finally:
                Path(temporary).unlink(missing_ok=True)
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        if self._record_fd is not None:
            os.close(self._record_fd)
            self._record_fd = None
        if self._fd is not None:
            # Keep the record: falling back to the configured port after shutdown could reach another server.
            os.close(self._fd)
            self._fd = None


def read(root: Path) -> tuple[str, int] | None:
    """No record means a pre-discovery server; an existing but unlocked/broken record is never a fallback."""
    try:
        fd = os.open(root / FILE, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    try:
        with os.fdopen(fd, "r", encoding="utf-8") as handle:
            try:
                fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
            except BlockingIOError:
                pass  # Only the listening server holds the exclusive lock.
            else:
                raise ValueError("server address record is stale; start Looper with this runtime root")
            record = json.load(handle)
        host, port = record["host"], record["port"]
        if not isinstance(host, str) or not host or type(port) is not int or not 1 <= port <= 65535:
            raise ValueError("invalid host or port")
        return host, port
    except (OSError, UnicodeError, ValueError, KeyError, TypeError) as exc:
        raise ValueError(f"invalid or stale server address record: {exc}; restart Looper with this runtime root") from None
