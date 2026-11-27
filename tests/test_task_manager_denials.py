"""Code-generated classification and records for task Manager sandbox denials (pure functions)."""
from __future__ import annotations

import hashlib

import pytest

from looper.agents.task_manager_denials import (
    KNOWN_CLIENTS,
    Category,
    Case,
    Client,
    Denial,
    KnownClient,
    TurnThrottle,
    Violation,
    classify,
    command_name,
    record_summary,
)


def test_enum_and_known_client_values():
    assert Category.NETWORK.value == "network"
    assert Category.WRITE.value == "write"
    assert Category.OTHER.value == "other"
    assert Case.NOT_ALLOWED.value == "not_allowed"
    assert Case.UNSUPPORTED_CLIENT.value == "unsupported_client"
    assert Case.UNKNOWN.value == "unknown"
    assert Client.GO_TLS.value == "go_tls"
    assert Client.NODE_NO_PROXY.value == "node_no_proxy"
    assert KNOWN_CLIENTS == (
        KnownClient(Client.GO_TLS, frozenset({"gh", "gcloud", "terraform"}), "x509: OSStatus -26276"),
        KnownClient(Client.NODE_NO_PROXY, frozenset(), "getaddrinfo ENOTFOUND"),
    )


@pytest.mark.parametrize(
    "command, expected",
    [
        ("TOKEN=abc gh api x", "gh"),
        ("A=1 B=2 /usr/local/bin/terraform plan", "terraform"),
        ("/usr/bin/curl -s", "curl"),
        ("curl https://example.com", "curl"),
        ("gh", "gh"),
        ("FOO=bar", ""),
        ("", ""),
        ("   ", ""),
    ],
)
def test_command_name_skips_assignments_and_takes_basename(command, expected):
    assert command_name(command) == expected


def test_command_name_falls_back_before_first_space_on_shlex_error():
    assert command_name("gh api 'unclosed quote") == "gh"
    assert command_name("TOKEN=abc 'unclosed") == ""


def test_command_name_never_returns_more_than_the_first_word():
    assert command_name("curl -X POST https://example.com") == "curl"


def test_classify_network_target_not_on_allowlist_is_not_allowed():
    violations = [Violation(Category.NETWORK, "blocked.example", "network")]
    denials = classify(violations, command="curl https://blocked.example", output="403", allowed=[])
    assert denials == [
        Denial(Category.NETWORK, Case.NOT_ALLOWED, "blocked.example", None, "curl")
    ]


def test_classify_network_target_on_allowlist_with_go_executable_is_unsupported_client():
    violations = [Violation(Category.NETWORK, "api.github.com", "network")]
    denials = classify(violations, command="gh api /zen", output="", allowed=["api.github.com"])
    assert denials == [
        Denial(Category.NETWORK, Case.UNSUPPORTED_CLIENT, "api.github.com", Client.GO_TLS, "gh")
    ]


def test_classify_uses_port_stripping_when_matching_the_allowlist():
    violations = [Violation(Category.NETWORK, "api.github.com:443", "network")]
    denials = classify(violations, command="gh api /zen", output="", allowed=["api.github.com"])
    assert denials[0].case is Case.UNSUPPORTED_CLIENT
    assert denials[0].target == "api.github.com:443"
    assert denials[0].client is Client.GO_TLS


def test_classify_signature_in_output_identifies_go_client_without_known_executable():
    violations = [Violation(Category.NETWORK, "api.example.com", "network")]
    denials = classify(
        violations,
        command="custom-tool https://api.example.com",
        output="x509: OSStatus -26276",
        allowed=["api.example.com"],
    )
    assert denials == [
        Denial(Category.NETWORK, Case.UNSUPPORTED_CLIENT, "api.example.com", Client.GO_TLS, "custom-tool")
    ]


def test_classify_node_signature_identifies_node_no_proxy_client():
    violations = [Violation(Category.NETWORK, "registry.example.com", "network")]
    denials = classify(
        violations,
        command="node-tool install",
        output="getaddrinfo ENOTFOUND registry.example.com",
        allowed=["registry.example.com"],
    )
    assert denials[0].case is Case.UNSUPPORTED_CLIENT
    assert denials[0].client is Client.NODE_NO_PROXY


def test_classify_allowed_target_without_known_client_is_unknown_case():
    violations = [Violation(Category.NETWORK, "api.example.com", "network")]
    denials = classify(
        violations, command="curl https://api.example.com", output="403 denied", allowed=["api.example.com"]
    )
    assert denials == [
        Denial(Category.NETWORK, Case.UNKNOWN, "api.example.com", None, "curl")
    ]


def test_classify_write_violation_is_always_not_allowed_with_path_target():
    violations = [Violation(Category.WRITE, "/etc/secret", "write")]
    denials = classify(violations, command="touch /etc/secret", output="", allowed=["example.com"])
    assert denials == [
        Denial(Category.WRITE, Case.NOT_ALLOWED, "/etc/secret", None, "touch")
    ]


def test_classify_other_violation_is_unknown_without_target():
    violations = [Violation(Category.OTHER, "something-internal", "mystery")]
    denials = classify(violations, command="curl https://example.com", output="", allowed=[])
    assert denials == [Denial(Category.OTHER, Case.UNKNOWN, None, None, "curl")]


def test_classify_signature_without_violations_emits_one_unsupported_client_without_target():
    denials = classify([], command="gh api /zen", output="x509: OSStatus -26276", allowed=["api.github.com"])
    assert denials == [
        Denial(Category.NETWORK, Case.UNSUPPORTED_CLIENT, None, Client.GO_TLS, "gh")
    ]


def test_classify_without_violations_or_signature_returns_empty():
    assert classify([], command="curl https://example.com", output="all good", allowed=[]) == []


def test_classify_keeps_one_denial_per_violation_in_order():
    violations = [
        Violation(Category.NETWORK, "blocked.example", "network"),
        Violation(Category.WRITE, "/etc/secret", "write"),
        Violation(Category.OTHER, None, "mystery"),
    ]
    denials = classify(violations, command="curl https://blocked.example", output="", allowed=[])
    assert [denial.case for denial in denials] == [Case.NOT_ALLOWED, Case.NOT_ALLOWED, Case.UNKNOWN]
    assert [denial.category for denial in denials] == [Category.NETWORK, Category.WRITE, Category.OTHER]
    assert denials[2].target is None


def test_record_summary_format_and_hash():
    command = "curl https://blocked.example"
    denial = Denial(Category.NETWORK, Case.NOT_ALLOWED, "blocked.example", None, "curl")
    summary = record_summary(denial, tool_use_id="tu_1", command=command)
    digest = hashlib.sha256(command.encode("utf-8")).hexdigest()[:16]
    assert summary == (
        f"network not_allowed blocked.example tool_use_id=tu_1 command_sha256={digest}"
    )


def test_record_summary_uses_dash_for_missing_target():
    denial = Denial(Category.NETWORK, Case.UNSUPPORTED_CLIENT, None, Client.GO_TLS, "gh")
    summary = record_summary(denial, tool_use_id="tu_2", command="gh api")
    assert summary.startswith("network unsupported_client - tool_use_id=tu_2 command_sha256=")


def test_record_summary_never_contains_the_command_text_or_its_secrets():
    command = "TOKEN=abc curl https://blocked.example/path"
    denial = Denial(Category.NETWORK, Case.NOT_ALLOWED, "blocked.example", None, "curl")
    summary = record_summary(denial, tool_use_id="tu_3", command=command)
    assert "TOKEN" not in summary
    assert "abc" not in summary
    assert "curl" not in summary
    assert "path" not in summary
    assert "blocked.example" in summary  # the typed target is allowed, the command text is not


def test_turn_throttle_emits_once_for_twelve_identical_denials():
    denial = Denial(Category.NETWORK, Case.NOT_ALLOWED, "blocked.example", None, "curl")
    throttle = TurnThrottle()
    results = [throttle.first(denial) for _ in range(12)]
    assert results == [True] + [False] * 11


def test_turn_throttle_distinguishes_category_and_target_pairs():
    throttle = TurnThrottle()
    first = Denial(Category.NETWORK, Case.NOT_ALLOWED, "a.example", None, "curl")
    same_pair = Denial(Category.NETWORK, Case.UNKNOWN, "a.example", None, "curl")
    other_target = Denial(Category.NETWORK, Case.NOT_ALLOWED, "b.example", None, "curl")
    other_category = Denial(Category.WRITE, Case.NOT_ALLOWED, "a.example", None, "touch")
    assert throttle.first(first) is True
    assert throttle.first(same_pair) is False
    assert throttle.first(other_target) is True
    assert throttle.first(other_category) is True
