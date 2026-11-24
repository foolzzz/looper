"""Closed operator-only rule management. Every operation stays inside Looper."""
from .. import intent_rules as service, intent_rules_model as model
from ..action_levels import ActionLevel
from ..api_v1 import Operation, _object, _array, _STR, _BOOL, _INT


def _nullable(schema):
    return {"anyOf": [schema, {"type": "null"}]}


_TIME = model.TIMESTAMP_SCHEMA
_UUID = model.UUID_SCHEMA
_NULL_INT = {"type": ["integer", "null"]}
_CONTRACT_FIELDS = {"workflow_key": _STR, "version": model.POSITIVE_INT, "workflow_digest": _STR,
                    **model.TEMPLATE_CONTRACT_SCHEMA["properties"]}
_CONTRACT_REFERENCE = _object(_CONTRACT_FIELDS, closed=True)
_CONTRACT = _object({**_CONTRACT_FIELDS, "updated_at": _TIME}, list(_CONTRACT_FIELDS), closed=True)
_SETTINGS = _object({"enabled": _BOOL, "intents_enabled": _BOOL, "delivery_enabled": _BOOL,
    "runtime_ready": _BOOL, "generation": _INT, "adapter": _nullable(model.ADAPTER_SCHEMA),
    "discovery_sources": model.DISCOVERY_SOURCES_SCHEMA,
    "preconditions": _array(_object({"code": _STR, "ok": _BOOL}, closed=True)),
    "acceptance": _nullable(model.ACCEPTANCE_SCHEMA)}, closed=True)
_PROJECT = _object({"id": _STR, "name": _STR, "auto_task_daily_cap": _NULL_INT, "config_version": _INT}, closed=True)
_LIST = _object({"generation": _INT, "runtime_ready": _BOOL, "rules": _array(model.RULE_SCHEMA),
    "fallback": model.FALLBACK_SCHEMA,
    "configurable_types": _array(_object({"key": _STR, "name": _STR, "enabled": _BOOL,
                                         "available_when_rules_on": _BOOL}, closed=True)),
    "projects": _array(_PROJECT), "templates": _array(_CONTRACT), "settings": _SETTINGS}, closed=True)
_CURRENT = _object({**model.RULE_SCHEMA["properties"], "position": _NULL_INT,
    "deleted_at": _nullable(_TIME)}, closed=True)
_VERSION = _object({**model.RULE_SCHEMA["properties"], "rolled_back_from": _NULL_INT,
    "edited_by": _STR, "created_at": _TIME}, closed=True)
_HISTORY = _object({"current": _CURRENT, "versions": _array(_VERSION)}, closed=True)
_CONFIG = _object({"generation": model.NONNEGATIVE_INT, "adapter": _nullable(model.ADAPTER_SCHEMA),
    "discovery_sources": model.DISCOVERY_SOURCES_SCHEMA,
    "activation_acceptance": _nullable(model.ACCEPTANCE_SCHEMA)}, closed=True)
_CONFIG_STATE = _object({"rules": _array(model.RULE_SCHEMA), "config": _CONFIG, "enabled": _BOOL}, closed=True)
_PROJECT_REFERENCE = _object({"id": _STR, "name": _STR, "auto_task_daily_cap": _NULL_INT,
    "auto_task_config_version": model.NONNEGATIVE_INT}, closed=True)
_DIFF = _object({"kind": {"type": "string", "enum": [kind.value for kind in model.ChangeKind]},
    "before": _CONFIG_STATE, "after": _CONFIG_STATE,
    "reference_before": {"anyOf": [_PROJECT_REFERENCE, _CONTRACT_REFERENCE, {"type": "null"}]},
    "reference_after": {"anyOf": [_PROJECT_REFERENCE, _CONTRACT_REFERENCE]}},
    ["kind", "before", "after"], closed=True)
_IMPACT = _object({"ordered_rules": _array(model.RULE_SCHEMA),
    "projects": _array(_object({"id": _STR, "auto_task_daily_cap": _NULL_INT}, closed=True)),
    "templates": _array(_CONTRACT_REFERENCE),
    "verification": {"type": "string", "enum": ["configured", "verification_unavailable"]},
    "warnings": _array(_STR)}, closed=True)
_FINGERPRINTS = _object({**dict.fromkeys(
    ("projects", "templates", "contracts", "types", "chats", "sources", "channels", "dependencies"),
    {"type": "string", "pattern": r"^[0-9a-f]{64}$(?![\s\S])"}), "runtime_ready": _BOOL}, closed=True)
_PREVIEW = _object({"preview_id": _UUID, "expires_at": _TIME, "generation": _INT,
    "diff": _DIFF, "impact": _IMPACT, "required_acceptance": model.ACCEPTANCE_SCHEMA,
    "reference_fingerprints": _FINGERPRINTS}, closed=True)
_CONFIRM = _object({"generation": _INT, "settings": _SETTINGS, "rules": _array(model.RULE_SCHEMA)}, closed=True)
_EXECUTION = _object({"id": model.POSITIVE_INT, "task_id": _STR,
    "mode": {"enum": ["auto", "manual"]}, "mr_key": model.MR_IDENTITY_SCHEMA,
    "quota": _nullable(_object({"day": _STR, "project_id": _STR, "units": {"enum": [0,1]}}, closed=True)),
    "start_state": {"enum": ["pending","requested","started","failed","cancelled"]},
    "notice": _nullable(_object({"state": {"enum": ["pending","sending","sent","digest","failed","uncertain"]},
        "decision": _nullable(_STR), "reason": _nullable(_STR)}, closed=True)),
    "revoke": _object({"state": {"enum": ["none","requested","stopped","already_terminal","failed"]},
        "reason": _nullable(_STR)}, closed=True), "last_error_code": _nullable(_STR)}, closed=True)
_HIT = _object({"id": _INT, "created_at": _TIME, "rule_id": _nullable(_UUID), "rule_version": _NULL_INT,
    "selection": {"enum": [item.value for item in model.Selection]}, "action": {"enum": list(model.ACTIONS)},
    "reason": _nullable(_STR), "intent_id": _NULL_INT, "discovery_id": _NULL_INT,
    "task_id": _nullable(_STR), "mr_identity": _nullable(model.MR_IDENTITY_SCHEMA),
    "execution_state": _nullable(_STR)}, closed=True)
_CONDITION = model.CONDITION_RESULT_SCHEMA
_SOURCES = _object({"event_seq": _INT, "source_id": _STR, "chat_id": _STR,
    "external_id": _STR, "sent_at": _TIME}, closed=True)
_HIT_DETAIL = _object({"hit": _HIT, "conditions": _array(_CONDITION), "sources": _array(_SOURCES),
    "execution": _nullable(_EXECUTION), "attempts": _array(model.ATTEMPT_RESULT_SCHEMA)}, closed=True)
_STATS = _object({"available": _BOOL,
    "range": _object({"since": _TIME, "until": _TIME}, closed=True),
    "rules": _array(_object({"rule_id": _nullable(_UUID), "version": _NULL_INT, "hits": _INT,
        **dict.fromkeys(("auto_created", "deduped", "daily_cap", "verification_unavailable"), _NULL_INT)}, closed=True)),
    "projects": _array(_object({"project_id": _STR, "local_day": _STR, "timezone": _STR,
        "used": _NULL_INT, "cap": _INT}, closed=True))}, closed=True)


def _op(domain, action, request, result, *, write=False):
    summary = f"Manage intent rules: {action.replace('_', ' ')}." if domain == "intent_rules" else f"Read intent rule hits: {action}."
    # These operations administer local admission, tasks and audit; no review-system write is permitted.
    return Operation(f"{domain}.{action}", "POST", f"/api/v1/{domain}/{action}", summary, summary,
        request, result, risk="write" if write else "read", operator_only=True, cli=False, level=ActionLevel.INSIDE)


RESULTS = {}
OPERATIONS = (
    _op("intent_rules", "list", _object({}, closed=True), _LIST),
    _op("intent_rules", "settings_get", _object({}, closed=True), _SETTINGS),
    _op("intent_rules", "history", _object({"rule_id": _UUID}, closed=True), _HISTORY),
    _op("intent_rules", "preview", model.PREVIEW_REQUEST_SCHEMA, _PREVIEW, write=True),
    _op("intent_rules", "confirm", model.CONFIRM_REQUEST_SCHEMA, _CONFIRM, write=True),
    _op("intent_rule_hits", "list", model.HITS_LIST_REQUEST_SCHEMA,
        _object({"hits": _array(_HIT), "more": _BOOL}, closed=True)),
    _op("intent_rule_hits", "get", _object({"hit_id": model.POSITIVE_INT}, closed=True), _HIT_DETAIL),
    _op("intent_rule_hits", "stats", _object({"rule_id": _UUID, "since": _TIME, "until": _TIME}, [], closed=True), _STATS),
    _op("intent_rules", "discovery_submit", _object({"discovery_source_id":model.ID_SCHEMA,"source_ref":model.ID_SCHEMA,
        "system_id":model.ID_SCHEMA,"mr_url":_STR}, closed=True),
        _object({"discovery_id":model.POSITIVE_INT,"state":{"enum":["pending","confirmation","tasked","recorded","rejected"]},"task_id":_nullable(_STR)},closed=True),write=True),
    _op("intent_rules", "discovery_get", _object({"discovery_id":model.POSITIVE_INT},closed=True),
        _object({"discovery":_object({"id":model.POSITIVE_INT,"source_ref":_STR,"system_id":_STR,"mr_url":_STR,
            "state":{"enum":["pending","confirmation","tasked","recorded","rejected"]},"task_id":_nullable(_STR)},closed=True),"hit_id":_nullable(model.POSITIVE_INT),
            "execution":_nullable(_EXECUTION)},closed=True)),
    _op("intent_rules", "discovery_build", _object({"discovery_id":model.POSITIVE_INT,"project_id":model.ID_SCHEMA,
        "template_key":model.ID_SCHEMA},closed=True),_object({"task_id":_STR,"execution":_EXECUTION},closed=True),write=True),
    _op("intent_rules", "discovery_reject", _object({"discovery_id":model.POSITIVE_INT},closed=True),
        _object({"discovery_id":model.POSITIVE_INT,"state":{"const":"rejected"}},closed=True),write=True),
    _op("intent_rules", "revoke", _object({"execution_id":model.POSITIVE_INT},closed=True),
        _object({"execution_id":model.POSITIVE_INT,"task_id":_STR,
            "state":{"enum":["requested","stopped","already_terminal","failed"]},"reason":_nullable(_STR)},closed=True),write=True),
    _op("intent_rules", "claim_revision", _object({"task_id":model.ID_SCHEMA,"system_id":model.ID_SCHEMA,"mr_url":_STR,
        "expected_revision":model.ID_SCHEMA},closed=True),
        _object({"task_id":_STR,"mr_key":model.MR_IDENTITY_SCHEMA,"existing":_BOOL},closed=True),write=True),
    _op("intent_rules", "migration_preview", _object({"discovery_source_id":model.ID_SCHEMA,
        "entries":{"type":"array","minItems":1,"maxItems":100,"items":_object({"task_id":model.ID_SCHEMA,
            "system_id":model.ID_SCHEMA,"mr_url":_STR,"revision":model.ID_SCHEMA},
            ["task_id","system_id","mr_url"],closed=True)}},closed=True),
        _object({"preview_id":_UUID,"verified":_INT,"blocked":_INT,"conflicts":_INT,
            "impact":_array(_object({"task_id":_STR,"identity":_nullable(model.MR_IDENTITY_SCHEMA),
                "status":{"enum":["verified","blocked","conflict"]},"task_fingerprint":_nullable(_STR)},closed=True))},closed=True),write=True),
    _op("intent_rules", "migration_confirm", _object({"preview_id":_UUID},closed=True),
        _object({"registered":_INT,"blocked":_INT,"conflicts":_INT},closed=True),write=True),
)


async def run(server, operation, arguments, principal, headers):
    # The service checks again, including direct callers outside HTTP dispatch.
    service._operator(principal)
    domain, action = operation.operation_id.split(".")
    values = dict(arguments)
    if domain == "intent_rules" and action in {"discovery_submit","discovery_get","discovery_build","discovery_reject",
                                              "revoke","claim_revision","migration_preview","migration_confirm"}:
        from .. import intent_rule_engine as engine
        if action == "discovery_submit":
            return await engine.submit_discovery(principal=principal, **values)
        function = getattr(engine, action)
        return service._plain(await function(principal=principal, **values))
    if domain == "intent_rules":
        function = {"list": service.list_rules, "settings_get": service.settings_get, "history": service.history,
                    "preview": service.preview, "confirm": service.confirm}[action]
        if action != "history":
            values["executor_available"] = getattr(server, "_intent_rule_executor", None) is not None
        if action == "preview":
            values["change"] = service._validate(model.validate_change, values["change"])
    else:
        function = {"list": service.hits, "get": service.get_hit, "stats": service.hit_stats}[action]
    return await function(principal=principal, **values)
