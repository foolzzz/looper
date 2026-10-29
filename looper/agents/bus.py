"""In-process async message bus with JSONL journal for crash recovery.

No external dependencies (no Redis/RabbitMQ). Looper is a single-machine
developer tool — an in-process asyncio bus is the right fit.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Any, AsyncIterator, Callable

from .message import BOT_CHANNEL_TYPES, MessageEnvelope, MessageType

logger = logging.getLogger(__name__)

# Planners and Workers exist per task and never survive a restart; the server
# stops their tasks at startup, so their journaled messages are not replayed.
_PER_TASK_AGENT_TYPES = frozenset({"planner", "worker"})
# A status (a streamed delta, a phase) is progress of a turn a restart ends: it is not journaled, so it is never
# replayed as a stale line, and a streamed reply costs no fsync on the event loop per delta.
_TRANSIENT = frozenset({MessageType.STATUS_UPDATE.value})


def _journaled(envelope: MessageEnvelope) -> bool:
    kind = envelope.message_type
    return (kind.value if isinstance(kind, MessageType) else kind) not in _TRANSIENT


class MessageJournal:
    """Append-only JSONL journal for crash recovery.

    Messages are written before delivery and marked delivered after.
    On restart, undelivered messages are replayed.
    """

    def __init__(self, journal_dir: Path) -> None:
        self._dir = journal_dir
        self._dir.mkdir(parents=True, exist_ok=True)
        self._journal_path = self._dir / "journal.jsonl"
        self._delivered: set[str] = set()
        self._load_delivered()

    def _delivered_path(self) -> Path:
        return self._dir / "delivered.txt"

    def _load_delivered(self) -> None:
        path = self._delivered_path()
        if path.exists():
            self._delivered = set(path.read_text().splitlines())

    def append(self, envelope: MessageEnvelope) -> None:
        line = json.dumps(envelope.to_dict(), ensure_ascii=False)
        with open(self._journal_path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
            f.flush()
            os.fsync(f.fileno())

    def mark_delivered(self, message_id: str) -> None:
        self._delivered.add(message_id)
        with open(self._delivered_path(), "a", encoding="utf-8") as f:
            f.write(message_id + "\n")
            f.flush()

    def _entries(self) -> list[tuple[int, dict[str, Any]]]:
        if not self._journal_path.exists():
            return []
        result = []
        for number, line in enumerate(self._journal_path.read_text(encoding="utf-8").split("\n"), 1):
            if not line.strip():
                continue
            try:
                raw = json.loads(line)
                envelope = MessageEnvelope.from_dict(raw)
                for value in (envelope.message_id, envelope.correlation_id, envelope.timestamp,
                              envelope.source.agent_type, envelope.source.agent_id,
                              envelope.target.agent_type, envelope.target.agent_id):
                    if not isinstance(value, str) or not value:
                        raise ValueError("identity fields must be nonempty strings")
                for address in (envelope.source, envelope.target):
                    if address.channel is not None and address.channel not in ("web", "api", *BOT_CHANNEL_TYPES):
                        raise ValueError("unknown or malformed address channel")
                if not isinstance(envelope.payload, dict) or not isinstance(envelope.metadata, dict):
                    raise ValueError("payload and metadata must be objects")
                if envelope.reply_to is not None and not isinstance(envelope.reply_to, str):
                    raise ValueError("reply_to must be a string or null")
            except (TypeError, AttributeError, json.JSONDecodeError, KeyError, ValueError) as exc:
                raise ValueError(f"invalid journal envelope at {self._journal_path}:{number}: {exc}") from exc
            result.append((number, raw))
        return result

    def _replace(self, path: Path, text: str) -> None:
        temporary = path.with_name(path.name + ".tmp")
        try:
            with temporary.open("w", encoding="utf-8") as stream:
                stream.write(text)
                stream.flush()
                os.fsync(stream.fileno())
            temporary.replace(path)
            directory = os.open(self._dir, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            temporary.unlink(missing_ok=True)

    def transition(self) -> None:
        """Once per runtime, seal bridge journal identities before any message can replay.

        The startup owner is single-server. Validate the complete input before either
        replacement; repeating after a crash between journal and marker is harmless.
        """
        from .message import builtin_channel_id
        from .task_notice import Notice, NotifyChannel

        marker = self._dir / "notice-identities-v1"
        if marker.exists():
            return
        kept = []
        for number, raw in self._entries():
            if raw["message_id"] not in self._delivered:
                if raw["message_type"] in _TRANSIENT:
                    continue  # Historical progress ended with its turn, exactly as before.
                payload = raw["payload"]
                notice = payload if raw["message_type"] == MessageType.CHAT_NOTICE.value else payload.get("notice")
                if notice is not None or "notice" in payload:
                    try:
                        if isinstance(notice, dict) and "channel_id" not in notice:
                            notice["channel_id"] = builtin_channel_id(NotifyChannel(notice.get("channel")).value)
                        parsed = Notice.from_payload(notice)
                        if parsed is None:
                            raise ValueError("missing or mismatched notice channel identity")
                        target = raw["target"]
                        if raw["message_type"] == MessageType.CHAT_NOTICE.value and (
                                target["agent_type"] != "channel" or target["agent_id"] != parsed.channel_id
                                or target.get("channel") != parsed.channel.value):
                            raise ValueError("notice target does not match its channel identity")
                    except (TypeError, ValueError) as exc:
                        raise ValueError(f"invalid journal notice at {self._journal_path}:{number}: {exc}") from exc
            kept.append(json.dumps(raw, ensure_ascii=False) + "\n")
        self._replace(self._journal_path, "".join(kept))
        self._replace(marker, "complete\n")

    def undelivered(self) -> list[MessageEnvelope]:
        return [MessageEnvelope.from_dict(raw) for _, raw in self._entries() if raw["message_id"] not in self._delivered]

    def compact(self) -> None:
        """Remove delivered entries only after validating the complete journal."""
        if not self._journal_path.exists():
            return
        kept = [json.dumps(raw, ensure_ascii=False) + "\n" for _, raw in self._entries()
                if raw["message_id"] not in self._delivered]
        self._replace(self._journal_path, "".join(kept))
        self._delivered.clear()
        self._delivered_path().unlink(missing_ok=True)


class MessageBus:
    """In-process async message bus with filesystem durability."""

    def __init__(self, runtime_root: Path) -> None:
        self._queues: dict[str, asyncio.Queue[MessageEnvelope]] = {}
        self._topic_subscribers: dict[str, list[Callable[[MessageEnvelope], Any]]] = {}
        self._topic_consumers: dict[str, list[Callable[[MessageEnvelope], bool]]] = {}
        self._journal = MessageJournal(runtime_root / ".local" / "agent-bus")
        self._reply_futures: dict[str, asyncio.Future[MessageEnvelope]] = {}
        self._started = False
        self._queued_before_start: set[str] = set()

    @property
    def journal(self) -> MessageJournal:
        return self._journal

    def settle(self, message_id: str) -> None:
        """Mark a message delivered although it is still queued (the user stopped it): a restart does not replay it."""
        self._journal.mark_delivered(message_id)

    async def start(self) -> None:
        """Replay undelivered messages from the journal, then compact it.

        Messages for per-task agents are dropped instead: replaying them would
        re-run work for tasks that were stopped when the server restarted.
        """
        self._journal.transition()
        replay: list[MessageEnvelope] = []
        dropped = 0
        for envelope in self._journal.undelivered():
            # Channels can receive during startup checks, before journal replay.
            # Keep their queued messages durable without delivering them a second time.
            if envelope.message_id in self._queued_before_start:
                continue
            if envelope.target.agent_type in _PER_TASK_AGENT_TYPES:
                self._journal.mark_delivered(envelope.message_id)
                dropped += 1
            else:
                replay.append(envelope)
        if dropped:
            logger.info("dropped %d undelivered messages for per-task agents of a previous run", dropped)
        if replay:
            logger.info("replaying %d undelivered messages from journal", len(replay))
            for envelope in replay:
                await self._deliver(envelope, journaled=True)
        self._journal.compact()
        self._started = True
        self._queued_before_start.clear()

    async def publish(self, envelope: MessageEnvelope) -> None:
        """Publish a message. Journaled before delivery for crash recovery (a status is not: ``_TRANSIENT``)."""
        journaled = _journaled(envelope)
        if journaled:
            self._journal.append(envelope)
        await self._deliver(envelope, journaled=journaled)

    async def _deliver(self, envelope: MessageEnvelope, *, journaled: bool = False) -> None:
        msg_type = envelope.message_type.value if isinstance(envelope.message_type, MessageType) else envelope.message_type
        if self._hand_to_consumer(envelope, msg_type):
            if journaled:
                self._journal.mark_delivered(envelope.message_id)
        else:
            await self.get_queue(envelope.target.agent_id).put(envelope)
            if journaled and not self._started:
                self._queued_before_start.add(envelope.message_id)

        # Resolve any pending request-reply futures
        if envelope.reply_to and envelope.reply_to in self._reply_futures:
            future = self._reply_futures.pop(envelope.reply_to)
            if not future.done():
                future.set_result(envelope)

        # Notify topic subscribers
        for callback in self._topic_subscribers.get(msg_type, []):
            try:
                callback(envelope)
            except Exception:
                logger.exception("topic subscriber error for %s", msg_type)

    def _hand_to_consumer(self, envelope: MessageEnvelope, msg_type: str) -> bool:
        for callback in self._topic_consumers.get(msg_type, []):
            try:
                if callback(envelope):
                    return True
            except Exception:
                logger.exception("topic consumer error for %s", msg_type)
        return False

    def get_queue(self, agent_id: str) -> asyncio.Queue[MessageEnvelope]:
        """Get or create the message queue for an agent."""
        if agent_id not in self._queues:
            self._queues[agent_id] = asyncio.Queue()
        return self._queues[agent_id]

    async def subscribe(self, agent_id: str) -> AsyncIterator[MessageEnvelope]:
        """Yield messages for the given agent. Marks each as delivered after yield."""
        q = self.get_queue(agent_id)
        while True:
            envelope = await q.get()
            yield envelope
            if _journaled(envelope):
                self._journal.mark_delivered(envelope.message_id)

    async def request(
        self,
        envelope: MessageEnvelope,
        timeout: float = 300.0,
    ) -> MessageEnvelope:
        """Send a message and wait for a reply (matched by reply_to == message_id)."""
        future: asyncio.Future[MessageEnvelope] = asyncio.get_event_loop().create_future()
        self._reply_futures[envelope.message_id] = future
        try:
            await self.publish(envelope)
            # Not asyncio.wait_for: in Python 3.11 it returns the reply when a cancellation (a stop) arrives with
            # it, and the stop's cancellation is lost.
            async with asyncio.timeout(timeout):
                return await future
        finally:
            self._reply_futures.pop(envelope.message_id, None)

    def subscribe_topic(
        self,
        message_type: str | MessageType,
        callback: Callable[[MessageEnvelope], Any],
        *,
        consumes: bool = False,
    ) -> None:
        """Register a topic subscriber.

        An observer (the default) sees each message while it is still queued
        for its target. A consumer takes delivery: when its callback returns
        True, the message is marked delivered and is not queued for its target.
        """
        key = message_type.value if isinstance(message_type, MessageType) else message_type
        subscribers = self._topic_consumers if consumes else self._topic_subscribers
        subscribers.setdefault(key, []).append(callback)

    def unsubscribe_topic(
        self,
        message_type: str | MessageType,
        callback: Callable[[MessageEnvelope], Any],
    ) -> None:
        key = message_type.value if isinstance(message_type, MessageType) else message_type
        for subscribers in (self._topic_subscribers, self._topic_consumers):
            subs = subscribers.get(key, [])
            if callback in subs:
                subs.remove(callback)

    async def drain_queue(self, agent_id: str) -> list[MessageEnvelope]:
        """Non-blocking drain of all queued messages for an agent."""
        q = self.get_queue(agent_id)
        result: list[MessageEnvelope] = []
        while not q.empty():
            try:
                envelope = q.get_nowait()
                result.append(envelope)
                self._journal.mark_delivered(envelope.message_id)
            except asyncio.QueueEmpty:
                break
        return result

    def take_queue(self, agent_id: str) -> list[MessageEnvelope]:
        """Remove every queued message for an agent and return them, not marked delivered: the caller settles
        each one (``settle``) once it is dealt with, so a crash in between replays it."""
        q = self._queues.get(agent_id)
        result: list[MessageEnvelope] = []
        while q is not None and not q.empty():
            try:
                result.append(q.get_nowait())
            except asyncio.QueueEmpty:
                break
        return result

    def queue_ids(self) -> list[str]:
        """The ids of the agents that have a queue."""
        return list(self._queues)

    def pending_count(self, agent_id: str) -> int:
        q = self._queues.get(agent_id)
        return q.qsize() if q else 0

    async def shutdown(self) -> None:
        """Graceful shutdown: compact journal."""
        if self._started:
            self._journal.compact()
        self._started = False
