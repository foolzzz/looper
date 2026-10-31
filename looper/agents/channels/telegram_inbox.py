"""Durable Telegram updates, saved before the polling client acknowledges its offset."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from pathlib import Path
from typing import Any


class TelegramInbox(asyncio.Queue):
    """SQLite commits precede Queue.put; processed ids survive journal compaction and restart.

    A planned stop drains Application's handlers. Pending rows additionally protect
    updates acknowledged by polling before their handler started. A clean polling
    stop also saves the boundary after which offline messages bypass the age cap.
    """

    def __init__(self, path: Path) -> None:
        super().__init__()
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path)
        self._db.execute("PRAGMA synchronous = FULL")
        self._db.execute("CREATE TABLE IF NOT EXISTS updates ("
                         "update_id INTEGER PRIMARY KEY, payload TEXT, processed INTEGER NOT NULL DEFAULT 0)")
        self._db.execute("CREATE TABLE IF NOT EXISTS polling ("
                         "id INTEGER PRIMARY KEY CHECK (id = 1), stopped_at INTEGER NOT NULL)")
        self._db.commit()
        row = self._db.execute("SELECT stopped_at FROM polling WHERE id = 1").fetchone()
        self.previous_polling_stopped_at: int | None = row[0] if row else None
        self.polling_resumed_at: int | None = None
        self._stopping = False

    def polling_started(self, resumed_at: int | None = None) -> None:
        self.polling_resumed_at = int(time.time()) if resumed_at is None else resumed_at

    def polling_received(self, updates: list[dict[str, Any]]) -> None:
        """Commit the response before PTB sees it; retire the old boundary after the last page."""
        with self._db:
            for update in updates:
                self._db.execute("INSERT OR IGNORE INTO updates (update_id, payload) VALUES (?, ?)",
                                 (update["update_id"], json.dumps(update)))
            # PTB requests the default maximum of 100. A full page can leave old
            # updates at Telegram; a shutdown's final getUpdates must keep its stop.
            if len(updates) < 100 and not self._stopping:
                self._db.execute("DELETE FROM polling")

    def polling_stopped(self, stopped_at: int) -> None:
        self._stopping = True
        with self._db:
            # A failed recovery or repeated shutdown must not shorten the window.
            self._db.execute("INSERT OR IGNORE INTO polling (id, stopped_at) VALUES (1, ?)", (stopped_at,))

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
