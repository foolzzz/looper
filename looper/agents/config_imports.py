"""Configuration imports through the dashboard: upload, preview, then apply or cancel (``loopx config import``).

A configuration file with the catalog's files is usually far over the 8 MiB
the server reads into memory for a request (``REQUEST_BODY_MAX_BYTES``), so
``POST /api/config/imports`` alone takes up to ``UPLOAD_MAX_BYTES`` and
streams the body to a file under ``<runtime root>/config-imports/`` (mode
0600: the file may hold secrets) instead. The file is then checked and
previewed exactly like ``loopx config import``, and waits under an import id:
``apply`` writes it in one transaction like ``--apply`` and deletes it,
``cancel`` deletes it, and an import neither applied nor cancelled expires
after ``TTL_SECONDS``. Pending imports live in this process only: the server
removes what a previous one left behind when it starts, and its own files when
it stops.
"""

from __future__ import annotations

import asyncio
import logging
import os
import secrets
import shutil
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from . import config_transfer
from .config_transfer import ConfigFileError
from .library import LIBRARY_MAX_BYTES

logger = logging.getLogger(__name__)

# Twice the catalog's cap: a full catalog as JSON-escaped text with room for projects' knowledge. A larger file
# still imports with the CLI, which reads it from disk.
UPLOAD_MAX_BYTES = 2 * LIBRARY_MAX_BYTES
TTL_SECONDS = 3600
# Uploads in flight and imports waiting at once; each holds a file of up to UPLOAD_MAX_BYTES on disk.
PENDING_MAX = 4
DIRNAME = "config-imports"
_CHUNK_BYTES = 1024 * 1024
_READ_TIMEOUT_SECONDS = 30.0
_LABEL = "the uploaded file"
# The file's own description, shown with the preview: a short string each, from the file's head.
_ABOUT_FIELDS = ("format", "loopx_version", "exported_at", "secrets")


class ImportNotFound(LookupError):
    """No pending import has this id: it was never made, was applied or cancelled, or expired."""


class ImportConflict(RuntimeError):
    """The import cannot start now: too many are pending, or this one is being applied."""


@dataclass
class _Pending:
    path: Path
    about: dict[str, str]
    deadline: float  # time.monotonic()
    expires_at: datetime
    timer: asyncio.TimerHandle | None = None
    applying: bool = False


def _about(data: dict[str, Any]) -> dict[str, str]:
    return {name: data[name][:100] for name in _ABOUT_FIELDS if isinstance(data.get(name), str)}


async def _run(data: dict[str, Any], *, apply: bool) -> config_transfer.Plan:
    from ..db.connection import get_pool

    async with get_pool().acquire() as conn:
        return await config_transfer.run_import(conn, data, apply=apply)


class ConfigImports:
    """The imports uploaded to one server, each a file under ``<runtime_root>/config-imports/``."""

    def __init__(self, runtime_root: Path) -> None:
        self._directory = runtime_root / DIRNAME
        self._pending: dict[str, _Pending] = {}
        self._receiving = 0

    def clear(self) -> None:
        """Forget every pending import and remove the directory with every file in it."""
        for pending in self._pending.values():
            if pending.timer is not None:
                pending.timer.cancel()
        self._pending.clear()
        shutil.rmtree(self._directory, ignore_errors=True)

    async def receive(self, reader: asyncio.StreamReader, length: int) -> dict[str, Any]:
        """Stream a body of ``length`` bytes to a file, check it and preview it; the file waits under a new id.

        Raises ``ConfigFileError`` for a body that is not a usable file (its
        file is removed), and ``ImportConflict`` when ``PENDING_MAX`` imports
        are pending or arriving.
        """
        if length <= 0:
            raise ConfigFileError("the request holds no file")
        if len(self._pending) + self._receiving >= PENDING_MAX:
            raise ImportConflict(f"{PENDING_MAX} imports are pending; apply or cancel one, or wait for one to "
                                 f"expire ({TTL_SECONDS // 60} minutes after its upload)")
        import_id = f"cfgimp-{secrets.token_hex(8)}"
        self._directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = self._directory / f"{import_id}.json"
        self._receiving += 1
        try:
            with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as handle:
                remaining = length
                while remaining:
                    chunk = await asyncio.wait_for(reader.read(min(remaining, _CHUNK_BYTES)),
                                                   timeout=_READ_TIMEOUT_SECONDS)
                    if not chunk:
                        raise asyncio.IncompleteReadError(b"", remaining)
                    handle.write(chunk)
                    remaining -= len(chunk)
            data = await asyncio.to_thread(config_transfer.read_file, path, label=_LABEL)
            plan = await _run(data, apply=False)
        except BaseException:
            path.unlink(missing_ok=True)
            raise
        finally:
            self._receiving -= 1
        pending = _Pending(path, _about(data), time.monotonic() + TTL_SECONDS,
                           datetime.now(UTC) + timedelta(seconds=TTL_SECONDS))
        self._pending[import_id] = pending
        self._arm(import_id, pending)
        return {"import_id": import_id, "bytes": length, "file": pending.about,
                "expires_at": pending.expires_at.isoformat(timespec="seconds"), "plan": plan.view()}

    async def apply(self, import_id: str) -> dict[str, Any]:
        """Write the pending import in one transaction and delete its file. On a failure nothing is written and
        it stays pending (until it expires) for another try."""
        pending = self._get(import_id)
        if pending.applying:
            raise ImportConflict("this import is being applied")
        pending.applying = True
        if pending.timer is not None:
            pending.timer.cancel()
        try:
            data = await asyncio.to_thread(config_transfer.read_file, pending.path, label=_LABEL)
            plan = await _run(data, apply=True)
        except BaseException:
            pending.applying = False
            self._arm(import_id, pending)
            raise
        self._drop(import_id)
        return {"import_id": import_id, "plan": plan.view(targets=True)}

    async def cancel(self, import_id: str) -> dict[str, str]:
        if self._get(import_id).applying:
            raise ImportConflict("this import is being applied")
        self._drop(import_id)
        return {"deleted": import_id}

    def _get(self, import_id: str) -> _Pending:
        pending = self._pending.get(import_id)
        if pending is None or (not pending.applying and time.monotonic() >= pending.deadline):
            if pending is not None:
                self._drop(import_id)
            raise ImportNotFound(f"no pending import {import_id}: it was applied or cancelled, expired "
                                 f"({TTL_SECONDS // 60} minutes after its upload), or the server restarted; "
                                 "upload the file again")
        return pending

    def _arm(self, import_id: str, pending: _Pending) -> None:
        delay = max(pending.deadline - time.monotonic(), 0.0)
        pending.timer = asyncio.get_running_loop().call_later(delay, self._expire, import_id)

    def _expire(self, import_id: str) -> None:
        pending = self._pending.get(import_id)
        if pending is not None and not pending.applying:
            logger.info("config import %s expired unapplied; its file is removed", import_id)
            self._drop(import_id)

    def _drop(self, import_id: str) -> None:
        pending = self._pending.pop(import_id, None)
        if pending is None:
            return
        if pending.timer is not None:
            pending.timer.cancel()
        pending.path.unlink(missing_ok=True)
