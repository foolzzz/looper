"""Turn queued memory seeds into reviewable revisions; no vault writes or Manager context.

Short transactions claim a bounded spend lease and persist the result; inference holds no database locks.
Typed target waits survive restarts without paid retries. Failures record only typed reasons. The writer
still requires the operator's confirmation of the new hash.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

import asyncpg

from ..db import connection, memory as store, system_settings
from . import memory_draft, memory_draft_role, memory_model as model, memory_settings, memory_write, vault
from .model_call import CallFailed, CallFailure, Execution
from .spawn import MIN_RUN_USD, SpawnParent

logger = logging.getLogger(__name__)
TICK_SECONDS = 10
# The whole call is bounded before the lease expires, including input assembly and role resolution.
_CALL_SECONDS = memory_draft_role.TIMEOUT_SEC + 30
_LEASE_SECONDS = _CALL_SECONDS + 30
# Independent of the vault writer's repository lock; only drafting spends this daily allowance.
_LOCK = 741934
_WAKE = "looper_memory_draft"
_NOTICE = "memory_draft_budget_notice"


class MemoryDraftService:
    def __init__(self, runtime_root: Path, *, db: Any = None, drain: Any = None, claude_bin: str = "claude",
                 backend: Any = None, sender: Any = None):
        self.runtime_root, self.db, self.drain = runtime_root, db, drain
        self.claude_bin, self.backend, self.sender = claude_bin, backend, sender
        self._woken = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._listener: Any = None
        self._stopping = False
        self._busy = False
        self._pass_lock = asyncio.Lock()

    def wake(self) -> None:
        self._woken.set()

    def drain_waiting(self) -> int:
        return int(self._busy)

    def _notified(self, *_args) -> None:
        self.wake()

    async def start(self, database_url: str) -> None:
        if self._task is None:
            self._stopping = False
            # LISTEN owns a session for the service's lifetime; leave pooled slots for short queries.
            self._listener = await asyncpg.connect(database_url, timeout=10)
            await self._listener.add_listener(_WAKE, self._notified)
            self._task = asyncio.create_task(self._loop(), name="memory-draft")

    async def stop(self) -> None:
        self._stopping = True
        self.wake()
        if self._task is not None:
            await asyncio.shield(self._task)
            self._task = None
        if self._listener is not None:
            await self._listener.close()
            self._listener = None

    async def _loop(self) -> None:
        while not self._stopping:
            self._woken.clear()
            try:
                processed = await self.run_once()
            except Exception:
                logger.warning("memory drafting pass deferred")  # Never log private model input or its reply.
                processed = False
            if not processed and not self._stopping:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._woken.wait(), TICK_SECONDS)

    async def run_once(self) -> bool:
        async with self._pass_lock:
            raw = await memory_settings.stored(self.db)
            if raw is None or raw.get("enabled") is not True or (self.drain is not None and self.drain.draining):
                return False
            self._busy = True
            try:
                return await self._pass()
            finally:
                self._busy = False

    async def _pass(self) -> bool:
        pool = self.db or connection.get_pool()
        token = uuid4()
        async with pool.acquire() as conn, conn.transaction():
            if not await conn.fetchval("SELECT pg_try_advisory_xact_lock($1::bigint)", _LOCK):
                return False
            if await conn.fetchval("SELECT EXISTS(SELECT 1 FROM memory_draft_lease WHERE expires_at>now())"):
                return False
            # Capture the settings under short locks; changed settings fence the eventual result.
            saved = dict(await conn.fetch("SELECT key,value FROM system_settings WHERE key = ANY($1::text[]) "
                                          "ORDER BY key FOR SHARE", [model.SETTING_KEY, vault.SETTING_KEY]))
            raw = await memory_settings.stored(conn)
            if raw is None or raw.get("enabled") is not True:
                return False
            config, problem = await vault.load()
            if config is None or problem:
                return False
            try:
                settings = model.check_settings(raw, config)
            except (ValueError, OSError):
                return False
            row = await conn.fetchrow(
                "SELECT c.* FROM memory_candidates c WHERE destination='vault' AND state='drafting' "
                "AND NOT EXISTS (SELECT 1 FROM memory_draft_waits w JOIN memory_candidates b "
                "ON b.id=w.blocking_candidate_id WHERE w.candidate_id=c.id AND b.state=ANY($1::text[])) "
                "ORDER BY c.updated_at,c.id FOR UPDATE OF c SKIP LOCKED LIMIT 1", list(model.DRAFT_BLOCKING_STATES))
            if row is None:
                return False
            if await self._merge_exact(conn, row):
                return True
            now = datetime.now(timezone.utc)
            midnight = now.astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
            # Unknown-cost runs reserve a whole cap, so a missing price never opens an unlimited budget.
            spent = float(await conn.fetchval("SELECT COALESCE(sum(COALESCE(cost_usd,$2)),0) FROM llm_records "
                                               "WHERE run_kind='memory_draft' AND created_at >= $1",
                                               midnight, settings.draft_budget_usd))
            cap = min(settings.draft_budget_usd, settings.daily_budget_usd - spent)
            if cap >= MIN_RUN_USD:
                found = await store.get(row["id"], db=conn)
                sources = found["sources"]
                for source in sources:
                    if source["lead_id"] is not None:
                        lead = await conn.fetchrow("SELECT summary,detail FROM message_leads WHERE id=$1", source["lead_id"])
                        source["text"] = "\n".join(str(v or "") for v in lead.values()) if lead else ""
                    if source["journal_id"] is not None:
                        source["text"] = await conn.fetchval("SELECT summary FROM task_journal WHERE id=$1", source["journal_id"])
                opened, rejected = await self._related(conn, row, settings)
                await conn.execute("DELETE FROM memory_draft_waits WHERE candidate_id=$1", row["id"])
                await conn.execute("INSERT INTO memory_draft_lease(token,candidate_id,expires_at) "
                                   "VALUES($1,$2,now()+$3*interval '1 second') ON CONFLICT(id) DO UPDATE "
                                   "SET token=EXCLUDED.token,candidate_id=EXCLUDED.candidate_id,expires_at=EXCLUDED.expires_at",
                                   token, row["id"], _LEASE_SECONDS)
        if cap < MIN_RUN_USD:
            await self._budget_notice(settings, midnight.date().isoformat())
            return False
        try:
            draft = None
            try:
                async with asyncio.timeout(_CALL_SECONDS):
                    draft = await memory_draft.assemble(config, settings, {**dict(row), "sources": sources},
                                                       open_candidates=opened, rejected=rejected)
                    task_id = next((s["task_id"] for s in sources if s.get("task_id")), None)
                    done = await memory_draft_role.run(draft, config, settings, row["kind"],
                                                       parent=SpawnParent(task_id), runtime_root=self.runtime_root,
                                                       cap_usd=cap, claude_bin=self.claude_bin, backend=self.backend)
            except memory_draft.DraftError as error:
                done = error
            except TimeoutError:
                done = CallFailed(execution=Execution(None), failure=CallFailure.TIMEOUT, reason="draft deadline")
            async with pool.acquire() as conn, conn.transaction():
                # A reclaimed/expired lease, deletion or concurrent revision must never accept stale output.
                await conn.execute("SELECT pg_advisory_xact_lock($1::bigint)", _LOCK)
                if not await conn.fetchval("SELECT EXISTS(SELECT 1 FROM memory_draft_lease "
                                           "WHERE token=$1 AND expires_at>now())", token):
                    return True
                live = dict(await conn.fetch("SELECT key,value FROM system_settings WHERE key = ANY($1::text[]) "
                                             "ORDER BY key FOR SHARE", [model.SETTING_KEY, vault.SETTING_KEY]))
                current = await conn.fetchrow("SELECT * FROM memory_candidates WHERE id=$1 FOR UPDATE", row["id"])
                if current is None or current["state"] != "drafting" or current["updated_at"] != row["updated_at"] or live != saved:
                    return True
                if isinstance(done, memory_draft.DraftError):
                    await self._drop(conn, row["id"], done.reason)
                elif isinstance(done, CallFailed):
                    if isinstance(done.error, memory_draft.DraftError) and not done.error.retryable:
                        await self._drop(conn, row["id"], done.error.reason)
                    else:
                        await self._failed(conn, row["id"], done.failure.value)
                else:
                    await self._apply(conn, dict(current), done.value, draft, settings, done.record_id)
                    logger.info("memory draft candidate=%s decision=%s record=%s", model.format_id(row["id"]),
                                done.value.decision, done.record_id)
            return True
        finally:
            await pool.execute("DELETE FROM memory_draft_lease WHERE token=$1", token)

    async def _related(self, conn, row, settings):
        target = settings.targets[model.Kind(row["kind"])]
        folders = [str(Path(settings.root) / name) + "/" for name in target.folders]
        paths = [str(Path(settings.root) / name) for name in target.files]
        # Drafting rows of a kind sharing the same directories are relevant even before a note_path exists.
        kinds = [kind.value for kind, other in settings.targets.items() if other == target]
        related = "(kind=ANY($2::text[]) OR note_path=ANY($3::text[]) OR EXISTS " \
                  "(SELECT 1 FROM unnest($4::text[]) f WHERE starts_with(note_path,f)))"
        opened = await conn.fetch("SELECT id,statement,note_path,state,source_class,kind FROM memory_candidates "
                                   "WHERE id<>$1 AND destination='vault' AND state IN "
                                   "('pending','confirmed','writing','blocked','conflict') AND " + related +
                                   " ORDER BY id DESC LIMIT 10", row["id"], kinds, paths, folders)
        rejected = await conn.fetch("SELECT id,statement,note_path, "
                                     "(SELECT note FROM memory_candidate_events WHERE candidate_id=c.id "
                                     "AND action='reject' ORDER BY id DESC LIMIT 1) AS reason "
                                     "FROM memory_candidates c WHERE id<>$1 AND destination='vault' "
                                     "AND state='rejected' AND updated_at>=now()-interval '90 days' AND " + related +
                                     " ORDER BY id DESC LIMIT 10", row["id"], kinds, paths, folders)
        return [dict(r) for r in opened], [dict(r) for r in rejected]

    async def _drop(self, conn, cid, reason, *, note=None, merged_into=None):
        await store.transition(conn, cid, model.State.DROPPED, action=model.Action.DROP, actor=model.Actor.SYSTEM,
                               via=model.Via.SYSTEM, reason=reason, note=note, merged_into=merged_into,
                               now=datetime.now(timezone.utc))

    async def _failed(self, conn, cid, failure):
        # Attempts since the last new seed or explicit redraft survive restarts, apart from write attempts.
        await store._event(conn, cid, model.Action.DRAFT, model.Actor.SYSTEM, model.Via.SYSTEM,
                           note=f"draft_failed:{failure}")
        count = await conn.fetchval("SELECT count(*) FROM memory_candidate_events WHERE candidate_id=$1 "
                                    "AND action='draft' AND id>COALESCE((SELECT max(id) FROM memory_candidate_events "
                                    "WHERE candidate_id=$1 AND action IN ('redraft','propose')),0)", cid)
        if count >= 3:
            await self._drop(conn, cid, model.Reason.DRAFT_FAILED)
        logger.warning("memory draft candidate=%s failure=%s attempt=%s", model.format_id(cid), failure, count)

    async def _apply(self, conn, row, result, draft, settings, record_id):
        decision = result.decision.value
        if result.instructions_seen:
            await conn.execute("UPDATE memory_candidates SET flagged=true WHERE id=$1", row["id"])
        if decision in {"duplicate", "skip"}:
            ref = draft.refs[result.of]
            await self._drop(conn, row["id"], model.Reason.DUPLICATE if decision == "duplicate"
                             else model.Reason.REJECTED_BEFORE, note=ref.path or model.format_id(ref.candidate_id))
            return True
        target_id = draft.refs[result.revise or result.of].candidate_id if result.revise or decision == "same_as" else None
        target = None
        if target_id is not None:
            target = await conn.fetchrow("SELECT * FROM memory_candidates WHERE id=$1 FOR UPDATE", target_id)
            if target is None or target["state"] not in {"pending", "confirmed", "writing", "blocked", "conflict"}:
                # Its state changed since retrieval. Defer without accepting a stale model decision.
                await conn.execute("UPDATE memory_candidates SET updated_at=now() WHERE id=$1", row["id"])
                return False
            combined = model.least_trusted([row["source_class"], target["source_class"]])
            if not model.allowed(target["kind"], combined):
                await self._drop(conn, row["id"], model.Reason.DUPLICATE)
                return True
            if result.revise and model.State(target["state"]) in model.DRAFT_BLOCKING_STATES:
                await conn.execute("INSERT INTO memory_draft_waits(candidate_id,blocking_candidate_id,reason) "
                                   "VALUES($1,$2,$3) ON CONFLICT(candidate_id) DO UPDATE "
                                   "SET blocking_candidate_id=EXCLUDED.blocking_candidate_id,reason=EXCLUDED.reason",
                                   row["id"], target_id, model.DraftWaitReason.TARGET_BUSY.value)
                logger.info("memory draft candidate=%s waiting=%s target=%s", model.format_id(row["id"]),
                            model.DraftWaitReason.TARGET_BUSY.value, model.format_id(target_id))
                return True
            await self._merge_into(conn, row, target, flagged=bool(result.instructions_seen))
            # Free this candidate's old conflict path before the target's new revision takes it.
            await self._drop(conn, row["id"], model.Reason.SAME_AS, merged_into=target_id)
            if decision == "same_as":
                return True
        owner = dict(target) if target is not None else row
        owner["title"] = result.title
        await store.add_revision(conn, owner["id"], action=model.Action.REDRAFT if target is not None else model.Action.DRAFT,
                                 actor=model.Actor.SYSTEM, via=model.Via.SYSTEM,
                                 made_by=model.MadeBy.REDRAFT if target is not None else model.MadeBy.DRAFT,
                                 changes=result.changes, title=result.title, subject=memory_write.subject(settings, owner),
                                 diff_stats=result.diff_stats, note_path=result.note_path, pending_days=settings.pending_days,
                                 now=datetime.now(timezone.utc), large_removal=result.large_removal,
                                 flagged=bool(result.instructions_seen), hard_rule=result.hard_rule, record_id=record_id)
        return True

    async def _merge_exact(self, conn, row):
        target = await conn.fetchrow("SELECT * FROM memory_candidates WHERE destination='vault' AND kind=$1 "
                                     "AND fingerprint=$2 AND id<>$3 AND (state IN "
                                     "('pending','confirmed','writing','blocked','conflict') OR (state='drafting' AND id<$3)) "
                                     "ORDER BY id LIMIT 1 FOR UPDATE SKIP LOCKED", row["kind"], row["fingerprint"], row["id"])
        if target is None:
            return False
        if not model.allowed(target["kind"], model.least_trusted([row["source_class"], target["source_class"]])):
            await self._drop(conn, row["id"], model.Reason.DUPLICATE)
        else:
            await self._merge_into(conn, row, target)
            await self._drop(conn, row["id"], model.Reason.SAME_AS, merged_into=target["id"])
        return True

    async def _merge_into(self, conn, row, target, flagged=False):
        combined = model.least_trusted([row["source_class"], target["source_class"]])
        await conn.execute("INSERT INTO memory_candidate_sources(candidate_id,kind,manager_action_id,journal_id,lead_id,quote) "
                               "SELECT $2,s.kind,s.manager_action_id,s.journal_id,s.lead_id,s.quote FROM memory_candidate_sources s "
                               "WHERE s.candidate_id=$1 AND NOT EXISTS (SELECT 1 FROM memory_candidate_sources t "
                               "WHERE t.candidate_id=$2 AND t.kind=s.kind AND "
                               "t.manager_action_id IS NOT DISTINCT FROM s.manager_action_id AND "
                               "t.journal_id IS NOT DISTINCT FROM s.journal_id AND t.lead_id IS NOT DISTINCT FROM s.lead_id)",
                               row["id"], target["id"])
        await conn.execute("UPDATE memory_candidates SET source_class=$2,untrusted=untrusted OR $3, "
                               "flagged=flagged OR $4 WHERE id=$1",
                               target["id"], combined.value, row["untrusted"], flagged or row["flagged"])

    async def _budget_notice(self, settings, day):
        # Commit the claim before sending: a crash after delivery must not send it again.
        if not await system_settings.claim(_NOTICE, day, pool=self.db):
            return
        logger.warning("memory drafting paused: daily allowance exhausted")
        if self.sender is not None and settings.push_channel:
            from .operator_identity import operator_sender_id
            chat = await operator_sender_id(settings.push_channel)
            if chat:
                await self.sender(settings.push_channel, chat, "今天的记忆起草预算已用完，未完成的候选会在明天继续起草。")
