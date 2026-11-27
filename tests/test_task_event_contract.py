"""The task-event stream schema is the closed union of every payload the sink emits."""

import pytest
import jsonschema

from looper.agents.api_v1 import document
from looper.agents.runtimes import StopMethod
from looper.agents.task_commands import TaskCommand
from looper.agents.tiers.task_manager import RefuseReason
from looper.agents.task_manager_settings import ModelSwitchRejection


def _schema():
    return document()["components"]["schemas"]["OperatorChatTaskEvent"]


def _payloads():
    emitted = [
        {"kind": "accepted", "reason": "duplicate"},
        {"kind": "starting", "status": "pending"},
        {"kind": "starting", "status": "assigned"},
        *({"kind": "command", "command": command.value} for command in TaskCommand),
        {"kind": "queued", "reason": "cap", "position": 1, "active": 0},
        {"kind": "queued", "reason": "turn", "position": 2, "active": 2},
        {"kind": "model_switched", "from_profile_id": "previous", "to_profile_id": "selected"},
        *({"kind": "model_switch_rejected", "profile_id": None, "reason": reason.value}
          for reason in ModelSwitchRejection),
        {"kind": "session_fresh", "reason": "context_bound",
         "detail": "The task session reached its context bound."},
        {"kind": "session_fresh", "reason": "read_only_open", "detail": "branch_gone"},
        {"kind": "session_fresh", "reason": "read_only_open", "detail": "branch_checked_out"},
        {"kind": "session_fresh", "reason": "read_only_open", "detail": "workspace_missing"},
        {"kind": "session_fresh", "reason": "resume_failed",
         "detail": "Claude Code resumed a different session"},
        *({"kind": "stopped", "method": method.value} for method in StopMethod),
        {"kind": "sandbox_denial", "tool_use_id": "toolu_1", "category": "network", "case": "not_allowed",
         "target": "blocked.example:443", "client": None, "command_name": "curl"},
        {"kind": "sandbox_denial", "tool_use_id": "toolu_2", "category": "network", "case": "unsupported_client",
         "target": None, "client": "go_tls", "command_name": "gh"},
        {"kind": "sandbox_denial", "tool_use_id": "toolu_4", "category": "network", "case": "not_allowable",
         "target": "10.0.0.1:443", "client": None, "command_name": "curl"},
        {"kind": "sandbox_denial", "tool_use_id": "toolu_3", "category": "other", "case": "unknown",
         "target": None, "client": None, "command_name": ""},
        {"kind": "turn_summary", "files": ["src/app.py"], "commit": None, "mode": "direct"},
        {"kind": "turn_summary", "files": [], "commit": "abc", "mode": "orchestrated"},
        {"kind": "turn_summary", "files": ["a.py"], "commit": None, "mode": "direct", "uncommitted": ["a.py"]},
        {"kind": "refused", "reason": "orchestrated", "status": "running"},
        *({"kind": "refused", "reason": reason.value} for reason in RefuseReason),
    ]
    rejected = [
        {"kind": "command"},
        {"kind": "command", "command": "/done", "extra": True},
        {"kind": "nope"},
        {"kind": "sandbox_denial", "tool_use_id": "toolu_1", "category": "network", "case": "not_allowed",
         "target": "blocked.example:443", "client": None},
        {"kind": "sandbox_denial", "tool_use_id": "toolu_1", "category": "dns", "case": "not_allowed",
         "target": None, "client": None, "command_name": "curl"},
        {"kind": "sandbox_denial", "tool_use_id": "toolu_1", "category": "network", "case": "not_allowed",
         "target": None, "client": "wasm", "command_name": "curl"},
        {"kind": "turn_summary", "files": []},
        {"kind": "turn_summary", "files": [], "commit": None, "mode": "direct", "uncommitted": [1]},
        {"kind": "refused", "reason": "orchestrated"},
        {"kind": "refused", "reason": "budget", "status": "running"},
        {"kind": "queued", "reason": "cap"},
        {"kind": "accepted", "reason": "other"},
    ]
    return emitted, rejected


def test_task_event_contract_matches_every_emitted_payload():
    schema = _schema()
    assert "oneOf" in schema
    variants = {variant["properties"]["kind"]["const"]: variant for variant in schema["oneOf"]}
    assert set(variants) == {
        "accepted", "starting", "command", "queued", "session_fresh", "stopped", "turn_summary", "refused",
        "model_switched", "model_switch_rejected", "sandbox_denial",
    }
    assert set(variants["command"]["properties"]["command"]["enum"]) == {command.value for command in TaskCommand}
    assert set(variants["stopped"]["properties"]["method"]["enum"]) == {method.value for method in StopMethod}
    refused = schema["oneOf"]
    manager_reasons = next(
        variant["properties"]["reason"]["enum"]
        for variant in refused
        if variant["properties"].get("kind", {}).get("const") == "refused" and "enum" in variant["properties"]["reason"])
    assert set(manager_reasons) == {reason.value for reason in RefuseReason}
    validator = jsonschema.Draft202012Validator(schema)
    emitted, rejected = _payloads()
    for payload in emitted:
        validator.validate(payload)
    for payload in rejected:
        with pytest.raises(jsonschema.ValidationError):
            validator.validate(payload)


def test_every_task_event_has_one_kind_and_the_schema_rejects_type():
    """History and live task events share one discriminator. A `type` field is not that discriminator."""
    emitted, _rejected = _payloads()
    for payload in emitted:
        assert {key for key in ("kind", "type") if key in payload} == {"kind"}
    schema = _schema()
    validator = jsonschema.Draft202012Validator(schema)
    for variant in schema["oneOf"]:
        assert "kind" in variant["properties"] and "type" not in variant["properties"]
        assert "kind" in variant["required"] and variant["additionalProperties"] is False
    for payload in emitted:
        validator.validate(payload)
        both = {**payload, "type": payload["kind"]}
        swapped = {"type" if key == "kind" else key: value for key, value in payload.items()}
        for rejected in (both, swapped):
            with pytest.raises(jsonschema.ValidationError):
                validator.validate(rejected)


def test_sandbox_denial_schema_enums_are_the_enums_the_code_emits():
    from looper.agents.task_manager_denials import Case, Category, Client

    variant = next(variant for variant in _schema()["oneOf"]
                   if variant["properties"]["kind"].get("const") == "sandbox_denial")
    assert set(variant["properties"]["category"]["enum"]) == {item.value for item in Category}
    assert set(variant["properties"]["case"]["enum"]) == {item.value for item in Case}
    assert set(variant["properties"]["client"]["enum"]) == {item.value for item in Client} | {None}
