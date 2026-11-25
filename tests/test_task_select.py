"""Pure task selection: one case per normalize, reference, token and rank rule."""

import pytest

from looper.agents.task_select import (
    Candidate, MrRef, deepest_paths, normalize, parse_mr, rank, text_refs, tokens,
)


@pytest.mark.parametrize("raw, expected", [
    ("ＡＢＣ１２", "abc12"),
    ("AbC", "abc"),
    ("a   b\tc", "a b c"),
    ('"hello"', "hello"),
    ("'hello'", "hello"),
    ("「hello」", "hello"),
    ("“hello”", "hello"),
    ("‘hello’", "hello"),
    ('"「hello」"', "「hello」"),
    ('""', ""),
])
def test_normalize(raw, expected):
    assert normalize(raw) == expected


@pytest.mark.parametrize("raw, expected", [
    ("!12", MrRef(12)),
    ("#12", MrRef(12)),
    ("MR   12", MrRef(12)),
    ("pr 12", MrRef(12)),
    ("12", MrRef(12)),
    ("1234567", MrRef(1234567)),
    ("https://github.com/owner/name/pull/12", MrRef(12, "owner/name")),
    ("https://github.com/Owner/Name/pull/12?w=1", MrRef(12, "owner/name")),
    ("https://gitlab.example/group/sub/name/-/merge_requests/12", MrRef(12, "group/sub/name")),
    ("https://gitlab.example/group/sub/name/merge_requests/12", MrRef(12, "group/sub/name")),
    ("12 34", None),
    ("abc12", None),
    ("12345678", None),
    ("https://github.com/owner/name/issues/12", None),
    ("https://github.com/owner/name/pull/12345678", None),
    ('"「12」"', None),
    ("https://[/pull/12", None),
])
def test_parse_mr(raw, expected):
    assert parse_mr(raw) == expected


@pytest.mark.parametrize("text, expected", [
    ("please review !12 today", {MrRef(12)}),
    ("see #12 now", {MrRef(12)}),
    ("open mr 12 please", {MrRef(12)}),
    ("open pr 12 please", {MrRef(12)}),
    ("see https://github.com/owner/name/pull/34 thanks", {MrRef(34, "owner/name")}),
    ("see https://gitlab.example/group/sub/name/-/merge_requests/34 thanks", {MrRef(34, "group/sub/name")}),
    ("the count is 99 today", set()),
    ("amr 12 stays a word", set()),
    ("see https://example.com/a#5 please", set()),
    ("see ！１２ now", {MrRef(12)}),
    ("review !12 https://[/pull/12", {MrRef(12)}),
    ("only https://[/pull/12 here", set()),
])
def test_text_refs(text, expected):
    assert text_refs(text) == expected


@pytest.mark.parametrize("raw, expected", [
    ("fix login bug", ["fix", "login", "bug"]),
    ("修复登录", ["修复登录"]),
    ("修复 bug 登录", ["修复", "bug", "登录"]),
])
def test_tokens(raw, expected):
    assert tokens(raw) == expected


def test_rank_orders_by_tier_then_open_then_activity_then_id():
    older, newer = 10.0, 20.0
    candidates = [
        Candidate("b", tier=1, ended=False, activity=newer),
        Candidate("a", tier=0, ended=True, activity=newer),
        Candidate("d", tier=0, ended=False, activity=older),
        Candidate("c", tier=0, ended=False, activity=older),
    ]
    assert [item.task_id for item in rank(candidates)] == ["c", "d", "a", "b"]


@pytest.mark.parametrize("cwd, paths, expected", [
    ("/proj/sub/leaf", ["/proj", "/proj/sub", "/proj-other"], {"/proj/sub"}),
    ("/proj-other", ["/proj"], set()),
    ("/proj", ["/proj", "/proj/"], {"/proj"}),
])
def test_deepest_directory_contains_cwd(cwd, paths, expected):
    assert deepest_paths(cwd, paths) == expected


def test_select_is_not_a_generated_client_command():
    from looper.agents import api_v1
    from looper.loopercli import generated_operations

    operation = next(item for item in api_v1.operations() if item.operation_id == "tasks.select")
    assert operation.cli is False and operation.tool is None
    pairs = {(head, tail) for head, tail, *_rest in generated_operations(api_v1.document())}
    assert ("tasks", "select") not in pairs
