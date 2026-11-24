"""Operator-only, versioned rule configuration; this module never executes a rule.

A persisted preview binds the command and all current reference fingerprints.
Confirmation owns one admission lock and one transaction, including its audit.

Global row/admission lock order: source_chats (engine only) -> admission ->
rule config/preview -> projects -> workflows -> template contracts. Management
never row-locks source chats, event sources, channels, types or dependency settings
as catalog references under admission. Those reads are optimistic: confirmation
revalidates preview fingerprints, including reference versions; the engine must
recheck current authorization and dependencies before admitting any execution.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
from typing import Any
from uuid import UUID, uuid4

from ..db import intent_rules as store
from ..db.connection import _connection, get_pool
from . import chat_settings, intent_rules_model as model, leads_model
from .api_v1 import Principal
from .ops import ErrorCode, OpError
from .ops.service import _plain
from .channels.allowlist import SENDER_ID_FORMATS, sender_ids
from .message import parse_channel_id
from .subscription_runs import push_target
from .workflows import Workflow


def _operator(principal: Principal | None) -> str:
    # The transport supplies a Principal; a JSON boolean or a model's claim is never an identity.
    if not isinstance(principal, Principal) or principal.kind != "operator":
        raise OpError(ErrorCode.FORBIDDEN, "only the authenticated operator can manage intent rules")
    return principal.key


def _validate(function, *args, **kwargs):
    try:
        return function(*args, **kwargs)
    except (ValueError, TypeError) as exc:
        raise OpError(ErrorCode.INVALID, str(exc)) from None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _json(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(_plain(value), sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _ready(executor_available: bool) -> bool:
    return executor_available is True and leads_model.RULE_CLASSIFICATION_AVAILABLE


def _fail(code: ErrorCode, message: str):
    raise OpError(code, message)


async def _catalog(conn: Any, *, lock: bool = False) -> dict:
    """Small configuration tables only; never query messages or credentials.

    Only project/template share locks follow the admission/config locks.
    Fingerprints are conservative: unrelated configuration edits also require a new preview.
    """
    queries = {
        "projects": "SELECT id,name,work_dir,auto_task_daily_cap,auto_task_config_version FROM projects ORDER BY id",
        "templates": "SELECT key,name,description,definition FROM workflows ORDER BY key",
        "contracts": "SELECT * FROM intent_rule_template_contracts ORDER BY workflow_key",
        "types": "SELECT t.key,t.builtin,t.version,t.enabled,t.archived_at,v.name FROM intent_types t "
                 "JOIN intent_type_versions v ON v.type_key=t.key AND v.version=t.version ORDER BY t.key",
        "chats": "SELECT source_id,chat_id,kind,inclusion,extraction,state FROM source_chats ORDER BY source_id,chat_id",
        "sources": "SELECT id,user_open_id,ingest_mode FROM event_sources ORDER BY id",
        "channels": "SELECT channel_id,enabled,config FROM channel_configs ORDER BY channel_id",
        "dependencies": "SELECT key,value FROM system_settings WHERE key IN "
                        "('intents.enabled','delivery.enabled','intents.rules.enabled') ORDER BY key",
    }
    return {key: [{field: _json(value) if field in ("definition", "config") else value
                   for field, value in dict(row).items()} for row in await conn.fetch(
                       sql + (" FOR SHARE" if lock and key in ("projects", "templates", "contracts") else ""))]
            for key, sql in queries.items()}


def _flags(catalog: dict) -> dict[str, bool]:
    values = {row["key"]: row["value"] for row in catalog["dependencies"]}
    return {"enabled": values.get(leads_model.INTENTS_RULES_ENABLED_KEY) == "true",
            "intents_enabled": values.get(leads_model.INTENTS_ENABLED_KEY) == "true",
            "delivery_enabled": values.get(chat_settings.DELIVERY_ENABLED_KEY) == "true"}


def _preconditions(config: dict, catalog: dict, executor_available: bool) -> list[dict]:
    flags = _flags(catalog)
    enabled_types = sum(row["enabled"] and row["archived_at"] is None for row in catalog["types"])
    # A prepared MR type still needs room when the classification capability is installed.
    if not any(row["key"] == leads_model.RULE_INTENT_TYPE for row in catalog["types"]):
        enabled_types += 1
    checks = {"intents_enabled": flags["intents_enabled"], "delivery_enabled": flags["delivery_enabled"],
              "runtime_ready": _ready(executor_available), "type_capacity": enabled_types <= 10,
              "discovery_migrated": all(not source["enabled"] or source["migration_state"] != "unconfigured"
                                        for source in config["discovery_sources"])}
    return [{"code": code, "ok": bool(ok)} for code, ok in checks.items()]


def _settings(config: dict, catalog: dict, executor_available: bool) -> dict:
    return _plain({**_flags(catalog), "runtime_ready": _ready(executor_available),
        "generation": config["generation"], "adapter": config["adapter"],
        "discovery_sources": config["discovery_sources"],
        "preconditions": _preconditions(config, catalog, executor_available),
        "acceptance": config["activation_acceptance"]})


async def settings_get(*, principal=None, db=None, executor_available: bool = False) -> dict:
    _operator(principal)
    conn = db or get_pool()
    return _settings(await store.get_config(conn), await _catalog(conn), executor_available)


async def list_rules(*, principal=None, db=None, executor_available: bool = False) -> dict:
    _operator(principal)
    conn = db or get_pool()
    config, catalog = await store.get_config(conn), await _catalog(conn)
    rules = await store.read_rules(conn)
    types = [{"key": row["key"], "name": row["name"], "enabled": row["enabled"],
              "available_when_rules_on": row["archived_at"] is None}
             for row in catalog["types"] if row["archived_at"] is None]
    if not any(row["key"] == leads_model.RULE_INTENT_TYPE for row in types):
        types.append({"key": leads_model.RULE_INTENT_TYPE, "name": "MR 评审", "enabled": True,
                      "available_when_rules_on": True})
    return _plain({"generation": config["generation"], "runtime_ready": _ready(executor_available),
        "rules": rules, "fallback": model.fallback_view(max((r["position"] for r in rules), default=0) + 1),
        "configurable_types": types, "projects": [{"id": row["id"], "name": row["name"],
            "auto_task_daily_cap": row["auto_task_daily_cap"], "config_version": row["auto_task_config_version"]}
            for row in catalog["projects"]], "templates": catalog["contracts"],
        "settings": _settings(config, catalog, executor_available)})


async def history(rule_id: str, *, principal=None, db=None) -> dict:
    _operator(principal)
    _validate(model.validate_schema, rule_id, model.UUID_SCHEMA)
    rid = UUID(rule_id)
    result = await store.history(db or get_pool(), str(rid))
    if result is None:
        _fail(ErrorCode.NOT_FOUND, "intent rule not found")
    return _plain(result)


def _find(rows, key, value, *, code=ErrorCode.INVALID):
    row = next((row for row in rows if row[key] == value), None)
    if row is None:
        _fail(code, f"unknown {key}: {value}")
    return row


def _references(rules: list[dict], config: dict, catalog: dict) -> None:
    adapter = _validate(model.validate_adapter, config["adapter"])
    for source in config["discovery_sources"]:
        if source["operator_key"] != "operator":
            _fail(ErrorCode.INVALID, "discovery source belongs to another operator")
        target = source["private_target"]
        channel = _find(catalog["channels"], "channel_id", target["channel_id"])
        operator = channel["config"].get("operator_sender_id")
        if (parse_channel_id(target["channel_id"]) is None or push_target(target["channel_id"], target["chat"]).group
                or not channel["enabled"] or not operator or target["chat"] != operator):
            _fail(ErrorCode.INVALID, "discovery target must be the configured operator's private chat")
        parsed = parse_channel_id(target["channel_id"])
        allowed = _validate(sender_ids, parsed.channel_type,
                            channel["config"].get(SENDER_ID_FORMATS[parsed.channel_type].field))
        if operator not in allowed:
            _fail(ErrorCode.INVALID, "discovery operator is not in this channel's allowed senders")
        if adapter and source["system_id"] != adapter["system_id"]:
            _fail(ErrorCode.INVALID, "discovery source and adapter must name the same review system")
    for rule in rules:
        if rule["intent_type"] not in ("any", leads_model.RULE_INTENT_TYPE):
            kind = _find(catalog["types"], "key", rule["intent_type"])
            if not kind["enabled"] or kind["archived_at"] is not None:
                _fail(ErrorCode.INVALID, "rule intent type is disabled or archived")
        elif rule["intent_type"] == leads_model.RULE_INTENT_TYPE:
            kind = next((row for row in catalog["types"] if row["key"] == leads_model.RULE_INTENT_TYPE), None)
            if kind is not None and (not kind["enabled"] or kind["archived_at"] is not None):
                _fail(ErrorCode.INVALID, "MR review type is disabled or archived")
        if rule["scope"]["kind"] == "named_chats":
            for ref in rule["scope"]["chats"]:
                chat = next((row for row in catalog["chats"] if row["source_id"] == ref["source_id"]
                             and row["chat_id"] == ref["chat_id"]), None)
                if chat is None or chat["inclusion"] != "included" or chat["extraction"] != "included" or chat["state"] != "active":
                    _fail(ErrorCode.INVALID, "rule chat is unknown or excluded from authorized extraction")
        project = _find(catalog["projects"], "id", rule["project_id"]) if rule["project_id"] else None
        template = _find(catalog["templates"], "key", rule["template_key"]) if rule["template_key"] else None
        if template:
            _validate(Workflow.from_row, template)
        for condition in rule["conditions"]:
            if condition["kind"] == "internal_mr_open" and adapter:
                if not set(condition["allowed_domains"]) <= set(adapter["allowed_domains"]):
                    _fail(ErrorCode.INVALID, "rule domains exceed the adapter's allowed domains")
        if rule["action"] == "create_task":
            if not project or not project["work_dir"] or not project["auto_task_daily_cap"]:
                _fail(ErrorCode.INVALID, "automatic tasks require a project directory and a positive daily cap")
            contract = _find(catalog["contracts"], "workflow_key", rule["template_key"])
            _validate(model.validate_template_contract, {key: contract[key] for key in model.TEMPLATE_CONTRACT_SCHEMA["properties"]})
            if contract["workflow_digest"] != _digest(template["definition"]):
                _fail(ErrorCode.INVALID, "workflow changed; confirm its input and external-action contract again")


def _acceptance(config: dict, rules: list[dict], catalog: dict) -> list[str]:
    risks = ["reviewer_can_be_added", "addressed_requests_can_trigger", "named_chats_can_trigger", "source_migration"]
    templates = {rule["template_key"] for rule in rules if rule["enabled"] and rule["action"] == "create_task"}
    if any(row["workflow_key"] in templates and row["comment_mode"] == "direct" for row in catalog["contracts"]):
        risks.append("external_comments")
    if config["adapter"]:
        risks.append("adapter_execution")
    return risks


async def _prepare(conn, config, catalog, rules, command, operator_key, *, executor_available):
    """Compute the exact candidate state without changing any effective row."""
    config_fields = ("generation", "adapter", "discovery_sources", "activation_acceptance")
    original = deepcopy({"rules": rules, "config": {key: config[key] for key in config_fields},
                         "enabled": _flags(catalog)["enabled"]})
    reference_before = None
    kind = command["kind"]
    if kind in ("project_cap", "template_contract"):
        table, key, value = (("projects", "id", command["project_id"]) if kind == "project_cap"
                             else ("contracts", "workflow_key", command["workflow_key"]))
        reference_before = next((deepcopy(row) for row in catalog[table] if row[key] == value), None)
    config, catalog, rules = deepcopy(config), deepcopy(catalog), deepcopy(rules)
    deleted, rollback = None, None
    if kind == "rule_create":
        rules.append(_validate(model.validate_rule, {"id": command["generated_id"], "version": 1, **command["rule"]}))
    elif kind in ("rule_update", "rule_disable", "rule_delete", "rule_rollback"):
        row = _find(rules, "id", command["rule_id"], code=ErrorCode.NOT_FOUND)
        if row["version"] != command["expected_version"]:
            _fail(ErrorCode.CONFLICT, "rule version changed; preview again")
        if kind == "rule_update":
            row.update(command["fields"])
        elif kind == "rule_disable":
            row["enabled"] = False
        elif kind == "rule_delete":
            deleted = deepcopy(row)
            rules.remove(row)
        else:
            previous = await store.history(conn, row["id"])
            target = _find(previous["versions"], "version", command["target_version"])
            rollback = target["version"]
            row.update({key: target[key] for key in model.RULE_INPUT_SCHEMA["properties"] if key != "position"})
        row["version"] += 1
        if deleted:
            deleted["version"] = row["version"]
    elif kind == "rule_reorder":
        if len(command["ids"]) != len(rules) or set(command["ids"]) != {row["id"] for row in rules}:
            _fail(ErrorCode.INVALID, "reorder must contain every live rule exactly once")
        for position, rid in enumerate(command["ids"], 1):
            row = _find(rules, "id", rid)
            if row["position"] != position:
                row["position"], row["version"] = position, row["version"] + 1
    elif kind == "settings":
        if "adapter" in command:
            _validate(model.validate_adapter, command["adapter"])
        for field in ("adapter", "discovery_sources"):
            if field in command:
                config[field] = deepcopy(command[field])
        if "discovery_sources" in command:
            for source in config["discovery_sources"]:
                if source.get("operator_key", operator_key) != operator_key:
                    _fail(ErrorCode.INVALID, "discovery operator is determined by the server")
                source["operator_key"] = operator_key
    elif kind == "project_cap":
        project = _find(catalog["projects"], "id", command["project_id"])
        if project["auto_task_config_version"] != command["expected_config_version"]:
            _fail(ErrorCode.CONFLICT, "project configuration changed; preview again")
        project["auto_task_daily_cap"] = command["auto_task_daily_cap"]
        project["auto_task_config_version"] += 1
    elif kind == "template_contract":
        template = _find(catalog["templates"], "key", command["workflow_key"])
        _validate(Workflow.from_row, template)
        current = next((row for row in catalog["contracts"] if row["workflow_key"] == command["workflow_key"]), None)
        if (current["version"] if current else 0) != command["expected_contract_version"]:
            _fail(ErrorCode.CONFLICT, "template contract changed; preview again")
        replacement = {"workflow_key": command["workflow_key"], "version": command["expected_contract_version"] + 1,
                       "workflow_digest": _digest(template["definition"]), **command["contract"]}
        catalog["contracts"] = [row for row in catalog["contracts"] if row["workflow_key"] != command["workflow_key"]] + [replacement]
    rules = sorted(_validate(model.validate_rules, rules), key=lambda row: row["position"])
    # Turning the switch off must remain possible after a referenced object disappears.
    if not (kind == "settings" and command.get("enabled") is False):
        _references(rules, config, catalog)
    enabled = command.get("enabled", _flags(catalog)["enabled"]) if kind == "settings" else _flags(catalog)["enabled"]
    if enabled:
        failed = [item["code"] for item in _preconditions(config, catalog, executor_available) if not item["ok"]]
        if failed:
            _fail(ErrorCode.CONFLICT, "cannot enable intent rules: " + ", ".join(failed))
    required = _acceptance(config, rules, catalog) if enabled else []
    impact = {"ordered_rules": rules, "projects": [{"id": p["id"], "auto_task_daily_cap": p["auto_task_daily_cap"]}
        for p in catalog["projects"] if any(r["project_id"] == p["id"] for r in rules)],
        "templates": [{key: value for key, value in c.items() if key != "updated_at"} for c in catalog["contracts"]
                      if any(r["template_key"] == c["workflow_key"] for r in rules)],
        "verification": "configured" if config["adapter"] else "verification_unavailable",
        "warnings": ["reviewer 可被他人添加；私聊、真实提及和授权群请求可能触发评审。",
                     "模板可能直接发布评论，通知和撤销不能保证先于评论；approve/withdraw 仍需确认，禁止 merge 和修改 reviewer。",
                     "未接入共享入口的旧自动来源须由操作者先停用；迁移状态是声明，不是自动探测。"]}
    impact["warnings"].append("项目每日额度由全部规则及接入的来源共享，撤销任务不返还额度。")
    if not config["adapter"]:
        impact["warnings"].append("未配置核验适配器，全部 API 依赖降级确认。")
    else:
        impact["warnings"].append("适配器以服务账号在本机执行，不是沙箱；启用前须审核其实现、只读凭据和账号权限。")
    after = {"rules": rules, "config": {key: config[key] for key in config_fields}, "enabled": enabled}
    diff = {"kind": kind, "before": original, "after": after}
    if kind in ("project_cap", "template_contract"):
        table = "projects" if kind == "project_cap" else "contracts"
        key, value = ("id", command["project_id"]) if kind == "project_cap" else ("workflow_key", command["workflow_key"])
        diff["reference_before"] = ({name: item for name, item in reference_before.items()
                                      if name not in ("work_dir", "updated_at")} if reference_before else None)
        diff["reference_after"] = {name: item for name, item in _find(catalog[table], key, value).items()
                                   if name not in ("work_dir", "updated_at")}
    return {"config": config, "rules": rules, "catalog": catalog, "deleted": deleted, "rollback": rollback,
            "enabled": enabled, "required_acceptance": required, "impact": _plain(impact), "diff": _plain(diff)}


async def preview(expected_generation: int, change: dict, *, principal=None, db=None,
                  executor_available: bool = False) -> dict:
    operator_key = _operator(principal)
    _validate(model.validate_schema, {"expected_generation": expected_generation, "change": change}, model.PREVIEW_REQUEST_SCHEMA)
    command = _validate(model.validate_change, change)
    if command["kind"] == "rule_create":
        command["generated_id"] = str(uuid4())
    async with _connection(db or get_pool()) as conn, conn.transaction():
        config = await store.get_config(conn)
        if config["generation"] != expected_generation:
            _fail(ErrorCode.CONFLICT, "configuration generation changed; preview again")
        catalog = await _catalog(conn)
        prepared = await _prepare(conn, config, catalog, await store.read_rules(conn), command, operator_key,
                                  executor_available=executor_available)
        fingerprints = {key: _digest(rows) for key, rows in catalog.items()}
        fingerprints["runtime_ready"] = _ready(executor_available)
        token = await store.preview(conn, operator_key=operator_key, expected_generation=expected_generation,
            command=command, reference_fingerprints=fingerprints, diff={"change": prepared["diff"],
                "impact": prepared["impact"], "required_acceptance": prepared["required_acceptance"]}, now=_now())
        return _plain({"preview_id": str(token["id"]), "expires_at": token["expires_at"], "generation": expected_generation,
            "diff": prepared["diff"], "impact": prepared["impact"], "required_acceptance": prepared["required_acceptance"],
            "reference_fingerprints": fingerprints})


async def confirm(preview_id: str, acceptance: list[str], *, principal=None, db=None,
                  executor_available: bool = False) -> dict:
    operator_key = _operator(principal)
    _validate(model.validate_schema, {"preview_id": preview_id, "acceptance": acceptance}, model.CONFIRM_REQUEST_SCHEMA)
    async with _connection(db or get_pool()) as conn, conn.transaction():
        await store.lock_admission(conn)
        config = await store.lock_config(conn)
        token = await store.get_preview(conn, preview_id, lock=True)
        if token is None:
            _fail(ErrorCode.NOT_FOUND, "preview not found")
        if token["operator_key"] != operator_key:
            _fail(ErrorCode.FORBIDDEN, "preview belongs to another operator")
        if token["consumed_at"] is not None:
            return token["result"]
        now = _now()
        if token["expires_at"] <= now:
            _fail(ErrorCode.EXPIRED, "preview expired; preview again")
        if token["expected_generation"] != config["generation"]:
            _fail(ErrorCode.CONFLICT, "configuration generation changed; preview again")
        catalog = await _catalog(conn, lock=True)
        fingerprints = {key: _digest(rows) for key, rows in catalog.items()}
        fingerprints["runtime_ready"] = _ready(executor_available)
        if fingerprints != token["reference_fingerprints"]:
            _fail(ErrorCode.CONFLICT, "a referenced object, authorization or runtime capability changed; preview again")
        command = token["command"]
        prepared = await _prepare(conn, config, catalog, await store.read_rules(conn), command, operator_key,
                                  executor_available=executor_available)
        if set(prepared["required_acceptance"]) - set(acceptance):
            _fail(ErrorCode.INVALID, "explicitly accept every external-action risk shown by the preview")
        previous = {row["id"]: row for row in await store.read_rules(conn)}
        for rule in prepared["rules"]:
            if rule != previous.get(rule["id"]):
                await store.append_rule_version(conn, rule, edited_by=operator_key,
                    rolled_back_from=prepared["rollback"] if command.get("rule_id") == rule["id"] else None)
        if prepared["deleted"]:
            await store.append_rule_version(conn, prepared["deleted"], edited_by=operator_key, deleted=True)
        if command["kind"] == "project_cap":
            await conn.execute("UPDATE projects SET auto_task_daily_cap=$2,auto_task_config_version=auto_task_config_version+1 "
                               "WHERE id=$1", command["project_id"], command["auto_task_daily_cap"])
        elif command["kind"] == "template_contract":
            contract = _find(prepared["catalog"]["contracts"], "workflow_key", command["workflow_key"])
            await conn.execute("INSERT INTO intent_rule_template_contracts(workflow_key,version,workflow_digest,input_kind,"
                "comment_mode,approve_requires_confirmation,withdraw_requires_confirmation,allow_merge,allow_reviewer_changes) "
                "VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9) ON CONFLICT(workflow_key) DO UPDATE SET version=excluded.version,"
                "workflow_digest=excluded.workflow_digest,input_kind=excluded.input_kind,comment_mode=excluded.comment_mode,"
                "approve_requires_confirmation=excluded.approve_requires_confirmation,withdraw_requires_confirmation=excluded.withdraw_requires_confirmation,"
                "allow_merge=excluded.allow_merge,allow_reviewer_changes=excluded.allow_reviewer_changes,updated_at=now()",
                *(contract[key] for key in ("workflow_key", "version", "workflow_digest", "input_kind", "comment_mode",
                  "approve_requires_confirmation", "withdraw_requires_confirmation", "allow_merge", "allow_reviewer_changes")))
        if command["kind"] == "settings" and "enabled" in command:
            await conn.execute("INSERT INTO system_settings(key,value) VALUES($1,$2) ON CONFLICT(key) "
                "DO UPDATE SET value=excluded.value,updated_at=now()", leads_model.INTENTS_RULES_ENABLED_KEY,
                "true" if command["enabled"] else "false")
        generation = config["generation"] + 1
        candidate = prepared["config"]
        await store.update_config(conn, generation=generation, adapter=candidate["adapter"],
            discovery_sources=candidate["discovery_sources"],
            activation_acceptance=acceptance if prepared["enabled"] else None, now=now)
        result = {"generation": generation,
                  "settings": await settings_get(principal=principal, db=conn, executor_available=executor_available),
                  "rules": await store.read_rules(conn)}
        result = _plain(result)
        await store.confirm(conn, preview_id=preview_id, operator_key=operator_key, generation=generation,
                            change=command, result=result, now=now)
        return result


async def hits(*, principal=None, db=None, **filters) -> dict:
    _operator(principal)
    _validate(model.validate_schema, filters, model.HITS_LIST_REQUEST_SCHEMA)
    return _plain(await store.list_hits(db or get_pool(), **filters))


async def get_hit(hit_id: int, *, principal=None, db=None) -> dict:
    _operator(principal)
    if type(hit_id) is not int or hit_id < 1:
        _fail(ErrorCode.INVALID, "hit_id must be a positive integer")
    result = await store.get_hit(db or get_pool(), hit_id)
    if result is None:
        _fail(ErrorCode.NOT_FOUND, "intent rule hit not found")
    return _plain(result)


def _time(value: str | datetime | None, default: datetime) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) else value or default
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError
        return parsed.astimezone(timezone.utc)
    except (ValueError, TypeError, AttributeError):
        _fail(ErrorCode.INVALID, "statistics times must be timezone-aware ISO timestamps")


async def hit_stats(*, principal=None, db=None, rule_id=None, since=None, until=None) -> dict:
    operator_key = _operator(principal)
    if rule_id is not None:
        _validate(model.validate_schema, rule_id, model.UUID_SCHEMA)
    end = _time(until, _now())
    start = _time(since, end - timedelta(days=7))
    if not timedelta(0) < end - start <= timedelta(days=90):
        _fail(ErrorCode.INVALID, "statistics range must be positive and at most 90 days")
    conn = db or get_pool()
    result = await store.hit_stats(conn, rule_id=rule_id, since=start, until=end)
    from . import turn_context
    zone_info = turn_context.zone()
    zone = getattr(zone_info, "key", None) or str(zone_info)
    local_day = _now().astimezone(zone_info).date()
    result["projects"] = [{"project_id": row["id"], "local_day": local_day.isoformat(),
        "timezone": zone, "used": row["used"], "cap": row["auto_task_daily_cap"]}
        for row in await conn.fetch("SELECT p.id,p.auto_task_daily_cap,COALESCE(d.used,0) AS used "
            "FROM projects p LEFT JOIN intent_rule_project_days d ON d.project_id=p.id "
            "AND d.operator_key=$1 AND d.local_day=$2 WHERE p.auto_task_daily_cap IS NOT NULL ORDER BY p.id",
            operator_key, local_day)]
    return _plain(result)
