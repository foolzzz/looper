"""Synthetic message stream data for the tests: a directory of per-chat NDJSON files in the shape an outside
collector writes (one raw Lark message per line), plus its name files. Nothing here is sampled from real data."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ME = "ou_me_synthetic"
ALICE = "ou_alice_synthetic"
BOB = "ou_bob_synthetic"
LOOPER_APP = "cli_looper_synthetic"
OTHER_APP = "cli_other_synthetic"
NOW = datetime(2026, 11, 18, 12, 0, 0, tzinfo=timezone.utc)


def message(message_id: str, *, text: str = "hello", sent_at: datetime = NOW - timedelta(minutes=5),
            sender: str = ALICE, sender_type: str = "user", mentions: list[tuple[str, str]] = (),
            msg_type: str = "text", thread_id: str | None = None, root_id: str | None = None,
            parent_id: str | None = None) -> dict[str, Any]:
    """One raw ``im/v1/messages`` item; ``mentions`` is ``[(open_id, name), ...]``, written as ``@_user_N`` keys."""
    body_text = text
    items = []
    for number, (open_id, name) in enumerate(mentions, 1):
        body_text += f" @_user_{number}"
        items.append({"key": f"@_user_{number}", "id": open_id, "id_type": "open_id", "name": name})
    raw: dict[str, Any] = {
        "message_id": message_id, "msg_type": msg_type, "create_time": str(int(sent_at.timestamp() * 1000)),
        "sender": {"id": sender, "id_type": "app_id" if sender_type == "app" else "open_id",
                   "sender_type": sender_type},
        "body": {"content": json.dumps({"text": body_text})}, "mentions": items,
    }
    for key, value in (("thread_id", thread_id), ("root_id", root_id), ("parent_id", parent_id)):
        if value:
            raw[key] = value
    return raw


def line(raw: dict[str, Any]) -> bytes:
    return (json.dumps(raw, ensure_ascii=False) + "\n").encode()


def append(directory: Path, chat_id: str, *messages: dict[str, Any], partial: bytes = b"") -> Path:
    """Append whole lines (and optionally an unterminated tail) to the chat's file."""
    path = directory / "chats" / f"{chat_id}.ndjson"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("ab") as handle:
        for raw in messages:
            handle.write(line(raw))
        handle.write(partial)
    return path


def write_names(directory: Path, *, chats: dict[str, dict[str, str]] | None = None,
                users: dict[str, dict[str, str]] | None = None) -> None:
    if chats is not None:
        (directory / "chats.json").write_text(json.dumps(chats))
    if users is not None:
        (directory / "users.json").write_text(json.dumps(users))
