"""Versioned neutral conversation history persisted at completed-request boundaries."""
from __future__ import annotations

import json

from ....db import api_sessions
from ....db.api_sessions import TranscriptMismatch as TranscriptMismatch
from .. import ApiProtocol
from .loop import (
    MessageItem, SessionItem, ToolResultItem, TranscriptFormatError,
    item_from_json, item_to_json,
)


class DbTranscript:
    def __init__(self, session_id: str, protocol: ApiProtocol, model: str):
        self.session_id, self.protocol, self.model = session_id, protocol, model
        self._history: list[SessionItem] | None = None
        self._repair: list[SessionItem] = []
        self._turn: int | None = None

    async def items(self) -> list[SessionItem]:
        if self._history is None:
            rows = await api_sessions.load(self.session_id)
            history = []
            try:
                for row in rows:
                    if row['protocol'] != self.protocol.value:
                        raise TranscriptMismatch('session protocol does not match')
                    value = json.loads(row['item']) if isinstance(row['item'], str) else row['item']
                    history.append(item_from_json(value))
            except (TranscriptFormatError, ValueError, TypeError) as exc:
                raise TranscriptMismatch('invalid versioned session history') from exc
            self._turn = (await api_sessions.last_turn(self.session_id) or 0) + 1
            self._history = history
            if history and isinstance(history[-1], ToolResultItem):
                # Only commit this note together with the next completed request.
                self._repair = [MessageItem('assistant', '(the previous turn did not finish)')]
        return [*self._history, *self._repair]

    async def append(self, items: list[SessionItem]) -> None:
        await self.items()
        batch = [*self._repair, *items]
        values = [item_to_json(item) for item in batch]
        await api_sessions.append(self.session_id, self._turn, self.protocol.value, self.model, values)
        self._history.extend(batch)
        self._repair = []
