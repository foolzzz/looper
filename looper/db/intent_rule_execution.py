"""Transaction-owned execution ledger primitives; no model or external side effects.

Admission follows source events, chats, group reads, admission, configuration,
project, template, candidate, MR target, project day, task and start. Callers
acquire the earlier locks; these helpers never acquire them in reverse order.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone as dt_timezone
from typing import Any
from uuid import UUID, uuid4

from looper.agents import intent_rules_model as model

_JSON_FIELDS = {"prepared_target", "first_fact", "mr_key", "entries", "result"}


def _row(row: Any) -> dict | None:
    if row is None:
        return None
    result = dict(row)
    for key, value in result.items():
        if key in _JSON_FIELDS and isinstance(value, str):
            result[key] = json.loads(value)
        elif isinstance(value, UUID):
            result[key] = str(value)
    return result


def _writing(conn: Any) -> None:
    if not conn.is_in_transaction():
        raise ValueError("execution writes require the caller's transaction")


def _now(now: datetime | None) -> datetime:
    return now if now is not None else datetime.now(dt_timezone.utc)


def _token(value: str | UUID | None) -> UUID | None:
    return UUID(str(value)) if value is not None else None


def _mr_key(key: dict) -> dict:
    if not isinstance(key, dict):
        raise ValueError("MR ownership requires a verified identity")
    return model.validate_mr_identity(key)


def _first_fact(value: dict | None) -> None:
    if value is None:
        return
    target = model._object({"system_id": model.ID_SCHEMA, "mr_url": {"type": "string"},
        "host": model.HOST_SCHEMA, "route_ids": {"type": "object", "additionalProperties": model.ID_SCHEMA}})
    unavailable = model._object({"status": {"const": "unavailable"}, "reason": {"type": "string", "enum": [
        "adapter_missing", "adapter_start_failed", "verification_timeout", "verification_output_limit",
        "verification_exit", "verification_protocol", "authentication_failed", "permission_denied",
        "not_readable", "cannot_decide", "upstream_error", "ambiguous_target"]},
        "verified_at": model.TIMESTAMP_SCHEMA})
    nullable_id = model._nullable(model.ID_SCHEMA)
    verified = model._object({"status": {"const": "verified"}, "target": target,
        "exists": model.BOOL, "state": model._nullable({"enum": ["open", "merged", "closed"]}),
        "reviewers": model._array(model.ID_SCHEMA), "repository": nullable_id,
        "mr_id": nullable_id, "revision": nullable_id, "canonical_url": nullable_id,
        "verified_at": model.TIMESTAMP_SCHEMA, "operator_id": {"type": "string"}})
    model.validate_schema(value, {"oneOf": [verified, unavailable]})
    if value["status"] == "verified":
        fields = ("state", "repository", "mr_id", "revision", "canonical_url")
        if value["exists"] and any(value[field] is None for field in fields):
            raise ValueError("existing MR fact requires its verified identity")
        if not value["exists"] and (any(value[field] is not None for field in fields) or value["reviewers"]):
            raise ValueError("missing MR fact cannot carry an identity or reviewers")


async def create_discovery(conn: Any, *, discovery_source_id: str, source_ref: str,
                           system_id: str, mr_url: str) -> dict:
    _writing(conn)
    row = await conn.fetchrow("INSERT INTO intent_rule_discoveries(discovery_source_id,source_ref,system_id,mr_url) "
        "VALUES ($1,$2,$3,$4) ON CONFLICT (discovery_source_id,source_ref) DO NOTHING RETURNING *",
        discovery_source_id, source_ref, system_id, mr_url)
    if row is None:
        row = await conn.fetchrow("SELECT * FROM intent_rule_discoveries WHERE discovery_source_id=$1 AND source_ref=$2",
                                  discovery_source_id, source_ref)
    return _row(row)


async def get_discovery(conn: Any, discovery_id: int, *, lock: bool = False) -> dict | None:
    if lock:
        _writing(conn)
    return _row(await conn.fetchrow("SELECT * FROM intent_rule_discoveries WHERE id=$1" +
                                   (" FOR UPDATE" if lock else ""), discovery_id))


async def enqueue_work(conn: Any, *, generation: int, source_fingerprint: str,
                       intent_id: int | None = None, discovery_id: int | None = None,
                       prepared_target: dict | None = None, first_fact: dict | None = None,
                       first_verified_at: datetime | None = None, first_verification_status: str = "unavailable",
                       first_reason: str | None = None) -> dict:
    _writing(conn)
    if (intent_id is None) == (discovery_id is None):
        raise ValueError("rule work requires exactly one source")
    if prepared_target is not None and not isinstance(prepared_target, dict):
        raise ValueError("prepared target must be typed metadata")
    _first_fact(first_fact)
    if first_verification_status == "verified" and (first_fact is None or first_fact.get("status") != "verified"):
        raise ValueError("verified work requires the persisted first verification")
    row = await conn.fetchrow("INSERT INTO intent_rule_work(intent_id,discovery_id,generation,source_fingerprint,"
        "prepared_target,first_fact,first_verified_at,first_verification_status,first_reason) "
        "VALUES ($1,$2,$3,$4,$5::jsonb,$6::jsonb,$7,$8,$9) ON CONFLICT DO NOTHING RETURNING *",
        intent_id, discovery_id, generation, source_fingerprint,
        json.dumps(prepared_target) if prepared_target is not None else None,
        json.dumps(first_fact) if first_fact is not None else None, first_verified_at,
        first_verification_status, first_reason)
    return _row(row) if row is not None else await get_work(conn, intent_id=intent_id, discovery_id=discovery_id)


async def get_work(conn: Any, *, intent_id: int | None = None, discovery_id: int | None = None,
                   work_id: int | None = None) -> dict | None:
    supplied = [(key, value) for key, value in (("intent_id", intent_id), ("discovery_id", discovery_id),
                                               ("id", work_id)) if value is not None]
    if len(supplied) != 1:
        raise ValueError("select exactly one work reference")
    key, value = supplied[0]
    return _row(await conn.fetchrow(f"SELECT * FROM intent_rule_work WHERE {key}=$1", value))


async def claim_work(conn: Any, *, now: datetime, limit: int = 20, lease_seconds: int = 30) -> list[dict]:
    _writing(conn)
    if not 1 <= limit <= 100 or not 1 <= lease_seconds <= 300:
        raise ValueError("invalid work lease bounds")
    token = uuid4()
    rows = await conn.fetch("WITH ready AS (SELECT id FROM intent_rule_work WHERE state='pending' "
        "OR (state='evaluating' AND lease_until <= $1) ORDER BY id FOR UPDATE SKIP LOCKED LIMIT $2) "
        "UPDATE intent_rule_work w SET state='evaluating',lease_token=$3,lease_until=$4 "
        "FROM ready WHERE w.id=ready.id RETURNING w.*", now, limit, token, now + timedelta(seconds=lease_seconds))
    return [_row(row) for row in rows]


async def decide_work(conn: Any, work_id: int, *, decision: str, reason: str, hit_id: int | None = None,
                      lease_token: str | UUID | None = None, now: datetime | None = None) -> bool:
    _writing(conn)
    return await conn.fetchval("UPDATE intent_rule_work SET state='decided',decision=$2,reason=$3,hit_id=$4,"
        "decided_at=$5,lease_token=NULL,lease_until=NULL WHERE id=$1 AND state <> 'decided' "
        "AND ($6::uuid IS NULL OR lease_token=$6) RETURNING TRUE", work_id, decision, reason,
        hit_id, _now(now), _token(lease_token)) is True


async def lock_mr_target(conn: Any, mr_key: dict) -> dict:
    _writing(conn)
    key = model.validate_schema(
        {field: mr_key[field] for field in ("system_id", "repository_id", "mr_id")},
        model._object({field: model.ID_SCHEMA for field in ("system_id", "repository_id", "mr_id")}))
    args = (key["system_id"], key["repository_id"], key["mr_id"])
    await conn.execute("INSERT INTO intent_rule_mr_targets(system_id,repository_id,mr_id) VALUES ($1,$2,$3) "
                       "ON CONFLICT DO NOTHING", *args)
    return _row(await conn.fetchrow("SELECT * FROM intent_rule_mr_targets WHERE system_id=$1 "
                                   "AND repository_id=$2 AND mr_id=$3 FOR UPDATE", *args))


async def get_claim(conn: Any, mr_key: dict) -> dict | None:
    key = _mr_key(mr_key)
    return _row(await conn.fetchrow("SELECT * FROM intent_rule_mr_claims WHERE system_id=$1 AND repository_id=$2 "
        "AND mr_id=$3 AND revision=$4", key["system_id"], key["repository_id"], key["mr_id"], key["revision"]))


async def insert_claim(conn: Any, mr_key: dict, task_id: str) -> dict:
    _writing(conn)
    key = _mr_key(mr_key)
    row = await conn.fetchrow("INSERT INTO intent_rule_mr_claims(system_id,repository_id,mr_id,revision,task_id) "
        "VALUES ($1,$2,$3,$4,$5) ON CONFLICT DO NOTHING RETURNING *", key["system_id"], key["repository_id"],
        key["mr_id"], key["revision"], task_id)
    return _row(row) if row is not None else await get_claim(conn, key)


async def lock_project_day(conn: Any, *, operator_key: str, project_id: str, local_day: date,
                           timezone: str) -> dict:
    _writing(conn)
    await conn.execute("INSERT INTO intent_rule_project_days(operator_key,project_id,local_day,timezone) "
                       "VALUES ($1,$2,$3,$4) ON CONFLICT DO NOTHING", operator_key, project_id, local_day, timezone)
    return _row(await conn.fetchrow("SELECT * FROM intent_rule_project_days WHERE operator_key=$1 AND project_id=$2 "
                                   "AND local_day=$3 FOR UPDATE", operator_key, project_id, local_day))


async def consume_project_day(conn: Any, *, operator_key: str, project_id: str, local_day: date,
                              timezone: str, cap: int) -> bool:
    _writing(conn)
    if type(cap) is not int or cap < 1:
        raise ValueError("project automatic task cap must be positive")
    await lock_project_day(conn, operator_key=operator_key, project_id=project_id, local_day=local_day, timezone=timezone)
    return await conn.fetchval("UPDATE intent_rule_project_days SET used=used+1,updated_at=now() "
        "WHERE operator_key=$1 AND project_id=$2 AND local_day=$3 AND used<$4 RETURNING TRUE",
        operator_key, project_id, local_day, cap) is True


async def insert_execution(conn: Any, *, task_id: str, mode: str, mr_key: dict,
                           quota_day: date | None = None, quota_operator: str | None = None,
                           quota_project: str | None = None, quota_units: int = 0) -> dict:
    _writing(conn)
    key = _mr_key(mr_key)
    row = await conn.fetchrow("INSERT INTO intent_rule_executions(task_id,mode,mr_key,quota_day,quota_operator,"
        "quota_project,quota_units) VALUES ($1,$2,$3::jsonb,$4,$5,$6,$7) ON CONFLICT (task_id) DO NOTHING RETURNING *",
        task_id, mode, json.dumps(key), quota_day, quota_operator, quota_project, quota_units)
    return _row(row) if row is not None else await get_execution(conn, task_id=task_id)


async def get_execution(conn: Any, execution_id: int | None = None, *, task_id: str | None = None,
                        lock: bool = False) -> dict | None:
    if (execution_id is None) == (task_id is None):
        raise ValueError("select exactly one execution reference")
    if lock:
        _writing(conn)
    key, value = ("id", execution_id) if execution_id is not None else ("task_id", task_id)
    return _row(await conn.fetchrow(f"SELECT * FROM intent_rule_executions WHERE {key}=$1" +
                                   (" FOR UPDATE" if lock else ""), value))


async def link_hit_execution(conn: Any, hit_id: int, execution_id: int) -> None:
    _writing(conn)
    await conn.execute("INSERT INTO intent_rule_hit_executions(hit_id,execution_id) VALUES ($1,$2) "
                       "ON CONFLICT (hit_id) DO NOTHING", hit_id, execution_id)
    if await conn.fetchval("SELECT execution_id FROM intent_rule_hit_executions WHERE hit_id=$1", hit_id) != execution_id:
        raise ValueError("hit already belongs to another execution")


async def execution_view(conn: Any, execution_id: int | None = None, *, task_id: str | None = None,
                         hit_id: int | None = None) -> dict | None:
    if hit_id is not None:
        execution_id = await conn.fetchval("SELECT execution_id FROM intent_rule_hit_executions WHERE hit_id=$1", hit_id)
        if execution_id is None:
            return None
    item = await get_execution(conn, execution_id, task_id=task_id)
    if item is None:
        return None
    notice = await conn.fetchrow("SELECT state,decision,reason FROM intent_rule_notices "
                                 "WHERE execution_id=$1 AND kind='created'", item["id"])
    return {"id": item["id"], "task_id": item["task_id"], "mode": item["mode"], "mr_key": item["mr_key"],
            "quota": {"day": item["quota_day"], "project_id": item["quota_project"], "units": item["quota_units"]}
                     if item["quota_units"] else None,
            "start_state": item["start_state"], "notice": dict(notice) if notice is not None else None,
            "revoke": {"state": item["revoke_state"], "reason": item["last_error_code"]
                       if item["revoke_state"] in {"failed", "already_terminal"} else None},
            "last_error_code": item["last_error_code"]}


async def enqueue_start(conn: Any, task_id: str, execution_id: int) -> dict:
    _writing(conn)
    row = await conn.fetchrow("INSERT INTO intent_rule_task_starts(task_id,execution_id) VALUES ($1,$2) "
                              "ON CONFLICT (task_id) DO NOTHING RETURNING *", task_id, execution_id)
    if row is None:
        row = await conn.fetchrow("SELECT * FROM intent_rule_task_starts WHERE task_id=$1", task_id)
    return _row(row)


async def claim_starts(conn: Any, *, now: datetime, limit: int = 20, lease_seconds: int = 30) -> list[dict]:
    _writing(conn)
    if not 1 <= limit <= 100 or not 1 <= lease_seconds <= 300:
        raise ValueError("invalid start lease bounds")
    rows = await conn.fetch("WITH ready AS (SELECT s.task_id FROM intent_rule_task_starts s "
        "JOIN intent_rule_executions e ON e.id=s.execution_id WHERE e.revoke_state='none' AND "
        "((s.state='pending' AND s.next_attempt_at<=$1) OR (s.state='claimed' AND s.lease_until<=$1)) "
        "ORDER BY s.next_attempt_at,s.task_id FOR UPDATE OF s SKIP LOCKED LIMIT $2) "
        "UPDATE intent_rule_task_starts s SET state='claimed',lease_token=$3,lease_until=$4,attempts=attempts+1,"
        "updated_at=$1 FROM ready WHERE s.task_id=ready.task_id RETURNING s.*", now, limit, uuid4(),
        now + timedelta(seconds=lease_seconds))
    return [_row(row) for row in rows]


async def finish_start(conn: Any, task_id: str, *, lease_token: str | UUID, state: str,
                       error_code: str | None = None, now: datetime) -> bool:
    _writing(conn)
    if state not in {"pending", "acknowledged", "cancelled"}:
        raise ValueError("invalid completed start state")
    row = await conn.fetchrow("UPDATE intent_rule_task_starts SET state=$3,lease_token=NULL,lease_until=NULL,"
        "last_error_code=$4,updated_at=$5,next_attempt_at=$5::timestamptz + make_interval(secs => LEAST(300,"
        "30 * power(2,LEAST(attempts-1,4)))::integer) WHERE task_id=$1 AND lease_token=$2 "
        "AND state='claimed' RETURNING execution_id", task_id, _token(lease_token), state, error_code, now)
    if row is None:
        return False
    execution_state = {"pending": "failed", "acknowledged": "started", "cancelled": "cancelled"}[state]
    await conn.execute("UPDATE intent_rule_executions SET start_state=$2,last_error_code=$3,updated_at=$4 WHERE id=$1",
                       row["execution_id"], execution_state, error_code, now)
    return True


async def enqueue_notice(conn: Any, *, kind: str, dedupe_key: str, execution_id: int | None = None,
                         discovery_id: int | None = None, channel_id: str | None = None,
                         chat: str | None = None, reason: str | None = None) -> dict:
    _writing(conn)
    row = await conn.fetchrow("INSERT INTO intent_rule_notices(kind,dedupe_key,execution_id,discovery_id,channel_id,chat,reason) "
        "VALUES ($1,$2,$3,$4,$5,$6,$7) ON CONFLICT DO NOTHING RETURNING *", kind, dedupe_key,
        execution_id, discovery_id, channel_id, chat, reason)
    if row is None:
        row = await conn.fetchrow("SELECT * FROM intent_rule_notices WHERE dedupe_key=$1 "
                                 "OR (kind='created' AND execution_id=$2) ORDER BY id LIMIT 1", dedupe_key, execution_id)
    return _row(row)


async def get_notice(conn: Any, notice_id: int) -> dict | None:
    return _row(await conn.fetchrow("SELECT * FROM intent_rule_notices WHERE id=$1", notice_id))


async def claim_notices(conn: Any, *, now: datetime, limit: int = 20) -> list[dict]:
    _writing(conn)
    if not 1 <= limit <= 100:
        raise ValueError("invalid notice claim limit")
    rows = await conn.fetch("WITH ready AS (SELECT id FROM intent_rule_notices WHERE state IN ('pending','failed') "
        "ORDER BY id FOR UPDATE SKIP LOCKED LIMIT $1) UPDATE intent_rule_notices n "
        "SET state='sending',attempts=attempts+1,updated_at=$2 FROM ready WHERE n.id=ready.id RETURNING n.*", limit, now)
    return [_row(row) for row in rows]


async def update_notice(conn: Any, notice_id: int, *, state: str, external_id: str | None = None,
                        channel_id: str | None = None, chat: str | None = None,
                        decision: str | None = None, reason: str | None = None,
                        error_code: str | None = None, now: datetime | None = None) -> None:
    _writing(conn)
    await conn.execute("UPDATE intent_rule_notices SET state=$2,external_id=COALESCE($3,external_id),"
        "channel_id=COALESCE($4,channel_id),chat=COALESCE($5,chat),decision=$6,reason=$7,last_error_code=$8,updated_at=$9 "
        "WHERE id=$1", notice_id, state, external_id, channel_id, chat, decision, reason, error_code, _now(now))


async def record_verification(conn: Any, system_id: str, *, available: bool,
                              now: datetime) -> dict | None:
    """Serialize the failure episode and its unique normal-priority notice."""
    _writing(conn)
    await conn.execute("INSERT INTO intent_rule_verification_health(system_id) VALUES ($1) ON CONFLICT DO NOTHING", system_id)
    row = await conn.fetchrow("SELECT * FROM intent_rule_verification_health WHERE system_id=$1 FOR UPDATE", system_id)
    if available:
        await conn.execute("UPDATE intent_rule_verification_health SET consecutive_unavailable=0,alerted=false,"
                           "updated_at=$2 WHERE system_id=$1", system_id, now)
        return None
    count, episode = row["consecutive_unavailable"] + 1, row["episode"]
    if count == 1:
        episode += 1
    alert = count >= 3 and not row["alerted"]
    await conn.execute("UPDATE intent_rule_verification_health SET consecutive_unavailable=$2,episode=$3,"
                       "alerted=$4,updated_at=$5 WHERE system_id=$1", system_id, count, episode,
                       row["alerted"] or alert, now)
    if not alert:
        return None
    return await enqueue_notice(conn, kind="verification_fault", dedupe_key=f"rule-fault:{system_id}:{episode}",
                                reason="verification_unavailable")
