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


def format_report(report: Any, *, days: int, dry_run: bool, per_file: bool = False) -> str:
    verb = "would store" if dry_run else "stored"
    files = [f for f in report.file_reports if not f.baselined]
    total = {name: sum(getattr(f, name) for f in files) for name in
             ("lines", "distinct", "new", "duplicates", "skipped", "bad_lines", "rescans")}
    unreconciled = [f for f in files if not f.reconciled]
    lines = [
        f"{'dry run: ' if dry_run else ''}import of the last {days} day{'s' if days != 1 else ''}: {report.status}",
        f"  files: {report.files} read, {report.files_failed} failed, {total['rescans']} read again, "
        f"{sum(f.incomplete_tail for f in files)} with an unterminated last line",
        f"  messages read: {total['lines']} ({total['distinct']} distinct ids)",
        f"  {verb} {total['new']}, already stored or repeated {total['duplicates']}, skipped {total['skipped']} "
        "(a Looper bot's own, an excluded chat, older than the days asked)",
        f"  reconciliation: {'every file adds up (read = new + already stored + skipped)' if not unreconciled else str(len(unreconciled)) + ' files do not add up: ' + ', '.join(f.chat_id for f in unreconciled[:10])}",
    ]
    if total["bad_lines"]:
        lines.append(f"  lines that were not a message: {total['bad_lines']}")
    if per_file:
        lines += [f"  {f.chat_id}: read {f.lines}, distinct {f.distinct}, new {f.new}, already stored {f.duplicates}, "
                  f"skipped {f.skipped}, bad {f.bad_lines}, rescans {f.rescans}"
                  f"{', unterminated tail' if f.incomplete_tail else ''}" for f in files]
    if report.error:
        lines.append(f"  error: {report.error}")
    return "\n".join(lines)


async def _import(database_url: str, *, source_id: str | None, days: int, dry_run: bool,
                  per_file: bool = False) -> tuple[int, str]:
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
        return (0 if report.status in ("ok", "partial") else 1), format_report(report, days=days, dry_run=dry_run, per_file=per_file)
    finally:
        await close_pool()


def import_history(database_url: str, *, source_id: str | None, days: int, dry_run: bool,
                   per_file: bool = False) -> int:
    code, text = asyncio.run(_import(database_url, source_id=source_id, days=days, dry_run=dry_run,
                                     per_file=per_file))
    print(text, file=sys.stderr if code else sys.stdout)
    return code


def run(args: Any, load_ops: Any) -> int:
    if not 1 <= args.days <= IMPORT_MAX_DAYS:
        print(f"error: --days is a whole number from 1 to {IMPORT_MAX_DAYS}", file=sys.stderr)
        return 2
    ops = load_ops(args)
    if ops is None:
        return 1
    return import_history(ops.database_url, source_id=args.source, days=args.days, dry_run=args.dry_run,
                          per_file=args.per_file)


__all__: Sequence[str] = ("run", "import_history")
