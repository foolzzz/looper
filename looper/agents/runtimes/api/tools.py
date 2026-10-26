"""The Manager's platform tools, called in the server's process."""
from __future__ import annotations

import json

from ...ops.service import ManagerOps
from .loop import ToolCall, ToolOutcome, ToolSpec


class ManagerTurnTools:
    def __init__(self, ops: ManagerOps, binding: str | None) -> None:
        self._ops = ops
        self._binding = binding

    async def specs(self) -> list[ToolSpec]:
        return [ToolSpec(tool["name"], tool["description"], tool["inputSchema"])
                for tool in await self._ops.current_listing()]

    async def call(self, call: ToolCall) -> ToolOutcome:
        try:
            arguments = json.loads(call.arguments or "{}")
        except ValueError:
            arguments = None
        if not isinstance(arguments, dict):
            return ToolOutcome(json.dumps({"status": "error", "error": {
                "code": "invalid", "message": "the arguments must be a JSON object"}}, ensure_ascii=False),
                is_error=True, ran=False)
        answer = await self._ops.call(call.name, arguments, binding=self._binding, call_id=call.call_id)
        text = json.dumps(answer, ensure_ascii=False)
        encoded = text.encode("utf-8")
        if len(encoded) > 102_400:
            note = (f"\n[Looper] This result was cut from {len(encoded)} bytes to 100 KB. "
                    "Ask for less (a filter, one task id).")
            text = encoded[:102_400 - len(note.encode("utf-8"))].decode("utf-8", errors="ignore") + note
        return ToolOutcome(text, is_error=answer.get("status") == "error", ran=True)
