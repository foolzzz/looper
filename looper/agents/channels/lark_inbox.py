"""Per-channel restart boundary and bounded message receipts in the runtime root."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from .lark_cli import RECEIVE_EVENT

PROCESSED_KEPT = 20_000


class LarkInbox:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path)
        self._db.execute("PRAGMA synchronous = FULL")
        self._db.execute("CREATE TABLE IF NOT EXISTS boundary (id INTEGER PRIMARY KEY CHECK (id = 1), at REAL NOT NULL)")
        self._db.execute("CREATE TABLE IF NOT EXISTS processed (seq INTEGER PRIMARY KEY, message_id TEXT UNIQUE NOT NULL)")
        self._db.execute("CREATE TABLE IF NOT EXISTS catchup_debt (chat_id TEXT PRIMARY KEY, start REAL NOT NULL, end REAL NOT NULL)")
        self._db.execute("CREATE TABLE IF NOT EXISTS discovery (id INTEGER PRIMARY KEY CHECK (id = 1), start REAL NOT NULL, end REAL NOT NULL)")
        self._db.commit()
        row = self._db.execute("SELECT at FROM boundary WHERE id = 1").fetchone()
        self.boundary: float | None = row[0] if row else None

    def checkpoint(self, at: float) -> None:
        with self._db:
            self._db.execute("INSERT INTO boundary VALUES (1, ?) ON CONFLICT(id) DO UPDATE SET at = excluded.at", (at,))
        self.boundary = at

    def discovery(self) -> tuple[float, float] | None:
        return self._db.execute("SELECT start, end FROM discovery WHERE id = 1").fetchone()

    def defer_discovery(self, start: float, end: float) -> None:
        with self._db:
            self._db.execute("INSERT INTO discovery VALUES (1, ?, ?) ON CONFLICT(id) DO UPDATE SET "
                             "start = min(start, excluded.start), end = max(end, excluded.end)", (start, end))

    def complete_discovery(self) -> None:
        with self._db:
            self._db.execute("DELETE FROM discovery")

    def debts(self) -> dict[str, tuple[float, float]]:
        return {chat: (start, end) for chat, start, end in
                self._db.execute("SELECT chat_id, start, end FROM catchup_debt ORDER BY start, end, chat_id")}

    def defer(self, chat_id: str, start: float, end: float) -> None:
        with self._db:
            self._db.execute(
                "INSERT INTO catchup_debt VALUES (?, ?, ?) ON CONFLICT(chat_id) DO UPDATE SET "
                "start = excluded.start, end = excluded.end", (chat_id, start, end))

    def complete(self, chat_id: str) -> None:
        with self._db:
            self._db.execute("DELETE FROM catchup_debt WHERE chat_id = ?", (chat_id,))

    def processed(self, message_id: str) -> bool:
        return self._db.execute("SELECT 1 FROM processed WHERE message_id = ?", (message_id,)).fetchone() is not None

    def finish(self, message_id: str) -> None:
        with self._db:
            self._db.execute("INSERT OR IGNORE INTO processed(message_id) VALUES (?)", (message_id,))
            self._db.execute("DELETE FROM processed WHERE seq <= (SELECT max(seq) FROM processed) - ?", (PROCESSED_KEPT,))

    def close(self) -> None:
        self._db.close()


def history_event(message: dict[str, Any], chat_id: str, chat_type: str) -> dict[str, Any]:
    """The transport normalizes raw history content and mentions like the live consumer."""
    sender = message.get("sender") or {}
    sender_id = sender.get("id") or ""
    if isinstance(sender_id, dict):
        sender_id = sender_id.get("open_id") or ""
    mentions = []
    for item in message.get("mentions") or []:
        identity = item.get("id") or ""
        if isinstance(identity, dict):
            identity = identity.get("open_id") or ""
        mentions.append({"id": identity, "key": item.get("key") or "", "name": item.get("name") or ""})
    return {"type": RECEIVE_EVENT, "chat_id": chat_id, "chat_type": chat_type,
            "message_id": message["message_id"], "message_type": message["msg_type"],
            "sender_id": sender_id, "sender_type": sender.get("sender_type") or sender.get("type") or "",
            "mentions": mentions, "content": message.get("content") or "",
            "reply_to": message.get("reply_to") or message.get("parent_id") or "",
            "root_id": message.get("root_id") or ""}
