"""A Manager's sessions while its session routing is on (``session_routing``): a worker per session, and the
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
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from .. import admission, lanes_settings, mid_turn_input, session_routing
from ..lanes import Lane
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


class CompletionState(StrEnum):
    QUEUED = "queued"
    STARTED = "started"
    EXPIRED = "expired"


@dataclass(eq=False)
class _Completion:
    envelope: MessageEnvelope
    rows: list[dict[str, Any]]
    batch: uuid.UUID
    state: CompletionState = CompletionState.QUEUED
    deadline: asyncio.TimerHandle | None = None


class _Queues:
    """Two FIFO lanes; sequence numbers preserve the original FIFO while lanes are off."""

    def __init__(self) -> None:
        self.interactive: deque[tuple[int, MessageEnvelope | _Completion]] = deque()
        self.background: deque[tuple[int, MessageEnvelope | _Completion]] = deque()
        self.ready = asyncio.Event()
        self.sequence = 0

    def put_nowait(self, item: MessageEnvelope | _Completion) -> None:
        background = isinstance(item, _Completion) or item.message_type is MessageType.IDLE_CHECK_DUE
        queue = self.background if background else self.interactive
        queue.append((self.sequence, item))
        self.sequence += 1
        self.ready.set()

    def qsize(self, lane: Lane | None = None) -> int:
        if lane is Lane.INTERACTIVE:
            return len(self.interactive)
        if lane is Lane.BACKGROUND:
            return len(self.background)
        return len(self.interactive) + len(self.background)

    def empty(self) -> bool:
        return not (self.interactive or self.background)

    async def get(self) -> MessageEnvelope | _Completion:
        while self.empty():
            self.ready.clear()
            await self.ready.wait()
        queue = self.interactive
        if not queue or (self.background and not lanes_settings.enabled()
                         and self.background[0][0] < queue[0][0]):
            queue = self.background
        return queue.popleft()[1]

    def remove(self, item: _Completion) -> None:
        for entry in self.background:
            if entry[1] is item:
                self.background.remove(entry)
                break

    def take_idle_checks(self) -> list[MessageEnvelope]:
        """Atomically remove unstarted checks before any asynchronous state writes."""
        checks = [item for _, item in self.background if isinstance(item, MessageEnvelope)
                  and item.message_type is MessageType.IDLE_CHECK_DUE]
        self.background = deque((sequence, item) for sequence, item in self.background
                                if not (isinstance(item, MessageEnvelope)
                                        and item.message_type is MessageType.IDLE_CHECK_DUE))
        return checks


class SessionRouter:
    """The workers of a Manager's sessions, and its resident processes' cap."""

    def __init__(self, agent: ManagerAgent) -> None:
        self._agent = agent
        self._queues: dict[str, _Queues] = {}
        self._expiring: set[asyncio.Task[None]] = set()
        self._deadlines: set[asyncio.TimerHandle] = set()
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
        main = self._agent._sessions[MAIN_SESSION]
        if kind is MessageType.IDLE_CHECK_DUE:
            return main if (lanes_settings.enabled() or session_routing.routes(self._agent.agent_id)
                            or self.holds(main) or admission.enabled()) else None
        if kind not in (MessageType.USER_MESSAGE, MessageType.COMPLETION_DUE):
            return None
        if kind is MessageType.COMPLETION_DUE and (lanes_settings.enabled() or admission.enabled()):
            return None  # claim in the loop; only the claimed batch joins the background lane
        if not session_routing.routes(self._agent.agent_id):
            return main if self.holds(main) or (kind is MessageType.USER_MESSAGE
                and (mid_turn_input.enabled(self._agent.agent_id) or lanes_settings.enabled()
                     or admission.enabled())) else None
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

    async def void_idle_checks(self, session: ManagerSession) -> None:
        """A user returned to main: checks still queued no longer have an idle period to inspect."""
        if session.key != MAIN_SESSION or (queue := self._queues.get(session.key)) is None:
            return
        from ...db import idle_checks

        for wake in queue.take_idle_checks():
            # A failed write leaves the wake unacknowledged; restart marks its running row failed.
            try:
                await idle_checks.settle(wake.payload["check_id"], "preempted")
            except Exception as exc:
                logger.warning("could not void idle check %s; restart will recover it: %s",
                               wake.payload["check_id"], exc, exc_info=True)
                continue
            if self._agent._bus is not None:
                self._agent._bus.settle_handled(wake)

    def _enqueue(self, session: ManagerSession, item: MessageEnvelope | _Completion) -> None:
        queue = self._queues.setdefault(session.key, _Queues())
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
        from ..managers_api import DEFAULT_MANAGER_ID

        if self._agent.agent_id != DEFAULT_MANAGER_ID and not origin["group"]:
            return session_routing.MAIN_KEY
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
        from ..managers_api import DEFAULT_MANAGER_ID

        if self._agent.agent_id != DEFAULT_MANAGER_ID and not origin["group"]:
            key = session_routing.MAIN_KEY
        session = (self.session(key) if session_routing.routes(self._agent.agent_id)
                   else self._agent._sessions[MAIN_SESSION])
        item = _Completion(envelope, rows, batch)
        self._completion_ids.add(envelope.message_id)
        self._enqueue(session, item)
        if lanes_settings.enabled():
            from ...db.manager_events import QUEUE_SECONDS

            remaining = (min(row["created_at"] for row in rows) - datetime.now(timezone.utc)).total_seconds()
            remaining += QUEUE_SECONDS
            if remaining <= 0:
                self._expire_completion(session, item)
            else:
                item.deadline = asyncio.get_running_loop().call_later(remaining, self._expire_completion, session, item)
                self._deadlines.add(item.deadline)

    def _expire_completion(self, session: ManagerSession, item: _Completion) -> None:
        # No await between check and transition: either this callback or the worker owns the batch.
        if item.state is not CompletionState.QUEUED:
            return
        item.state = CompletionState.EXPIRED
        self._cancel_deadline(item)
        self._queues[session.key].remove(item)
        task = asyncio.create_task(self._finish_expired(session, item), name="manager-completion-deadline")
        self._expiring.add(task)
        task.add_done_callback(self._expiring.discard)

    def _cancel_deadline(self, item: _Completion) -> None:
        if item.deadline is not None:
            item.deadline.cancel()
            self._deadlines.discard(item.deadline)
            item.deadline = None

    async def _finish_expired(self, session: ManagerSession, item: _Completion) -> None:
        self._agent._current.set(session)
        try:
            await self._agent._fall_back_completion(item.batch, "queue_deadline", {})
        except Exception:
            logger.exception("could not expire completion batch %s; restart will recover it", item.batch)
            return
        if self._agent._running and self._agent._bus is not None:
            self._agent._bus.settle_handled(item.envelope)
            self._completion_ids.discard(item.envelope.message_id)

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
                if isinstance(item, _Completion):
                    if item.state is not CompletionState.QUEUED:
                        continue
                    item.state = CompletionState.STARTED
                    self._cancel_deadline(item)
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
                    if isinstance(item, _Completion) or envelope.message_type is MessageType.USER_MESSAGE:
                        session.turn_ended_at = session.last_activity
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

    def queued_count(self, session: ManagerSession, lane: Lane | None = None) -> int:
        """How many messages wait in ``session``'s queue. The one a worker has already taken is not in it."""
        queue = self._queues.get(session.key)
        return 0 if queue is None else (queue.qsize() if lane is None else queue.qsize(lane))

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
        for handle in self._deadlines:
            handle.cancel()
        self._deadlines.clear()
        workers = [*self._workers.values(), *self._expiring]
        for worker in workers:
            worker.cancel()
        await asyncio.gather(*workers, return_exceptions=True)
        self._workers.clear()
        self._expiring.clear()
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
