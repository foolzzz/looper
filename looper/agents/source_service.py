"""Ingest message sources into the event store (message-stream RFC, sections 3, 6 and 9).

``run_bridge_pass`` reads an outside collector's per-chat NDJSON files (``bridge_reader``) into ``source_events``:
one transaction per file holds the file's events, its saved offset and ``committed_seq``, so a pass that dies loses
only the file in flight. A PostgreSQL advisory lock keeps one writer per source, across servers and ``looper msgflow
import``. ``SourceService`` runs a pass for every bridged source on the source's cron rule, inside ``looper serve``.

A file is read in chunks, one transaction per chunk (its events, its saved state, ``committed_seq``), so an interrupted
pass resumes at the last chunk without a duplicate. ``bridge_start_from`` decides where a new source starts: ``end``
(the default) saves every file's current end and stores only what is appended later; ``beginning`` reads each file
from its start on its first read, marking those events ``import``. ``looper msgflow import --days N`` reads every file
from its start for the last N days either way.

``origin`` of a stored event: a first read (or an import) is ``import``; a line of a known file that was sent more
than 24 hours before it was ingested is ``history``; the rest are ``live``. A source that starts from the end keeps
nothing older than seven days from a later read, so a collector rewriting a file brings no history in. Retention is
applied at the door: a body older than its source's days (apps: 30 by default) is stored as text only.
Messages sent by one of Looper's own bots are never stored, and a direct chat that holds one is excluded.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable, Collection, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import asyncpg

from ..db import message_stream as store
from . import cron
from .bridge_reader import BridgeMessage, FileState, baseline_file, scan_file
from .drain import DrainState
from .supervisor import cancel_tasks

logger = logging.getLogger(__name__)

TICK_SECONDS = 15.0
CHUNK_BYTES = 8 * 1024 * 1024
HISTORY_AFTER = timedelta(hours=24)
# A pass that is not a first read or an import keeps nothing older: a collector that rewrites a file must not bring
# its whole history in through the door a first read keeps shut.
BRIDGE_MAX_AGE = timedelta(days=7)
OWN_APPS_REFRESH_SECONDS = 300.0
BRIDGE_CRON = "*/2 * * * *"

OwnApps = Callable[[], Awaitable[Collection[str]]]


@dataclass
class PassReport:
    status: str
    run_id: int | None = None
    files: int = 0
    files_unchanged: int = 0
    files_rescanned: int = 0
    files_baselined: int = 0
    files_failed: int = 0
    events_new: int = 0
    events_seen: int = 0
    events_skipped: int = 0
    skipped_by_deletion: int = 0
    bad_lines: int = 0
    error: str | None = None
    failed_chats: list[str] = field(default_factory=list)
    file_reports: list[FileReport] = field(default_factory=list)
    # Ids a dry run has counted, across files: a message in two files would be stored once.
    dry_ids: set[str] = field(default_factory=set, repr=False)


_name_cache: dict[Path, tuple[tuple[int, int], dict[str, Any]]] = {}


def _load_names(path: Path) -> dict[str, Any]:
    """chats.json or users.json of the outside collector; {} when missing or unreadable."""
    try:
        stat = path.stat()
        key = (stat.st_mtime_ns, stat.st_size)
        cached = _name_cache.get(path)
        if cached and cached[0] == key:
            return cached[1]
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    data = data if isinstance(data, dict) else {}
    _name_cache[path] = (key, data)
    return data


def _chat_kind(entry: Any) -> str | None:
    if not isinstance(entry, Mapping):
        return None
    kind = entry.get("type")
    return kind if kind in ("p2p", "group", "topic") else "unknown"


def _people(messages: list[BridgeMessage], users: Mapping[str, Any]) -> dict[str, tuple[str, str]]:
    found: dict[str, tuple[str, str]] = {}
    for message in messages:
        ids = [*message.mention_ids, *([message.sender_id] if message.sender_kind == "user" else [])]
        for open_id in ids:
            entry = users.get(open_id)
            if isinstance(entry, Mapping) and entry.get("name") and entry.get("source") in ("contact", "mention"):
                found[open_id] = (str(entry["name"]), entry["source"])
            elif open_id in message.mention_names and open_id not in found:
                found[open_id] = (message.mention_names[open_id], "mention")
    return found


def _state_of(chat: Mapping[str, Any]) -> FileState | None:
    if chat["bridge_offset"] is None or chat["bridge_inode"] is None or chat["bridge_mtime"] is None:
        return None
    return FileState(offset=chat["bridge_offset"], size=chat["bridge_size"], inode=chat["bridge_inode"],
                     mtime=chat["bridge_mtime"], last_id=chat["bridge_last_id"])


@dataclass
class FileReport:
    """What one file gave, for the import's reconciliation: ``lines`` (messages read) is ``new`` + ``duplicates`` +
    ``skipped`` + ``skipped_by_deletion``; ``distinct`` counts the message ids among them."""

    chat_id: str
    lines: int = 0
    distinct: int = 0
    new: int = 0
    duplicates: int = 0
    skipped: int = 0
    skipped_by_deletion: int = 0
    bad_lines: int = 0
    incomplete_tail: bool = False
    rescans: int = 0
    baselined: bool = False

    @property
    def reconciled(self) -> bool:
        return self.lines == self.new + self.duplicates + self.skipped + self.skipped_by_deletion


def _body_dropped(message: BridgeMessage, source: Mapping[str, Any], now: datetime) -> bool:
    """Retention at the door: a body past its source's keep-for days is stored as text only."""
    days = source["app_body_retention_days"] if message.sender_kind == "app" else source["body_retention_days"]
    return days is not None and message.sent_at < now - timedelta(days=days)


async def _chunk(conn: Any, source: Mapping[str, Any], path: Path, run_id: int | None, file: FileReport,
                 scan: Any, *, first: bool, floor: datetime | None, own_apps: Collection[str], seen_ids: set[str],
                 users: Mapping[str, Any], entry: Any, now: datetime, chat: Mapping[str, Any],
                 dry_ids: set[str] | None) -> bool:
    """Store one chunk (``dry_ids`` is not None: count what would be stored, writing nothing). Whether the chat is
    a direct chat holding one of Looper's own bots' messages, which is excluded from now on."""
    source_id, chat_id = source["id"], path.stem
    kind = _chat_kind(entry) or chat["kind"]
    own = [m for m in scan.messages if m.sender_kind == "app" and m.sender_id in own_apps]
    looper_chat = bool(own) and kind == "p2p"
    if looper_chat and dry_ids is None:
        await store.exclude_chat(conn, source_id, chat_id, "looper_channel", now=now)
    rows: list[dict[str, Any]] = []
    kept: list[BridgeMessage] = []
    skipped = 0
    repeats: dict[str, int] = {}
    in_chunk: set[str] = set()
    user_open_id = source["user_open_id"]
    for message in scan.messages:
        file.lines += 1
        seen_ids.add(message.external_id)
        if looper_chat or (message.sender_kind == "app" and message.sender_id in own_apps) or (
                floor is not None and message.sent_at < floor):
            skipped += 1
            continue
        if message.external_id in in_chunk:
            repeats[message.external_id] = repeats.get(message.external_id, 0) + 1
            continue
        in_chunk.add(message.external_id)
        origin = "import" if first else "history" if message.sent_at < now - HISTORY_AFTER else "live"
        kept.append(message)
        rows.append({
            "external_id": message.external_id, "chat_id": chat_id, "thread_id": message.thread_id,
            "root_id": message.root_id, "parent_id": message.parent_id, "sender_id": message.sender_id,
            "sender_kind": message.sender_kind, "sent_at": message.sent_at, "msg_type": message.msg_type,
            "mentions_me": bool(user_open_id) and user_open_id in message.mention_ids,
            "mention_ids": message.mention_ids, "text": message.text,
            "body": None if _body_dropped(message, source, now) else message.body,
            "body_bytes": message.body_bytes, "origin": origin})
    deleted = await store.deleted_ids(conn, source_id, rows)
    skipped_by_deletion = sum(1 + repeats.pop(message_id, 0) for message_id in deleted)
    rows = [row for row in rows if row["external_id"] not in deleted]
    kept = [message for message in kept if message.external_id not in deleted]
    if dry_ids is not None:
        stored = await store.existing_ids(conn, source_id, [r["external_id"] for r in rows])
        fresh = [r for r in rows if r["external_id"] not in stored and r["external_id"] not in dry_ids]
        dry_ids.update(r["external_id"] for r in rows)
        new = len(fresh)
    else:
        inserted = await store.insert_events(conn, source_id, run_id, rows)
        if inserted:
            await store.advance_committed(conn, source_id, max(seq for _, seq in inserted))
        await store.upsert_people(conn, source_id, _people(kept, users), now=now)
        await store.save_bridge_state(
            conn, source_id, chat_id, offset=scan.state.offset, size=scan.state.size, inode=scan.state.inode,
            last_id=scan.state.last_id, mtime=scan.state.mtime, now=now,
            newest=max((m.sent_at for m in kept), default=None), importing=first and scan.more)
        new = len(inserted)
    seen = len(rows) - new + sum(repeats.values())
    file.new, file.duplicates, file.skipped = file.new + new, file.duplicates + seen, file.skipped + skipped
    file.skipped_by_deletion += skipped_by_deletion
    if run_id is not None:
        await store.add_run_counts(conn, run_id, new=new, seen=seen, skipped=skipped,
                                   skipped_by_deletion=skipped_by_deletion)
    return looper_chat


async def _ingest_file(source: Mapping[str, Any], path: Path, run_id: int | None, report: PassReport, *,
                       names: tuple[dict[str, Any], dict[str, Any]], own_apps: Collection[str],
                       import_since: datetime | None, force_first: bool, now: datetime, dry_run: bool,
                       conn: Any, chunk_bytes: int) -> FileReport:
    """Read one file to its end in chunks, on the connection that holds the source's writer lock: a lost lock
    session ends the writes with it. A real run commits each chunk and its saved state in one transaction, so an
    interrupted pass resumes at the last chunk; a dry run reads and writes nothing."""
    chat_id, source_id = path.stem, source["id"]
    chats, users = names
    entry = chats.get(chat_id)
    file = FileReport(chat_id)
    seen_ids: set[str] = set()
    state: FileState | None = None
    first = started = excluded_here = False
    dry_ids: set[str] | None = report.dry_ids if dry_run else None
    chat_name = str(entry.get("name") or "") if isinstance(entry, Mapping) else ""

    async def step() -> bool:
        """One chunk (or the baseline or the skip); whether the file has more."""
        nonlocal state, first, started, excluded_here
        if dry_run:
            chat = await store.chat_row(conn, source_id, chat_id) or {
                "inclusion": "included", "kind": "unknown", "bridge_offset": None, "bridge_inode": None,
                "bridge_mtime": None, "bridge_importing": False}
        else:
            chat = await store.chat_for_update(conn, source_id, chat_id, kind=_chat_kind(entry) or "unknown",
                                               name=chat_name)
        if chat["inclusion"] == "excluded" or excluded_here:
            report.files_unchanged += 1
            return False
        if not started:
            started = True
            state = None if force_first else _state_of(chat)
            # A file whose first read was cut short is still in its first read: its rest is part of that import.
            first = state is None or bool(chat["bridge_importing"])
            if not dry_run:
                await store.set_chat_directory(conn, source_id, chat_id, kind=_chat_kind(entry),
                                               name=chat_name if isinstance(entry, Mapping) else None)
            if state is None and not force_first and source["bridge_start_from"] == "end":
                base = await asyncio.to_thread(baseline_file, path)
                if not dry_run:
                    await store.save_bridge_state(conn, source_id, chat_id, offset=base.offset, size=base.size,
                                                  inode=base.inode, last_id=base.last_id, mtime=base.mtime, now=now,
                                                  newest=None)
                file.baselined = True
                report.files_baselined += 1
                return False
        scan = await asyncio.to_thread(scan_file, path, state, max_bytes=chunk_bytes)
        if scan.unchanged:
            report.files_unchanged += 1
            return False
        file.bad_lines += scan.bad_lines
        file.rescans += scan.rescanned
        file.incomplete_tail = scan.incomplete_tail
        report.bad_lines += scan.bad_lines
        report.files_rescanned += scan.rescanned
        # Which messages a read keeps: an import its days; a first read from the beginning all of them; a later
        # read of a source that starts from the end a week back at most, so a rewritten file brings no history in.
        floor = (import_since if force_first else None if first or source["bridge_start_from"] == "beginning"
                 else now - BRIDGE_MAX_AGE)
        excluded_here = await _chunk(conn, source, path, run_id, file, scan, first=first, floor=floor,
                                     own_apps=own_apps, seen_ids=seen_ids, users=users, entry=entry, now=now,
                                     chat=chat, dry_ids=dry_ids) and dry_run
        state = scan.state
        return scan.more

    more = True
    while more:
        if dry_run:
            more = await step()
        else:
            async with store.transaction(conn):
                more = await step()
    file.distinct = len(seen_ids)
    report.events_new += file.new
    report.events_seen += file.duplicates
    report.events_skipped += file.skipped
    report.skipped_by_deletion += file.skipped_by_deletion
    return file


async def run_bridge_pass(source: Mapping[str, Any], *, trigger: str = "scheduled", days: int | None = None,
                          dry_run: bool = False, own_apps: Collection[str] = (), now: datetime | None = None,
                          db: Any = None, retention_days: int | None = None) -> PassReport:
    """One pass over the source's directory. ``days`` makes it an import: every file is read from its start and kept
    back to ``days`` days ago. ``dry_run`` reads the same files and the stored ids and writes nothing; it takes the
    lock too, so it never counts beside a pass that is writing."""
    from ..db.connection import get_pool

    pool = db if db is not None else get_pool()
    now = now or datetime.now(timezone.utc)
    mode = "import" if days is not None else "bridge"
    # Every write of the pass goes through this connection, the one whose session holds the writer lock.
    async with pool.acquire() as lock:
        if not await store.try_lock(lock, source["id"]):
            return PassReport(status="skipped_busy")
        try:
            current = await store.get_source(source["id"], db=lock)
            if current is None:
                return PassReport(status="failed", error="source no longer exists")
            if current["ingest_mode"] == "collect":
                from ..db.message_collection import CollectionConflict
                raise CollectionConflict("collection owns this source; switch to bridge before importing")
            if days is None and current["ingest_mode"] != "bridge":
                return PassReport(status="skipped_busy")
            return await _pass(current, Path(current["bridge_dir"]).expanduser(), mode, trigger, days, dry_run,
                               own_apps, now, lock, retention_days)
        finally:
            try:
                await store.unlock(lock, source["id"])
            except Exception:
                logger.debug("the writer lock was already gone")


def _connection_closed(conn: Any) -> bool:
    try:
        return conn.is_closed()
    except asyncpg.InterfaceError:
        # A lost session detaches its pooled proxy; even is_closed() then raises.
        return True


async def _pass(source: Mapping[str, Any], directory: Path, mode: str, trigger: str, days: int | None, dry_run: bool,
                own_apps: Collection[str], now: datetime, conn: Any, retention_days: int | None) -> PassReport:
    report = PassReport(status="running")
    run_id: int | None = None
    if not dry_run:
        await store.interrupt_stale_runs(conn, source["id"])
        run_id = report.run_id = await store.start_run(conn, source["id"], mode,
                                                       "manual" if days is not None else trigger, now=now)
    names = (_load_names(directory / "chats.json"), _load_names(directory / "users.json"))
    import_since = now - timedelta(days=days) if days is not None else None
    files: list[Path] = []
    if (directory / "chats").is_dir():
        files = sorted((directory / "chats").glob("*.ndjson"))
    else:
        report.error = "the bridge directory has no chats directory"
    failures: list[str] = []
    try:
        for path in files:
            report.files += 1
            try:
                report.file_reports.append(await _ingest_file(
                    source, path, run_id, report, names=names, own_apps=own_apps, import_since=import_since,
                    force_first=days is not None, now=now, dry_run=dry_run, conn=conn, chunk_bytes=CHUNK_BYTES))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # Name the failure by chat and kind, never by its text: a database error can quote a message.
                logger.warning("bridge read of chat %s failed: %s", path.stem, type(exc).__name__)
                failures.append(f"{path.stem}: {type(exc).__name__}")
                if _connection_closed(conn):
                    break
        report.files_failed = len(failures)
        report.failed_chats = [item.split(":", 1)[0] for item in failures]
        if report.error is not None or (failures and len(failures) == report.files) or _connection_closed(conn):
            report.status = "failed"
        else:
            report.status = "partial" if failures else "ok"
        if failures:
            report.error = report.error or f"{len(failures)} of {report.files} files failed; first {failures[0]}"
    except asyncio.CancelledError:
        if run_id is not None:
            await asyncio.shield(store.finish_run(conn, run_id, "interrupted", now=datetime.now(timezone.utc),
                                                  files_rescanned=report.files_rescanned))
        raise
    if run_id is not None and not _connection_closed(conn):
        await store.finish_run(conn, run_id, report.status, now=datetime.now(timezone.utc), error=report.error,
                               files_rescanned=report.files_rescanned,
                               ok_for=source["id"] if report.status in ("ok",) else None)
        if retention_days:
            await store.prune_runs(conn, source["id"], before=now - timedelta(days=retention_days))
    return report


async def own_bot_app_ids() -> set[str]:
    """The Lark apps of every Lark channel Looper has configured, enabled or not (one it no longer runs still sent
    what it sent), read from the channel configuration."""
    from ..db.channel_configs import list_configs
    from .channels.bots import lark_app_id

    apps: set[str] = set()
    for row in await list_configs():
        if row["channel_type"] in ("lark", "lark_bot"):
            app = await lark_app_id(row["channel_type"], row.get("config") or {})
            if app:
                apps.add(app)
    return apps


def is_due(source: Mapping[str, Any], rule: cron.CronRule, now: datetime) -> bool:
    """A fire time of the rule has passed since the source last ran (after a sleep, one catch-up run)."""
    last = cron.last_fire(rule, cron.zone(source["timezone"]), now)
    return last is not None and (source["last_run_at"] is None or last > source["last_run_at"])


def health(source: Mapping[str, Any], last_status: str | None, now: datetime) -> str:
    """``off``, ``degraded`` (the last run was partial), ``stale`` (no ok run for an hour) or ``healthy``."""
    if source["ingest_mode"] == "off":
        return "off"
    if source["ingest_mode"] == "collect":
        if source.get("auth_state") in ("missing", "expired") or last_status == "auth_failed":
            return "degraded"
        since = source.get("last_collect_ok_at") or source.get("collect_enabled_at") or source["updated_at"]
        if (now - since).total_seconds() >= store.STALE_AFTER_SECONDS:
            return "stale"
        ratio = source.get("unindexed_ratio")
        return "degraded" if last_status in ("partial", "failed", "search_failed", "interrupted") or (ratio is not None and ratio > .02) else "healthy"
    since = source["last_ok_at"] or source["updated_at"]
    if (now - since).total_seconds() > store.STALE_AFTER_SECONDS:
        return "stale"
    return "degraded" if last_status in ("partial", "failed") else "healthy"


async def cancel_collect_task(task: asyncio.Task[Any] | None) -> None:
    """Cancel one collection the manual start handshake launched."""
    await cancel_tasks([task])


class SourceService:
    """Every ``interval`` seconds, run a pass for each bridged source whose time rule has fired."""

    def __init__(self, *, interval: float = TICK_SECONDS, drain: DrainState | None = None,
                 own_apps: OwnApps | None = None, collector_backend: Any = None, alerts: Any = None) -> None:
        self._interval = interval
        self._drain = drain
        self._own_apps_source = own_apps
        self._own_apps: set[str] = set()
        self._own_apps_at = float("-inf")
        self._task: asyncio.Task[Any] | None = None
        self._busy: set[str] = set()
        self._collector_backend = collector_backend
        self._alerts = alerts
        self._alerts_enabled = False
        self._collect_tasks: dict[str, asyncio.Task[Any]] = {}
        self._alert_task: asyncio.Task[Any] | None = None

    async def start(self) -> None:
        if self._task is None:
            await self.refresh_alerts()
            self._task = asyncio.create_task(self._loop(), name="source-service")

    async def refresh_alerts(self) -> None:
        """Read the switch at startup and after settings changes, never on an idle tick."""
        if self._alerts is not None:
            from ..db import message_stream_alerts
            from ..db.connection import get_pool

            self._alerts_enabled = await message_stream_alerts.configured(get_pool())

    async def stop(self) -> None:
        task, self._task = self._task, None
        # The loop can start a collection after a dropped cancel. Wait until the loop has ended, then cancel
        # whatever it left in ``_collect_tasks``.
        await cancel_tasks([task])
        collecting = list(self._collect_tasks.values())
        alert, self._alert_task = self._alert_task, None
        await cancel_tasks([*collecting, alert])
        self._collect_tasks.clear()
        if self._alerts is not None:
            await self._alerts.stop()

    async def _loop(self) -> None:
        while True:
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("the source loop failed; trying again later", exc_info=True)
            await asyncio.sleep(self._interval)

    async def known_own_apps(self) -> set[str]:
        """The apps of Looper's own bots; it only grows, so a bot replaced since still counts."""
        loop_time = asyncio.get_running_loop().time()
        if self._own_apps_source is not None and loop_time - self._own_apps_at >= OWN_APPS_REFRESH_SECONDS:
            self._own_apps_at = loop_time
            try:
                self._own_apps |= {app for app in await self._own_apps_source() if app}
            except Exception:
                logger.warning("could not read the bots' app ids; using the ones known", exc_info=True)
        return set(self._own_apps)

    async def run_once(self, *, clock: Callable[[], datetime] | None = None) -> int:
        """Run each due bridged source; how many ran."""
        now = (clock or (lambda: datetime.now(timezone.utc)))()
        if self._alerts_enabled and (self._alert_task is None or self._alert_task.done()):
            self._alert_task = asyncio.create_task(self._alert_tick(now), name="source-alerts")
        if self._drain is not None and self._drain.draining:
            return 0
        ran = 0
        for source in await store.list_sources():
            if source.get("retention_last_at") is None or now - source["retention_last_at"] >= timedelta(days=1):
                try:
                    dropped = await store.retain_bodies(source["id"], now=now)
                    logger.info("source %s retention dropped %d raw bodies", source["id"], dropped)
                    await self._prune_collection_metadata(source["id"], now)
                except Exception:
                    logger.warning("source %s retention failed; trying again later", source["id"], exc_info=True)
            if source["ingest_mode"] == "collect":
                ran += await self._schedule_collect(source["id"], now)
                continue
            if source["ingest_mode"] != "bridge" or source["id"] in self._busy:
                continue
            try:
                rule = cron.parse(source["cron"])
                due = is_due(source, rule, now)
            except ValueError:
                logger.warning("source %s has a time rule that does not parse", source["id"])
                continue
            if not due:
                continue
            self._busy.add(source["id"])
            try:
                report = await self.run_source(source, now=now)
                ran += report.status != "skipped_busy"
            finally:
                self._busy.discard(source["id"])
        return ran

    async def run_source(self, source: Mapping[str, Any], *, trigger: str = "scheduled",
                         now: datetime | None = None) -> PassReport:
        from . import scheduler_settings

        if source["ingest_mode"] == "collect":
            from .message_collect import run_collect_pass
            return await run_collect_pass(source, backend=self._backend(), own_apps=await self.known_own_apps(), now=now)
        try:
            retention = await scheduler_settings.run_retention_days()
        except Exception:
            retention = scheduler_settings.DEFAULT_RUN_RETENTION_DAYS
        return await run_bridge_pass(source, trigger=trigger, own_apps=await self.known_own_apps(), now=now,
                                     retention_days=retention)


    async def _prune_collection_metadata(self, source_id: str, now: datetime) -> None:
        from ..db.connection import get_pool
        from .message_collect import prune_collection

        async with get_pool().acquire() as conn:
            configured = await conn.fetchval(
                "SELECT EXISTS(SELECT 1 FROM source_collection_settings WHERE source_id=$1)", source_id)
            if not configured or not await store.try_lock(conn, source_id):
                return
            try:
                await asyncio.wait_for(prune_collection(conn, source_id, now), 5)
            finally:
                await store.unlock(conn, source_id)

    async def _alert_tick(self, now: datetime) -> None:
        if not self._alerts_enabled:
            return
        try:
            await self._alerts.evaluate(now=now)
            await self._alerts.flush()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("source alert tick failed; trying again later")

    def _backend(self) -> Any:
        if self._collector_backend is None:
            from .message_collect_lark import LarkUserCollector
            self._collector_backend = LarkUserCollector()
        return self._collector_backend

    async def _schedule_collect(self, source_id: str, now: datetime) -> int:
        from ..db import message_collection as collection
        from ..db.connection import get_pool
        source = await collection.collector_source(source_id)
        if source is None or source["ingest_mode"] != "collect":
            return 0
        try:
            fire = cron.last_fire(cron.parse(source["collect_cron"]), cron.zone(source["timezone"]), now)
        except ValueError:
            logger.warning("source %s has an invalid collection time rule", source_id)
            return 0
        if fire is None or not await collection.claim_fire(source_id, source["generation"], fire):
            return 0
        if source_id in self._collect_tasks:
            async with get_pool().acquire() as conn:
                run_id = await store.start_run(conn, source_id, "collect", "scheduled", now=now)
                await store.finish_run(conn, run_id, "skipped_busy", now=now)
            return 0
        self._launch_collect(source, now=now)
        return 1

    def _launch_collect(self, source: Mapping[str, Any], *, now: datetime | None = None,
                        started: asyncio.Future | None = None) -> asyncio.Task[Any]:
        from .message_collect import run_collect_pass
        source_id = source["id"]

        async def perform() -> Any:
            try:
                return await run_collect_pass(source, backend=self._backend(), generation=source["generation"],
                    own_apps=await self.known_own_apps(), now=now, started=started)
            except asyncio.CancelledError:
                if started is not None and not started.done():
                    started.cancel()
                raise
            except Exception as exc:
                if started is not None and not started.done():
                    started.set_exception(exc)
                logger.warning("source %s collection failed: %s", source_id, type(exc).__name__)
            finally:
                try:
                    if self._alerts_enabled and not asyncio.current_task().cancelling():
                        try:
                            await asyncio.wait_for(self._alerts.evaluate(source_id), 5)
                        except Exception:
                            logger.warning("source %s alert evaluation failed", source_id)
                finally:
                    self._collect_tasks.pop(source_id, None)

        task = asyncio.create_task(perform(), name=f"source-collect:{source_id}")
        self._collect_tasks[source_id] = task
        return task

    async def run_now(self, source_id: str) -> dict[str, Any]:
        """A bounded start handshake; never queue behind an account read or writer."""
        from ..db import message_collection as collection
        from .message_collect import CollectorError, CollectorErrorKind
        if self._drain is not None and self._drain.draining:
            raise CollectorError(CollectorErrorKind.CONFIGURATION)
        if source_id in self._collect_tasks:
            raise CollectorError(CollectorErrorKind.CONNECTION)
        source = await collection.collector_source(source_id)
        if source is None or source["ingest_mode"] != "collect":
            raise CollectorError(CollectorErrorKind.CONFIGURATION)
        # Recheck after the DB await, so simultaneous manual requests cannot overlap.
        if source_id in self._collect_tasks:
            raise CollectorError(CollectorErrorKind.CONNECTION)
        started = asyncio.get_running_loop().create_future()
        task = self._launch_collect(source, started=started)
        try:
            return await asyncio.wait_for(asyncio.shield(started), 5)
        except BaseException:
            await cancel_collect_task(task)
            if not started.done():
                started.cancel()
            elif not started.cancelled():
                started.exception()
            raise
