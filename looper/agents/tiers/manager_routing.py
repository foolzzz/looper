"""The default Manager's sessions while session routing is on (``session_routing``): a worker per session, and the
cap on resident processes.

The Manager's message loop asks ``SessionRouter.target`` for each message. With routing on, a user message goes to
the session its key names and a completion wake-up to the session of the chat it tells; each session has a queue
and a worker task that runs its messages in order, on that session (``ManagerAgent._current``), and settles each
one on the bus once handled, so a message handed on and not finished replays at restart. Sessions run in parallel.
Messages that run no model turn stay in the loop.

At most ``session_routing.max_resident()`` sessions keep a process. Before a session other than main starts one at
the cap, the least recently active idle session other than main is stopped (its session id stays saved, and its
next message resumes it); when none is idle, the message waits, with a status line, until a turn ends. Main never
waits and is never stopped for another. With a cap of 1, a session other than main still runs its turn when only
main holds a process, and stops it once the turn is over.

Switched off, new messages go to main again: main's worker keeps the messages it holds in order, the other workers
finish theirs, and their processes stop once idle (``sweep``).
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .. import mid_turn_input, session_routing
from ..message import MessageEnvelope, MessageType
from ..runtimes import CLAUDE_CODE
from ..session_routing import SessionKey, SessionKind
from .manager_session import MAIN_SESSION, ManagerSession

if TYPE_CHECKING:
    from .manager import ManagerAgent

logger = logging.getLogger(__name__)

OPERATOR_ID_SECONDS = 30.0  # how long a bot's operator id is reused before it is read again
WAITING_STATUS = "排队中：其他会话正忙"


class IdentityUnavailable(Exception):
    """The identity store failed: retain the message instead of choosing main."""


@dataclass(frozen=True)
class _Completion:
    envelope: MessageEnvelope
    rows: list[dict[str, Any]]
    batch: uuid.UUID


class SessionRouter:
    """The workers of a Manager's sessions, and its resident processes' cap."""

    def __init__(self, agent: ManagerAgent) -> None:
        self._agent = agent
        self._queues: dict[str, asyncio.Queue[MessageEnvelope | _Completion]] = {}
        self._workers: dict[str, asyncio.Task[None]] = {}
        self._handling: dict[str, MessageEnvelope] = {}
        self._operator_ids: dict[str, tuple[float, str | None]] = {}
        self._resident = asyncio.Condition()
        self._reservations: set[str] = set()
        self._completion_ids: set[str] = set()

    # --- Where a message goes ---

    async def target(self, envelope: MessageEnvelope) -> ManagerSession | None:
        """The session whose worker takes ``envelope``; None when the message loop handles it itself: one that runs
        no model turn, or any message while routing is off and main's worker holds none."""
        kind = envelope.message_type
        if kind not in (MessageType.USER_MESSAGE, MessageType.COMPLETION_DUE):
            return None
        main = self._agent._sessions[MAIN_SESSION]
        if not session_routing.routes(self._agent.agent_id):
            return main if self.holds(main) or (kind is MessageType.USER_MESSAGE
                and mid_turn_input.enabled(self._agent.agent_id)) else None
        if kind is MessageType.COMPLETION_DUE:
            return None  # claim first in the handler; only that event can name the session
        from .. import completion_events

        while True:
            try:
                return self.session(await self._user_key(envelope))
            except IdentityUnavailable:
                # Not settled on the bus: shutdown/restart also retries this message.
                await asyncio.sleep(completion_events.RETRY_SECONDS)

    def dispatch(self, session: ManagerSession, envelope: MessageEnvelope) -> None:
        """Queue ``envelope`` for ``session``'s worker, started on first use."""
        self._enqueue(session, envelope)

    def _enqueue(self, session: ManagerSession, item: MessageEnvelope | _Completion) -> None:
        queue = self._queues.setdefault(session.key, asyncio.Queue())
        queue.put_nowait(item)
        worker = self._workers.get(session.key)
        if worker is None or worker.done():
            self._workers[session.key] = asyncio.create_task(self._work(session), name=f"manager-session:{session.key}")

    def session(self, key: SessionKey) -> ManagerSession:
        """The Manager's session of ``key``, made (not started) on first use; its saved state is read before its
        first turn (``ManagerAgent._load_session_state``)."""
        sessions = self._agent._sessions
        if key.text not in sessions:
            label = session_routing.resident_label(self._agent.agent_id, key)
            sessions[key.text] = ManagerSession(key=key.text, route=key, claude=CLAUDE_CODE.resident_session(label),
                                                claude_runtime=CLAUDE_CODE, last_activity=self._agent._clock())
        return sessions[key.text]

    async def _user_key(self, envelope: MessageEnvelope) -> SessionKey:
        from .manager import _message_client, _message_origin

        origin = _message_origin(envelope)
        client_id, _ = _message_client(envelope)
        operator = await self._operator_id(origin["channel_id"]) if not origin["group"] else None
        return session_routing.session_key_for(origin, client_id=client_id, operator_id=operator)

    async def completion_operator(self, channel_id: str) -> str | None:
        """Resolve identity before claiming: a failed read leaves the events pending for retry."""
        return await self._operator_id(channel_id)

    def dispatch_completion(self, envelope: MessageEnvelope, rows: list[dict[str, Any]], batch: uuid.UUID,
                            operator: str | None) -> None:
        """Queue a claimed batch on the session of the event actually taken, never an earlier peek."""
        from .manager import _completion_context, _message_origin

        origin = _message_origin(_completion_context(rows[0], self._agent.agent_address, batch))
        if origin["sender"] is None and origin["channel"] == "telegram" and not origin["group"]:
            origin["sender"] = origin["chat"]
        if origin["sender"] is None and not origin["group"] and origin["channel"] != "web" \
                and origin["channel_id"] and origin["chat"]:
            key = SessionKey(SessionKind.DM, origin["channel_id"], origin["chat"])
        else:
            key = session_routing.session_key_for(origin, client_id=None, operator_id=operator)
        self._completion_ids.add(envelope.message_id)
        self._enqueue(self.session(key), _Completion(envelope, rows, batch))

    def holds_completion(self, envelope: MessageEnvelope) -> bool:
        """A claimed wake-up is settled by its session worker when its turn ends."""
        return envelope.message_id in self._completion_ids

    async def _operator_id(self, channel_id: str | None) -> str | None:
        """The bot's identity, cached for ``OPERATOR_ID_SECONDS``. None means successfully read and unconfigured;
        a failed refresh retains a known identity, or pauses routing when none is known."""
        from ..operator_identity import operator_sender_id

        if not channel_id:
            return None
        cached = self._operator_ids.get(channel_id)
        now = time.monotonic()
        if cached is not None and now - cached[0] < OPERATOR_ID_SECONDS:
            return cached[1]
        try:
            value = await operator_sender_id(channel_id)
        except Exception:
            logger.warning("%s: cannot read the operator id of %s", self._agent._log, channel_id, exc_info=True)
            if cached is not None and cached[1] is not None:
                return cached[1]
            raise IdentityUnavailable(channel_id) from None
        self._operator_ids[channel_id] = (now, value)
        return value

    # --- The workers ---

    async def _work(self, session: ManagerSession) -> None:
        agent = self._agent
        agent._current.set(session)  # this task's turn code reads and writes this session
        queue = self._queues[session.key]
        try:
            while agent._running:
                if queue.empty() and not session_routing.routes(agent.agent_id):
                    break  # switched off: nothing left to keep in order here
                item = await queue.get()
                envelope = item.envelope if isinstance(item, _Completion) else item
                self._handling[session.key] = envelope
                try:
                    if isinstance(item, _Completion):
                        await agent._tell_completion(item.rows, item.batch)
                    else:
                        await agent._handle(envelope)
                except Exception:
                    logger.exception("error handling message %s in session %s", envelope.message_id, session.key)
                finally:
                    self._handling.pop(session.key, None)
                    session.last_activity = agent._clock()
                if not agent._running:
                    break  # left unsettled: a restart replays it
                if isinstance(item, _Completion) or not self.holds_completion(envelope):
                    if agent._bus is not None:
                        agent._bus.settle_handled(envelope)
                    self._completion_ids.discard(envelope.message_id)
                await self._turn_ended(session)
        finally:
            if self._workers.get(session.key) is asyncio.current_task():
                del self._workers[session.key]
            if session.key != MAIN_SESSION and not session_routing.routes(agent.agent_id):
                await self._stop_idle(session)

    def queued(self, session: ManagerSession) -> bool:
        """A queued command or message must run before later input can merge."""
        queue = self._queues.get(session.key)
        return queue is not None and not queue.empty()

    def queued_count(self, session: ManagerSession) -> int:
        """How many messages wait in ``session``'s queue. The one a worker has already taken is not in it."""
        queue = self._queues.get(session.key)
        return 0 if queue is None else queue.qsize()

    def holds(self, session: ManagerSession) -> bool:
        """Whether ``session``'s worker has a message: queued, or being handled."""
        queue = self._queues.get(session.key)
        return bool(queue is not None and not queue.empty()) or session.key in self._handling

    def handling(self) -> list[MessageEnvelope]:
        """The messages the workers are handling now."""
        return list(self._handling.values())

    def pending(self) -> bool:
        """Whether any worker has a message."""
        return any(self.holds(session) for session in self._agent._sessions.values())

    def idle(self, session: ManagerSession) -> bool:
        """Whether ``session`` runs nothing and holds nothing: its process may be stopped."""
        return not (session.key in self._reservations or self.holds(session)
                    or session.turn_lock.locked() or session.running_token is not None
                    or session.lifecycle_gate.locked())

    async def stop(self) -> None:
        """Cancel every worker; what one was handling stays unsettled (a restart replays it)."""
        workers = list(self._workers.values())
        for worker in workers:
            worker.cancel()
        await asyncio.gather(*workers, return_exceptions=True)
        self._workers.clear()
        self._reservations.clear()
        self._completion_ids.clear()

    # --- Resident processes ---

    async def admit(self, session: ManagerSession, envelope: MessageEnvelope | None) -> None:
        """Before ``session`` starts its process: make room under the cap, or wait for it (module docstring)."""
        agent = self._agent
        if not session_routing.routes(agent.agent_id):
            return
        told = False
        async with self._resident:
            while True:
                others = [s for s in agent._sessions.values() if s is not session
                          and (s.claude.started or s.key in self._reservations)]
                if len(others) < session_routing.max_resident():
                    self._reservations.add(session.key)
                    return
                idle = sorted((s for s in others if s.key != MAIN_SESSION and self.idle(s)),
                              key=lambda s: s.last_activity)
                if idle and await self._stop_idle(idle[0], notify=False):
                    logger.info("%s: stopped the idle session %s for %s", agent._log, idle[0].key, session.key)
                    continue
                if session.key == MAIN_SESSION or not any(s.key != MAIN_SESSION for s in others):
                    self._reservations.add(session.key)
                    return  # main never waits; nor does a session when only main holds a process
                if not told and envelope is not None:
                    told = True
                    await agent._send_status(envelope, WAITING_STATUS, phase=True)
                await self._resident.wait()

    async def release(self, session: ManagerSession) -> None:
        """A start succeeded, failed or was cancelled: release its reservation and wake waiting starts."""
        async with self._resident:
            self._reservations.discard(session.key)
            self._resident.notify_all()

    async def _turn_ended(self, session: ManagerSession) -> None:
        """A message of ``session`` was handled: over the cap (a cap of 1), its own process stops; waiting starts
        are woken."""
        started = [s for s in self._agent._sessions.values() if s.claude.started]
        if session.key != MAIN_SESSION and session.claude.started \
                and len(started) > session_routing.max_resident() and self.idle(session):
            await self._stop_idle(session, notify=False)
        await self.changed()

    async def changed(self) -> None:
        """A turn ended or a process stopped: a start waiting for room looks again."""
        async with self._resident:
            self._resident.notify_all()

    async def _stop_idle(self, session: ManagerSession, *, notify: bool = True) -> bool:
        """Stop ``session``'s process if it is idle and its runtime agrees (``try_stop_if_idle``): its session id
        stays saved. Whether it stopped."""
        if not session.claude.started or not self.idle(session):
            return False
        close = getattr(session.claude, "try_stop_if_idle", None)
        if close is None:
            return False
        async with session.lifecycle_gate:
            try:
                stopped = bool(await close())
            except Exception as exc:
                logger.warning("%s: could not stop the idle session %s (%s)", self._agent._log, session.key,
                               type(exc).__name__)
                return False
        if stopped and notify:
            await self.changed()
        return stopped

    async def sweep(self) -> None:
        """Routing off: stop the idle processes of the sessions other than main (the Manager's idle sweep)."""
        if session_routing.routes(self._agent.agent_id):
            return
        for session in list(self._agent._sessions.values()):
            if session.key != MAIN_SESSION:
                await self._stop_idle(session)

    def resident_count(self) -> int:
        return sum(1 for session in self._agent._sessions.values() if session.claude.started)
