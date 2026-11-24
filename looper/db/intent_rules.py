"""Rule storage. The caller owns the connection, transaction and authorization."""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from looper.agents import intent_rules_model as model
from .intent_rule_execution import (
    claim_notices as claim_notices,
    claim_starts as claim_starts,
    claim_work as claim_work,
    consume_project_day as consume_project_day,
    create_discovery as create_discovery,
    decide_work as decide_work,
    enqueue_notice as enqueue_notice,
    enqueue_start as enqueue_start,
    enqueue_work as enqueue_work,
    execution_view as execution_view,
    finish_start as finish_start,
    get_claim as get_claim,
    get_discovery as get_discovery,
    get_execution as get_execution,
    get_notice as get_notice,
    get_work as get_work,
    insert_claim as insert_claim,
    insert_execution as insert_execution,
    link_hit_execution as link_hit_execution,
    lock_mr_target as lock_mr_target,
    lock_project_day as lock_project_day,
    record_verification as record_verification,
    update_notice as update_notice,
)

_RULE_FIELDS = ("id", "version", "enabled", "position", "intent_type", "scope", "conditions", "action",
                "project_id", "template_key")
_JSON_FIELDS = {"adapter", "discovery_sources", "activation_acceptance", "scope", "conditions", "command",
                "reference_fingerprints", "diff", "result", "change", "mr_identity"}


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
        raise ValueError("rule writes require the caller's transaction")


def _time(value: Any) -> datetime | None:
    return datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) else value


async def get_config(conn: Any) -> dict:
    return _row(await conn.fetchrow("SELECT * FROM intent_rule_config WHERE id")) or {
        "generation": 0, "adapter": None, "discovery_sources": [], "activation_acceptance": None,
        "updated_at": None}


async def lock_admission(conn: Any) -> None:
    """Shared by configuration activation and the executor's final admission."""
    _writing(conn)
    await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended('looper.intent_rules.admission', 0))")


async def lock_config(conn: Any) -> dict:
    _writing(conn)
    await conn.execute("INSERT INTO intent_rule_config(id) VALUES (TRUE) ON CONFLICT DO NOTHING")
    return _row(await conn.fetchrow("SELECT * FROM intent_rule_config WHERE id FOR UPDATE"))


async def read_rules(conn: Any, *, include_deleted: bool = False) -> list[dict]:
    rows = await conn.fetch("SELECT r.id, v.version, v.enabled, v.position, v.intent_type, v.scope, v.conditions, "
        "v.action, v.project_id, v.template_key FROM intent_rules r JOIN intent_rule_versions v "
        "ON v.rule_id=r.id AND v.version=r.version WHERE ($1 OR r.deleted_at IS NULL) "
        "ORDER BY r.position NULLS LAST, r.id", include_deleted)
    return [_row(row) for row in rows]


async def history(conn: Any, rule_id: str) -> dict | None:
    pointer = _row(await conn.fetchrow("SELECT * FROM intent_rules WHERE id=$1", UUID(rule_id)))
    if pointer is None:
        return None
    versions = [_row(row) for row in await conn.fetch("SELECT rule_id AS id, version, enabled, position, "
        "intent_type, scope, conditions, action, project_id, template_key, rolled_back_from, edited_by, created_at "
        "FROM intent_rule_versions WHERE rule_id=$1 ORDER BY version", UUID(rule_id))]
    current = {key: versions[-1][key] for key in _RULE_FIELDS}
    current.update(position=pointer["position"], deleted_at=pointer["deleted_at"])
    return {"current": current, "versions": versions}


async def append_rule_version(conn: Any, rule: dict, *, edited_by: str,
                              rolled_back_from: int | None = None, deleted: bool = False) -> None:
    _writing(conn)
    rule = model.validate_rule(rule)
    rule_id = UUID(rule["id"])
    if rule["version"] == 1:
        await conn.execute("INSERT INTO intent_rules(id, version, position, deleted_at) "
                           "VALUES ($1,1,$2,CASE WHEN $3 THEN now() END)", rule_id,
                           None if deleted else rule["position"], deleted)
    else:
        changed = await conn.fetchval("UPDATE intent_rules SET version=$2, position=$3, "
            "deleted_at=CASE WHEN $4 THEN now() END WHERE id=$1 AND version=$2-1 AND deleted_at IS NULL RETURNING id",
            rule_id, rule["version"], None if deleted else rule["position"], deleted)
        if changed is None:
            raise ValueError("rule version changed or the rule was deleted")
    await conn.execute("INSERT INTO intent_rule_versions(rule_id,version,enabled,position,intent_type,scope,conditions,"
        "action,project_id,template_key,rolled_back_from,edited_by) VALUES ($1,$2,$3,$4,$5,$6::jsonb,$7::jsonb,"
        "$8,$9,$10,$11,$12)", rule_id, rule["version"], rule["enabled"], rule["position"], rule["intent_type"],
        json.dumps(rule["scope"]), json.dumps(rule["conditions"]), rule["action"], rule["project_id"],
        rule["template_key"], rolled_back_from, edited_by)


async def preview(conn: Any, *, operator_key: str, expected_generation: int, command: dict,
                  reference_fingerprints: dict, diff: dict, now: datetime) -> dict:
    _writing(conn)
    return _row(await conn.fetchrow("INSERT INTO intent_rule_previews(id,operator_key,expected_generation,command,"
        "reference_fingerprints,diff,expires_at,created_at) VALUES ($1,$2,$3,$4::jsonb,$5::jsonb,$6::jsonb,$7,$8) "
        "RETURNING *", uuid4(), operator_key, expected_generation, json.dumps(command),
        json.dumps(reference_fingerprints), json.dumps(diff), now + timedelta(minutes=10), now))


async def get_preview(conn: Any, preview_id: str, *, lock: bool = False) -> dict | None:
    return _row(await conn.fetchrow("SELECT * FROM intent_rule_previews WHERE id=$1" +
                                   (" FOR UPDATE" if lock else ""), UUID(preview_id)))


async def update_config(conn: Any, *, generation: int, adapter: dict | None, discovery_sources: list,
                        activation_acceptance: list | None, now: datetime) -> None:
    _writing(conn)
    adapter = model.validate_adapter(adapter)
    discovery_sources = model.validate_discovery_sources(discovery_sources)
    await conn.execute("UPDATE intent_rule_config SET generation=$1,adapter=$2::jsonb,discovery_sources=$3::jsonb,"
        "activation_acceptance=$4::jsonb,updated_at=$5 WHERE id", generation,
        json.dumps(adapter) if adapter is not None else None, json.dumps(discovery_sources),
        json.dumps(activation_acceptance) if activation_acceptance is not None else None, now)


async def confirm(conn: Any, *, preview_id: str, operator_key: str, generation: int,
                  change: dict, result: dict, now: datetime) -> None:
    """The audit and consumption are part of the configuration writer's transaction."""
    _writing(conn)
    await conn.execute("INSERT INTO intent_rule_config_audit(generation,preview_id,operator_key,change,created_at) "
                       "VALUES ($1,$2,$3,$4::jsonb,$5)", generation, UUID(preview_id), operator_key,
                       json.dumps(change), now)
    changed = await conn.fetchval("UPDATE intent_rule_previews SET consumed_at=$3,result=$4::jsonb "
        "WHERE id=$1 AND operator_key=$2 AND consumed_at IS NULL RETURNING id", UUID(preview_id), operator_key,
        now, json.dumps(result))
    if changed is None:
        raise ValueError("the preview was consumed or belongs to another operator")


_HIT_SUMMARY = ("id,created_at,rule_id,rule_version,selection,action,reason,intent_id,discovery_id,mr_identity")


def _summary(row: Any) -> dict:
    result = {"task_id": None, "execution_state": None, **_row(row)}
    if result.get("reason") is not None and result["reason"] not in set(model.ExecutionReason):
        result["reason"] = "unknown"
    return result


async def list_hits(conn: Any, *, rule_id: str | None = None, action: str | None = None, reason: str | None = None,
                    before_id: int | None = None, limit: int = 50) -> dict:
    rows = await conn.fetch("SELECT " + ",".join("h." + key for key in _HIT_SUMMARY.split(",")) +
        ",e.task_id,e.start_state AS execution_state FROM intent_rule_hits h "
        "LEFT JOIN intent_rule_hit_executions x ON x.hit_id=h.id "
        "LEFT JOIN intent_rule_executions e ON e.id=x.execution_id "
        "WHERE ($1::uuid IS NULL OR h.rule_id=$1) AND ($2::text IS NULL OR h.action=$2) "
        "AND ($3::text IS NULL OR h.reason=$3) AND ($4::bigint IS NULL OR h.id<$4) ORDER BY h.id DESC LIMIT $5",
        UUID(rule_id) if rule_id is not None else None, action, reason, before_id, limit + 1)
    return {"hits": [_summary(row) for row in rows[:limit]], "more": len(rows) > limit}


async def get_hit(conn: Any, hit_id: int) -> dict | None:
    hit = await conn.fetchrow(f"SELECT {_HIT_SUMMARY},attempts FROM intent_rule_hits WHERE id=$1", hit_id)
    if hit is None:
        return None
    hit = dict(hit)
    attempts = hit.pop("attempts")
    if isinstance(attempts, str):
        attempts = json.loads(attempts)
    conditions = [{**_row({key: value for key, value in dict(row).items() if key != "result"}),
                   "result": row["result"]}
                  for row in await conn.fetch("SELECT rule_position,condition_position,rule_id,rule_version,"
        "kind,result,reason,verified_at FROM intent_rule_condition_results WHERE hit_id=$1 "
        "ORDER BY rule_position,condition_position", hit_id)]
    sources = [dict(row) for row in await conn.fetch("SELECT s.event_seq,e.source_id,e.chat_id,e.external_id,e.sent_at "
        "FROM intent_rule_hit_sources s JOIN source_events e ON e.seq=s.event_seq WHERE s.hit_id=$1 "
        "ORDER BY e.sent_at,e.seq", hit_id)]
    execution = await execution_view(conn, hit_id=hit_id)
    summary = _summary(hit)
    if execution is not None:
        summary.update(task_id=execution["task_id"], execution_state=execution["start_state"])
    return {"hit": summary, "conditions": conditions, "attempts": attempts, "sources": sources, "execution": execution}


async def hit_stats(conn: Any, *, since: datetime, until: datetime, rule_id: str | None = None) -> dict:
    rows = await conn.fetch("WITH window_hits AS (SELECT * FROM intent_rule_hits "
        "WHERE created_at >= $1 AND created_at < $2), metrics AS ("
        "SELECT h.rule_id,h.rule_version AS version,count(*) AS hits,"
        "count(*) FILTER (WHERE h.reason IN ('verification_unavailable','adapter_missing','adapter_start_failed',"
        "'verification_timeout','verification_output_limit','verification_exit','verification_protocol',"
        "'authentication_failed','permission_denied','not_readable','cannot_decide','upstream_error') "
        "AND NOT EXISTS (SELECT 1 FROM intent_rule_condition_results c WHERE c.hit_id=h.id "
        "AND c.result='unavailable')) AS verification_unavailable,"
        "count(DISTINCT e.id) FILTER (WHERE e.mode='auto' AND h.action='create_task' "
        "AND h.reason IS DISTINCT FROM 'deduped') AS auto_created,"
        "count(*) FILTER (WHERE h.reason='deduped') AS deduped,"
        "count(*) FILTER (WHERE h.reason='daily_cap') AS daily_cap "
        "FROM window_hits h LEFT JOIN intent_rule_hit_executions x ON x.hit_id=h.id "
        "LEFT JOIN intent_rule_executions e ON e.id=x.execution_id "
        "WHERE ($3::uuid IS NULL OR h.rule_id=$3) GROUP BY h.rule_id,h.rule_version "
        "UNION ALL SELECT c.rule_id,c.rule_version AS version,0 AS hits,"
        "count(DISTINCT h.id) AS verification_unavailable,0 AS auto_created,0 AS deduped,0 AS daily_cap "
        "FROM window_hits h JOIN intent_rule_condition_results c ON c.hit_id=h.id "
        "WHERE c.result='unavailable' AND ($3::uuid IS NULL OR c.rule_id=$3) "
        "GROUP BY c.rule_id,c.rule_version) "
        "SELECT rule_id,version,sum(hits)::bigint AS hits,"
        "sum(verification_unavailable)::bigint AS verification_unavailable,"
        "sum(auto_created)::bigint AS auto_created,sum(deduped)::bigint AS deduped,"
        "sum(daily_cap)::bigint AS daily_cap FROM metrics GROUP BY rule_id,version ORDER BY rule_id,version",
        since, until, UUID(rule_id) if rule_id is not None else None)
    return {"available": True, "range": {"since": since, "until": until},
            "rules": [_row(row) for row in rows], "projects": []}


async def append_hit(conn: Any, hit: dict, *, conditions: list | tuple = (), sources: list | tuple = (),
                     attempts: list | tuple = ()) -> int:
    _writing(conn)
    hit = model.validate_hit(hit)
    conditions = [model.validate_condition_result(item) for item in conditions]
    attempts = [model.validate_schema(item, model.ATTEMPT_RESULT_SCHEMA) for item in attempts]
    if any(isinstance(seq, bool) or not isinstance(seq, int) or seq <= 0 for seq in sources):
        raise ValueError("hit sources must be positive event sequence IDs")
    hit_id = await conn.fetchval("INSERT INTO intent_rule_hits(evaluation_key,intent_id,discovery_id,generation,"
        "rule_id,rule_version,selection,action,reason,verified_at,mr_identity,attempts) "
        "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11::jsonb,$12::jsonb) ON CONFLICT (evaluation_key) DO NOTHING RETURNING id",
        hit["evaluation_key"], hit["intent_id"], hit["discovery_id"], hit["generation"],
        UUID(hit["rule_id"]) if hit["rule_id"] is not None else None, hit["rule_version"], hit["selection"],
        hit["action"], hit["reason"], _time(hit["verified_at"]),
        json.dumps(hit["mr_identity"]) if hit["mr_identity"] is not None else None, json.dumps(attempts))
    if hit_id is None:
        return await conn.fetchval("SELECT id FROM intent_rule_hits WHERE evaluation_key=$1", hit["evaluation_key"])
    for item in conditions:
        await conn.execute("INSERT INTO intent_rule_condition_results(hit_id,rule_position,condition_position,rule_id,"
            "rule_version,kind,result,reason,verified_at) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)", hit_id,
            item["rule_position"], item["condition_position"], UUID(item["rule_id"]), item["rule_version"],
            item["kind"], item["result"], item["reason"], _time(item["verified_at"]))
    for seq in sorted(set(sources)):
        await conn.execute("INSERT INTO intent_rule_hit_sources(hit_id,event_seq) VALUES ($1,$2)", hit_id, seq)
    return hit_id
