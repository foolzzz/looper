"""Transactional MR admission and recoverable execution, behind the rule switch.

Lock order: schedule (when present), source events by seq, chats by id, group
reads, admission, config, project, workflow/contract, candidate, MR target,
project day, task/start. Revocation takes task then execution/start only.
External verification always runs before entering these transactions.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import datetime, timezone
import json
import logging
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from ..db import connection, intent_rules as rules, intents as db_intents, tasks as db_tasks
from ..db import intent_rule_execution as ledger
from . import intent_rules as management, leads_model, projects
from .intent_rule_evaluation import (PreparedCandidate, PreparedMRBatch, RuleDecision, evaluate,
                                     prepare_message_candidates)
from .intent_rule_verifier import VerifiedFact, VerificationUnavailable, parse_target, verify
from .ops import ErrorCode, OpError

logger = logging.getLogger(__name__)
_INSTALLED = None


class _StaleWork(Exception):
    """The current lease or candidate has moved on; no work may be committed."""


class _AdmissionPaused(Exception):
    """The switch closed while verification ran; preserve the original work."""



def install(service):
    global _INSTALLED
    _INSTALLED = service


def installed():
    return _INSTALLED


def _now():
    return datetime.now(timezone.utc)


def _stamp(value):
    return datetime.fromisoformat(value) if isinstance(value, str) else value


def _json(value):
    return json.loads(value) if isinstance(value, str) else value


def _digest(value):
    return management._digest(value)


async def enabled(db=None):
    return await leads_model.rule_types_enabled(db or connection.get_pool())


@asynccontextmanager
async def candidate_gate(conn, *, intent_id=None, discovery_id=None):
    # The same session locks used by confirmation push and discovery delivery.
    if intent_id is not None:
        await conn.execute("SELECT pg_advisory_lock(741923,$1::int)", intent_id % 2147483647)
    else:
        await conn.execute("SELECT pg_advisory_lock(hashtextextended($1,0))",
                           f"looper.intent_rules.discovery:{discovery_id}")
    try:
        yield
    finally:
        if intent_id is not None:
            await conn.execute("SELECT pg_advisory_unlock(741923,$1::int)", intent_id % 2147483647)
        else:
            await conn.execute("SELECT pg_advisory_unlock(hashtextextended($1,0))",
                               f"looper.intent_rules.discovery:{discovery_id}")


async def _source_rows(db, seqs):
    return [dict(row) for row in await db.fetch(
        "SELECT e.*,c.kind AS chat_kind,s.user_open_id AS operator_id "
        "FROM source_events e JOIN source_chats c ON c.source_id=e.source_id AND c.chat_id=e.chat_id "
        "JOIN event_sources s ON s.id=e.source_id WHERE e.seq=ANY($1::bigint[]) ORDER BY e.seq", sorted(set(seqs)))]


async def _source_versions(db, source_id, seqs, *, captured_events=None):
    events = captured_events if captured_events is not None else await _source_rows(db, seqs)
    wanted = set(seqs)
    events = [row for row in events if row["seq"] in wanted]
    # Fingerprints retain no body, title, sender names or message content.
    chats = [dict(row) for row in await db.fetch(
        "SELECT c.source_id,c.chat_id,c.kind,c.inclusion,c.extraction,c.state,g.read_from "
        "FROM source_chats c LEFT JOIN intent_group_reads g ON g.source_id=c.source_id AND g.chat_id=c.chat_id "
        "WHERE c.source_id=$1 AND c.chat_id=ANY($2::text[]) ORDER BY c.chat_id",
        source_id, sorted({row["chat_id"] for row in events}))]
    return _digest({"events": events, "chats": chats})


async def _reference_snapshot(db, project_id, template_key):
    project = await db.fetchrow("SELECT * FROM projects WHERE id=$1", project_id)
    workflow = await db.fetchrow("SELECT * FROM workflows WHERE key=$1", template_key)
    contract = await db.fetchrow("SELECT * FROM intent_rule_template_contracts WHERE workflow_key=$1", template_key)
    return {"project": _digest(dict(project)) if project else None,
            "workflow": _digest(dict(workflow)) if workflow else None,
            "contract": _digest(dict(contract)) if contract else None}


async def _versions(db, config, candidate, ruleset, *, source_events=None):
    projects_snapshot = {}
    for rule in ruleset:
        if rule["action"] == "create_task":
            projects_snapshot[rule["id"]] = await _reference_snapshot(db, rule["project_id"], rule["template_key"])
    return {"generation": config["generation"], "references": projects_snapshot,
        "timezone": await db.fetchval("SELECT value FROM system_settings WHERE key='user.timezone'"),
        "source": await _source_versions(db, candidate.sources[0].source_id,
            [s.seq for s in candidate.sources], captured_events=source_events) if candidate.sources else None}


async def prepare_for_extraction(parsed, *, source_id, db=None):
    db = db or connection.get_pool()
    if not await enabled(db):
        return PreparedMRBatch((), 0), {}
    config = await rules.get_config(db)
    events = await _source_rows(db, [seq for item in parsed.items for seq in item["sources"]])
    # References precede the first external observation. A concurrent directory,
    # execution-config or template edit cannot become a new initial authorization.
    planned = await prepare_message_candidates(parsed, events, config["adapter"], source_id=source_id,
        generation=config["generation"], verify_facts=False)
    ruleset = await rules.read_rules(db)
    versions = {candidate.index: await _versions(db, config, candidate, ruleset, source_events=events)
                for candidate in planned.candidates}
    batch = await prepare_message_candidates(parsed, events, config["adapter"], source_id=source_id,
        generation=config["generation"])
    return batch, versions


async def _schedule_authorization(db, schedule_id):
    schedule = await db.fetchrow("SELECT id,enabled,kind,project_id,cron,timezone,requirement,notify,"
        "notify_channel,notify_chat,template_key,template_inputs FROM schedules WHERE id=$1", schedule_id)
    subscription = await db.fetchrow("SELECT source_id,chat_kinds,include_chats,exclude_chats,sender_kinds,"
        "exclude_mentions_me,include_history,output_kind,rules,config_epoch,target_confirmed_at,"
        "target_confirmed_version,\"window\",delivery_policy,check_items FROM subscriptions WHERE schedule_id=$1", schedule_id)
    if schedule is None or not schedule["enabled"] or schedule["kind"] != "subscription" or subscription is None or subscription["output_kind"] != "intents":
        return None
    # Authorization only: cursors and run/health timestamps change on every
    # legitimate extraction and must not invalidate queued work.
    return {"id": schedule_id, "fingerprint": _digest({"schedule": dict(schedule), "subscription": dict(subscription)})}


async def register_candidate(conn, intent_id, candidate, versions):
    # The extraction finisher already owns the schedule lock before source locks.
    # Explicit preparation of an old manual candidate takes it before calling us.
    versions = dict(versions)
    schedule_id = await conn.fetchval("SELECT schedule_id FROM intents WHERE id=$1", intent_id)
    if schedule_id is not None:
        versions["schedule"] = await _schedule_authorization(conn, schedule_id) or {"id": schedule_id, "fingerprint": None}
    fact = candidate.first_fact
    await ledger.enqueue_work(conn, intent_id=intent_id, generation=candidate.generation,
        source_fingerprint=candidate.source_fingerprint,
        prepared_target={"candidate": candidate.to_dict(), "versions": versions},
        first_fact=fact.to_dict() if fact else None,
        first_verified_at=_stamp(getattr(fact, "verified_at", None)),
        first_verification_status="verified" if isinstance(fact, VerifiedFact) else "unavailable",
        first_reason=getattr(fact, "reason", None))


def _identity(fact, adapter):
    if not isinstance(fact, VerifiedFact) or adapter is None or fact.target.system_id != adapter["system_id"]:
        return None
    raw = fact.to_dict()
    body = raw.get("facts", raw)
    if not body.get("exists"):
        return None
    return {"system_id": adapter["system_id"], "repository_id": body["repository"],
        "mr_id": body["mr_id"], "revision": body["revision"], "canonical_url": body["canonical_url"]}


def _identity_failure(fact):
    if isinstance(fact, VerificationUnavailable):
        return fact.reason
    if isinstance(fact, VerifiedFact) and not fact.exists:
        return "mr_missing"
    return "cannot_decide"


def _fallback(decision, reason):
    # A matched rule can be denied at admission. Keep that rule/version for
    # attribution while recording the actual confirmation action and reason.
    matched = decision.rule_id is not None
    return replace(decision, action="push_for_confirmation", reason=reason,
                   selection="matched" if matched else "fallback", project_id=None, template_key=None)


async def _audit(conn, work, candidate, decision, identity, verified_at):
    return await rules.append_hit(conn, {"evaluation_key": work.get("evaluation_key") or f"rule-work:{work['id']}",
        "intent_id": work["intent_id"], "discovery_id": work["discovery_id"],
        "generation": work["generation"], "rule_id": decision.rule_id, "rule_version": decision.rule_version,
        "selection": decision.selection, "action": decision.action, "reason": decision.reason,
        "verified_at": verified_at, "mr_identity": identity},
        conditions=[item.to_dict() if hasattr(item, "to_dict") else item for item in decision.conditions],
        sources=[item.seq for item in candidate.sources], attempts=decision.attempts)


async def _finish_decision(conn, work, candidate, decision, identity=None, verified_at=None):
    hit_id = await _audit(conn, work, candidate, decision, identity, verified_at)
    if not await ledger.decide_work(conn, work["id"], decision=decision.action, reason=decision.reason,
            hit_id=hit_id, lease_token=work.get("lease_token"), now=_now()):
        raise _StaleWork
    if work["discovery_id"] is not None:
        state = "recorded" if decision.action == "record_only" else "confirmation"
        await conn.execute("UPDATE intent_rule_discoveries SET state=$2,handled_at=now() WHERE id=$1 AND state='pending'",
                           work["discovery_id"], state)
        if state == "confirmation":
            config = await rules.get_config(conn)
            discovery = await ledger.get_discovery(conn, work["discovery_id"])
            target = _discovery_notice_target(config, discovery)
            await ledger.enqueue_notice(conn, kind="discovery_confirmation", discovery_id=work["discovery_id"],
                dedupe_key=f"rule-discovery:{work['discovery_id']}", channel_id=target[0] if target else None,
                chat=target[1] if target else None, reason=decision.reason)
    return {"state": "confirmation" if decision.action == "push_for_confirmation" else "recorded", "hit_id": hit_id}


def _discovery_notice_target(config, discovery):
    if discovery is None:
        return None
    source = next((source for source in config["discovery_sources"]
                   if source["id"] == discovery["discovery_source_id"] and source["enabled"]), None)
    target = source.get("private_target") if source else None
    return (target["channel_id"], target["chat"]) if target else None


async def _message_notice_target(db, work):
    if work["intent_id"] is None:
        return None
    from . import intent_push
    schedule_id = await db.fetchval("SELECT schedule_id FROM intents WHERE id=$1", work["intent_id"])
    return await intent_push.target_for(schedule_id) if schedule_id else None


async def _notice_target(conn, work, config, versions):
    if work["discovery_id"] is not None:
        return _discovery_notice_target(config, await ledger.get_discovery(conn, work["discovery_id"]))
    return versions.get("notice_target")


async def _lock_sources(conn, candidate, work):
    if not candidate.sources:
        return True
    from . import extraction
    source_id = candidate.sources[0].source_id
    seqs = [source.seq for source in candidate.sources]
    # Locks are acquired by seq, then chat, then group-read before admission.
    events, revoked = await extraction._lock_sources(conn, source_id, seqs)
    if set(events) != set(seqs) or revoked:
        return False
    source_rows = await _source_rows(conn, seqs)
    group_seqs = [row["seq"] for row in source_rows if row["chat_kind"] != "p2p"
                  and row["sender_id"] != row["operator_id"] and row["operator_id"] not in (row["mention_ids"] or [])]
    lost = await extraction._lost_group_seqs(conn, source_id, group_seqs, events) if group_seqs else set()
    if lost:
        return False
    stored = await conn.fetchval("SELECT array_agg(event_seq ORDER BY event_seq) FROM intent_sources WHERE intent_id=$1", work["intent_id"])
    return sorted(seqs) == (stored or [])


async def claim_and_create(conn, candidate, decision, fresh_facts, versions, *, work, manual=False,
                           operator_key="operator", project_id=None, template_key=None, via="dashboard"):
    """One transaction owns validation, revision, task, quota, audit and outboxes.

    The caller holds the candidate session gate; it must not hold any row locks
    from the legacy build path before entering this function.
    """
    schedule_ok = True
    schedule_snapshot = versions.get("schedule")
    if schedule_snapshot is not None:
        await conn.execute("SELECT id FROM schedules WHERE id=$1 FOR NO KEY UPDATE", schedule_snapshot["id"])
        schedule_ok = await _schedule_authorization(conn, schedule_snapshot["id"]) == schedule_snapshot
    source_ok = await _lock_sources(conn, candidate, work)
    await rules.lock_admission(conn)
    config = await rules.lock_config(conn)
    flags = {row["key"]: row["value"] for row in await conn.fetch(
        "SELECT key,value FROM system_settings WHERE key=ANY($1::text[])",
        ["intents.rules.enabled", "intents.enabled", "delivery.enabled", "user.timezone"])}
    reason = None
    for key, code in (("intents.rules.enabled", "switch_off"), ("intents.enabled", "intents_off"), ("delivery.enabled", "delivery_off")):
        if flags.get(key) != "true" and (not manual or key == "intents.enabled"):
            reason = code
            break
    if not manual and flags.get("intents.rules.enabled") != "true":
        raise _AdmissionPaused
    if config["generation"] != work["generation"] or versions.get("timezone") != flags.get("user.timezone"):
        reason = "config_changed"
    if not schedule_ok or (candidate.sources and (not source_ok or versions.get("source") != await _source_versions(
            conn, candidate.sources[0].source_id, [s.seq for s in candidate.sources]))):
        reason = "source_revoked"
    if work["discovery_id"] is not None:
        discovery = await ledger.get_discovery(conn, work["discovery_id"])
        source = next((s for s in config["discovery_sources"] if s["id"] == discovery["discovery_source_id"]
            and s["enabled"] and s["system_id"] == discovery["system_id"]), None)
        if source is None:
            reason = "source_revoked"
        else:
            operator_key = source.get("operator_key", "operator")
    project_id, template_key = project_id or decision.project_id, template_key or decision.template_key
    project = workflow = contract = None
    if project_id:
        project = await conn.fetchrow("SELECT * FROM projects WHERE id=$1 FOR SHARE", project_id)
    if template_key:
        workflow = await conn.fetchrow("SELECT * FROM workflows WHERE key=$1 FOR SHARE", template_key)
        contract = await conn.fetchrow("SELECT * FROM intent_rule_template_contracts WHERE workflow_key=$1 FOR SHARE", template_key)
    if decision.action == "create_task" or manual:
        if project is None or not (project["work_dir"] or "").strip():
            reason = "project_changed"
        if workflow is None or contract is None or contract["workflow_digest"] != _digest(_json(workflow["definition"])):
            reason = "template_changed"
        if manual:
            expected = versions["manual_references"]
            if project and expected["project"] != _digest(dict(project)):
                reason = "project_changed"
            if workflow and (expected["workflow"] != _digest(dict(workflow)) or expected["contract"] != _digest(dict(contract))):
                reason = "template_changed"
        if not manual:
            current = next((r for r in await rules.read_rules(conn) if r["id"] == decision.rule_id and r["version"] == decision.rule_version and r["enabled"]), None)
            if current is None:
                reason = "config_changed"
            expected = versions.get("references", {}).get(decision.rule_id, {})
            if project and expected.get("project") != _digest(dict(project)):
                reason = "project_changed"
            if workflow and (expected.get("workflow") != _digest(dict(workflow)) or expected.get("contract") != _digest(dict(contract))):
                reason = "template_changed"
    if work["intent_id"] is not None:
        row = await db_intents.locked(conn, work["intent_id"])
    else:
        row = await ledger.get_discovery(conn, work["discovery_id"], lock=True)
    if row is None:
        raise _StaleWork
    current_work = await conn.fetchrow("SELECT state,lease_token,lease_until FROM intent_rule_work WHERE id=$1 FOR UPDATE", work["id"])
    if current_work is None or (not manual and (current_work["state"] != "evaluating"
            or str(current_work["lease_token"]) != str(work.get("lease_token"))
            or current_work["lease_until"] is None or current_work["lease_until"] <= _now())):
        raise _StaleWork
    if manual and row["state"] == "tasked":
        execution = await ledger.get_execution(conn, task_id=row["task_id"])
        if execution is None:
            raise OpError(ErrorCode.CONFLICT, "task has no shared revision execution")
        return {"state": "tasked", "task_id": row["task_id"],
                "execution": await ledger.execution_view(conn, execution["id"]), "existing": True}
    if row["state"] not in (("pending", "expired") if manual and work["intent_id"] else ("pending", "confirmation", "recorded") if manual else ("pending",)):
        raise OpError(ErrorCode.CONFLICT, "candidate was already handled")
    if manual and versions.get("card_press") is not None:
        from . import intent_cards
        await intent_cards.claim(conn, row, versions["card_press"])
    if row.get("flagged") and not manual:
        reason = "flagged"
    identity = _identity(fresh_facts, config["adapter"])
    first = _identity(candidate.first_fact, config["adapter"])
    if (decision.action == "create_task" or manual) and (identity is None or first is None):
        reason = _identity_failure(fresh_facts if identity is None else candidate.first_fact)
    elif identity and first and identity != first:
        reason = "revision_changed"
    if identity and fresh_facts.to_dict().get("facts", fresh_facts.to_dict()).get("state") != "open":
        reason = "mr_not_open"
    verified_at = getattr(fresh_facts, "verified_at", None)
    if reason:
        if manual:
            raise OpError(ErrorCode.CONFLICT, reason)
        return await _finish_decision(conn, work, candidate, _fallback(decision, reason), identity, verified_at)
    if decision.action != "create_task" and not manual:
        return await _finish_decision(conn, work, candidate, decision, identity, verified_at)
    target = await ledger.lock_mr_target(conn, identity)
    existing = await ledger.get_claim(conn, identity)
    if target["blocked_reason"] and not existing:
        if manual:
            raise OpError(ErrorCode.CONFLICT, "unknown_legacy_revision")
        return await _finish_decision(conn, work, candidate, _fallback(decision, "unknown_legacy_revision"), identity, verified_at)
    if existing:
        execution = await ledger.get_execution(conn, task_id=existing["task_id"])
        if execution is None:
            raise ValueError("missing execution for revision claim")
        result_decision = replace(decision, reason="deduped")
        hit_id = await _audit(conn, work, candidate, result_decision, identity, verified_at)
        await ledger.link_hit_execution(conn, hit_id, execution["id"])
        task_id = existing["task_id"]
    else:
        quota_day = None
        zone = flags.get("user.timezone")
        if zone:
            tz = ZoneInfo(zone)
        else:
            from .turn_context import server_zone
            tz = server_zone()
            zone = str(tz)
        if not manual:
            quota_day = _now().astimezone(tz).date()
            cap = project["auto_task_daily_cap"]
            if not cap or not await ledger.consume_project_day(conn, operator_key=operator_key, project_id=project_id,
                    local_day=quota_day, timezone=zone, cap=cap):
                return await _finish_decision(conn, work, candidate, _fallback(decision, "daily_cap"), identity, verified_at)
        created = await projects.create_task_core({"requirement": f"按本项目评审流程评审此 MR：{identity['canonical_url']}",
            "title": f"MR 评审 {identity['mr_id']}", "project_id": project_id, "workflow": template_key,
            "config": {"intent_rule_bound": True}},
            task_fields={"manager_id": "manager"}, conn=conn)
        task_id = created["task_id"]
        await db_tasks.mark_untrusted(task_id, "intent", pool=conn)
        await ledger.insert_claim(conn, identity, task_id)
        execution = await ledger.insert_execution(conn, task_id=task_id, mode="manual" if manual else "auto", mr_key=identity,
            quota_day=quota_day, quota_operator=operator_key if quota_day else None,
            quota_project=project_id if quota_day else None, quota_units=0 if manual else 1)
        hit_id = await _audit(conn, work, candidate, decision, identity, verified_at)
        await ledger.link_hit_execution(conn, hit_id, execution["id"])
        await ledger.enqueue_start(conn, task_id, execution["id"])
        notice_target = await _notice_target(conn, work, config, versions)
        await ledger.enqueue_notice(conn, kind="created", execution_id=execution["id"],
            dedupe_key=f"rule-created:{execution['id']}", channel_id=notice_target[0] if notice_target else None,
            chat=notice_target[1] if notice_target else None, reason=decision.reason)
    if work["intent_id"] is not None:
        await db_intents.transition(conn, work["intent_id"], "tasked", via="system" if not manual else via, now=_now(), task_id=task_id)
    else:
        await conn.execute("UPDATE intent_rule_discoveries SET state='tasked',task_id=$2,handled_at=now() WHERE id=$1", work["discovery_id"], task_id)
    if manual:
        await conn.execute("UPDATE intent_rule_work SET state='decided',decision='create_task',reason=$2,hit_id=$3,decided_at=now(),lease_token=NULL,lease_until=NULL WHERE id=$1",work["id"],"deduped" if existing else "matched",hit_id)
    else:
        if not await ledger.decide_work(conn, work["id"], decision="create_task", reason="deduped" if existing else "matched", hit_id=hit_id,
                                        lease_token=work.get("lease_token"), now=_now()):
            raise _StaleWork
    return {"state": "tasked", "task_id": task_id, "execution": await ledger.execution_view(conn, execution["id"]), "existing": bool(existing)}


class IntentRuleService:
    def __init__(self, platform=None, registry=None, *, db=None, verifier=verify, drain=None, dashboard_url=""):
        self.platform, self.registry, self.db, self.verifier, self.drain = platform, registry, db, verifier, drain
        self.dashboard_url = dashboard_url
        self._task = None
        self._stop = asyncio.Event()

    def pool(self):
        return self.db or connection.get_pool()

    async def start(self):
        self._stop.clear()
        self._task = asyncio.create_task(self._loop(), name="intent-rules")

    async def stop(self):
        self._stop.set()
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    async def _loop(self):
        while not self._stop.is_set():
            try:
                await self.run_once()
            except Exception:
                logger.error("intent_rule_service_failed code=execution_unavailable")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=2)
            except TimeoutError:
                pass

    async def run_once(self):
        if self.drain is not None and self.drain.draining:
            return
        pool = self.pool()
        # Previously committed tasks remain recoverable even with the rule switch off.
        if self.platform is not None:
            await self._starts()
            await self._revocations()
        if not await enabled(pool):
            return
        for _ in range(20):
            if (self.drain is not None and self.drain.draining) or not await enabled(pool):
                break
            async with pool.acquire() as conn, conn.transaction():
                work_items = await ledger.claim_work(conn, now=_now(), limit=1)
            if not work_items:
                break
            await self._process(work_items[0])
        if self.registry is not None:
            from .intent_rule_delivery import NoticeDispatcher
            await NoticeDispatcher(self.registry, drain=self.drain, dashboard_url=self.dashboard_url).run_once()

    async def _process(self, work):
        pool = self.pool()
        if (self.drain is not None and self.drain.draining) or not await enabled(pool):
            return
        current = await ledger.get_work(pool, work_id=work["id"])
        if (current is None or current["state"] != "evaluating"
                or current["lease_token"] != work.get("lease_token")
                or current["lease_until"] is None or current["lease_until"] <= _now()):
            return
        prepared = work["prepared_target"]
        candidate = PreparedCandidate.from_mapping(prepared["candidate"])
        config = await rules.get_config(pool)
        if config["generation"] != work["generation"]:
            decision = RuleDecision("push_for_confirmation", "config_changed")
        else:
            decision = evaluate(candidate, await rules.read_rules(pool))
        fresh = candidate.first_fact
        if decision.action == "create_task":
            fresh = await self.verifier(candidate.target.canonical_url, config["adapter"])
            if isinstance(fresh, VerificationUnavailable):
                decision = _fallback(decision, fresh.reason)
        if self.drain is not None and self.drain.draining:
            return
        versions = {**prepared["versions"], "notice_target": await _message_notice_target(pool, work)}
        async with pool.acquire() as conn, candidate_gate(conn, intent_id=work["intent_id"], discovery_id=work["discovery_id"]):
            try:
                async with conn.transaction():
                    await claim_and_create(conn, candidate, decision, fresh, versions, work=work)
                    if candidate.type_key == "mr_review" and candidate.target is not None and decision.reason != "config_changed":
                        await self._health(conn, config, fresh, work=work, versions=versions)
            except (_StaleWork, _AdmissionPaused):
                return
            except Exception:
                logger.error("intent_rule_execution_failed work=%s code=audit_unavailable", work["id"])
                try:
                    async with conn.transaction():
                        # The same admission boundary prevents an off switch from
                        # acquiring a new audit/decision side effect after rollback.
                        await rules.lock_admission(conn)
                        if not await enabled(conn):
                            return
                        decided = await ledger.decide_work(conn, work["id"], decision="push_for_confirmation", reason="audit_unavailable",
                            hit_id=None, lease_token=work["lease_token"], now=_now())
                        if decided and work["discovery_id"] is not None:
                            await conn.execute("UPDATE intent_rule_discoveries SET state='confirmation',handled_at=now() WHERE id=$1 AND state='pending'", work["discovery_id"])
                            current_config = await rules.get_config(conn)
                            target = await _notice_target(conn, work, current_config, versions)
                            await ledger.enqueue_notice(conn, kind="discovery_confirmation", discovery_id=work["discovery_id"],
                                dedupe_key=f"rule-discovery:{work['discovery_id']}", reason="audit_unavailable",
                                channel_id=target[0] if target else None, chat=target[1] if target else None)
                except Exception:
                    logger.error("intent_rule_fallback_failed work=%s code=audit_unavailable", work["id"])

    async def _health(self, conn, config, fact, *, work, versions):
        if config["adapter"] is None:
            return
        notice = await ledger.record_verification(conn, config["adapter"]["system_id"],
            available=isinstance(fact, VerifiedFact), now=_now())
        if notice:
            target = await _notice_target(conn, work, config, versions)
            if target:
                await conn.execute("UPDATE intent_rule_notices SET channel_id=$2,chat=$3 WHERE id=$1",
                                   notice["id"], *target)

    async def submit_discovery(self, discovery_source_id, source_ref, system_id, mr_url, *, principal=None):
        management._operator(principal)
        pool = self.pool()
        if not await enabled(pool):
            raise OpError(ErrorCode.CONFLICT, "intent rules are off")
        config = await rules.get_config(pool)
        source = next((s for s in config["discovery_sources"] if s["id"] == discovery_source_id and s["enabled"]
            and s["system_id"] == system_id and s["migration_state"] != "unconfigured"), None)
        if source is None:
            raise OpError(ErrorCode.FORBIDDEN, "discovery source is not authorized")
        target = parse_target(mr_url, config["adapter"])
        if target is None or system_id != config["adapter"]["system_id"]:
            raise OpError(ErrorCode.INVALID, "invalid MR target")
        previous = await pool.fetchrow("SELECT id,state,task_id FROM intent_rule_discoveries WHERE discovery_source_id=$1 AND source_ref=$2", discovery_source_id, source_ref)
        if previous:
            return {"discovery_id": previous["id"], "state": previous["state"], "task_id": previous["task_id"]}
        candidate = PreparedCandidate(index=0, type_key="mr_review", sources=(), target=target,
            first_fact=VerificationUnavailable("cannot_decide"), generation=config["generation"],
            source_fingerprint=_digest({"source": discovery_source_id, "ref": source_ref, "url": target.canonical_url}))
        versions = await _versions(pool, config, candidate, await rules.read_rules(pool))
        first = await self.verifier(target.canonical_url, config["adapter"])
        candidate = replace(candidate, first_fact=first)
        async with pool.acquire() as conn, conn.transaction():
            await rules.lock_admission(conn)
            current = await rules.get_config(conn)
            if current["generation"] != config["generation"] or not await enabled(conn):
                raise OpError(ErrorCode.CONFLICT, "configuration changed; submit again")
            discovery = await ledger.create_discovery(conn, discovery_source_id=discovery_source_id, source_ref=source_ref,
                system_id=system_id, mr_url=target.canonical_url)
            await ledger.enqueue_work(conn, discovery_id=discovery["id"], generation=config["generation"],
                source_fingerprint=candidate.source_fingerprint, prepared_target={"candidate":candidate.to_dict(),"versions":versions},
                first_fact=first.to_dict(), first_verified_at=_stamp(getattr(first,"verified_at",None)),
                first_verification_status="verified" if isinstance(first, VerifiedFact) else "unavailable", first_reason=getattr(first,"reason",None))
        return {"discovery_id":discovery["id"],"state":discovery["state"],"task_id":discovery["task_id"]}

    async def _starts(self):
        from .message import AgentAddress
        pool = self.pool()
        async with pool.acquire() as conn, conn.transaction():
            items = await ledger.claim_starts(conn, now=_now())
        for item in items:
            try:
                async with pool.acquire() as conn, conn.transaction():
                    task = await conn.fetchrow("SELECT status,manager_id FROM tasks WHERE id=$1 FOR UPDATE", item["task_id"])
                    execution = await ledger.get_execution(conn, item["execution_id"], lock=True)
                    current = await conn.fetchrow("SELECT state,lease_token,lease_until FROM intent_rule_task_starts WHERE task_id=$1 FOR UPDATE", item["task_id"])
                    if (current is None or current["state"] != "claimed"
                            or str(current["lease_token"]) != str(item["lease_token"])
                            or current["lease_until"] is None or current["lease_until"] <= _now()):
                        continue
                    if execution is None or execution["revoke_state"] != "none" or task is None or task["status"] in ("cancelled", "stopped"):
                        await ledger.finish_start(conn, item["task_id"], lease_token=item["lease_token"], state="cancelled", now=_now())
                        continue
                    if task["status"] == "init":
                        await projects.start_ready_task(item["task_id"], self.platform.bus, AgentAddress("manager",task["manager_id"]), pool=conn)
                    status = await conn.fetchval("SELECT status FROM tasks WHERE id=$1", item["task_id"])
                    if status != "init":
                        await ledger.finish_start(conn, item["task_id"], lease_token=item["lease_token"], state="acknowledged", now=_now())
                    else:
                        # Publishing is a request. Only TaskRouter's atomic
                        # init->pending transition acknowledges the start.
                        released = await ledger.finish_start(conn, item["task_id"], lease_token=item["lease_token"], state="pending", now=_now())
                        if released:
                            await conn.execute("UPDATE intent_rule_executions SET start_state='requested',last_error_code=NULL,updated_at=now() WHERE id=$1", execution["id"])
            except Exception:
                async with pool.acquire() as conn, conn.transaction():
                    await ledger.finish_start(conn, item["task_id"], lease_token=item["lease_token"], state="pending", error_code="start_failed", now=_now())

    async def _revocations(self):
        for row in await self.pool().fetch("SELECT id FROM intent_rule_executions WHERE revoke_state='requested' ORDER BY id LIMIT 20"):
            await _continue_revoke(row["id"], platform=self.platform, db=self.pool())


async def submit_discovery(*args, principal=None, **kwargs):
    service = installed() or IntentRuleService()
    return await service.submit_discovery(*args, principal=principal, **kwargs)


async def discovery_get(discovery_id, *, principal=None, db=None):
    management._operator(principal)
    db = db or connection.get_pool()
    row = await ledger.get_discovery(db, discovery_id)
    if row is None:
        raise OpError(ErrorCode.NOT_FOUND, "discovery not found")
    work = await ledger.get_work(db, discovery_id=discovery_id)
    return {"discovery": {key: row[key] for key in ("id","source_ref","system_id","mr_url","state","task_id")},
            "hit_id": work["hit_id"] if work else None,
            "execution": await ledger.execution_view(db, hit_id=work["hit_id"]) if work and work["hit_id"] else None}


async def discovery_reject(discovery_id, *, principal=None, db=None):
    management._operator(principal)
    async with (db or connection.get_pool()).acquire() as conn, candidate_gate(conn, discovery_id=discovery_id), conn.transaction():
        row = await ledger.get_discovery(conn, discovery_id, lock=True)
        if row is None:
            raise OpError(ErrorCode.NOT_FOUND, "discovery not found")
        if row["state"] == "tasked":
            raise OpError(ErrorCode.CONFLICT, "discovery already has a task")
        await conn.execute("UPDATE intent_rule_discoveries SET state='rejected',handled_at=now() WHERE id=$1", discovery_id)
    return {"discovery_id": discovery_id, "state": "rejected"}


async def _manual(work, *, project_id, template_key, db, platform=None, via="dashboard", card_press=None):
    pool = db or connection.get_pool()
    config = await rules.get_config(pool)
    candidate = PreparedCandidate.from_mapping(work["prepared_target"]["candidate"])
    service = installed() or IntentRuleService(db=pool)
    versions = await _versions(pool, config, candidate, await rules.read_rules(pool))
    versions["manual_references"] = await _reference_snapshot(pool, project_id, template_key)
    if "schedule" in work["prepared_target"]["versions"]:
        versions["schedule"] = work["prepared_target"]["versions"]["schedule"]
    original_source = work["prepared_target"]["versions"].get("source")
    if candidate.sources and versions["source"] != original_source:
        raise OpError(ErrorCode.CONFLICT, "source_revoked")
    fresh = await service.verifier(candidate.target.canonical_url, config["adapter"]) if candidate.target else VerificationUnavailable("ambiguous_target")
    if _identity(fresh, config["adapter"]) is None:
        # A stable identity is mandatory even for an explicit manual action.
        raise OpError(ErrorCode.CONFLICT, _identity_failure(fresh))
    # Manual authorization does not reuse old reviewer/DM predicates or automatic quota.
    candidate = replace(candidate, first_fact=fresh)
    decision = RuleDecision(action="create_task", reason="matched", selection="manual", rule_id=None,
        rule_version=None, project_id=project_id, template_key=template_key, conditions=())
    work = {**work, "generation": config["generation"], "lease_token": None,
            "evaluation_key": f"rule-manual:{work['id']}:{uuid4()}"}
    versions["notice_target"] = await _message_notice_target(pool, work)
    versions["card_press"] = card_press
    async with pool.acquire() as conn, candidate_gate(conn, intent_id=work["intent_id"], discovery_id=work["discovery_id"]), conn.transaction():
        result = await claim_and_create(conn, candidate, decision, fresh, versions, work=work, manual=True,
                                         project_id=project_id, template_key=template_key, via=via)
        if card_press is not None:
            from . import intent_cards
            await intent_cards.resolve(conn, work["intent_id"], intent_cards.Choice.BUILD, card_press)
    if platform:
        # Recovery always uses the committed start outbox and the original task id.
        await IntentRuleService(platform=platform, db=pool)._starts()
    return result


async def discovery_build(discovery_id, project_id, template_key, *, principal=None, db=None, platform=None):
    management._operator(principal)
    pool = db or connection.get_pool()
    discovery = await ledger.get_discovery(pool, discovery_id)
    if discovery is not None and discovery["state"] == "tasked":
        execution = await ledger.execution_view(pool, task_id=discovery["task_id"])
        if execution is None:
            raise OpError(ErrorCode.CONFLICT, "task has no shared revision execution")
        return {"task_id": discovery["task_id"], "execution": execution}
    work = await ledger.get_work(pool, discovery_id=discovery_id)
    if work is None:
        raise OpError(ErrorCode.NOT_FOUND, "discovery work not found")
    result = await _manual(work, project_id=project_id, template_key=template_key, db=pool, platform=platform)
    return {"task_id": result["task_id"], "execution": result["execution"]}


async def manual_build(intent_id, *, via, origin, candidate, project_id, workflow_key, platform, db, card_press=None):
    from . import intents
    row = await db.fetchrow("SELECT * FROM intents WHERE id=$1", intent_id)
    if row is None:
        raise db_intents.IntentRefused(code="not_found")
    if row["state"] == "tasked":
        await intents.resume_start(row, platform=platform, db=db)
        raise db_intents.IntentRefused(state="tasked", row=row)
    choices = await intents.candidates(intent_id, db=db)
    if candidate is not None:
        if not 1 <= candidate <= len(choices):
            raise intents.CandidateOutOfRange(candidate)
        project_id = choices[candidate - 1]["id"]
    project_id = project_id or row["project_id"]
    if project_id is None:
        raise intents.NeedsProject(choices)
    project = await db.fetchrow("SELECT * FROM projects WHERE id=$1", project_id)
    if project is None:
        raise intents.CandidateDeleted(candidate or 1)
    if not project["work_dir"]:
        raise intents.NeedsDirectory(dict(project))
    template_key = workflow_key or row["workflow_key"] or project["default_workflow"]
    if not template_key:
        raise db_intents.IntentRefused("请选择声明支持已核验 MR 的工作流。")
    work = await ledger.get_work(db, intent_id=intent_id)
    if work is None:
        # No automatic backfill. An explicit manual action can prepare just its own real sources.
        from types import SimpleNamespace
        seqs = await db.fetch("SELECT event_seq FROM intent_sources WHERE intent_id=$1 ORDER BY event_seq", intent_id)
        parsed = SimpleNamespace(items=[{"type":"mr_review","sources":[s["event_seq"] for s in seqs]}])
        batch, snapshots = await prepare_for_extraction(parsed, source_id=row["source_id"], db=db)
        async with db.acquire() as conn, conn.transaction():
            if row["schedule_id"]:
                await conn.execute("SELECT id FROM schedules WHERE id=$1 FOR NO KEY UPDATE", row["schedule_id"])
            await register_candidate(conn, intent_id, batch.candidates[0], snapshots[0])
        work = await ledger.get_work(db, intent_id=intent_id)
    try:
        result = await _manual(work, project_id=project_id, template_key=template_key, db=db, platform=platform,
                               via=via, card_press=card_press)
    except OpError as exc:
        raise db_intents.IntentRefused(str(exc)) from None
    await intents._final(intent_id)
    return {"intent": (await intents.get(intent_id, db=db))["intent"], "task_id": result["task_id"]}


async def _continue_revoke(execution_id, *, platform=None, db=None):
    pool = db or connection.get_pool()
    execution = await ledger.get_execution(pool, execution_id)
    if execution is None:
        raise OpError(ErrorCode.NOT_FOUND, "execution not found")
    task_id = execution["task_id"]
    async with pool.acquire() as conn, conn.transaction():
        task = await conn.fetchrow("SELECT * FROM tasks WHERE id=$1 FOR UPDATE", task_id)
        execution = await ledger.get_execution(conn, execution_id, lock=True)
        if execution["revoke_state"] in ("stopped", "already_terminal"):
            state = execution["revoke_state"]
        elif task is None or task["status"] in ("completed", "cancelled", "failed", "stopped"):
            state = "already_terminal"
        elif task["status"] == "init":
            await conn.execute("UPDATE tasks SET status='cancelled',completed_at=now(),updated_at=now() WHERE id=$1", task_id)
            from . import attempts
            await attempts.record_outcome(task_id, attempts.TaskOutcome.CANCELLED, attempts.StopCause.USER_CANCEL, pool=conn)
            await conn.execute("UPDATE intent_rule_task_starts SET state='cancelled',lease_token=NULL,lease_until=NULL,updated_at=now() WHERE task_id=$1", task_id)
            state = "stopped"
        else:
            state = "requested"
        await conn.execute("UPDATE intent_rule_executions SET revoke_state=$2, start_state=CASE WHEN $2='stopped' AND start_state!='started' THEN 'cancelled' ELSE start_state END,updated_at=now() WHERE id=$1", execution_id, state)
    if state == "requested" and platform is not None:
        from .message import AgentAddress
        try:
            await projects.control_task(task_id, "stop", platform.bus, AgentAddress("manager", task["manager_id"]))
            status = await pool.fetchval("SELECT status FROM tasks WHERE id=$1", task_id)
            if status in ("cancelled", "stopped"):
                state = "stopped"
            elif status in ("completed", "cancelled", "failed", "stopped"):
                state = "already_terminal"
        except Exception:
            state = "failed"
        await pool.execute("UPDATE intent_rule_executions SET revoke_state=$2,last_error_code=CASE WHEN $2='failed' THEN 'revoke_failed' ELSE NULL END,updated_at=now() WHERE id=$1", execution_id, state)
    return {"execution_id": execution_id, "task_id": task_id, "state": state,
            "reason": "revoke_failed" if state == "failed" else "already_terminal" if state == "already_terminal" else None}


async def revoke(execution_id, *, principal=None, platform=None, db=None):
    management._operator(principal)
    return await _continue_revoke(execution_id, platform=platform or getattr(installed(), "platform", None), db=db)


async def claim_revision(task_id, system_id, mr_url, expected_revision, *, principal=None, db=None):
    management._operator(principal)
    pool = db or connection.get_pool()
    config = await rules.get_config(pool)
    adapter = config["adapter"]
    if adapter is None or adapter["system_id"] != system_id:
        raise OpError(ErrorCode.INVALID, "unknown review system")
    service = installed() or IntentRuleService(db=pool)
    fact = await service.verifier(mr_url, adapter)
    identity = _identity(fact, adapter)
    if identity is None:
        raise OpError(ErrorCode.CONFLICT, _identity_failure(fact))
    if identity["revision"] != expected_revision:
        raise OpError(ErrorCode.CONFLICT, "revision_changed")
    async with pool.acquire() as conn, conn.transaction():
        await rules.lock_admission(conn)
        if (await rules.get_config(conn))["generation"] != config["generation"]:
            raise OpError(ErrorCode.CONFLICT, "configuration changed")
        await ledger.lock_mr_target(conn, identity)
        existing = await ledger.get_claim(conn, identity)
        if existing and existing["task_id"] != task_id:
            raise OpError(ErrorCode.CONFLICT, "revision belongs to another task")
        task = await conn.fetchrow("SELECT id,status FROM tasks WHERE id=$1 FOR SHARE", task_id)
        if task is None:
            raise OpError(ErrorCode.NOT_FOUND, "task not found")
        await ledger.insert_claim(conn, identity, task_id)
        execution = await ledger.insert_execution(conn, task_id=task_id, mode="manual", mr_key=identity)
        if task["status"] != "init":
            await conn.execute("UPDATE intent_rule_executions SET start_state='started' WHERE id=$1 AND start_state='pending'", execution["id"])
    return {"task_id":task_id,"mr_key":identity,"existing":bool(existing)}


async def migration_preview(discovery_source_id, entries, *, principal=None, db=None):
    operator = management._operator(principal)
    pool = db or connection.get_pool()
    if not 1 <= len(entries) <= 100:
        raise OpError(ErrorCode.INVALID, "expected 1 to 100 entries")
    config = await rules.get_config(pool)
    source = next((s for s in config["discovery_sources"] if s["id"] == discovery_source_id), None)
    if source is None or not source["enabled"]:
        raise OpError(ErrorCode.INVALID, "unknown discovery source")
    if config["adapter"] is None or source["system_id"] != config["adapter"]["system_id"]:
        raise OpError(ErrorCode.INVALID, "review system is not configured")
    service = installed() or IntentRuleService(db=pool)
    results = []
    for entry in entries:
        fact = (await service.verifier(entry["mr_url"], config["adapter"])
                if entry["system_id"] == source["system_id"] else VerificationUnavailable("cannot_decide"))
        identity = _identity(fact, config["adapter"])
        task = await pool.fetchrow("SELECT id,updated_at FROM tasks WHERE id=$1", entry["task_id"])
        status = "conflict"
        if task and identity and entry["system_id"] == identity["system_id"]:
            claim = await ledger.get_claim(pool, identity)
            status = "conflict" if claim and claim["task_id"] != entry["task_id"] else (
                "verified" if entry.get("revision") == identity["revision"] else "blocked")
        results.append({"task_id":entry["task_id"],"identity":identity,"status":status,
                        "task_fingerprint":_digest(dict(task)) if task else None})
    preview_id = uuid4()
    fingerprint = _digest({"config":config,"source":source})
    from datetime import timedelta
    await pool.execute("INSERT INTO intent_rule_migration_previews(id,operator_key,generation,discovery_source_id,entries,fingerprint,expires_at) VALUES($1,$2,$3,$4,$5::jsonb,$6,$7)",
        preview_id, operator, config["generation"], discovery_source_id, json.dumps(results), fingerprint, _now()+timedelta(minutes=10))
    counts = {key:sum(r["status"] == status for r in results) for key,status in (("verified","verified"),("blocked","blocked"),("conflicts","conflict"))}
    return {"preview_id":str(preview_id),**counts,"impact":results}


async def migration_confirm(preview_id, *, principal=None, db=None):
    operator = management._operator(principal)
    pool = db or connection.get_pool()
    async with pool.acquire() as conn, conn.transaction():
        await rules.lock_admission(conn)
        config = await rules.lock_config(conn)
        preview = await conn.fetchrow("SELECT * FROM intent_rule_migration_previews WHERE id=$1 FOR UPDATE", UUID(preview_id))
        if preview is None or preview["operator_key"] != operator:
            raise OpError(ErrorCode.NOT_FOUND, "migration preview not found")
        if preview["consumed_at"]:
            return _json(preview["result"])
        source = next((s for s in config["discovery_sources"] if s["id"] == preview["discovery_source_id"]), None)
        if preview["expires_at"] <= _now() or preview["fingerprint"] != _digest({"config":config,"source":source}):
            raise OpError(ErrorCode.CONFLICT, "migration preview expired or configuration changed")
        counts = {"registered":0,"blocked":0,"conflicts":0}
        for entry in sorted(_json(preview["entries"]), key=lambda e:json.dumps(e["identity"],sort_keys=True)):
            identity = entry["identity"]
            if identity is not None:
                await ledger.lock_mr_target(conn, identity)
            task = await conn.fetchrow("SELECT id,updated_at FROM tasks WHERE id=$1 FOR SHARE", entry["task_id"])
            if entry["status"] == "conflict" or identity is None or task is None or _digest(dict(task)) != entry["task_fingerprint"]:
                counts["conflicts"] += 1
                continue
            await ledger.lock_mr_target(conn, identity)
            existing = await ledger.get_claim(conn, identity)
            if existing and existing["task_id"] != entry["task_id"]:
                counts["conflicts"] += 1
            elif entry["status"] == "blocked":
                await conn.execute("UPDATE intent_rule_mr_targets SET blocked_reason='unknown_legacy_revision' WHERE system_id=$1 AND repository_id=$2 AND mr_id=$3",
                    identity["system_id"],identity["repository_id"],identity["mr_id"])
                counts["blocked"] += 1
            else:
                await ledger.insert_claim(conn,identity,entry["task_id"])
                await ledger.insert_execution(conn,task_id=entry["task_id"],mode="manual",mr_key=identity)
                counts["registered"] += 1
        await conn.execute("UPDATE intent_rule_migration_previews SET consumed_at=now(),result=$2::jsonb WHERE id=$1", UUID(preview_id),json.dumps(counts))
    return counts
