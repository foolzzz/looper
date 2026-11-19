"""Ingest message sources into the event store (message-stream RFC, sections 3, 6 and 9).

``run_bridge_pass`` reads an outside collector's per-chat NDJSON files (``bridge_reader``) into ``source_events``:
one transaction per file holds the file's events, its saved offset and ``committed_seq``, so a pass that dies loses
only the file in flight. A PostgreSQL advisory lock keeps one writer per source, across servers and ``looper msgflow
import``. ``SourceService`` runs a pass for every bridged source on the source's cron rule, inside ``looper serve``.

``origin`` of a stored event: a file read for the first time (or by an import) is ``import``; a line of a known file
that was sent more than 24 hours before it was ingested is ``history``; the rest are ``live``. A first read keeps
only the last 24 hours (an import: ``days``), so switching the bridge on never pulls a collector's whole history.
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

from ..db import message_stream as store
from . import cron
from .bridge_reader import BridgeMessage, FileState, scan_file
from .drain import DrainState

logger = logging.getLogger(__name__)

TICK_SECONDS = 15.0
FIRST_SCAN_DAYS = 1
HISTORY_AFTER = timedelta(hours=24)
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
    files_failed: int = 0
    events_new: int = 0
    events_seen: int = 0
    events_skipped: int = 0
    bad_lines: int = 0
    error: str | None = None
    failed_chats: list[str] = field(default_factory=list)


class _DryRun(Exception):
    """Raised inside a file's transaction to roll it back after it has been counted."""


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


async def _ingest_file(source: Mapping[str, Any], path: Path, run_id: int | None, report: PassReport, *,
                       names: tuple[dict[str, Any], dict[str, Any]], own_apps: Collection[str], first_since: datetime,
                       force_first: bool, now: datetime, dry_run: bool, db: Any) -> None:
    chat_id, source_id = path.stem, source["id"]
    chats, users = names
    entry = chats.get(chat_id)
    try:
        async with store.transaction(db) as conn:
            chat = await store.chat_for_update(conn, source_id, chat_id, kind=_chat_kind(entry) or "unknown",
                                               name=str(entry.get("name") or "") if isinstance(entry, Mapping) else "")
            if chat["inclusion"] == "excluded":
                report.files_unchanged += 1
                return
            state = None if force_first else _state_of(chat)
            scan = await asyncio.to_thread(scan_file, path, state)
            if scan.unchanged:
                report.files_unchanged += 1
                return
            first = state is None
            report.bad_lines += scan.bad_lines
            report.files_rescanned += scan.rescanned
            await store.set_chat_directory(conn, source_id, chat_id, kind=_chat_kind(entry),
                                           name=str(entry.get("name") or "") if isinstance(entry, Mapping) else None)
            kind = _chat_kind(entry) or chat["kind"]
            own = [m for m in scan.messages if m.sender_kind == "app" and m.sender_id in own_apps]
            looper_chat = bool(own) and kind == "p2p"
            if looper_chat:
                await store.exclude_chat(conn, source_id, chat_id, "looper_channel", now=now)
            rows: list[dict[str, Any]] = []
            kept: list[BridgeMessage] = []
            skipped = scan.bad_lines
            seen_here: set[str] = set()
            repeats = 0
            user_open_id = source["user_open_id"]
            for message in scan.messages:
                if looper_chat or (message.sender_kind == "app" and message.sender_id in own_apps) or (
                        first and message.sent_at < first_since):
                    skipped += 1
                    continue
                if message.external_id in seen_here:
                    repeats += 1
                    continue
                seen_here.add(message.external_id)
                origin = "import" if first else "history" if message.sent_at < now - HISTORY_AFTER else "live"
                kept.append(message)
                rows.append({
                    "external_id": message.external_id, "chat_id": chat_id, "thread_id": message.thread_id,
                    "root_id": message.root_id, "parent_id": message.parent_id, "sender_id": message.sender_id,
                    "sender_kind": message.sender_kind, "sent_at": message.sent_at, "msg_type": message.msg_type,
                    "mentions_me": bool(user_open_id) and user_open_id in message.mention_ids,
                    "mention_ids": message.mention_ids, "text": message.text, "body": message.body,
                    "body_bytes": message.body_bytes, "origin": origin})
            inserted = await store.insert_events(conn, source_id, run_id, rows)
            if inserted:
                await store.advance_committed(conn, source_id, max(seq for _, seq in inserted))
            await store.upsert_people(conn, source_id, _people(kept, users), now=now)
            await store.save_bridge_state(
                conn, source_id, chat_id, offset=scan.state.offset, size=scan.state.size, inode=scan.state.inode,
                last_id=scan.state.last_id, mtime=scan.state.mtime, now=now,
                newest=max((m.sent_at for m in kept), default=None))
            new = len(inserted)
            report.events_new += new
            report.events_seen += len(rows) - new + repeats
            report.events_skipped += skipped
            if run_id is not None:
                await store.add_run_counts(conn, run_id, new=new, seen=len(rows) - new + repeats, skipped=skipped)
            if dry_run:
                raise _DryRun
    except _DryRun:
        pass


async def run_bridge_pass(source: Mapping[str, Any], *, trigger: str = "scheduled", days: int | None = None,
                          dry_run: bool = False, own_apps: Collection[str] = (), now: datetime | None = None,
                          db: Any = None, retention_days: int | None = None) -> PassReport:
    """One pass over the source's directory. ``days`` makes it an import: every file is read from its start and kept
    back to ``days`` days ago. ``dry_run`` counts what would be stored and stores nothing (it needs the lock too)."""
    from ..db.connection import get_pool

    pool = db if db is not None else get_pool()
    now = now or datetime.now(timezone.utc)
    mode = "import" if days is not None else "bridge"
    directory = Path(source["bridge_dir"]).expanduser()
    async with pool.acquire() as lock:
        if not await store.try_lock(lock, source["id"]):
            return PassReport(status="skipped_busy")
        try:
            return await _pass(source, directory, mode, trigger, days, dry_run, own_apps, now, pool, retention_days)
        finally:
            await store.unlock(lock, source["id"])


async def _pass(source: Mapping[str, Any], directory: Path, mode: str, trigger: str, days: int | None, dry_run: bool,
                own_apps: Collection[str], now: datetime, pool: Any, retention_days: int | None) -> PassReport:
    report = PassReport(status="running")
    run_id: int | None = None
    if not dry_run:
        await store.interrupt_stale_runs(pool, source["id"])
        run_id = report.run_id = await store.start_run(pool, source["id"], mode, "manual" if days is not None else trigger,
                                                       now=now)
    names = (_load_names(directory / "chats.json"), _load_names(directory / "users.json"))
    first_since = now - timedelta(days=days if days is not None else FIRST_SCAN_DAYS)
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
                await _ingest_file(source, path, run_id, report, names=names, own_apps=own_apps,
                                   first_since=first_since, force_first=days is not None, now=now, dry_run=dry_run,
                                   db=pool)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # Name the failure by chat and kind, never by its text: a database error can quote a message.
                logger.warning("bridge read of chat %s failed: %s", path.stem, type(exc).__name__)
                logger.debug("bridge read failure", exc_info=True)
                failures.append(f"{path.stem}: {type(exc).__name__}")
        report.files_failed = len(failures)
        report.failed_chats = [item.split(":", 1)[0] for item in failures]
        if report.error is not None or (failures and len(failures) == report.files):
            report.status = "failed"
        else:
            report.status = "partial" if failures else "ok"
        if failures:
            report.error = report.error or f"{len(failures)} of {report.files} files failed; first {failures[0]}"
    except asyncio.CancelledError:
        if run_id is not None:
            await asyncio.shield(store.finish_run(pool, run_id, "interrupted", now=datetime.now(timezone.utc),
                                                  files_rescanned=report.files_rescanned))
        raise
    if run_id is not None:
        await store.finish_run(pool, run_id, report.status, now=datetime.now(timezone.utc), error=report.error,
                               files_rescanned=report.files_rescanned,
                               ok_for=source["id"] if report.status in ("ok",) else None)
        if retention_days:
            await store.prune_runs(pool, source["id"], before=now - timedelta(days=retention_days))
    return report


async def own_bot_app_ids() -> set[str]:
    """The Lark apps Looper's own enabled channel bots act as, read from the channel configuration."""
    from ..db.channel_configs import list_configs
    from .channels.bots import lark_app_id

    apps: set[str] = set()
    for row in await list_configs():
        if row["enabled"] and row["channel_type"] in ("lark", "lark_bot"):
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
    since = source["last_ok_at"] or source["updated_at"]
    if (now - since).total_seconds() > store.STALE_AFTER_SECONDS:
        return "stale"
    return "degraded" if last_status in ("partial", "failed") else "healthy"


class SourceService:
    """Every ``interval`` seconds, run a pass for each bridged source whose time rule has fired."""

    def __init__(self, *, interval: float = TICK_SECONDS, drain: DrainState | None = None,
                 own_apps: OwnApps | None = None) -> None:
        self._interval = interval
        self._drain = drain
        self._own_apps_source = own_apps
        self._own_apps: set[str] = set()
        self._own_apps_at = float("-inf")
        self._task: asyncio.Task[Any] | None = None
        self._busy: set[str] = set()

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop(), name="source-service")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

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
        if self._drain is not None and self._drain.draining:
            return 0
        now = (clock or (lambda: datetime.now(timezone.utc)))()
        ran = 0
        for source in await store.list_sources():
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

        try:
            retention = await scheduler_settings.run_retention_days()
        except Exception:
            retention = scheduler_settings.DEFAULT_RUN_RETENTION_DAYS
        return await run_bridge_pass(source, trigger=trigger, own_apps=await self.known_own_apps(), now=now,
                                     retention_days=retention)
