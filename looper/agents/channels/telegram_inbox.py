"""Durable Telegram updates, saved before the polling client acknowledges its offset."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path
from typing import Any


class TelegramInbox(asyncio.Queue):
    """SQLite commits precede Queue.put; processed ids survive journal compaction and restart.

    A planned stop drains Application's handlers. Pending rows additionally protect
    updates acknowledged by polling before their handler started.
    """

    def __init__(self, path: Path) -> None:
        super().__init__()
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path)
        self._db.execute("PRAGMA synchronous = FULL")
        self._db.execute("CREATE TABLE IF NOT EXISTS updates ("
                         "update_id INTEGER PRIMARY KEY, payload TEXT, processed INTEGER NOT NULL DEFAULT 0)")
        self._db.commit()

    async def put(self, update: Any) -> None:
        # Application's private stop sentinel is not a Telegram update.
        if hasattr(update, "update_id"):
            with self._db:
                self._db.execute("INSERT OR IGNORE INTO updates (update_id, payload) VALUES (?, ?)",
                                 (update.update_id, update.to_json()))
        await super().put(update)

    def processed(self, update_id: int) -> bool:
        row = self._db.execute("SELECT processed FROM updates WHERE update_id = ?", (update_id,)).fetchone()
        return row is not None and bool(row[0])

    def finish(self, update_id: int) -> None:
        with self._db:
            # Keep only the dedupe key once delivered; no retained message contents.
            self._db.execute("UPDATE updates SET processed = 1, payload = NULL WHERE update_id = ?", (update_id,))

    async def replay(self, bot: Any) -> None:
        from telegram import Update

        for (payload,) in self._db.execute("SELECT payload FROM updates WHERE processed = 0 ORDER BY update_id").fetchall():
            await super().put(Update.de_json(json.loads(payload), bot))

    def close(self) -> None:
        self._db.close()
