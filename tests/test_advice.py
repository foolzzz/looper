"""Advice requests, answers and the judge's verdict (``looper/agents/advice.py``), and the code checks
(``looper/agents/advice_lint.py``): the advisor RFC, sections 9 and 10."""

from __future__ import annotations

import json
from typing import Any

import pytest

from looper.agents.advice import (
    REQUEST_MAX_CHARS, AdviceError, AdviceKind, AdviceStatus, Binding, parse_bindings, parse_request,
    parse_response, parse_verdict,
)
from looper.agents.advice_lint import describe, lint


def _request(kind: str = "diagnose", **fields: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "v": "advice_request/1", "kind": kind,
        "problem": "Service A calls key-value store B synchronously on every request; the P99 budget is 50 ms and "
                   "the observed P99 is 80 to 120 ms at about 20k QPS, with no fallback when B times out.",
        "goal": "Bring the P99 under 50 ms without losing reads.",
        "acceptance": ["P99 under 50 ms at 20k QPS"],
        "stack": ["Go 1.23", "a Redis-compatible store"],
        "attempts": [{"approach": "Raised the client timeout to 200 ms", "signal": "P99 grew to 210 ms",
                      "learned": "the timeout is not the cause"}],
        "artifacts": [{"kind": "pseudocode", "lang": "go",
                       "content": "v, err := store.Get(ctx, key)\nif err != nil { return err }"}],
        "questions": [{"id": "q1", "text": "Which mitigation should come first?"}],
    }
    if kind == "design_choice":
        base.update(constraints=["reads must stay consistent within 1 s"],
                    options=[{"id": "o1", "summary": "a local cache", "pros": "fast", "cons": "stale reads"},
                             {"id": "o2", "summary": "hedged reads", "pros": "simple", "cons": "more load"}])
    if kind == "plan_review":
        base.update(constraints=["the migration is reversible: the old column stays until step 3"],
                    artifacts=[{"kind": "interface", "content": "step 1: add column c2\nstep 2: dual write"}])
    return {**base, **fields}


@pytest.mark.parametrize("kind", [k.value for k in AdviceKind])
def test_a_request_of_each_kind_with_what_it_needs_passes_the_format_check(kind: str) -> None:
    raw = _request(kind)
    if kind == "unblock":
        raw.pop("artifacts")
    request = parse_request(raw)
    assert request.kind is AdviceKind(kind) and request.question_ids == ("q1",)
    assert json.loads(request.text) == raw


@pytest.mark.parametrize(("change", "problem"), [
    ({"v": "advice_request/2"}, "v must be"),
    ({"kind": "explain"}, "kind must be one of"),
    ({"problem": ""}, "problem must be non-empty"),
    ({"problem": "x" * 1_201}, "problem has 1201 characters"),
    ({"questions": []}, "questions is required"),
    ({"questions": [{"id": "q1", "text": "a"}, {"id": "q1", "text": "b"}]}, "ids must be unique"),
    ({"questions": [{"id": "Question One", "text": "a"}]}, "short lower-case names"),
    ({"questions": [{"id": f"q{i}", "text": "a"} for i in range(4)]}, "questions has 4 items"),
    ({"artifacts": [{"kind": "screenshot", "content": "x"}]}, "artifacts[0].kind must be one of"),
    ({"attempts": [{"approach": "a", "signal": "b", "extra": "c"}]}, "has no field 'extra'"),
    ({"secret_notes": "x"}, "has no field 'secret_notes'"),
    ({"attempts": [{"approach": "Raised the timeout"}]}, "each with its failure signal"),
    ({"stack": None}, "names the environment and versions"),
])
def test_a_request_that_breaks_its_format_says_what_to_fix(change: dict[str, Any], problem: str) -> None:
    raw = {key: value for key, value in {**_request(), **change}.items() if value is not None}
    with pytest.raises(AdviceError) as raised:
        parse_request(raw)
    assert any(problem in p for p in raised.value.problems), raised.value.problems


def test_a_checkpoint_takes_only_a_plan_review_and_a_long_request_is_refused() -> None:
    with pytest.raises(AdviceError, match="kind must be plan_review here"):
        parse_request(_request("diagnose"), kind=AdviceKind.PLAN_REVIEW)
    assert parse_request(_request("plan_review"), kind=AdviceKind.PLAN_REVIEW).kind is AdviceKind.PLAN_REVIEW
    long = _request(constraints=["c" * 400] * 8, acceptance=["a" * 400] * 5,
                    artifacts=[{"kind": "snippet", "content": "x" * 2_000}] * 3,
                    attempts=[{"approach": "a" * 600, "signal": "s" * 600, "learned": "l" * 600}] * 5)
    with pytest.raises(AdviceError, match=f"longer than {REQUEST_MAX_CHARS}"):
        parse_request(long)


def test_bindings_are_aliases_with_what_they_stand_for() -> None:
    assert parse_bindings([{"alias": "service A", "refers_to": "the billing sync worker"}]) == (
        Binding("service A", "the billing sync worker"),)
    assert parse_bindings(None) == ()
    with pytest.raises(AdviceError):
        parse_bindings([{"alias": "service A"}])


def _answer(**fields: Any) -> str:
    body = {"status": "answered",
            "answers": [{"question_id": "q1", "conclusion": "Add a bounded local cache first.",
                         "basis": ["most reads repeat", "the store's tail is the cause"], "confidence": "med"}],
            "next_steps": [{"id": "s1", "action": "cache hot keys for 1 s", "verify": "P99 under 50 ms"}],
            "risks": ["stale reads"], **fields}
    return "Here is my advice.\n```json\n" + json.dumps(body) + "\n```"


def test_an_answer_that_fits_its_request_is_parsed_and_clipped() -> None:
    request = parse_request(_request("design_choice"))
    response = parse_response(_answer(options=[{"id": "o1", "verdict": "prefer", "why": "w"},
                                               {"id": "o9", "verdict": "prefer", "why": "unknown option"}],
                                      extra="dropped"), request)
    assert response.status is AdviceStatus.ANSWERED
    assert [o["id"] for o in response.fields["options"]] == ["o1"] and "extra" not in response.fields
    assert response.fields["answers"][0]["basis"] == ["most reads repeat", "the store's tail is the cause"]


@pytest.mark.parametrize(("text", "problem"), [
    ("I would add a cache.", "no JSON object with a status"),
    (_answer(status="maybe"), "status must be one of"),
    (_answer(answers=[{"question_id": "q7", "conclusion": "x"}]), "names one of the request's question ids"),
    (_answer(answers=[]), "answers every question"),
    (_answer(status="needs_info", answers=[], needs_info=[]), "asks at least one question"),
])
def test_an_answer_that_does_not_fit_fails(text: str, problem: str) -> None:
    with pytest.raises(AdviceError) as raised:
        parse_response(text, parse_request(_request()))
    assert any(problem in p for p in raised.value.problems)


def test_a_needs_info_answer_carries_the_advisors_questions() -> None:
    response = parse_response(_answer(status="needs_info", answers=[],
                                      needs_info=[{"id": "n1", "question": "What is the hit rate?", "why": "sizing"}]),
                              parse_request(_request()))
    assert response.status is AdviceStatus.NEEDS_INFO
    assert response.needs_info == ({"id": "n1", "question": "What is the hit rate?", "why": "sizing",
                                    "blocking": True},)


def test_the_judges_verdict_keeps_only_categories_and_field_names() -> None:
    verdict = parse_verdict(json.dumps({"standalone": True, "answerable": 0.9, "missing": [],
                                        "leaks": [{"category": "identity", "field": "problem"},
                                                  {"category": "email", "field": "alice@example.com"}]}))
    assert verdict.leaks == (("identity", "problem"), ("other", "?")) and not verdict.passed
    assert not any("alice" in p for p in verdict.problems())
    low = parse_verdict(json.dumps({"standalone": True, "leaks": [], "answerable": 0.3, "missing": ["the QPS"]}))
    assert not low.passed and "the QPS" in low.problems()[0]
    assert parse_verdict(json.dumps({"standalone": True, "leaks": [], "answerable": 0.8})).passed
    with pytest.raises(AdviceError):
        parse_verdict("looks fine")


# --- Code checks ---


@pytest.mark.parametrize(("text", "category"), [
    ("the key sk-ant-api03-abcdefghijklmnopqrstuvwxyz0123", "secret"),
    ("token ghp_abcdefghijklmnopqrstuvwxyz0123456789", "secret"),
    ("-----BEGIN RSA PRIVATE KEY-----", "secret"),
    ("the value Zm9vYmFyYmF6cXV4MTIzNDU2Nzg5MGFiY2RlZmdoaWprbG1u", "secret"),
    ("it fails in /Users/someone/project/sync.go", "local_path"),
    ("the config at ~/.config/app.toml", "local_path"),
    ("see https://intranet.example.com/wiki/page", "url"),
    ("ask ops-lead@example.com", "identity"),
    ("the host 10.12.3.4 times out", "identity"),
    ("request 3f2a9c1b7d4e8f60a1b2 failed", "identity"),
    ("trace 123e4567-e89b-12d3-a456-426614174000", "identity"),
    ("2026-11-19 10:02:03 ERROR timeout", "raw_log"),
    ("Traceback (most recent call last):\n  File \"x\", line 3", "raw_log"),
    ("as mentioned above, the cache misses", "dangling"),
    ("见日志里的报错", "dangling"),
    ("the bug is in importer.py", "dangling"),
])
def test_each_code_check_finds_its_category_by_position_only(text: str, category: str) -> None:
    request = parse_request(_request(problem=text))
    hits = lint(request, secrets=())
    assert {(h.category, h.field) for h in hits} == {(category, "problem")}, hits
    hit = hits[0]
    assert 0 <= hit.start < hit.end <= len(text)
    told = " ".join(describe(hits))
    assert "problem (characters" in told and text[hit.start:hit.end] not in told


def test_the_code_checks_find_bindings_registered_secrets_local_dirs_and_ids_written_out() -> None:
    request = parse_request(_request(
        problem="The Billing Sync Worker in /srv/app calls the store; task-42 shows it, with s3cr3t-value-123.",
        goal="Stay under 50 ms in the run worker-0a1b2c."))
    hits = lint(request, bindings=[Binding("service A", "billing sync worker")], local_paths=["/srv/app"],
                ids=["task-42", "worker-0a1b2c"], secrets=["s3cr3t-value-123"])
    assert sorted((h.category, h.field) for h in hits) == [
        ("binding", "problem"), ("identity", "goal"), ("identity", "problem"), ("local_path", "problem"),
        ("secret", "problem")]


def test_a_clean_request_passes_and_a_file_its_artifact_shows_may_be_named() -> None:
    request = parse_request(_request(
        problem="The loop in cache.go reads every key twice; service A's P99 is 80 ms at 20k QPS, above 50 ms.",
        artifacts=[{"kind": "snippet", "lang": "go", "content": "// cache.go\nfor k := range keys { get(k); get(k) }"}]))
    assert lint(request, secrets=()) == []


def test_a_long_log_excerpt_and_a_copied_request_are_returned() -> None:
    excerpt = "\n".join(f"line {i}: retry" for i in range(25))
    request = parse_request(_request(artifacts=[{"kind": "log_excerpt", "content": excerpt}]))
    assert [h.category for h in lint(request, secrets=())] == ["raw_log"]
    copied = parse_request(_request())
    material = " ".join(text for _, text in copied.strings())
    hits = lint(copied, material=material, secrets=())
    assert [(h.category, h.field) for h in hits] == [("verbatim", "*")]
    assert describe(hits) == [describe(hits)[0]] and "the request as a whole" in describe(hits)[0]
