"""Closed configuration contracts for operator-managed intent rules."""
from copy import deepcopy
from dataclasses import FrozenInstanceError

import pytest

from looper.agents import intent_rules_model as model


def rule(**changes):
    value = {"id": "00112233-4455-4677-8899-aabbccddeeff", "version": 1,
             "enabled": True, "position": 1, "intent_type": "mr_review",
             "scope": {"kind": "all"},
             "conditions": [{"kind": "internal_mr_open", "allowed_domains": ["review.example.invalid"]}],
             "action": "create_task", "project_id": "review-project", "template_key": "review-template"}
    return value | changes


def adapter(**changes):
    return {"system_id": "review-system", "executable": "/usr/bin/mr-verifier", "args": ["verify"],
            "allowed_domains": ["review.example.invalid"],
            "route": [{"kind": "literal", "value": "repositories"}, {"kind": "id", "name": "repository"},
                      {"kind": "literal", "value": "merge_requests"}, {"kind": "id", "name": "mr_id"}],
            "operator_id": "operator-id", "credential_env": {"REVIEW_TOKEN": "secret:review-readonly"},
            "env": {"LANG": "C.UTF-8"}} | changes


@pytest.mark.parametrize("changes", [
    {"unknown": True}, {"action": "merge"}, {"conditions": []}, {"intent_type": "any"},
    {"intent_type": "dev_task"}, {"project_id": None}, {"template_key": None}, {"position": 0},
    {"version": True}, {"enabled": 1}, {"id": "fallback"},
    {"scope": {"kind": "all", "chats": []}},
    {"scope": {"kind": "named_chats", "chats": []}},
    {"scope": {"kind": "named_chats", "chats": [{"source_id": "source", "chat_id": "*"}]}},
    {"scope": {"kind": "named_chats", "chats": [{"source_id": "source", "chat_id": "chat"}] * 2}},
    {"conditions": [{"kind": "expression", "value": "true"}]},
    {"conditions": [{"kind": "internal_mr_open", "allowed_domains": []}]},
    {"conditions": [{"kind": "internal_mr_open", "allowed_domains": ["*.example.invalid"]}]},
    {"conditions": [{"kind": "internal_mr_open", "allowed_domains": ["https://review.example.invalid"]}]},
    {"conditions": [{"kind": "operator_is_reviewer"}]},
    {"conditions": [{"kind": "mr_repository_in_allowlist", "repository_ids": ["repo"]}]},
    {"conditions": [{"kind": "internal_mr_open", "allowed_domains": ["review.example.invalid"]},
                    {"kind": "sender_in_allowlist", "sender_ids": []}]},
    {"conditions": [{"kind": "internal_mr_open", "allowed_domains": ["review.example.invalid"]},
                    {"kind": "addressed_to_operator", "expression": "true"}]},
    {"action": "record_only"},
])
def test_rule_schema_rejects_unsafe_configuration(changes):
    with pytest.raises(ValueError):
        model.validate_rule(rule(**changes))


def test_rule_contract_accepts_only_typed_conditions_and_returns_a_copy():
    value = rule(conditions=[{"kind": "internal_mr_open", "allowed_domains": ["review.example.invalid"]},
                             {"kind": "operator_is_reviewer"}, {"kind": "addressed_to_operator"},
                             {"kind": "sender_in_allowlist", "sender_ids": ["sender-id"]},
                             {"kind": "mr_repository_in_allowlist", "repository_ids": ["repository-id"]}])
    assert model.validate_rule(value) == value
    assert model.validate_rule(value) is not value
    confirmation = rule(action="push_for_confirmation", conditions=[], intent_type="any", project_id=None,
                        template_key=None)
    assert model.validate_rule(confirmation) == confirmation
    record = confirmation | {"action": "record_only"}
    assert model.validate_rule(record) == record
    new = {key: item for key, item in value.items() if key not in {"id", "version"}}
    assert model.validate_rule(new, complete=False) == new


def test_fallback_is_code_owned_and_cannot_be_mutated():
    value = model.fallback_view(4)
    assert value["position"] == 4
    assert value["intent_type"] == "any"
    assert value["scope"] == {"kind": "all"}
    assert value["conditions"] == []
    assert value["action"] == "push_for_confirmation"
    value["scope"]["kind"] = "named_chats"
    assert model.fallback_view(4)["scope"] == {"kind": "all"}
    with pytest.raises(FrozenInstanceError):
        model.FALLBACK.enabled = False


@pytest.mark.parametrize("changes", [
    {"shell": True}, {"executable": "verify --command"}, {"executable": "/bin/sh"},
    {"allowed_domains": []}, {"allowed_domains": ["review.example.invalid:443"]},
    {"allowed_domains": ["Review.example.invalid"]}, {"operator_id": ""},
    {"route": [{"kind": "regex", "value": ".*"}]},
    {"route": [{"kind": "literal", "value": "repos/other"}, {"kind": "id", "name": "mr_id"}]},
    {"route": [{"kind": "id", "name": "repository"}]},
    {"route": [{"kind": "id", "name": "repository"}, {"kind": "id", "name": "repository"},
               {"kind": "id", "name": "mr_id"}]},
    {"credential_env": {"REVIEW_TOKEN": "raw-secret-value"}},
    {"credential_env": {"invalid-key": "secret:review-readonly"}},
    {"env": {"REVIEW_TOKEN": "raw-secret-value"}}, {"args": ["verify", None]},
])
def test_adapter_configuration_is_closed_and_credentials_are_references(changes):
    with pytest.raises(ValueError):
        model.validate_adapter(adapter(**changes))


def test_valid_adapter_keeps_only_explicit_environment_and_fixed_route():
    assert model.validate_adapter(None) is None
    assert model.validate_adapter(adapter()) == adapter()


@pytest.mark.parametrize("change", [
    {"kind": "rule_create", "rule": rule()},
    {"kind": "rule_update", "rule_id": "00112233-4455-4677-8899-aabbccddeeff", "expected_version": 1,
     "fields": {"id": "00112233-4455-4677-8899-aabbccddeeff"}},
    {"kind": "rule_update", "rule_id": "00112233-4455-4677-8899-aabbccddeeff", "expected_version": 1,
     "fields": {}},
    {"kind": "rule_reorder", "ids": ["00112233-4455-4677-8899-aabbccddeeff"] * 2},
    {"kind": "rule_delete", "rule_id": "fallback", "expected_version": 1},
    {"kind": "settings"}, {"kind": "settings", "enabled": "true"},
    {"kind": "settings", "runtime_ready": True},
    {"kind": "project_cap", "project_id": "project", "expected_config_version": 0, "auto_task_daily_cap": 0},
    {"kind": "project_cap", "project_id": "project", "expected_config_version": -1, "auto_task_daily_cap": 50},
    {"kind": "rule_rollback", "rule_id": "00112233-4455-4677-8899-aabbccddeeff", "expected_version": 1,
     "target_version": 0}, {"kind": "expression", "value": "true"},
])
def test_change_requires_explicit_closed_versioned_payload(change):
    with pytest.raises(ValueError):
        model.validate_change(change)


@pytest.mark.parametrize("kind", ["rule_disable", "rule_delete", "rule_rollback"])
def test_rule_mutations_require_stable_id_and_expected_version(kind):
    value = {"kind": kind, "rule_id": "00112233-4455-4677-8899-aabbccddeeff", "expected_version": 1}
    if kind == "rule_rollback":
        value["target_version"] = 1
    assert model.validate_change(value) == value


def test_partial_rule_update_defers_cross_field_validation_to_combined_rule():
    value = {"kind": "rule_update", "rule_id": "00112233-4455-4677-8899-aabbccddeeff", "expected_version": 1,
             "fields": {"conditions": []}}
    assert model.validate_change(value) == value
    with pytest.raises(ValueError):
        model.validate_rule(rule() | value["fields"])


def test_template_contract_preserves_external_action_boundaries():
    value = {"input_kind": "verified_mr", "comment_mode": "direct", "approve_requires_confirmation": True,
             "withdraw_requires_confirmation": True, "allow_merge": False, "allow_reviewer_changes": False}
    assert model.validate_template_contract(value) == value
    for field in ("approve_requires_confirmation", "withdraw_requires_confirmation", "allow_merge",
                  "allow_reviewer_changes"):
        bad = deepcopy(value)
        bad[field] = not bad[field]
        with pytest.raises(ValueError):
            model.validate_template_contract(bad)
    with pytest.raises(ValueError):
        model.validate_template_contract(value | {"input_kind": "message"})


def test_discovery_sources_allow_server_assigned_operator_and_require_unique_ids():
    source = {"id": "source", "system_id": "review-system", "enabled": True,
              "private_target": {"channel_id": "channel", "chat": "private-chat"},
              "migration_state": "unconfigured"}
    assert model.validate_discovery_sources([source]) == [source]
    assert model.validate_discovery_sources([source | {"operator_key": "operator"}])
    with pytest.raises(ValueError):
        model.validate_discovery_sources([source, source])
    with pytest.raises(ValueError):
        model.validate_discovery_sources([source | {"private_target": {"channel_id": "channel", "chat": "*"}}])


def test_acceptance_and_mr_identity_are_closed_and_do_not_include_message_text():
    model.validate_schema(["external_comments", "source_migration"], model.ACCEPTANCE_SCHEMA)
    with pytest.raises(ValueError):
        model.validate_schema(["merge_allowed"], model.ACCEPTANCE_SCHEMA)
    identity = {"system_id": "review-system", "repository_id": "repository-id", "mr_id": "23",
                "revision": "revision", "canonical_url": "https://review.example.invalid/repositories/1/merge_requests/23"}
    assert model.validate_mr_identity(identity) == identity
    with pytest.raises(ValueError):
        model.validate_mr_identity(identity | {"body": "untrusted instructions"})


def test_hit_requires_one_source_and_a_consistent_rule_reference():
    hit = {"evaluation_key": "evaluation-1", "intent_id": 12, "discovery_id": None, "generation": 3,
           "rule_id": "00112233-4455-4677-8899-aabbccddeeff", "rule_version": 2, "selection": "matched",
           "action": "record_only", "reason": None, "verified_at": None, "mr_identity": None}
    assert model.validate_hit(hit) == hit
    for changes in ({"intent_id": None}, {"discovery_id": 1}, {"rule_version": None},
                    {"selection": "unknown"}, {"generation": -1}, {"message": "untrusted"}):
        with pytest.raises(ValueError):
            model.validate_hit(hit | changes)
    fallback = hit | {"selection": "fallback", "rule_id": None, "rule_version": None,
                      "action": "push_for_confirmation"}
    assert model.validate_hit(fallback) == fallback
    with pytest.raises(ValueError):
        model.validate_hit(fallback | {"action": "create_task"})


def test_condition_result_is_closed_and_normalizes_aware_verification_time():
    from datetime import datetime, timezone

    value = {"rule_position": 1, "condition_position": 1,
             "rule_id": "00112233-4455-4677-8899-aabbccddeeff", "rule_version": 2,
             "kind": "internal_mr_open", "result": "unavailable", "reason": "authentication_failed",
             "verified_at": datetime(2026, 1, 1, tzinfo=timezone.utc)}
    normalized = model.validate_condition_result(value)
    assert normalized["verified_at"] == "2026-01-01T00:00:00+00:00"
    for changes in ({"reason": "arbitrary text"}, {"result": "unknown"}, {"condition_position": 0},
                    {"verified_at": datetime(2026, 1, 1)}, {"body": "untrusted"}):
        with pytest.raises(ValueError):
            model.validate_condition_result(value | changes)


def test_route_names_are_installation_declared_without_assuming_repository_url_ids():
    value = adapter(route=[{"kind": "id", "name": "namespace"}, {"kind": "id", "name": "project"},
                           {"kind": "literal", "value": "reviews"}, {"kind": "id", "name": "mr_id"}])
    assert model.validate_adapter(value) == value


def test_rule_set_rejects_duplicate_identity_and_order():
    first = rule()
    second = rule(id="00112233-4455-4677-8899-aabbccddee00", position=2)
    assert model.validate_rules([first, second]) == [first, second]
    for value in ([first, first], [first, second | {"position": 1}]):
        with pytest.raises(ValueError):
            model.validate_rules(value)
