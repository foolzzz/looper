"""Deterministic adapter protocol and event-bound authorization contracts."""
from __future__ import annotations

from dataclasses import replace
import json
import asyncio
from pathlib import Path
import sys
from uuid import UUID

import pytest

from looper.agents.intent_rule_verifier import (
    MRTarget, VerificationUnavailable, VerifiedFact, parse_target, verify,
)
from looper.agents.intent_rule_evaluation import (
    PreparedCandidate, PreparedSource, evaluate, prepare_message_candidates,
)
from looper.agents.extraction import Parsed

URL = "https://review.example.test/repos/repo-one/merge_requests/12"
CONFIG = {
    "system_id": "review-system", "executable": sys.executable, "args": [],
    "allowed_domains": ["review.example.test"], "operator_id": "reviewer-one",
    "route": [{"kind": "literal", "value": "repos"}, {"kind": "id", "name": "repository_id"},
              {"kind": "literal", "value": "merge_requests"}, {"kind": "id", "name": "mr_id"}],
    "env": {}, "credential_env": {},
}


def response(**changes):
    facts = {"exists": True, "state": "open", "reviewers": ["reviewer-one"],
             "repository": "repo-one", "mr_id": "12", "revision": "revision-a", "canonical_url": URL}
    facts.update(changes)
    return {"schema_version": 1, "status": "verified", "facts": facts}


def adapter(tmp_path, source):
    script = tmp_path / "adapter.py"
    script.write_text(source)
    return {**CONFIG, "args": [str(script)]}


@pytest.mark.parametrize("url", [
    URL.replace("https:", "http:"), URL.replace("review.example.test", "review.example.test.evil.test"),
    URL.replace("review.example.test", "review.example.test@evil.test"),
    URL.replace("review.example.test", "user@review.example.test"),
    URL + "#fragment", URL + "?redirect=https://evil.test", URL + "/files",
    URL.replace("repo-one", "repo%2fone"), URL.replace("repo-one", ".."),
    URL.replace("repo-one", "repo%252fone"), URL.replace("https://", "https:// "),
    URL + "\n", URL.replace("review.example.test", "review.example.test."),
    URL.replace("review.example.test", "review.example.test:444"),
])
def test_strict_target_rejects_ambiguous_or_non_mr_routes(url):
    assert parse_target(url, CONFIG) is None
    target = parse_target(URL, CONFIG)
    assert target.mr_url == URL
    assert target.route_ids == {"repository_id": "repo-one", "mr_id": "12"}
    assert MRTarget.from_mapping(target.to_dict()) == target


def test_target_normalizes_only_provably_equivalent_host():
    target = parse_target(URL.replace("review.example.test", "REVIEW.EXAMPLE.TEST:443"), CONFIG)
    assert target.mr_url == URL


def test_verifier_receives_only_one_canonical_url(tmp_path, monkeypatch):
    async def check():
        capture = tmp_path / "capture.json"
        source = (
            "import os,sys,json\nfrom pathlib import Path\n"
            f"Path({str(capture)!r}).write_text(json.dumps({{'request':json.load(sys.stdin),"
            "'env':dict(os.environ),'files':os.listdir('.'),'cwd':os.getcwd(),'args':sys.argv[1:]}))\n"
            f"print({json.dumps(response())!r})\n"
        )
        monkeypatch.setenv("UNTRUSTED_CONTEXT", "malicious-message")
        result = await verify(URL, adapter(tmp_path, source))
        assert isinstance(result, VerifiedFact)
        assert result.revision == "revision-a"
        assert VerifiedFact.from_mapping(result.to_dict()) == result
        observed = json.loads(capture.read_text())
        assert observed["request"] == {"schema_version": 1, "mr_url": URL}
        assert "UNTRUSTED_CONTEXT" not in observed["env"]
        assert observed["files"] == []
        assert observed["cwd"] != str(tmp_path)
        assert observed["args"] == []
        assert not Path(observed["cwd"]).exists()
    asyncio.run(check())


@pytest.mark.parametrize("output", [
    "not-json", json.dumps(response()) + "{}", json.dumps({**response(), "extra": True}),
    json.dumps({**response(), "schema_version": True}),
    json.dumps(response(state="draft")), json.dumps(response(reviewers=[1])),
    json.dumps(response(revision="")), json.dumps(response(mr_id=12)),
    json.dumps(response(reviewers=[" "])), json.dumps(response(revision=" ")), json.dumps(response(repository="repo space")),
    json.dumps(response(extra="untrusted")), json.dumps({"schema_version": 1, "status": "verified"}),
    '{"schema_version":1,"schema_version":1,"status":"verified","facts":{"exists":false}}',
    json.dumps({"schema_version": 1, "status": "verification_unavailable", "reason": "unknown"}),
])
def test_unavailable_verification_rejects_protocol_errors(tmp_path, output):
    async def check():
        result = await verify(URL, adapter(tmp_path, f"print({output!r})"))
        assert isinstance(result, VerificationUnavailable)
        assert result.reason == "verification_protocol"
        good = await verify(URL, adapter(tmp_path, f"print({json.dumps(response())!r})"))
        assert isinstance(good, VerifiedFact)
    asyncio.run(check())


@pytest.mark.parametrize("reason", ["authentication_failed", "permission_denied", "not_readable", "cannot_decide", "upstream_error"])
def test_explicit_adapter_unavailable_is_typed(tmp_path, reason):
    async def check():
        output = {"schema_version": 1, "status": "verification_unavailable", "reason": reason}
        result = await verify(URL, adapter(tmp_path, f"print({json.dumps(output)!r})"))
        assert isinstance(result, VerificationUnavailable) and result.reason == reason
    asyncio.run(check())


@pytest.mark.parametrize("changes", [
    {"canonical_url": URL.replace("review.example.test", "evil.test")},
    {"canonical_url": URL.replace("/12", "/13")}, {"mr_id": "13"}, {"repository": "other-repo"},
])
def test_response_canonical_identity_must_match_request(tmp_path, changes):
    async def check():
        result = await verify(URL, adapter(tmp_path, f"print({json.dumps(response(**changes))!r})"))
        assert isinstance(result, VerificationUnavailable) and result.reason == "cannot_decide"
    asyncio.run(check())


def test_stdout_limit_is_exact_and_stderr_is_drained(tmp_path):
    async def check():
        encoded = json.dumps(response()).encode()
        for size, accepted in [(65536, True), (65537, False)]:
            source = f"import sys\nsys.stderr.write('discard-me' * 20000)\nsys.stdout.write({encoded.decode()!r} + ' ' * {size - len(encoded)})"
            result = await verify(URL, adapter(tmp_path, source))
            assert isinstance(result, VerifiedFact) is accepted
            if not accepted:
                assert result.reason == "verification_output_limit"
    asyncio.run(check())


def test_missing_config_credentials_start_failure_and_exit(tmp_path):
    async def check():
        assert (await verify(URL, None)).reason == "adapter_missing"
        assert (await verify(URL, {**CONFIG, "operator_id": ""})).reason == "adapter_missing"
        assert (await verify(URL, {**CONFIG, "credential_env": {"READ_TOKEN": "secret:read-only"}})).reason == "adapter_missing"
        assert (await verify(URL, {**CONFIG, "executable": "/nonexistent/adapter"})).reason == "adapter_start_failed"
        failed = adapter(tmp_path, "import sys\nsys.stderr.write('sensitive diagnostics')\nsys.exit(2)")
        assert (await verify(URL, failed)).reason == "verification_exit"
    asyncio.run(check())


def test_timeout_terminates_process_group(tmp_path, monkeypatch):
    async def check():
        import looper.agents.intent_rule_verifier as verifier
        monkeypatch.setattr(verifier, "TIMEOUT_SECONDS", 0.3)
        marker = tmp_path / "child-marker"
        child = f"import time; from pathlib import Path; time.sleep(0.8); Path({str(marker)!r}).write_text('survived')"
        source = f"import subprocess,sys,time\nsubprocess.Popen([sys.executable,'-c',{child!r}])\ntime.sleep(30)"
        result = await verify(URL, adapter(tmp_path, source))
        assert result.reason == "verification_timeout"
        import asyncio
        await asyncio.sleep(0.9)
        assert not marker.exists()
    asyncio.run(check())


def test_api_missing_and_closed_are_deterministic_facts(tmp_path):
    async def check():
        missing = {"schema_version": 1, "status": "verified", "facts": {"exists": False}}
        result = await verify(URL, adapter(tmp_path, f"print({json.dumps(missing)!r})"))
        assert isinstance(result, VerifiedFact) and not result.exists
        closed = await verify(URL, adapter(tmp_path, f"print({json.dumps(response(state='closed'))!r})"))
        assert isinstance(closed, VerifiedFact) and closed.state == "closed"
    asyncio.run(check())


def rule(position=1, conditions=None, **changes):
    value = {"id": str(UUID(int=position)), "version": 1, "enabled": True, "position": position,
             "intent_type": "mr_review", "scope": {"kind": "all"}, "action": "create_task",
             "project_id": "review-project", "template_key": "review-template",
             "conditions": conditions if conditions is not None else [{"kind": "internal_mr_open", "allowed_domains": CONFIG["allowed_domains"]},
                                           {"kind": "mr_repository_in_allowlist", "repository_ids": ["repo-one"]}]}
    value.update(changes)
    return value


def source(seq=1, **changes):
    value = dict(seq=seq, source_id="messages-one", chat_id="review-room", sender_id="requester-one",
                 mention_ids=(), dm_recipient_id=None, operator_id="message-operator", target_urls=(URL,))
    value.update(changes)
    return PreparedSource(**value)


def fact(**changes):
    values = {"status": "verified", "target": parse_target(URL, CONFIG).to_dict(),
              **response(**changes)["facts"], "operator_id": CONFIG["operator_id"], "verified_at": "2026-11-24T00:00:00+00:00"}
    return VerifiedFact.from_mapping(values)


def candidate(**changes):
    value = dict(index=0, type_key="mr_review", sources=(source(),), target=parse_target(URL, CONFIG),
                 first_fact=fact(), generation=1, source_fingerprint="fingerprint")
    value.update(changes)
    return PreparedCandidate(**value)


def test_first_matching_and_rule_selects_one_action():
    reviewer = rule(conditions=[*rule()["conditions"], {"kind": "operator_is_reviewer"}])
    addressed = rule(2, conditions=[*rule()["conditions"], {"kind": "addressed_to_operator"}])
    value = candidate()
    assert evaluate(value, [reviewer, addressed]).rule_id == reviewer["id"]
    addressed_value = replace(value, sources=(source(mention_ids=("message-operator",)),), first_fact=fact(reviewers=[]))
    assert evaluate(addressed_value, [reviewer, addressed]).rule_id == addressed["id"]
    assert evaluate(replace(value, first_fact=fact(reviewers=[])), [reviewer, addressed]).action == "push_for_confirmation"
    assert evaluate(replace(value, flagged=True), [reviewer]).reason == "flagged"
    recording = rule(action="record_only", project_id=None, template_key=None, conditions=[])
    decision = evaluate(value, [recording, reviewer])
    assert decision.action == "record_only"
    assert value.state == "pending"
    assert decision.rule_id == recording["id"]


def test_named_review_group_needs_no_reviewer_or_mention():
    named = rule(scope={"kind": "named_chats", "chats": [{"source_id": "messages-one", "chat_id": "review-room"}]})
    value = candidate(first_fact=fact(reviewers=[]))
    assert evaluate(value, [named]).action == "create_task"
    for bad in [source(chat_id="same-display-name"), source(source_id="different-source")]:
        assert evaluate(replace(value, sources=(bad,)), [named]).action == "push_for_confirmation"
    assert evaluate(replace(value, sources=(source(), source(2, chat_id="outside-room"))), [named]).action == "push_for_confirmation"


def test_mr_target_and_source_authorization_are_not_combined():
    addressed = rule(conditions=[*rule()["conditions"], {"kind": "addressed_to_operator"},
                                {"kind": "sender_in_allowlist", "sender_ids": ["allowed-sender"]}])
    # A request's sender and structured mention must both belong to that same linked request.
    mixed = candidate(sources=(source(sender_id="allowed-sender"), source(2, mention_ids=("message-operator",))))
    assert evaluate(mixed, [addressed]).action == "push_for_confirmation"
    good = candidate(sources=(source(sender_id="allowed-sender", mention_ids=("message-operator",)),))
    assert evaluate(good, [addressed]).action == "create_task"
    assert evaluate(replace(good, sources=(source(sender_id="allowed-sender", mention_ids=("all",)),)), [addressed]).action == "push_for_confirmation"


def test_discovery_attention_only_cannot_authorize():
    value = candidate(sources=(), first_fact=fact(reviewers=[]))
    reviewer = rule(conditions=[*rule()["conditions"], {"kind": "operator_is_reviewer"}])
    assert evaluate(value, [reviewer]).action == "push_for_confirmation"
    assert evaluate(replace(value, first_fact=fact()), [reviewer]).action == "create_task"
    for condition in [{"kind": "addressed_to_operator"}, {"kind": "sender_in_allowlist", "sender_ids": ["requester-one"]}]:
        assert evaluate(value, [rule(conditions=[*rule()["conditions"], condition])]).action == "push_for_confirmation"
    assert evaluate(replace(value, first_fact=fact(repository="denied")), [reviewer]).action == "push_for_confirmation"


def test_unavailable_verification_stops_rule_search():
    first = rule(conditions=[*rule()["conditions"], {"kind": "operator_is_reviewer"}])
    later = rule(2, action="record_only", project_id=None, template_key=None, conditions=[])
    for reason in ["adapter_missing", "verification_timeout", "authentication_failed", "cannot_decide"]:
        unavailable = VerificationUnavailable(reason)
        decision = evaluate(candidate(first_fact=unavailable), [first, later])
        assert decision.action == "push_for_confirmation" and decision.reason == reason
        assert decision.selection == "fallback" and decision.rule_id is None
        assert decision.conditions[-1]["result"] == "unavailable"
    assert evaluate(candidate(first_fact=fact(state="closed")), [first, later]).action == "record_only"
    missing = VerifiedFact(parse_target(URL, CONFIG), False)
    decision = evaluate(candidate(first_fact=missing), [first, later])
    assert decision.action == "record_only" and decision.rule_id == later["id"]
    assert decision.conditions[0]["reason"] == "mr_not_found"
    assert decision.attempts[0]["reason"] == "mr_missing"


def test_prepare_uses_only_cited_event_urls_and_preserves_first_fact(tmp_path):
    async def check():
        config = adapter(tmp_path, f"print({json.dumps(response())!r})")
        events = {1: {"seq": 1, "source_id": "messages-one", "chat_id": "review-room", "chat_kind": "group",
                      "sender_id": "requester-one", "mention_ids": [], "operator_id": "message-operator", "text": URL},
                  2: {"seq": 2, "source_id": "messages-one", "chat_id": "other-room", "sender_id": "allowed-sender",
                      "mention_ids": ["message-operator"], "operator_id": "message-operator", "text": URL.replace("/12", "/13")}}
        parsed = Parsed("items", items=[{"type": "mr_review", "sources": [1], "title": "malicious title", "mr_url": "https://evil.test"}])
        prepared = await prepare_message_candidates(parsed, events, config, generation=3)
        item = prepared.candidates[0]
        assert item.target.mr_url == URL and item.first_fact.revision == "revision-a"
        assert [ref.seq for ref in item.sources] == [1]
        assert "malicious title" not in json.dumps(item.to_dict())
        assert PreparedCandidate.from_mapping(item.to_dict()) == item
        changed = {**events, 1: {**events[1], "mention_ids": ["message-operator"]}}
        second = await prepare_message_candidates(parsed, changed, config, generation=3)
        assert second.candidates[0].source_fingerprint != item.source_fingerprint
        assert prepared.generation == 3
    asyncio.run(check())


def test_prepare_multiple_or_missing_targets_never_selects_first(tmp_path):
    async def check():
        config = adapter(tmp_path, f"print({json.dumps(response())!r})")
        parsed = Parsed("items", items=[{"type": "mr_review", "sources": [1]}])
        for text in [URL + " " + URL.replace("/12", "/13"), "a reviewer is mentioned but no link"]:
            result = await prepare_message_candidates(parsed, {1: {"seq": 1, "text": text}}, config)
            assert result.candidates[0].reason == "ambiguous_target"
            assert evaluate(result.candidates[0], [rule()]).action == "push_for_confirmation"
        duplicated = await prepare_message_candidates(parsed, {1: {"seq": 1, "text": URL + " " + URL}}, config)
        assert duplicated.candidates[0].target.mr_url == URL
    asyncio.run(check())


def test_explicit_credentials_are_the_only_secrets_passed_to_adapter(tmp_path, monkeypatch):
    async def check():
        capture = tmp_path / "credential-capture.json"
        source_code = (
            "import os,json\nfrom pathlib import Path\n"
            f"Path({str(capture)!r}).write_text(json.dumps(dict(os.environ)))\n"
            f"print({json.dumps(response())!r})\n"
        )
        config = {**adapter(tmp_path, source_code), "credential_env": {"READ_TOKEN": "secret:review-readonly"},
                  "env": {"LANG": "C"}}
        monkeypatch.setenv("OTHER_SECRET", "unrelated-secret")
        result = await verify(URL, config, credentials={"secret:review-readonly": "synthetic-readonly-secret"})
        assert isinstance(result, VerifiedFact)
        observed = json.loads(capture.read_text())
        assert observed["READ_TOKEN"] == "synthetic-readonly-secret"
        assert "OTHER_SECRET" not in observed
    asyncio.run(check())


def test_prepare_uses_structured_link_destination_instead_of_display_text(tmp_path):
    async def check():
        config = adapter(tmp_path, f"print({json.dumps(response())!r})")
        parsed = Parsed("items", items=[{"type": "mr_review", "sources": [1]}])
        event = {"seq": 1, "text": URL, "body": {"content": [[
            {"tag": "a", "href": "https://evil.test/mr/12", "text": URL}]]}}
        prepared = await prepare_message_candidates(parsed, {1: event}, config)
        assert prepared.candidates[0].target is None
        assert prepared.candidates[0].reason == "ambiguous_target"
        assert "evil.test" not in json.dumps(prepared.to_dict())
        event["body"]["content"][0][0]["href"] = URL
        good = await prepare_message_candidates(parsed, {1: event}, config)
        assert good.candidates[0].first_fact.revision == "revision-a"
    asyncio.run(check())


def test_discovery_cannot_use_an_all_scope_rule_without_reviewer_condition():
    assert evaluate(candidate(sources=()), [rule()]).action == "push_for_confirmation"
    assert evaluate(candidate(), [rule()]).action == "create_task"


def test_bootstrap_credentials_are_shared_by_prepare_and_verify(tmp_path):
    from looper.agents.intent_rule_verifier import configure_credentials
    async def check():
        config = {**adapter(tmp_path, f"print({json.dumps(response())!r})"),
                  "credential_env": {"READ_TOKEN": "secret:review-readonly"}}
        configure_credentials({"secret:review-readonly": "synthetic-readonly-secret"})
        try:
            assert isinstance(await verify(URL, config), VerifiedFact)
            parsed = Parsed("items", items=[{"type": "mr_review", "sources": [1]}])
            prepared = await prepare_message_candidates(parsed, {1: {"seq":1, "text":URL}}, config)
            assert isinstance(prepared.candidates[0].first_fact, VerifiedFact)
            assert (await verify(URL, config, credentials={})).reason == "adapter_missing"
        finally:
            configure_credentials({})
        assert (await verify(URL, config)).reason == "adapter_missing"
    asyncio.run(check())


def test_record_only_precedes_adapter_dependent_conditions_when_unconfigured():
    unavailable = candidate(target=None, first_fact=VerificationUnavailable("adapter_missing"), reason="adapter_missing")
    recording = rule(action="record_only", project_id=None, template_key=None, conditions=[])
    automatic = rule(2)
    assert evaluate(unavailable, [recording, automatic]).action == "record_only"
    decision = evaluate(unavailable, [automatic, {**recording, "position":3}])
    assert decision.action == "push_for_confirmation" and decision.reason == "adapter_missing"
    for reason in ("ambiguous_target", "source_revoked"):
        assert evaluate(replace(unavailable, reason=reason), [recording]).action == "push_for_confirmation"
    assert evaluate(replace(unavailable, flagged=True), [recording]).reason == "flagged"


def test_raw_url_tokenization_preserves_valid_route_identifier_suffixes(tmp_path):
    async def check():
        dotted_url = URL + "."
        config = adapter(tmp_path, f"print({json.dumps(response(mr_id='12.', canonical_url=dotted_url))!r})")
        parsed = Parsed("items", items=[{"type":"mr_review", "sources":[1]}])
        prepared = await prepare_message_candidates(parsed, {1:{"seq":1,"text":"Review " + dotted_url}}, config)
        assert prepared.candidates[0].target.mr_url == dotted_url
        assert prepared.candidates[0].first_fact.mr_id == "12."
    asyncio.run(check())


@pytest.mark.parametrize("exit_code", [0, 2])
def test_normal_exit_terminates_detached_output_children(tmp_path, exit_code):
    async def check():
        ready = tmp_path / "child-ready"
        marker = tmp_path / "child-survived"
        child = (f"from pathlib import Path; import time; Path({str(ready)!r}).touch(); "
                 f"time.sleep(0.8); Path({str(marker)!r}).touch()")
        source = (
            "import subprocess,sys,time\nfrom pathlib import Path\n"
            f"subprocess.Popen([sys.executable,'-c',{child!r}], "
            "stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)\n"
            f"while not Path({str(ready)!r}).exists(): time.sleep(0.01)\n"
            f"print({json.dumps(response())!r})\nsys.exit({exit_code})"
        )
        result = await verify(URL, adapter(tmp_path, source))
        assert ready.exists()
        if exit_code == 0:
            assert isinstance(result, VerifiedFact)
        else:
            assert result.reason == "verification_exit"
        await asyncio.sleep(1)
        assert not marker.exists()
    asyncio.run(check())


def test_relative_executable_is_never_resolved_through_path(tmp_path):
    async def check():
        config = adapter(tmp_path, f"print({json.dumps(response())!r})")
        config.update(executable=Path(sys.executable).name, env={"PATH": str(Path(sys.executable).parent)})
        assert (await verify(URL, config)).reason == "adapter_missing"
    asyncio.run(check())
