"""The token a user message carries through its whole turn: typed phases and one cancellation primitive."""

from __future__ import annotations

import asyncio
import secrets
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import TYPE_CHECKING, Any, TypeVar

from ..runtimes.cancel import TurnCancel, TurnCancelled

if TYPE_CHECKING:
    from ..message import MessageEnvelope
    from ..ops import Turn

T = TypeVar("T")

# Tests only: awaited with a step's name before the step runs, to stop the turn at that exact point.
FAULTS: Callable[[str], Awaitable[None]] | None = None


class TurnStopped(Exception):
    """The user stopped the turn; ``partial`` is what Claude had written."""

    def __init__(self, partial: str = "") -> None:
        super().__init__("stopped by the user")
        self.partial = partial


class TurnPhase(str, Enum):
    RECEIVED = "received"
    PREPARING = "preparing"
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"
    ENDED = "ended"


class TurnKind(str, Enum):
    MODEL = "model"
    CODE = "code"


@dataclass(frozen=True)
class StopRequest:
    """A user's request to stop the running turn: where it came from, when, and the phase it found the turn in."""

    via: str
    at: datetime
    phase: TurnPhase | None = None


_ALLOWED = {
    (TurnPhase.RECEIVED, TurnPhase.PREPARING),
    (TurnPhase.PREPARING, TurnPhase.STARTING),
    (TurnPhase.STARTING, TurnPhase.RUNNING),
}


@dataclass(eq=False)
class TurnToken:
    message_id: str
    client_message_id: str | None
    channel: str
    chat: str
    received_at: datetime
    binding: str = field(default_factory=lambda: secrets.token_urlsafe(18))
    phase: TurnPhase = TurnPhase.RECEIVED
    stop: StopRequest | None = None
    cancel: TurnCancel = field(default_factory=TurnCancel)
    ops_turn: Turn | None = None
    kind: TurnKind = TurnKind.MODEL
    outcome: Any = None  # ReplyOutcome
    method: Any = None  # StopMethod: how the runtime ended the turn; None until known
    envelope: MessageEnvelope | None = field(default=None, repr=False)  # what the stop's status is published for
    on_settle: Callable[[TurnToken], None] | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        self.cancel.on_sent(self._prompt_sent)

    def _prompt_sent(self) -> None:
        if self.phase is TurnPhase.STARTING:
            self.phase = TurnPhase.RUNNING

    @property
    def live(self) -> bool:
        return self.phase is not TurnPhase.ENDED

    def advance(self, to: TurnPhase) -> None:
        if self.phase is TurnPhase.STOPPING:
            raise TurnStopped()
        if (self.phase, to) not in _ALLOWED:
            raise AssertionError(f"turn {self.message_id}: {self.phase.value} -> {to.value} is not a transition")
        self.phase = to

    def restart(self) -> None:
        """The resume retry: running goes back to starting."""
        if self.phase is TurnPhase.STOPPING:
            raise TurnStopped()
        if self.phase is not TurnPhase.RUNNING:
            raise AssertionError(f"turn {self.message_id}: restart from {self.phase.value}")
        self.phase = TurnPhase.STARTING

    def request_stop(self, via: str) -> bool:
        if self.phase in (TurnPhase.ENDED, TurnPhase.STOPPING):
            return False
        self.stop = StopRequest(via=via, at=datetime.now(timezone.utc), phase=self.phase)
        self.phase = TurnPhase.STOPPING
        self.cancel.request()
        return True

    def settle(self, outcome: Any = None) -> None:
        if self.phase is TurnPhase.ENDED:
            return
        self.phase = TurnPhase.ENDED
        self.outcome = outcome
        if self.on_settle is not None:
            self.on_settle(self)

    def check(self) -> None:
        if self.phase is TurnPhase.STOPPING:
            raise TurnStopped()

    async def step(self, name: str, aw: Awaitable[T]) -> T:
        """Await ``aw`` unless the turn is stopped first: a stop abandons it and raises ``TurnStopped``."""
        try:
            if FAULTS is not None:
                await FAULTS(name)
            return await self.cancel.race(aw)
        except TurnCancelled:
            raise TurnStopped() from None

    async def keep(self, name: str, aw: Awaitable[T], timeout: float) -> T:
        """Await ``aw`` to completion (bounded by ``timeout``), then look at the stop."""
        if FAULTS is not None:
            await FAULTS(name)
        async with asyncio.timeout(timeout):
            result = await asyncio.shield(aw)
        self.check()
        return result


class TurnRegistry:
    def __init__(self) -> None:
        self._tokens: dict[str, TurnToken] = {}

    def receive(self, envelope: MessageEnvelope, *, channel: str, chat: str,
                client_message_id: str | None = None) -> TurnToken:
        token = TurnToken(
            message_id=envelope.message_id,
            client_message_id=client_message_id,
            channel=channel,
            chat=chat,
            received_at=datetime.now(timezone.utc),
            envelope=envelope,
            on_settle=self.drop,
        )
        self._tokens[token.message_id] = token
        return token

    def find(self, id: str) -> TurnToken | None:
        token = self._tokens.get(id)
        if token is not None and token.live:
            return token
        for token in self._tokens.values():
            if token.live and token.client_message_id == id:
                return token
        return None

    def live(self) -> list[TurnToken]:
        return [t for t in self._tokens.values() if t.live]

    def live_for(self, channel: str, chat: str) -> list[TurnToken]:
        return [t for t in self._tokens.values() if t.live and t.channel == channel and t.chat == chat]

    def oldest_for(self, channel: str, chat: str) -> TurnToken | None:
        live = self.live_for(channel, chat)
        return live[0] if live else None

    def drop(self, token: TurnToken) -> None:
        if self._tokens.get(token.message_id) is token:
            del self._tokens[token.message_id]

    def clear(self) -> None:
        self._tokens.clear()
