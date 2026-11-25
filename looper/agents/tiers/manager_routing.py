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

from .. import admission, chat_scopes, lanes_settings, mid_turn_input, session_routing, session_visibility
from ..lanes import Lane
from ..message import MessageEnvelope, MessageType
from ..runtimes import CLAUDE_CODE
from ..session_routing import SessionKey, SessionKind
from .manager_session import MAIN_SESSION, ManagerSession, Retirement
from ..chat_scopes import ScopeInvariantViolation, ScopeUnavailable

if TYPE_CHECKING:
    from .manager import ManagerAgent

logger = logging.getLogger(__name__)

OPERATOR_ID_SECONDS = 30.0  # how long a bot's operator id is reused before it is read again
WAITING_STATUS = "排队中：其他会话正忙"


SCOPE_UNAVAILABLE_REPLY = "这个群的能力范围设置现在用不了，没有执行任何操作；请操作者检查这个群的范围。"


class RouteOutcome(StrEnum):
    ANSWERED = "answered"
    HELD = "held"


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
        self._principals: dict[str, tuple[chat_scopes.Principal, chat_scopes.SessionScope]] = {}
        self._completion_scopes: dict[str, chat_scopes.SessionScope] = {}
        self._reconciling: dict[tuple[str, str], asyncio.Lock] = {}
        self._resolving: set[str] = set()
        self._ending: set[str] = set()

    # --- Where a message goes ---

    async def target(self, envelope: MessageEnvelope) -> ManagerSession | RouteOutcome | None:
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
                key = await self._user_key(envelope)
                return self.session(key, self._scope(envelope))
            except (ScopeUnavailable, ScopeInvariantViolation) as exc:
                return await self._scope_error(envelope, exc)
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

    def session(self, key: SessionKey, scope: chat_scopes.SessionScope | None = None) -> ManagerSession:
        """The Manager's session of ``key``, made (not started) on first use; its saved state is read before its
        first turn (``ManagerAgent._load_session_state``)."""
        sessions = self._agent._sessions
        if key.text not in sessions:
            label = session_routing.resident_label(self._agent.agent_id, key)
            sessions[key.text] = ManagerSession(key=key.text, route=key, claude=CLAUDE_CODE.resident_session(label),
                                                claude_runtime=CLAUDE_CODE, last_activity=self._agent._clock(), scope=scope)
            if scope is not None and scope.mode is chat_scopes.LaunchMode.PLATFORM_TOOLS:
                session_visibility.register_scoped(self._agent.agent_id, key.text,
                                                   session_visibility.SCOPED_CHAT_ALLOWLIST)
        session = sessions[key.text]
        if (session.scope.fingerprint if session.scope else None) != (scope.fingerprint if scope else None):
            raise ScopeInvariantViolation("session scope differs from its key")
        if session.retired is not None or session.key in self._ending:
            raise IdentityUnavailable("the previous session is still retiring")
        return session

    def _scope(self, envelope: MessageEnvelope) -> chat_scopes.SessionScope | None:
        recorded = self._principals.get(envelope.message_id)
        return recorded[1] if recorded else None

    def principal(self, envelope: MessageEnvelope, session: ManagerSession) -> chat_scopes.Principal:
        recorded = self._principals.get(envelope.message_id)
        if recorded is None or session.scope is None or recorded[1].fingerprint != session.scope.fingerprint:
            raise ScopeInvariantViolation("turn principal differs from its session scope")
        return recorded[0]

    async def _scope_error(self, envelope: MessageEnvelope, exc: Exception) -> RouteOutcome:
        from .manager import _message_origin

        origin = _message_origin(envelope)
        digest = session_routing.key_digest(str(origin["chat"]))
        self._principals.pop(envelope.message_id, None)
        if isinstance(exc, ScopeUnavailable):
            logger.warning("%s: chat %s scope %s unavailable: %s", self._agent.agent_id, digest, exc.scope, exc.reason)
            await self._agent._send_reply(envelope, SCOPE_UNAVAILABLE_REPLY, error="scope_unavailable")
            return RouteOutcome.ANSWERED
        logger.critical("%s: chat %s scope invariant: %s", self._agent.agent_id, digest, exc)
        return RouteOutcome.HELD

    async def _scoped_key(self, envelope: MessageEnvelope) -> SessionKey:
        from .manager import _message_origin, _sender_kind

        origin = _message_origin(envelope)
        key, scope = await self._resolve_scope(origin, _sender_kind(envelope))
        resolved = scope.resolved
        self._principals[envelope.message_id] = (chat_scopes.Principal(
            resolved.audience, resolved.principal_kind, resolved.scope_id, scope.version), scope)
        return key

    async def _resolve_scope(self, origin: dict[str, Any], kind: chat_scopes.PrincipalKind | None
                             ) -> tuple[SessionKey, chat_scopes.SessionScope]:
        from ...db import chat_bindings

        channel, chat = origin["channel_id"], origin["chat"]
        operator = await self._operator_id(channel)
        try:
            config = await chat_bindings.scope_config(channel, chat, self._agent.agent_id)
        except Exception as exc:
            raise IdentityUnavailable("cannot read the chat scope") from exc
        resolved = chat_scopes.resolve(sender=origin["sender"], sender_kind=kind, operator_id=operator, config=config)
        scope = await self._session_scope(resolved)
        key = SessionKey(SessionKind.GROUP, channel, chat, scope_fingerprint=scope.fingerprint)
        await self.reconcile(channel, chat, config=config, operator_id=operator)
        return key, scope

    async def _session_scope(self, resolved: chat_scopes.Resolved) -> chat_scopes.SessionScope:
        from ...db import capability_sets

        try:
            sets = await capability_sets.current([resolved.scope_id])
        except Exception as exc:
            raise IdentityUnavailable("cannot read the capability set") from exc
        if resolved.scope_id not in sets:
            raise ScopeUnavailable(resolved.scope_id, "set is missing or deleted")
        version, items = sets[resolved.scope_id]
        scope = chat_scopes.session_scope(resolved, version, items, self._agent._definition,
            supported=frozenset(chat_scopes.LaunchMode), inputs_version=chat_scopes.inputs_version(resolved))
        if any(isinstance(item, chat_scopes.FilesRead) and not item.required for item in scope.effective.items):
            raise ScopeUnavailable(resolved.scope_id, "optional items need the session's runtime at routing")
        return scope

    async def reconcile(self, channel_id: str, chat: str, *, config: chat_scopes.ScopeConfig,
                        operator_id: str | None, stop_now: bool = False) -> None:
        from ...db import manager_sessions

        retired = []
        async with self._reconciling.setdefault((channel_id, chat), asyncio.Lock()):
            resolutions = [chat_scopes.resolve(sender=None, sender_kind=kind, operator_id=None, config=config)
                           for kind in chat_scopes.PrincipalKind]
            if operator_id:
                resolutions.append(chat_scopes.resolve(sender=operator_id, sender_kind=chat_scopes.PrincipalKind.PERSON,
                                                       operator_id=operator_id, config=config))
            for sender, override in config.overrides.items():
                resolutions.append(chat_scopes.resolve(sender=sender, sender_kind=override.kind,
                                                       operator_id=operator_id, config=config))
            reachable = {(await self._session_scope(resolved)).fingerprint for resolved in resolutions}
            try:
                rows = await manager_sessions.active_for_chat(self._agent.agent_id, channel_id, chat)
                keys = [key for key, fingerprint in rows if fingerprint not in reachable]
                await manager_sessions.retire(self._agent.agent_id, keys, Retirement.SCOPE_CHANGED.value)
            except Exception as exc:
                raise IdentityUnavailable("cannot retire obsolete sessions") from exc
            for session in list(self._agent._sessions.values()):
                if session.route.kind is not SessionKind.GROUP or session.route.channel_id != channel_id \
                        or session.route.chat != chat:
                    continue
                if session.scope is None or session.scope.fingerprint not in reachable:
                    session.retired = Retirement.SCOPE_CHANGED
                if session.retired is not None:
                    retired.append(session)
        for session in retired:
            if self.idle(session):
                await self._end_retired(session)
            elif stop_now and (token := session.running_token) is not None:
                await self._agent.stop_turn(token.message_id, via="scope_change")

    async def _end_retired(self, session: ManagerSession, first: MessageEnvelope | _Completion | None = None) -> None:
        if session.key in self._ending or self._agent._sessions.get(session.key) is not session:
            return
        from ...db import manager_sessions

        self._ending.add(session.key)
        queue = self._queues.get(session.key)
        pending = [first] if first is not None else []
        while queue is not None and not queue.empty():
            pending.append(await queue.get())
        try:
            # Transfer queues before ending the process. A return to this same key waits until the old tree
            # is gone, so it cannot resume or share the retiring resident.
            deferred = [item for item in pending if not await self._redispatch(item, retiring_key=session.key)]
            async with session.lifecycle_gate:
                await session.claude.stop(kill=True)
            # The last turn may have inserted its first row after reconcile's UPDATE.
            await manager_sessions.retire(self._agent.agent_id, [session.key], Retirement.SCOPE_CHANGED.value)
            if self._agent.ops is not None:
                self._agent.ops.retire_session(session.key)
            session_visibility.forget_scoped(self._agent.agent_id, session.key)
            worker = self._workers.get(session.key)
            if worker is not None and worker is not asyncio.current_task():
                worker.cancel()
                await asyncio.gather(worker, return_exceptions=True)
            if self._workers.get(session.key) is worker:
                self._workers.pop(session.key, None)
            self._agent._sessions.pop(session.key, None)
            self._queues.pop(session.key, None)
            self._completion_scopes.pop(session.key, None)
            for item in deferred:
                if isinstance(item, MessageEnvelope):
                    self._principals.pop(item.message_id, None)
            await self.changed()
            self._ending.discard(session.key)
            for item in deferred:
                await self._redispatch(item)
        finally:
            self._ending.discard(session.key)

    async def _redispatch(self, item: MessageEnvelope | _Completion, *, retiring_key: str | None = None) -> bool:
        from .manager import _completion_origin
        from .. import completion_events

        while True:
            try:
                if not session_routing.routes(self._agent.agent_id):
                    key, scope = session_routing.MAIN_KEY, None
                elif isinstance(item, _Completion):
                    key = await self.completion_key(_completion_origin(item.rows[0], self._agent.agent_address, item.batch))
                    scope = self._completion_scopes.get(key.text)
                else:
                    key = await self._user_key(item)
                    scope = self._scope(item)
                if key.text == retiring_key:
                    return False
                target = self.session(key, scope)
                break
            except IdentityUnavailable:
                await asyncio.sleep(completion_events.RETRY_SECONDS)
            except (ScopeUnavailable, ScopeInvariantViolation) as exc:
                if isinstance(item, _Completion):
                    logger.critical("%s: completion scope unavailable while retiring: %s", self._agent.agent_id, exc)
                    return True  # held unsettled; restart recovers the claimed batch
                outcome = await self._scope_error(item, exc)
                if outcome is RouteOutcome.ANSWERED and self._agent._bus is not None:
                    self._agent._bus.settle_handled(item)
                return True
        remaining = item.deadline.when() - asyncio.get_running_loop().time() if isinstance(item, _Completion) \
            and item.deadline else None
        if isinstance(item, _Completion):
            self._cancel_deadline(item)
        self._enqueue(target, item)
        if remaining is not None:
            item.deadline = asyncio.get_running_loop().call_later(max(0, remaining), self._expire_completion, target, item)
            self._deadlines.add(item.deadline)
        return True

    async def completion_key(self, origin: dict[str, Any]) -> SessionKey:
        if session_routing.scoped(self._agent.agent_id) and origin["group"]:
            kind = chat_scopes.PrincipalKind.PERSON if origin["sender"] else chat_scopes.PrincipalKind.BOT
            key, scope = await self._resolve_scope(origin, kind)
            self._completion_scopes[key.text] = scope
            return key
        from ..managers_api import DEFAULT_MANAGER_ID

        if self._agent.agent_id != DEFAULT_MANAGER_ID and not origin["group"]:
            return session_routing.MAIN_KEY
        operator = await self._operator_id(origin["channel_id"]) if not origin["group"] else None
        return session_routing.session_key_for(origin, client_id=None, operator_id=operator)

    async def _user_key(self, envelope: MessageEnvelope) -> SessionKey:
        from .manager import _message_client, _message_origin

        self._principals.pop(envelope.message_id, None)
        origin = _message_origin(envelope)
        client_id, _ = _message_client(envelope)
        from ..managers_api import DEFAULT_MANAGER_ID

        if self._agent.agent_id != DEFAULT_MANAGER_ID and not origin["group"]:
            return session_routing.MAIN_KEY
        if session_routing.scoped(self._agent.agent_id) and origin["group"]:
            return await self._scoped_key(envelope)
        operator = await self._operator_id(origin["channel_id"]) if not origin["group"] else None
        return session_routing.session_key_for(origin, client_id=client_id, operator_id=operator)

    async def completion_operator(self, channel_id: str) -> str | None:
        """Resolve identity before claiming: a failed read leaves the events pending for retry."""
        return await self._operator_id(channel_id)

    def dispatch_completion(self, envelope: MessageEnvelope, rows: list[dict[str, Any]], batch: uuid.UUID,
                            operator: str | None, *, key: SessionKey | None = None) -> None:
        """Queue a claimed batch on the session of the event actually taken, never an earlier peek."""
        from .manager import _completion_origin

        origin = _completion_origin(rows[0], self._agent.agent_address, batch)
        if key is None:
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
        session = (self.session(key, self._completion_scopes.get(key.text)) if session_routing.routes(self._agent.agent_id)
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
            if cached is not None and cached[1] is not None and not session_routing.scoped(self._agent.agent_id):
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
                if session.retired is not None:
                    await self._end_retired(session, first=item)
                    break
                if isinstance(item, _Completion):
                    if item.state is not CompletionState.QUEUED:
                        continue
                    item.state = CompletionState.STARTED
                    self._cancel_deadline(item)
                envelope = item.envelope if isinstance(item, _Completion) else item
                if not isinstance(item, _Completion) and envelope.message_type is MessageType.USER_MESSAGE \
                        and session_routing.scoped(agent.agent_id):
                    self._resolving.add(session.key)
                    try:
                        from .. import completion_events

                        while True:
                            try:
                                key = await self._user_key(envelope)
                                if session.retired is not None:
                                    break
                                target = self.session(key, self._scope(envelope))
                                break
                            except (ScopeUnavailable, ScopeInvariantViolation) as exc:
                                target = await self._scope_error(envelope, exc)
                                break
                            except IdentityUnavailable:
                                if session.retired is not None:
                                    break
                                await asyncio.sleep(completion_events.RETRY_SECONDS)
                    finally:
                        self._resolving.discard(session.key)
                    if session.retired is not None:
                        await self._end_retired(session, first=item)
                        break
                    if target is not session:
                        if isinstance(target, ManagerSession):
                            self._enqueue(target, envelope)
                        elif target is RouteOutcome.ANSWERED and agent._bus is not None:
                            agent._bus.settle_handled(envelope)
                        if session.retired is not None and self.idle(session):
                            await self._end_retired(session)
                            break
                        continue
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
                self._principals.pop(envelope.message_id, None)
                await self._turn_ended(session)
                if session.retired is not None and self.idle(session):
                    await self._end_retired(session)
                    break
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
        return not (session.key in self._reservations or session.key in self._resolving or session.key in self._ending or self.holds(session)
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
