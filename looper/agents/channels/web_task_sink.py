"""Streams one task Manager turn to the dashboard and stores its tool and event rows."""

from __future__ import annotations

import json
from collections.abc import Mapping
from enum import Enum
from typing import Any

from ..message import AgentAddress, MessageEnvelope, MessageType, task_manager_address


def _tool_payload(record: Any) -> dict[str, Any]:
    if isinstance(record, Mapping):
        data = record
    else:
        data = {"tool_use_id": record.tool_use_id, "name": record.name, "args": record.args,
                "result": record.result, "is_error": record.is_error}
    return {"tool_use_id": str(data.get("tool_use_id", "")), "name": str(data.get("name", "")),
            "args": str(data.get("args", "")), "result": str(data.get("result", "")),
            "is_error": bool(data.get("is_error", False))}


class WebTaskSink:
    """Publish a task turn to the web channel under one correlation id.

    ``reply_id`` is the user's message id. The assistant row is saved by the web adapter's awaiting path; this
    sink stores only the tool and event rows.
    """

    def __init__(self, bus: Any, web_address: AgentAddress, correlation_id: str, task_id: str, session_id: str,
                 reply_id: str) -> None:
        self._bus = bus
        self._web = web_address
        self._correlation_id = correlation_id
        self._task_id = task_id
        self._session_id = session_id
        self._reply_id = reply_id
        self._events = 0

    async def delta(self, text: str) -> None:
        await self._publish(MessageType.STATUS_UPDATE, {"text": text, "delta": True})

    async def tool(self, record: Any) -> None:
        from ...db.chat import save_message

        payload = _tool_payload(record)
        await save_message(session_id=self._session_id, message_id=f"{self._reply_id}:tool:{payload['tool_use_id']}",
                           role="tool", content=json.dumps(payload, ensure_ascii=False))
        await self._publish(MessageType.STATUS_UPDATE, {"tool_record": payload})

    async def event(self, kind: Enum | str, payload: Mapping[str, Any]) -> None:
        from ...db.chat import save_message

        self._events += 1
        name = kind.value if isinstance(kind, Enum) else str(kind)
        body = {"kind": name, **dict(payload)}
        await save_message(session_id=self._session_id, message_id=f"{self._reply_id}:event:{self._events}",
                           role="event", content=json.dumps(body, ensure_ascii=False))
        await self._publish(MessageType.STATUS_UPDATE, {"task_event": body})

    async def final(self, text: str, outcome: str) -> None:
        from ..task_commands import ENTRY_COMMAND_REPLY

        payload = {"text": text, "outcome": outcome}
        if outcome == "command":
            payload["entry_kind"] = ENTRY_COMMAND_REPLY
        await self._publish(MessageType.AGENT_REPLY, payload)

    async def _publish(self, message_type: MessageType, payload: dict[str, Any]) -> None:
        await self._bus.publish(MessageEnvelope.create(
            source=task_manager_address(self._task_id), target=self._web, message_type=message_type,
            payload=payload, correlation_id=self._correlation_id))
