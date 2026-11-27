"""Validation and matching for the task Manager's per-project network allowlist (pure functions)."""
from __future__ import annotations

import pytest

from looper.agents.task_manager_network import (
    MAX_DOMAINS,
    NON_PUBLIC_SUFFIXES,
    SCOPE_TEXT,
    PolicyInvalid,
    matches,
    validate_domains,
)

FIELD = "network_allowed_domains"


def test_empty_list_is_allowed_and_returns_tuple_in_input_order():
    result = validate_domains([])
    assert result == ()
    assert isinstance(result, tuple)


@pytest.mark.parametrize(
    "values, expected",
    [
        (["example.com"], ("example.com",)),
        (["Registry.NPMjs.org."], ("registry.npmjs.org",)),
        (["EXAMPLE.COM."], ("example.com",)),
        (["*.example.com"], ("*.example.com",)),
        (["a-b.c-d.example.com"], ("a-b.c-d.example.com",)),
        (["b.example.com", "a.example.com"], ("b.example.com", "a.example.com")),
        (["host.example.com", "*.cdn.example.com"], ("host.example.com", "*.cdn.example.com")),
    ],
)
def test_accepts_and_normalizes(values, expected):
    assert validate_domains(values) == expected


def test_only_one_trailing_dot_is_removed():
    assert validate_domains(["example.com."]) == ("example.com",)
    with pytest.raises(PolicyInvalid) as exc:
        validate_domains(["example.com.."])
    assert exc.value.field == FIELD
    assert exc.value.why == "invalid hostname"


def test_accepts_up_to_max_domains():
    values = [f"host{i}.example.com" for i in range(MAX_DOMAINS)]
    assert len(validate_domains(values)) == MAX_DOMAINS


def test_more_than_max_domains_is_refused_without_truncation():
    values = [f"host{i}.example.com" for i in range(MAX_DOMAINS + 1)]
    with pytest.raises(PolicyInvalid) as exc:
        validate_domains(values)
    assert exc.value.field == FIELD
    assert exc.value.entry is None
    assert exc.value.why == f"at most {MAX_DOMAINS} entries"


@pytest.mark.parametrize("bad_values", ["example.com", None, 123, ("example.com",), {"example.com"}, [["example.com"]]])
def test_values_must_be_a_list_of_strings(bad_values):
    with pytest.raises(PolicyInvalid) as exc:
        validate_domains(bad_values)
    assert exc.value.field == FIELD
    assert exc.value.entry is None
    assert exc.value.why == "not a list of strings"


def test_non_string_entry_is_refused():
    with pytest.raises(PolicyInvalid) as exc:
        validate_domains(["example.com", 1])
    assert exc.value.entry is None
    assert exc.value.why == "not a list of strings"


def test_duplicate_after_normalization_names_second_occurrence():
    with pytest.raises(PolicyInvalid) as exc:
        validate_domains(["a.com", "A.com"])
    assert exc.value.field == FIELD
    assert exc.value.entry == "a.com"
    assert exc.value.why == "duplicate entry"


def test_duplicate_wildcard_is_refused_not_dropped():
    with pytest.raises(PolicyInvalid) as exc:
        validate_domains(["*.Example.COM", "*.example.com"])
    assert exc.value.entry == "*.example.com"
    assert exc.value.why == "duplicate entry"


@pytest.mark.parametrize(
    "entry, expected_entry, why",
    [
        ("10.0.0.1", "10.0.0.1", "IP literals are not external domains"),
        ("[::1]", "[::1]", "IP literals are not external domains"),
        ("[2001:db8::1]", "[2001:db8::1]", "IP literals are not external domains"),
        ("::1", "::1", "IP literals are not external domains"),
        ("localhost", "localhost", "localhost is not an external domain"),
        ("foo.local", "foo.local", "names under non-public suffixes are not external domains"),
        ("foo.localhost", "foo.localhost", "names under non-public suffixes are not external domains"),
        ("host.internal", "host.internal", "names under non-public suffixes are not external domains"),
        ("host.home.arpa", "host.home.arpa", "names under non-public suffixes are not external domains"),
        ("host.lan", "host.lan", "names under non-public suffixes are not external domains"),
        ("home.arpa", "home.arpa", "names under non-public suffixes are not external domains"),
        ("example.com:443", "example.com:443", "ports are not allowed"),
        ("http://example.com", "http://example.com", "ports are not allowed"),
        ("example.com/path", "example.com/path", "paths are not allowed"),
        ("user@example.com", "user@example.com", "userinfo is not allowed"),
        ("example .com", "example .com", "whitespace is not allowed"),
        ("example.com\t", "example.com\t", "whitespace is not allowed"),
        ("*", "*", "a wildcard is only allowed as a leading '*.' prefix"),
        # The single trailing dot is removed during normalization, so the refused entry is "*".
        ("*.", "*", "a wildcard is only allowed as a leading '*.' prefix"),
        ("a.*.com", "a.*.com", "a wildcard is only allowed as a leading '*.' prefix"),
        ("*.*.example.com", "*.*.example.com", "a wildcard is only allowed as a leading '*.' prefix"),
        ("intranet", "intranet", "single-label names are not external domains"),
        ("*.com", "*.com", "single-label names are not external domains"),
        ("", "", "empty entries are not allowed"),
        ("-bad.example.com", "-bad.example.com", "invalid hostname"),
        ("bad-.example.com", "bad-.example.com", "invalid hostname"),
        ("ex_ample.com", "ex_ample.com", "invalid hostname"),
    ],
)
def test_each_refused_shape_has_its_own_typed_reason(entry, expected_entry, why):
    with pytest.raises(PolicyInvalid) as exc:
        validate_domains([entry])
    assert exc.value.field == FIELD
    assert exc.value.entry == expected_entry
    assert exc.value.why == why


def test_label_length_bounds():
    long_label = "a" * 63
    assert validate_domains([f"{long_label}.example.com"]) == (f"{long_label}.example.com",)
    with pytest.raises(PolicyInvalid) as exc:
        validate_domains([f"{'a' * 64}.example.com"])
    assert exc.value.why == "invalid hostname"


def test_hostname_total_length_bound_is_measured_on_the_hostname():
    hostname = ".".join(["a" * 63, "a" * 63, "a" * 63, "a" * 61])
    assert len(hostname) == 253
    assert validate_domains([hostname]) == (hostname,)
    assert validate_domains([f"*.{hostname}"]) == (f"*.{hostname}",)
    too_long = hostname + "a"
    with pytest.raises(PolicyInvalid) as exc:
        validate_domains([too_long])
    assert exc.value.why == "invalid hostname"


def test_non_public_suffix_list_matches_rfc_scope():
    assert NON_PUBLIC_SUFFIXES == ("local", "localhost", "internal", "home.arpa", "lan")


def test_matches_exact_entries_case_insensitive_and_strips_a_single_port():
    allowed = ["example.com", "*.api.example.com"]
    assert matches("example.com", allowed)
    assert matches("EXAMPLE.COM:443", allowed)
    assert matches("host.api.example.com", allowed)
    assert matches("HOST.API.EXAMPLE.COM:8443", allowed)


def test_matches_wildcard_does_not_cover_apex_or_suffix_lookalikes():
    allowed = ["*.api.example.com"]
    assert matches("host.api.example.com", allowed)
    assert not matches("api.example.com", allowed)
    assert not matches("evilapi.example.com", allowed)
    assert not matches("other.example.com", allowed)
    assert not matches("api.example.com.evil.com", allowed)


def test_matches_unlisted_host_and_empty_allowlist():
    assert not matches("example.com", [])
    assert not matches("other.com", ["example.com"])


def test_scope_text_covers_proxy_clients_go_and_node_tools_and_operator_responsibility():
    assert isinstance(SCOPE_TEXT, str) and SCOPE_TEXT
    for phrase in ("HTTP(S)", "proxy", "curl", "npm", "pip", "Python", "gh", "Node", "operator"):
        assert phrase in SCOPE_TEXT


@pytest.mark.parametrize("entry", ["127.1", "10.1", "0x7f.1", "*.127.1", "example.0x1"])
def test_shorthand_ipv4_forms_are_refused_as_ip_literals(entry):
    with pytest.raises(PolicyInvalid) as exc:
        validate_domains([entry])
    assert exc.value.entry == entry
    assert exc.value.why == "IP literals are not external domains"


def test_matches_ignores_a_trailing_dot_of_the_denied_host():
    assert matches("example.com.", ["example.com"]) is True
    assert matches("a.example.com.", ["*.example.com"]) is True
