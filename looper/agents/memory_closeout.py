"""An ended task's memory closeout: propose lesson and project candidates from its journal.

The project switch, memory writing, and a lesson or project target must all be on. Otherwise the vault
closeout runs as before. Failures warn; they do not change the task's result. A blocked turn still fails it.
"""

from __future__ import annotations

import json
import logging
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from ..db import activity, journal as db_journal, memory as db_memory, projects as db_projects
from . import memory_model as model
from . import memory_settings, untrusted
from .advice_lint import _SECRETS
from .claude_cli import TurnBlocked
from .memory_model import Actor, EvidenceKind, Kind, Via
from .project_execution import format_usd
from .tiers import journal
from .tiers.journal import JournalEntry
from .tiers.planner_protocol import ProtocolError, TurnKind, _only_keys, _required_text, extract_reply

logger = logging.getLogger(__name__)

STATEMENT_MAX = WHY_MAX = NOTE_MAX = 300
KEYWORDS_MAX = 6
KEYWORD_MAX_CHARS = 40
_ITEM_KEYS = {"kind", "statement", "why", "keywords", "evidence"}
_KINDS = (Kind.LESSON, Kind.PROJECT)


@dataclass(frozen=True)
class MemoryItem:
    kind: str
    statement: str
    why: str
    keywords: tuple[str, ...]
    journal_ids: tuple[int, ...]


@dataclass(frozen=True)
class MemoryReply:
    items: tuple[MemoryItem, ...]
    dropped: Counter[str]
    note: str


def memory_message(ended: str, lines: Sequence[str], kinds: Sequence[str]) -> str:
    """The ``[MEMORY]`` turn (memory-write-path RFC §3.3). ``kinds`` are the configured lesson and project targets."""
    shown = "|".join(kinds)
    return "\n".join([
        f"[MEMORY] The task {ended}. Propose what later work in any project should know, for the operator to confirm into",
        "their knowledge vault: verified lessons (a cause and its fix, a check that works, a pitfall) and stable facts about",
        "a project (paths, commands, environments). Not what only this task needed, not what the project's own knowledge",
        "already says, nothing about the operator's preferences. Each proposal cites the journal entries it rests on (the",
        "J ids). At most 5; none is a fine answer. Do not change any files.",
        "",
        *lines,
        "",
        "End your reply with exactly one ```json block, and nothing after it:",
        "```json",
        '{"memory": [{"kind": "' + shown + '", "statement": "…", "why": "…", "keywords": ["…"], "evidence": ["J3"]}],',
        ' "note": "one line: what you proposed, or why nothing"}',
        "```",
    ])


def _item_problem(item: Any, ids: Mapping[str, int], kinds: set[str]) -> str | None:
    """Why ``item`` is dropped, or None when it can be proposed. One reason, cheapest shape first."""
    if not isinstance(item, Mapping) or set(item) != _ITEM_KEYS:
        return "invalid"
    if item.get("kind") not in kinds:
        return "kind"
    statement, why, words = item.get("statement"), item.get("why"), item.get("keywords")
    if not isinstance(statement, str) or not isinstance(why, str) or not isinstance(words, list):
        return "invalid"
    statement, why = statement.strip(), why.strip()
    if not 1 <= len(statement) <= STATEMENT_MAX or len(why) > WHY_MAX or not 1 <= len(words) <= KEYWORDS_MAX:
        return "invalid"
    for word in words:
        if not isinstance(word, str) or not (stripped := word.strip()) or not 1 <= len(stripped) <= KEYWORD_MAX_CHARS:
            return "invalid"
    evidence = item.get("evidence")
    if (not isinstance(evidence, list) or not evidence
            or any(not isinstance(label, str) or label not in ids for label in evidence)):
        return "evidence"
    if _SECRETS.search(statement) or _SECRETS.search(why):
        return "secret"
    return None


def parse_memory(text: str, ids: Mapping[str, int], kinds: Sequence[str]) -> MemoryReply:
    """The proposals in a ``[MEMORY]`` reply. A bad reply raises ``ProtocolError``; a bad item is counted and dropped."""
    data = extract_reply(text)
    _only_keys(data, {"memory", "note"}, "the MEMORY reply")
    raw = data.get("memory")
    if not isinstance(raw, list) or len(raw) > 5:
        raise ProtocolError("memory must be a list of at most 5 proposals")
    note = " ".join(_required_text(data, "note").split())[:NOTE_MAX]
    allowed = set(kinds)
    items: list[MemoryItem] = []
    dropped: Counter[str] = Counter()
    for item in raw:
        reason = _item_problem(item, ids, allowed)
        if reason:
            dropped[reason] += 1
            continue
        words = tuple(word.strip() for word in item["keywords"])
        items.append(MemoryItem(
            item["kind"], item["statement"].strip(), item["why"].strip(), words,
            tuple(ids[label] for label in item["evidence"])))
    return MemoryReply(tuple(items), dropped, note)


def _configured_kinds(settings: model.MemorySettings) -> tuple[str, ...]:
    return tuple(kind.value for kind in _KINDS if kind in settings.targets)


async def applies(planner: Any, plan: Any) -> bool:
    """Whether this ended task proposes memories. No vault I/O before the stored flag and the project switch.

    Any failure, including no database, is off: the vault closeout then runs as before.
    """
    try:
        raw = await memory_settings.stored()
        if not isinstance(raw, Mapping) or raw.get("enabled") is not True:
            return False
        sources = raw.get("sources")
        if not isinstance(sources, Mapping) or sources.get("tasks") is not True:
            return False
        project_id = getattr(plan, "project_id", None)
        if not project_id:
            return False
        project = await db_projects.get_project(project_id)
        if project is None or project.get("memory_closeout") != "on":
            return False
        settings, problem = await memory_settings.load()
        if problem or settings is None:
            return False
        return bool({Kind.LESSON, Kind.PROJECT} & set(settings.targets))
    except Exception:
        logger.debug("memory closeout does not apply", exc_info=True)
        return False


def _closeout_line(proposed: Sequence[str], note: str, dropped: Counter[str]) -> str:
    head = "Memory closeout: "
    if proposed:
        head += "proposed " + ", ".join(proposed) + ": "
    head += note
    if dropped:
        parts = ", ".join(f"{reason}: {dropped[reason]}" for reason in sorted(dropped))
        head += f"; dropped {sum(dropped.values())} ({parts})"
    return head


async def run(planner: Any, plan: Any, ended: str) -> None:
    """Propose memories for ``plan`` once. A block propagates; every other failure is a warning."""
    from .tiers.planner import PlannerTurnError

    planner._vault_closed = True
    try:
        if planner._token is None:
            return
        entries = [JournalEntry.from_row(row) for row in await db_journal.list_entries(planner._task_id)]
        if not entries:
            await planner._activity("phase", "No memory closeout: the task journal is empty")
            return
        cap = await planner._cap_reached()
        if cap is not None:
            await planner._activity(
                "error", f"Warning: no memory closeout: the task has spent its "
                         f"${format_usd(cap)} cap (task_budget_usd)"[:500])
            return
        planner._fence = await untrusted.enabled()
        lines, ids = journal.numbered_lessons_journal(entries, fence=planner._fence)
        settings, problem = await memory_settings.load()
        if settings is None:
            raise RuntimeError(problem or "memory writing is not configured")
        kinds = _configured_kinds(settings)
        try:
            reply = await planner._ask(
                TurnKind.MEMORY, memory_message(ended, lines, kinds),
                lambda text: parse_memory(text, ids, kinds))
        except PlannerTurnError as exc:
            await planner._activity("error", f"Warning: the Planner's memory closeout failed: {exc}"[:500])
            return
        proposed: list[str] = []
        dropped: Counter[str] = Counter(reply.dropped)
        for item in reply.items:
            try:
                async with db_memory.transaction() as conn:
                    created = await db_memory.create(
                        conn, kind=item.kind, statement=item.statement, why=item.why,
                        keywords=list(item.keywords),
                        evidence=[db_memory.Evidence(EvidenceKind.JOURNAL, journal_id=i) for i in item.journal_ids],
                        actor=Actor.PLANNER, via=Via.SYSTEM, project_id=plan.project_id)
                proposed.append(model.format_id(created))
            except db_memory.MemoryRefused as exc:
                dropped[exc.code] += 1
        await planner._activity("phase", _closeout_line(proposed, reply.note, dropped)[:500])
    except TurnBlocked:
        raise
    except Exception as exc:
        logger.warning("planner memory closeout failed", exc_info=True)
        try:
            await planner._activity("error", f"Warning: the Planner's memory closeout failed: {exc}"[:500])
        except Exception:
            logger.warning("planner memory closeout could not record its warning", exc_info=True)


def _changed_path(changes: Any) -> str:
    if isinstance(changes, str):
        changes = json.loads(changes)
    for change in changes or []:
        if isinstance(change, Mapping) and change.get("path"):
            return str(change["path"])
    return ""


async def note_written(conn: Any, row: Mapping[str, Any], commit: str) -> None:
    """One activity line on each task whose journal sourced this written candidate. Failures only log."""
    try:
        path = row.get("note_path") or ""
        if not path:
            revision = await conn.fetchrow(
                "SELECT changes FROM memory_candidate_revisions WHERE candidate_id = $1 AND revision = $2",
                row["id"], row.get("revision") or 0)
            path = _changed_path(revision["changes"]) if revision else ""
        summary = f"Memory: {model.format_id(row['id'])} written to {path} (commit {commit[:12]})"
        tasks = await conn.fetch(
            "SELECT DISTINCT j.task_id FROM memory_candidate_sources s "
            "JOIN task_journal j ON j.id = s.journal_id WHERE s.candidate_id = $1", row["id"])
        for task in tasks:
            await activity.insert_activity(
                task_id=task["task_id"], worker="memory", kind="phase", summary=summary, pool=conn)
    except Exception:
        logger.warning("could not record a memory write on its task", exc_info=True)
