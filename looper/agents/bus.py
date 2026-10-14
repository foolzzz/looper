"""In-process async message bus with JSONL journal for crash recovery.

No external dependencies (no Redis/RabbitMQ). Looper is a single-machine
developer tool — an in-process asyncio bus is the right fit.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, AsyncIterator, Callable

from .message import MessageEnvelope, MessageType

logger = logging.getLogger(__name__)

# Planners and Workers exist per task and never survive a restart; the server
# stops their tasks at startup, so their journaled messages are not replayed.
_PER_TASK_AGENT_TYPES = frozenset({"planner", "worker"})


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

    def undelivered(self) -> list[MessageEnvelope]:
        if not self._journal_path.exists():
            return []
        result: list[MessageEnvelope] = []
        with open(self._journal_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                    mid = d.get("message_id", "")
                    if mid not in self._delivered:
                        result.append(MessageEnvelope.from_dict(d))
                except (json.JSONDecodeError, KeyError, ValueError):
                    logger.warning("skipping corrupt journal entry")
        return result

    def compact(self) -> None:
        """Remove delivered entries from the journal file to save disk."""
        if not self._journal_path.exists():
            return
        kept: list[str] = []
        with open(self._journal_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                    if d.get("message_id", "") not in self._delivered:
                        kept.append(line)
                except (json.JSONDecodeError, KeyError):
                    pass
        tmp = self._journal_path.with_suffix(".tmp")
        tmp.write_text("\n".join(kept) + ("\n" if kept else ""), encoding="utf-8")
        tmp.replace(self._journal_path)
        # Reset delivered tracker
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

    @property
    def journal(self) -> MessageJournal:
        return self._journal

    async def start(self) -> None:
        """Replay undelivered messages from the journal, then compact it.

        Messages for per-task agents are dropped instead: replaying them would
        re-run work for tasks that were stopped when the server restarted.
        """
        replay: list[MessageEnvelope] = []
        dropped = 0
        for envelope in self._journal.undelivered():
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

    async def publish(self, envelope: MessageEnvelope) -> None:
        """Publish a message. Journaled before delivery for crash recovery."""
        self._journal.append(envelope)
        await self._deliver(envelope, journaled=True)

    async def _deliver(self, envelope: MessageEnvelope, *, journaled: bool = False) -> None:
        msg_type = envelope.message_type.value if isinstance(envelope.message_type, MessageType) else envelope.message_type
        if self._hand_to_consumer(envelope, msg_type):
            if journaled:
                self._journal.mark_delivered(envelope.message_id)
        else:
            await self.get_queue(envelope.target.agent_id).put(envelope)

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
            return await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError:
            self._reply_futures.pop(envelope.message_id, None)
            raise
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

    def pending_count(self, agent_id: str) -> int:
        q = self._queues.get(agent_id)
        return q.qsize() if q else 0

    async def shutdown(self) -> None:
        """Graceful shutdown: compact journal."""
        self._started = False
        self._journal.compact()
