"""Serial user-message collection on the source's existing writer session.

Search discovers containers; a completely read chat and its required threads are
one commit. Observations are made from read rows, including canonical duplicates.
No platform answer or exception text is retained in collection state.
"""

from __future__ import annotations

import asyncio
import logging
import math
import random
import time
from collections.abc import Awaitable, Callable, Collection, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Generic, Protocol, TypeVar

import asyncpg

from ..db import message_collection as collection
from ..db import message_stream as store
from .bridge_reader import BridgeMessage, _parse

logger = logging.getLogger(__name__)
T = TypeVar("T")
UTC = timezone.utc
RUN_SECONDS = 420.0
CALL_SECONDS = 20.0
OVERLAP = timedelta(minutes=10)


class CollectorErrorKind(str, Enum):
    AUTH_MISSING = "auth_missing"
    AUTH_EXPIRED = "auth_expired"
    IDENTITY = "identity"
    PROTOCOL = "protocol"
    PAGINATION = "pagination"
    RATE_LIMIT = "rate_limit"
    TRANSIENT = "transient"
    PERMISSION = "permission"
    NOT_MEMBER = "not_member"
    DEADLINE = "deadline"
    CONNECTION = "connection"
    STORAGE = "storage"
    INTERRUPTED = "interrupted"
    CONFIGURATION = "configuration"


class CollectorError(RuntimeError):
    def __init__(self, kind: CollectorErrorKind, *, code: int | None = None, retry_after: float | None = None) -> None:
        self.kind, self.code, self.retry_after = kind, code, retry_after
        super().__init__(kind.value)


@dataclass(frozen=True)
class AuthStatus:
    state: str
    user_open_id: str | None
    refresh_expires_at: datetime | None = None


@dataclass(frozen=True)
class Page(Generic[T]):
    items: list[T]
    has_more: bool = False
    next_token: str = ""


@dataclass(frozen=True)
class SearchHit:
    message_id: str
    chat_id: str
    sent_at: datetime
    thread_id: str | None = None
    root_id: str | None = None
    parent_id: str | None = None


@dataclass(frozen=True)
class Chat:
    chat_id: str
    kind: str = "unknown"
    name: str = ""


class CollectorBackend(Protocol):
    async def auth_status(self, profile: str) -> AuthStatus: ...
    async def search(self, profile: str, start: datetime, end: datetime, page_token: str) -> Page[SearchHit]: ...
    async def list_messages(
        self,
        profile: str,
        container_kind: str,
        container_id: str,
        start: datetime,
        end: datetime,
        order: str,
        page_token: str,
    ) -> Page[dict[str, Any]]: ...
    async def list_chats(self, profile: str, page_token: str) -> Page[Chat]: ...
    async def lookup_people(self, profile: str, open_ids: list[str]) -> dict[str, str]: ...


@dataclass
class CollectReport:
    status: str = "running"
    run_id: int | None = None
    generation: int = 0
    error: str | None = None
    error_code: int | None = None
    slices: int = 0
    search_hits: int = 0
    unindexed: int = 0
    chats_pulled: int = 0
    chats_failed: int = 0
    threads_pulled: int = 0
    threads_failed: int = 0
    sweep_chats: int = 0
    sweep_failed: int = 0
    sweep_found: int = 0
    events_new: int = 0
    events_seen: int = 0
    events_skipped: int = 0
    skipped_by_deletion: int = 0
    watermark: datetime | None = None
    window_from: datetime | None = None
    window_to: datetime | None = None
    # Distinct observations, rather than INSERT RETURNING, drive these counts.
    search_ids: set[str] = field(default_factory=set, repr=False)
    observed_ids: set[str] = field(default_factory=set, repr=False)
    sweep_ids: set[str] = field(default_factory=set, repr=False)
    new_sweep_ids: set[str] = field(default_factory=set, repr=False)
    names: set[str] = field(default_factory=set, repr=False)
    failed_chats: set[str] = field(default_factory=set, repr=False)
    counters_complete: bool = False

    def counters(self) -> dict[str, int]:
        return {
            key: getattr(self, key)
            for key in (
                "slices",
                "search_hits",
                "unindexed",
                "chats_pulled",
                "chats_failed",
                "threads_pulled",
                "threads_failed",
                "sweep_chats",
                "sweep_failed",
                "sweep_found",
            )
        }


class Budget:
    """One monotonic budget, shared by auth, retries, history, sweep and listing."""

    def __init__(self, *, seconds: float = RUN_SECONDS) -> None:
        self.deadline = time.monotonic() + seconds

    def remaining(self) -> float:
        left = self.deadline - time.monotonic()
        if left <= 0:
            raise CollectorError(CollectorErrorKind.DEADLINE)
        return left

    async def call(self, call: Callable[[], Awaitable[T]]) -> T:
        for attempt in range(3):
            timeout = min(CALL_SECONDS, self.remaining())
            try:
                return await asyncio.wait_for(call(), timeout)
            except asyncio.TimeoutError:
                failure = CollectorError(
                    CollectorErrorKind.DEADLINE if self.deadline <= time.monotonic() else CollectorErrorKind.TRANSIENT
                )
            except CollectorError as exc:
                failure = exc
            except (OSError, ConnectionError):
                failure = CollectorError(CollectorErrorKind.TRANSIENT)
            if failure.kind not in (CollectorErrorKind.RATE_LIMIT, CollectorErrorKind.TRANSIENT) or attempt == 2:
                raise failure from None
            retry = failure.retry_after
            delay = (
                retry
                if retry is not None and math.isfinite(retry) and retry >= 0
                else random.uniform(0.25, 0.75) * 2**attempt
            )
            if delay >= self.remaining():
                raise CollectorError(CollectorErrorKind.DEADLINE) from None
            await asyncio.sleep(delay)
        raise AssertionError("unreachable")


async def pages(budget: Budget, read: Callable[[str], Awaitable[Page[T]]]):
    """A changing opaque token and a changing page are both required for progress."""
    token, seen, previous = "", set(), None
    while True:
        page = await budget.call(lambda: read(token))
        if not isinstance(page, Page) or not isinstance(page.items, list) or type(page.has_more) is not bool:
            raise CollectorError(CollectorErrorKind.PROTOCOL)
        fingerprint = repr(page.items)
        if page.items and fingerprint == previous:
            raise CollectorError(CollectorErrorKind.PAGINATION)
        previous = fingerprint
        if page.has_more and (not isinstance(page.next_token, str) or not page.next_token or page.next_token in seen):
            raise CollectorError(CollectorErrorKind.PAGINATION)
        yield page.items
        if not page.has_more:
            return
        seen.add(page.next_token)
        token = page.next_token


def _connection_closed(conn: Any) -> bool:
    try:
        return conn.is_closed()
    except asyncpg.InterfaceError:
        return True


def check_identity(auth: AuthStatus, expected: str) -> None:
    if not isinstance(auth, AuthStatus) or auth.state not in ("ready", "needs_refresh", "expired", "missing"):
        raise CollectorError(CollectorErrorKind.PROTOCOL)
    if auth.state == "missing":
        raise CollectorError(CollectorErrorKind.AUTH_MISSING)
    if auth.state == "expired":
        raise CollectorError(CollectorErrorKind.AUTH_EXPIRED)
    if not isinstance(auth.user_open_id, str) or not auth.user_open_id:
        raise CollectorError(CollectorErrorKind.PROTOCOL)
    if auth.user_open_id != expected:
        raise CollectorError(CollectorErrorKind.IDENTITY)


def parse_message(raw: Mapping[str, Any]) -> BridgeMessage:
    """Keep bridge rendering/NUL/truncation rules; refuse ambiguous time units."""
    import json

    if not isinstance(raw, dict) or not isinstance(raw.get("create_time"), str) or not raw["create_time"].isdigit():
        raise CollectorError(CollectorErrorKind.PROTOCOL)
    # Current Lark history is epoch milliseconds, never epoch seconds.
    if not 946684800000 <= int(raw["create_time"]) < 4102444800000:
        raise CollectorError(CollectorErrorKind.PROTOCOL)
    try:
        encoded = json.dumps(raw, ensure_ascii=False, allow_nan=False).encode("utf-8")
        item = _parse(encoded)
    except (ValueError, TypeError, OverflowError):
        item = None
    if item is None:
        raise CollectorError(CollectorErrorKind.PROTOCOL)
    return item


@dataclass
class _Run:
    conn: Any
    source: dict[str, Any]
    backend: CollectorBackend
    report: CollectReport
    budget: Budget
    now: datetime
    start: datetime
    own_apps: Collection[str]

    @property
    def source_id(self):
        return self.source["id"]

    @property
    def profile(self):
        return self.source["lark_profile"]


async def pull_thread(run: _Run, thread_id: str, start: datetime, end: datetime) -> list[BridgeMessage]:
    result: list[BridgeMessage] = []
    previous: datetime | None = None
    async for batch in pages(
        run.budget, lambda token: run.backend.list_messages(run.profile, "thread", thread_id, start, end, "desc", token)
    ):
        messages = [parse_message(raw) for raw in batch]
        if messages and previous is not None and max(m.sent_at for m in messages) > previous:
            raise CollectorError(CollectorErrorKind.PAGINATION)
        if messages:
            previous = min(m.sent_at for m in messages)
        result.extend(m for m in messages if start <= m.sent_at < end)
        if messages and min(m.sent_at for m in messages) < start:
            break
    return result


def _cursor(messages: list[BridgeMessage], old_time: datetime | None, old_ids: list[str]):
    newest = max((m.sent_at for m in messages), default=old_time)
    if old_time is not None and (newest is None or newest < old_time):
        return old_time, sorted(old_ids)
    ids = {m.external_id for m in messages if m.sent_at == newest}
    if newest == old_time:
        ids.update(old_ids)
    return newest, sorted(ids)


async def _failure(run: _Run, chat_id: str, error: CollectorError, *, thread_id: str | None = None) -> None:
    if chat_id in run.report.failed_chats:
        return
    async with store.transaction(run.conn):
        await store.chat_for_update(run.conn, run.source_id, chat_id, kind="unknown", name="")
        await run.conn.execute(
            "UPDATE source_chats SET failures=failures+1, last_error=$3,last_error_code=$4, "
            "state=CASE WHEN $3='not_member' THEN 'left' WHEN failures+1>=6 THEN 'failing' ELSE state END "
            "WHERE source_id=$1 AND chat_id=$2 AND purged_at IS NULL",
            run.source_id,
            chat_id,
            error.kind.value,
            error.code,
        )
        if thread_id:
            await run.conn.execute(
                "INSERT INTO source_threads(source_id,chat_id,thread_id,failures,last_error) VALUES($1,$2,$3,1,$4) "
                "ON CONFLICT(source_id,thread_id) DO UPDATE SET failures=source_threads.failures+1,last_error=$4",
                run.source_id,
                chat_id,
                thread_id,
                error.kind.value,
            )
        from .message_stream_parity import observe_collect

        await observe_collect(
            run.conn,
            run.source_id,
            chat_id,
            [],
            [
                {
                    "container_kind": "thread" if thread_id else "chat",
                    "container_id": thread_id or chat_id,
                    "complete": False,
                }
            ],
            run.start,
            run.now,
            own_app_ids=run.own_apps,
        )
    run.report.error, run.report.error_code = error.kind.value, error.code
    run.report.failed_chats.add(chat_id)
    run.report.chats_failed = len(run.report.failed_chats)
    if thread_id:
        run.report.threads_failed += 1


async def pull_chat(
    run: _Run, chat_id: str, hits: list[SearchHit], end: datetime, *, start: datetime | None = None, sweep: bool = False
) -> bool:
    """Read before the transaction, then commit a whole chat and all required threads."""
    chat = await store.chat_row(run.conn, run.source_id, chat_id)
    if chat and (chat["inclusion"] != "included" or chat.get("purged_at")):
        return True
    if chat and chat["state"] == "left":
        run.report.failed_chats.add(chat_id)
        run.report.chats_failed = len(run.report.failed_chats)
        return False
    old_time = chat["cursor_time"] if chat else None
    floor = run.source.get("collect_enabled_at") or run.now
    floor -= timedelta(hours=24)
    read_from = old_time or floor
    if start is not None:
        read_from = min(read_from, start)
    messages: list[BridgeMessage] = []
    threads: dict[str, tuple[list[BridgeMessage], dict[str, Any] | None]] = {}
    thread_id = None
    try:
        previous = None
        async for batch in pages(
            run.budget,
            lambda token: run.backend.list_messages(run.profile, "chat", chat_id, read_from, end, "asc", token),
        ):
            items = [parse_message(raw) for raw in batch]
            if items and previous is not None and min(m.sent_at for m in items) < previous:
                raise CollectorError(CollectorErrorKind.PAGINATION)
            if items:
                previous = max(m.sent_at for m in items)
            messages.extend(m for m in items if read_from <= m.sent_at < end)
        existing = {
            row["thread_id"]: dict(row)
            for row in await run.conn.fetch(
                "SELECT * FROM source_threads WHERE source_id=$1 AND chat_id=$2", run.source_id, chat_id
            )
        }
        # A root reveals a thread even when search omitted every reply.
        required = (
            set(existing) | {m.thread_id for m in messages if m.thread_id} | {h.thread_id for h in hits if h.thread_id}
        )
        for thread_id in sorted(required):
            old = existing.get(thread_id)
            thread_from = old["cursor_time"] if old and old["cursor_time"] else floor
            if start is not None:
                thread_from = min(thread_from, start)
            threads[thread_id] = (await pull_thread(run, thread_id, thread_from, end), old)
        thread_id = None
    except CollectorError as exc:
        if exc.kind in (
            CollectorErrorKind.DEADLINE,
            CollectorErrorKind.AUTH_EXPIRED,
            CollectorErrorKind.AUTH_MISSING,
            CollectorErrorKind.IDENTITY,
        ):
            raise
        await _failure(run, chat_id, exc, thread_id=thread_id)
        return False
    all_messages = messages + [m for batch, _ in threads.values() for m in batch]
    all_messages.sort(key=lambda m: (m.sent_at, m.external_id))
    observation_messages = all_messages[:]
    unique = {m.external_id: m for m in reversed(all_messages)}
    all_messages = sorted(unique.values(), key=lambda m: (m.sent_at, m.external_id))
    async with store.transaction(run.conn):
        locked = await store.chat_for_update(run.conn, run.source_id, chat_id, kind="unknown", name="")
        if locked["inclusion"] != "included" or locked.get("purged_at"):
            return True
        own = [m for m in all_messages if m.sender_kind == "app" and m.sender_id in run.own_apps]
        looper_chat = bool(own) and locked["kind"] == "p2p"
        if looper_chat:
            await store.exclude_chat(run.conn, run.source_id, chat_id, "looper_channel", now=run.now)
        kept = [
            m for m in all_messages if not looper_chat and not (m.sender_kind == "app" and m.sender_id in run.own_apps)
        ]
        rows = []
        thread_first = {key for key, (_, old) in threads.items() if not old or old["cursor_time"] is None}
        for m in kept:
            days = (
                run.source["app_body_retention_days"] if m.sender_kind == "app" else run.source["body_retention_days"]
            )
            historical = (old_time is None or m.thread_id in thread_first) and m.sent_at < max(
                run.start, run.source.get("collect_enabled_at") or run.start
            )
            rows.append(
                {
                    "external_id": m.external_id,
                    "chat_id": chat_id,
                    "thread_id": m.thread_id,
                    "root_id": m.root_id,
                    "parent_id": m.parent_id,
                    "sender_id": m.sender_id,
                    "sender_kind": m.sender_kind,
                    "sent_at": m.sent_at,
                    "msg_type": m.msg_type,
                    "mentions_me": bool(run.source["user_open_id"] in m.mention_ids),
                    "mention_ids": m.mention_ids,
                    "text": m.text,
                    "body": None if days and m.sent_at < run.now - timedelta(days=days) else m.body,
                    "body_bytes": m.body_bytes,
                    "origin": "history" if historical else "live",
                }
            )
        deleted = await store.deleted_ids(run.conn, run.source_id, rows)
        rows = [row for row in rows if row["external_id"] not in deleted]
        inserted = await store.insert_events(run.conn, run.source_id, run.report.run_id, rows)
        if inserted:
            await store.advance_committed(run.conn, run.source_id, max(seq for _, seq in inserted))
        people = {
            open_id: (name, "mention")
            for m in kept
            if m.external_id not in deleted
            for open_id, name in m.mention_names.items()
        }
        await store.upsert_people(run.conn, run.source_id, people, now=run.now)
        cursor_time, cursor_ids = _cursor(all_messages, locked["cursor_time"], locked["cursor_ids"])
        await collection.save_chat_progress(
            run.conn, run.source_id, chat_id, cursor_time=cursor_time, cursor_ids=cursor_ids, now=run.now
        )
        for tid, (batch, old) in threads.items():
            cursor_time, cursor_ids = _cursor(
                batch, old["cursor_time"] if old else None, old["cursor_ids"] if old else []
            )
            await collection.save_thread_progress(
                run.conn, run.source_id, chat_id, tid, cursor_time=cursor_time, cursor_ids=cursor_ids, now=run.now
            )
        from .message_stream_parity import observe_collect

        containers = [{"container_kind": "chat", "container_id": chat_id, "complete": True}] + [
            {"container_kind": "thread", "container_id": tid, "complete": True} for tid in threads
        ]
        observed = [
            {
                "external_id": m.external_id,
                "chat_id": chat_id,
                "sent_at": m.sent_at,
                "thread_id": m.thread_id,
                "root_id": m.root_id,
                "parent_id": m.parent_id,
            }
            for m in observation_messages
            if not looper_chat
            and m.external_id not in deleted
            and not (m.sender_kind == "app" and m.sender_id in run.own_apps)
        ]
        await observe_collect(
            run.conn, run.source_id, chat_id, observed, containers, read_from, end, own_app_ids=run.own_apps
        )
        new, seen, skipped = len(inserted), len(rows) - len(inserted), len(all_messages) - len(kept)
        await store.add_run_counts(
            run.conn, run.report.run_id, new=new, seen=seen, skipped=skipped, skipped_by_deletion=len(deleted)
        )
    run.report.events_new += new
    run.report.events_seen += seen
    run.report.events_skipped += skipped
    run.report.skipped_by_deletion += len(deleted)
    run.report.chats_pulled += 1
    run.report.threads_pulled += len(threads)
    ids = {row["external_id"] for row in rows}
    run.report.observed_ids |= ids
    if sweep:
        run.report.sweep_ids |= ids
        run.report.new_sweep_ids.update(message_id for message_id, _ in inserted)
    run.report.names |= {m.sender_id for m in kept if m.sender_kind == "user" and m.external_id not in deleted}
    return True


async def collect_slice(run: _Run, start: datetime, end: datetime) -> bool:
    by_chat: dict[str, list[SearchHit]] = {}
    async for batch in pages(run.budget, lambda token: run.backend.search(run.profile, start, end, token)):
        for hit in batch:
            if (
                not isinstance(hit, SearchHit)
                or not hit.chat_id
                or not hit.message_id
                or not isinstance(hit.sent_at, datetime)
                or hit.sent_at.tzinfo is None
            ):
                raise CollectorError(CollectorErrorKind.PROTOCOL)
            if start <= hit.sent_at < end:
                by_chat.setdefault(hit.chat_id, []).append(hit)
                run.report.search_ids.add(hit.message_id)
    complete = True
    for chat_id, hits in sorted(by_chat.items()):
        chat = await store.chat_row(run.conn, run.source_id, chat_id)
        if chat and chat["state"] == "failing":
            run.report.failed_chats.add(chat_id)
            run.report.chats_failed = len(run.report.failed_chats)
            complete = False
            continue
        if not await pull_chat(run, chat_id, hits, end):
            complete = False
    run.report.slices += 1
    return complete


async def run_sweep(run: _Run) -> bool:
    chats = await run.conn.fetch(
        "SELECT chat_id FROM source_chats WHERE source_id=$1 AND inclusion='included' "
        "AND purged_at IS NULL AND state<>'left' AND (cursor_time IS NOT NULL OR failures>0) ORDER BY chat_id",
        run.source_id,
    )
    ids = [row["chat_id"] for row in chats]
    if not ids:
        return True
    after = run.source.get("sweep_after") or ""
    rotated = [chat for chat in ids if chat > after] + [chat for chat in ids if chat <= after]
    selected = rotated[: max(1, math.ceil(len(ids) / run.source["sweep_cycle_runs"]))]
    complete = True
    for chat_id in selected:
        run.report.sweep_chats += 1
        if await pull_chat(run, chat_id, [], run.now, sweep=True):
            if complete:
                await run.conn.execute("UPDATE event_sources SET sweep_after=$2 WHERE id=$1", run.source_id, chat_id)
        else:
            run.report.sweep_failed += 1
            complete = False
    return complete


async def run_listing(run: _Run) -> None:
    last = run.source.get("last_listing_at")
    if last is None or run.now - last >= timedelta(days=1):
        async for batch in pages(run.budget, lambda token: run.backend.list_chats(run.profile, token)):
            async with store.transaction(run.conn):
                for chat in batch:
                    if not isinstance(chat, Chat) or not chat.chat_id or chat.kind not in store.CHAT_KINDS:
                        raise CollectorError(CollectorErrorKind.PROTOCOL)
                    row = await store.chat_for_update(
                        run.conn, run.source_id, chat.chat_id, kind=chat.kind, name=chat.name
                    )
                    if row.get("purged_at"):
                        continue
                    await store.set_chat_directory(
                        run.conn, run.source_id, chat.chat_id, kind=chat.kind, name=chat.name
                    )
                    await run.conn.execute(
                        "UPDATE source_chats SET state=CASE WHEN state='left' THEN 'active' ELSE state END "
                        "WHERE source_id=$1 AND chat_id=$2",
                        run.source_id,
                        chat.chat_id,
                    )
        await run.conn.execute("UPDATE event_sources SET last_listing_at=$2 WHERE id=$1", run.source_id, run.now)
        run.source["last_listing_at"] = run.now
    known = set(await store.known_people(run.conn, run.source_id))
    wanted = sorted(run.report.names - known)
    for offset in range(0, len(wanted), 30):
        try:
            names = await run.budget.call(lambda: run.backend.lookup_people(run.profile, wanted[offset : offset + 30]))
        except CollectorError as exc:
            if exc.kind in (
                CollectorErrorKind.AUTH_EXPIRED,
                CollectorErrorKind.AUTH_MISSING,
                CollectorErrorKind.IDENTITY,
                CollectorErrorKind.DEADLINE,
            ):
                raise
            continue
        if not isinstance(names, dict) or any(
            not isinstance(key, str) or not isinstance(value, str) for key, value in names.items()
        ):
            continue
        async with store.transaction(run.conn):
            await store._mutation_lock(run.conn, run.source_id)
            await store.upsert_people(
                run.conn,
                run.source_id,
                {key: (value, "contact") for key, value in names.items() if key in wanted},
                now=run.now,
            )


async def _collect(run: _Run) -> None:
    auth = await run.budget.call(lambda: run.backend.auth_status(run.profile))
    if isinstance(auth, AuthStatus) and auth.state in ("ready", "needs_refresh", "expired", "missing"):
        await run.conn.execute(
            "UPDATE event_sources SET auth_state=$2,auth_checked_at=$3,refresh_expires_at=$4 WHERE id=$1",
            run.source_id,
            auth.state,
            run.now,
            auth.refresh_expires_at,
        )
    check_identity(auth, run.source["user_open_id"])
    await run_listing(run)
    run.report.counters_complete = True
    point = run.start
    contiguous = True
    search_failed = False
    while point < run.now:
        end = min(point + timedelta(hours=1), run.now)
        try:
            complete = await collect_slice(run, point, end)
        except CollectorError as exc:
            if exc.kind == CollectorErrorKind.DEADLINE:
                raise
            run.report.error, run.report.error_code = exc.kind.value, exc.code
            search_failed = True
            break
        if complete and contiguous:
            run.report.watermark = end
            await run.conn.execute(
                "UPDATE event_sources SET watermark_at=GREATEST(watermark_at,$2) WHERE id=$1", run.source_id, end
            )
        else:
            contiguous = False
        point = end
    sweep_ok = await run_sweep(run)
    await run_listing(run)
    # Reference containers must be fully revisited even when search is empty or ordinary cursors passed them.
    references = await run.conn.fetch(
        "SELECT DISTINCT c.chat_id,s.window_from,s.window_to FROM source_parity_sessions s "
        "JOIN source_parity_coverage c ON c.session_id=s.id WHERE s.source_id=$1 AND s.reason IS DISTINCT FROM 'evidence_deleted' "
        "ORDER BY s.window_from,s.window_to,c.chat_id",
        run.source_id,
    )
    parity_ok = True
    for reference in references:
        parity_ok = (
            await pull_chat(
                run, reference["chat_id"], [], max(run.now, reference["window_to"]), start=reference["window_from"]
            )
            and parity_ok
        )
    run.report.search_hits = len(run.report.search_ids)
    run.report.unindexed = len(run.report.observed_ids - run.report.search_ids)
    run.report.sweep_found = len(run.report.new_sweep_ids - run.report.search_ids)
    run.report.status = (
        "search_failed" if search_failed else "ok" if contiguous and parity_ok and sweep_ok else "failed"
    )
    if run.report.status == "failed" and run.report.error is None:
        run.report.error = CollectorErrorKind.PERMISSION.value
    if run.report.status == "ok":
        from .message_stream_parity import finalize_source

        await finalize_source(run.conn, run.source_id)


async def prune_collection(conn: Any, source_id: str, now: datetime) -> None:
    """Bounded metadata retention, using the writer's connection and no platform I/O."""
    from ..db.message_parity import prune_sessions
    from . import scheduler_settings

    days = await scheduler_settings.run_retention_days(conn)
    if days:
        before = now - timedelta(days=days)
        async with conn.transaction():
            await store.prune_runs(conn, source_id, before=before)
            await prune_sessions(conn, source_id, before=before)


async def run_collect_pass(
    source: str | Mapping[str, Any],
    *,
    backend: CollectorBackend,
    own_apps: Collection[str] = (),
    now: datetime | None = None,
    db: Any = None,
    generation: int | None = None,
    started: asyncio.Future | None = None,
    seconds: float = RUN_SECONDS,
) -> CollectReport:
    from ..db.connection import get_pool
    from .channels.lark_cli import profile_name

    source_id = source if isinstance(source, str) else source["id"]
    now = now or datetime.now(UTC)
    pool = db if db is not None else get_pool()
    report = CollectReport()
    start_clock = time.monotonic()
    async with pool.acquire() as conn:
        if not await store.try_lock(conn, source_id):
            report.status = "skipped_busy"
            if started is None:
                report.run_id = await store.start_run(conn, source_id, "collect", "scheduled", now=now)
                await store.finish_run(conn, report.run_id, "skipped_busy", now=now)
            if started is not None and not started.done():
                started.set_exception(CollectorError(CollectorErrorKind.CONNECTION))
            return report
        try:
            fresh = await collection.collector_source(source_id, db=conn)
            if (
                fresh is None
                or fresh["ingest_mode"] != "collect"
                or (generation is not None and generation != fresh["generation"])
            ):
                raise CollectorError(CollectorErrorKind.CONFIGURATION)
            report.generation = fresh["generation"]
            if not profile_name(fresh["lark_profile"]) or not fresh["user_open_id"]:
                raise CollectorError(CollectorErrorKind.CONFIGURATION)
            await store.interrupt_stale_runs(conn, source_id)
            report.run_id = await store.start_run(
                conn, source_id, "collect", "manual" if started else "scheduled", now=now
            )
            before = fresh["watermark_at"] or now - timedelta(hours=24)
            start = before - OVERLAP
            report.window_from, report.window_to = start, now
            await conn.execute(
                "UPDATE source_runs SET window_from=$2,window_to=$3,watermark_before=$4 WHERE id=$1",
                report.run_id,
                start,
                now,
                before,
            )
            if started is not None and not started.done():
                started.set_result(
                    {
                        "source_id": source_id,
                        "accepted": True,
                        "generation": report.generation,
                        "run_id": str(report.run_id),
                    }
                )
            run = _Run(
                conn,
                fresh,
                backend,
                report,
                Budget(seconds=max(0, seconds - (time.monotonic() - start_clock))),
                now,
                start,
                own_apps,
            )
            try:
                await asyncio.wait_for(_collect(run), run.budget.remaining())
            except asyncio.TimeoutError:
                report.status, report.error = "partial", CollectorErrorKind.DEADLINE.value
            except CollectorError as exc:
                report.error, report.error_code = exc.kind.value, exc.code
                report.status = (
                    "partial"
                    if exc.kind == CollectorErrorKind.DEADLINE
                    else "auth_failed"
                    if exc.kind
                    in (CollectorErrorKind.AUTH_MISSING, CollectorErrorKind.AUTH_EXPIRED, CollectorErrorKind.IDENTITY)
                    else "failed"
                )
                if exc.kind in (CollectorErrorKind.AUTH_MISSING, CollectorErrorKind.AUTH_EXPIRED):
                    await conn.execute(
                        "UPDATE event_sources SET auth_state=$2 WHERE id=$1",
                        source_id,
                        "missing" if exc.kind == CollectorErrorKind.AUTH_MISSING else "expired",
                    )
            except asyncio.CancelledError:
                report.status, report.error = "interrupted", CollectorErrorKind.INTERRUPTED.value
                if not _connection_closed(conn):
                    await asyncio.shield(
                        collection.finish_collect_run(
                            conn,
                            report.run_id,
                            report.status,
                            now=datetime.now(UTC),
                            source_id=source_id,
                            error=report.error,
                            counters=report.counters() if report.counters_complete else None,
                        )
                    )
                raise
            except Exception:
                if _connection_closed(conn):
                    report.status, report.error = "interrupted", CollectorErrorKind.CONNECTION.value
                    return report
                report.status, report.error = "failed", CollectorErrorKind.STORAGE.value
            if report.status != "ok":
                await conn.execute(
                    "UPDATE source_parity_sessions SET state='incomplete',reason='collection_failed',finished_at=NULL "
                    "WHERE source_id=$1 AND (reason IS NULL OR reason IN ('coverage_missing','collection_failed'))",
                    source_id,
                )
            report.unindexed = len(report.observed_ids - report.search_ids)
            report.search_hits = len(report.search_ids)
            report.sweep_found = len(report.new_sweep_ids - report.search_ids)
            await collection.finish_collect_run(
                conn,
                report.run_id,
                report.status,
                now=datetime.now(UTC),
                source_id=source_id,
                error=report.error,
                error_code=report.error_code,
                counters=report.counters() if report.counters_complete else None,
                watermark=report.watermark,
            )
            try:
                await asyncio.wait_for(prune_collection(conn, source_id, now), 5)
            except Exception:
                logger.warning("source %s collection metadata retention failed", source_id)
            return report
        except CollectorError as exc:
            if started is not None and not started.done():
                started.set_exception(exc)
            raise
        finally:
            try:
                await store.unlock(conn, source_id)
            except Exception:
                pass
            logger.info(
                "message collection source_id=%s run_id=%s generation=%s status=%s window_from=%s window_to=%s "
                "watermark=%s slices=%s search_hits=%s unindexed=%s chats_pulled=%s chats_failed=%s "
                "threads_pulled=%s threads_failed=%s sweep_chats=%s sweep_failed=%s sweep_found=%s events_new=%s "
                "events_seen=%s events_skipped=%s skipped_by_deletion=%s elapsed_ms=%s error=%s",
                source_id,
                report.run_id,
                report.generation,
                report.status,
                report.window_from,
                report.window_to,
                report.watermark,
                report.slices,
                report.search_hits,
                report.unindexed,
                report.chats_pulled,
                report.chats_failed,
                report.threads_pulled,
                report.threads_failed,
                report.sweep_chats,
                report.sweep_failed,
                report.sweep_found,
                report.events_new,
                report.events_seen,
                report.events_skipped,
                report.skipped_by_deletion,
                round((time.monotonic() - start_clock) * 1000),
                report.error,
            )
