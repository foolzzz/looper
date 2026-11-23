"""The secret rules a skill proposal is checked against: one hit and one miss each, and no secret in a finding."""

import pytest

from looper.agents import skill_secrets

# rule -> (text that must match, text that must not)
CASES = {
    "pem_private_key": ("-----BEGIN RSA PRIVATE KEY-----\nabc", "-----BEGIN CERTIFICATE-----\nabc"),
    "aws_access_key": ("key AKIAABCDEFGHIJKLMNOP here", "key AKIAABCDE is too short"),
    "github_token": ("ghp_" + "a1B2" * 9, "ghp_tooshort"),
    "api_key_sk": ("use sk-" + "abcdefghij" * 2, "the sk-short prefix"),
    "slack_token": ("xoxb-1234567890-abcdef", "xoxb-12 is short"),
    "bearer_token": ("Authorization: Bearer " + "abcdef0123456789" * 2, "Bearer tokens go in a header"),
    "credential_assignment": ("password = hunter22222", "password = short"),
}


@pytest.mark.parametrize("rule", sorted(CASES))
def test_each_rule_matches_its_shape_and_not_a_near_miss(rule):
    hit, miss = CASES[rule]
    assert rule in {finding.rule for finding in skill_secrets.find(hit)}
    assert rule not in {finding.rule for finding in skill_secrets.find(miss)}


def test_every_rule_has_a_case():
    assert {name for name, _ in skill_secrets.RULES} == set(CASES)


def test_a_finding_names_the_rule_and_the_span_but_never_the_text():
    text = "intro\nGITHUB_TOKEN=ghp_" + "Zz9Y" * 9 + "\nend"
    findings = skill_secrets.find(text)
    assert findings and all(not hasattr(finding, "text") for finding in findings)
    first = findings[0]
    assert text[first.start:first.end].startswith(("TOKEN", "ghp_")) and first.start >= len("intro\n")
    assert "Zz9Y" not in repr(findings)
    assert skill_secrets.find("Check the release notes, then run the tests.") == []
