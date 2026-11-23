"""What the memory candidate tests share: synthetic sources (a Manager's propose call, a task's journal entry, a
lead) and candidates made and revised through ``db.memory``, all on a disposable schema."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from looper.agents.memory_model import Action, Actor, EvidenceKind, MadeBy, Via
from looper.db import memory as db_memory
from tests import leads_support, subscription_support as stream

UTC = timezone.utc
# A database from before the memory candidates migration: none of its objects, and no marker. One from before an
# earlier migration they point at (the leads) predates them too.
DROP_MEMORY = ("DROP TABLE memory_posts, memory_candidate_sources, memory_candidate_revisions, memory_tombstones, "
               "memory_candidate_events, memory_candidates CASCADE; "
               "DROP TRIGGER projects_memory_lessons_drop ON projects; "
               "DROP FUNCTION memory_candidates_source_deleted(), memory_candidates_project_deleted(); "
               "DELETE FROM schema_migrations WHERE name = 'memory_candidates'; ")
NOW = datetime(2026, 11, 21, 9, 0, tzinfo=UTC)
NOTE = {"op": "create_note", "path": "mem/prefs/weekly.md", "content": "---\nkind: memory\n---\nConclusion first.\n"}
INDEX = {"op": "append_line", "path": "mem/prefs/_index.md", "under": "## Index",
         "line": "- [[weekly]] conclusion first"}
SUBJECT = "mem: preference weekly reports"


async def manager_action(pool: Any, *, chat: str = "ou_operator_synthetic") -> int:
    return await pool.fetchval(
        "INSERT INTO manager_actions (channel, chat, message_id, tool, risk, status) "
        "VALUES ('lark', $1, 'om_synthetic', 'looper_memory_propose', 'write', 'ok') RETURNING id", chat)


async def journal_entry(pool: Any, *, task_id: str = "task-mem") -> int:
    if not await pool.fetchval("SELECT EXISTS (SELECT 1 FROM projects WHERE id = 'proj-mem')"):
        await pool.execute("INSERT INTO projects (id, name) VALUES ('proj-mem', 'Synthetic project')")
    await pool.execute("INSERT INTO tasks (id, requirement, project_id) VALUES ($1, 'Do it', 'proj-mem') "
                       "ON CONFLICT DO NOTHING", task_id)
    return await pool.fetchval("INSERT INTO task_journal (task_id, step_id, kind, summary) "
                               "VALUES ($1, 's1', 'step_accepted', 'the build needs make gen') RETURNING id", task_id)


async def lead(pool: Any) -> int:
    await stream.source(pool)
    seq = await stream.add(pool, "we decided to ship on Friday", sent_at=NOW)
    return await leads_support.lead(pool, [seq])


def operator_turn(action_id: int, quote: str = "from now on, conclusion first") -> db_memory.Evidence:
    return db_memory.Evidence(EvidenceKind.MANAGER_TURN, manager_action_id=action_id, quote=quote)


async def candidate(pool: Any, *, kind: str = "preference", statement: str = "Weekly reports: conclusion first",
                    evidence: list[db_memory.Evidence] | None = None, actor: Actor = Actor.MANAGER,
                    via: Via = Via.LARK, **fields: Any) -> int:
    """A drafting candidate (the operator's own turn by default)."""
    evidence = evidence if evidence is not None else [operator_turn(await manager_action(pool))]
    async with db_memory.transaction(pool) as conn:
        return await db_memory.create(conn, kind=kind, statement=statement, why="easier to skim",
                                      keywords=["weekly", "reports"], evidence=evidence, actor=actor, via=via,
                                      **fields)


async def revise(pool: Any, candidate_id: int, *, changes: list[dict] | None = None, action: Action = Action.DRAFT,
                 actor: Actor = Actor.SYSTEM, via: Via = Via.SYSTEM, made_by: MadeBy = MadeBy.DRAFT,
                 note_path: str | None = "mem/prefs/weekly.md", now: datetime = NOW) -> dict:
    async with db_memory.transaction(pool) as conn:
        return await db_memory.add_revision(
            conn, candidate_id, action=action, actor=actor, via=via, made_by=made_by,
            changes=changes or [NOTE, INDEX], title="Weekly reports: conclusion first", subject=SUBJECT,
            diff_stats={"mem/prefs/weekly.md": {"added": 4, "removed": 0}}, note_path=note_path, pending_days=7,
            now=now)


async def move(pool: Any, candidate_id: int, to: str, action: str, *, actor: str = "system", via: str = "system",
               **fields: Any) -> dict:
    async with db_memory.transaction(pool) as conn:
        return await db_memory.transition(conn, candidate_id, to, action=action, actor=actor, via=via, now=NOW,
                                          **fields)


async def confirmed(pool: Any, candidate_id: int) -> dict:
    """``candidate_id`` drafted and confirmed by the operator on the dashboard."""
    row = await revise(pool, candidate_id)
    return await move(pool, candidate_id, "confirmed", "confirm", actor="user", via="dashboard",
                      content_hash=row["content_hash"])


def synthetic_vault(tmp_path):
    """A committed synthetic vault. It exists only under the test's temporary directory."""
    import subprocess

    repo = tmp_path / "vault"
    (repo / "mem/prefs").mkdir(parents=True)
    (repo / "mine").mkdir()
    (repo / "mem/prefs/_index.md").write_text("## Index\n")
    (repo / "mem/AGENTS.md").write_text("Synthetic rules\n")
    (repo / "mine/notes.md").write_text("Personal synthetic note\n")
    (repo / "other.md").write_text("Unrelated synthetic note\n")
    for args in (("init", "-q", "-b", "main"), ("config", "user.email", "test@example.invalid"),
                 ("config", "user.name", "Synthetic test"),
                 ("config", "core.hooksPath", str(repo / ".git/hooks")),
                 ("add", "--", "mem", "mine", "other.md"),
                 ("-c", "commit.gpgsign=false", "commit", "-q", "-m", "Synthetic vault")):
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)
    return repo


async def configure_write(repo, pool):
    """Enable writes to the synthetic vault; return its checked setting."""
    from looper.agents import memory_settings, vault

    await vault.update_settings({"path": str(repo), "writable": []})
    await memory_settings.update_settings({
        "enabled": True, "root": "mem", "rules_files": ["mem/AGENTS.md"],
        "required_frontmatter": ["kind"], "worktree_root": str(repo.parent / "worktrees"),
        "targets": {"preference": {"shape": "note", "folders": ["prefs"], "index": "prefs/_index.md"}}}, db=pool)
    settings, problem = await memory_settings.load(pool)
    assert problem is None
    return settings


async def revise_current(pool, candidate_id, settings, *, changes=None, note_path=None, title="Conclusion first"):
    """Draft a revision with the writer's exact subject, ready for a real write-path test."""
    from looper.agents import memory_write

    row = (await db_memory.get(candidate_id, db=pool))["candidate"]
    async with db_memory.transaction(pool) as conn:
        return await db_memory.add_revision(
            conn, candidate_id, action=Action.DRAFT, actor=Actor.SYSTEM, via=Via.SYSTEM,
            made_by=MadeBy.DRAFT, changes=changes or [NOTE, INDEX], title=title,
            subject=memory_write.subject(settings, {**row, "title": title}), diff_stats={}, note_path=note_path,
            pending_days=7, now=datetime.now(UTC))
