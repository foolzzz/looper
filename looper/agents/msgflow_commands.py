"""``looper msgflow``: one-off work on the message stream that does not belong in the server.

``import`` reads an outside collector's per-chat files from their start and stores the last ``--days`` days of them
(``--dry-run`` stores nothing and reports what it would). It takes the source's writer lock; while the server is
in a pass it says so and exits.
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Sequence
from typing import Any

IMPORT_MAX_DAYS = 3650


def format_report(report: Any, *, days: int, dry_run: bool) -> str:
    verb = "would store" if dry_run else "stored"
    lines = [
        f"{'dry run: ' if dry_run else ''}import of the last {days} day{'s' if days != 1 else ''}: {report.status}",
        f"  files: {report.files} read, {report.files_failed} failed, {report.files_rescanned} read again",
        f"  events: {verb} {report.events_new}, already stored {report.events_seen}, skipped {report.events_skipped}",
    ]
    if report.bad_lines:
        lines.append(f"  lines that were not a message: {report.bad_lines}")
    if report.error:
        lines.append(f"  error: {report.error}")
    return "\n".join(lines)


async def _import(database_url: str, *, source_id: str | None, days: int, dry_run: bool) -> tuple[int, str]:
    from ..db import message_stream as store
    from ..db.connection import close_pool, init_pool
    from .source_service import own_bot_app_ids, run_bridge_pass

    pool = await init_pool(database_url, min_size=1, max_size=3)
    try:
        if not await pool.fetchval("SELECT to_regclass('event_sources') IS NOT NULL"):
            return 1, "the database has no message stream tables yet: start `looper serve` once to upgrade it"
        sources = await store.list_sources()
        found = [s for s in sources if source_id in (None, s["id"])]
        if source_id is None and len(sources) > 1:
            return 1, f"there are {len(sources)} sources ({', '.join(s['id'] for s in sources)}); name one with --source"
        if not found:
            return 1, "no message source is set up: create it from the dashboard's Message stream page"
        source = found[0]
        if not source["bridge_dir"]:
            return 1, f"source {source['id']} has no bridge directory to import from"
        report = await run_bridge_pass(source, days=days, dry_run=dry_run, own_apps=await own_bot_app_ids())
        if report.status == "skipped_busy":
            return 1, f"source {source['id']} is being written by another pass; try again in a moment"
        return (0 if report.status in ("ok", "partial") else 1), format_report(report, days=days, dry_run=dry_run)
    finally:
        await close_pool()


def import_history(database_url: str, *, source_id: str | None, days: int, dry_run: bool) -> int:
    code, text = asyncio.run(_import(database_url, source_id=source_id, days=days, dry_run=dry_run))
    print(text, file=sys.stderr if code else sys.stdout)
    return code


def add_parser(commands: Any) -> None:
    msgflow = commands.add_parser("msgflow", allow_abbrev=False, help="Work on the message stream.",
                                  description="One-off work on the message stream.")
    sub = msgflow.add_subparsers(dest="msgflow_command", required=True)
    imp = sub.add_parser(
        "import", allow_abbrev=False, help="Import the last N days from an outside collector's files.",
        description="Read the source's bridge directory from the start of every file and store the messages sent in "
                    "the last N days (a message already stored is left as it is). --dry-run stores nothing and "
                    "prints what it would store. If the server is writing the source at that moment it says so and exits; run it again.")
    imp.add_argument("--days", type=int, required=True, help=f"How many days back to import (1 to {IMPORT_MAX_DAYS}).")
    imp.add_argument("--dry-run", action="store_true", help="Count what would be stored; store nothing.")
    imp.add_argument("--source", help="The source id (default: the only one).")
    imp.add_argument("--runtime-root", help="The state directory (default ~/.looper).")
    imp.add_argument("--config", help="The ops file (default <runtime root>/ops/server.toml).")


def run(args: Any, load_ops: Any) -> int:
    if not 1 <= args.days <= IMPORT_MAX_DAYS:
        print(f"error: --days is a whole number from 1 to {IMPORT_MAX_DAYS}", file=sys.stderr)
        return 2
    ops = load_ops(args)
    if ops is None:
        return 1
    return import_history(ops.database_url, source_id=args.source, days=args.days, dry_run=args.dry_run)


__all__: Sequence[str] = ("add_parser", "run", "import_history")
