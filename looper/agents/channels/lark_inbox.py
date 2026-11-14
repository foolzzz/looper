"""Per-channel restart boundary and bounded message receipts in the runtime root."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from .lark_cli import RECEIVE_EVENT
from .sqlite_inbox import SQLiteInbox, serialized

PROCESSED_KEPT = 20_000
THREADS_KEPT = 1_000
THREAD_MAX_AGE = 7 * 24 * 60 * 60
BOT_ACTIVITY_MAX_AGE = 7 * 24 * 60 * 60


class LarkInbox(SQLiteInbox):
    def _open(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path)
        self._db.execute("PRAGMA synchronous = FULL")
        self._db.execute("CREATE TABLE IF NOT EXISTS boundary (id INTEGER PRIMARY KEY CHECK (id = 1), at REAL NOT NULL)")
        self._db.execute("CREATE TABLE IF NOT EXISTS processed (seq INTEGER PRIMARY KEY, message_id TEXT UNIQUE NOT NULL)")
        self._db.execute("CREATE TABLE IF NOT EXISTS catchup_debt (chat_id TEXT PRIMARY KEY, start REAL NOT NULL, end REAL NOT NULL)")
        self._db.execute("CREATE TABLE IF NOT EXISTS discovery (id INTEGER PRIMARY KEY CHECK (id = 1), start REAL NOT NULL, end REAL NOT NULL)")
        self._db.execute("CREATE TABLE IF NOT EXISTS thread_activity (chat_id TEXT NOT NULL, thread_id TEXT NOT NULL, "
                         "at REAL NOT NULL, PRIMARY KEY (chat_id, thread_id))")
        self._db.execute("CREATE TABLE IF NOT EXISTS bot_activity (chat_id TEXT PRIMARY KEY, at REAL NOT NULL)")
        self._db.commit()
        row = self._db.execute("SELECT at FROM boundary WHERE id = 1").fetchone()
        self.boundary: float | None = row[0] if row else None

    @serialized
    def checkpoint(self, at: float) -> None:
        with self._db:
            self._db.execute("INSERT INTO boundary VALUES (1, ?) ON CONFLICT(id) DO UPDATE SET at = excluded.at", (at,))
        self.boundary = at

    @serialized
    def discovery(self) -> tuple[float, float] | None:
        return self._db.execute("SELECT start, end FROM discovery WHERE id = 1").fetchone()

    @serialized
    def defer_discovery(self, start: float, end: float) -> None:
        with self._db:
            self._db.execute("INSERT INTO discovery VALUES (1, ?, ?) ON CONFLICT(id) DO UPDATE SET "
                             "start = min(start, excluded.start), end = max(end, excluded.end)", (start, end))

    @serialized
    def complete_discovery(self) -> None:
        with self._db:
            self._db.execute("DELETE FROM discovery")

    @serialized
    def debts(self) -> dict[str, tuple[float, float]]:
        return {chat: (start, end) for chat, start, end in
                self._db.execute("SELECT chat_id, start, end FROM catchup_debt ORDER BY start, end, chat_id")}

    @serialized
    def defer(self, chat_id: str, start: float, end: float) -> None:
        with self._db:
            self._db.execute(
                "INSERT INTO catchup_debt VALUES (?, ?, ?) ON CONFLICT(chat_id) DO UPDATE SET "
                "start = excluded.start, end = excluded.end", (chat_id, start, end))

    @serialized
    def complete(self, chat_id: str) -> None:
        with self._db:
            self._db.execute("DELETE FROM catchup_debt WHERE chat_id = ?", (chat_id,))

    @serialized
    def processed(self, message_id: str) -> bool:
        return self._db.execute("SELECT 1 FROM processed WHERE message_id = ?", (message_id,)).fetchone() is not None

    @serialized
    def finish(self, message_id: str, *, chat_id: str = "", thread_id: str = "", at: float | None = None) -> None:
        with self._db:
            self._db.execute("INSERT OR IGNORE INTO processed(message_id) VALUES (?)", (message_id,))
            self._db.execute("DELETE FROM processed WHERE seq <= (SELECT max(seq) FROM processed) - ?", (PROCESSED_KEPT,))
            if chat_id and thread_id and at is not None:
                self._store_thread(chat_id, thread_id, at)

    @serialized
    def remember_thread(self, chat_id: str, thread_id: str, at: float) -> None:
        with self._db:
            self._store_thread(chat_id, thread_id, at)

    def _store_thread(self, chat_id: str, thread_id: str, at: float) -> None:
        self._db.execute("INSERT INTO thread_activity VALUES (?, ?, ?) ON CONFLICT(chat_id, thread_id) "
                         "DO UPDATE SET at = max(at, excluded.at)", (chat_id, thread_id, at))
        self._prune_threads(at)

    def _prune_threads(self, at: float) -> None:
        self._db.execute("DELETE FROM thread_activity WHERE at < ?", (at - THREAD_MAX_AGE,))
        self._db.execute("DELETE FROM thread_activity WHERE rowid IN (SELECT rowid FROM thread_activity "
                         "ORDER BY at DESC, rowid DESC LIMIT -1 OFFSET ?)", (THREADS_KEPT,))

    @serialized
    def recent_threads(self, chat_id: str, at: float) -> list[str]:
        with self._db:
            self._prune_threads(at)
        return [row[0] for row in self._db.execute("SELECT thread_id FROM thread_activity WHERE chat_id = ? "
                                                 "ORDER BY at DESC, rowid DESC", (chat_id,))]

    @serialized
    def has_thread_activity(self, chat_id: str, since: float) -> bool:
        return self._db.execute("SELECT 1 FROM thread_activity WHERE chat_id = ? AND at >= ? LIMIT 1",
                                (chat_id, since)).fetchone() is not None

    @serialized
    def remember_bot(self, chat_id: str, at: float) -> None:
        with self._db:
            self._db.execute("INSERT INTO bot_activity VALUES (?, ?) ON CONFLICT(chat_id) "
                             "DO UPDATE SET at = max(at, excluded.at)", (chat_id, at))

    @serialized
    def recent_bot_chats(self, at: float) -> set[str]:
        with self._db:
            self._db.execute("DELETE FROM bot_activity WHERE at < ?", (at - BOT_ACTIVITY_MAX_AGE,))
        return {row[0] for row in self._db.execute("SELECT chat_id FROM bot_activity")}

def history_event(message: dict[str, Any], chat_id: str, chat_type: str) -> dict[str, Any]:
    """The transport normalizes raw history content and mentions like the live consumer."""
    sender = message.get("sender") or {}
    sender_id = sender.get("open_bot_id") or sender.get("id") or ""
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
            "sender_id": sender_id, "sender_app_id": sender.get("id") if isinstance(sender.get("id"), str) else "",
            "sender_type": sender.get("sender_type") or sender.get("type") or "",
            "mentions": mentions, "content": message.get("content") or "",
            "reply_to": message.get("reply_to") or message.get("parent_id") or "",
            "root_id": message.get("root_id") or "", "thread_id": message.get("thread_id") or ""}
