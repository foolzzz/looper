"""Pure, closed configuration contracts shared by rule management and execution.

A template contract declares the existing workflow's boundaries. It does not install
an executor or add a comment approval gate to that workflow.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import PurePath
from typing import Any, Mapping
from urllib.parse import urlsplit

from jsonschema import Draft202012Validator, FormatChecker, validators


class Action(StrEnum):
    PUSH_FOR_CONFIRMATION = "push_for_confirmation"
    CREATE_TASK = "create_task"
    RECORD_ONLY = "record_only"


class ConditionKind(StrEnum):
    INTERNAL_MR_OPEN = "internal_mr_open"
    OPERATOR_IS_REVIEWER = "operator_is_reviewer"
    ADDRESSED_TO_OPERATOR = "addressed_to_operator"
    SENDER_IN_ALLOWLIST = "sender_in_allowlist"
    MR_REPOSITORY_IN_ALLOWLIST = "mr_repository_in_allowlist"


class ConditionState(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    UNAVAILABLE = "unavailable"


class ConditionReason(StrEnum):
    PASSED = "passed"
    CONDITION_NOT_MET = "condition_not_met"
    VERIFICATION_UNAVAILABLE = "verification_unavailable"
    AUTHENTICATION_FAILED = "authentication_failed"
    PERMISSION_DENIED = "permission_denied"
    NOT_READABLE = "not_readable"
    CANNOT_DECIDE = "cannot_decide"
    UPSTREAM_ERROR = "upstream_error"
    INVALID_RESPONSE = "invalid_response"
    ADAPTER_NOT_CONFIGURED = "adapter_not_configured"
    MR_NOT_FOUND = "mr_not_found"
    MR_NOT_OPEN = "mr_not_open"
    REVIEWER_MISSING = "reviewer_missing"
    NOT_ADDRESSED = "not_addressed"
    SENDER_NOT_ALLOWED = "sender_not_allowed"
    REPOSITORY_NOT_ALLOWED = "repository_not_allowed"
    SCOPE_MISMATCH = "scope_mismatch"
    TYPE_MISMATCH = "type_mismatch"
    AMBIGUOUS_TARGET = "ambiguous_target"


class Selection(StrEnum):
    MATCHED = "matched"
    UNMATCHED = "unmatched"
    FALLBACK = "fallback"
    MANUAL = "manual"


class ExecutionReason(StrEnum):
    MATCHED = "matched"
    NO_MATCH = "no_match"
    TYPE_MISMATCH = "type_mismatch"
    SCOPE_MISMATCH = "scope_mismatch"
    CONDITION_FAILED = "condition_failed"
    FLAGGED = "flagged"
    AMBIGUOUS_TARGET = "ambiguous_target"
    VERIFICATION_UNAVAILABLE = "verification_unavailable"
    ADAPTER_MISSING = "adapter_missing"
    ADAPTER_START_FAILED = "adapter_start_failed"
    VERIFICATION_TIMEOUT = "verification_timeout"
    VERIFICATION_OUTPUT_LIMIT = "verification_output_limit"
    VERIFICATION_EXIT = "verification_exit"
    VERIFICATION_PROTOCOL = "verification_protocol"
    AUTHENTICATION_FAILED = "authentication_failed"
    PERMISSION_DENIED = "permission_denied"
    NOT_READABLE = "not_readable"
    CANNOT_DECIDE = "cannot_decide"
    UPSTREAM_ERROR = "upstream_error"
    MR_MISSING = "mr_missing"
    MR_NOT_OPEN = "mr_not_open"
    REPOSITORY_DENIED = "repository_denied"
    NOT_REVIEWER = "not_reviewer"
    NOT_ADDRESSED = "not_addressed"
    SENDER_DENIED = "sender_denied"
    CONFIG_CHANGED = "config_changed"
    PROJECT_CHANGED = "project_changed"
    TEMPLATE_CHANGED = "template_changed"
    SOURCE_REVOKED = "source_revoked"
    SWITCH_OFF = "switch_off"
    DELIVERY_OFF = "delivery_off"
    INTENTS_OFF = "intents_off"
    REVISION_CHANGED = "revision_changed"
    DEDUPED = "deduped"
    UNKNOWN_LEGACY_REVISION = "unknown_legacy_revision"
    DAILY_CAP = "daily_cap"
    AUDIT_UNAVAILABLE = "audit_unavailable"
    START_FAILED = "start_failed"
    NOTIFICATION_FAILED = "notification_failed"
    REVOKE_FAILED = "revoke_failed"
    ALREADY_TERMINAL = "already_terminal"


class Acceptance(StrEnum):
    REVIEWER_CAN_BE_ADDED = "reviewer_can_be_added"
    ADDRESSED_REQUESTS_CAN_TRIGGER = "addressed_requests_can_trigger"
    NAMED_CHATS_CAN_TRIGGER = "named_chats_can_trigger"
    EXTERNAL_COMMENTS = "external_comments"
    ADAPTER_EXECUTION = "adapter_execution"
    SOURCE_MIGRATION = "source_migration"


class MigrationState(StrEnum):
    UNCONFIGURED = "unconfigured"
    SHARED_ENTRY = "shared_entry"
    OLD_DISABLED = "old_disabled"


class CommentMode(StrEnum):
    DIRECT = "direct"
    APPROVAL_GATE = "approval_gate"


class ChangeKind(StrEnum):
    RULE_CREATE = "rule_create"
    RULE_UPDATE = "rule_update"
    RULE_REORDER = "rule_reorder"
    RULE_DISABLE = "rule_disable"
    RULE_DELETE = "rule_delete"
    RULE_ROLLBACK = "rule_rollback"
    SETTINGS = "settings"
    PROJECT_CAP = "project_cap"
    TEMPLATE_CONTRACT = "template_contract"


ACTIONS = tuple(item.value for item in Action)
CONDITION_REASONS = tuple(item.value for item in ConditionReason)
ACCEPTANCE_VALUES = tuple(item.value for item in Acceptance)


def _object(properties: Mapping[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {"type": "object", "properties": dict(properties), "additionalProperties": False,
            "required": list(properties) if required is None else required}


def _enum(enum: type[StrEnum]) -> dict[str, Any]:
    return {"type": "string", "enum": [item.value for item in enum]}


def _array(items: Mapping[str, Any], *, minimum: int = 0, unique: bool = False) -> dict[str, Any]:
    return {"type": "array", "items": dict(items), "minItems": minimum, "uniqueItems": unique}


def _nullable(schema: Mapping[str, Any]) -> dict[str, Any]:
    return {"anyOf": [dict(schema), {"type": "null"}]}


ID_SCHEMA = {"type": "string", "minLength": 1, "pattern": r"^[^\s\x00-\x1f\x7f*]+$(?![\s\S])"}
UUID_SCHEMA = {"type": "string", "format": "uuid"}
POSITIVE_INT = {"type": "integer", "minimum": 1, "maximum": 2**63 - 1}
NONNEGATIVE_INT = {"type": "integer", "minimum": 0, "maximum": 2**63 - 1}
_RULE_INT = {**POSITIVE_INT, "maximum": 2**31 - 1}
BOOL = {"type": "boolean"}
HOST_SCHEMA = {"type": "string", "pattern": (
    r"^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)*"
    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$(?![\s\S])"
)}
TIMESTAMP_SCHEMA = {"type": "string", "format": "date-time"}
CHAT_REFERENCE_SCHEMA = _object({"source_id": ID_SCHEMA, "chat_id": ID_SCHEMA})
SCOPE_SCHEMA = {"oneOf": [
    _object({"kind": {"const": "all"}}),
    _object({"kind": {"const": "named_chats"},
             "chats": _array(CHAT_REFERENCE_SCHEMA, minimum=1, unique=True)}),
]}
CONDITION_SCHEMA = {"oneOf": [
    _object({"kind": {"const": "internal_mr_open"},
             "allowed_domains": _array(HOST_SCHEMA, minimum=1, unique=True)}),
    _object({"kind": {"const": "operator_is_reviewer"}}),
    _object({"kind": {"const": "addressed_to_operator"}}),
    _object({"kind": {"const": "sender_in_allowlist"},
             "sender_ids": _array(ID_SCHEMA, minimum=1, unique=True)}),
    _object({"kind": {"const": "mr_repository_in_allowlist"},
             "repository_ids": _array(ID_SCHEMA, minimum=1, unique=True)}),
]}
RULE_FIELDS = {"enabled": BOOL, "position": _RULE_INT, "intent_type": ID_SCHEMA,
               "scope": SCOPE_SCHEMA, "conditions": _array(CONDITION_SCHEMA), "action": _enum(Action),
               "project_id": _nullable(ID_SCHEMA), "template_key": _nullable(ID_SCHEMA)}
# The contains predicates only select kind; CONDITION_SCHEMA above closes each actual condition.
_INTERNAL_CONDITION = {"contains": {"properties": {"kind": {"const": "internal_mr_open"}}, "required": ["kind"]}}
_RULE_DEPENDENCIES = [
    {"if": {"properties": {"action": {"const": "create_task"}}},
     "then": {"properties": {"intent_type": {"const": "mr_review"}, "conditions": _INTERNAL_CONDITION,
                              "project_id": ID_SCHEMA, "template_key": ID_SCHEMA}}},
    {"if": {"properties": {"action": {"const": "record_only"}}},
     "then": {"properties": {"project_id": {"type": "null"}, "template_key": {"type": "null"}}}},
    {"if": {"properties": {"conditions": {"contains": {"properties": {"kind": {"enum": [
        "operator_is_reviewer", "mr_repository_in_allowlist"]}}, "required": ["kind"]}}}},
     "then": {"properties": {"conditions": _INTERNAL_CONDITION}}},
]
RULE_INPUT_SCHEMA = _object(RULE_FIELDS) | {"allOf": _RULE_DEPENDENCIES}
RULE_SCHEMA = _object({"id": UUID_SCHEMA, "version": _RULE_INT, **RULE_FIELDS}) | {"allOf": _RULE_DEPENDENCIES}
RULE_FIELDS_SCHEMA = _object(RULE_FIELDS, []) | {"minProperties": 1}
FALLBACK_SCHEMA = _object({"id": {"const": "fallback"}, "version": {"const": 1},
    "enabled": {"const": True}, "position": POSITIVE_INT, "intent_type": {"const": "any"},
    "scope": _object({"kind": {"const": "all"}}), "conditions": {"type": "array", "maxItems": 0},
    "action": {"const": "push_for_confirmation"}, "project_id": {"type": "null"},
    "template_key": {"type": "null"}})

ROUTE_SEGMENT_SCHEMA = {"oneOf": [
    _object({"kind": {"const": "literal"}, "value": {"type": "string", "pattern": r"^[A-Za-z0-9._~-]+$(?![\s\S])"}}),
    _object({"kind": {"const": "id"}, "name": {"type": "string", "pattern": r"^[a-z][a-z0-9_]*$(?![\s\S])"}}),
]}
_ENV_NAME = {"type": "string", "pattern": r"^[A-Z_][A-Z0-9_]*$(?![\s\S])"}
_PLAIN_ENV_NAME = {"type": "string", "pattern": (
    r"^(?!.*(?:KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL))[A-Z_][A-Z0-9_]*$(?![\s\S])"
)}
ADAPTER_SCHEMA = _object({
    "system_id": ID_SCHEMA,
    "executable": {"type": "string", "pattern": r"^(?:/[^\x00\r\n]+|[A-Za-z0-9_.-]+)$(?![\s\S])"},
    "args": _array({"type": "string", "pattern": r"^[^\x00]*$(?![\s\S])"}),
    "allowed_domains": _array(HOST_SCHEMA, minimum=1, unique=True),
    "route": _array(ROUTE_SEGMENT_SCHEMA, minimum=1), "operator_id": ID_SCHEMA,
    # This is the reference-only wire format, not an installed credential resolver.
    "credential_env": {"type": "object", "propertyNames": _ENV_NAME,
                       "additionalProperties": {"type": "string", "pattern": r"^secret:[A-Za-z0-9._:/-]+$(?![\s\S])"}},
    "env": {"type": "object", "propertyNames": _PLAIN_ENV_NAME,
            "additionalProperties": {"type": "string", "pattern": r"^[^\x00]*$(?![\s\S])"}},
})
TEMPLATE_CONTRACT_SCHEMA = _object({
    "input_kind": {"const": "verified_mr"}, "comment_mode": _enum(CommentMode),
    "approve_requires_confirmation": {"type": "boolean", "const": True},
    "withdraw_requires_confirmation": {"type": "boolean", "const": True},
    "allow_merge": {"type": "boolean", "const": False},
    "allow_reviewer_changes": {"type": "boolean", "const": False},
})
DISCOVERY_SOURCE_SCHEMA = _object({
    "id": ID_SCHEMA, "system_id": ID_SCHEMA, "enabled": BOOL, "operator_key": ID_SCHEMA,
    "private_target": _object({"channel_id": ID_SCHEMA, "chat": ID_SCHEMA}),
    "migration_state": _enum(MigrationState),
}, ["id", "system_id", "enabled", "private_target", "migration_state"])
DISCOVERY_SOURCES_SCHEMA = _array(DISCOVERY_SOURCE_SCHEMA)
ACCEPTANCE_SCHEMA = _array(_enum(Acceptance), unique=True)


def _change(kind: str, fields: Mapping[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    properties = {"kind": {"const": kind}, **fields}
    return _object(properties, None if required is None else ["kind", *required])


_VERSIONED_RULE = {"rule_id": UUID_SCHEMA, "expected_version": _RULE_INT}
CHANGE_SCHEMA = {"oneOf": [
    _change("rule_create", {"rule": RULE_INPUT_SCHEMA}),
    _change("rule_update", {**_VERSIONED_RULE, "fields": RULE_FIELDS_SCHEMA}),
    _change("rule_reorder", {"ids": _array(UUID_SCHEMA, unique=True)}),
    _change("rule_disable", _VERSIONED_RULE),
    _change("rule_delete", _VERSIONED_RULE),
    _change("rule_rollback", {**_VERSIONED_RULE, "target_version": _RULE_INT}),
    _change("settings", {"enabled": BOOL, "adapter": _nullable(ADAPTER_SCHEMA),
                          "discovery_sources": DISCOVERY_SOURCES_SCHEMA}, []) | {"minProperties": 2},
    _change("project_cap", {"project_id": ID_SCHEMA, "expected_config_version": NONNEGATIVE_INT,
                             "auto_task_daily_cap": _nullable(_RULE_INT)}),
    _change("template_contract", {"workflow_key": ID_SCHEMA, "expected_contract_version": NONNEGATIVE_INT,
                                   "contract": TEMPLATE_CONTRACT_SCHEMA}),
]}
PREVIEW_REQUEST_SCHEMA = _object({"expected_generation": NONNEGATIVE_INT, "change": CHANGE_SCHEMA})
CONFIRM_REQUEST_SCHEMA = _object({"preview_id": UUID_SCHEMA, "acceptance": ACCEPTANCE_SCHEMA})
HITS_LIST_REQUEST_SCHEMA = _object({
    "rule_id": UUID_SCHEMA, "action": _enum(Action), "reason": ID_SCHEMA,
    "before_id": POSITIVE_INT, "limit": {"type": "integer", "minimum": 1, "maximum": 100},
}, [])
MR_IDENTITY_SCHEMA = _object({"system_id": ID_SCHEMA, "repository_id": ID_SCHEMA, "mr_id": ID_SCHEMA,
    "revision": ID_SCHEMA, "canonical_url": {"type": "string", "format": "uri", "pattern": r"^https://"}})
HIT_SCHEMA = _object({
    "evaluation_key": ID_SCHEMA, "intent_id": _nullable(POSITIVE_INT), "discovery_id": _nullable(POSITIVE_INT),
    "generation": NONNEGATIVE_INT, "rule_id": _nullable(UUID_SCHEMA), "rule_version": _nullable(_RULE_INT),
    "selection": _enum(Selection), "action": _enum(Action), "reason": _nullable(_enum(ExecutionReason)),
    "verified_at": _nullable(TIMESTAMP_SCHEMA), "mr_identity": _nullable(MR_IDENTITY_SCHEMA),
})
CONDITION_RESULT_SCHEMA = _object({
    "rule_position": _RULE_INT, "condition_position": _RULE_INT, "rule_id": UUID_SCHEMA,
    "rule_version": _RULE_INT, "kind": _enum(ConditionKind), "result": _enum(ConditionState),
    "reason": _enum(ConditionReason), "verified_at": _nullable(TIMESTAMP_SCHEMA),
})
ATTEMPT_RESULT_SCHEMA = _object({
    "rule_id": UUID_SCHEMA, "rule_version": _RULE_INT, "rule_position": _RULE_INT,
    "result": _enum(ConditionState), "reason": _enum(ExecutionReason),
})



# Match the transport and PostgreSQL: integral JSON floats are not Python integers.
_Validator = validators.extend(Draft202012Validator, type_checker=Draft202012Validator.TYPE_CHECKER.redefine(
    "integer", lambda _checker, value: type(value) is int))


def validate_schema(value: Any, schema: Mapping[str, Any]) -> Any:
    """Validate without coercion; diagnostics never echo configuration values."""
    failure = next(_Validator(schema, format_checker=FormatChecker()).iter_errors(value), None)
    if failure is not None:
        location = ".".join(str(part) for part in failure.absolute_path) or "configuration"
        raise ValueError(f"Invalid intent rule {location}")
    return deepcopy(value)


def validate_rule(value: Any, *, complete: bool = True) -> dict[str, Any]:
    return validate_schema(value, RULE_SCHEMA if complete else RULE_INPUT_SCHEMA)


def validate_rules(value: Any) -> list[dict[str, Any]]:
    rules = validate_schema(value, _array(RULE_SCHEMA))
    for key in ("id", "position"):
        if len({rule[key] for rule in rules}) != len(rules):
            raise ValueError(f"Duplicate rule {key}")
    return rules


def validate_adapter(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    adapter = validate_schema(value, ADAPTER_SCHEMA)
    if PurePath(adapter["executable"]).name.lower() in {"sh", "bash", "zsh", "dash", "fish", "csh", "ksh"}:
        raise ValueError("Adapter executable must not be a shell")
    names = [segment["name"] for segment in adapter["route"] if segment["kind"] == "id"]
    if "mr_id" not in names or len(names) != len(set(names)):
        raise ValueError("Adapter route requires unique named IDs including mr_id")
    if any(segment.get("value") in {".", ".."} for segment in adapter["route"]):
        raise ValueError("Adapter route cannot contain relative segments")
    if set(adapter["env"]) & set(adapter["credential_env"]):
        raise ValueError("Adapter environment overlaps credential references")
    return adapter


def validate_template_contract(value: Any) -> dict[str, Any]:
    return validate_schema(value, TEMPLATE_CONTRACT_SCHEMA)


def validate_discovery_sources(value: Any) -> list[dict[str, Any]]:
    sources = validate_schema(value, DISCOVERY_SOURCES_SCHEMA)
    if len({source["id"] for source in sources}) != len(sources):
        raise ValueError("Duplicate discovery source ID")
    return sources


def validate_change(value: Any) -> dict[str, Any]:
    change = validate_schema(value, CHANGE_SCHEMA)
    if change["kind"] == "settings":
        if "adapter" in change:
            validate_adapter(change["adapter"])
        if "discovery_sources" in change:
            validate_discovery_sources(change["discovery_sources"])
    return change


def validate_mr_identity(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    identity = validate_schema(value, MR_IDENTITY_SCHEMA)
    try:
        target = urlsplit(identity["canonical_url"])
        valid = (target.scheme == "https" and target.hostname is not None and target.username is None
                 and target.password is None and not target.fragment and target.port in (None, 443))
    except ValueError:
        valid = False
    if not valid:
        raise ValueError("Invalid canonical MR URL")
    return identity


def _with_verification_time(value: Any) -> Any:
    normalized = deepcopy(value)
    if isinstance(normalized, dict) and isinstance(normalized.get("verified_at"), datetime):
        stamp = normalized["verified_at"]
        if stamp.tzinfo is None or stamp.utcoffset() is None:
            raise ValueError("Verification time must include a timezone")
        normalized["verified_at"] = stamp.isoformat()
    return normalized


def validate_hit(value: Any) -> dict[str, Any]:
    hit = validate_schema(_with_verification_time(value), HIT_SCHEMA)
    if (hit["intent_id"] is None) == (hit["discovery_id"] is None):
        raise ValueError("A rule hit requires exactly one source")
    if (hit["rule_id"] is None) != (hit["rule_version"] is None):
        raise ValueError("A rule hit requires both rule ID and version")
    if hit["selection"] == Selection.FALLBACK:
        if hit["rule_id"] is not None or hit["action"] != Action.PUSH_FOR_CONFIRMATION:
            raise ValueError("Invalid fallback hit")
    elif hit["selection"] == Selection.MANUAL:
        if hit["action"] != Action.CREATE_TASK:
            raise ValueError("Manual MR execution must create or link a task")
    elif hit["rule_id"] is None:
        raise ValueError("A selected rule requires an ID and version")
    validate_mr_identity(hit["mr_identity"])
    return hit


def validate_condition_result(value: Any) -> dict[str, Any]:
    return validate_schema(_with_verification_time(value), CONDITION_RESULT_SCHEMA)


@dataclass(frozen=True)
class Rule:
    id: str
    version: int
    enabled: bool
    position: int
    intent_type: str
    scope: dict[str, Any]
    conditions: list[dict[str, Any]]
    action: Action
    project_id: str | None
    template_key: str | None

    @classmethod
    def from_mapping(cls, value: Any) -> Rule:
        fields = validate_rule(value)
        fields["action"] = Action(fields["action"])
        return cls(**fields)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class RuleFallback:
    id: str = "fallback"
    version: int = 1
    enabled: bool = True
    intent_type: str = "any"
    action: Action = Action.PUSH_FOR_CONFIRMATION


FALLBACK = RuleFallback()


def fallback_view(position: int) -> dict[str, Any]:
    return validate_schema({**asdict(FALLBACK), "position": position, "scope": {"kind": "all"},
                            "conditions": [], "project_id": None, "template_key": None}, FALLBACK_SCHEMA)


@dataclass(frozen=True)
class PreviewRequest:
    expected_generation: int
    change: dict[str, Any]

    @classmethod
    def from_mapping(cls, value: Any) -> PreviewRequest:
        fields = validate_schema(value, PREVIEW_REQUEST_SCHEMA)
        fields["change"] = validate_change(fields["change"])
        return cls(**fields)


@dataclass(frozen=True)
class ConfirmRequest:
    preview_id: str
    acceptance: tuple[Acceptance, ...]

    @classmethod
    def from_mapping(cls, value: Any) -> ConfirmRequest:
        fields = validate_schema(value, CONFIRM_REQUEST_SCHEMA)
        return cls(preview_id=fields["preview_id"], acceptance=tuple(Acceptance(item) for item in fields["acceptance"]))


@dataclass(frozen=True)
class PreviewResult:
    preview_id: str
    expires_at: str
    generation: int
    diff: dict[str, Any]
    impact: dict[str, Any]
    required_acceptance: tuple[Acceptance, ...]
    reference_fingerprints: dict[str, Any]


@dataclass(frozen=True)
class ConfirmResult:
    generation: int
    settings: dict[str, Any]
    rules: list[dict[str, Any]]
