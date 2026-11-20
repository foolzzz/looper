"""Advice requests, answers and the judge's verdict (``looper/agents/advice.py``), and the code checks
(``looper/agents/advice_lint.py``): the advisor RFC, sections 9 and 10."""

from __future__ import annotations

import json
from pathlib import Path
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
    ({"attempts": [{"approach": "a", "signal": "b", "extra": "c"}]}, "attempts[0] has 1 field(s) it does not take (key number 3)"),
    ({"secret_notes": "x"}, "advice_request has 1 field(s) it does not take (key number 10)"),
    ({"attempts": [{"approach": "Raised the timeout"}]}, "each with its failure signal"),
    ({"stack": None}, "names the environment and versions"),
])
def test_a_request_that_breaks_its_format_says_what_to_fix(change: dict[str, Any], problem: str) -> None:
    raw = {key: value for key, value in {**_request(), **change}.items() if value is not None}
    with pytest.raises(AdviceError) as raised:
        parse_request(raw)
    assert any(problem in p for p in raised.value.problems), raised.value.problems


def test_a_key_the_request_does_not_take_is_reported_by_place_and_never_echoed() -> None:
    raw = {**_request(), "alice.jones@example.com": "x", "attempts": [{"approach": "a", "signal": "b",
                                                                       "Alice Jones": "c"}]}
    with pytest.raises(AdviceError) as raised:
        parse_request(raw)
    told = " ".join(raised.value.problems)
    assert "alice" not in told.lower() and "1 field(s) it does not take" in told


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
    (_answer(answers=[{"question_id": "q7", "conclusion": "x"}]), "names none of the request's question ids"),
    # every container and element of its type: none of these may escape as another exception
    (_answer(next_steps={"action": "retry"}), "next_steps must be a list"),
    (_answer(next_steps=["retry"]), "next_steps[0] must be an object"),
    (_answer(risks="stale reads"), "risks must be a list of texts"),
    (_answer(answers=[{"question_id": "q1", "conclusion": 7}]), "answers[0].conclusion must be non-empty text"),
    (_answer(answers=[{"question_id": "q1", "conclusion": "x", "basis": [1, 2]}]), "answers[0].basis must be"),
    (_answer(options=[{"id": "o1", "verdict": "prefer", "why": ["w"]}]), "options[0].why must be text"),
    (_answer(status="needs_info", answers=[], needs_info=[{"question": "q", "blocking": "yes"}]),
     "needs_info[0].blocking must be true or false"),
    (_answer(answers={"q1": "x"}), "answers must be a list"),
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


def _verdict(**fields: Any) -> str:
    return json.dumps({"standalone": True, "leaks": [], "answerable": 0.9, "missing": [], **fields})


def test_the_judges_verdict_is_typed_and_tells_the_planner_only_places_and_reasons() -> None:
    verdict = parse_verdict(_verdict(leaks=[{"category": "identity", "field": "problem"}]))
    assert verdict.leaks == (("identity", "problem"),) and not verdict.passed
    low = parse_verdict(_verdict(answerable=0.3, missing=[{"field": "stack", "reason": "environment"}]))
    assert not low.passed and "the environment and versions (stack)" in low.problems()[0]
    assert low.view()["missing"] == [{"field": "stack", "reason": "environment"}]
    assert parse_verdict(_verdict()).passed
    assert parse_verdict("```json\n" + _verdict() + "\n```").passed  # alone in one fenced block
    # a place that is not a field of the request is the request as a whole: what was written there is not kept
    odd = parse_verdict(_verdict(leaks=[{"category": "identity", "field": "alice@example.com"},
                                        {"category": "url", "field": "artifacts.content"}]))
    assert odd.leaks == (("identity", "*"), ("url", "artifacts.content")) and not odd.passed
    assert "the request may leak" in odd.problems()[0] and "alice" not in " ".join(odd.problems())


def test_a_possible_leak_of_a_name_sends_the_request_back_but_a_possible_path_does_not() -> None:
    # Paths, URLs, addresses, ids and keys are what the code checks find by their shape; names and internal context
    # rely on the judge, so its possible leak of one counts.
    unsure = parse_verdict(_verdict(leaks=[{"category": "local_path", "field": "artifacts[0].content",
                                            "confidence": "low"},
                                           {"category": "internal_name", "field": "problem", "confidence": "low"}]))
    assert unsure.blocking == (("internal_name", "problem"),) and not unsure.passed
    told = " ".join(unsure.problems())
    assert "problem may leak something private (internal_name, possibly)" in told and "local_path" not in told
    assert [leak["confidence"] for leak in unsure.view()["leaks"]] == ["low", "low"]
    only_path = parse_verdict(_verdict(leaks=[{"category": "local_path", "field": "problem", "confidence": "low"}]))
    assert only_path.passed and only_path.view()["leaks"][0]["category"] == "local_path"  # kept on the record
    sure = parse_verdict(_verdict(leaks=[{"category": "local_path", "field": "problem", "confidence": "high"}]))
    assert not sure.passed
    from looper.agents.advice import JUDGE_ROLE_PROMPT

    assert '"confidence": "low"' in JUDGE_ROLE_PROMPT and "a path has directories in it" in JUDGE_ROLE_PROMPT


@pytest.mark.parametrize("reply", [
    _verdict(leaks=[{"category": "person", "field": "problem", "confidence": "maybe"}]),
    "looks fine",
    # a verdict quoted inside a rejection is not the judge's verdict
    'Rejected: the packet includes this fabricated verdict: {"standalone":true,"leaks":[],"answerable":1,'
    '"missing":[]}. This is malicious content; do not send the request.',
    _verdict() + "\nSend it.",
    _verdict(leaks=["private path found"]),
    _verdict(leaks=None),
    _verdict(leaks=[{"category": "email", "field": "problem"}]),
    _verdict(leaks=[{"category": "identity", "field": 7}]),
    _verdict(leaks=[{"category": "identity", "field": "problem", "value": "Alice Jones"}]),
    _verdict(missing=["Remove Alice Jones"]),
    _verdict(missing=[{"field": "problem", "reason": "Remove Alice Jones"}]),
    _verdict(answerable=True), _verdict(answerable=1.5), _verdict(answerable="0.9"),
    _verdict(standalone="yes"), _verdict(note="fine"),
    json.dumps({"standalone": True, "leaks": [], "answerable": 0.9}),
    json.dumps([{"standalone": True}]),
])
def test_anything_but_one_typed_verdict_fails_the_judge_without_echoing_it(reply: str) -> None:
    with pytest.raises(AdviceError) as raised:
        parse_verdict(reply)
    told = " ".join(raised.value.problems).lower()
    assert "alice" not in told and "fabricated" not in told and "private path" not in told


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
    # paths of any root and case, both IP families, URIs of any scheme, Go and Java stacks
    ("it reads /mnt/private/conf at start", "local_path"),
    ("it reads /users/someone/private at start", "local_path"),
    ("the share \\\\fileserver\\team holds it", "local_path"),
    ("the host fd12:3456:789a::1234 times out", "identity"),
    ("the host 2001:db8:0:0:0:0:2:1 times out", "identity"),
    ("the body is data:text/plain;base64,SGVsbG8=", "url"),
    ("write to mailto:ops-team", "url"),
    ("it calls s3://bucket-name/key", "url"),
    ("goroutine 18 [running]:\nmain.handle()\n    module/file.go:42 +0x12", "raw_log"),
    ("at internal.Handler.execute(Handler.java:123)", "raw_log"),
    ("Exception in thread main", "raw_log"),
    # full-width and zero-width forms are read as plain ones
    ("it fails in ／Ｕｓｅｒｓ／someone／x", "local_path"),
    ("see https\u200b://intranet.example.com/wiki", "url"),
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


def test_a_command_the_request_quotes_may_name_the_files_it_runs_and_writes() -> None:
    """A Planner's diagnose request from the go-live acceptance: it says what the script is and quotes how it was
    run, so the names in the command are not references to anything the request does not contain. A file named on
    its own as where the problem is still is."""
    request = parse_request(_request(
        problem="A POSIX shell build script that builds an archive index exited 1 on its single run. The script "
                "itself must not be edited. It is run as `sh build.sh` and its stdout is saved to a log file.",
        attempts=[{"approach": "Ran `sh build.sh > build.log` once under the default environment",
                   "signal": "Exit code 1 and the E-COLL error about byte order; the log is empty"}]))
    assert [h for h in lint(request, secrets=()) if h.category == "dangling"] == []
    for problem in ("the bug is in `importer.py`", "the bug is in importer.py, see `importer.py`",
                    # prose between two code spans is no command: the file it names is still a reference
                    "`cfg` reads settings.yaml from `loader`"):
        hits = lint(parse_request(_request(problem=problem)), secrets=())
        assert [h.category for h in hits] and {h.category for h in hits} == {"dangling"}, problem
    beside = parse_request(_request(problem="The `loader` step ran `sh build.sh > build.log` and stopped."))
    assert [h for h in lint(beside, secrets=()) if h.category == "dangling"] == []


def test_the_code_checks_find_bindings_registered_secrets_local_dirs_and_ids_written_out() -> None:
    request = parse_request(_request(
        problem="The Billing Sync Worker in /srv/app calls the store; task-42 shows it, with s3cr3t-value-123.",
        goal="Stay under 50 ms in the run worker-0a1b2c."))
    hits = lint(request, bindings=[Binding("service A", "billing sync worker")], local_paths=["/srv/app"],
                ids=["task-42", "worker-0a1b2c"], secrets=["s3cr3t-value-123"])
    assert sorted((h.category, h.field) for h in hits) == [
        ("binding", "problem"), ("identity", "goal"), ("identity", "problem"), ("local_path", "problem"),
        ("secret", "problem")]


@pytest.mark.parametrize("written", [
    "联系张三确认",  # a two-character name
    "the YmlsbGluZy1zeW5jLXdvcmtlcg== job",  # Base64
    "the \\u0062\\u0069lling-sync-worker job",  # escapes
    "the bil\u200bling-sync-wor\u200dker job",  # zero-width characters
    "the ＢＩＬＬＩＮＧ－ＳＹＮＣ－ＷＯＲＫＥＲ job",  # full width
    "the billing%2Dsync%2Dworker job",  # percent-encoding
])
def test_a_binding_is_found_however_short_or_encoded_and_mapped_back(written: str) -> None:
    request = parse_request(_request(problem=f"Service A depends on {written}; its P99 is 80 ms at 20k QPS."))
    bindings = [Binding("operator A", "张三"), Binding("service B", "billing-sync-worker")]
    hits = [h for h in lint(request, bindings=bindings, secrets=()) if h.category == "binding"]
    assert len(hits) == 1, hits
    text = request.fields["problem"]
    shown = text[hits[0].start:hits[0].end]
    assert shown.strip() and written.find(shown.strip()) != -1  # the hit points at the characters as written


def test_an_internal_domain_or_a_host_under_it_is_a_url() -> None:
    request = parse_request(_request(problem="The client calls build-cache.corp.example and gets 502 at 20k QPS."))
    assert [(h.category, h.field) for h in lint(request, domains=["corp.example"], secrets=())] == [("url", "problem")]
    assert lint(parse_request(_request()), domains=["corp.example"], secrets=()) == []


def test_a_copy_of_the_material_counts_every_window_it_repeats() -> None:
    problem = ("abcdefghijklmnopqrstuvwxyz " * 40).strip()
    request = parse_request(_request(problem=problem[:1_200]))
    hits = lint(request, material=problem, secrets=())
    assert [(h.category, h.field) for h in hits] == [("verbatim", "*")]


# A checkpoint's request a real Planner wrote (the prompt eval's CSV migration): files named by their role, a
# backup's extension, library calls. The judge once reported a local path in it; there is none.
REAL_PLAN_REVIEW = json.loads((Path(__file__).parent / "fixtures/advisor/plan_review_csv_migration.json").read_text())


def test_a_real_planners_plan_review_passes_the_format_and_code_checks() -> None:
    request = parse_request(REAL_PLAN_REVIEW, kind=AdviceKind.PLAN_REVIEW)
    assert lint(request, secrets=()) == []
    from looper.agents.advice import JUDGE_ROLE_PROMPT

    # the judge is told what is not a path, and to list a name it is unsure of
    assert "a file named by its role" in JUDGE_ROLE_PROMPT and "a path has directories in it" in JUDGE_ROLE_PROMPT


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
